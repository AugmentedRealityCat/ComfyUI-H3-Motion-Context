"""Keep the join the pin established, take the refine everywhere else.

EXPERIMENTAL

WHY THIS EXISTS

Each chained clip continues from a copy of the previous clip, and every
copy drifts a little: the face polishes or ages, fine detail builds up,
and it compounds over the chain. A refine pass pulls each clip back: add
noise to the finished latent and denoise it again with the plain
reference conditioning, so the model re-reads the character sheet
instead of only copying the previous clip.

The catch is that refining moves the head off the pin, which reopens the
seam the pin exists to close. So hold the pinned frames exactly as
sampled, ramp into the refined version over the frames after them, and
take the refine neat from there on.

WHAT THE REFINE SHOULD BE

How strong the refine is decides everything, and H3's noise shift (12)
makes the usual image-model numbers misleading: a denoise of 0.55 on
H3 restarts at noise 0.94, nearly a full re-render, and 0.025 restarts
at 0.24. Measured over six-clip chains of the same scenes:

    0.55     face held, background redrawn, color up 18%
    0.025    background held, fine detail up 43%, face aged and harsh
    one step from the stock schedule's last level (about 0.63)
             face held, background held, color flat, the least detail
             build-up of any refine

A light refine adds detail because it runs entirely in the clean end of
the schedule, where the model adds fine texture every time it runs, and
a distilled model like FastH3 is not trained to work there at all.
Re-running FastH3's own last step stays inside what it knows.

So the recommended refine is one step: Split Sigmas on the main
BasicScheduler output (before any Sigmas Tail), step 7 for an 8 step
schedule, and its low_sigmas into the refine sampler. Plain Reference to
Video conditioning, not the Motion Context output, and its own random
seed.

WHY THE PICTURE BLENDS CLEANLY AND THE AUDIO DOES NOT

The video pin comes back at about 0.997 correlation, so the held frames
and the refined ones are near-identical renderings of the same moment.
Crossfading those produces no ghosting and no doubled motion.

The audio pin comes back at 0.88 at best, and on material without
dialogue the model latches onto whatever transient is in the pinned
second and stamps it forward into every clip. Blending two audio latents
that differ that much is not a crossfade in any acoustic sense over most
of a second. So audio defaults to the original, untouched.

WIRING

    sampler ─┬────────────────────────────────> Refine Blend (original)
             └─> refine sampler, one step ────> Refine Blend (refined)
                                                       |
                                                H3 Join Match
                                                       |
                                     Save Latent, VAE Decode, VAE Decode Audio

Join Match goes after this node, not before it, so it also sees the
detail the refine adds at the join. Moving it there cut the detail
added at the joins by about three quarters.

Save Latent and both decodes take the final output, or the mp4 will not
match the saved latent the next clip continues from.

THE FIRST CLIP

`clip_index` is driven by the Chain node, the same way Load and Save are:
the frontend finds this node by type inside their canvas group and mirrors
Load's index into it. Put this node in that group and it follows along.

At 0 there is no previous clip and so no join to protect: the whole thing
gets refined and the hold and ramp are skipped.

There is no wire for this. Load Latent's clip_index is a widget the chain
writes, not an output, and reading its latent output instead would not
work: an optional input is empty both when nothing is connected and when
it is connected to something that returned nothing, so clip 0 and an
unwired graph cannot be told apart.

Outside a chain group it stays wherever you set it, and 1 means every
clip is treated as a chained one.

`use_refine` defaults on. Switched off it does not merely ignore the
refined input: the input is lazy, so ComfyUI does not run the second
sampler at all and you do not pay for a refine you are not using.

BLEND GEOMETRY

Weights are per latent step, taken from each step's own first frame, so
`blend_frames` does not have to land on a step boundary. Note that 44
frames is not a whole number of steps: the step-aligned lengths near it
are 39 and 43.

AUTO RAMP

How long the ramp needs to be depends on how far the refine moved this
clip. On a still shot the refined frames sit almost on top of the
originals and a short ramp is invisible; on a moving or busy shot the
refine redraws more and a short ramp shows as a slide. With tuning on
auto (the default), the node measures that directly, in the frames
after the hold:

    how far the refined latent sits from the original, as a share of how
    much the original varies across the frame.

The gap picks the ramp from AUTO_RAMPS, and the ramp is shortened if it
would leave nothing taken from the refine at full strength. The log
names the gap and the ramp chosen on every clip. The gap bands are a
first guess, not yet calibrated on real renders: the logged gaps from a
still chain and a moving one are what will settle them.

On manual, blend_frames is used as set, exactly as before. On auto it
is ignored. The first clip has no ramp either way.
"""

import logging

_LOG = logging.getLogger("h3_motion_context")

FRAME_PER_TOKEN = (1, 4, 4, 4, 4)

# Auto ramp: (largest gap, ramp frames), smallest first; the last band
# takes everything above. First guesses, to be calibrated from the
# logged gaps of real still and moving chains.
AUTO_RAMPS = ((0.10, 13), (0.25, 22), (None, 39))


def nearest_valid(frames, span=3):
    """Frame counts either side of `frames` that land on a step boundary.

    Not just Motion Context's four window options: any count reachable by
    the 1,4,4,4,4 cycle works, which is 1, 5, 9, 13, 17, 18, 22, 26, 30
    and so on. Saying "the legal windows are 5, 22, 39, 56" would be
    wrong and would send someone hunting for a problem that is not there.
    """
    ok, t = [], 0
    while pixel_frames(t) <= frames + 40:
        ok.append(pixel_frames(t))
        t += 1
    below = [x for x in ok if x < frames][-span:]
    above = [x for x in ok if x > frames][:span]
    return below + above


def pixel_frames(t):
    return sum(FRAME_PER_TOKEN[k % 5] for k in range(int(t)))


def steps_for_frames(frames):
    t = 0
    while pixel_frames(t) < frames:
        t += 1
        if t > 4096:
            return None
    return t if pixel_frames(t) == frames else None


def repack(samples, video, sound):
    """Hand back the same kind of container that came in.

    Load Latent produces a plain list, deliberately, because only
    context_latent accepts it. The sampler produces
    comfy.nested_tensor.NestedTensor, and anything that decodes checks
    is_nested on it: a list reaches the audio VAE and fails with
    'list object has no attribute is_nested'. So match what arrived
    rather than always returning one or the other.
    """
    if isinstance(samples, (list, tuple)):
        return [video, sound]
    try:
        import comfy.nested_tensor
        return comfy.nested_tensor.NestedTensor((video, sound))
    except ImportError:
        return [video, sound]


def streams_of(latent, label):
    """The video and audio halves of an H3 AV latent.

    NestedTensor indexes by broadcasting into both tensors rather than
    selecting one, so unbind is the way to get the pair. A plain list
    turns up too, because that is what this pack's Load Latent hands to
    context_latent.
    """
    if not isinstance(latent, dict) or "samples" not in latent:
        raise ValueError("h3_motion_context: %s is not a latent, got %r"
                         % (label, type(latent)))
    samples = latent["samples"]
    if hasattr(samples, "unbind"):
        parts = list(samples.unbind())
    elif isinstance(samples, (tuple, list)):
        parts = list(samples)
    else:
        raise ValueError(
            "h3_motion_context: %s should be a MiniMax H3 AV latent (a "
            "nested video/audio pair), got %r" % (label, type(samples)))
    if len(parts) < 2:
        raise ValueError(
            "h3_motion_context: %s is not an H3 AV latent. It unpacked into "
            "%d stream(s), so it is an ordinary single-stream latent. Wire "
            "the H3 sampler output, not a stock latent." % (label, len(parts)))
    video, sound = parts[0], parts[1]
    raw_ndim = video.ndim
    if video.ndim == 4:
        video = video.unsqueeze(0)
    if video.ndim != 5:
        raise ValueError("h3_motion_context: %s video should be "
                         "[B,C,T,H,W], got %s" % (label, tuple(video.shape)))
    # A plain video latent with a batch of 2 unbinds into two 4d tensors,
    # and the second would be taken as the audio stream and blended as
    # though it were one. It is not detectable from the video side, since
    # both halves look like video, so check the sound side has no spatial
    # dimensions: H3 audio is [B,C,2,T].
    if sound.ndim > 4 or (sound.ndim == 4 and int(sound.shape[-2]) > 16):
        raise ValueError(
            "h3_motion_context: %s second stream is shaped %s, which is "
            "another picture rather than a soundtrack. This looks like a "
            "plain video latent with a batch of more than one, not an H3 "
            "AV pair." % (label, tuple(sound.shape)))
    return video, sound, raw_ndim


def weights(total_steps, hold_frames, blend_frames):
    """How much of the refined clip each latent step takes.

    Taken from the step's own first frame rather than its index, so the
    ramp means the same thing whatever the step lengths are, and
    blend_frames need not land on a step boundary.
    """
    out, frame = [], 0
    for k in range(total_steps):
        if frame < hold_frames:
            w = 0.0
        elif blend_frames <= 0:
            w = 1.0
        else:
            w = (frame - hold_frames) / float(blend_frames)
            w = 0.0 if w < 0.0 else (1.0 if w > 1.0 else w)
        out.append(w)
        frame += FRAME_PER_TOKEN[k % 5]
    return out


def refine_gap(o_vid, r_vid, start):
    """How far the refine moved this clip, in the steps from `start` on.

    The difference between refined and original as a share of how much
    the original varies across the frame, averaged over the batch and
    every step. None when there are no steps to measure.
    """
    o = o_vid[:, :, int(start):]
    r = r_vid[:, :, int(start):].to(device=o.device, dtype=o.dtype)
    if int(o.shape[2]) == 0:
        return None
    diff = r - o
    centred = o - o.mean(dim=(-2, -1), keepdim=True)
    num = float((diff * diff).mean().sqrt())
    den = float((centred * centred).mean().sqrt())
    return num / max(den, 1e-9)


def auto_ramp(gap, total_steps, hold_frames):
    """The ramp for this gap, shortened until some of the clip is neat."""
    want = AUTO_RAMPS[-1][1]
    for limit, frames in AUTO_RAMPS:
        if limit is None or (gap is not None and gap <= limit):
            want = frames
            break
    options = sorted({f for _, f in AUTO_RAMPS if f <= want}, reverse=True)
    for frames in options:
        if any(w >= 1.0 for w in weights(total_steps, hold_frames, frames)):
            return want, frames
    return want, options[-1]


class MiniMaxH3RefineBlend:
    """Hold the pinned frames, ramp into a refined version of the clip."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "original": ("LATENT", {
                    "tooltip": "Straight from the sampler, untouched. Its "
                               "head is what matches the previous clip."}),
                "hold_frames": ("INT", {
                    "default": 22, "min": 0, "max": 512, "step": 1,
                    "tooltip": "Kept exactly as sampled: the frames the "
                               "pin established. Match the Motion Context "
                               "context_length, or drive it from the trim "
                               "so the two cannot drift apart. Has to be a "
                               "whole number of latent steps (5, 22, 39, "
                               "56)."}),
                "blend_frames": ("INT", {
                    "default": 22, "min": 0, "max": 512, "step": 1,
                    "tooltip": "Manual tuning only. Frames to ramp from "
                               "held to refined after the hold. 0 is a hard "
                               "cut, which will show as a pop."}),
                "audio": (("original", "refined"), {
                    "default": "original",
                    "tooltip": "Leave on original. The audio pin returns at "
                               "0.88 at best and stamps transients forward "
                               "on material without dialogue, so refining "
                               "it is a separate experiment."}),
                "use_refine": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Off ignores the refined input and passes "
                               "the original through, so you can A/B the "
                               "refine without unwiring the second "
                               "sampler."}),
                # Last on purpose. ComfyUI stores widget values by
                # position, so inserting one in the middle shifts every
                # value after it in saved graphs. New widgets go here.
                "clip_index": ("INT", {
                    "default": 1, "min": 0, "max": 9999, "step": 1,
                    "tooltip": "Driven by the Chain node, the same way "
                               "Load and Save are, as long as this node "
                               "sits in their canvas group. 0 is the first "
                               "clip: nothing pinned, so the whole thing "
                               "gets refined and the hold is skipped."}),
                "tuning": (("auto", "manual"), {
                    "default": "auto",
                    "tooltip": "Auto measures how far the refine moved this "
                               "clip and picks the ramp length itself. "
                               "Manual uses blend_frames as set."}),
            },
            "optional": {
                "refined": ("LATENT", {
                    # Lazy, so the second sampler does not run at all when
                    # use_refine is off. Without this the toggle only
                    # skips the blending: ComfyUI executes upstream nodes
                    # regardless of whether their result is used, so the
                    # refine pass still costs its full render time for
                    # nothing.
                    "lazy": True,
                    "tooltip": "The same latent after a second sampler: one "
                               "step from the main schedule's last level. "
                               "With use_refine off, whatever feeds this "
                               "is not executed at all."}),
            },
        }

    def check_lazy_status(self, **kwargs):
        """Ask for the refined latent only when it is going to be used.

        ComfyUI runs everything upstream of a node whether or not the
        value is wanted, so without this the use_refine toggle would skip
        the blend but still pay for the second sampler.
        """
        if kwargs.get("use_refine") and kwargs.get("refined") is None:
            return ["refined"]
        return []

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)
    FUNCTION = "apply"
    CATEGORY = "latent/minimax"
    DESCRIPTION = ("Experimental. Holds the pinned frames from the original "
                   "clip and ramps into a refined version over the frames "
                   "after them. Audio passes through unrefined by default.")

    def apply(self, original, hold_frames, blend_frames, audio,
              use_refine, clip_index=1, tuning="auto", refined=None):
        # This is a widget rather than an input because the Chain node
        # drives Load and Save the same way: the frontend finds them by
        # type inside the shared canvas group and writes their
        # clip_index. There is no wire to take.
        #
        # Reading the Load Latent output instead would not work either.
        # An optional input is empty both when nothing is connected and
        # when it is connected to something that returned nothing, so
        # clip 0 and an unwired graph cannot be told apart.
        first_clip = int(clip_index) <= 0
        if tuning not in ("auto", "manual"):
            raise ValueError(
                "h3_motion_context: refine blend: tuning is %r, expected "
                "auto or manual" % (tuning,))
        if not use_refine:
            refined = None
        if refined is None:
            _LOG.info("h3_motion_context: refine blend: refine OFF, passing "
                      "the original through unchanged")
            return (original,)

        o_vid, o_aud, raw_ndim = streams_of(original, "original")
        r_vid, r_aud, _ = streams_of(refined, "refined")

        if tuple(o_vid.shape) != tuple(r_vid.shape):
            raise ValueError(
                "h3_motion_context: the two latents are different shapes, "
                "%s against %s. The refined one has to be the same clip "
                "through a second sampler, not a different render."
                % (tuple(o_vid.shape), tuple(r_vid.shape)))

        hold = int(hold_frames)
        hold_steps = steps_for_frames(hold)
        if hold_steps is None:
            raise ValueError(
                "h3_motion_context: a %d frame hold is not a whole number "
                "of latent steps. Nearest that are: %s."
                % (hold, ", ".join(str(x) for x in nearest_valid(hold))))
        total = int(o_vid.shape[2])
        if hold_steps > total:
            raise ValueError(
                "h3_motion_context: holding %d frames needs %d latent steps "
                "but this clip only has %d" % (hold, hold_steps, total))

        blend = int(blend_frames)
        if tuning == "auto" and not first_clip:
            gap = refine_gap(o_vid, r_vid, hold_steps)
            want, blend = auto_ramp(gap, total, hold)
            _LOG.info("h3_motion_context: refine blend: auto, refine moved "
                      "the clip %.3f after the hold, ramp %d frames%s",
                      gap if gap is not None else float("nan"), blend,
                      "" if blend == want else
                      " (shortened from %d to leave some of the clip neat)"
                      % want)

        if first_clip:
            w = [1.0] * total
        else:
            w = weights(total, hold, blend)
        out = o_vid.clone()
        for k, wk in enumerate(w):
            if wk <= 0.0:
                continue
            if wk >= 1.0:
                out[:, :, k] = r_vid[:, :, k]
            else:
                out[:, :, k] = (1.0 - wk) * o_vid[:, :, k] + wk * r_vid[:, :, k]

        held = sum(1 for x in w if x <= 0.0)
        ramped = sum(1 for x in w if 0.0 < x < 1.0)
        pure = total - held - ramped
        if first_clip:
            _LOG.info("h3_motion_context: refine blend: first clip, nothing "
                      "pinned, so the whole clip is taken from the refined "
                      "latent; audio from the %s", audio)
        else:
            _LOG.info("h3_motion_context: refine blend: steps 0-%d untouched "
                      "(the %d frame hold, plus the step on the boundary "
                      "where the ramp is still zero), %d ramped over %d "
                      "frames, %d taken neat from the refined latent; audio "
                      "from the %s", held - 1, hold, ramped,
                      blend, pure, audio)
        if ramped:
            shown = [x for x in w if 0.0 < x < 1.0][:8]
            _LOG.info("h3_motion_context: refine blend: ramp %s",
                      " ".join("%.2f" % x for x in shown))
        if not pure:
            _LOG.warning("h3_motion_context: refine blend: the hold and "
                         "ramp cover the whole clip, so nothing is taken "
                         "from the refined latent at full strength")

        if raw_ndim == 4:
            out = out.squeeze(0)
        want_refined_audio = audio == "refined"
        return ({"samples": repack(original["samples"], out,
                                   r_aud if want_refined_audio
                                   else o_aud)},)


NODE_CLASS_MAPPINGS = {
    "MiniMaxH3RefineBlend": MiniMaxH3RefineBlend,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3RefineBlend": "H3 Refine Blend (experimental)",
}

"""Reshape the last noise levels of the sampler's schedule.

EXPERIMENTAL

Every chained clip comes out a few percent crisper than the frames it was
pinned from, at every join, and on a face that reads as aging. FastH3's
schedule makes one large final step, from about 0.63 straight to zero,
and that last step settles nearly all of the fine detail. Starting it
from a slightly noisier point removes the extra crispness.

H3 Sigmas Tail reshapes only the last few noise levels before zero. The
first level, every level before the tail, and the final zero stay where
they are. Within the tail, each level is moved as a fraction of the
level just before the tail:

    new = anchor * (old / anchor) ** (1 / (1 + strength))

Positive strength lifts the tail toward the anchor, so less of the
schedule is spent at the lowest noise and the last step to zero is
larger. Negative strength does the opposite. Order is always kept, and
0.0 returns the schedule unchanged. The console shows the levels before
and after.

Tested: tail_steps 3, strength 0.5 stopped the per-join sharpening on
locked-off chains with a calm background, and kept faces from aging over
six clips. A busy, high-contrast background still crisps up slowly.

USING IT

    BasicScheduler -> H3 Sigmas Tail -> SamplerCustomAdvanced, sigmas
"""

import logging

_LOG = logging.getLogger("h3_motion_context")


def values_of(sigmas):
    return [float(v) for v in sigmas]


def same_kind(values, ref):
    """A schedule tensor like the one that came in."""
    if hasattr(ref, "new_tensor"):
        return ref.new_tensor(values)
    return values


def describe(values):
    return ", ".join("%.4f" % v for v in values)


class MiniMaxH3SigmasTail:
    """Reshape only the lowest noise levels before the final zero."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "sigmas": ("SIGMAS",),
                "tail_steps": ("INT", {
                    "default": 3, "min": 1, "max": 20,
                    "tooltip": "How many of the lowest levels before zero to "
                               "reshape. Everything above them is kept."}),
                "strength": ("FLOAT", {
                    "default": 0.5, "min": -0.9, "max": 3.0, "step": 0.05,
                    "round": False,
                    "tooltip": "Above 0 lifts the tail, spending less time at "
                               "the lowest noise, which is expected to add "
                               "less fine detail. Below 0 does the opposite. "
                               "0 leaves the schedule unchanged."}),
            },
        }

    RETURN_TYPES = ("SIGMAS",)
    RETURN_NAMES = ("sigmas",)
    FUNCTION = "apply"
    CATEGORY = "sampling/custom_sampling/sigmas"
    DESCRIPTION = ("Experimental. Reshapes only the last few noise levels "
                   "before zero; the start, the levels above the tail and the "
                   "final zero stay put.")

    def apply(self, sigmas, tail_steps, strength):
        v = values_of(sigmas)
        if abs(strength) < 1e-9:
            return (sigmas,)
        # The levels to reshape: the last tail_steps above zero. The final
        # level is normally exactly 0 and is never touched.
        end = len(v) - 1 if v and v[-1] == 0.0 else len(v)
        start = max(1, end - int(tail_steps))
        if start >= end:
            _LOG.warning("h3_motion_context: sigmas tail: schedule too short "
                         "to reshape, passing it through")
            return (sigmas,)
        anchor = v[start - 1]
        if anchor <= 0:
            return (sigmas,)
        p = 1.0 / (1.0 + float(strength))
        out = list(v)
        for i in range(start, end):
            r = max(v[i], 0.0) / anchor
            out[i] = anchor * (r ** p)
        # keep strictly decreasing, in case of a flat stretch
        for i in range(start, end):
            if out[i] >= out[i - 1]:
                out[i] = out[i - 1] * 0.999
        _LOG.info("h3_motion_context: sigmas tail: reshaped the last %d "
                  "levels before zero at strength %.2f: %s -> %s",
                  end - start, strength, describe(v[start:end]),
                  describe(out[start:end]))
        return (same_kind(out, sigmas),)


NODE_CLASS_MAPPINGS = {
    "MiniMaxH3SigmasTail": MiniMaxH3SigmasTail,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3SigmasTail": "H3 Sigmas Tail (experimental)",
}

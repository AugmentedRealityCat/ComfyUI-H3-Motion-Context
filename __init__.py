"""H3 Motion Context.

Clip chaining for MiniMax H3: pin the tail of the previous clip, picture
and sound, so the next clip genuinely continues it.

This registers the nodes and nothing else. Nothing in ComfyUI is
modified. Earlier versions wrapped ComfyUI's H3 layout constructor to
lift a restriction on where a keyframe could be anchored; ComfyUI 0.34.0
lifted it upstream, so the nodes now build plain keyframe dicts and hand
them to stock code.

What remains is a dependency on meaning rather than structure: the pinned
audio window is placed with a fractional, negative anchor index, which is
legal arithmetic in the layout but is not reachable through any stock
node and therefore has no upstream test. layout_contract.py checks it
before the first render and refuses if it ever stops holding, so an
upstream change produces a clear error rather than a silently wrong join.

join_match.py, sigma_tools.py and refine_blend.py carry no such
dependency: they shift, scale and mix latent rows and noise levels, and
nothing else. join_match and sigma_tools at strength 0 are byte-identical
no-ops.

Those checks run on first use, not at import, so having the pack
installed changes nothing until you actually chain a clip.

Requires ComfyUI 0.34.0 or newer. Pack version 0.3.1 is the last release
that works with 0.33.4 and older.
"""

import logging

from .nodes import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    register_chain_routes,
)
from .probe_node import (
    NODE_CLASS_MAPPINGS as _PROBE_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _PROBE_NAMES,
)
from .join_match import (
    NODE_CLASS_MAPPINGS as _JOIN_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _JOIN_NAMES,
)
from .sigma_tools import (
    NODE_CLASS_MAPPINGS as _SIGMA_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _SIGMA_NAMES,
)
from .refine_blend import (
    NODE_CLASS_MAPPINGS as _BLEND_CLASSES,
    NODE_DISPLAY_NAME_MAPPINGS as _BLEND_NAMES,
)

NODE_CLASS_MAPPINGS.update(_PROBE_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_PROBE_NAMES)
NODE_CLASS_MAPPINGS.update(_JOIN_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_JOIN_NAMES)
NODE_CLASS_MAPPINGS.update(_SIGMA_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_SIGMA_NAMES)
NODE_CLASS_MAPPINGS.update(_BLEND_CLASSES)
NODE_DISPLAY_NAME_MAPPINGS.update(_BLEND_NAMES)
register_chain_routes()

logging.getLogger("h3_motion_context").info(
    "h3_motion_context: nodes registered. ComfyUI is not modified; the "
    "layout checks run on the first use of a Motion Context node.")

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

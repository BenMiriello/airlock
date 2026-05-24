"""ComfyUI custom node that integrates with airlock GPU broker.

Loaded automatically by ComfyUI when the directory is present under
custom_nodes/. Imports the integration module which performs side-effectful
monkey-patching of the prompt executor and registers HTTP routes.

ComfyUI's custom-node loader expects either NODE_CLASS_MAPPINGS in __init__
or files registering nodes. We don't add user-visible nodes — the integration
is invisible. Empty mappings satisfy the loader.
"""
from . import integration  # noqa: F401  (side-effectful import)


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = None

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

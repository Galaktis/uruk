"""Element-tree rendering: registry + built-in block kinds."""

from rendering import blocks  # import registers the built-in block kinds
from rendering.registry import (
    RENDERERS,
    RenderContext,
    Renderer,
    UnknownElementKindError,
    register,
    render_element,
    render_page,
)

__all__ = [
    "RENDERERS",
    "RenderContext",
    "Renderer",
    "UnknownElementKindError",
    "register",
    "render_element",
    "render_page",
]

from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def create_app(*args: Any, **kwargs: Any):
    # Keep package import lightweight for wrapper/runtime entrypoints that do
    # not need uvicorn/fastapi until create_app is explicitly requested.
    from .main import create_app as _create_app

    return _create_app(*args, **kwargs)

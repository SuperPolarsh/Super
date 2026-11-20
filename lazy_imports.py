"""Loads the video pipeline only when it is needed.

Importing ``main`` pulls in torch, whisper and moviepy, which together cost
about 2.2 s - most of the time between starting Super and seeing its window.
The window now opens first and a background thread warms these modules, so the
first Generate click never waits for them.
"""
from __future__ import annotations

import importlib
import threading

_lock = threading.Lock()

# Modules worth importing in the background after the window appears.
WARM_MODULES = ("main", "source_downloader", "editor")


def load(name: str):
    """Imports a module on first use, then returns it from ``sys.modules``."""
    with _lock:
        return importlib.import_module(name)


def call(module: str, attribute: str, *args, **kwargs):
    """Calls ``module.attribute``, importing ``module`` if needed."""
    return getattr(load(module), attribute)(*args, **kwargs)


def warm(names=WARM_MODULES):
    """Pre-imports the heavy modules. Safe to call from a worker thread."""
    for name in names:
        try:
            load(name)
        except Exception:
            pass  # a broken optional import must not break start-up

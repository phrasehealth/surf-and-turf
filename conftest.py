"""Test-session setup.

WeasyPrint dlopens Pango/Cairo/GObject. On macOS those live under the Homebrew
prefix, which dyld does not search by default, so `pytest` fails with
`OSError: cannot load library 'libgobject-2.0-0'` -- directly in test_pdf, and
indirectly in test_api, where publish_report raises and no report event is
emitted.  ctypes reads DYLD_FALLBACK_LIBRARY_PATH on each lookup, so setting it
before any test imports app.pdf is enough; app/pdf.py imports weasyprint lazily.

Linux and the container image put these libraries on the default search path,
so this is a no-op there.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "darwin":
    existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    for prefix in ("/opt/homebrew/lib", "/usr/local/lib"):
        if (Path(prefix) / "libgobject-2.0.0.dylib").exists():
            if prefix not in existing.split(":"):
                # Keep dyld's implicit defaults, which setting this variable replaces.
                parts = [prefix, existing or "/usr/local/lib:/usr/lib"]
                os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(p for p in parts if p)
            break

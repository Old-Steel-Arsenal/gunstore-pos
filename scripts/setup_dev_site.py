"""Backwards-compat shim. The real implementation now lives inside the app
at `ffl_core.setup.dev_site` so it's importable from anywhere bench can
reach (CI, container, prod) without sys.path tricks.

Run via:
    bench --site dev.localhost execute ffl_core.setup.dev_site.run
"""
from __future__ import annotations

from ffl_core.setup.dev_site import run  # noqa: F401  (re-export for legacy callers)

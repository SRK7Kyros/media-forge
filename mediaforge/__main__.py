"""``python -m mediaforge`` is the same entry point as the ``mediaforge`` script."""

from __future__ import annotations

from .cli import main

raise SystemExit(main())

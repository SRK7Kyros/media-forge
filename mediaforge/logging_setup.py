"""Logging: a rotating file log plus a console stream."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import Settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(filename)s:%(lineno)d] %(message)s"


def configure(settings: Settings) -> logging.Logger:
    root = logging.getLogger("mediaforge")
    if root.handlers:
        return root
    root.setLevel(logging.DEBUG)
    root.propagate = False

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        settings.log_dir / "mediaforge.log",
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, settings.log_level, logging.INFO))
    console.setFormatter(logging.Formatter(FORMAT))

    root.addHandler(file_handler)
    root.addHandler(console)
    return root

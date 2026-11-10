"""Small filesystem durability helpers shared by storage adapters."""

from __future__ import annotations

import os
from pathlib import Path


def fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

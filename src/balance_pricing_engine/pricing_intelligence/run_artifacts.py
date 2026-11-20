"""Atomic writes for operational artifacts (run summaries, skip-escalation state).

A scheduler, canary or dashboard may read a summary while the next run writes it. Writing
to a temporary file in the same directory, flushing it to disk and then ``os.replace``-ing
it over the target means a reader sees either the old complete file or the new complete
file, never a partial one -- and a crash mid-write leaves the previous file intact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


def write_json_atomically(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))

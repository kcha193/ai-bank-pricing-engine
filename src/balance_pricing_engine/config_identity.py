"""Canonical identity of a pricing configuration (GOV-004).

Two different questions, two different hashes -- never conflate them:

* :func:`effective_config_hash` -- identity of the *validated* ``EngineConfig``
  that actually drove pricing. Independent of YAML layout, key order, file name
  and temp paths. This is the ``config_hash`` recorded in decision records and
  run-ledger metadata.
* :func:`file_sha256` -- SHA-256 of a caller's original source file bytes. This
  is the ``source_config_file_sha256`` provenance field.

Configuration identity is not model approval: a decision with a correctly
recorded hash can still be ``REVIEW_REQUIRED`` or ``BLOCKED`` by governance gates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from balance_pricing_engine.config import EngineConfig

CONFIG_HASH_BASIS = "effective_config_canonical_json_sha256"
"""Label recorded next to ``config_hash`` so readers can tell it from legacy file-byte hashes."""


def canonical_config_bytes(config: "EngineConfig") -> bytes:
    """Canonical UTF-8 JSON encoding of the validated config.

    ``effective_config.json`` in a run ledger holds exactly these bytes, so
    ``sha256(effective_config.json) == config_hash``.
    """
    payload = config.model_dump(mode="json")
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def effective_config_hash(config: "EngineConfig") -> str:
    """SHA-256 hex digest of :func:`canonical_config_bytes`."""
    return hashlib.sha256(canonical_config_bytes(config)).hexdigest()


def file_sha256(path: str | Path) -> str:
    """SHA-256 hex digest of the raw bytes of ``path``."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_sha256_if_present(path: str | Path | None) -> str | None:
    """:func:`file_sha256`, or ``None`` when no readable source file exists at ``path``.

    ``None`` means source-file provenance is unavailable; it is never replaced by the
    hash of some other file (such as a temporary effective-config overlay).
    """
    if path is None or not Path(path).is_file():
        return None
    return file_sha256(path)

"""Local model persistence helpers.

Security note: joblib artifacts are pickles. Loading one executes code chosen by whoever wrote
it. Treat model artifacts as trusted local assets only. ``load_verified_model`` checks that the
bytes match an approved SHA-256 *before* deserialising them, which protects the approved
identity of an artifact. It does not make an untrusted pickle safe to load.
"""

from __future__ import annotations

import hmac
import io
from hashlib import sha256
from pathlib import Path
from typing import Any

import joblib


class ArtifactIntegrityError(ValueError):
    """Raised when artifact bytes do not match the approved SHA-256."""


def save_model(model: Any, path: str | Path) -> None:
    """Save a fitted model with joblib."""
    model_path = Path(path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, model_path)


def load_model(path: str | Path) -> Any:
    """Load a fitted model saved with joblib (no integrity check; trusted local files only)."""
    return joblib.load(Path(path))


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data``."""
    return sha256(data).hexdigest()


def artifact_sha256(path: str | Path) -> str:
    """Return the lowercase hex SHA-256 of the artifact file's bytes."""
    return sha256_hex(Path(path).read_bytes())


def load_model_from_bytes(data: bytes) -> Any:
    """Deserialise a joblib artifact from bytes already read (and, if required, verified)."""
    return joblib.load(io.BytesIO(data))


def verify_artifact_bytes(data: bytes, expected_sha256: str) -> str:
    """Return the SHA-256 of ``data``; raise ArtifactIntegrityError if it differs."""
    actual = sha256_hex(data)
    if not hmac.compare_digest(actual, str(expected_sha256).lower()):
        raise ArtifactIntegrityError(
            f"artifact SHA-256 mismatch: approved {str(expected_sha256)[:12]}..., "
            f"found {actual[:12]}...; the artifact changed after approval"
        )
    return actual


def load_verified_model(path: str | Path, expected_sha256: str) -> Any:
    """Read the artifact once, verify its SHA-256, then deserialise those same bytes.

    Hashing and loading one in-memory copy avoids a check-then-reopen race: the object
    returned is always the one built from the verified bytes.
    """
    data = Path(path).read_bytes()
    verify_artifact_bytes(data, expected_sha256)
    return load_model_from_bytes(data)

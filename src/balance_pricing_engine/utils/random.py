"""Random number utilities."""

from __future__ import annotations

import numpy as np


def make_rng(seed: int | None) -> np.random.Generator:
    """Create a NumPy random generator from an optional seed."""
    return np.random.default_rng(seed)


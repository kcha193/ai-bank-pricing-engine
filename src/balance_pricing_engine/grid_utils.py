"""Shared helpers for inclusive numeric grids."""

from __future__ import annotations

import numpy as np


def build_inclusive_numeric_grid(
    start: float,
    stop: float,
    step: float,
    *,
    label: str = "Grid",
    start_name: str = "start",
    stop_name: str = "stop",
) -> np.ndarray:
    """Build an inclusive numeric grid where the step lands exactly on the stop value."""
    if step <= 0:
        raise ValueError(f"{label} step must be greater than zero.")
    if stop < start:
        raise ValueError(
            f"{label} {stop_name} must be greater than or equal to {start_name}."
        )

    step_count = (stop - start) / step
    rounded_step_count = int(round(step_count))
    if not np.isclose(step_count, rounded_step_count, rtol=1e-9, atol=1e-12):
        raise ValueError(f"{label} step must land exactly on {stop_name}.")

    return np.round(start + np.arange(rounded_step_count + 1) * step, 6)

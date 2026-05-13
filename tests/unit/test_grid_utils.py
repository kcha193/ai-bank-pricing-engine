import numpy as np
import pytest

from balance_pricing_engine.grid_utils import build_inclusive_numeric_grid


def test_build_inclusive_numeric_grid_includes_exact_stop():
    grid = build_inclusive_numeric_grid(0.03, 0.04, 0.005)

    assert np.allclose(grid, np.array([0.03, 0.035, 0.04]))


def test_build_inclusive_numeric_grid_rejects_step_that_misses_stop():
    with pytest.raises(ValueError, match="land exactly"):
        build_inclusive_numeric_grid(0.03, 0.04, 0.006)

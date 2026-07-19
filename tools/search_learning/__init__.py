"""Offline search-learning training and calibration helpers.

The runtime never imports heavy machine-learning dependencies.  These tools
use deterministic, pure-Python implementations and emit compact JSON models
that the desktop application can load directly.
"""

from .calibration import (
    CalibrationExample,
    apply_calibrator,
    build_calibration_registry,
    calibration_metrics,
)
from .ranker import RankerExample, train_ranker

__all__ = [
    "CalibrationExample",
    "RankerExample",
    "apply_calibrator",
    "build_calibration_registry",
    "calibration_metrics",
    "train_ranker",
]

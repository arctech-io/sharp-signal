"""Signal detection and flagging engine."""

from app.detection.engine import (
    DetectionConfig,
    DetectionEngine,
    build_reason,
    check_for_signal,
    compute_confidence,
    determine_direction,
    percent_change,
    rolling_mean,
    rolling_std,
    z_score,
)

__all__ = [
    "DetectionConfig",
    "DetectionEngine",
    "build_reason",
    "check_for_signal",
    "compute_confidence",
    "determine_direction",
    "percent_change",
    "rolling_mean",
    "rolling_std",
    "z_score",
]

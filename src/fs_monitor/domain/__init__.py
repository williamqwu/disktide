"""Framework-independent domain types for fsmonitor."""

from fs_monitor.domain.metrics import MetricId, StorageMeasurements
from fs_monitor.domain.policy import ScanPolicy
from fs_monitor.domain.scan import (
    ScanRequest,
    ScanResourcePolicy,
    ScanRun,
    ScanStatus,
    ScanWorkerSelection,
)

__all__ = [
    "MetricId",
    "ScanPolicy",
    "ScanRequest",
    "ScanResourcePolicy",
    "ScanRun",
    "ScanStatus",
    "ScanWorkerSelection",
    "StorageMeasurements",
]

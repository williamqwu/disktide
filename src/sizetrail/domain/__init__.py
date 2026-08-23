"""Framework-independent domain types for sizetrail."""

from sizetrail.domain.metrics import MetricId, StorageMeasurements
from sizetrail.domain.policy import ScanPolicy
from sizetrail.domain.scan import (
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

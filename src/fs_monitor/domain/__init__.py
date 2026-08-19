"""Framework-independent domain types for fsmonitor."""

from fs_monitor.domain.metrics import MetricId, StorageMeasurements
from fs_monitor.domain.policy import ScanPolicy

__all__ = ["MetricId", "ScanPolicy", "StorageMeasurements"]

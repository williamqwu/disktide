"""Optional filesystem-event acceleration adapters."""

from sizetrail.collectors.events.base import (
    EventBackend,
    EventBackendInfo,
    EventCallback,
    EventWatch,
    FilesystemEvent,
    FilesystemEventKind,
)
from sizetrail.collectors.events.native import (
    InotifyEventBackend,
    create_native_event_backend,
    probe_native_event_backend,
)

__all__ = [
    "EventBackend",
    "EventBackendInfo",
    "EventCallback",
    "EventWatch",
    "FilesystemEvent",
    "FilesystemEventKind",
    "InotifyEventBackend",
    "create_native_event_backend",
    "probe_native_event_backend",
]

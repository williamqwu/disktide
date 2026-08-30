"""DiskTide: terminal storage intelligence."""

PRODUCT_NAME = "DiskTide"
APP_NAME = PRODUCT_NAME
CLI_NAME = "disktide"
STORAGE_NAMESPACE = "disktide"
PREVIOUS_STORAGE_NAMESPACE = "sizetrail"
LEGACY_STORAGE_NAMESPACE = "fsmonitor-cli"
LEGACY_STORAGE_NAMESPACES = (
    PREVIOUS_STORAGE_NAMESPACE,
    LEGACY_STORAGE_NAMESPACE,
)
QUARANTINE_DIRECTORY_NAME = ".disktide-quarantine"
LEGACY_QUARANTINE_DIRECTORY_NAMES = (
    ".sizetrail-quarantine",
    ".fsmonitor-quarantine",
)
__version__ = "0.2.28"

"""Compatibility import for the pre-SizeTrail package namespace."""

from sizetrail import (
    APP_NAME,
    CLI_NAME,
    LEGACY_QUARANTINE_DIRECTORY_NAMES,
    LEGACY_STORAGE_NAMESPACE,
    PRODUCT_NAME,
    QUARANTINE_DIRECTORY_NAME,
    STORAGE_NAMESPACE,
    __version__,
)

__all__ = [
    "APP_NAME",
    "CLI_NAME",
    "LEGACY_QUARANTINE_DIRECTORY_NAMES",
    "LEGACY_STORAGE_NAMESPACE",
    "PRODUCT_NAME",
    "QUARANTINE_DIRECTORY_NAME",
    "STORAGE_NAMESPACE",
    "__version__",
]

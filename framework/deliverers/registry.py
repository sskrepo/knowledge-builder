"""Deliverer registry."""
from __future__ import annotations
from .filesystem import FilesystemDeliverer
from .sync_return import SyncReturnDeliverer
from .object_storage import ObjectStorageDeliverer
from .email import EmailDeliverer
from .slack import SlackDeliverer

_DELIVERERS = {
    "filesystem":         FilesystemDeliverer,
    "sync_return":        SyncReturnDeliverer,
    "oci_object_storage": ObjectStorageDeliverer,
    "email":              EmailDeliverer,
    "slack":              SlackDeliverer,
}

def get_deliverer(name: str):
    if name not in _DELIVERERS:
        raise ValueError(f"unknown deliverer: {name}; available: {list(_DELIVERERS)}")
    return _DELIVERERS[name]()

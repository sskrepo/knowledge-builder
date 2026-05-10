"""Deliverer Protocol — per ADR-016."""
from __future__ import annotations
from typing import Protocol, runtime_checkable


@runtime_checkable
class Deliverer(Protocol):
    name: str
    def deliver(self, artifact: bytes, destination: dict) -> dict: ...


class BaseDeliverer:
    name: str = ""
    def deliver(self, artifact: bytes, destination: dict) -> dict:
        raise NotImplementedError

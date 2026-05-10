"""Trigger dispatcher — routes triggers (on_request | on_schedule | on_event) to executor."""
from __future__ import annotations
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class TriggerDispatcher:
    def __init__(self, executor):
        self.executor = executor

    def on_request(self, skill_path: Path, inputs: dict) -> dict:
        log.info("on_request invocation: %s", skill_path)
        return self.executor.execute(skill_path, inputs)

    def on_schedule(self, skill_path: Path, default_inputs: dict | None = None) -> dict:
        log.info("on_schedule invocation: %s", skill_path)
        return self.executor.execute(skill_path, default_inputs or {})

    def on_event(self, skill_path: Path, event: dict) -> dict:
        log.info("on_event invocation: %s event=%s", skill_path, event)
        return self.executor.execute(skill_path, event)

"""Scheduler — backfill / repair / scheduled polls.

Phase 1: simple cron-style runner. Phase 4: distributed via OCI Functions.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, jobs: list):
        # jobs: list of (name, callable, interval_seconds)
        self.jobs = jobs
        self._last_run: dict = {}

    def tick(self) -> None:
        now = time.time()
        for name, fn, interval in self.jobs:
            last = self._last_run.get(name, 0)
            if now - last >= interval:
                try:
                    fn()
                except Exception as e:
                    log.exception("job %s failed: %s", name, e)
                self._last_run[name] = now

    def run_forever(self, tick_interval: int = 30) -> None:
        while True:
            self.tick()
            time.sleep(tick_interval)

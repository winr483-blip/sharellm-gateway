"""O3.QA — deterministic 100-concurrent matcher stress + Python-heap bound.

100 RAPID CONCURRENT ``ChannelMatcher.select_targets`` calls against a real
temporary SQLite WAL registry. Measures:

* real SQLite connection lifecycle via a ``sqlite3.Connection`` subclass
  (opened / closed / active counters, thread-safe), installed only through a
  monkeypatched connect factory; the assertion is ``opened == closed`` — never
  "closed by GC".
* Python heap growth via ``tracemalloc`` (this is the traced Python heap, NOT
  RSS and NOT 10,000-developer scale).

No sleeps, polling or retries; the 100-request batch is measured exactly once
(no loop-until-pass). Cards are explicitly SYNTHETIC matched fixtures in a
QA-owned temp database, not verified marketplace data.
"""

from __future__ import annotations

import asyncio
import gc
import importlib
import sqlite3
import threading
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from gateway.config import RouteTargetConfig
from gateway.engine.registry import ChannelNode, ChannelRegistryStorage
from gateway.engine.registry.matcher import ChannelMatcher

MB = 1024 * 1024
HEAP_LIMIT = 5 * MB
ADMISSION_BOUND = 16


class _TrackedConnection(sqlite3.Connection):
    """Real SQLite connection that counts its own open/close exactly once."""

    lock = threading.Lock()
    opened = 0
    closed = 0
    active = 0
    max_active = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._counted = False
        with _TrackedConnection.lock:
            _TrackedConnection.opened += 1
            _TrackedConnection.active += 1
            _TrackedConnection.max_active = max(
                _TrackedConnection.max_active, _TrackedConnection.active
            )

    def close(self) -> None:
        with _TrackedConnection.lock:
            if not self._counted:
                self._counted = True
                _TrackedConnection.closed += 1
                _TrackedConnection.active -= 1
        super().close()

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.opened = 0
            cls.closed = 0
            cls.active = 0
            cls.max_active = 0


_CARDS: dict[str, float] = {"c1": 0.1, "c2": 0.2, "c3": 0.3, "c4": 0.4}


def _node(channel_id: str, *, price: float, latency: float) -> ChannelNode:
    """Synthetic matched fixture (explicit, not a marketplace measurement)."""
    return ChannelNode.model_validate(
        {
            "channel_id": channel_id,
            "model_name": "alias",
            "publisher": "SYNTHETIC QA FIXTURE / NOT VERIFIED",
            "is_certified": False,
            "pricing": {
                "input_price_per_1m": price,
                "cache_read_price_per_1m": 0.05,
                "cache_write_price_per_1m": 1.0,
                "output_price_per_1m": 4.0,
                "multiplier": 1.0,
            },
            "capabilities": {"supports_vision": True, "supports_tools": True, "context_window": 1_024_000},
            "authenticity": {"fingerprint_status": "matched"},
            "metrics": {
                "first_token_latency": latency,
                "tps": 50.0,
                "cache_hit_rate": 0.0,
                "sla_realtime": 99.0,
                "sla_24h": 98.0,
                "sla_7d": 97.0,
                "community_hearts": 1,
            },
        }
    )


def _bindings() -> dict[str, RouteTargetConfig]:
    return {cid: RouteTargetConfig(provider="p", model=f"wire-{cid}") for cid in _CARDS}


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["budget", "performance"])
async def test_hundred_concurrent_matcher_requests_close_and_bounded_heap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    storage_module = importlib.import_module("gateway.engine.registry.storage")
    _TrackedConnection.reset()
    real_connect = storage_module.sqlite3.connect

    def tracked_connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs.setdefault("factory", _TrackedConnection)
        return real_connect(database, *args, **kwargs)

    # Instrument BEFORE initialize/seed so every real storage connection is counted.
    monkeypatch.setattr(storage_module.sqlite3, "connect", tracked_connect)

    tracing_owned = not tracemalloc.is_tracing()
    if tracing_owned:
        tracemalloc.start()  # start before warmup so warmup heap is in the baseline
    tracemalloc.reset_peak()

    storage = ChannelRegistryStorage(db_path=tmp_path / "stress.db")
    try:
        await storage.initialize()
        for cid, price in _CARDS.items():
            await storage.upsert_channel(_node(cid, price=price, latency=price * 10))

        matcher = ChannelMatcher(storage, _bindings())

        # Warmup: warms the worker threadpool + dependencies BEFORE the baseline.
        await asyncio.gather(*(matcher.select_targets("alias", profile, {}) for _ in range(20)))
        gc.collect()
        baseline_current, _ = tracemalloc.get_traced_memory()
        baseline_opened = _TrackedConnection.opened
        tracemalloc.reset_peak()

        # Synchronous start barrier: all 100 drivers released at once (no sleeps).
        barrier = asyncio.Event()

        async def _one() -> list[RouteTargetConfig]:
            await barrier.wait()
            return await matcher.select_targets("alias", profile, {})

        tasks = [asyncio.create_task(_one()) for _ in range(100)]
        barrier.set()
        results = await asyncio.gather(*tasks)

        # Validate the 100 results BEFORE any disposal.
        assert len(results) == 100
        assert all(len(row) >= 1 for row in results), "every request must yield >=1 eligible target"
        first_ids = [target.model for target in results[0]]
        assert all(
            [target.model for target in row] == first_ids for row in results
        ), "selection order must be deterministic across all calls"
        measured_connections = _TrackedConnection.opened - baseline_opened
        assert measured_connections == 100, f"expected 100 read connections, saw {measured_connections}"

        await storage.close()
        # Explicit close accounting: no connection is unclosed (never GC-based).
        assert _TrackedConnection.opened == _TrackedConnection.closed, "unclosed SQLite connection"
        assert _TrackedConnection.active == 0, "a tracked connection is still active"
        assert _TrackedConnection.max_active <= ADMISSION_BOUND, "admission bound exceeded"

        # Retained (results still referenced) then released; both reported.
        gc.collect()
        retained_current, _ = tracemalloc.get_traced_memory()
        retained_delta = retained_current - baseline_current
        del results, first_ids
        gc.collect()
        released_current, peak = tracemalloc.get_traced_memory()
        released_delta = released_current - baseline_current

        print(
            f"[stress profile={profile}] requests=100 "
            f"conn_peak={_TrackedConnection.max_active} opened={_TrackedConnection.opened} "
            f"closed={_TrackedConnection.closed} active={_TrackedConnection.active} "
            f"heap_retained_bytes={retained_delta} heap_released_bytes={released_delta} "
            f"peak_bytes={peak} limit_bytes={HEAP_LIMIT}",
            flush=True,
        )
        assert retained_delta < HEAP_LIMIT, f"retained Python heap grew {retained_delta} bytes (>= 5 MiB)"
        assert released_delta < HEAP_LIMIT, f"released Python heap grew {released_delta} bytes (>= 5 MiB)"
    finally:
        try:
            await storage.close()  # idempotent; guarantees cleanup on error paths too
        finally:
            if tracing_owned and tracemalloc.is_tracing():
                tracemalloc.stop()

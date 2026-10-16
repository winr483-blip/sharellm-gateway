"""Hermetic acceptance tests for the S2.1 channel registry (models + WAL storage).

Scope
-----
* Verifies only ``gateway.engine.registry`` (S2.1) and does not touch production
  code or any preexisting test module.
* No real network, no wall-clock sleeps, no polling and no timing-dependent
  assertions.  Asynchronous determinism is achieved with ``threading.Event`` /
  ``asyncio.Event`` handshakes and ``loop.call_soon`` callbacks.

Synthetic-data disclaimer
-------------------------
The only user-supplied identity values are ``channel_id="#716"``,
``model_name="gpt-6.1-sol"``, ``publisher="#1 denni"`` and
``multiplier=0.0164``.  Every other numeric value in this module is a
synthetic test input chosen by QA; none of them is a verified marketplace
measurement, a live price, or a production SLA sample.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

import pytest
from pydantic import ValidationError

from gateway.engine.registry import (
    ChannelAuthenticity,
    ChannelCapabilities,
    ChannelMetrics,
    ChannelNode,
    ChannelPricing,
    ChannelRegistryStorage,
)

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

_IDENTITY: dict[str, Any] = {
    "channel_id": "#716",
    "model_name": "gpt-6.1-sol",
    "publisher": "#1 denni",
    "is_certified": True,
}


def _pricing(multiplier: float = 0.0164) -> dict[str, Any]:
    # Synthetic prices (per 1M tokens) and the user-supplied multiplier.
    return {
        "input_price_per_1m": 0.15,
        "cache_read_price_per_1m": 0.03,
        "cache_write_price_per_1m": 0.19,
        "output_price_per_1m": 0.60,
        "multiplier": multiplier,
    }


def _capabilities(
    supports_vision: bool = True,
    supports_tools: bool = True,
    context_window: int = 1_024_000,
) -> dict[str, Any]:
    return {
        "supports_vision": supports_vision,
        "supports_tools": supports_tools,
        "context_window": context_window,
    }


def _authenticity(
    fingerprint_status: str = "matched",
) -> dict[str, Any]:
    return {"fingerprint_status": fingerprint_status}


def _metrics(first_token_latency: float = 0.25) -> dict[str, Any]:
    # Synthetic latency sample; all other metrics fall back to model defaults.
    return {"first_token_latency": first_token_latency}


def make_node(
    *,
    channel_id: str = "#716",
    model_name: str = "gpt-6.1-sol",
    publisher: str = "#1 denni",
    is_certified: bool = True,
    pricing: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
    authenticity: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> ChannelNode:
    payload: dict[str, Any] = {
        "channel_id": channel_id,
        "model_name": model_name,
        "publisher": publisher,
        "is_certified": is_certified,
        "pricing": _pricing() if pricing is None else pricing,
        "capabilities": _capabilities() if capabilities is None else capabilities,
        "authenticity": _authenticity() if authenticity is None else authenticity,
        "metrics": _metrics() if metrics is None else metrics,
    }
    return ChannelNode.model_validate(payload)


async def new_storage(tmp_path: Path, **kwargs: Any) -> ChannelRegistryStorage:
    storage = ChannelRegistryStorage(db_path=tmp_path / "registry.db", **kwargs)
    await storage.initialize()
    return storage


def _drive(loop: asyncio.AbstractEventLoop, rounds: int = 8) -> Any:
    """Yield to the event loop ``rounds`` times without sleeping.

    Each yield resolves a future via a ``call_soon`` callback, which lets all
    callbacks already queued on the loop run before this coroutine resumes.
    """

    async def _yield_once() -> None:
        future = loop.create_future()
        loop.call_soon(future.set_result, None)
        await future

    async def _run() -> None:
        for _ in range(rounds):
            await _yield_once()

    return _run()


# ---------------------------------------------------------------------------
# 1. Identity fixture and exact defaults
# ---------------------------------------------------------------------------


async def test_complete_identity_fixture_and_defaults() -> None:
    node = make_node()

    assert node.channel_id == "#716"
    assert node.model_name == "gpt-6.1-sol"
    assert node.publisher == "#1 denni"
    assert node.is_certified is True
    assert node.pricing.multiplier == 0.0164

    # Documented defaults from the accepted contract.
    assert node.capabilities.context_window == 1_024_000
    assert node.capabilities.endpoints == ["responses", "gzip"]
    assert node.authenticity.anti_degradation is False
    assert node.authenticity.anti_injection is False
    assert node.authenticity.bazaarlink_similarity is None
    assert node.metrics.tps is None
    assert node.metrics.cache_hit_rate == 0.0
    assert node.metrics.sla_realtime == 100.0
    assert node.metrics.sla_24h == 100.0
    assert node.metrics.sla_7d == 100.0
    assert node.metrics.community_hearts == 0


async def test_endpoint_defaults_are_independent_lists() -> None:
    first = make_node()
    second = make_node()
    assert first.capabilities.endpoints == second.capabilities.endpoints
    assert first.capabilities.endpoints is not second.capabilities.endpoints
    first.capabilities.endpoints.append("websocket")
    assert second.capabilities.endpoints == ["responses", "gzip"]


# ---------------------------------------------------------------------------
# 2. Strict nested validation
# ---------------------------------------------------------------------------


async def test_unknown_fields_forbidden_at_every_level() -> None:
    with pytest.raises(ValidationError):
        make_node(pricing={**_pricing(), "currency": "usd"})
    with pytest.raises(ValidationError):
        make_node(capabilities={**_capabilities(), "max_tokens": 10})
    with pytest.raises(ValidationError):
        make_node(authenticity={**_authenticity(), "verified": True})
    with pytest.raises(ValidationError):
        make_node(metrics={**_metrics(), "uptime": 99.9})
    payload = {**_IDENTITY, "pricing": _pricing(), "capabilities": _capabilities(),
               "authenticity": _authenticity(), "metrics": _metrics(),
               "provenance": "marketplace"}
    with pytest.raises(ValidationError):
        ChannelNode.model_validate(payload)


@pytest.mark.parametrize(
    "field, value",
    [
        ("context_window", "1024000"),
        ("context_window", 1024.0),
        ("context_window", True),
        ("supports_vision", "true"),
        ("supports_tools", 1),
    ],
)
async def test_string_and_bool_coercion_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        make_node(capabilities={**_capabilities(), field: value})


@pytest.mark.parametrize(
    "field, value",
    [
        ("community_hearts", True),
        ("community_hearts", 5.0),
        ("community_hearts", "5"),
    ],
)
async def test_strict_int_fields_reject_non_ints(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        make_node(metrics={**_metrics(), field: value})


@pytest.mark.parametrize(
    "field, value",
    [
        ("input_price_per_1m", -0.01),
        ("cache_read_price_per_1m", -1.0),
        ("cache_write_price_per_1m", -1.0),
        ("output_price_per_1m", -1.0),
        ("multiplier", -0.1),
        ("input_price_per_1m", float("nan")),
        ("output_price_per_1m", float("inf")),
        ("multiplier", float("nan")),
    ],
)
async def test_pricing_rejects_negative_and_non_finite(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        make_node(pricing={**_pricing(), field: value})


@pytest.mark.parametrize(
    "field, value",
    [
        ("cache_hit_rate", -0.1),
        ("cache_hit_rate", 1.1),
        ("cache_hit_rate", float("nan")),
        ("sla_realtime", 100.5),
        ("sla_24h", -1.0),
        ("sla_7d", 101.0),
        ("tps", -0.1),
        ("tps", float("inf")),
        ("first_token_latency", -0.001),
        ("first_token_latency", float("inf")),
    ],
)
async def test_metric_bounds_and_finiteness(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        make_node(metrics={**_metrics(), field: value})


async def test_similarity_bounds_and_none_default() -> None:
    node = make_node()
    assert node.authenticity.bazaarlink_similarity is None
    with pytest.raises(ValidationError):
        make_node(authenticity={**_authenticity(), "bazaarlink_similarity": -0.01})
    with pytest.raises(ValidationError):
        make_node(authenticity={**_authenticity(), "bazaarlink_similarity": float("nan")})
    ok = make_node(authenticity={**_authenticity(), "bazaarlink_similarity": 0.87})
    assert ok.authenticity.bazaarlink_similarity == 0.87


@pytest.mark.parametrize("status", ["matched", "differs", "untested"])
async def test_fingerprint_literal_accepts_documented_values(status: str) -> None:
    node = make_node(authenticity=_authenticity(status))
    assert node.authenticity.fingerprint_status == status


@pytest.mark.parametrize("status", ["unknown", "MATCHED", "verified", ""])
async def test_fingerprint_literal_rejects_other_values(status: str) -> None:
    with pytest.raises(ValidationError):
        make_node(authenticity=_authenticity(status))


async def test_required_nested_fields_are_mandatory() -> None:
    with pytest.raises(ValidationError):
        make_node(authenticity={})  # fingerprint_status required
    with pytest.raises(ValidationError):
        make_node(metrics={})  # first_token_latency required
    with pytest.raises(ValidationError):
        make_node(capabilities={"context_window": 1000})  # vision/tools required
    with pytest.raises(ValidationError):
        make_node(pricing={"multiplier": 0.0164})  # prices required


# ---------------------------------------------------------------------------
# 3. Initialization, pragmas and schema
# ---------------------------------------------------------------------------


async def test_initialize_creates_nested_parent_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "deeply" / "nested" / "registry.db"
    assert not db_path.parent.exists()
    storage = ChannelRegistryStorage(db_path=db_path)
    await storage.initialize()
    assert db_path.parent.is_dir()
    assert db_path.is_file()
    # Idempotent second call must not raise.
    await storage.initialize()
    await storage.close()


async def test_production_connections_enforce_pragmas(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    connection = storage._connect()
    try:
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    finally:
        connection.close()

    assert journal_mode.lower() == "wal"
    assert synchronous == 1  # NORMAL
    assert busy_timeout == 5000
    assert foreign_keys == 1

    # A brand-new connection to the same file must also report WAL.
    reopened = sqlite3.connect(storage.db_path)
    try:
        assert str(reopened.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        reopened.close()
    await storage.close()


async def test_schema_tables_and_model_index(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    connection = sqlite3.connect(storage.db_path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_channels_model'"
        ).fetchone()
    finally:
        connection.close()

    assert {"channels", "channel_telemetry_history"} <= tables
    assert "idx_channels_model" in indexes
    assert index_sql is not None
    assert "model_name" in str(index_sql[0])
    await storage.close()


# ---------------------------------------------------------------------------
# 4. Upsert, persistence and reopen
# ---------------------------------------------------------------------------


async def test_upsert_replaces_same_channel_id_and_persists(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    await storage.upsert_channel(make_node())

    count = _scalar(storage, "SELECT COUNT(*) FROM channels")
    assert count == 1

    updated = make_node(model_name="gpt-6.1-sol-v2")
    await storage.upsert_channel(updated)
    assert _scalar(storage, "SELECT COUNT(*) FROM channels") == 1

    assert await storage.get_channels_for_model("gpt-6.1-sol") == []
    fetched = await storage.get_channels_for_model("gpt-6.1-sol-v2")
    assert [node.channel_id for node in fetched] == ["#716"]
    assert fetched[0].model_name == "gpt-6.1-sol-v2"
    await storage.close()

    # Reopen a fresh instance over the same file and confirm persistence.
    reopened = ChannelRegistryStorage(db_path=storage.db_path)
    await reopened.initialize()
    persisted = await reopened.get_channels_for_model("gpt-6.1-sol-v2")
    assert len(persisted) == 1
    assert persisted[0].channel_id == "#716"
    assert persisted[0].model_name == "gpt-6.1-sol-v2"
    assert persisted[0].pricing.multiplier == 0.0164
    await reopened.close()


# ---------------------------------------------------------------------------
# 5. Filtering and deterministic ordering
# ---------------------------------------------------------------------------


async def _seed_filter_fixture(storage: ChannelRegistryStorage) -> None:
    nodes = [
        make_node(channel_id="#2", model_name="m1", capabilities=_capabilities(True, True, 1_024_000)),
        make_node(channel_id="#1", model_name="m1", capabilities=_capabilities(True, True, 1_024_000)),
        make_node(channel_id="#3", model_name="m1", capabilities=_capabilities(False, True, 1_024_000)),
        make_node(channel_id="#4", model_name="m1", capabilities=_capabilities(True, True, 128_000)),
        make_node(channel_id="#5", model_name="m1", authenticity=_authenticity("differs")),
        make_node(channel_id="#6", model_name="m1", authenticity=_authenticity("untested")),
        make_node(channel_id="#7", model_name="m2", capabilities=_capabilities(True, True, 1_024_000)),
    ]
    for node in nodes:
        await storage.upsert_channel(node)


async def test_exact_model_filter_and_ordering(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    await _seed_filter_fixture(storage)
    result = await storage.get_channels_for_model("m1")
    assert [node.channel_id for node in result] == ["#1", "#2", "#3", "#4", "#5", "#6"]
    await storage.close()


async def test_vision_filter_excludes_false(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    await _seed_filter_fixture(storage)
    result = await storage.get_channels_for_model("m1", vision_required=True)
    ids = [node.channel_id for node in result]
    assert "#3" not in ids  # supports_vision False excluded
    assert ids == ["#1", "#2", "#4", "#5", "#6"]
    await storage.close()


async def test_min_context_filter_excludes_small_window(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    await _seed_filter_fixture(storage)
    result = await storage.get_channels_for_model("m1", min_context=500_000)
    ids = [node.channel_id for node in result]
    assert "#4" not in ids  # 128000 context excluded
    assert ids == ["#1", "#2", "#3", "#5", "#6"]
    await storage.close()


async def test_strict_authenticity_excludes_differs_and_untested(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    await _seed_filter_fixture(storage)
    result = await storage.get_channels_for_model("m1", strict_authenticity=True)
    ids = [node.channel_id for node in result]
    assert "#5" not in ids  # differs
    assert "#6" not in ids  # untested
    assert ids == ["#1", "#2", "#3", "#4"]
    await storage.close()


async def test_combined_filters(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    await _seed_filter_fixture(storage)
    result = await storage.get_channels_for_model(
        "m1", min_context=500_000, vision_required=True, strict_authenticity=True
    )
    assert [node.channel_id for node in result] == ["#1", "#2"]
    await storage.close()


# ---------------------------------------------------------------------------
# 6. Telemetry: concurrency, retention and marketplace preservation
# ---------------------------------------------------------------------------


async def test_thirty_concurrent_telemetry_writes(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    await storage.upsert_channel(make_node())

    await asyncio.gather(
        *(
            storage.record_telemetry("#716", float(index), index % 2 == 0, index % 3 == 0)
            for index in range(30)
        )
    )

    connection = sqlite3.connect(storage.db_path)
    try:
        rows = connection.execute(
            "SELECT latency, success, cache_hit FROM channel_telemetry_history"
        ).fetchall()
    finally:
        connection.close()

    assert len(rows) == 30
    latencies = sorted(int(row[0]) for row in rows)
    assert latencies == list(range(30))
    assert sum(int(row[1]) for row in rows) == 15  # even indices
    assert sum(int(row[2]) for row in rows) == 10  # multiples of three
    await storage.close()


async def test_bounded_retention_keeps_newest_and_preserves_sla(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path, telemetry_retention=5)
    await storage.upsert_channel(make_node())

    before = _node_json(storage)
    for index in range(8):
        await storage.record_telemetry("#716", float(index), True, False)

    connection = sqlite3.connect(storage.db_path)
    try:
        rows = connection.execute(
            "SELECT latency FROM channel_telemetry_history ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    retained = sorted(int(row[0]) for row in rows)
    assert retained == [3, 4, 5, 6, 7]  # newest five only

    after = _node_json(storage)
    assert after == before
    stored = json.loads(after)
    assert stored["metrics"]["sla_24h"] == 100.0
    assert stored["metrics"]["sla_7d"] == 100.0
    await storage.close()


# ---------------------------------------------------------------------------
# 7. Input errors and post-close behaviour
# ---------------------------------------------------------------------------


async def test_telemetry_input_errors(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    await storage.upsert_channel(make_node())

    with pytest.raises(KeyError):
        await storage.record_telemetry("#404", 1.0, True, False)
    with pytest.raises(ValueError):
        await storage.record_telemetry("#716", -0.5, True, False)
    with pytest.raises(ValueError):
        await storage.record_telemetry("#716", float("nan"), True, False)
    with pytest.raises(ValueError):
        await storage.record_telemetry("#716", float("inf"), True, False)
    with pytest.raises(TypeError):
        await storage.record_telemetry("#716", "1.0", True, False)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await storage.record_telemetry("#716", 1.0, 1, False)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await storage.record_telemetry(716, 1.0, True, False)  # type: ignore[arg-type]
    await storage.close()


async def test_query_input_errors(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    with pytest.raises(TypeError):
        await storage.get_channels_for_model(5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await storage.get_channels_for_model("m1", min_context=True)
    with pytest.raises(ValueError):
        await storage.get_channels_for_model("m1", min_context=-1)
    with pytest.raises(TypeError):
        await storage.get_channels_for_model("m1", vision_required=1)  # type: ignore[arg-type]
    await storage.close()


async def test_initialize_is_required_before_use(tmp_path: Path) -> None:
    storage = ChannelRegistryStorage(db_path=tmp_path / "registry.db")
    with pytest.raises(RuntimeError):
        await storage.upsert_channel(make_node())
    with pytest.raises(RuntimeError):
        await storage.get_channels_for_model("gpt-6.1-sol")
    with pytest.raises(RuntimeError):
        await storage.record_telemetry("#716", 1.0, True, False)


async def test_close_is_idempotent_and_rejects_further_calls(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    await storage.upsert_channel(make_node())
    await storage.close()
    await storage.close()  # idempotent

    with pytest.raises(RuntimeError):
        await storage.upsert_channel(make_node())
    with pytest.raises(RuntimeError):
        await storage.get_channels_for_model("gpt-6.1-sol")
    with pytest.raises(RuntimeError):
        await storage.record_telemetry("#716", 1.0, True, False)


async def test_connections_closed_even_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gateway.engine.registry.storage as storage_module

    created: list[Any] = []
    real_connect = storage_module.sqlite3.connect

    class _TrackingConnection:
        def __init__(self, real: sqlite3.Connection) -> None:
            self._real = real
            self.closed = False

        def __getattr__(self, item: str) -> Any:
            return getattr(self._real, item)

        def close(self) -> None:
            self.closed = True
            self._real.close()

    def tracking_connect(*args: Any, **kwargs: Any) -> _TrackingConnection:
        wrapper = _TrackingConnection(real_connect(*args, **kwargs))
        created.append(wrapper)
        return wrapper

    monkeypatch.setattr(storage_module.sqlite3, "connect", tracking_connect)

    storage = await new_storage(tmp_path)
    await storage.upsert_channel(make_node())
    with pytest.raises(KeyError):
        await storage.record_telemetry("#missing", 1.0, True, False)

    assert len(created) >= 3
    assert all(connection.closed for connection in created)

    # A subsequent valid operation still works, proving no leaked/locked handle.
    await storage.record_telemetry("#716", 1.0, True, False)
    await storage.close()
    assert all(connection.closed for connection in created)


# ---------------------------------------------------------------------------
# 8. Cancellation / close lifecycle (deterministic, event-driven)
# ---------------------------------------------------------------------------


class _BlockingOperation:
    """A worker-thread operation that blocks until the test releases it."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.started = asyncio.Event()
        self._gate = threading.Event()
        self._loop = loop

    def __call__(self) -> str:
        self._loop.call_soon_threadsafe(self.started.set)
        self._gate.wait()
        return "done"

    def release(self) -> None:
        self._gate.set()


async def test_single_cancellation_waits_for_admitted_worker(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path, max_concurrent_operations=1)
    loop = asyncio.get_running_loop()
    op = _BlockingOperation(loop)

    task = asyncio.create_task(storage._run_db(op))
    await op.started.wait()
    try:
        task.cancel()
        await _drive(loop)
        # Worker still blocked: the permit must NOT be released yet.
        assert storage._active == 1
        assert not storage._drained.is_set()
        assert not task.done()

        close_task = asyncio.create_task(storage.close())
        await _drive(loop)
        assert not close_task.done()  # close waits for admitted work
    finally:
        op.release()
        await _drive(loop, 32)
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
    assert storage._active == 0
    assert storage._drained.is_set()


async def test_close_waits_for_blocked_worker(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path, max_concurrent_operations=1)
    loop = asyncio.get_running_loop()
    op = _BlockingOperation(loop)

    task = asyncio.create_task(storage._run_db(op))
    await op.started.wait()
    close_task = asyncio.create_task(storage.close())
    try:
        await _drive(loop)
        assert not close_task.done()
        assert storage._active == 1
    finally:
        op.release()
        await _drive(loop, 32)
        await asyncio.gather(task, return_exceptions=True)
    await asyncio.gather(close_task)
    assert close_task.done()
    assert storage._active == 0


async def test_second_cancellation_must_not_release_admitted_worker(tmp_path: Path) -> None:
    """Regression guard for storage.py ``_run_db`` lines 172-182.

    A first ``CancelledError`` shields the admitted worker until its thread
    finishes.  A second cancellation delivered while the second ``shield``
    wait is pending must NOT release the admission permit, clear the drained
    state, or allow ``close()`` to complete while the worker thread is still
    inside SQLite.  This test asserts the required behaviour and therefore
    fails against the current implementation, which releases prematurely.
    """

    storage = await new_storage(tmp_path, max_concurrent_operations=1)
    loop = asyncio.get_running_loop()
    op = _BlockingOperation(loop)

    task = asyncio.create_task(storage._run_db(op))
    await op.started.wait()
    close_task: asyncio.Task[None] | None = None
    try:
        # Deliver a second cancellation deterministically: cancel now, then
        # schedule another cancel on the next loop turn (call_soon preserves
        # FIFO ordering, so the second cancel lands while the coroutine is
        # suspended on its second shield(worker) wait).
        task.cancel()
        loop.call_soon(task.cancel)
        await _drive(loop)

        close_task = asyncio.create_task(storage.close())
        await _drive(loop)

        # Required contract: worker thread is still blocked, so admission is
        # still held and close() must not have completed.
        assert storage._active == 1, "admission released while worker still blocked"
        assert not storage._drained.is_set(), "drained set while worker still blocked"
        assert not close_task.done(), "close() completed while worker still blocked"
    finally:
        op.release()
        await _drive(loop, 32)
        await asyncio.gather(task, return_exceptions=True)
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
    assert storage._active == 0
    assert storage._drained.is_set()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _scalar(storage: ChannelRegistryStorage, sql: str) -> int:
    connection = sqlite3.connect(storage.db_path)
    try:
        return int(connection.execute(sql).fetchone()[0])
    finally:
        connection.close()


def _node_json(storage: ChannelRegistryStorage) -> str:
    connection = sqlite3.connect(storage.db_path)
    try:
        return str(connection.execute("SELECT node_json FROM channels").fetchone()[0])
    finally:
        connection.close()

# ---------------------------------------------------------------------------
# O1.QA — offline marketplace seeder regression tests.
# APPENDED ONLY: every byte above is preserved verbatim. All marketplace values
# are illustrative fixtures, not verified measurements.
# ---------------------------------------------------------------------------

from gateway.engine.registry.seed import (  # noqa: E402  (appended import)
    build_marketplace_cards,
    seed_marketplace_data,
)


async def test_seed_real_sqlite_25_unique_strict_cards_with_supplied_fields(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    try:
        await seed_marketplace_data(storage)
        cards = await storage.list_channels()
    finally:
        await storage.close()

    seed_cards = [card for card in cards if card.channel_id.startswith("seed:")]
    assert len(seed_cards) == 25
    assert len({card.channel_id for card in seed_cards}) == 25
    by_id = {card.channel_id: card for card in seed_cards}

    # Supplied fixture values retained exactly.
    sol = by_id["seed:716:gpt-6.1-sol:discount"]
    assert (sol.metrics.tps, sol.metrics.first_token_latency, sol.metrics.cache_hit_rate) == (71.9, 4.98, 0.706)
    pro = by_id["seed:716:gpt-6.1-sol:us-pro"]
    assert pro.metrics.cache_hit_rate == 0.913
    assert pro.capabilities.supports_vision is True
    assert pro.authenticity.anti_degradation is True
    astra = by_id["seed:716:gpt-6-astra:discount"]
    assert (astra.metrics.tps, astra.metrics.first_token_latency) == (90.1, 2.69)
    opus = by_id["seed:1111:claude-opus-5.5:ccmax"]
    assert opus.metrics.sla_realtime == 100.0
    flash = by_id["seed:2529:deepseek-v4.1-flash:flash"]
    assert (flash.metrics.cache_hit_rate, flash.metrics.tps) == (0.954, 198.0)

    # Both #716 cards are distinct composite IDs (namespaced by model/variant).
    assert sol.channel_id != astra.channel_id
    assert sol.channel_id.startswith("seed:716:") and astra.channel_id.startswith("seed:716:")

    # Provenance / safety: uncertified, untested fingerprint, no fabricated similarity.
    for card in seed_cards:
        assert isinstance(card, ChannelNode)
        assert card.is_certified is False
        assert card.authenticity.fingerprint_status == "untested"
        assert card.authenticity.bazaarlink_similarity is None
        assert "NOT VERIFIED" in card.publisher


async def test_seeding_twice_identical_and_preserves_unrelated_user_card(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    try:
        user_card = make_node(channel_id="#716")  # unrelated: no 'seed:' prefix collision
        await storage.upsert_channel(user_card)
        await seed_marketplace_data(storage)
        first = await storage.list_channels()
        await seed_marketplace_data(storage)
        second = await storage.list_channels()
    finally:
        await storage.close()

    assert first == second  # repeat seeding is idempotent (same content)
    ids = [card.channel_id for card in second]
    assert len(ids) == len(set(ids)) == 26  # 25 seed cards + 1 unrelated user card
    assert len([value for value in ids if value.startswith("seed:")]) == 25
    preserved = next(card for card in second if card.channel_id == "#716")
    assert preserved == user_card  # unrelated user #716 untouched by seeding


async def test_concurrent_repeat_seeds_converge_without_pk_errors(tmp_path: Path) -> None:
    storage = await new_storage(tmp_path)
    try:
        await asyncio.gather(*(seed_marketplace_data(storage) for _ in range(5)))
        cards = await storage.list_channels()
    finally:
        await storage.close()

    ids = [card.channel_id for card in cards]
    assert len(ids) == 25
    assert len(set(ids)) == 25  # no duplicate/PK explosion under concurrency
    expected = {card.channel_id: card.model_dump() for card in build_marketplace_cards()}
    assert {card.channel_id: card.model_dump() for card in cards} == expected


async def test_fresh_builder_independent_and_uninitialized_or_closed_rejected(tmp_path: Path) -> None:
    first = build_marketplace_cards()
    second = build_marketplace_cards()
    # Deterministic content across fresh builds.
    assert [card.model_dump() for card in first] == [card.model_dump() for card in second]
    # Nested lists are fresh per build; mutating one card cannot leak to another.
    first[0].capabilities.endpoints.append("mutation")
    assert "mutation" not in second[0].capabilities.endpoints
    assert "mutation" not in first[1].capabilities.endpoints

    # Uninitialized store is rejected and remains inert (no database created).
    never_initialized = ChannelRegistryStorage(db_path=tmp_path / "never.db")
    with pytest.raises(RuntimeError, match="initialize"):
        await seed_marketplace_data(never_initialized)
    assert not never_initialized.db_path.exists()

    # Seeding never owns the caller's lifecycle: the store stays open, then a
    # closed store rejects further seeding.
    storage = await new_storage(tmp_path)
    await seed_marketplace_data(storage)
    assert await storage.list_channels()
    await storage.close()
    with pytest.raises(RuntimeError):
        await seed_marketplace_data(storage)

"""Bounded asynchronous SQLite storage for channel registry entries."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import threading
from pathlib import Path
from typing import Callable, TypeVar

from gateway.engine.registry.models import ChannelNode

__all__ = ["ChannelRegistryStorage"]

_ResultT = TypeVar("_ResultT")
_DEFAULT_RETENTION = 100_000
_DEFAULT_MAX_CONCURRENT_OPERATIONS = 16


class ChannelRegistryStorage:
    """Async facade over short, independently closed SQLite transactions.

    A bounded semaphore admits at most ``max_concurrent_operations`` worker
    transactions. Cancellation shields an admitted worker until its thread has
    finished, so a permit is never released while SQLite is still in use.
    Call :meth:`initialize` before registry operations and :meth:`close` during
    application shutdown. Calls after closing are rejected.
    """

    def __init__(
        self,
        db_path: str | Path = "data/registry.db",
        telemetry_retention: int = _DEFAULT_RETENTION,
        max_concurrent_operations: int = _DEFAULT_MAX_CONCURRENT_OPERATIONS,
    ) -> None:
        if not isinstance(telemetry_retention, int) or isinstance(
            telemetry_retention, bool
        ) or telemetry_retention < 1:
            raise ValueError("telemetry_retention must be a positive integer")
        if not isinstance(max_concurrent_operations, int) or isinstance(
            max_concurrent_operations, bool
        ) or max_concurrent_operations < 1:
            raise ValueError("max_concurrent_operations must be a positive integer")
        self._db_path = Path(db_path)
        self._telemetry_retention = telemetry_retention
        self._admission = asyncio.BoundedSemaphore(max_concurrent_operations)
        self._sqlite_lock = threading.Lock()
        self._active = 0
        self._drained = asyncio.Event()
        self._drained.set()
        self._initialized = False
        self._closing = False
        self._closed = False

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database file."""

        return self._db_path

    async def initialize(self) -> None:
        """Create the schema and indexes; safe to call repeatedly."""

        await self._run_db(self._initialize_sync, allow_uninitialized=True)
        self._initialized = True

    async def upsert_channel(self, channel: ChannelNode) -> None:
        """Atomically insert or replace a channel by its stable identifier."""

        self._require_initialized()
        payload = json.dumps(channel.model_dump(mode="json"), separators=(",", ":"))
        await self._run_db(
            lambda: self._upsert_sync(channel.channel_id, channel.model_name, payload)
        )

    async def get_channels_for_model(
        self,
        model_name: str,
        min_context: int = 0,
        vision_required: bool = False,
        strict_authenticity: bool = False,
    ) -> list[ChannelNode]:
        """Return matching channels in deterministic ``channel_id`` order.

        Strict authenticity includes only fingerprint-matched channels. Channels
        marked ``differs`` or ``untested`` are intentionally excluded.
        """

        self._require_initialized()
        if not isinstance(model_name, str):
            raise TypeError("model_name must be a string")
        if not isinstance(min_context, int) or isinstance(min_context, bool):
            raise TypeError("min_context must be an integer")
        if min_context < 0:
            raise ValueError("min_context must be non-negative")
        if not isinstance(vision_required, bool) or not isinstance(
            strict_authenticity, bool
        ):
            raise TypeError("filter flags must be booleans")
        rows = await self._run_db(lambda: self._read_model_sync(model_name))
        channels: list[ChannelNode] = []
        for payload in rows:
            channel = ChannelNode.model_validate_json(payload)
            if channel.capabilities.context_window < min_context:
                continue
            if vision_required and not channel.capabilities.supports_vision:
                continue
            if strict_authenticity and channel.authenticity.fingerprint_status != "matched":
                continue
            channels.append(channel)
        channels.sort(key=lambda item: item.channel_id)
        return channels

    async def list_channels(self) -> list[ChannelNode]:
        """Return all current cards, including unbound models, ordered by ID."""
        self._require_initialized()
        rows = await self._run_db(self._read_all_sync)
        return [ChannelNode.model_validate_json(payload) for payload in rows]

    def _read_all_sync(self) -> list[str]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT node_json FROM channels ORDER BY channel_id"
            ).fetchall()
            return [str(row[0]) for row in rows]
        finally:
            connection.close()

    async def record_telemetry(
        self, channel_id: str, latency: float, success: bool, cache_hit: bool
    ) -> None:
        """Durably append one bounded telemetry sample.

        Unknown channel IDs raise ``KeyError`` and therefore cannot create
        orphaned history. This method does not rewrite marketplace SLA fields;
        deriving real time windows is outside this storage node's contract.
        """

        self._require_initialized()
        if not isinstance(channel_id, str):
            raise TypeError("channel_id must be a string")
        if not isinstance(latency, (int, float)) or isinstance(latency, bool):
            raise TypeError("latency must be a finite number")
        if not math.isfinite(float(latency)) or latency < 0.0:
            raise ValueError("latency must be finite and non-negative")
        if not isinstance(success, bool) or not isinstance(cache_hit, bool):
            raise TypeError("success and cache_hit must be booleans")
        await self._run_db(
            lambda: self._record_telemetry_sync(
                channel_id, float(latency), success, cache_hit
            )
        )

    async def close(self) -> None:
        """Stop admissions and drain admitted worker transactions idempotently."""

        if self._closed:
            return
        self._closing = True
        await asyncio.shield(self._drained.wait())
        self._closed = True

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("ChannelRegistryStorage.initialize() is required")
        if self._closing or self._closed:
            raise RuntimeError("ChannelRegistryStorage is closed")

    async def _run_db(
        self, operation: Callable[[], _ResultT], *, allow_uninitialized: bool = False
    ) -> _ResultT:
        if self._closing or self._closed:
            raise RuntimeError("ChannelRegistryStorage is closed")
        if not allow_uninitialized and not self._initialized:
            raise RuntimeError("ChannelRegistryStorage.initialize() is required")
        await self._admission.acquire()
        if self._closing or self._closed:
            self._admission.release()
            raise RuntimeError("ChannelRegistryStorage is closed")
        self._active += 1
        self._drained.clear()
        worker = asyncio.create_task(asyncio.to_thread(self._run_locked, operation))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Keep the admission permit until the shielded worker has settled,
            # even when cancellation is delivered repeatedly while waiting.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if worker.done():
                # Retrieve a worker exception before preserving the caller's
                # cancellation, avoiding an unretrieved-task warning.
                try:
                    worker.result()
                except BaseException:
                    pass
            raise
        finally:
            self._active -= 1
            self._admission.release()
            if self._active == 0:
                self._drained.set()

    def _run_locked(self, operation: Callable[[], _ResultT]) -> _ResultT:
        with self._sqlite_lock:
            return operation()

    def _connect(self) -> sqlite3.Connection:
        """Open and configure one connection; caller always closes it."""

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=5.0)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
            busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
            foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
            if journal_mode.lower() != "wal" or synchronous != 1 or busy_timeout != 5000 or foreign_keys != 1:
                raise RuntimeError("SQLite connection pragmas did not apply")
            return connection
        except BaseException:
            connection.close()
            raise

    def _initialize_sync(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS channels (
                    channel_id TEXT PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    node_json TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS channel_telemetry_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    latency REAL NOT NULL,
                    success INTEGER NOT NULL,
                    cache_hit INTEGER NOT NULL,
                    FOREIGN KEY(channel_id) REFERENCES channels(channel_id)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_channels_model ON channels(model_name)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_telemetry_channel_time "
                "ON channel_telemetry_history(channel_id, timestamp)"
            )
            connection.commit()
        finally:
            connection.close()

    def _upsert_sync(self, channel_id: str, model_name: str, payload: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO channels(channel_id, model_name, node_json) VALUES (?, ?, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET model_name=excluded.model_name, "
                "node_json=excluded.node_json",
                (channel_id, model_name, payload),
            )
            connection.commit()
        finally:
            connection.close()

    def _read_model_sync(self, model_name: str) -> list[str]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT node_json FROM channels WHERE model_name = ?", (model_name,)
            ).fetchall()
            return [str(row[0]) for row in rows]
        finally:
            connection.close()

    def _record_telemetry_sync(
        self, channel_id: str, latency: float, success: bool, cache_hit: bool
    ) -> None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT 1 FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown channel_id: {channel_id}")
            connection.execute(
                "INSERT INTO channel_telemetry_history "
                "(channel_id, timestamp, latency, success, cache_hit) "
                "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?, ?)",
                (channel_id, latency, int(success), int(cache_hit)),
            )
            connection.execute(
                "DELETE FROM channel_telemetry_history WHERE id NOT IN ("
                "SELECT id FROM channel_telemetry_history ORDER BY id DESC LIMIT ?)",
                (self._telemetry_retention,),
            )
            connection.commit()
        finally:
            connection.close()

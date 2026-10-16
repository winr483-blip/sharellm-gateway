"""SQLite-backed channel registry and bounded telemetry history."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway.models import ChannelNode

__all__ = ["ChannelRegistryStorage"]


class ChannelRegistryStorage:
    """Async facade over SQLite using short worker-thread transactions."""

    def __init__(self, db_path: str | Path = "data/registry.db") -> None:
        self._db_path = Path(db_path)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
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
                recorded_at TEXT NOT NULL,
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
            "ON channel_telemetry_history(channel_id, recorded_at)"
        )
        return connection

    async def upsert_channel(self, channel: ChannelNode) -> None:
        payload = json.dumps(channel.model_dump(mode="json"), separators=(",", ":"))
        await asyncio.to_thread(self._upsert, channel.channel_id, channel.model_name, payload)

    def _upsert(self, channel_id: str, model_name: str, payload: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO channels(channel_id, model_name, node_json) VALUES (?, ?, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET model_name=excluded.model_name, "
                "node_json=excluded.node_json",
                (channel_id, model_name, payload),
            )

    async def get_channels_for_model(
        self,
        model_name: str,
        min_context: int = 0,
        vision_required: bool = False,
        strict_authenticity: bool = False,
    ) -> list[ChannelNode]:
        rows = await asyncio.to_thread(self._read_model_rows, model_name)
        channels: list[ChannelNode] = []
        for row in rows:
            channel = ChannelNode.model_validate(json.loads(row))
            if channel.capabilities.context_window < min_context:
                continue
            if vision_required and not channel.capabilities.supports_vision:
                continue
            if strict_authenticity and channel.authenticity.fingerprint_status != "matched":
                continue
            channels.append(channel)
        channels.sort(key=self._sort_key)
        return channels

    def _read_model_rows(self, model_name: str) -> list[str]:
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT node_json FROM channels WHERE model_name = ?", (model_name,)
            )
            return [str(row[0]) for row in cursor.fetchall()]

    @staticmethod
    def _sort_key(channel: ChannelNode) -> tuple[int, int, float, float, float, str]:
        authenticity_rank = {"matched": 2, "untested": 1, "differs": 0}
        return (
            -int(channel.is_certified),
            -authenticity_rank[channel.authenticity.fingerprint_status],
            -channel.metrics.sla_realtime,
            -channel.metrics.sla_24h,
            -float(channel.metrics.community_hearts),
            channel.channel_id,
        )

    async def record_telemetry(
        self, channel_id: str, latency: float, success: bool, cache_hit: bool
    ) -> None:
        if latency < 0.0:
            raise ValueError("latency must be non-negative")
        await asyncio.to_thread(
            self._record_telemetry, channel_id, latency, success, cache_hit
        )

    def _record_telemetry(
        self, channel_id: str, latency: float, success: bool, cache_hit: bool
    ) -> None:
        recorded_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown channel_id: {channel_id}")
            connection.execute(
                "INSERT INTO channel_telemetry_history "
                "(channel_id, recorded_at, latency, success, cache_hit) VALUES (?, ?, ?, ?, ?)",
                (channel_id, recorded_at, latency, int(success), int(cache_hit)),
            )
            self._refresh_metrics(connection, channel_id)

    def _refresh_metrics(self, connection: sqlite3.Connection, channel_id: str) -> None:
        samples = connection.execute(
            "SELECT latency, success, cache_hit FROM channel_telemetry_history "
            "WHERE channel_id = ? ORDER BY id", (channel_id,)
        ).fetchall()
        if not samples:
            return
        row = connection.execute(
            "SELECT node_json FROM channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            return
        data: dict[str, Any] = json.loads(str(row[0]))
        successes = sum(int(sample[1]) for sample in samples)
        cache_hits = sum(int(sample[2]) for sample in samples)
        metrics = data["metrics"]
        metrics["first_token_latency"] = float(samples[-1][0])
        metrics["cache_hit_rate"] = cache_hits / len(samples)
        metrics["sla_realtime"] = successes / len(samples)
        metrics["sla_24h"] = successes / len(samples)
        metrics["sla_7d"] = successes / len(samples)
        connection.execute(
            "UPDATE channels SET node_json = ? WHERE channel_id = ?",
            (json.dumps(data, separators=(",", ":")), channel_id),
        )

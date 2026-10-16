"""Explicit desktop entry point. No GUI, socket or filesystem work on import.

Optional pywebview and Windows WebView2 must be supplied by the operator.
This is packaging preparation, not evidence of a working frozen executable.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
from logging.handlers import RotatingFileHandler
import math
import multiprocessing
import os
from pathlib import Path
import socket
import sys
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Protocol, cast

import uvicorn
from fastapi import FastAPI

from gateway.config import load_config
from gateway.main import create_app

DASHBOARD_URL = "http://127.0.0.1:8000/dashboard"


class DesktopError(RuntimeError):
    """Actionable desktop startup or shutdown failure."""


class ServerBackend(Protocol):
    def run(self, listener: socket.socket, ready: threading.Event) -> None: ...
    def request_stop(self) -> None: ...
    def verify_closed(self) -> None: ...


class WindowEvents(Protocol):
    closed: object


class Window(Protocol):
    events: WindowEvents


class WebView(Protocol):
    def create_window(
        self, title: str, url: str, *, background_color: str,
    ) -> Window: ...
    def start(self) -> None: ...


def load_webview() -> WebView:
    """Import optional native UI only at explicit launch, never at import."""
    try:
        module = importlib.import_module("webview")
    except ImportError as exc:
        raise DesktopError(
            "Desktop UI unavailable. Install pywebview and the Windows WebView2 "
            "runtime in an authorized environment, then retry. No install was attempted."
        ) from exc
    if not callable(getattr(module, "create_window", None)) or not callable(
        getattr(module, "start", None)
    ):
        raise DesktopError("Installed webview module does not expose the pywebview API.")
    return cast(WebView, module)


def resource_root() -> Path:
    """Read-only bundle root; never used for user configuration or databases."""
    frozen_root: object = getattr(sys, "_MEIPASS", None)
    if isinstance(frozen_root, str):
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parents[3]


def user_directory(explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise DesktopError("LOCALAPPDATA is unavailable; supply --user-dir PATH.")
    return (Path(local) / "ShareLLM-Gateway").resolve()


def prepare_config(directory: Path) -> Path:
    """Create a safe empty configuration only on explicit launch; never overwrite."""
    directory = directory.resolve()
    frozen_root: object = getattr(sys, "_MEIPASS", None)
    if isinstance(frozen_root, str) and directory.is_relative_to(Path(frozen_root).resolve()):
        raise DesktopError("User data must not be stored in the temporary frozen bundle.")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.yaml"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(
                "# Configuration needed: add your own providers and routes.\n"
                "# No sample credentials, automatic seeding or outbound routes.\n"
                "gateway:\n  host: 127.0.0.1\n  port: 8000\n  log_level: info\n"
                "providers: {}\nroutes: {}\nregistry_db_path: data/registry.db\n"
            )
    except FileExistsError:
        pass
    config = load_config(path)
    if config.gateway.host != "127.0.0.1" or config.gateway.port != 8000:
        raise DesktopError("Desktop mode requires gateway.host=127.0.0.1 and port=8000.")
    if not Path(config.registry_db_path).resolve().is_relative_to(directory):
        raise DesktopError("Desktop registry_db_path must stay inside the user directory.")
    return path


def reserve_listener() -> socket.socket:
    """Own the exact loopback port before readiness; never attach to another server."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 8000))
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise


class _OwnedServer(uvicorn.Server):
    """Signal only after Uvicorn's lifespan and owned listener have started."""

    def __init__(self, app: FastAPI) -> None:
        self._lifespan_entered = threading.Event()
        self._lifespan_closed = threading.Event()
        original = app.router.lifespan_context

        @asynccontextmanager
        async def tracked(application: FastAPI) -> AsyncIterator[None]:
            self._lifespan_entered.set()
            try:
                async with original(application):
                    yield
            finally:
                self._lifespan_closed.set()

        app.router.lifespan_context = tracked
        super().__init__(uvicorn.Config(
            app, host="127.0.0.1", port=8000, loop="asyncio", http="h11",
            ws="none", lifespan="on", log_config=None, access_log=False,
            timeout_graceful_shutdown=5, workers=1,
        ))
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._shutdown_complete = False
        self._app = app

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started and not self.should_exit and not self._stop.is_set():
            self._ready.set()

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        await super().shutdown(sockets=sockets)
        self._shutdown_complete = True

    def request_stop(self) -> None:
        self._stop.set()
        self.should_exit = True
        with self._lock:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._cancel_startup)

    def _cancel_startup(self) -> None:
        if not self.started and self._task is not None:
            self._task.cancel()

    async def _run_owned(self, listener: socket.socket) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
        try:
            if not self._stop.is_set():
                await self.serve(sockets=[listener])
        except asyncio.CancelledError:
            if not self._stop.is_set():
                raise
        finally:
            try:
                if self.started and not self._shutdown_complete:
                    await self.shutdown(sockets=[listener])
                elif self._lifespan_entered.is_set() and not self._lifespan_closed.is_set():
                    # Startup may be cancelled after lifespan entered but before
                    # listener creation. Signal shutdown only once startup settled;
                    # asyncio.run cancels outstanding lifespan tasks otherwise.
                    if self.lifespan.startup_event.is_set():
                        await asyncio.wait_for(self.lifespan.shutdown(), timeout=5.0)
            finally:
                with self._lock:
                    self._loop = None
                    self._task = None

    def run_owned(self, listener: socket.socket, ready: threading.Event) -> None:
        self._ready = ready
        asyncio.run(self._run_owned(listener))

    def verify_closed(self) -> None:
        if self._lifespan_entered.is_set() and not self._lifespan_closed.is_set():
            raise DesktopError("Application lifespan cleanup has not completed.")
        client = getattr(self._app.state, "http_client", None)
        if client is not None and not client.is_closed:
            raise DesktopError("Application HTTP client remains open.")
        # Registry closure is awaited inside the existing app lifespan; no
        # private storage flags are inspected or modified here.


class UvicornBackend:
    def __init__(self, config_path: Path) -> None:
        self.server = _OwnedServer(create_app(config_path))

    def run(self, listener: socket.socket, ready: threading.Event) -> None:
        self.server.run_owned(listener, ready)

    def request_stop(self) -> None:
        self.server.request_stop()

    def verify_closed(self) -> None:
        self.server.verify_closed()


class DesktopController:
    """Single-use controller with injectable backend/socket factory for offline QA.

    Start/close calls are serialized. Backend failure is retained and raised on
    startup or final close. A stuck backend is reported after bounded join; no
    unsafe thread kill or false successful-cleanup claim is made.
    """

    def __init__(
        self, backend: ServerBackend, *,
        listener_factory: Callable[[], socket.socket] = reserve_listener,
        startup_timeout: float = 15.0, shutdown_timeout: float = 10.0,
    ) -> None:
        for value in (startup_timeout, shutdown_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Lifecycle timeouts must be finite and positive.")
        self.backend = backend
        self.listener_factory = listener_factory
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.ready = threading.Event()
        self.settled = threading.Event()
        self.failure: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | None = None
        self._closed = False
        self._lock = threading.RLock()

    def _run(self) -> None:
        try:
            assert self._listener is not None
            self.backend.run(self._listener, self.ready)
            if not self.ready.is_set() and not self._closed:
                raise DesktopError("Server exited before owned listener became ready.")
        except BaseException as exc:
            self.failure = exc
        finally:
            self.settled.set()
            self.ready.set()  # Wake startup wait also on failure, never imply success.

    def start(self) -> None:
        with self._lock:
            if self._closed or self._thread is not None:
                raise DesktopError("Desktop controller is single-use or already closed.")
            try:
                self._listener = self.listener_factory()
                self._thread = threading.Thread(target=self._run, name="sharellm-server", daemon=True)
                self._thread.start()
                if not self.ready.wait(self.startup_timeout):
                    raise DesktopError("Server startup timed out; shutdown requested.")
                if self.failure is not None or self.settled.is_set():
                    raise DesktopError("Server failed before window startup.") from self.failure
            except BaseException:
                self.close()
                raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self.backend.request_stop()
            thread = self._thread
            if thread is not None and thread.ident is not None:
                thread.join(self.shutdown_timeout)
                if thread.is_alive():
                    raise DesktopError(
                        "Server thread did not stop within the shutdown deadline; "
                        "cleanup is NOT confirmed. No thread was force-killed."
                    )
            if self._listener is not None:
                self._listener.close()
                self._listener = None
            self.backend.verify_closed()
            if self.failure is not None:
                raise DesktopError("Server lifecycle failed; inspect desktop.log.") from self.failure


def run_window(controller: DesktopController, webview: WebView) -> None:
    """Native GUI loop stays on the main thread; cleanup runs on every exit."""
    if threading.current_thread() is not threading.main_thread():
        raise DesktopError("The native GUI must run on the main thread.")
    try:
        controller.start()
        window = webview.create_window(
            "ShareLLM Gateway — configure providers in config.yaml",
            DASHBOARD_URL, background_color="#090c15",
        )
        # pywebview Event.__iadd__ registers callbacks. Its optional dependency
        # is adapted here instead of importing it or suppressing type checking.
        event = window.events.closed
        register = getattr(event, "__iadd__", None)
        if not callable(register):
            raise DesktopError("pywebview closed event does not support registration.")
        register(controller.close)
        webview.start()
    finally:
        controller.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShareLLM optional desktop launcher")
    parser.add_argument("--user-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    handler: RotatingFileHandler | None = None
    try:
        directory = user_directory(args.user_dir)
        config_path = prepare_config(directory)
        handler = RotatingFileHandler(
            directory / "desktop.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8",
        )
        logging.getLogger().addHandler(handler)
        webview = load_webview()
        run_window(DesktopController(UvicornBackend(config_path)), webview)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.getLogger(__name__).error("Desktop launch failed: %s", exc)
        if sys.stderr is not None:
            print(f"Desktop launch failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())

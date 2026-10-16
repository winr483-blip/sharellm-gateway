"""O2.QA — offline desktop launcher lifecycle, resource and packaging tests.

Hermetic: no GUI, server bind, network, install or PyInstaller build is
performed. Behaviour is exercised through injected fake backends, listeners and
webview objects plus ``threading.Event`` handshakes (no sleeps/poll loops).
All native/packaging claims are preparation-only, not executable-build proof.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from gateway.desktop import launcher
from gateway.desktop.launcher import (
    DASHBOARD_URL,
    DesktopController,
    DesktopError,
    prepare_config,
    resource_root,
    run_window,
    user_directory,
)

_REPO = Path(__file__).resolve().parents[1]
_SPEC = _REPO / "packaging" / "sharellm-gateway.spec"

# ---------------------------------------------------------------------------
# Offline fakes
# ---------------------------------------------------------------------------


class FakeListener:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        return -1 if self.closed else 7


class FakeBackend:
    """Injectable ServerBackend; 'ready' mode blocks until request_stop."""

    def __init__(self, mode: str = "ready") -> None:
        self.mode = mode
        self.started = threading.Event()
        self._stop = threading.Event()
        self.stop_calls = 0
        self.verify_calls = 0

    def run(self, listener: Any, ready: threading.Event) -> None:
        self.started.set()
        if self.mode == "ready":
            ready.set()
            self._stop.wait(5)
        elif self.mode == "exits_before_ready":
            return
        elif self.mode == "raises":
            raise RuntimeError("backend boom")
        elif self.mode == "stuck":
            self._stop.wait(5)  # never signals readiness

    def request_stop(self) -> None:
        self.stop_calls += 1
        self._stop.set()

    def verify_closed(self) -> None:
        self.verify_calls += 1


class FakeClosed:
    def __init__(self) -> None:
        self.callbacks: list[Any] = []

    def __iadd__(self, callback: Any) -> "FakeClosed":
        self.callbacks.append(callback)
        return self


class FakeEvents:
    def __init__(self) -> None:
        self.closed = FakeClosed()


class FakeWindow:
    def __init__(self) -> None:
        self.events = FakeEvents()


class FakeWebView:
    def __init__(self, *, create_error: bool = False, start_error: bool = False,
                 on_create: Any = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.started = 0
        self.created: FakeWindow | None = None
        self._create_error = create_error
        self._start_error = start_error
        self._on_create = on_create

    def create_window(self, title: str, url: str, *, background_color: str) -> FakeWindow:
        self.calls.append((title, url, background_color))
        if self._on_create is not None:
            self._on_create()
        if self._create_error:
            raise RuntimeError("create boom")
        self.created = FakeWindow()
        return self.created

    def start(self) -> None:
        self.started += 1
        if self._start_error:
            raise RuntimeError("start boom")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_import_is_inert_and_missing_webview_is_actionable() -> None:
    sys.modules.pop("webview", None)
    importlib.import_module("gateway.desktop")
    importlib.import_module("gateway.desktop.launcher")
    # Importing the optional-UI entry point must not import pywebview.
    assert "webview" not in sys.modules
    assert callable(launcher.load_webview)

    if importlib.util.find_spec("webview") is not None:
        pytest.skip("webview installed; missing-dependency path not applicable")
    with pytest.raises(DesktopError) as exc:
        launcher.load_webview()
    message = str(exc.value)
    assert "pywebview" in message
    assert "No install was attempted" in message


def test_run_window_requires_main_thread() -> None:
    backend = FakeBackend("ready")
    controller = DesktopController(backend, listener_factory=FakeListener)
    webview = FakeWebView()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            run_window(controller, webview)
        except BaseException as exc:  # noqa: BLE001 - captured for assertion
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(5)

    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], DesktopError)
    assert "main thread" in str(errors[0])
    # Guard fires before any backend/webview work begins.
    assert backend.stop_calls == 0 and webview.calls == [] and webview.started == 0


def test_readiness_before_window_with_exact_url_and_dark_background() -> None:
    backend = FakeBackend("ready")
    controller = DesktopController(backend, listener_factory=FakeListener)
    observed: dict[str, bool] = {}
    webview = FakeWebView(on_create=lambda: observed.__setitem__("ready", controller.ready.is_set()))

    run_window(controller, webview)

    assert observed.get("ready") is True  # readiness signalled before window creation
    assert len(webview.calls) == 1
    title, url, background = webview.calls[0]
    assert url == DASHBOARD_URL == "http://127.0.0.1:8000/dashboard"
    assert background == "#090c15"
    assert "ShareLLM" in title
    assert webview.started == 1
    assert webview.created is not None
    assert webview.created.events.closed.callbacks == [controller.close]
    # finally-cleanup ran exactly through the ctor's controller.
    assert backend.verify_calls >= 1
    assert backend.started.is_set()


def test_close_is_idempotent_joins_thread_closes_listener_and_verifies() -> None:
    backend = FakeBackend("ready")
    controller = DesktopController(backend, listener_factory=FakeListener)
    controller.start()
    listener = controller._listener
    thread = controller._thread
    assert controller.ready.is_set()
    assert listener is not None and thread is not None

    controller.close()

    assert listener.closed is True
    assert controller._listener is None
    assert not thread.is_alive()
    assert backend.stop_calls >= 1 and backend.verify_calls >= 1

    # Second close is a no-op (idempotent), not an error.
    controller.close()
    assert controller._listener is None


def test_window_or_start_failure_triggers_cleanup_without_false_window() -> None:
    # create_window raises -> cleanup, no completed window.
    backend = FakeBackend("ready")
    controller = DesktopController(backend, listener_factory=FakeListener)
    with pytest.raises(RuntimeError, match="create boom"):
        run_window(controller, FakeWebView(create_error=True))
    assert backend.stop_calls >= 1 and backend.verify_calls >= 1
    assert not controller._thread.is_alive() if controller._thread else True

    # webview.start raises -> cleanup still runs.
    backend2 = FakeBackend("ready")
    controller2 = DesktopController(backend2, listener_factory=FakeListener)
    with pytest.raises(RuntimeError, match="start boom"):
        run_window(controller2, FakeWebView(start_error=True))
    assert backend2.stop_calls >= 1 and backend2.verify_calls >= 1

    # Backend exits before readiness -> bounded failure, no window is created.
    exits = FakeBackend("exits_before_ready")
    controller3 = DesktopController(exits, listener_factory=FakeListener)
    webview3 = FakeWebView()
    with pytest.raises(DesktopError, match="lifecycle failed"):
        run_window(controller3, webview3)
    assert webview3.calls == [] and exits.verify_calls >= 1

    # Backend raises -> failure surfaced (close reports the lifecycle failure).
    raiser = FakeBackend("raises")
    controller4 = DesktopController(raiser, listener_factory=FakeListener)
    with pytest.raises(DesktopError, match="lifecycle failed"):
        controller4.start()
    assert raiser.verify_calls >= 1


def test_listener_factory_failure_cleans_up_and_propagates() -> None:
    def boom() -> Any:
        raise OSError("bind refused")

    backend = FakeBackend("ready")
    controller = DesktopController(backend, listener_factory=boom)
    with pytest.raises(OSError, match="bind refused"):
        controller.start()
    assert controller._thread is None
    assert backend.stop_calls >= 1 and backend.verify_calls >= 1


def test_startup_timeout_is_bounded_and_cleaned() -> None:
    backend = FakeBackend("stuck")  # never signals readiness
    controller = DesktopController(
        backend, listener_factory=FakeListener, startup_timeout=0.2
    )
    with pytest.raises(DesktopError, match="timed out"):
        controller.start()
    assert backend.stop_calls >= 1 and backend.verify_calls >= 1
    assert controller._thread is not None and not controller._thread.is_alive()


@pytest.mark.asyncio
async def test_owned_server_tracks_lifespan_and_verify_closed() -> None:
    class _Client:
        def __init__(self) -> None:
            self.is_closed = False

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        app.state.http_client = _Client()
        try:
            yield
        finally:
            app.state.http_client.is_closed = True

    app = FastAPI(lifespan=lifespan)
    server = launcher._OwnedServer(app)  # wraps app.router.lifespan_context
    assert not server._lifespan_entered.is_set()

    async with app.router.lifespan_context(app):
        assert server._lifespan_entered.is_set()
        assert not server._lifespan_closed.is_set()
        # Lifespan not settled -> cleanup is not confirmed yet.
        with pytest.raises(DesktopError, match="lifespan cleanup"):
            server.verify_closed()

    assert server._lifespan_closed.is_set()
    assert app.state.http_client.is_closed is True
    server.verify_closed()  # settled + client closed -> clean


@pytest.mark.asyncio
async def test_owned_server_readiness_gated_by_started_and_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        yield

    async def fake_super_startup(self: Any, sockets: Any = None) -> None:
        # Model Uvicorn having entered the (tracked) lifespan and started.
        self._lifespan_entered.set()
        self.started = True

    monkeypatch.setattr(launcher.uvicorn.Server, "startup", fake_super_startup)

    server = launcher._OwnedServer(FastAPI(lifespan=lifespan))
    await server.startup()
    assert server._ready.is_set()  # ready only after started / lifespan entered

    stopped = launcher._OwnedServer(FastAPI(lifespan=lifespan))
    stopped._stop.set()
    await stopped.startup()
    assert not stopped._ready.is_set()  # stop request suppresses readiness


def test_packaging_spec_is_valid_offline_preparation() -> None:
    text = _SPEC.read_text(encoding="utf-8")
    tree = ast.parse(text)  # syntax only; the spec is NOT executed
    assert isinstance(tree, ast.Module)
    assert "SPECPATH" in text
    assert "dashboard.html" in text and "config.example.yaml" in text
    assert "'gateway/ui'" in text  # UI bundled under gateway/ui
    assert "pathex=[str(src)]" in text
    assert "console=False" in text and "upx=False" in text
    assert "COLLECT(" not in text  # one-file build (no COLLECT stage call)
    # No user config, credentials or API keys are bundled as data files.
    assert "'config.yaml'" not in text and "('config.yaml'" not in text
    assert "api_key" not in text
    assert text.count("config.example.yaml") == 1  # only the example config is bundled
    assert "PyInstaller" in text  # build-time hooks only; not imported at runtime here


def test_prepare_config_exclusive_persist_and_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "userdata"
    path = prepare_config(directory)
    assert path == directory / "config.yaml" and path.is_file()
    first = path.read_text(encoding="utf-8")
    assert "host: 127.0.0.1" in first and "port: 8000" in first
    assert "providers: {}" in first and "routes: {}" in first

    # Never overwrite an existing config on repeat.
    path.write_text(first + "# sentinel\n", encoding="utf-8")
    again = prepare_config(directory)
    assert again == path
    assert "# sentinel" in path.read_text(encoding="utf-8")

    # Registry DB path escaping the user directory is rejected.
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (escaped / "config.yaml").write_text(
        "gateway:\n  host: 127.0.0.1\n  port: 8000\nproviders: {}\nroutes: {}\n"
        "registry_db_path: ../escape.db\n",
        encoding="utf-8",
    )
    with pytest.raises(DesktopError, match="registry_db_path"):
        prepare_config(escaped)

    # User data rejected inside the frozen temporary bundle.
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(frozen), raising=False)
    with pytest.raises(DesktopError, match="temporary frozen bundle"):
        prepare_config(frozen / "inner")


def test_user_directory_and_resource_root_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(DesktopError, match="LOCALAPPDATA"):
        user_directory()

    explicit = tmp_path / "chosen"
    assert user_directory(explicit) == explicit.resolve()

    # Source mode: bundle root is the repository root.
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert resource_root() == Path(launcher.__file__).resolve().parents[3]

    frozen = tmp_path / "fr"
    frozen.mkdir()
    monkeypatch.setattr(sys, "_MEIPASS", str(frozen), raising=False)
    assert resource_root() == frozen.resolve()

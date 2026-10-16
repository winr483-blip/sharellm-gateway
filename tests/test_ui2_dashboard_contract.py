"""QA UI-2 hermetic acceptance tests for the ShareLLM Studio dashboard.

Added by QA (node UI-2). These tests touch **no production code** and make **no
network call**. They verify the two things the accepted UI-2 node promises that
can be checked deterministically in this environment:

1. ``GET /dashboard`` and ``GET /`` serve ``src/gateway/ui/dashboard.html``
   byte-identically. The route returns a ``FileResponse`` and therefore does not
   require the app lifespan (whose registry-storage WAL init hangs in this
   sandbox — see the QA handoff), so the transport is used without lifespan.
2. The shipped document contains the Studio composition the user asked for:
   left navigation, centre composer + timeline, right model/access/task/artifacts
   inspector, a bottom-ordered composer, 900/560 px responsive breakpoints, the
   retained Market DOM/API hooks, and the original ShareLLM glass/aurora brand —
   with no third-party product branding.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
import yaml

from gateway.main import create_app

pytestmark = pytest.mark.asyncio

_REPO = Path(__file__).resolve().parents[1]
_UI_FILE = _REPO / "src" / "gateway" / "ui" / "dashboard.html"


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "registry_db_path": "channels.db",
                "gateway": {"host": "127.0.0.1", "port": 8000},
                "providers": {"p": {"type": "openai", "base_url": "https://p.test/v1", "api_key": "synthetic"}},
                "routes": {
                    "smart": {
                        "models": ["alias"],
                        "strategy": "matchmaker",
                        "routing_profile": "balanced",
                        "channel_targets": {"card-a": {"provider": "p", "model": "wire-a"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@asynccontextmanager
async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    # Deliberately NO lifespan_context: the dashboard document route is a plain
    # FileResponse and must serve without registry storage being initialised.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        yield client


async def test_dashboard_served_byte_identical_without_lifespan(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path))
    packaged = _UI_FILE.read_bytes()
    assert packaged

    async with _client(app) as client:
        responses = [await client.get("/dashboard"), await client.get("/")]

    for response in responses:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.content == packaged

    assert responses[0].content == responses[1].content


async def test_ui2_studio_composition_is_present(tmp_path: Path) -> None:
    html = _UI_FILE.read_text(encoding="utf-8")

    # Left navigation with three labelled, accessible views.
    assert 'class="studio-shell"' in html
    assert 'class="studio-sidebar glass"' in html
    assert 'class="studio-nav"' in html
    nav_views = re.findall(r'data-studio-view="([a-z]+)"', html)
    assert nav_views == ["workspace", "market", "skills"]
    assert 'data-studio-view="workspace" aria-current="true"' in html
    assert '<nav class="studio-nav" aria-label=' in html

    # Centre composer + timeline; right model/access/task/artifacts inspector.
    for anchor in (
        'id="studio-prompt"',
        'id="studio-timeline"',
        'id="studio-sessions"',
        'id="studio-selected-model"',
        'id="studio-selected-access"',
        'id="studio-detail"',
        'id="studio-artifacts"',
    ):
        assert anchor in html, anchor

    # Composer is ordered below the timeline inside the centre column (bottom
    # composer in normal flow; the accepted node does not make it position:fixed).
    assert ".studio-grid>div>section:first-child{order:3}" in html
    assert ".studio-grid>div>section:nth-child(2){order:1}" in html


async def test_ui2_responsive_breakpoints_present(tmp_path: Path) -> None:
    html = _UI_FILE.read_text(encoding="utf-8")
    assert "@media(max-width:900px)" in html
    assert "@media(max-width:560px)" in html
    # Single-column collapse at the 900px breakpoint.
    assert ".studio-shell{grid-template-columns:1fr;" in html
    assert ".studio-grid{grid-template-columns:1fr}" in html


async def test_ui2_market_dom_and_api_hooks_retained(tmp_path: Path) -> None:
    html = _UI_FILE.read_text(encoding="utf-8")
    for anchor in (
        'id="market-view"',
        'id="channels"',
        'id="channel-count"',
        'id="profile-badge"',
        'id="connection"',
        'data-profile="budget"',
        'data-profile="balanced"',
        'data-profile="performance"',
    ):
        assert anchor in html, anchor
    # The market client still polls the dashboard API surface.
    assert "/v1/dashboard/stats" in html
    assert "/v1/dashboard/channels" in html
    assert "/v1/dashboard/profile" in html


async def test_ui2_original_sharellm_branding_no_third_party(tmp_path: Path) -> None:
    html = _UI_FILE.read_text(encoding="utf-8")
    low = html.lower()
    assert "ShareLLM" in html
    assert "Glass HUD" in html
    # Original glass + aurora layer (multi-stop radial gradients with the
    # ShareLLM indigo/violet/teal palette over a dark base).
    assert "backdrop-filter" in html
    assert "radial-gradient" in html
    assert "#818cf8" in low and "#2dd4bf" in low
    for foreign in ("workbuddy", "coze", "trae", "copilot", "chatgpt", "claude"):
        assert foreign not in low, foreign

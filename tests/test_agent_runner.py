"""Hermetic runner acceptance: no network and only temporary workspace writes."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from gateway.agent.runner import AgentRunner, AgentSession
from gateway.agent.tools import ToolRegistry


def reply(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return httpx.Response(200, json={"choices": [{"message": message}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})


def call(name, arguments, call_id="call-1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.mark.asyncio
async def test_multistep_write_then_done(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    seen = []
    def handle(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            assert len(body["tools"]) == 5
            return reply(calls=[call("write_file", {"path": "answer.txt", "content": "hello"})])
        assert body["messages"][-1]["role"] == "tool"
        assert body["messages"][-1]["tool_call_id"] == "call-1"
        return reply("Done")
    try:
        async with httpx.AsyncClient(base_url="http://gateway.test", transport=httpx.MockTransport(handle)) as client:
            session = AgentSession(messages=[{"role": "system", "content": "Use the workspace tools."}])
            result = await AgentRunner(registry, client, "test-model").run_turn(session, "Write a file")
            assert result == "Done"
            assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "hello"
            assert session.status == "completed" and session.steps_taken == 2
            assert session.total_tokens == 24
            assert len(session.trajectory) == 1 and session.trajectory[0].success
            assert session.trajectory[0].duration_ms >= 0
            assert [m["role"] for m in session.messages] == ["system", "user", "assistant", "tool", "assistant"]
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_step_limit_and_hard_ceiling(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    requests = []
    def handle(request):
        requests.append(request)
        return reply(calls=[call("list_dir", {"path": "."}, f"call-{len(requests)}")])
    try:
        async with httpx.AsyncClient(base_url="http://gateway.test", transport=httpx.MockTransport(handle)) as client:
            runner = AgentRunner(registry, client, "test-model")
            session = AgentSession()
            result = await runner.run_turn(session, "Keep checking", max_steps=2)
            assert len(requests) == 2
            assert session.status == "limit_reached" and session.steps_taken == 2
            assert len(session.trajectory) == 2
            assert "step limit reached (2)" in result
            assert all(m["role"] == "tool" for m in [session.messages[2], session.messages[4]])
            for invalid in (0, 16, True):
                with pytest.raises(ValueError, match="between 1 and 15"):
                    await runner.run_turn(AgentSession(), "invalid", max_steps=invalid)
            assert len(requests) == 2
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_tool_failure_returned_to_model_then_recovers(tmp_path: Path):
    registry = ToolRegistry(tmp_path)
    requests = []
    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return reply(calls=[call("read_file", {"path": "missing.txt", "offset": 0, "limit": 10})])
        tool_message = body["messages"][-1]
        assert tool_message["role"] == "tool"
        assert "Tool error (FileNotFoundError)" in tool_message["content"]
        assert str(tmp_path) not in tool_message["content"]
        return reply("Recovered")
    try:
        async with httpx.AsyncClient(base_url="http://gateway.test", transport=httpx.MockTransport(handle)) as client:
            session = AgentSession()
            result = await AgentRunner(registry, client, "test-model").run_turn(session, "Read missing file")
            assert result == "Recovered" and session.status == "completed"
            assert len(session.trajectory) == 1
            assert session.trajectory[0].success is False
            assert not list(tmp_path.iterdir())
    finally:
        await registry.close()

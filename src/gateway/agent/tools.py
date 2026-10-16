"""Fail-closed, bounded local workspace tools for the agent engine.

The registry is deliberately an explicit-root capability.  It is not an OS
sandbox: callers must provide an exclusively owned private workspace, and the
implementation rejects path traversal, links, protected names, and arbitrary
process execution before touching the filesystem.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "CommandResult",
    "EditArgs",
    "ListArgs",
    "ReadArgs",
    "RunArgs",
    "ToolDefinition",
    "ToolRegistry",
    "WriteArgs",
]

_MAX_BYTES = 1 << 20
_MAX_OUTPUT = 64 << 10
_MAX_LINES = 1_000
_MAX_LIST = 1_000
_DENIED_COMPONENTS = frozenset({".git", ".workbuddy-ai", "__pycache__", ".venv"})
_PROTECTED_NAMES = frozenset({"config.yaml", ".env", "agentdb"})
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ReadArgs(_StrictModel):
    path: str
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=_MAX_LINES, ge=1, le=_MAX_LINES, strict=True)


class WriteArgs(_StrictModel):
    path: str
    content: str


class EditArgs(_StrictModel):
    path: str
    old_str: str
    new_str: str


class ListArgs(_StrictModel):
    path: str = "."


class RunArgs(_StrictModel):
    command: str
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=30.0, strict=True, allow_inf_nan=False)


class _FunctionSpec(_StrictModel):
    name: str
    description: str
    parameters: dict[str, Any]
    strict: Literal[True] = True


class ToolDefinition(_StrictModel):
    type: Literal["function"] = "function"
    function: _FunctionSpec


class CommandResult(TypedDict):
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    truncated: bool


def _strict_schema(model: type[_StrictModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema["additionalProperties"] = False
    props = cast(dict[str, Any], schema.get("properties", {}))
    # OpenAI strict tool schemas require every property to be required.  Local
    # models still retain ergonomic defaults when instantiated directly.
    schema["required"] = list(props)
    return schema


def tool_definitions() -> list[ToolDefinition]:
    specs: list[tuple[str, str, type[_StrictModel]]] = [
        ("read_file", "Read up to 1000 UTF-8 lines from the private workspace.", ReadArgs),
        ("write_file", "Atomically write a UTF-8 file in the private workspace.", WriteArgs),
        ("edit_file", "Atomically replace one exact occurrence in a workspace file.", EditArgs),
        ("list_dir", "List one non-recursive workspace directory.", ListArgs),
        ("run_command", "Run one fixed safe local command from the allowlist.", RunArgs),
    ]
    return [
        ToolDefinition(
            function=_FunctionSpec(
                name=name,
                description=description,
                parameters=_strict_schema(model),
                strict=True,
            )
        )
        for name, description, model in specs
    ]


class ToolRegistry:
    """An explicit-root, bounded registry of safe local operations."""

    def __init__(self, root: Path, *, max_workers: int = 4) -> None:
        if not isinstance(root, Path):
            raise TypeError("root must be pathlib.Path")
        if not root.exists() or not root.is_dir():
            raise ValueError("root must be an existing directory")
        resolved = root.resolve(strict=True)
        home = Path.home().resolve()
        system = Path(os.environ.get("SystemRoot", "C:/Windows")).resolve()
        if resolved == home or resolved == system or system in resolved.parents:
            raise ValueError("root must be a private project directory, not home/system")
        self.root = resolved
        if max_workers < 1 or max_workers > 16:
            raise ValueError("max_workers must be between 1 and 16")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="agent-tool")
        self._admission = asyncio.Semaphore(max_workers)
        self._state_lock = asyncio.Lock()
        self._drained = asyncio.Condition()
        self._active = 0
        self._closed = False
        self._locks = tuple(threading.RLock() for _ in range(32))
        self._python_executable = str(Path(sys.executable).resolve(strict=True))
        git = shutil.which("git")
        self._git_executable = str(Path(git).resolve(strict=True)) if git else None
        if self._git_executable is not None:
            try:
                Path(self._git_executable).relative_to(self.root)
            except ValueError:
                pass
            else:
                self._git_executable = None

    @property
    def definitions(self) -> list[ToolDefinition]:
        return tool_definitions()

    async def close(self) -> None:
        async with self._state_lock:
            self._closed = True
        async with self._drained:
            await self._drained.wait_for(lambda: self._active == 0)
        self._executor.shutdown(wait=True, cancel_futures=False)

    async def _begin(self) -> None:
        await self._admission.acquire()
        async with self._state_lock:
            if self._closed:
                self._admission.release()
                raise RuntimeError("tool registry is closed")
            self._active += 1

    async def _end(self) -> None:
        async with self._state_lock:
            self._active -= 1
        self._admission.release()
        async with self._drained:
            self._drained.notify_all()

    async def _blocking(self, fn: Any, *args: Any) -> Any:
        await self._begin()
        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # Keep admission until the executor finishes, including when
                # the caller is cancelled again during the cleanup wait.
                while not future.done():
                    try:
                        await asyncio.shield(future)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        break
                # Retrieve worker failures without replacing caller cancellation.
                try:
                    future.result()
                except BaseException:
                    pass
                raise
        finally:
            # Also release admission if executor submission itself fails.
            await self._end()

    def _lock_for(self, path: Path) -> threading.RLock:
        return self._locks[hash(path) % len(self._locks)]

    def _parts(self, raw: str) -> list[str]:
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("path must be a non-empty UTF-8 string")
        if ":" in raw or _DRIVE_RE.match(raw) or raw.startswith(("/", "\\", "//")):
            # POSIX absolute paths are accepted only after the explicit-root
            # check below; Windows drive/UNC/device/ADS syntax is never valid.
            if ":" in raw or _DRIVE_RE.match(raw) or raw.startswith(("\\", "//")):
                raise ValueError("absolute Windows/device/ADS paths are forbidden")
        parts = [part for part in re.split(r"[\\/]", raw) if part]
        if any(part == ".." for part in parts):
            raise ValueError("parent path components are forbidden")
        if any(part in _DENIED_COMPONENTS for part in parts):
            raise ValueError("protected workspace component")
        if any(part in _PROTECTED_NAMES for part in parts):
            raise ValueError("protected workspace file")
        return parts

    def _safe_path(self, raw: str, *, directory: bool = False) -> Path:
        parts = self._parts(raw)
        candidate = Path(raw) if Path(raw).is_absolute() else self.root.joinpath(*parts)
        self._check_components(candidate)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("path escapes workspace root") from exc
        self._check_components(resolved)
        if directory and resolved.exists() and not resolved.is_dir():
            raise ValueError("path is not a directory")
        return resolved

    def _check_components(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("path escapes workspace root") from exc
        current = self.root
        for component in relative.parts:
            current /= component
            folded = component.casefold()
            if folded in _DENIED_COMPONENTS or folded in _PROTECTED_NAMES:
                raise ValueError("protected workspace component")
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            reparse = bool(
                getattr(info, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
            if stat.S_ISLNK(info.st_mode) or reparse:
                raise ValueError("symlink/reparse-point components are forbidden")

    @staticmethod
    def _is_reparse(path: Path) -> bool:
        if os.name != "nt":
            return False
        try:
            return bool(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except (AttributeError, OSError):
            return False

    def _check_file(self, path: Path, *, write: bool = False) -> None:
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("target is not a regular file")
            if info.st_nlink > 1:
                raise ValueError("hard-linked files are forbidden")
            if path.is_symlink() or self._is_reparse(path):
                raise ValueError("symlink/reparse-point files are forbidden")
        elif write:
            self._check_components(path.parent)

    @staticmethod
    def _read_sync(path: Path, offset: int, limit: int) -> str:
        with path.open("rb") as handle:
            data = handle.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            raise ValueError("file exceeds 1 MiB read limit")
        text = data.decode("utf-8")
        return "\n".join(text.splitlines()[offset : offset + limit])

    def _write_sync(self, path: Path, content: str) -> str:
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            raise ValueError("content exceeds 1 MiB write limit")
        lock = self._lock_for(path)
        with lock:
            self._check_file(path, write=True)
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            temp = Path(name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._check_components(temp)
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        return f"wrote {path.relative_to(self.root).as_posix()}"

    def _edit_sync(self, path: Path, old: str, new: str) -> str:
        if not old:
            raise ValueError("old_str must not be empty")
        if old == new:
            raise ValueError("edit must change content")
        lock = self._lock_for(path)
        with lock:
            self._check_file(path)
            # Edit the complete bounded byte stream, preserving CRLF/LF and EOF.
            with path.open("rb") as handle:
                data = handle.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise ValueError("file exceeds 1 MiB edit limit")
            original = data.decode("utf-8")
            count = original.count(old)
            if count != 1:
                raise ValueError(f"old_str occurrence count is {count}, expected 1")
            updated = original.replace(old, new, 1)
            return self._write_sync(path, updated)

    def _list_sync(self, path: Path) -> list[str]:
        entries: list[str] = []
        for child in path.iterdir():
            if child.name in _DENIED_COMPONENTS or child.name in _PROTECTED_NAMES:
                continue
            if child.is_symlink() or self._is_reparse(child):
                continue
            entries.append(child.name)
            if len(entries) > _MAX_LIST:
                raise ValueError("directory exceeds 1000-entry limit")
        return sorted(entries)

    async def read_file(self, path: str, offset: int = 0, limit: int = _MAX_LINES) -> str:
        args = ReadArgs(path=path, offset=offset, limit=limit)
        safe = self._safe_path(args.path)
        self._check_file(safe)
        return cast(str, await self._blocking(self._read_sync, safe, args.offset, args.limit))

    async def write_file(self, path: str, content: str) -> str:
        args = WriteArgs(path=path, content=content)
        safe = self._safe_path(args.path)
        return cast(str, await self._blocking(self._write_sync, safe, args.content))

    async def edit_file(self, path: str, old_str: str, new_str: str) -> str:
        args = EditArgs(path=path, old_str=old_str, new_str=new_str)
        safe = self._safe_path(args.path)
        return cast(str, await self._blocking(self._edit_sync, safe, args.old_str, args.new_str))

    async def list_dir(self, path: str = ".") -> list[str]:
        args = ListArgs(path=path)
        safe = self._safe_path(args.path, directory=True)
        return cast(list[str], await self._blocking(self._list_sync, safe))

    def _parse_command(self, command: str) -> tuple[str, list[str]]:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be non-empty")
        if any(token in command for token in ("|", ">", "<", "&", ";", "`", "$", "\n", "\r")):
            raise ValueError("shell operators are forbidden")
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            raise ValueError("invalid command quoting") from exc
        if argv == ["python", "--version"]:
            return self._python_executable, ["-I", "--version"]
        if argv == ["git", "--version"] and self._git_executable is not None:
            return self._git_executable, ["--version"]
        raise ValueError("command is not in the fixed safe allowlist")

    async def _run_command(self, args: RunArgs) -> CommandResult:
        executable, argv = self._parse_command(args.command)
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        proc = await asyncio.create_subprocess_exec(executable, *argv, cwd=str(self.root), stdin=subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        truncated = False
        async def drain(stream: asyncio.StreamReader) -> bytes:
            nonlocal truncated
            chunks: list[bytes] = []; total = 0
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    return b"".join(chunks)
                if total < _MAX_OUTPUT:
                    keep = chunk[: _MAX_OUTPUT - total]; chunks.append(keep); total += len(keep)
                    if len(keep) != len(chunk): truncated = True
                else:
                    truncated = True
        assert proc.stdout is not None and proc.stderr is not None
        out_task = asyncio.create_task(drain(proc.stdout))
        err_task = asyncio.create_task(drain(proc.stderr))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=args.timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        stdout, stderr = await asyncio.gather(out_task, err_task)
        return {"stdout": stdout.decode("utf-8", "replace"), "stderr": stderr.decode("utf-8", "replace"), "exit_code": 124 if timed_out else int(proc.returncode or 0), "timed_out": timed_out, "truncated": truncated}

    async def run_command(self, command: str, timeout_seconds: float = 30.0) -> dict[str, object]:
        args = RunArgs(command=command, timeout_seconds=timeout_seconds)
        await self._begin()
        try:
            result = await asyncio.shield(self._run_command(args))
            return dict(result)
        except asyncio.CancelledError:
            raise
        finally:
            await self._end()

    async def dispatch(self, name: str, args: str | dict[str, Any]) -> object:
        if name not in {"read_file", "write_file", "edit_file", "list_dir", "run_command"}:
            raise ValueError("unknown tool name")
        if isinstance(args, str):
            try:
                raw: Any = json.loads(args)
            except json.JSONDecodeError as exc:
                raise ValueError("arguments must be valid JSON") from exc
        elif isinstance(args, dict):
            raw = args
        else:
            raise TypeError("arguments must be JSON object or object string")
        if not isinstance(raw, dict):
            raise ValueError("arguments must be a JSON object")
        model_map: dict[str, type[_StrictModel]] = {"read_file": ReadArgs, "write_file": WriteArgs, "edit_file": EditArgs, "list_dir": ListArgs, "run_command": RunArgs}
        model = model_map[name]
        try:
            parsed = model.model_validate(raw, strict=True)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        if name == "read_file":
            read_args = cast(ReadArgs, parsed)
            return await self.read_file(read_args.path, read_args.offset, read_args.limit)
        if name == "write_file":
            write_args = cast(WriteArgs, parsed)
            return await self.write_file(write_args.path, write_args.content)
        if name == "edit_file":
            edit_args = cast(EditArgs, parsed)
            return await self.edit_file(edit_args.path, edit_args.old_str, edit_args.new_str)
        if name == "list_dir":
            return await self.list_dir(cast(ListArgs, parsed).path)
        return await self.run_command(cast(RunArgs, parsed).command, cast(RunArgs, parsed).timeout_seconds)

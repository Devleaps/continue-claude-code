"""continue-claude-code: nudges Claude after inactivity via MCP Channels."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

CONTINUE_CLAUDE_CODE_TIMEOUT = int(os.environ.get("CONTINUE_CLAUDE_CODE_TIMEOUT", "120"))

_write_stream = None
_notification_pending = False

server = Server(
    "continue-claude-code",
    instructions=(
        f"continue-claude-code sends a channel notification after {CONTINUE_CLAUDE_CODE_TIMEOUT}s of inactivity. "
        "The timer resets automatically when activity is detected."
    ),
)


_log_file = Path.home() / ".claude" / "continue-claude-code.log"


def _log(msg: str) -> None:
    line = f"[continue-claude-code] {msg}"
    print(line, file=sys.stderr, flush=True)
    with _log_file.open("a") as f:
        f.write(line + "\n")


_SESSION_MAX_AGE = 300  # only consider sessions active in the last 5 minutes


def _find_session() -> tuple[str, Path] | None:
    """Find the most recently active session matching cwd, ignoring stale ones."""
    sessions_dir = Path.home() / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return None
    cwd = os.getcwd()
    cutoff = time.time() - _SESSION_MAX_AGE
    best: tuple[str, Path, float] | None = None
    for f in sessions_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        try:
            os.kill(data["pid"], 0)
        except (ProcessLookupError, PermissionError, KeyError):
            continue
        if data.get("cwd") != cwd:
            continue
        session_id = data.get("sessionId")
        if not session_id:
            continue
        project_dir_name = cwd.replace("/", "-")
        transcript = (
            Path.home()
            / ".claude"
            / "projects"
            / project_dir_name
            / f"{session_id}.jsonl"
        )
        if transcript.exists():
            mtime = transcript.stat().st_mtime
            if mtime > cutoff and (best is None or mtime > best[2]):
                best = (session_id, transcript, mtime)
    return (best[0], best[1]) if best else None


def _get_last_activity(session_id: str, transcript: Path) -> float:
    """Most recent mtime across transcript + any active subagent transcripts."""
    mtimes = [transcript.stat().st_mtime]
    subagent_dir = transcript.parent / session_id / "subagents"
    if subagent_dir.is_dir():
        for f in subagent_dir.glob("agent-*.jsonl"):
            try:
                mtimes.append(f.stat().st_mtime)
            except OSError:
                pass
    return max(mtimes)


async def _send_channel_notification(elapsed: float) -> bool:
    content = (
        f"You've been idle for {elapsed:.0f}s.\n\n"
        "If you have pending work, this is a good time to continue."
    )
    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content},
    )
    msg = SessionMessage(message=JSONRPCMessage(root=notification))
    try:
        await _write_stream.send(msg)
        return True
    except Exception as e:
        _log(f"ERROR sending notification: {e!r}")
        return False


_RESCAN_INTERVAL = 10  # re-check for a newer session every N seconds


async def _monitor_loop() -> None:
    _log(f"monitor started, cwd={os.getcwd()}, timeout={CONTINUE_CLAUDE_CODE_TIMEOUT}s")
    session_id: str | None = None
    transcript: Path | None = None
    notification_pending = False
    last_rescan = 0.0

    while True:
        try:
            now = time.time()
            # Re-scan for a newer session periodically or when we have none
            if session_id is None or (now - last_rescan) >= _RESCAN_INTERVAL:
                fresh = _find_session()
                last_rescan = now
                if fresh is None:
                    await anyio.sleep(1)
                    continue
                fresh_id, fresh_transcript = fresh
                if fresh_id != session_id:
                    session_id, transcript = fresh_id, fresh_transcript
                    notification_pending = False
                    _log(f"watching session {session_id}")

            last_activity = _get_last_activity(session_id, transcript)
            remaining = CONTINUE_CLAUDE_CODE_TIMEOUT - (time.time() - last_activity)
            if remaining > 0:
                notification_pending = False
                await anyio.sleep(min(remaining, _RESCAN_INTERVAL))
                continue
            if not notification_pending:
                elapsed = time.time() - last_activity
                _log(f"idle for {elapsed:.0f}s — sending notification")
                if await _send_channel_notification(elapsed):
                    notification_pending = True
            await anyio.sleep(_RESCAN_INTERVAL)
        except Exception as e:
            _log(f"exception in monitor: {e}")
            await anyio.sleep(1)


async def main() -> None:
    global _write_stream
    init_options = server.create_initialization_options(
        experimental_capabilities={"claude/channel": {}},
    )
    async with stdio_server() as (read_stream, write_stream):
        _write_stream = write_stream
        async with anyio.create_task_group() as tg:
            tg.start_soon(_monitor_loop)
            await server.run(read_stream, write_stream, init_options)
            tg.cancel_scope.cancel()


if __name__ == "__main__":
    anyio.run(main)

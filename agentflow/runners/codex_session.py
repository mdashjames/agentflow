from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


_THREAD_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{36}$")


@dataclass(frozen=True, slots=True)
class CodexSessionCompletion:
    exit_code: int
    final_message: str | None = None
    terminal_event_seen_on_stdout: bool = False


class CodexSessionCompletionMonitor:
    """Recover when a completed local Codex turn stalls during CLI shutdown.

    ``codex exec --json`` normally emits ``turn.completed`` and exits. Codex also
    persists the same turn lifecycle to its rollout JSONL. In rare shutdown
    failures the persisted root turn reaches ``task_complete`` while the exec
    event stream and process remain stuck. This monitor treats that matching
    persisted event as an authoritative fallback after a grace period.
    """

    _POLL_SECONDS = 0.25
    _CLOCK_SLOP_SECONDS = 5.0
    _INITIAL_SCAN_BYTES = 1_048_576

    def __init__(self, codex_home: Path, *, stall_grace_seconds: float) -> None:
        self.codex_home = codex_home
        self.stall_grace_seconds = stall_grace_seconds
        self.started_at = time.time()
        self.thread_id: str | None = None
        self.terminal_event_seen_on_stdout = False
        self._thread_started = asyncio.Event()

    @classmethod
    def for_execution(
        cls,
        *,
        trace_kind: str,
        target_kind: str,
        command: list[str],
        env: dict[str, str],
        stall_grace_seconds: float,
    ) -> CodexSessionCompletionMonitor | None:
        codex_home = env.get("CODEX_HOME")
        if (
            trace_kind != "codex"
            or target_kind != "local"
            or not codex_home
            or "--ephemeral" in command
        ):
            return None
        return cls(Path(codex_home), stall_grace_seconds=stall_grace_seconds)

    def observe_stdout(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type")
        if event_type == "thread.started":
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str) and _THREAD_ID_PATTERN.fullmatch(thread_id):
                self.thread_id = thread_id
                self._thread_started.set()
        elif event_type in {"turn.completed", "turn.failed"}:
            self.terminal_event_seen_on_stdout = True

    async def wait(self) -> CodexSessionCompletion:
        await self._thread_started.wait()
        assert self.thread_id is not None
        session_path = await self._find_session_path(self.thread_id)
        return await self._wait_for_terminal_event(session_path)

    async def _find_session_path(self, thread_id: str) -> Path:
        sessions_root = self.codex_home / "sessions"
        pattern = f"*{thread_id}.jsonl"
        while True:
            if sessions_root.is_dir():
                matches = [path for path in sessions_root.rglob(pattern) if path.is_file()]
                if matches:
                    return max(matches, key=lambda path: path.stat().st_mtime_ns)
            await asyncio.sleep(self._POLL_SECONDS)

    def _parse_lifecycle_event(
        self,
        line: str,
        active_turn_id: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return active_turn_id, None, None
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            return active_turn_id, None, None
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return active_turn_id, None, None
        event_type = payload.get("type")
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str):
            return active_turn_id, None, None

        if event_type == "task_started":
            timestamp = record.get("timestamp")
            # A resumed session contains completed turns from earlier invocations.
            # Only arm the monitor from a task started with this process.
            if self._timestamp_is_current(timestamp):
                return turn_id, None, None
            return active_turn_id, None, None

        if turn_id != active_turn_id:
            return active_turn_id, None, None
        if event_type == "task_complete":
            final_message = payload.get("last_agent_message")
            return active_turn_id, "complete", final_message if isinstance(final_message, str) else None
        if event_type == "turn_aborted":
            return active_turn_id, "aborted", None
        return active_turn_id, None, None

    def _timestamp_is_current(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            timestamp = value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
            observed_at = datetime.fromisoformat(timestamp).timestamp()
        except (ValueError, OverflowError):
            return False
        return observed_at >= self.started_at - self._CLOCK_SLOP_SECONDS

    async def _wait_for_terminal_event(self, path: Path) -> CodexSessionCompletion:
        active_turn_id: str | None = None
        try:
            offset = max(0, path.stat().st_size - self._INITIAL_SCAN_BYTES)
        except OSError:
            offset = 0
        fragment = ""
        discard_partial_line = offset > 0
        while True:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    stream.seek(offset)
                    chunk = stream.read()
                    offset = stream.tell()
            except OSError:
                await asyncio.sleep(self._POLL_SECONDS)
                continue

            if chunk:
                complete_lines = (fragment + chunk).split("\n")
                fragment = complete_lines.pop()
                if discard_partial_line and complete_lines:
                    complete_lines.pop(0)
                    discard_partial_line = False
                for line in complete_lines:
                    active_turn_id, terminal, final_message = self._parse_lifecycle_event(
                        line, active_turn_id
                    )
                    if terminal is not None:
                        await asyncio.sleep(self.stall_grace_seconds)
                        return CodexSessionCompletion(
                            exit_code=0 if terminal == "complete" else 1,
                            final_message=final_message,
                            terminal_event_seen_on_stdout=self.terminal_event_seen_on_stdout,
                        )
            await asyncio.sleep(self._POLL_SECONDS)

    @staticmethod
    def recovered_stdout_events(completion: CodexSessionCompletion) -> list[str]:
        if completion.terminal_event_seen_on_stdout:
            return []
        events: list[dict[str, Any]] = []
        if completion.exit_code == 0 and completion.final_message:
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "agentflow_recovered_final",
                        "type": "agent_message",
                        "text": completion.final_message,
                    },
                    "recovered_from": "codex_session",
                }
            )
        events.append(
            {
                "type": "turn.completed" if completion.exit_code == 0 else "turn.failed",
                "recovered_from": "codex_session",
            }
        )
        return [json.dumps(event, ensure_ascii=False) for event in events]

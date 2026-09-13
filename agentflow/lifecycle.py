"""Persist terminal scheduler and preparation failures for reliable cleanup."""
from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
import traceback

from agentflow.orchestrator import Orchestrator
from agentflow.specs import NodeStatus, RunStatus

ORCHESTRATOR_EXCEPTION_EXIT_CODE = 70
_TERMINAL_NODE_STATUSES = frozenset({NodeStatus.COMPLETED, NodeStatus.FAILED, NodeStatus.CANCELLED, NodeStatus.SKIPPED})


class ResilientOrchestrator(Orchestrator):
    """Turn node-task exceptions into persisted AgentFlow failures.

    AgentFlow's scheduler awaits node tasks without a task-level exception
    boundary.  A preparation or runner exception can therefore leave the run
    marked ``running`` forever, which also prevents host cleanup from closing
    the invocation.  Convert those
    exceptions into terminal node and run states used for ordinary failures.
    """

    async def run(self, run_id: str) -> Any:
        """Persist a terminal failure if the scheduler itself raises.

        ``_execute_node`` handles preparation and runner failures.  A separate
        failure in dependency scheduling or result collection still escapes the
        upstream scheduler, whose background thread would otherwise disappear
        while ``wait()`` polls a permanently running run.
        """

        try:
            return await super().run(run_id)
        except Exception as error:  # noqa: BLE001 - close the host-owned run on any scheduler error.
            record = self.store.get_run(run_id)
            finished_at = datetime.now(timezone.utc).isoformat()
            detail = f"{type(error).__name__}: {str(error) or '<empty message>'}"
            failed_node_id: str | None = None
            for node_id, result in record.nodes.items():
                if result.status not in _TERMINAL_NODE_STATUSES:

                    failed_node_id = failed_node_id or node_id
                    result.status = NodeStatus.FAILED
                    result.started_at = result.started_at or finished_at
                    result.finished_at = finished_at
                    result.exit_code = ORCHESTRATOR_EXCEPTION_EXIT_CODE
                    result.final_response = None
                    result.output = ""
                    result.stdout_lines = []
                    result.stderr_lines = [detail]
                    result.success = False
                    result.success_details = [detail]
                    if result.attempts:
                        attempt = result.attempts[-1]
                        attempt.status = NodeStatus.FAILED
                        attempt.finished_at = finished_at
                        attempt.exit_code = ORCHESTRATOR_EXCEPTION_EXIT_CODE
                        attempt.final_response = None
                        attempt.output = ""
                        attempt.success = False
                        attempt.success_details = [detail]

            record.status = RunStatus.FAILED
            record.finished_at = finished_at
            artifact_node_id = failed_node_id or next(iter(record.nodes), "__run__")
            with suppress(Exception):
                await self.store.write_artifact_json(
                    run_id,
                    artifact_node_id,
                    "orchestrator-exception.json",
                    {
                        "schema_version": 1,
                        "authority": "host",
                        "run_id": run_id,
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                        "traceback": traceback.format_exc(),
                        "failed_node_id": failed_node_id,
                    },
                )
            with suppress(Exception):
                await self._publish(
                    run_id,
                    "run_failed",
                    error_type=type(error).__name__,
                    error_message=str(error),
                    failed_node_id=failed_node_id,
                )
                await self._publish(run_id, "run_completed", status=RunStatus.FAILED.value)
            await self.store.persist_run(run_id)
            return record

    async def _execute_node(
        self,
        run_id: str,
        node_id: str,
        *,
        periodic_tick_number: int | None = None,
        periodic_tick_started_at: str | None = None,
    ) -> Any:
        try:
            return await super()._execute_node(
                run_id,
                node_id,
                periodic_tick_number=periodic_tick_number,
                periodic_tick_started_at=periodic_tick_started_at,
            )
        except Exception as error:  # noqa: BLE001 - close the host-owned run on any node error.
            record = self.store.get_run(run_id)
            result = record.nodes[node_id]
            finished_at = datetime.now(timezone.utc).isoformat()
            detail = f"{type(error).__name__}: {str(error) or '<empty message>'}"
            result.status = NodeStatus.FAILED
            result.finished_at = finished_at
            result.exit_code = ORCHESTRATOR_EXCEPTION_EXIT_CODE
            result.final_response = None
            result.output = ""
            result.stdout_lines = []
            result.stderr_lines = [detail]
            result.success = False
            result.success_details = [detail]
            if result.attempts:
                attempt = result.attempts[-1]
                attempt.status = NodeStatus.FAILED
                attempt.finished_at = finished_at
                attempt.exit_code = ORCHESTRATOR_EXCEPTION_EXIT_CODE
                attempt.final_response = None
                attempt.output = ""
                attempt.success = False
                attempt.success_details = [detail]
            with suppress(Exception):
                await self.store.append_artifact_text(
                    run_id,
                    node_id,
                    "stderr.log",
                    detail + "\n",
                )
                await self.store.write_artifact_json(
                    run_id,
                    node_id,
                    "exception.json",
                    {
                        "schema_version": 1,
                        "authority": "host",
                        "run_id": run_id,
                        "node_id": node_id,
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                        "traceback": traceback.format_exc(),
                    },
                )
                await self.store.write_artifact_json(
                    run_id,
                    node_id,
                    "result.json",
                    result.model_dump(mode="json"),
                )
            with suppress(Exception):
                await self._publish(
                    run_id,
                    "node_failed",
                    node_id=node_id,
                    attempt=result.current_attempt,
                    exit_code=ORCHESTRATOR_EXCEPTION_EXIT_CODE,
                    success=False,
                    output="",
                    final_response=None,
                    success_details=[detail],
                )
            await self.store.persist_run(run_id)
            return SimpleNamespace(
                node_id=node_id,
                periodic_tick_number=periodic_tick_number,
                periodic_actions=None,
                periodic_action_parse_error=None,
            )

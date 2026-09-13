from pathlib import Path

import pytest

from agentflow.agents.base import AgentAdapter
from agentflow.agents.registry import AdapterRegistry
from agentflow.lifecycle import ResilientOrchestrator
from agentflow.specs import PipelineSpec
from agentflow.store import RunStore


class PreparationFailure(AgentAdapter):
    def prepare(self, node, prompt, paths):
        raise RuntimeError("intentional preparation failure")


@pytest.mark.asyncio
async def test_preparation_failure_becomes_terminal_and_persisted(tmp_path: Path):
    registry = AdapterRegistry()
    registry.register("codex", PreparationFailure())
    store = RunStore(tmp_path / "runs")
    orchestrator = ResilientOrchestrator(store=store, adapters=registry)
    pipeline = PipelineSpec(name="failure", working_dir=str(tmp_path), nodes=[{"id": "test", "agent": "codex", "prompt": "test", "target": {"kind": "docker"}}])
    run = await orchestrator.submit(pipeline)
    result = await orchestrator.wait(run.id, timeout=5)
    assert result.status == "failed"
    assert result.nodes["test"].exit_code == 70
    assert result.nodes["test"].finished_at
    assert not result.nodes["test"].success
    assert list((tmp_path / "runs").rglob("exception.json"))

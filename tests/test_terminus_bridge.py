"""Contract tests against pinned Harbor; no model controller is instantiated."""
import asyncio
from importlib.metadata import version
import inspect
import os
from pathlib import Path

import pytest

from agentflow.agents import terminus_controller as bridge


def test_bridge_rejects_non_container_before_harbor_import(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(RuntimeError, match="inside an agent container"):
        bridge.require_container()


@pytest.mark.asyncio
async def test_bridge_matches_pinned_harbor_and_executes_only_local_test_commands(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    if version("harbor") != "0.23.0":
        pytest.skip("bridge contract test targets pinned Harbor 0.23.0")
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    assert list(inspect.signature(Terminus2.run).parameters) == ["self", "instruction", "environment", "context"]
    # The only process in this test is a deterministic shell command below.
    # No controller, model, agent CLI, Docker lifecycle or network is invoked.
    monkeypatch.setattr(bridge, "require_container", lambda: None)
    environment = bridge.environment_class()(tmp_path, tmp_path / "logs")
    assert not type(environment).__abstractmethods__
    result = await environment.exec("printf bridge-ok", timeout_sec=2)
    assert result.return_code == 0
    assert result.stdout == "bridge-ok"
    with pytest.raises(RuntimeError, match="calling host"):
        await environment.start()
    with pytest.raises(RuntimeError, match="calling host"):
        await environment.stop()
    with pytest.raises(asyncio.TimeoutError):
        await environment.exec("sleep 10", timeout_sec=0.01)
    different_uid = 1 if os.geteuid() == 0 else 0
    denied = await environment.exec("true", user=different_uid)
    assert denied.return_code == 126

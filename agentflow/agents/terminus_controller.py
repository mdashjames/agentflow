"""In-container Harbor bridge. No Docker API, deployment, or grading authority.

This file is copied to the execution runtime so importing AgentFlow on the host
does not import Harbor or instantiate a model controller. Keep it self contained.
"""
from __future__ import annotations

import asyncio
from importlib.metadata import version
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import sys


def require_container() -> None:
    if not (Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()):
        raise RuntimeError("Terminus controller must execute inside an agent container")


def environment_class():
    # Lazy imports keep even inspection and adapter preparation model-free.
    from harbor.environments.base import BaseEnvironment, ExecResult
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths

    class InContainerEnvironment(BaseEnvironment):
        def __init__(self, workdir: Path, logs_dir: Path):
            require_container()
            self.workdir = workdir.resolve(strict=True)
            logs_dir.mkdir(parents=True, exist_ok=True)
            super().__init__(
                environment_dir=self.workdir, environment_name="agentflow-container",
                session_id=logs_dir.name, trial_paths=TrialPaths(trial_dir=logs_dir),
                task_env_config=EnvironmentConfig(),
            )

        @staticmethod
        def type() -> str:
            return "agentflow-in-container"

        def _validate_definition(self):
            require_container()

        async def start(self, force_build: bool = False) -> None:
            raise RuntimeError("container lifecycle is owned by the calling host")

        async def stop(self, delete: bool = False) -> None:
            raise RuntimeError("container lifecycle is owned by the calling host")

        async def exec(self, command: str, cwd: str | None = None,
                       env: dict[str, str] | None = None, timeout_sec: int | None = None,
                       user: str | int | None = None) -> ExecResult:
            # Harbor sometimes requests root for tool discovery. Honor it only
            # when the host already selected that uid; never change privileges.
            if user is not None:
                uid = int(user) if str(user).isdigit() else pwd.getpwnam(str(user)).pw_uid
                if uid != os.geteuid():
                    return ExecResult(return_code=126, stderr="user switching is forbidden")
            process = await asyncio.create_subprocess_exec(
                "bash", "-c", command, cwd=cwd or self.workdir,
                env={**os.environ, **(env or {})},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.communicate()
                raise
            return ExecResult(return_code=process.returncode,
                              stdout=stdout.decode(errors="replace"), stderr=stderr.decode(errors="replace"))

        @staticmethod
        def _copy_file(source, destination):
            source, destination = Path(source), Path(destination)
            if source.resolve() == destination.resolve():
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        async def upload_file(self, source_path, target_path):
            self._copy_file(source_path, target_path)

        async def download_file(self, source_path, target_path):
            self._copy_file(source_path, target_path)

        async def upload_dir(self, source_dir, target_dir):
            if Path(source_dir).resolve() != Path(target_dir).resolve():
                shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)

        async def download_dir(self, source_dir, target_dir):
            await self.upload_dir(source_dir, target_dir)

    return InContainerEnvironment


async def run(config: dict) -> None:
    require_container()
    installed = version("harbor")
    if installed != config["harbor_version"]:
        raise RuntimeError(f"Harbor revision mismatch: expected {config['harbor_version']}, installed {installed}")
    if shutil.which("tmux") is None:
        raise RuntimeError("Terminus image requires preinstalled tmux")
    from harbor.agents.terminus_2.terminus_2 import Terminus2
    from harbor.agents.terminus_2.tmux_session import TmuxSession
    from harbor.models.agent.context import AgentContext
    from harbor.models.trial.paths import EnvironmentPaths

    logs_dir = Path(config["logs_dir"])
    logs_dir.mkdir(parents=True, exist_ok=True)
    # Harbor normally writes to /logs/agent. Redirect its agent-only paths to
    # this invocation's existing runtime mount before setup creates its tmux log.
    EnvironmentPaths.agent_dir = logs_dir
    environment = environment_class()(Path(config["workdir"]), logs_dir)
    options = dict(config["options"])
    if config.get("api_key_env"):
        key = os.environ.get(config["api_key_env"])
        if not key:
            raise RuntimeError("missing injected provider credential")
        # Harbor persists constructor options into its native trajectory. Keep
        # the credential in process memory only; LiteLLM reads its standard env.
        os.environ[config["credential_env"]] = key
    class PreinstalledTmuxSession(TmuxSession):
        async def _attempt_tmux_installation(self) -> None:
            # Harbor's default installer requests root. The image contract
            # already requires tmux; enforce it without any package installation
            # or user-switch attempt in the invocation.
            if shutil.which("tmux") is None:
                raise RuntimeError("Terminus image requires preinstalled tmux")

    class ContainerTerminus2(Terminus2):
        async def setup(self, environment) -> None:
            self._session = PreinstalledTmuxSession(
                session_name=self.name(), environment=environment,
                logging_path=logs_dir / "terminus_2.pane",
                pane_width=self.options.tmux_pane_width,
                pane_height=self.options.tmux_pane_height,
                extra_env=self._extra_env, user=environment.default_user,
            )
            await self._session.start()

    controller = ContainerTerminus2(logs_dir=logs_dir, model_name=config["model"], **options)
    context = AgentContext()
    print(json.dumps({"type": "agentflow.terminus.started", "implementation": config["implementation"], "version": installed}), flush=True)
    try:
        await controller.setup(environment)
        await controller.run(config["instruction"], environment, context)
    finally:
        session = getattr(controller, "_session", None)
        if session is not None:
            await session.stop()
        (logs_dir / "context.json").write_text(context.model_dump_json(indent=2))
    print(json.dumps({"type": "agentflow.terminus.finished", "trajectory": str(logs_dir / "trajectory.json"), "n_input_tokens": context.n_input_tokens, "n_output_tokens": context.n_output_tokens}), flush=True)


if __name__ == "__main__":
    asyncio.run(run(json.loads(Path(sys.argv[1]).read_text())))

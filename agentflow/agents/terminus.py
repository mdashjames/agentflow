"""Harbor Terminus 2 controller, prepared for execution inside a caller's container."""
from __future__ import annotations

import json
from pathlib import Path

from agentflow.agents.base import AgentAdapter
from agentflow.agents.secrets import wrap_secret_files
from agentflow.env import merge_env_layers
from agentflow.prepared import ExecutionPaths, PreparedExecution
from agentflow.specs import NodeSpec, ToolAccess

HARBOR_VERSION = "0.23.0"
HARBOR_WHEEL_SHA256 = "8747400dbb2a5e2298e1338e17e88eba38433c0433fd700f34d1a9021bba5c37"


class TerminusAdapter(AgentAdapter):
    def prepare(self, node: NodeSpec, prompt: str, paths: ExecutionPaths) -> PreparedExecution:
        self.validate_node_features(node)
        if node.target.kind != "docker":
            raise ValueError("Harbor Terminus 2 controller requires target.kind=docker")
        if node.target.inherit_credentials:
            raise ValueError("Terminus requires explicit secret files, not host credentials")
        if node.mcps or node.extensions:
            raise ValueError("Terminus 2 does not support MCP/extension integrations through this adapter")
        if node.tools == ToolAccess.READ_ONLY:
            raise ValueError("Terminus 2 terminal tool does not enforce read_only tools")
        if not node.model:
            raise ValueError("Terminus 2 requires an explicit model")
        if node.extra_args:
            raise ValueError("Terminus 2 uses typed model settings, not CLI extra_args")
        provider = self.provider_config(node.provider, node.agent)
        settings = node.model_settings
        options: dict[str, object] = {"record_terminal_session": False}
        for name in ("temperature", "reasoning_effort", "max_turns"):
            value = getattr(settings, name)
            if value is not None:
                options[name] = value
        if settings.context_window or settings.max_output_tokens:
            options["model_info"] = {name: value for name, value in {
                "max_input_tokens": settings.context_window,
                "max_output_tokens": settings.max_output_tokens,
            }.items() if value is not None}
        if settings.max_output_tokens:
            options["llm_call_kwargs"] = {"max_tokens": settings.max_output_tokens}
        if provider:
            if provider.base_url:
                options["api_base"] = provider.base_url
            if provider.wire_api == "responses":
                options["use_responses_api"] = True
            if provider.headers:
                options.setdefault("llm_call_kwargs", {})["extra_headers"] = provider.headers
        root = Path(paths.target_runtime_dir)
        model = node.model
        if provider and provider.base_url and "/" not in model:
            model = ("anthropic/" if provider.wire_api in {"anthropic", "messages"} else "openai/") + model
        config = {
            "schema_version": 1, "implementation": "harbor.terminus-2",
            "harbor_version": HARBOR_VERSION, "model": model, "requested_model": node.model,
            "api_key_env": provider.api_key_env if provider else None,
            "credential_env": "ANTHROPIC_API_KEY" if provider and provider.wire_api in {"anthropic", "messages"} else "OPENAI_API_KEY",
            "instruction": prompt, "workdir": paths.target_workdir,
            "logs_dir": str(root / "terminus-trajectory"), "options": options,
        }
        files = {
            "terminus-config.json": json.dumps(config, sort_keys=True, indent=2),
            "terminus_controller.py": Path(__file__).with_name("terminus_controller.py").read_text(),
        }
        prepared = PreparedExecution(
            command=[node.executable or "python3", str(root / "terminus_controller.py"), str(root / "terminus-config.json")],
            env=merge_env_layers(getattr(provider, "env", None), node.env),
            cwd=paths.target_workdir, trace_kind="terminus", runtime_files=files,
        )
        return wrap_secret_files(node, prepared, paths)

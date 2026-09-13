"""Typed role behavior bound to AgentFlow's ordinary graph nodes."""
from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentflow.dsl import Graph, NodeBuilder, agent
from agentflow.profiles import AgentProfile

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class ArtifactContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    path: str
    required: bool = True
    media_type: str | None = None

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not value or value == ".":
            raise ValueError("artifact paths must stay inside the output workspace")
        return value


class ActorRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    capabilities: frozenset[str] = frozenset()


class InstructionBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    resources: dict[str, str] = Field(default_factory=dict)

    @field_validator("resources")
    @classmethod
    def validate_resources(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            ArtifactContract(name=name, path=name)
            if name in {"instructions.md", "bundle.json"}:
                raise ValueError(f"reserved instruction bundle resource: {name}")
        return value

    @property
    def identity(self) -> str:
        encoded = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def materialize(self, directory: Path) -> str:
        """Create a new immutable resource tree; host code owns its mount policy."""
        directory.mkdir(parents=True, exist_ok=False)
        payload = {"instructions.md": self.text, **self.resources}
        payload["bundle.json"] = json.dumps({"schema_version": 1, "sha256": self.identity}, sort_keys=True)
        for relative, content in payload.items():
            destination = directory / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            destination.chmod(0o444)
        for item in sorted(directory.rglob("*"), reverse=True):
            if item.is_dir():
                item.chmod(0o555)
        directory.chmod(0o555)
        return self.identity


class ActorNode(ABC, Generic[InputT, OutputT]):
    actor_id: str
    actor_version: int = 1
    input_type: type[InputT]
    output_type: type[OutputT]

    @abstractmethod
    def instructions(self, inputs: InputT) -> InstructionBundle:
        raise NotImplementedError

    def requirements(self) -> ActorRequirements:
        return ActorRequirements()

    def output_artifacts(self, inputs: InputT) -> list[ArtifactContract]:
        return []

    def binding_metadata(self, inputs: InputT | dict[str, Any]) -> dict[str, Any]:
        typed = self.input_type.model_validate(inputs)
        bundle = self.instructions(typed)
        return {
            "schema_version": 1, "id": self.actor_id, "version": self.actor_version,
            "inputs": typed.model_dump(mode="json"),
            "input_schema": self.input_type.model_json_schema(),
            "output_schema": self.output_type.model_json_schema(),
            "artifacts": [a.model_dump(mode="json") for a in self.output_artifacts(typed)],
            "requirements": self.requirements().model_dump(mode="json"),
            "instruction_sha256": bundle.identity,
        }

    def bind(self, *, node_id: str, inputs: InputT | dict[str, Any], profile: AgentProfile,
             target: Any, graph: Graph | None = None, **node_options: Any) -> NodeBuilder:
        typed = self.input_type.model_validate(inputs)
        target_kind = target.get("kind", "local") if isinstance(target, dict) else target.kind
        if target_kind == "docker" and (target.get("inherit_credentials", False) if isinstance(target, dict) else target.inherit_credentials):
            raise ValueError("actor Docker execution forbids inherited host credentials")
        options = profile.node_options(target_kind=target_kind, required_capabilities=self.requirements().capabilities)
        overlap = set(options) & set(node_options)
        if overlap:
            raise ValueError(f"actor binding cannot override resolved profile fields: {sorted(overlap)}")
        options.update(node_options)
        options.update(target=target, actor=self.binding_metadata(typed))
        prompt = self.instructions(typed).text
        if graph is not None:
            return NodeBuilder(dag=graph, id=node_id, agent=profile.agent, prompt=prompt, kwargs=options)
        return agent(profile.agent, task_id=node_id, prompt=prompt, **options)

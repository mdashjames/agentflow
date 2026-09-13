"""AgentFlow public package surface."""

from agentflow.actors import ActorNode, ActorRequirements, ArtifactContract, InstructionBundle
from agentflow.profiles import AgentProfile, BackendCapabilities, register_backend_capabilities

from agentflow.dsl import (
    DAG,
    Graph,
    InferenceSetup,
    agent,
    claude,
    codex,
    evolve,
    fanout,
    kimi,
    merge,
    pi,
    python_node,
    shell,
    sync,
)


def create_app(*args, **kwargs):
    from agentflow.app import create_app as _create_app

    return _create_app(*args, **kwargs)


__all__ = [
    "ActorNode", "ActorRequirements", "ArtifactContract", "InstructionBundle",
    "AgentProfile", "BackendCapabilities", "register_backend_capabilities",
    "DAG",
    "Graph",
    "InferenceSetup",
    "agent",
    "claude",
    "codex",
    "evolve",
    "fanout",
    "kimi",
    "merge",
    "pi",
    "python_node",
    "shell",
    "sync",
    "create_app",
]

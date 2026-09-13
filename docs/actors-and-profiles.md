# Actors and agent profiles

`ActorNode` supplies typed role behavior and binds to the existing `NodeBuilder` /
`NodeSpec` graph model. It does not introduce a second scheduler. Subclasses declare
`actor_id`, `actor_version`, `input_type`, `output_type`, `instructions(inputs)`,
`requirements()`, and `output_artifacts(inputs)`. Artifact contracts describe
expected outputs; applications retain authority over validation and evaluation.

```python
from pydantic import BaseModel
from agentflow import ActorNode, AgentProfile, Graph, InstructionBundle

class DraftInput(BaseModel):
    topic: str

class DraftOutput(BaseModel):
    path: str

class Writer(ActorNode[DraftInput, DraftOutput]):
    actor_id = "example.writer"
    input_type = DraftInput
    output_type = DraftOutput

    def instructions(self, inputs):
        return InstructionBundle(text=f"Write an article about {inputs.topic}.")

profile = AgentProfile(name="default", agent="codex", model="example-model")
with Graph("writing"):
    draft = Writer().bind(
        node_id="draft", inputs={"topic": "compilers"}, profile=profile,
        target={"kind": "docker", "image": "your-pinned-agent-image"},
    )
```

`InstructionBundle.materialize(new_directory)` writes an immutable bundle and
returns its SHA-256 identity. The application validates and mounts this directory.
Every bound node records typed inputs and schemas, output contracts, actor version,
capabilities, bundle digest, and selected profile digest. Profiles cannot be
overridden by extra binding options; resolve a new explicit profile when changing
backend settings. Instruction rendering should be pure and deterministic.

`AgentProfile.node_options(target_kind="docker", required_capabilities=...)`
validates requested features before producing `NodeSpec` fields. Call it during
preflight, before provisioning resources. Profiles keep model/provider settings,
MCP declarations, skills, extensions, secret references, and timeouts independent
of workflow configuration. `BACKEND_CAPABILITIES` covers Codex, Claude, Pi, and
Harbor Terminus 2. Pi and Terminus reject MCP declarations; Terminus also rejects
read-only tool access and CLI arguments. Pi token limits require an explicit
custom provider and model. Unsupported settings and protocols raise errors.

Trusted backend extensions can register their adapter in `AdapterRegistry` and
capabilities using `register_backend_capabilities`. Custom names survive graph and
trace serialization and resolve through the supplied adapter registry.

Pi extensions are copied from explicitly approved host files into content-addressed
runtime files and passed to Pi by their container paths. Declare a self-contained
JavaScript/TypeScript file, or a complete directory with `index.ts`, `index.js`,
or `package.json` `pi.extensions` entries. Directory resources retain their relative
paths. Symlinks, special files, non-UTF-8 resources, missing/escaping entries, and
bundles exceeding 256 files or 8 MiB are rejected. Dependencies must be included
in the directory or provided by the declared agent image; preparation never
installs packages. `extensions/manifest.json` records source paths, target paths,
and file/content digests. The application still authorizes host source paths.

## Secret files and MCP configuration

Use `SecretRef(path="/run/secrets/provider")` under `secret_env`. The caller owns
secret acquisition, short-lived read-only file injection, mount validation, and
cleanup. AgentFlow prepares a launcher containing only file references; the
launcher reads secret bytes inside the container immediately before execution.
Preparation never reads Docker-node credentials from host environment or home
directories. `inherit_credentials=True` is rejected for these Docker adapters.

Generic MCP declarations support `stdio` and `streamable_http` transports,
`secret_env`, `env_http_headers`, `bearer_token_env_var`, `startup_timeout_sec`, and
`tool_timeout_sec`. Codex supports both timeout fields; other backends explicitly
reject those overrides when they cannot preserve their meaning. Claude emits
environment references in its MCP JSON. No individual service declarations or
egress policies belong in these APIs. A declaration does not grant network access.

Codex settings are rendered directly into its runtime home, including the current
`agentflow.config.toml` profile file. There is no legacy `[profiles.agentflow]`
section. `web_search="disabled"` suppresses built-in web tools where available;
explicit MCP declarations remain separately controlled capabilities.

## Harbor Terminus 2

`terminus` runs Harbor **0.23.0**, with Python **3.12+** and preinstalled `tmux` in
the agent image. Install the optional `terminus` extra in that image. The package
pin and upstream wheel checksum are recorded in `harbor.lock.json`.

The adapter copies a standalone controller into the invocation runtime. The
controller imports `Terminus2` and bridges Harbor's `BaseEnvironment` to local
subprocesses and files inside that same container. It rejects execution without
container markers and checks the installed Harbor version. It does not use
Harbor's job runner, provision Docker, switch users, or run verification. Host
code owns those operations. Model credentials are supplied through process
environment, never Harbor constructor options, because Harbor serializes those
options into native trajectories.

Custom-provider bare model identifiers receive the LiteLLM protocol prefix;
`requested_model` remains recorded separately. Native trajectory and context
files are written under `terminus-trajectory/` in the mounted runtime. Their
existence says nothing about the application's task outcome.

Sources: [Harbor Terminus 2](https://www.harborframework.com/docs/agents/terminus-2),
[Harbor 0.23.0](https://pypi.org/project/harbor/0.23.0/).

## Failure lifecycle

`agentflow.lifecycle.ResilientOrchestrator` extends the normal orchestrator to
persist preparation, runner, and scheduler exceptions as terminal failures,
including exception artifacts and exit code 70. Applications can then finish
their own inspection and cleanup. It does not treat node output or exit status as
an application reward.

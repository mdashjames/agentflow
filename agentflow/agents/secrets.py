"""Build secret-reference launchers without accessing caller credentials."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from agentflow.prepared import ExecutionPaths, PreparedExecution
from agentflow.specs import NodeSpec


def wrap_secret_files(node: NodeSpec, prepared: PreparedExecution, paths: ExecutionPaths,
                      *, aliases: dict[str, str] | None = None) -> PreparedExecution:
    refs = dict(node.secret_env)
    for mcp in node.mcps:
        for name, reference in mcp.secret_env.items():
            if name in refs and refs[name] != reference:
                raise ValueError(f"conflicting secret references for {name}")
            refs[name] = reference
    if not refs and not aliases:
        return prepared
    if node.target.kind not in {"docker", "container", "cloud_hypervisor"}:
        raise ValueError("secret file profiles require isolated execution targets")
    for name in refs:
        if not name.isidentifier() or not name.isascii():
            raise ValueError("secret environment names must be ASCII identifiers")
        if name in prepared.env:
            raise ValueError(f"secret reference conflicts with literal environment: {name}")
    payload = {"files": {name: ref.path for name, ref in refs.items()}, "aliases": aliases or {}}
    script = '''import json, os, pathlib, sys
if not (pathlib.Path('/.dockerenv').exists() or pathlib.Path('/run/.containerenv').exists()):
    raise SystemExit('secret launcher requires an isolated container')
spec = json.loads(pathlib.Path(sys.argv[1]).read_text())
env = dict(os.environ)
for name, path in spec['files'].items():
    value = pathlib.Path(path).read_text().strip()
    if not value or '\\x00' in value or '\\n' in value:
        raise SystemExit('invalid injected secret file')
    env[name] = value
for destination, source in spec['aliases'].items():
    if source in env:
        env[destination] = env[source]
os.execvpe(sys.argv[2], sys.argv[2:], env)
'''
    files = dict(prepared.runtime_files)
    files["secret-launch.py"] = script
    files["secret-references.json"] = json.dumps(payload, sort_keys=True)
    root = Path(paths.target_runtime_dir)
    return replace(prepared, runtime_files=files, command=["python3", str(root / "secret-launch.py"), str(root / "secret-references.json"), *prepared.command])

import json
from pathlib import Path

import pytest

from agentflow.agents.pi import PiAdapter
from agentflow.extensions import prepare_extensions
from agentflow.prepared import ExecutionPaths
from agentflow.specs import NodeSpec


def test_pi_extension_arg_uses_materialized_runtime_file(tmp_path):
    extension = tmp_path / "approved.ts"
    extension.write_text("export default function extension(pi) {}\n")
    paths = ExecutionPaths(tmp_path, tmp_path / "runtime", "/workspace", "/agentflow-runtime", tmp_path)
    node = NodeSpec(id="test", agent="pi", prompt="hello", target={"kind": "docker"}, extensions=[str(extension)], repo_instructions_mode="ignore")
    prepared = PiAdapter().prepare(node, node.prompt, paths)
    target = prepared.command[prepared.command.index("--extension") + 1]
    relative = target.removeprefix("/agentflow-runtime/")
    assert target.startswith("/agentflow-runtime/extensions/")
    assert str(extension) not in prepared.command
    assert prepared.runtime_files[relative] == extension.read_text()
    manifest = json.loads(prepared.runtime_files["extensions/manifest.json"])
    assert manifest["extensions"][0]["target"] == target


def test_extension_tree_preserves_relative_imports_and_pins_bytes(tmp_path):
    package = tmp_path / "extension"
    package.mkdir()
    (package / "index.ts").write_text('import { value } from "./value.ts"; export default () => value;')
    (package / "value.ts").write_text("export const value = 1;")
    targets, files = prepare_extensions([str(package)], source_root=tmp_path, target_runtime_dir="/runtime")
    root = targets[0].removeprefix("/runtime/")
    assert files[root + "/value.ts"] == "export const value = 1;"
    (package / "value.ts").write_text("export const value = 2;")
    changed, _ = prepare_extensions([str(package)], source_root=tmp_path, target_runtime_dir="/runtime")
    assert changed != targets


def test_extension_local_import_requires_declared_tree(tmp_path):
    (tmp_path / "single.ts").write_text('import "./dependency.ts";')
    with pytest.raises(ValueError, match="self-contained directory"):
        prepare_extensions(["single.ts"], source_root=tmp_path, target_runtime_dir="/runtime")


def test_extension_rejects_symlinks_and_escaping_manifest(tmp_path):
    package = tmp_path / "extension"
    package.mkdir()
    (package / "index.ts").write_text("export default () => {};")
    (package / "secret").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="unsupported extension resource"):
        prepare_extensions([str(package)], source_root=tmp_path, target_runtime_dir="/runtime")
    (package / "secret").unlink()
    (package / "package.json").write_text(json.dumps({"pi": {"extensions": ["../../escape.ts"]}}))
    with pytest.raises(ValueError, match="escapes"):
        prepare_extensions([str(package)], source_root=tmp_path, target_runtime_dir="/runtime")


def test_extension_resource_limits(tmp_path):
    (tmp_path / "single.ts").write_text("export default () => {};")
    with pytest.raises(ValueError, match="byte limit"):
        prepare_extensions(["single.ts"], source_root=tmp_path, target_runtime_dir="/runtime", max_bytes=2)
    with pytest.raises(ValueError, match="file-count limit"):
        prepare_extensions(["single.ts"], source_root=tmp_path, target_runtime_dir="/runtime", max_files=0)

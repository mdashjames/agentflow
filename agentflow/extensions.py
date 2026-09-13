"""Materialize explicitly declared extension resources without host execution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

_RELATIVE_MODULE = re.compile(r'''(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)["']\.\.?/''')


def _checked_path(path: Path) -> Path:
    """Reject every symlink component, even one remaining within the source tree."""
    absolute = Path(os.path.abspath(path))
    for current in (*reversed(absolute.parents), absolute):
        if current.is_symlink():
            raise ValueError(f"extension resources must not contain symlinks: {current}")
    return absolute


def prepare_extensions(
    extensions: list[str], *, source_root: Path, target_runtime_dir: str,
    max_files: int = 256, max_bytes: int = 8 * 1024 * 1024,
) -> tuple[list[str], dict[str, str]]:
    """Return container paths and UTF-8 runtime files for approved extensions.

    A file must be self-contained; select its directory to include local imports.
    Directory packages require index.ts/index.js or explicit pi.extensions in
    package.json. Dependencies must already be included or available in the
    selected image. This function never installs packages or imports code.
    The caller, not this helper, authorizes host source paths.
    """
    files: dict[str, str] = {}
    targets: list[str] = []
    manifest: list[dict] = []
    count = size = 0
    for item in extensions:
        raw = Path(item)
        source = _checked_path(raw if raw.is_absolute() else source_root / raw)
        source_stat = source.stat()
        directory = stat.S_ISDIR(source_stat.st_mode)
        if directory:
            candidates = []
            for current, dirs, names in os.walk(source, followlinks=False):
                for name in [*dirs, *names]:
                    path = Path(current) / name
                    mode = path.lstat().st_mode
                    if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                        raise ValueError(f"unsupported extension resource: {path}")
                candidates.extend(Path(current) / name for name in names)
                if len(candidates) + count > max_files:
                    raise ValueError("extension resources exceed the file-count limit")
        elif stat.S_ISREG(source_stat.st_mode):
            candidates = [source]
        else:
            raise ValueError(f"extension must be a regular file or directory: {source}")
        contents: dict[str, str] = {}
        hashes: dict[str, str] = {}
        for path in sorted(candidates):
            count += 1
            if count > max_files:
                raise ValueError("extension resources exceed the file-count limit")
            declared_size = path.stat().st_size
            if size + declared_size > max_bytes:
                raise ValueError("extension resources exceed the byte limit")
            with path.open("rb") as handle:
                payload = handle.read(max_bytes - size + 1)
            size += len(payload)
            if size > max_bytes:
                raise ValueError("extension resources exceed the byte limit")
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"extension resource must be UTF-8 text: {path}") from exc
            relative = path.relative_to(source).as_posix() if directory else source.name
            contents[relative] = text
            hashes[relative] = hashlib.sha256(payload).hexdigest()
        if directory:
            entries = []
            if "package.json" in contents:
                package = json.loads(contents["package.json"])
                if isinstance(package, dict) and isinstance(package.get("pi"), dict):
                    entries = package["pi"].get("extensions", [])
            if not entries:
                entries = [name for name in ("index.ts", "index.js") if name in contents][:1]
            if not isinstance(entries, list) or not entries:
                raise ValueError("extension directory requires index.ts/index.js or pi.extensions")
            for entry in entries:
                if not isinstance(entry, str):
                    raise ValueError("extension entry paths must be strings")
                relative = PurePosixPath(entry)
                if relative.is_absolute() or ".." in relative.parts or relative.as_posix() not in contents:
                    raise ValueError(f"extension entry escapes the bundle or is missing: {entry}")
        else:
            if source.suffix not in {".js", ".ts", ".mjs", ".cjs"}:
                raise ValueError("extension files require a JavaScript or TypeScript suffix")
            if _RELATIVE_MODULE.search(contents[source.name]):
                raise ValueError("extension file imports local modules; declare its self-contained directory")
        identity = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        prefix = PurePosixPath("extensions") / identity
        for relative, content in contents.items():
            files[str(prefix / relative)] = content
        destination = PurePosixPath(target_runtime_dir) / prefix
        if not directory:
            destination /= source.name
        targets.append(str(destination))
        manifest.append({"source": str(source), "target": str(destination), "sha256": identity, "files": hashes})
    if manifest:
        files["extensions/manifest.json"] = json.dumps({"schema_version": 1, "extensions": manifest}, sort_keys=True, indent=2) + "\n"
    return targets, files

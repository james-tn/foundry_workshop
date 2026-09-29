"""Resolve and freeze a runtime manifest into this image.

Runs once, at image build time:

    python install_runtime.py /opt/contoso/runtime/py-contoso-analytics.json

It applies two rules to the runtime:

1. A runtime is a *name*, a language version and a list of libraries written as
   `{package manager} {package spec} [{installation url}]`. The name is
   lowercase and starts with the language abbreviation (`py-`).
2. Runtimes are always *frozen*: every library, including transitive ones, is
   resolved to an exact version and that resolution is committed. Here the
   resolution is written to `<name>.resolved.json` next to the manifest, and
   the image digest is the thing you deploy.
"""
from __future__ import annotations

import hashlib
import importlib.metadata as md
import json
import pathlib
import platform
import re
import subprocess
import sys


def fail(message: str) -> None:
    print(f"RUNTIME ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def main(manifest_path: str) -> None:
    path = pathlib.Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    name = manifest["name"]

    if name != name.lower() or not re.fullmatch(r"py-[a-z0-9-]+", name):
        fail(f"runtime name {name!r} must be lowercase and start with 'py-'")
    running = ".".join(platform.python_version_tuple()[:2])
    if manifest["languageVersion"] != running:
        fail(f"manifest wants Python {manifest['languageVersion']}, image has {running}")

    specs = []
    for entry in manifest["libraries"]:
        manager, spec, *url = entry.split()
        if manager != "pip":
            fail(f"only pip libraries are supported in this image, got {entry!r}")
        specs.append(url[0] if url else spec)

    print(f"Resolving {name} ({len(specs)} declared libraries)", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", *specs], check=True)
    subprocess.run([sys.executable, "-m", "pip", "check"], check=False)

    for entry in manifest["libraries"]:
        _, spec, *_ = entry.split()
        pkg, _, wanted = spec.partition("==")
        have = md.version(pkg)
        if wanted and have != wanted:
            fail(f"{pkg}: declared {wanted}, resolved {have}")
        print(f"  {pkg}=={have}")

    resolved = {
        d.metadata["Name"].lower(): d.version for d in md.distributions() if d.metadata["Name"]
    }
    digest = hashlib.sha256(json.dumps(resolved, sort_keys=True).encode()).hexdigest()
    lock = {
        "name": name,
        "languageVersion": platform.python_version(),
        "declared": manifest["libraries"],
        "resolved": dict(sorted(resolved.items())),
        "sha256": digest,
    }
    out = path.with_name(f"{name}.resolved.json")
    out.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    print(f"Froze {len(resolved)} packages -> {out} (sha256 {digest[:16]})")


if __name__ == "__main__":
    main(sys.argv[1])

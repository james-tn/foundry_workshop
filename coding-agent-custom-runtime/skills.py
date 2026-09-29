"""Publish, inspect and roll back Foundry skills for this demo.

A skill is a versioned SKILL.md stored in the Foundry project. Every publish
creates a new immutable version; the *default* version is what agents load.
Moving the default is how you change agent behaviour without touching code or
redeploying - and rolling it back is the undo.

Usage:
    python skills.py publish skills/anomaly-method-standard/v1
    python skills.py publish skills/anomaly-method-standard/v2
    python skills.py show     anomaly-method-standard
    python skills.py rollback anomaly-method-standard 1
    python skills.py delete   anomaly-method-standard   # also resets version numbering
"""
from __future__ import annotations

import io
import os
import pathlib
import re
import sys
import zipfile

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import SkillInlineContent
from azure.identity import DefaultAzureCredential

HERE = pathlib.Path(__file__).resolve().parent
_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _client():
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    return AIProjectClient(
        endpoint=endpoint, credential=DefaultAzureCredential(), allow_preview=True
    ).beta.skills


def parse_skill(path: pathlib.Path) -> tuple[str, str, str]:
    """Return (name, description, body) from a SKILL.md.

    Front-matter values must be unquoted - quoting them makes the service
    return HTTP 500 on create.
    """
    text = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(text)
    if not match:
        raise SystemExit(f"{path}: missing YAML front matter")
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta["name"], meta["description"], match.group(2).strip()


def publish(folder: str) -> int:
    skill_md = (HERE / folder / "SKILL.md") if not folder.endswith(".md") else HERE / folder
    name, description, body = parse_skill(skill_md)
    version = _client().create(
        name,
        inline_content=SkillInlineContent(description=description, instructions=body),
        default=True,
    )
    print(f"PUBLISHED name={name} version={version.version} (now default)")
    return show(name)


def show(name: str) -> int:
    skills = _client()
    details = skills.get(name)
    print(f"SKILL={details.name}")
    print(f"  default_version={details.default_version}  latest_version={details.latest_version}")
    print(f"  description={details.description[:120]}")
    versions = sorted(skills.list_versions(name), key=lambda v: int(v.version))
    print(f"  versions={[v.version for v in versions]}")
    body = read_default(name)
    first = next((ln for ln in body.splitlines() if ln.startswith("# ")), "")
    print(f"  default body heading: {first}")
    return 0


def read_default(name: str) -> str:
    raw = b"".join(_client().download(name))
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            member = next(n for n in z.namelist() if n.endswith("SKILL.md"))
            return z.read(member).decode("utf-8")
    return raw.decode("utf-8")


def rollback(name: str, version: str) -> int:
    _client().update(name, {"default_version": version})
    print(f"ROLLED_BACK name={name} default_version={version}")
    return show(name)


def delete(name: str) -> int:
    from azure.core.exceptions import ResourceNotFoundError

    try:
        _client().delete(name)
        print(f"DELETED name={name}")
    except ResourceNotFoundError:
        print(f"ABSENT name={name} (nothing to delete)")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd, *args = argv[1:]
    if cmd == "publish":
        return publish(args[0])
    if cmd == "show":
        return show(args[0])
    if cmd == "rollback":
        return rollback(args[0], args[1])
    if cmd == "delete":
        return delete(args[0])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

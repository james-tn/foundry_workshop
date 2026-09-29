"""Show the deployed agent's version, status, and identities.

Run it after a deploy and before granting roles. The interesting part is the
bottom: a hosted agent has TWO identities - an instance identity and a
blueprint identity - and only one of them can be used in an Azure role
assignment. Getting that wrong produces a 403 at tool-call time with nothing
to explain it, so print both.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from azure.identity import DefaultAzureCredential

AGENT = os.environ.get("AGENT_NAME", "coding-agent")
VERSION = os.environ.get("AGENT_VERSION", "")  # empty = whatever the endpoint serves
API_VERSION = os.environ.get("AGENTS_API_VERSION", "2025-11-15-preview")


def _get(url: str, token: str) -> dict:
    headers = {"Authorization": "Bearer " + token}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not endpoint:
        print("FOUNDRY_PROJECT_ENDPOINT is not set", file=sys.stderr)
        return 2

    token = DefaultAzureCredential().get_token("https://ai.azure.com/.default").token
    try:
        if VERSION:
            doc = _get(f"{endpoint}/agents/{AGENT}/versions/{VERSION}?api-version={API_VERSION}", token)
        else:
            agent = _get(f"{endpoint}/agents/{AGENT}?api-version={API_VERSION}", token)
            doc = agent["versions"]["latest"]
            selector = agent.get("agent_endpoint", {}).get("version_selector", {})
            for rule in selector.get("version_selection_rules", []):
                print(
                    f"ROUTING={rule.get('type')} agent_version={rule.get('agent_version')} "
                    f"traffic={rule.get('traffic_percentage')}%"
                )
    except urllib.error.HTTPError as exc:
        print(f"RESULT=NOT_FOUND status={exc.code} detail={exc.read().decode()[:300]}")
        return 1

    definition = doc.get("definition", {})
    code = definition.get("code_configuration", {})
    print(f"AGENT={doc.get('name')} VERSION={doc.get('version')}")
    print(f"STATUS={doc.get('status')}  DRAFT={doc.get('draft')}")
    print(f"KIND={definition.get('kind')} CPU={definition.get('cpu')} MEM={definition.get('memory')}")
    print(f"RUNTIME={code.get('runtime')} DEP_RESOLUTION={code.get('dependency_resolution')}")
    print(f"CONTENT_HASH={str(code.get('content_hash'))[:16]}...")
    print("ENVIRONMENT_VARIABLES:")
    for key, value in sorted((definition.get("environment_variables") or {}).items()):
        print(f"  {key}={value}")

    print("IDENTITIES:")
    inst = doc.get("instance_identity") or {}
    bp = doc.get("blueprint") or {}
    print(f"  instance_identity.principal_id = {inst.get('principal_id')}   <- grant RBAC to THIS one")
    print(f"  blueprint.principal_id         = {bp.get('principal_id')}   <- NOT assignable in RBAC")
    print("RESULT=OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

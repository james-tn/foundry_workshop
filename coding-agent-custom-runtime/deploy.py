"""Deploy the agent in agent/ to the Foundry hosted runtime.

Two lines in here carry the whole "agent runtime customization" story:

* `dependency_resolution="remote_build"` - Foundry installs
  `agent/requirements.txt` into the runtime image when the version is built.
  That is how your custom libraries get into the runtime. It is one field.

* `environment_variables` - anything prefixed `AGENTENV_` in your shell is
  forwarded into the agent container without being written to disk here.

That is the *agent's* runtime. The runtime that model-written code executes in
is a different thing on purpose - see sandbox/ - and the agent only knows it
by name, through SANDBOX_RUNTIMES.

There is no VNet hop in this script. Deployment is a data-plane call, so if
the Foundry account has publicNetworkAccess=Disabled, run this from a network
that can reach the project (findings.md, F13). Against a public-endpoint
project it is a direct call.
"""
from __future__ import annotations

import hashlib
import io
import os
import pathlib
import sys
import time
import zipfile

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    CodeConfiguration,
    HostedAgentDefinition,
    ProtocolVersionRecord,
)
from azure.identity import DefaultAzureCredential

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "agent"
AGENT_NAME = os.environ.get("AGENT_NAME", "coding-agent")

# Forwarded into the agent container. SANDBOX_RUNTIMES is the runtime
# registry (runtime name -> session pool); swapping a pool behind a runtime
# name is a config change, not a code change.
PASSTHROUGH = (
    "SANDBOX_RUNTIMES",
    "DEFAULT_RUNTIME",
    "CONTOSO_BACKEND_BASE",
    "CONTOSO_BACKEND_CONNECTION",
    "SESSIONS_API_VERSION",
    "SKILL_NAMES",
)


def build_zip() -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(SRC.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(SRC).as_posix())
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def agent_env(endpoint: str, deployment: str) -> dict[str, str]:
    env = {
        "FOUNDRY_PROJECT_ENDPOINT": endpoint,
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": deployment,
    }
    for key in PASSTHROUGH:
        value = os.environ.get(key)
        if value:
            env[key] = value
    for key, value in os.environ.items():
        if key.startswith("AGENTENV_"):
            env[key[len("AGENTENV_") :]] = value
    return env


def main() -> int:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not endpoint:
        print("FOUNDRY_PROJECT_ENDPOINT is not set", file=sys.stderr)
        return 2
    deployment = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "Kimi-K2.7-Code")

    data, digest = build_zip()
    env = agent_env(endpoint, deployment)

    print(f"AGENT_NAME={AGENT_NAME}")
    print(f"ZIP_BYTES={len(data)} SHA256={digest[:16]}...")
    print(f"MODEL={deployment}")
    print(f"ENV_KEYS={sorted(env)}")

    definition = HostedAgentDefinition(
        cpu="1",
        memory="2Gi",
        code_configuration=CodeConfiguration(
            runtime="python_3_13",
            entry_point=["python", "main.py"],
            dependency_resolution="remote_build",
        ),
        environment_variables=env,
        protocol_versions=[ProtocolVersionRecord(protocol="responses", version="2.0.0")],
    )

    stream = io.BytesIO(data)
    stream.name = "code.zip"

    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    started = time.time()
    try:
        version = client.agents.create_version_from_code(
            AGENT_NAME,
            definition=definition,
            code=stream,
            code_zip_sha256=digest,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ELAPSED_S={time.time() - started:.2f}")
        print(f"RESULT=FAILED status={getattr(exc, 'status_code', None)}")
        print(f"DETAIL={str(exc)[:600]}")
        return 1

    print(f"ELAPSED_S={time.time() - started:.2f}")
    print("RESULT=DEPLOY_ACCEPTED")
    print(f"VERSION={getattr(version, 'version', None)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

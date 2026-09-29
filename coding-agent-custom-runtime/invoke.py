"""Invoke the deployed hosted agent over the Responses protocol.

Note: a hosted agent is NOT addressable
as a model on the project's /responses endpoint. The service gives each agent
its own route:

    {project}/agents/{name}/endpoint/protocols/openai/responses

The first call after a deploy returns 424 while the runtime image builds and
the session starts. That is the cold start to budget for - this script
retries and prints how long it actually took, so the number on screen is
measured rather than claimed.

Usage:
    python invoke.py "your prompt here"
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

from azure.identity import DefaultAzureCredential

AGENT = os.environ.get("AGENT_NAME", "coding-agent")
API_VERSION = os.environ.get("AGENTS_API_VERSION", "2025-11-15-preview")
READY_TIMEOUT = int(os.environ.get("READY_TIMEOUT", "900"))


def post(url: str, token: str, prompt: str, timeout: int = 600):
    body = json.dumps({"input": prompt}).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode()), time.time() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:600], time.time() - started
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}", time.time() - started


def output_text(payload) -> str:
    if not isinstance(payload, dict):
        return str(payload)[:400]
    chunks = []
    for item in payload.get("output", []):
        for content in item.get("content") or []:
            if content.get("type") in ("output_text", "text"):
                chunks.append(content.get("text", ""))
    return "\n".join(chunks)


def main() -> int:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not endpoint:
        print("FOUNDRY_PROJECT_ENDPOINT is not set", file=sys.stderr)
        return 2
    prompt = " ".join(sys.argv[1:]) or "What is 6817 multiplied by 41? Use a tool."

    url = f"{endpoint}/agents/{AGENT}/endpoint/protocols/openai/responses?api-version={API_VERSION}"
    token = DefaultAzureCredential().get_token("https://ai.azure.com/.default").token

    print(f"AGENT={AGENT}")
    print(f"URL_PATH=/agents/{AGENT}/endpoint/protocols/openai/responses")
    print(f"PROMPT={prompt}")

    wall = time.time()
    deadline = wall + READY_TIMEOUT
    attempts = 0
    logged_424 = False
    while True:
        attempts += 1
        status, payload, elapsed = post(url, token, prompt)
        if status == 424:
            if not logged_424:
                print(f"  first_424={str(payload)[:240]}")
                logged_424 = True
            if attempts % 6 == 0:
                print(f"  waiting for runtime... attempts={attempts} elapsed_s={time.time()-wall:.0f}")
            if time.time() > deadline:
                print(f"RESULT=RUNTIME_NEVER_READY attempts={attempts}")
                return 1
            time.sleep(5)
            continue
        break

    print(f"ATTEMPTS={attempts}")
    print(f"TIME_TO_FIRST_RESPONSE_S={time.time()-wall:.2f}")
    print(f"STATUS={status}")
    print(f"TURN_ELAPSED_S={elapsed:.2f}")
    if status != 200:
        print(f"RESULT=INVOKE_FAILED DETAIL={str(payload)[:600]}")
        return 1

    for item in payload.get("output", []):
        if item.get("type") == "function_call":
            print(f"TOOL_CALL name={item.get('name')} args={str(item.get('arguments'))[:400]}")
        if item.get("type") == "function_call_output":
            print(f"TOOL_OUTPUT={str(item.get('output'))[:1600]}")
    print("--- OUTPUT ---")
    print(output_text(payload))
    print("RESULT=INVOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

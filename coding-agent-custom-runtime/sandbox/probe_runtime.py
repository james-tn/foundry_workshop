"""Ask a sandbox runtime what it actually contains. No agent involved.

    python sandbox\\probe_runtime.py py-generic
    python sandbox\\probe_runtime.py py-contoso-analytics
    python sandbox\\probe_runtime.py py-generic --freeze      # full pip freeze

Runtimes are resolved from SANDBOX_RUNTIMES (JSON: runtime name -> session pool
management endpoint), the same mapping the hosted agent receives. This is the proof step in
sandbox/README.md: the libraries the manifest declares are present, at
the pinned versions, and the generic managed pool does not have them.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

from azure.identity import DefaultAzureCredential

API_VERSION = os.environ.get("SESSIONS_API_VERSION", "2025-02-02-preview")
WATCH = ["numpy", "pandas", "scipy", "scikit-learn", "ruptures", "contoso_sdk"]

PROBE = r'''
import importlib.metadata as md, json, os, platform, socket
watch = %r
report = {
    "python": platform.python_version(),
    "hostname": socket.gethostname(),
    "runtime": os.environ.get("CONTOSO_RUNTIME", "(none - not a platform runtime image)"),
}
report["libraries"] = {}
modules = {"scikit-learn": "sklearn"}
for name in watch:
    try:
        imported = getattr(__import__(modules.get(name, name)), "__version__", "?")
    except ImportError:
        report["libraries"][name] = "NOT INSTALLED"
        continue
    try:
        meta = md.version(name)
    except md.PackageNotFoundError:
        meta = None
    # A pip upgrade over a base-image package can leave a ghost *.dist-info
    # entry that importlib.metadata finds first (findings.md, F4). Report what imports.
    report["libraries"][name] = imported if meta == imported else f"{imported} (metadata says {meta})"
lock = "/opt/contoso/runtime/py-contoso-analytics.resolved.json"
if os.path.exists(lock):
    resolved = json.load(open(lock))
    report["resolved_lock"] = {"path": lock, "sha256": resolved.get("sha256", "?")[:16],
                               "pinned_packages": len(resolved.get("resolved", {}))}
try:
    import contoso_sdk
    report["contoso_sdk"] = contoso_sdk.runtime_info()
except Exception as exc:
    report["contoso_sdk"] = f"import failed: {type(exc).__name__}"
if %r:
    report["freeze"] = sorted(f"{d.metadata['Name']}=={d.version}" for d in md.distributions())
print(json.dumps(report, indent=2))
'''


def runtimes() -> dict[str, str]:
    raw = os.environ.get("SANDBOX_RUNTIMES", "")
    if raw:
        return {k: v.rstrip("/") for k, v in json.loads(raw).items()}
    pool = os.environ.get("SESSION_POOL_ENDPOINT", "").rstrip("/")
    return {"py-generic": pool} if pool else {}


def execute(endpoint: str, code: str) -> dict:
    token = DefaultAzureCredential().get_token("https://dynamicsessions.io/.default").token
    url = f"{endpoint}/executions?api-version={API_VERSION}&identifier=probe-{uuid.uuid4().hex[:12]}"
    body = json.dumps({"codeInputType": "inline", "executionType": "synchronous", "code": code})
    req = urllib.request.Request(
        url,
        data=body.encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def pool_label(endpoint: str) -> str:
    if "/sessionPools/" in endpoint:
        return endpoint.rsplit("/", 1)[-1]
    return endpoint.split("//", 1)[-1].split(".", 1)[0]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    name = sys.argv[1]
    freeze = "--freeze" in sys.argv
    pools = runtimes()
    if name not in pools:
        print(f"unknown runtime {name!r}; SANDBOX_RUNTIMES has {sorted(pools)}", file=sys.stderr)
        return 2
    started = time.perf_counter()
    try:
        raw = execute(pools[name], PROBE % (WATCH, freeze))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode()[:400]}", file=sys.stderr)
        return 1
    result = raw.get("result") or {}
    print(f"RUNTIME={name}")
    print(f"POOL={pool_label(pools[name])}")
    print(f"STATUS={raw.get('status')}  ELAPSED_MS={round((time.perf_counter() - started) * 1000)}")
    print(result.get("stdout") or "")
    if result.get("stderr"):
        print("STDERR:", result["stderr"][:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

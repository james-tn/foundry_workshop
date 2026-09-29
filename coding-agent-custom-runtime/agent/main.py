"""A platform coding agent, written in LangGraph and hosted on Microsoft Foundry.

This file is written to be read as much as executed. The agent works on a
model-driven application platform, at demo scale: a developer asks for a method on a platform Type; the agent
reads the Type from the platform, writes the `.type` declaration and the
`Type.py` implementation, and tests the method on a real object in the runtime
the method claims - before it answers.

Five things are worth pointing at.

1. The harness is LangGraph, not a Microsoft framework. `create_agent()` builds
   an ordinary graph and `ResponsesHostServer` hosts it. Your harness moves;
   nothing in it becomes Foundry-specific.

2. The model is resolved from the project, and authentication is
   `DefaultAzureCredential`. No API key in this file, the image or the env.

3. The platform stays where it is. `describe_type` and `get_obj` call a
   Contoso platform API that Foundry does not host, with a credential
   resolved from a Foundry *connection* at call time.

4. Model-written code never runs in this process. `test_method` and
   `run_in_runtime` send it to a sandboxed *runtime*: a named runtime
   (`py-contoso-analytics`) implemented as an Azure Container Apps session pool
   built from a custom image with pinned and private libraries.
   `SANDBOX_RUNTIMES` is the registry: runtime name -> pool. A method claim
   (`py-contoso-analytics-server`) names the runtime, never the pool.
   `describe_execution_context` shows why the indirection matters.

5. Team standards come from Foundry *skills*, not from this file. On each
   request the agent lists the allow-listed skills that exist in the project;
   only a matching task calls `load_skill`, which fetches the *current default
   version*. Publish or roll back a skill and the next request follows it.
"""
from __future__ import annotations

import base64
import io
import json
import os
import time
import traceback
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import redirect_stdout
from typing import Annotated

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState, ModelRequest, after_model, dynamic_prompt
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from langchain_azure_ai.agents.hosting import ResponsesHostServer

_AZURE_AI_SCOPE = "https://ai.azure.com/.default"
_SESSIONS_SCOPE = "https://dynamicsessions.io/.default"

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
BACKEND_BASE = os.environ.get("CONTOSO_BACKEND_BASE", "").rstrip("/")
BACKEND_CONNECTION = os.environ.get("CONTOSO_BACKEND_CONNECTION", "")
SESSIONS_API_VERSION = os.environ.get("SESSIONS_API_VERSION", "2025-02-02-preview")
SKILL_NAMES = [s.strip() for s in os.environ.get("SKILL_NAMES", "").split(",") if s.strip()]


def _load_runtimes() -> dict[str, str]:
    """Runtime name -> session pool endpoint. Deploy config, not code."""
    raw = os.environ.get("SANDBOX_RUNTIMES", "")
    if raw:
        return {name: url.rstrip("/") for name, url in json.loads(raw).items()}
    legacy = os.environ.get("SESSION_POOL_ENDPOINT", "").rstrip("/")
    return {"py-generic": legacy} if legacy else {}


RUNTIMES = _load_runtimes()
DEFAULT_RUNTIME = os.environ.get("DEFAULT_RUNTIME", "py-contoso-analytics")

_credential = DefaultAzureCredential()
_project: AIProjectClient | None = None
_preview: AIProjectClient | None = None


def _project_client() -> AIProjectClient:
    global _project
    if _project is None:
        _project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=_credential)
    return _project


def _skills_client():
    # Skills are a preview API, so they get their own opt-in client.
    global _preview
    if _preview is None:
        _preview = AIProjectClient(
            endpoint=PROJECT_ENDPOINT, credential=_credential, allow_preview=True
        )
    return _preview.beta.skills


# ------------------------------------------------------ 3. the platform


def _token_principal() -> str:
    """Which identity the runtime is actually presenting (oid only; no secret)."""
    try:
        payload = _credential.get_token(_AZURE_AI_SCOPE).token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return f"oid={claims.get('oid', '?')[:8]}"
    except Exception as exc:  # noqa: BLE001
        return f"oid=?({type(exc).__name__})"


def _read_connection_secret() -> tuple[str | None, str]:
    conn = _project_client().connections.get(BACKEND_CONNECTION, include_credentials=True)
    creds = getattr(conn, "credentials", None)
    for attr in ("api_key", "key"):
        value = getattr(creds, attr, None)
        if isinstance(value, str) and value:
            return value, f"foundry_connection:{BACKEND_CONNECTION}"

    # CustomKeys connections return {"keys": {"x-api-key": "..."}}, so the
    # secret is one level deeper than for a plain api-key connection.
    bag = (conn.as_dict() if hasattr(conn, "as_dict") else {}).get("credentials") or {}
    nested = bag.get("keys") if isinstance(bag.get("keys"), dict) else {}
    for source in (nested, bag):
        for key_name, candidate in source.items():
            if isinstance(candidate, str) and candidate and key_name != "type":
                return candidate, f"foundry_connection:{BACKEND_CONNECTION}"
    return None, f"connection_had_no_secret:{sorted(bag)}"


def _resolve_backend_key() -> tuple[str | None, str]:
    """Fetch the platform credential from a Foundry connection, at call time.

    Returns (key, how_it_was_resolved) so the demo can *show* which path was
    taken rather than assert it. The key is never written to disk, never baked
    into the image, and never placed in the prompt.

    Connection reads occasionally return PermissionDenied for a principal that
    does hold the roles (see F7 in findings.md), so the read is retried and any
    retry is reported, not hidden.
    """
    if not BACKEND_CONNECTION:
        return None, "no_connection_configured"
    denied = []
    for attempt, pause in enumerate((0.0, 0.5, 1.5, 3.0), start=1):
        time.sleep(pause)
        try:
            key, how = _read_connection_secret()
            if denied:
                how += f" (attempt {attempt}; earlier: {'; '.join(denied)})"
            return key, how
        except Exception as exc:  # noqa: BLE001
            reason = "PermissionDenied" if "PermissionDenied" in str(exc) else type(exc).__name__
            denied.append(f"{reason} as {_token_principal()}")
            if reason != "PermissionDenied":
                break
    return None, f"connection_error:{' | '.join(denied)}"[:400]


def _platform_get(path: str) -> dict:
    if not BACKEND_BASE:
        return {"error": "CONTOSO_BACKEND_BASE is not configured"}
    api_key, how = _resolve_backend_key()
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    started = time.perf_counter()
    req = urllib.request.Request(f"{BACKEND_BASE}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = {"error": f"HTTP {exc.code}", "detail": exc.read().decode()[:200]}
    except Exception as exc:  # noqa: BLE001
        body = {"error": type(exc).__name__, "detail": str(exc)[:200]}
    body["_credential_source"] = how
    body["_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return body


@tool
def describe_type(
    type_name: Annotated[str, "Type name, for example WindTurbine."],
) -> str:
    """Read a Type from the platform: its .type declaration, its existing
    Type.py implementation, its object ids, and the runtimes methods may claim.
    """
    body = _platform_get(f"/types/{type_name}")
    for name, spec in (body.get("runtimes") or {}).items():
        spec["available_to_agent"] = name in RUNTIMES
    return json.dumps(body)


@tool
def get_obj(
    type_name: Annotated[str, "Type name, for example WindTurbine."],
    obj_id: Annotated[str, "Object id, for example TURBINE-014."],
) -> str:
    """Fetch one object of a Type from the platform, with all its fields."""
    return json.dumps(_platform_get(f"/types/{type_name}/objs/{obj_id}"))


# ------------------------------------------- 4. sandboxed runtimes (the pools)


def _runtime_name(runtime: str) -> str:
    """Accept a runtime ('py-contoso-analytics') or a method claim ('...-server')."""
    name = (runtime or DEFAULT_RUNTIME).strip()
    for suffix in ("-server", "-client"):
        if name.endswith(suffix) and name[: -len(suffix)] in RUNTIMES:
            return name[: -len(suffix)]
    return name


def _execute_in_runtime(runtime: str, code: str) -> dict:
    """POST code to the session pool that implements `runtime`.

    Every call gets a fresh session identifier, so no state - and no other
    user's data - survives from one execution to the next.
    """
    name = _runtime_name(runtime)
    pool = RUNTIMES.get(name)
    out: dict[str, object] = {"execution_mode": "aca_dynamic_session", "runtime": name}
    if not pool:
        out["error"] = f"unknown runtime {name!r}; available: {sorted(RUNTIMES)}"
        return out
    out["pool"] = (
        pool.rsplit("/", 1)[-1] if "/sessionPools/" in pool
        else pool.split("//", 1)[-1].split(".", 1)[0]  # custom pools: <name>.<env>.azurecontainerapps.io
    )

    started = time.perf_counter()
    token = _credential.get_token(_SESSIONS_SCOPE).token
    url = f"{pool}/executions?api-version={SESSIONS_API_VERSION}&identifier={uuid.uuid4()}"
    payload = json.dumps(
        {"codeInputType": "inline", "executionType": "synchronous", "code": code}
    ).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = json.loads(resp.read().decode())
        inner = raw.get("result") or {}
        out.update(
            status=raw.get("status"),
            stdout=(inner.get("stdout") or "")[:4000],
            stderr=(inner.get("stderr") or "")[:1500],
            sandbox_execution_ms=inner.get("executionTimeInMilliseconds"),
        )
    except urllib.error.HTTPError as exc:
        out.update(error=f"HTTP {exc.code}", detail=exc.read().decode()[:300])
    except Exception as exc:  # noqa: BLE001
        out.update(error=type(exc).__name__, detail=str(exc)[:300])
    out["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return out


@tool
def run_in_runtime(
    code: Annotated[str, "Python source to execute. Use print() to produce output."],
    runtime_name: Annotated[str, "Runtime name, e.g. py-contoso-analytics or py-generic."] = DEFAULT_RUNTIME,
) -> str:
    """Execute Python in a sandboxed platform runtime (an isolated session, no egress)."""
    # Not called `runtime`: LangChain reserves that parameter name for ToolRuntime.
    return json.dumps(_execute_in_runtime(runtime_name, code))


_METHOD_HARNESS = r'''
import json, traceback
TYPE, METHOD = %(type)r, %(method)r
SOURCE = json.loads(%(source)r)
THIS = json.loads(%(this)r)
ARGS = json.loads(%(args)r)
try:
    import contoso_sdk
except ModuleNotFoundError:
    print(json.dumps({"status": "RUNTIME_CANNOT_TEST",
                      "error": "contoso_sdk is not installed in this runtime; platform methods are "
                               "tested in a runtime that ships it (py-contoso-analytics)"}))
else:
    try:
        result = contoso_sdk.invoke(TYPE, SOURCE, METHOD, THIS, **ARGS)
        result["status"] = "PASS"
    except Exception as exc:
        result = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()[-1200:]}
    result["runtime_info"] = contoso_sdk.runtime_info()
    print(json.dumps(result))
'''


@tool
def test_method(
    type_name: Annotated[str, "Type the method belongs to, e.g. WindTurbine."],
    obj_id: Annotated[str, "Real object to test on, e.g. TURBINE-014."],
    implementation: Annotated[str, "Full source of the Type.py implementation file."],
    method: Annotated[str, "Name of the method (top-level function) to call."],
    runtime_name: Annotated[str, "Runtime the method claims, e.g. py-contoso-analytics."] = DEFAULT_RUNTIME,
    args_json: Annotated[str, "JSON object of keyword arguments, e.g. {\"window\": 24}."] = "{}",
) -> str:
    """Test a generated method on a real object, in the runtime it claims.

    The object is fetched from the platform here, with the connection
    credential, and passed in as data: the sandbox itself has no network and
    no credentials. Returns the method's value, timing, and convention checks.
    """
    record = _platform_get(f"/types/{type_name}/objs/{obj_id}")
    if "error" in record:
        return json.dumps({"status": "OBJ_FETCH_FAILED", **record})
    credential_source = record.pop("_credential_source", None)
    record.pop("_elapsed_ms", None)
    try:
        json.loads(args_json or "{}")
    except json.JSONDecodeError as exc:
        return json.dumps({"status": "BAD_ARGS", "error": str(exc)})

    code = _METHOD_HARNESS % {
        "type": type_name,
        "method": method,
        "source": json.dumps(implementation),
        "this": json.dumps(record),
        "args": args_json or "{}",
    }
    run = _execute_in_runtime(runtime_name, code)
    try:
        result = json.loads((run.get("stdout") or "").strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        result = {"status": "NO_RESULT", "stdout": run.get("stdout"), "stderr": run.get("stderr")}
    result.update(
        obj=f"{type_name}:{obj_id}",
        executed_in=run.get("runtime"),
        pool=run.get("pool"),
        sandbox_execution_ms=run.get("sandbox_execution_ms"),
        total_ms=run.get("elapsed_ms"),
        obj_credential_source=credential_source,
    )
    if run.get("error"):
        result.update(sandbox_error=run["error"], detail=run.get("detail"))
    return json.dumps(result)


# ------------------------------------------------------ the security reveal


def _exec_in_process(code: str) -> dict:
    """What many harnesses do with model-written code: exec() it in the agent.

    Deliberately NOT exposed to the model as a tool. It exists so that
    describe_execution_context can show what such code would be able to see.
    """
    started = time.perf_counter()
    buffer = io.StringIO()
    result: dict[str, object] = {"execution_mode": "in_process"}
    try:
        with redirect_stdout(buffer):
            exec(compile(code, "<agent_tool>", "exec"), {"__name__": "__main__"})  # noqa: S102
        result["stdout"] = buffer.getvalue()[:4000]
    except Exception:  # noqa: BLE001
        result["stdout"] = buffer.getvalue()[:2000]
        result["traceback"] = traceback.format_exc()[-800:]
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


_CONTEXT_PROBE = r'''
import json, os, socket, urllib.request, urllib.error

report = {"hostname": socket.gethostname(), "runtime": os.environ.get("CONTOSO_RUNTIME", "-")}

names = [k for k in os.environ if not k.startswith(("LC_", "LANG"))]
report["env_var_count"] = len(names)
report["sensitive_env_names"] = sorted(
    n for n in names
    if any(t in n.upper() for t in ("KEY", "SECRET", "TOKEN", "CONNECTION", "PASSWORD", "IDENTITY"))
)[:20]

# Can this code mint an Azure token from the ambient managed identity?
idep = os.environ.get("IDENTITY_ENDPOINT") or os.environ.get("MSI_ENDPOINT")
if idep:
    try:
        hdr = os.environ.get("IDENTITY_HEADER", "")
        u = f"{idep}?resource=https://management.azure.com/&api-version=2019-08-01"
        rq = urllib.request.Request(u, headers={"X-IDENTITY-HEADER": hdr})
        with urllib.request.urlopen(rq, timeout=10) as r:
            report["managed_identity_token"] = "MINTED" if b"access_token" in r.read() else "no_token"
    except Exception as e:
        report["managed_identity_token"] = f"blocked:{type(e).__name__}"
else:
    report["managed_identity_token"] = "no_identity_endpoint_visible"

egress = {}
for label, url in (("public_internet", "https://example.com"),
                   ("azure_control_plane", "https://management.azure.com/")):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            egress[label] = f"reachable:{r.status}"
    except urllib.error.HTTPError as e:
        egress[label] = f"reachable:{e.code}"
    except Exception as e:
        egress[label] = f"blocked:{type(e).__name__}"
report["egress"] = egress

print(json.dumps(report, indent=2))
'''


@tool
def describe_execution_context(
    mode: Annotated[str, "Either 'in_process' or 'sandboxed'."] = "in_process",
) -> str:
    """Report what executed code can actually see: secrets, identity, network.

    'in_process' runs the probe inside the agent process, the way a harness
    that exec()s model-written code would. 'sandboxed' runs the identical probe
    in the default runtime's session pool. Compare the two.
    """
    if mode == "sandboxed":
        return json.dumps(_execute_in_runtime(DEFAULT_RUNTIME, _CONTEXT_PROBE))
    return json.dumps(_exec_in_process(_CONTEXT_PROBE))


# ----------------------------------------------------------------- 5. skills


def _skill_body(raw: bytes) -> str:
    """The content endpoint may return a zip bundle or bare SKILL.md text."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            member = next(n for n in bundle.namelist() if n.endswith("SKILL.md"))
            raw = bundle.read(member)
    text = raw.decode("utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[-1]
    return text.strip()


_SKILL_INDEX_TTL_S = 10.0
_index_cache: tuple[float, str] = (float("-inf"), "")


def _skill_index() -> str:
    """Names and descriptions of the allow-listed skills that exist *right now*.

    Rebuilt on every request (with a 10 s cache), so publishing a skill makes it
    visible to a running agent and deleting it makes it disappear. The full
    instructions are not included here; they load on demand via load_skill.
    """
    global _index_cache
    fetched_at, cached = _index_cache
    if time.monotonic() - fetched_at < _SKILL_INDEX_TTL_S:
        return cached
    lines = []
    for name in SKILL_NAMES:  # the allow-list lives in deploy config, not the project
        try:
            lines.append(f"- {name}: {_skills_client().get(name).description}")
        except Exception as exc:  # not published (yet): the agent simply can't see it
            print(f"skill {name} not indexed: {type(exc).__name__}", flush=True)
    text = "" if not lines else (
        "\n\nSKILLS. These team standards are published in the Foundry project. "
        "Each description says exactly when it applies:\n"
        + "\n".join(lines)
        + "\nIf the user's request is what a description says it is for, call "
        "load_skill before any other tool and follow the returned instructions "
        "exactly, including the algorithm, the tests and the report format; load "
        "it fresh every time, because it may have changed. If the request is not "
        "what a description is for, do NOT call load_skill.\n"
    )
    _index_cache = (time.monotonic(), text)
    return text


@dynamic_prompt
def _prompt_with_skills(request: ModelRequest) -> str:
    return SYSTEM_PROMPT + _skill_index()


@after_model
def _answer_from_reasoning(state: AgentState, runtime) -> dict | None:
    """Surface a final answer that the model returned in its reasoning summary.

    Some model endpoints (seen with Kimi-K2.7-Code in about 1 final turn in 4)
    return an empty message and put the answer in the reasoning summary.
    Clients that read the message text would then show nothing, so the summary
    is added as the answer. Turns with text or tool calls are left alone.
    """
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or last.tool_calls or last.text.strip():
        return None
    blocks = last.content if isinstance(last.content, list) else []
    summary = "".join(
        part.get("text", "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "reasoning"
        for part in block.get("summary") or []
        if isinstance(part, dict)
    ).strip()
    if not summary:
        return None
    print("final answer was in the reasoning summary; surfacing it", flush=True)
    return {"messages": [AIMessage(content=summary, id=str(uuid.uuid4()))]}


@tool
def load_skill(name: Annotated[str, "Skill name from the SKILLS list"]) -> str:
    """Load the current default version of a Foundry skill: its full instructions."""
    started = time.perf_counter()
    try:
        skills = _skills_client()
        version = skills.get(name).default_version
        body = _skill_body(b"".join(skills.download(name)))
    except Exception as exc:
        return json.dumps({"name": name, "error": f"{type(exc).__name__}: {exc}"[:400]})
    return json.dumps(
        {
            "name": name,
            "version": version,
            "loaded_ms": round((time.perf_counter() - started) * 1000),
            "instructions": body,
        }
    )


# --------------------------------------------------------------- 1 + 2. host


def _build_chat_model() -> ChatOpenAI:
    deployment = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", "Kimi-K2.7-Code")
    openai_client = _project_client().get_openai_client()
    token_provider = get_bearer_token_provider(_credential, _AZURE_AI_SCOPE)
    return ChatOpenAI(
        model=deployment,
        base_url=str(openai_client.base_url),
        api_key=token_provider,
        use_responses_api=True,
        output_version="responses/v1",
    )


SYSTEM_PROMPT = """You are a coding agent for the Contoso platform, hosted on Microsoft Foundry.

Developers ask you to add or change methods on platform Types. You reach the platform
through describe_type and get_obj. Code you write never runs in your own
process: it runs in a sandboxed runtime through test_method or run_in_runtime.

For every request to add or change a method on a Type:
1. Call describe_type first. Follow the conventions of the files it returns.
2. Write the declaration to add to the .type file, with a /** doc */ comment.
   Its runtime claim is a runtime name plus "-server" (e.g. py-contoso-analytics-server).
   Claim a runtime that has every library your code imports.
3. Write the complete Type.py implementation: a top-level function named after
   the method; first parameter `this` for a member method, `cls` for a static
   one; third-party imports inside the function, never at the top of the file.
4. Call test_method on a real object id, in the runtime your claim names.
   If the test does not PASS, fix the code and test again.
5. Answer with the .type declaration, the full .py code, and the measured test
   result (value, runtime, sandbox time). Never report a result you did not get
   from a test.

When asked about execution modes, call describe_execution_context in each mode
asked for and report only what was measured.
"""


def main() -> None:
    graph = create_agent(
        _build_chat_model(),
        tools=[
            describe_type,
            get_obj,
            test_method,
            run_in_runtime,
            describe_execution_context,
            load_skill,
        ],
        middleware=[
            _prompt_with_skills,  # SYSTEM_PROMPT + live skill index
            _answer_from_reasoning,  # answer returned in the reasoning summary
        ],
    )
    ResponsesHostServer(graph).run(port=int(os.environ.get("PORT", "8088")))


if __name__ == "__main__":
    main()

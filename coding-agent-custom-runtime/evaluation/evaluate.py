"""Evaluate the platform coding agent with Microsoft Foundry evaluations.

Run from this folder, after dot-sourcing ..\\env.ps1:

    python evaluate.py register                   # register the four custom evaluators, pin their versions
    python evaluate.py recorded                   # score two recorded traces, no agent calls
    python evaluate.py run <agent> [--label L]    # Foundry sends every dataset row to the hosted agent
    python evaluate.py rescore <label>            # re-score tool_call_accuracy on de-duplicated traces
    python evaluate.py compare <label> <label>... [--fail-on-regression]
    python evaluate.py fetch <label>              # download a run's results again
    python evaluate.py tool-defs                  # regenerate tool_definitions.json from agent/main.py
    python evaluate.py cleanup [--evaluators]     # delete the eval groups (and evaluators) this script made

Evaluator versions, eval group IDs and run IDs are kept in .eval-state.json.
Results are written to results/<label>.json. Both are gitignored.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import warnings

HERE = pathlib.Path(__file__).resolve().parent
AGENT_ROOT = HERE.parent
STATE = HERE / ".eval-state.json"
RESULTS = HERE / "results"
EVALUATORS = HERE / "evaluators"
DATASET = HERE / "datasets" / "smoke.jsonl"
TOOL_DEFS = HERE / "tool_definitions.json"
SAMPLES = AGENT_ROOT / "sample-output"

sys.path.insert(0, str(EVALUATORS))
from grounded_numbers import analyze  # noqa: E402

warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# name -> how to register it. Prompt evaluators score 1-5; code evaluators score 0-1.
CUSTOM = {
    "expected_behavior": {
        "file": "expected_behavior.prompt.txt", "fields": ["query", "response", "expected_behavior"],
        "description": "The trace meets the row's expected_behavior rubric (1-5).",
    },
    "tool_claims_verified": {
        "file": "tool_claims_verified.prompt.txt", "fields": ["query", "response"],
        "description": "Every result the answer states is backed by a tool output in the trace (1-5).",
    },
    "grounded_numbers": {
        "file": "grounded_numbers.py",
        "description": "Share of numbers in the final answer that trace back to a tool input or output (0-1).",
    },
    "answer_present": {
        "file": "answer_present.py",
        "description": "1 if the agent returned a non-empty final answer.",
    },
}

AGENT_TOOLS = ["describe_type", "get_obj", "test_method", "run_in_runtime",
               "describe_execution_context", "load_skill"]
WARM_PROMPT = "Without calling any tools, reply with the single word: ready"


# --- configuration and state --------------------------------------------------

def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set. Dot-source ..\\env.ps1 first (see env.example.ps1).")
    return value


def clients():
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    project = AIProjectClient(endpoint=env("FOUNDRY_PROJECT_ENDPOINT").rstrip("/"),
                              credential=DefaultAzureCredential())
    return project, project.get_openai_client()


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def update_state(**changes) -> dict:
    """Merge top-level keys into the state file, under a lock, so parallel commands do not clobber it."""
    lock = STATE.with_suffix(".lock")
    deadline = time.time() + 30
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                lock.unlink(missing_ok=True)
            time.sleep(0.05)
    try:
        state = load_state()
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(state.get(key), dict):
                state[key].update(value)
            else:
                state[key] = value
        STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
        return state
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


# --- dataset --------------------------------------------------------------------

def system_prompt() -> str:
    source = (AGENT_ROOT / "agent" / "main.py").read_text(encoding="utf-8")
    return re.search(r'(?s)SYSTEM_PROMPT = """(.*?)"""', source).group(1).strip()


def with_context(row: dict) -> dict:
    """Add what the judges need but the agent does not get: tool definitions and the agent's instructions.

    The agent receives only {{item.query}}. The judges receive {{item.query_messages}}, the system prompt
    followed by the same user message, so they can check the answer against the agent's own rules.
    """
    row = dict(row)
    row["tool_definitions"] = json.loads(TOOL_DEFS.read_text(encoding="utf-8"))
    row["query_messages"] = [{"role": "system", "content": system_prompt()},
                             {"role": "user", "content": row["query"]}]
    return row


def smoke_rows() -> list[dict]:
    lines = DATASET.read_text(encoding="utf-8").splitlines()
    return [with_context(json.loads(line)) for line in lines if line.strip()]


def recorded_rows() -> list[dict]:
    """Two recorded runs of the same prompt, as chat-format traces: the F14 fabrication and an honest run."""
    probe = next(r for r in smoke_rows() if r["id"] == "in-process-vs-sandbox")
    rows = []
    for name, row_id in (("09-unverified-tool-claim.txt", "recorded-f14-fabricated"),
                         ("04-in-process-vs-sandbox.txt", "recorded-honest")):
        chunk = re.split(r"(?m)^AGENT=", (SAMPLES / name).read_text(encoding="utf-8"))[1]
        trace, n = [], 0
        for line in chunk.splitlines():
            if line.startswith("TOOL_CALL "):
                m = re.match(r"TOOL_CALL name=(\S+) args=(.*)", line)
                n += 1
                trace.append({"role": "assistant", "content": [
                    {"type": "tool_call", "tool_call_id": f"call_{n}", "name": m[1], "arguments": json.loads(m[2])}]})
            elif line.startswith("TOOL_OUTPUT="):
                trace.append({"role": "tool", "tool_call_id": f"call_{n}", "content": [
                    {"type": "tool_result", "tool_result": line[len("TOOL_OUTPUT="):]}]})
        answer = chunk.split("--- OUTPUT ---", 1)[1].split("RESULT=")[0].strip()
        trace.append({"role": "assistant", "content": answer})
        rows.append({**probe, "id": row_id, "response": trace, "response_text": answer})
    return rows


def item_schema(extra: dict | None = None) -> dict:
    # include_sample_schema must be true even for dataset runs, which have no sample:
    # with false, every custom code evaluator fails with "An error occurred during grading".
    arr, txt = {"type": "array"}, {"type": "string"}
    props = {"id": txt, "query": txt, "expected_behavior": txt, "tool_definitions": arr, "query_messages": arr}
    props.update(extra or {})
    return {"type": "custom", "include_sample_schema": True,
            "item_schema": {"type": "object", "properties": props, "required": ["query"]}}


# --- evaluators -------------------------------------------------------------------

def prompt_definition(text: str, fields: list[str]) -> dict:
    from azure.ai.projects.models import EvaluatorDefinitionType

    return {
        "type": EvaluatorDefinitionType.PROMPT,
        "prompt_text": text,
        "init_parameters": {"type": "object", "required": ["deployment_name", "threshold"],
                            "properties": {"deployment_name": {"type": "string"}, "threshold": {"type": "number"}}},
        "data_schema": {"type": "object", "required": fields,
                        "properties": {f: {"type": ["string", "array", "object"]} for f in fields}},
        "metrics": {"custom_prompt": {"type": "ordinal", "desirable_direction": "increase",
                                      "min_value": 1, "max_value": 5}},
    }


def code_definition(code: str) -> dict:
    from azure.ai.projects.models import EvaluatorDefinitionType

    return {
        "type": EvaluatorDefinitionType.CODE,
        "code_text": code,
        # pass_threshold must be declared a number, or the service compares a float with a string.
        "init_parameters": {"type": "object", "required": ["deployment_name", "pass_threshold"],
                            "properties": {"deployment_name": {"type": "string"},
                                           "pass_threshold": {"type": "number"}}},
        "metrics": {"result": {"type": "continuous", "desirable_direction": "increase",
                               "min_value": 0.0, "max_value": 1.0}},
        "data_schema": {"type": "object", "required": ["item"], "properties": {"item": {"type": "object"}}},
    }


def latest_version(project, name: str):
    """(version, prompt_or_code_text) of the newest version of a custom evaluator, or None."""
    try:
        versions = list(project.beta.evaluators.list_versions(name=name))
    except Exception:  # noqa: BLE001 - a name that was never registered
        return None
    if not versions:
        return None
    newest = max(versions, key=lambda v: int(v.version))
    definition = newest.as_dict().get("definition") or {}
    return str(newest.version), definition.get("code_text") or definition.get("prompt_text")


def register(force: bool) -> None:
    from azure.ai.projects.models import EvaluatorCategory

    project, _ = clients()
    pinned = {}
    for name, spec in CUSTOM.items():
        text = (EVALUATORS / spec["file"]).read_text(encoding="utf-8")
        is_prompt = "fields" in spec
        existing = latest_version(project, name)
        if existing and existing[1] == text and not force:
            pinned[name] = existing[0]
            print(f"{name:26s} v{existing[0]}  unchanged")
            continue
        definition = prompt_definition(text, spec["fields"]) if is_prompt else code_definition(text)
        created = project.beta.evaluators.create_version(name=name, evaluator_version={
            "name": name, "display_name": name, "description": spec["description"],
            "categories": [EvaluatorCategory.QUALITY], "definition": definition})
        pinned[name] = str(created.version)
        print(f"{name:26s} v{created.version}  registered")
    update_state(evaluators=pinned)
    print(f"Pinned versions saved to {STATE.name}")


def criteria(trace: str, answer: str) -> list[dict]:
    """Four built-in evaluators and four custom ones.

    trace  - the full trace: tool calls, tool outputs and the answer.
    answer - the final answer text only.
    Custom evaluators are pinned to the versions `register` recorded; an unpinned reference resolves to v1.
    """
    pinned = load_state().get("evaluators") or {}
    missing = [n for n in CUSTOM if n not in pinned]
    if missing:
        sys.exit(f"Custom evaluators not registered: {', '.join(missing)}. Run: python evaluate.py register")
    judge = {"deployment_name": env("EVAL_JUDGE_DEPLOYMENT")}
    tools, judge_query, query = "{{item.tool_definitions}}", "{{item.query_messages}}", "{{item.query}}"

    def evaluator(name, mapping, params=None, custom=False):
        c = {"type": "azure_ai_evaluator", "name": name, "evaluator_name": name if custom else f"builtin.{name}"}
        if mapping:  # code evaluators read the whole item, so they take no mapping
            c["data_mapping"] = mapping
        if custom:
            c["evaluator_version"] = pinned[name]
        if params is not None:
            c["initialization_parameters"] = params
        return c

    return [
        evaluator("task_adherence", {"query": judge_query, "response": trace, "tool_definitions": tools}, judge),
        evaluator("intent_resolution", {"query": judge_query, "response": answer, "tool_definitions": tools}, judge),
        evaluator("tool_call_accuracy", {"query": judge_query, "response": trace, "tool_definitions": tools}, judge),
        evaluator("code_vulnerability", {"query": query, "response": answer}),
        evaluator("expected_behavior",
                  {"query": query, "response": trace, "expected_behavior": "{{item.expected_behavior}}"},
                  {**judge, "threshold": 4}, custom=True),
        evaluator("tool_claims_verified", {"query": query, "response": trace},
                  {**judge, "threshold": 4}, custom=True),
        evaluator("grounded_numbers", {}, {**judge, "pass_threshold": 0.9}, custom=True),
        evaluator("answer_present", {}, {**judge, "pass_threshold": 1.0}, custom=True),
    ]


# --- eval groups and runs -----------------------------------------------------------

def eval_group(openai_client, key: str, name: str, schema: dict, testing_criteria: list[dict],
               override: str | None = None, new: bool = False) -> str:
    """Reuse the group recorded under `key`, so runs land side by side; create it on first use."""
    if override:
        return override
    existing = (load_state().get("groups") or {}).get(key)
    if existing and not new:
        return existing
    group = openai_client.evals.create(name=name, data_source_config=schema, testing_criteria=testing_criteria)
    update_state(groups={key: group.id})
    print(f"Created eval group '{name}': {group.id}")
    return group.id


def start_run(openai_client, eval_id: str, label: str, data_source: dict, metadata: dict) -> None:
    run = openai_client.evals.runs.create(eval_id=eval_id, name=label, metadata=metadata, data_source=data_source)
    update_state(runs={label: {"eval_id": eval_id, "run_id": run.id, **metadata}})
    print(f"Run '{label}' started: eval_id={eval_id} run_id={run.id}")
    wait_and_fetch(openai_client, eval_id, run.id, label)


def inline(rows: list[dict]) -> dict:
    return {"type": "file_content", "content": [{"item": r} for r in rows]}


def recorded(new_group: bool = False) -> None:
    _, oc = clients()
    rows = recorded_rows()
    eval_id = eval_group(oc, "recorded", "coding-agent - recorded traces",
                         item_schema({"response": {"type": "array"}, "response_text": {"type": "string"}}),
                         criteria("{{item.response}}", "{{item.response_text}}"), new=new_group)
    start_run(oc, eval_id, "recorded-f14-vs-honest", {"type": "jsonl", "source": inline(rows)},
              {"kind": "recorded"})


def warm(agent: str) -> None:
    """A cold hosted agent answers 424 while it starts; a target run would lose rows to that."""
    from azure.identity import DefaultAzureCredential

    sys.path.insert(0, str(AGENT_ROOT))
    import invoke  # the same client as ../invoke.py

    url = (f"{env('FOUNDRY_PROJECT_ENDPOINT').rstrip('/')}/agents/{agent}/endpoint/protocols/openai/responses"
           f"?api-version={invoke.API_VERSION}")
    token = DefaultAzureCredential().get_token("https://ai.azure.com/.default").token
    started = time.time()
    while True:
        status, payload, _ = invoke.post(url, token, WARM_PROMPT, timeout=300)
        if status == 200:
            print(f"Warm-up: {agent} answered in {time.time() - started:.0f}s")
            return
        if status == 424 and time.time() - started < 900:
            time.sleep(5)
            continue
        sys.exit(f"Warm-up failed for {agent}: HTTP {status} {str(payload)[:300]}")


def run(agent: str, label: str | None, group: str | None, new_group: bool, skip_warm: bool) -> None:
    _, oc = clients()
    label = label or agent
    if not skip_warm:
        warm(agent)
    eval_id = eval_group(oc, "smoke", "coding-agent - smoke suite", item_schema(),
                         criteria("{{sample.output_items}}", "{{sample.output_text}}"), group, new_group)
    data_source = {
        "type": "azure_ai_target_completions",
        "source": inline(smoke_rows()),
        "input_messages": {"type": "template", "template": [
            {"type": "message", "role": "user", "content": {"type": "input_text", "text": "{{item.query}}"}}]},
        "target": {"type": "azure_ai_agent", "name": agent},
    }
    start_run(oc, eval_id, label, data_source, {"kind": "agent", "agent": agent})


def sample_field(ds: dict, name: str):
    """Output items store the agent's response as flattened 'sample.<name>' keys."""
    if f"sample.{name}" in ds:
        return ds[f"sample.{name}"]
    sample = ds.get("sample")
    return sample.get(name) if isinstance(sample, dict) else None


def dedup(items: list) -> list:
    """Drop the repeated tool calls and orphan tool results an agent-target run adds to sample.output_items.

    The same call can appear alone and again inside a message that bundles several calls, so calls are
    split one per message and de-duplicated by tool_call_id.
    """
    def parts(m):
        return [p for p in m.get("content") or [] if isinstance(p, dict)] if isinstance(m.get("content"), list) else []

    call_ids = {p.get("tool_call_id") for m in items for p in parts(m) if p.get("type") == "tool_call"}
    seen, out = set(), []
    for m in items:
        calls = [p for p in parts(m) if p.get("type") == "tool_call"]
        if m.get("role") == "assistant" and calls:
            for p in calls:
                if ("call", p.get("tool_call_id")) not in seen:
                    seen.add(("call", p.get("tool_call_id")))
                    out.append({"role": "assistant", "content": [p]})
            continue
        if m.get("role") == "tool":
            if m.get("tool_call_id") not in call_ids:
                continue
            key = ("result", m.get("tool_call_id"))
        else:
            key = json.dumps({k: v for k, v in m.items() if k != "run_id"}, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


def rescore(label: str) -> None:
    """Score tool_call_accuracy again on the same traces, de-duplicated, in a dataset run (no agent calls)."""
    _, oc = clients()
    items = read_results(label)["items"]
    rows = []
    for it in sorted(items, key=lambda x: x.get("datasource_item_id") or 0):
        ds = it["datasource_item"]
        raw = sample_field(ds, "output_items") or []
        trace = dedup(raw)
        rows.append({"id": ds.get("id") or "", "query": ds["query"], "query_messages": ds["query_messages"],
                     "tool_definitions": ds["tool_definitions"], "response": trace})
        print(f"  {ds.get('id') or ds['query'][:40]}: {len(raw)} trace items -> {len(trace)} after de-duplication")
    judge = {"deployment_name": env("EVAL_JUDGE_DEPLOYMENT")}
    tca = [{"type": "azure_ai_evaluator", "name": "tool_call_accuracy", "evaluator_name": "builtin.tool_call_accuracy",
            "initialization_parameters": judge,
            "data_mapping": {"query": "{{item.query_messages}}", "response": "{{item.response}}",
                             "tool_definitions": "{{item.tool_definitions}}"}}]
    eval_id = eval_group(oc, "rescore", "coding-agent - tool_call_accuracy on de-duplicated traces",
                         item_schema({"response": {"type": "array"}}), tca)
    start_run(oc, eval_id, f"{label}-rescored", {"type": "jsonl", "source": inline(rows)},
              {"kind": "rescore", "source_label": label})


# --- results ----------------------------------------------------------------------

def wait_and_fetch(openai_client, eval_id: str, run_id: str, label: str) -> None:
    started = time.time()
    while True:
        current = openai_client.evals.runs.retrieve(run_id=run_id, eval_id=eval_id)
        if current.status in ("completed", "failed", "canceled", "cancelled"):
            break
        print(f"  {current.status} {time.time() - started:.0f}s", flush=True)
        time.sleep(20)
    print(f"STATUS={current.status} elapsed_s={time.time() - started:.0f}")
    print(f"REPORT={getattr(current, 'report_url', None)}")
    if getattr(current, "error", None):
        print(f"RUN_ERROR={current.error}")
    items = [i.model_dump() for i in openai_client.evals.runs.output_items.list(run_id=run_id, eval_id=eval_id)]
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{label}.json").write_text(
        json.dumps({"run": current.model_dump(), "items": items}, indent=1, default=str), encoding="utf-8")
    summarize(items)


def read_results(label: str) -> dict:
    path = RESULTS / f"{label}.json"
    if not path.exists():
        sys.exit(f"No results for '{label}' in {RESULTS}. Run it first, or: python evaluate.py fetch {label}")
    return json.loads(path.read_text(encoding="utf-8"))


def row_results(item: dict) -> dict:
    """evaluator name -> {score, passed, reason, error}; merges repeated entries for the same evaluator."""
    merged = {}
    for r in item.get("results") or []:
        entry = merged.setdefault(r.get("name"), {})
        for field in ("score", "passed", "reason"):
            if r.get(field) is not None:
                entry[field] = r[field]
        sample = r.get("sample")
        if isinstance(sample, dict) and sample.get("error"):
            entry["error"] = True
            entry.setdefault("reason", f"error: {sample['error']}")
    return merged


def row_verdict(results: dict) -> str:
    if not results or any(not v.get("passed") and not v.get("error") for v in results.values()):
        return "FAIL"
    return "ERROR" if any(v.get("error") for v in results.values()) else "PASS"


def summarize(items: list[dict]) -> None:
    for it in sorted(items, key=lambda x: x.get("datasource_item_id") or 0):
        ds = it.get("datasource_item") or {}
        results = row_results(it)
        print(f"\n## {ds.get('id') or (ds.get('query') or '')[:60]}  [{row_verdict(results)}]")
        for name, v in results.items():
            score = "-" if v.get("error") else v.get("score")
            score = f"{score:g}" if isinstance(score, (int, float)) else str(score)
            status = "ERROR" if v.get("error") else "pass" if v.get("passed") else "FAIL"
            reason = str(v.get("reason") or "").replace("\n", " ")[:150]
            print(f"  {name:26s} {score:>5s}  {status:5s} {reason}")
        out_items = sample_field(ds, "output_items") or ds.get("response")
        if out_items:
            local = analyze({}, {"query": ds.get("query", ""), "response": out_items,
                                 "output_text": sample_field(ds, "output_text")})
            if local["ungrounded"]:
                print(f"  (local check) numbers with no tool evidence: {local['ungrounded']}")


def compare(labels: list[str], fail_on_regression: bool) -> int:
    stats, order = {}, []
    for label in labels:
        data = read_results(label)
        per_eval, rows_passing, errors = {}, 0, 0
        for it in data["items"]:
            results = row_results(it)
            rows_passing += row_verdict(results) == "PASS"
            for name, v in results.items():
                if name not in order:
                    order.append(name)
                s = per_eval.setdefault(name, {"scores": [], "passed": 0, "n": 0, "errors": 0})
                if v.get("error"):
                    s["errors"] += 1
                    errors += 1
                    continue
                s["n"] += 1
                s["passed"] += bool(v.get("passed"))
                if isinstance(v.get("score"), (int, float)):
                    s["scores"].append(float(v["score"]))
        stats[label] = {"evals": per_eval, "rows": rows_passing, "n": len(data["items"]), "errors": errors,
                        "agent": (data["run"].get("metadata") or {}).get("agent", ""),
                        "report": data["run"].get("report_url")}

    def cell(label, name):
        s = stats[label]["evals"].get(name)
        if not s:
            return "-"
        mean = f"{sum(s['scores']) / len(s['scores']):.2f} · " if s["scores"] else ""
        err = f" · {s['errors']} error" if s["errors"] else ""
        return f"{mean}{s['passed']}/{s['n']} pass{err}" if s["n"] else f"{s['errors']} error"

    print("| Evaluator | " + " | ".join(labels) + " |")
    print("| --- | " + " | ".join("---" for _ in labels) + " |")
    print("| Rows with every check passing | " +
          " | ".join(f"**{stats[lb]['rows']} / {stats[lb]['n']}**" for lb in labels) + " |")
    for name in order:
        print(f"| `{name}` | " + " | ".join(cell(lb, name) for lb in labels) + " |")
    for label in labels:
        rescored = RESULTS / f"{label}-rescored.json"
        if rescored.exists():
            scores = [float(v["score"]) for it in json.loads(rescored.read_text(encoding="utf-8"))["items"]
                      for v in row_results(it).values()
                      if isinstance(v.get("score"), (int, float)) and not v.get("error")]
            if scores:
                print(f"\n`tool_call_accuracy` on de-duplicated traces, {label}: "
                      f"{sum(scores) / len(scores):.2f} (rows: {', '.join(f'{s:g}' for s in scores)})")
    print()
    for label in labels:
        print(f"{label}: agent={stats[label]['agent']} report={stats[label]['report']}")

    if fail_on_regression and len(labels) >= 2:
        errored = [lb for lb in labels if stats[lb]["errors"]]
        if errored:
            print(f"\nGRADING ERRORS in {', '.join(errored)}: re-run before gating on this comparison.")
            return 2
        base, cand = stats[labels[0]]["evals"], stats[labels[-1]]["evals"]
        regressed = [n for n in base if n in cand and cand[n]["passed"] < base[n]["passed"]]
        if regressed:
            print(f"\nREGRESSION: {labels[-1]} passes fewer rows than {labels[0]} on: {', '.join(regressed)}")
            return 1
        print(f"\nNo regression: {labels[-1]} passes at least as many rows as {labels[0]} on every evaluator.")
    return 0


def fetch(label: str) -> None:
    info = (load_state().get("runs") or {}).get(label)
    if not info:
        sys.exit(f"No run named '{label}' in {STATE.name}.")
    _, oc = clients()
    wait_and_fetch(oc, info["eval_id"], info["run_id"], label)


def tool_defs() -> None:
    """Needs the agent's own libraries: pip install -r ../agent/requirements.txt"""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    sys.path.insert(0, str(AGENT_ROOT / "agent"))
    import main as agent_main

    defs = []
    for name in AGENT_TOOLS:
        fn = convert_to_openai_tool(getattr(agent_main, name))["function"]
        defs.append({"name": fn["name"], "description": fn.get("description", ""), "parameters": fn["parameters"]})
    TOOL_DEFS.write_text(json.dumps(defs, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {len(defs)} tool definitions to {TOOL_DEFS.name}")


def cleanup(evaluators: bool) -> None:
    project, oc = clients()
    state = load_state()
    for key, eval_id in (state.get("groups") or {}).items():
        try:
            oc.evals.delete(eval_id)
            print(f"Deleted eval group {key}: {eval_id}")
        except Exception as exc:  # noqa: BLE001
            print(f"Could not delete eval group {key} ({eval_id}): {exc}")
    state.pop("groups", None)
    state.pop("runs", None)
    if evaluators:
        for name in CUSTOM:
            try:
                versions = list(project.beta.evaluators.list_versions(name=name))
            except Exception:  # noqa: BLE001
                versions = []
            for v in versions:
                project.beta.evaluators.delete_version(name=name, version=str(v.version))
            print(f"Deleted evaluator {name} ({len(versions)} versions)")
        state.pop("evaluators", None)
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("register", help="register or refresh the custom evaluators and pin their versions")
    p.add_argument("--force", action="store_true", help="register a new version even if the text is unchanged")
    p = sub.add_parser("recorded", help="score the two recorded traces (no agent calls)")
    p.add_argument("--new-group", action="store_true", help="start a new recorded-traces eval group")
    p = sub.add_parser("run", help="run the smoke dataset against a hosted agent")
    p.add_argument("agent")
    p.add_argument("--label", help="run name and results file name (default: the agent name)")
    p.add_argument("--group", help="an existing eval group ID to add the run to")
    p.add_argument("--new-group", action="store_true", help="start a new smoke eval group")
    p.add_argument("--no-warm", action="store_true", help="skip the warm-up call")
    p = sub.add_parser("rescore", help="re-score tool_call_accuracy on de-duplicated traces")
    p.add_argument("label")
    p = sub.add_parser("compare", help="compare runs side by side from results/")
    p.add_argument("labels", nargs="+")
    p.add_argument("--fail-on-regression", action="store_true",
                   help="exit 1 if the last run passes fewer rows than the first on any evaluator, "
                        "2 if any evaluator errored")
    p = sub.add_parser("fetch", help="download a run's results again")
    p.add_argument("label")
    sub.add_parser("tool-defs", help="regenerate tool_definitions.json from agent/main.py")
    p = sub.add_parser("cleanup", help="delete the eval groups recorded in the state file")
    p.add_argument("--evaluators", action="store_true", help="also delete every version of the custom evaluators")
    args = parser.parse_args()

    if args.command == "register":
        register(args.force)
    elif args.command == "recorded":
        recorded(args.new_group)
    elif args.command == "run":
        run(args.agent, args.label, args.group, args.new_group, args.no_warm)
    elif args.command == "rescore":
        rescore(args.label)
    elif args.command == "compare":
        return compare(args.labels, args.fail_on_regression)
    elif args.command == "fetch":
        fetch(args.label)
    elif args.command == "tool-defs":
        tool_defs()
    elif args.command == "cleanup":
        cleanup(args.evaluators)
    return 0


if __name__ == "__main__":
    sys.exit(main())

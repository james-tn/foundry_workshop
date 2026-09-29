"""Test the custom code evaluators locally, before registering them in Foundry.

    python test_graders.py        # or: python -m pytest test_graders.py

Foundry reports a failing code evaluator only as "An error occurred during
grading", so check a change here first. The main test replays every recorded
run in ../sample-output through grounded_numbers: only the run in which the
model reported a sandboxed result it never obtained
(09-unverified-tool-claim.txt, findings.md F14) should score below 1.0.

invoke.py records at most 400 characters of tool arguments and 1,600 of tool
output. A number that only appears past that cut, such as a parameter the model
passed to test_method, looks ungrounded in the recording although the full
trace in Foundry contains it. Those runs are listed but not asserted on.
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SAMPLES = HERE.parent / "sample-output"
sys.path.insert(0, str(HERE / "evaluators"))

import answer_present  # noqa: E402
import grounded_numbers  # noqa: E402

FABRICATED = "09-unverified-tool-claim.txt"
PASS_THRESHOLD = 0.9
ARGS_CUT, OUTPUT_CUT = 400, 1600  # what invoke.py prints of each tool call


def recorded_runs():
    """(file, run number, query, Responses-style trace, recorded in full?) for every run in sample-output."""
    for path in sorted(SAMPLES.rglob("0*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for n, chunk in enumerate(re.split(r"(?m)^AGENT=", text)[1:], 1):
            if "--- OUTPUT ---" not in chunk:
                continue
            query = (re.search(r"(?m)^PROMPT=(.*)$", chunk) or [None, ""])[1]
            items, complete = [], True
            for line in chunk.splitlines():
                if line.startswith("TOOL_CALL "):
                    m = re.match(r"TOOL_CALL name=(\S+) args=(.*)", line)
                    complete &= len(m[2]) < ARGS_CUT
                    items.append({"type": "function_call", "name": m[1], "arguments": m[2]})
                elif line.startswith("TOOL_OUTPUT="):
                    output = line[len("TOOL_OUTPUT="):]
                    complete &= len(output) < OUTPUT_CUT
                    items.append({"type": "function_call_output", "output": output})
            answer = chunk.split("--- OUTPUT ---", 1)[1].split("RESULT=")[0]
            items.append({"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": answer}]})
            yield path, n, query, items, complete


def trace(answer: str, tool_output: str = "", query: str = "q") -> dict:
    items = [{"type": "function_call_output", "output": tool_output}] if tool_output else []
    items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": answer}]})
    return {"query": query, "sample": {"output_items": items}}


def test_recorded_runs_only_the_fabrication_fails():
    rows = [r for r in recorded_runs() if r[4] or r[0].name == FABRICATED]
    assert len(rows) >= 3, f"too few fully recorded runs under {SAMPLES}"
    failing = []
    for path, n, query, items, _ in rows:
        item = {"query": query, "sample": {"output_items": items}}
        score = grounded_numbers.grade({}, item)
        if score < PASS_THRESHOLD:
            failing.append((path.name, n, score, grounded_numbers.analyze({}, item)["ungrounded"]))
    assert [(f[0], f[3]) for f in failing] == [(FABRICATED, ["3512.03"])], failing


def test_rounding_and_units_are_grounded():
    assert grounded_numbers.grade({}, trace("The mean is 3.758.", '{"value": 3.7579166667}')) == 1.0
    assert grounded_numbers.grade({}, trace("It took 1.25 s.", '{"elapsed_ms": 1250}')) == 1.0


def test_invented_number_is_not_grounded():
    assert grounded_numbers.grade({}, trace("The mean is 4.2 and the max is 17.5.", '{"mean": 4.2}')) == 0.5


def test_identifiers_code_and_small_counts_are_not_claims():
    answer = ("Tested on TURBINE-014 with gpt-4.1 in 2 runs.\n"
              "```python\ndef f(this):\n    return 123.456\n```\n")
    assert grounded_numbers.analyze({}, trace(answer))["claims"] == []


def test_chat_format_trace_is_read():
    item = {"query": "q", "response": [
        {"role": "assistant", "content": [{"type": "tool_call", "tool_call_id": "c1", "name": "t", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "tool_result", "tool_result": '{"v": 812.5}'}]},
        {"role": "assistant", "content": "The value is 812.5, not 999.1."},
    ]}
    assert grounded_numbers.grade({}, item) == 0.5


def test_grader_never_raises():
    assert grounded_numbers.grade(None, None) == 1.0
    assert grounded_numbers.grade({}, {"sample": {"output_items": "not json"}}) == 1.0


def test_answer_present():
    assert answer_present.grade({}, {"sample": {"output_text": "done"}}) == 1.0
    assert answer_present.grade({}, {"sample": {"output_text": ""}}) == 0.0
    assert answer_present.grade({}, {"response": [{"role": "assistant", "content": "ok"}]}) == 1.0
    assert answer_present.grade(None, {}) == 0.0


def test_dedup_matches_calls_by_id():
    # The shape an agent-target run returned for gpt-4.1-mini: each call alone, then both calls again in
    # one message, results with IDs that match no call, and every real result twice.
    import evaluate

    def call(cid, mode):
        return {"type": "tool_call", "tool_call_id": cid, "name": "describe_execution_context",
                "arguments": {"mode": mode}}

    def result(cid):
        return {"role": "tool", "tool_call_id": cid, "content": [{"type": "tool_result", "tool_result": {}}]}

    items = [
        {"role": "assistant", "content": [call("a", "in_process")]},
        {"role": "assistant", "content": [call("b", "sandboxed")]},
        result("orphan-1"), result("orphan-2"),
        {"role": "assistant", "content": [call("a", "in_process"), call("b", "sandboxed")]},
        result("a"), result("b"), result("a"), result("b"),
        {"role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
    ]
    out = evaluate.dedup(items)
    calls = [p["tool_call_id"] for m in out for p in m["content"] if p.get("type") == "tool_call"]
    results = [m["tool_call_id"] for m in out if m.get("role") == "tool"]
    assert (calls, results, len(out)) == (["a", "b"], ["a", "b"], 5), out


def main() -> int:
    for path, n, query, items, complete in recorded_runs():
        item = {"query": query, "sample": {"output_items": items}}
        result = grounded_numbers.analyze({}, item)
        score = grounded_numbers.grade({}, item)
        where = path.relative_to(SAMPLES).as_posix()
        note = "" if complete else "  (tool calls cut in the recording)"
        print(f"{score:.2f}  {where:44s} run {n}  claims={len(result['claims']):2d}  "
              f"ungrounded={result['ungrounded'][:6]}{note}")
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

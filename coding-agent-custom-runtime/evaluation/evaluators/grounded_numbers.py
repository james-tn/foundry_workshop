# Custom code evaluator: grounded_numbers.
#
# Score = share of the numbers stated in the final answer that also appear in a
# tool input or tool output of the same trace (or in the query). 1.0 means every
# number is backed by evidence; a made-up measurement lowers the score.
#
# Foundry runs this file in a restricted Python sandbox. Keep it that way:
#   - no compile() and no re.compile(): pass pattern strings to re.* instead;
#   - no dunder attribute access (for example, type(e) followed by its name);
#   - no network or file access.
# A violation surfaces only as "An error occurred during grading", so grade()
# catches everything and returns 0.0 instead. Test changes locally first with
# test_graders.py.

import json
import re

CODE_FENCE = r"(?s)```([A-Za-z0-9_+-]*)\n(.*?)```"
CODE_LANGS = {"python", "py", "type", "js", "javascript", "ts", "typescript", "java", "bash", "sh", "powershell"}
NUMBER = r"\d+(?:\.\d+)?"


def _as_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return []
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else []


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _collect(items):
    """Split a trace into evidence (tool inputs and outputs) and answer text.

    Accepts Responses API items and chat-style messages with tool_call and
    tool_result parts, which is what an agent-target run produces.
    """
    evidence, answer = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        kind, role = it.get("type"), it.get("role")
        if kind == "function_call":
            evidence.append(_text(it.get("arguments", "")))
        elif kind == "function_call_output":
            evidence.append(_text(it.get("output", "")))
        elif role == "tool":
            evidence.append(_text(it.get("content", "")))
        elif kind == "message" or role == "assistant":
            content = it.get("content")
            if isinstance(content, str):
                answer.append(content)
                continue
            for part in content or []:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype in ("output_text", "text"):
                    answer.append(part.get("text", ""))
                elif ptype in ("tool_call", "function_call"):
                    evidence.append(_text(part.get("arguments", "")))
                elif ptype in ("tool_result", "function_call_output"):
                    evidence.append(_text(part.get("tool_result", part.get("output", ""))))
    return "\n".join(evidence), "\n".join(answer)


def _strip_code(text):
    """Drop code blocks: numbers inside generated code are not claims."""
    def keep(match):
        lang, body = match.group(1).lower(), match.group(2)
        if lang in CODE_LANGS or "def " in body or ": method(" in body:
            return " "
        return body
    return re.sub(CODE_FENCE, keep, text)


def _claims(text):
    """Numbers stated as facts. Skips identifiers (TURBINE-014, gpt-4.1) and small counts."""
    found = []
    for m in re.finditer(NUMBER, text):
        start, token = m.start(), m.group(0)
        before = text[start - 1] if start else " "
        before2 = text[start - 2] if start > 1 else " "
        after = text[m.end()] if m.end() < len(text) else " "
        if before.isalnum() or before in "._" or after.isalpha() and after not in "smx%":
            continue
        if before == "-" and before2.isalnum():
            continue
        value = float(token)
        if "." not in token and value <= 10:
            continue
        found.append(token)
    return found


def _grounded(token, pool):
    """True if the number matches evidence, allowing rounding and a x1000 unit change (s and ms)."""
    value = float(token)
    decimals = len(token.split(".")[1]) if "." in token else 0
    tolerance = 0.5 * 10 ** (-decimals) + 1e-9
    for e in pool:
        for candidate in (e, e / 1000.0, e * 1000.0):
            if abs(candidate - value) <= tolerance:
                return True
    return False


def analyze(sample, item):
    sample = sample if isinstance(sample, dict) else {}
    item = item if isinstance(item, dict) else {}
    target = item.get("sample") if isinstance(item.get("sample"), dict) else sample
    items = _as_list(target.get("output_items")) or _as_list(item.get("output_items")) or _as_list(item.get("response"))
    evidence, answer = _collect(items)
    if not answer.strip():
        answer = target.get("output_text") or item.get("output_text") or ""
    query = _text(item.get("query", ""))
    pool = [float(n) for n in re.findall(NUMBER, evidence + "\n" + query)]
    claims = _claims(_strip_code(answer))
    ungrounded = [c for c in claims if not _grounded(c, pool)]
    return {"claims": claims, "ungrounded": ungrounded, "tool_items": len(items), "answer_chars": len(answer)}


def score(sample, item):
    result = analyze(sample, item)
    if not result["claims"]:
        return 1.0
    return round(1.0 - len(result["ungrounded"]) / len(result["claims"]), 4)


def grade(sample, item) -> float:
    try:
        return score(sample, item)
    except BaseException:
        return 0.0

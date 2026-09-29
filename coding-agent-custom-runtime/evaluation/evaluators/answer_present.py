# Custom code evaluator: answer_present.
#
# 1.0 if the agent returned a non-empty final answer, else 0.0. Cheap, and it
# catches a model swap that changes the response shape, such as an answer
# returned only in the reasoning summary (findings.md, F16).


def grade(sample, item) -> float:
    try:
        s = item.get("sample") if isinstance(item.get("sample"), dict) else (sample or {})
        text = s.get("output_text") or item.get("output_text") or ""
        if not text:
            resp = item.get("response")
            text = resp if isinstance(resp, str) else ""
            if isinstance(resp, list):
                for m in resp:
                    if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), str):
                        text += m["content"]
        return 1.0 if text.strip() else 0.0
    except BaseException:
        return 0.0

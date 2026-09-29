
import json
import csv
import os
from pathlib import Path

# ---- Configuration ---------------------------------------------------------
RESULTS_FILE = "evaluation_results.json"                   # your results file produced by evaluate.py
OUT_JSONL    = "test_dataset_with_response.jsonl"         # output for portal upload
OUT_CSV      = "test_dataset_with_response.csv"           # optional CSV
# If you also have references and want to include them, set this True and provide a function to extract ground_truth.
INCLUDE_GROUND_TRUTH = False
# ---------------------------------------------------------------------------

def safe_get(d, path):
    """
    Walk a dotted path (supports simple list index for 'messages[0]....').
    Returns None if any segment is missing.
    """
    cur = d
    for part in path.split('.'):
        if isinstance(cur, list):
            if '[' in part and ']' in part:
                name = part.split('[')[0]
                idx = int(part.split('[')[1].split(']')[0])
                if name:  # e.g., messages[0]
                    # when name is present, switch to dict[name]; else treat current list
                    cur = cur if name == "" else (cur if name == "messages" else None)
                if cur is None or not isinstance(cur, list) or idx >= len(cur):
                    return None
                cur = cur[idx]
            else:
                # cannot descend into list without explicit index
                return None
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
            if cur is None:
                return None
    return cur

def extract_prompt(case: dict):
    """
    Try multiple keys to locate the original prompt in evaluation_results.json.
    """
    for key in ["question", "prompt", "input", "query", "text",
                "test.question", "test.prompt", "test.input",
                "messages[0].content", "messages[0].text"]:
        val = safe_get(case, key) if ("." in key or "[" in key) else case.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None

def extract_response(case: dict):
    """
    Try multiple keys to locate the model/assistant output recorded in evaluation_results.json.
    This depends on how your evaluate.py serialized results.
    """
    candidates = [
        "response", "output", "answer",
        "sample.output_text",                   # some evaluators store this
        "assistant", "assistant_output",
        "messages[-1].content",                 # last assistant message if stored
        "result.output_text",
    ]
    # Try direct candidates
    for key in candidates:
        val = safe_get(case, key) if ("." in key or "[" in key) else case.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # Fallback: scan for a text blob under typical containers
    for container_key in ["sample", "result", "evaluation", "assistant", "output"]:
        container = case.get(container_key, {})
        if isinstance(container, dict):
            for k, v in container.items():
                if isinstance(v, str) and len(v.strip()) > 0:
                    return v.strip()
    return ""

def extract_ground_truth(case: dict):
    """
    Optional: retrieve a human/reference answer if present in results structure.
    Disable by setting INCLUDE_GROUND_TRUTH=False.
    """
    if not INCLUDE_GROUND_TRUTH:
        return None
    for key in ["ground_truth", "reference", "expected", "gold",
                "test.ground_truth", "test.reference"]:
        val = safe_get(case, key) if ("." in key) else case.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None

def gather_cases(data):
    """
    Normalize results to a list of case dictionaries.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # common containers in evaluate.py output
        for k in ["results", "cases", "items"]:
            if k in data and isinstance(data[k], list):
                return data[k]
        if "evaluation" in data and isinstance(data["evaluation"], dict):
            cases = data["evaluation"].get("cases")
            if isinstance(cases, list):
                return cases
    # fallback: treat the dict as a single case
    return [data]

def convert(results_path: Path, out_jsonl: Path, out_csv: Path):
    with results_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    cases = gather_cases(data)
    if not cases:
        raise ValueError("No cases found in evaluation_results.json")

    jsonl_lines = []
    csv_rows = []

    for idx, case in enumerate(cases, start=1):
        case_id = (case.get("id")
                   or safe_get(case, "test.id")
                   or safe_get(case, "metadata.id")
                   or f"case_{idx}")

        prompt = extract_prompt(case) or f"[Missing prompt for {case_id}]"
        response = extract_response(case)  # may be empty string if not found
        gt = extract_ground_truth(case)

        rec = {"id": case_id, "prompt": prompt, "response": response}
        if gt is not None:
            rec["ground_truth"] = gt

        jsonl_lines.append(rec)
        csv_rows.append({
            "id": case_id,
            "prompt": prompt,
            "response": response,
            **({"ground_truth": gt} if gt is not None else {})
        })

    # Write JSONL
    with out_jsonl.open("w", encoding="utf-8") as jf:
        for rec in jsonl_lines:
            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # Write CSV
    fieldnames = ["id", "prompt", "response"] + (["ground_truth"] if INCLUDE_GROUND_TRUTH else [])
    with out_csv.open("w", newline="", encoding="utf-8") as cf:
        writer = csv.DictWriter(cf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)

    return len(jsonl_lines)

if __name__ == "__main__":
    here = Path(os.getcwd())
    results_path = here / RESULTS_FILE
    out_jsonl = here / OUT_JSONL
    out_csv = here / OUT_CSV

    if not results_path.exists():
        raise FileNotFoundError(f"Cannot find {RESULTS_FILE} in {here}")

    count = convert(results_path, out_jsonl, out_csv)
    print(f"✅ Converted {count} test cases")
    print(f"📄 JSONL written to: {out_jsonl}")
    print(f"📄 CSV written to:    {out_csv}")
    print("ℹ️  Upload one of these in Foundry (Evaluation → Configure)")

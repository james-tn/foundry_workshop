# Sample output

Recorded console output from a live run of every scenario in the main
[`README.md`](../README.md), captured on 2026-09-28 against a public-endpoint
Foundry project in East US 2. The main set uses the open-weight
**Kimi-K2.7-Code** model. The same scenarios recorded with `gpt-4.1-mini` are in
[`gpt-4.1-mini/`](gpt-4.1-mini/) as a baseline for comparison.

Use these to compare with your own run, or as a fallback if a live call fails
during a demo. The model's own choices, such as method names and exact code,
vary from run to run. The structure should match: which tools are called, in
which runtime, and whether the tests pass.

| File | Scenario | Produced by |
| --- | --- | --- |
| [`01-runtime-image-build.txt`](01-runtime-image-build.txt) | Building the custom runtime image, including the `pip check` finding and the 670-package lock | `az acr build … sandbox` ([`sandbox/README.md`](../sandbox/README.md), step 4) |
| [`02-runtime-probe.txt`](02-runtime-probe.txt) | 1 · What is inside each runtime | `python sandbox\probe_runtime.py py-generic` and `py-contoso-analytics` |
| [`03-coding-run-no-skill.txt`](03-coding-run-no-skill.txt) | 2 · The agent writes and tests a method, with no skill | `python invoke.py "Add an anomaly-detection method to the WindTurbine type…"` |
| [`04-in-process-vs-sandbox.txt`](04-in-process-vs-sandbox.txt) | 3 · The same probe, in-process and sandboxed (3 runs) | `python invoke.py "Call describe_execution_context with mode='in_process'…"` |
| [`05-skill-v1-published.txt`](05-skill-v1-published.txt) | 4a · Publish skill v1, then the same prompt | `python skills.py publish skills/anomaly-method-standard/v1`, then `invoke.py` |
| [`06-skill-v2-published.txt`](06-skill-v2-published.txt) | 4b · Publish skill v2, then the same prompt | `python skills.py publish skills/anomaly-method-standard/v2`, then `invoke.py` |
| [`07-skill-rollback.txt`](07-skill-rollback.txt) | 4c · Roll the default back to v1 | `python skills.py rollback anomaly-method-standard 1`, then `invoke.py` |
| [`08-skill-routing.txt`](08-skill-routing.txt) | 5 · Prompts that should and should not load the skill | three `invoke.py` prompts with v1 published |
| [`09-unverified-tool-claim.txt`](09-unverified-tool-claim.txt) | 3 · A run in which the model reported a sandboxed result it never measured | the Scenario 3 prompt; see [finding F14](../findings.md#f14--a-model-can-report-a-tool-result-it-never-obtained--sharp-edge) |

Notes on reading them:

- `TOOL_CALL` and `TOOL_OUTPUT` lines are printed by [`invoke.py`](../invoke.py)
  for every tool the agent called, in order. `TURN_ELAPSED_S` is the wall
  clock for the whole turn.
- The Kimi recordings were made with a second agent, `coding-agent-kimi`,
  deployed from the same code next to the `gpt-4.1-mini` one
  (`coding-agent`). Only `AGENT_NAME` and
  `AZURE_AI_MODEL_DEPLOYMENT_NAME` differ; see
  [Choosing the model](../README.md#choosing-the-model).
- Kimi-K2.7-Code is a reasoning model. Some answers start with a sentence
  about what it will do next; that is its own commentary between tool calls.
- `_credential_source` in a platform result shows how the key was obtained,
  for example `foundry_connection:contoso-backend`.
- `runtime_info` in a test result is reported by the runtime itself: its name,
  its declared libraries and the lock hash written at build time.
- `sandbox_error: "HTTP 429"` in `05-skill-v1-published.txt` means every
  session in the pool was in use; see
  [finding F15](../findings.md#f15--every-sandboxed-call-holds-a-session-until-its-cooldown-ends--sharp-edge).
- In these recordings the managed pool is named `pool-warm`. The setup steps
  name it `pool-py-generic`; any name works.
- In `01-runtime-image-build.txt`, the registry name is replaced with
  `<registry>`, and layer download and push progress lines are removed.

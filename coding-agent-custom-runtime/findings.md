# Findings — what does not work today, and what it costs

These are the gaps, sharp edges and costs we hit while building this sample.
Each one was reproduced against a live environment, not read in a doc. Where
the sample output shows it, the file is linked.

**Status:** **Blocking** changes an architecture decision. **Sharp edge**
works, but will cost you a day if you do not know about it. **Noted** is worth
knowing.

| ID | Area | Finding | Status |
| --- | --- | --- | --- |
| [F1](#f1--mcp-cannot-be-enabled-on-a-custom-container-session-pool--blocking) | Execution | MCP cannot be enabled on a custom-container session pool | Blocking |
| [F2](#f2--sandbox-egress-is-a-pool-setting-independent-of-your-foundry-network-posture--sharp-edge) | Execution | Sandbox egress is a pool setting, independent of your Foundry network posture | Sharp edge |
| [F3](#f3--a-custom-container-pool-is-not-reachable-at-the-managed-pools-url--sharp-edge) | Execution | A custom-container pool is not reachable at the managed pools' URL | Sharp edge |
| [F4](#f4--a-custom-runtime-inherits-its-base-images-dependency-graph--noted) | Execution | A custom runtime inherits its base image's dependency graph | Noted |
| [F5](#f5--a-hosted-agent-deploys-with-no-role-assignments-and-project-scope-grants-are-not-enough--sharp-edge) | Identity | A hosted agent deploys with no role assignments, and project-scope grants are not enough | Sharp edge |
| [F6](#f6--the-blueprint-identity-cannot-be-used-in-a-role-assignment--sharp-edge) | Identity | The blueprint identity cannot be used in a role assignment | Sharp edge |
| [F7](#f7--connection-reads-intermittently-return-permissiondenied--sharp-edge) | Identity | Connection reads intermittently return `PermissionDenied` | Sharp edge |
| [F8](#f8--publishing-a-skill-changes-production-behaviour-with-no-deploy-gate--sharp-edge) | Skills | Publishing a skill changes production behaviour, with no deploy gate | Sharp edge |
| [F9](#f9--skill-routing-is-a-model-judgement-and-small-models-over-trigger--sharp-edge) | Skills | Skill routing is a model judgement, and small models over-trigger | Sharp edge |
| [F10](#f10--skills-and-hosted-agent-facts-you-will-need-on-day-one--noted) | Skills, hosting | Skills and hosted-agent facts you will need on day one | Noted |
| [F11](#f11--langgraph-silently-replaces-a-tool-parameter-named-runtime--sharp-edge) | Harness | LangGraph silently replaces a tool parameter named `runtime` | Sharp edge |
| [F12](#f12--the-hosted-runtime-is-python-only-in-practice--noted) | Hosting | The hosted runtime is Python-only in practice | Noted |
| [F13](#f13--agent-deployment-is-a-data-plane-operation--noted) | Hosting | Agent deployment is a data-plane operation | Noted |
| [F14](#f14--a-model-can-report-a-tool-result-it-never-obtained--sharp-edge) | Model | A model can report a tool result it never obtained | Sharp edge |
| [F15](#f15--every-sandboxed-call-holds-a-session-until-its-cooldown-ends--sharp-edge) | Execution | Every sandboxed call holds a session until its cooldown ends | Sharp edge |
| [F16](#f16--a-model-endpoint-can-return-the-answer-in-the-reasoning-summary--noted) | Model | A model endpoint can return the answer in the reasoning summary | Noted |
| [F17](#f17--an-agent-target-run-records-every-tool-call-twice--sharp-edge) | Evaluation | An agent-target run records every tool call twice | Sharp edge |
| [F18](#f18--a-code-grader-fails-with-a-generic-error--sharp-edge) | Evaluation | A code grader fails with a generic error | Sharp edge |
| [F19](#f19--an-unpinned-evaluator-reference-runs-version-1--sharp-edge) | Evaluation | An unpinned evaluator reference runs version 1 | Sharp edge |

Also here: [measured costs](#measured-costs-for-planning) and
[how to rank these for your deployment](#how-to-rank-these-for-your-deployment).

---

## Execution

### F1 — MCP cannot be enabled on a custom-container session pool · **Blocking**

If you want both custom container images *and* MCP tool surfaces in the
sandbox, today you have to choose one.

**Reproduction:** create a session pool with `containerType=CustomContainer`
and an `mcpServerSettings` block. The pool provisions `Succeeded`, with no
error and no warning, and `mcpServerSettings` stays `null`. Calling
`fetchMCPServerCredentials` then returns:

```
SessionMCPServerNotEnabled
```

The same call against a `PythonLTS` pool works. Pin API version
`2025-10-02-preview`. Note that `mcpServerSettings` is marked `@removed` in the
GA version `2026-01-01`, so this surface is moving.

**Why it bites:** the failure is silent at provisioning time. You find out at
tool-call time.

**Workaround today:** keep the MCP surface on a `PythonLTS` pool, and call the
custom-container pool from the application's tool layer instead of from MCP.
That is what this sample does. You lose a layer of indirection, not a capability.

**Decision to make:** which matters more in the next two quarters, custom base
images or MCP-native tools? The answer changes the pool topology.

### F2 — Sandbox egress is a pool setting, independent of your Foundry network posture · **Sharp edge**

Both pools in this sample are explicitly configured:

```json
"sessionNetworkConfiguration": { "status": "EgressDisabled", "outboundVnetSubnetId": null }
```

Code inside them could reach **nothing**: neither the public internet nor the
Azure control plane
([`04-in-process-vs-sandbox.txt`](sample-output/04-in-process-vs-sandbox.txt)).

**The trap:** with `EgressEnabled`, sandboxed code has unrestricted outbound
internet access *even when the Foundry account has
`publicNetworkAccess=Disabled`*. The two settings are unrelated. It is easy to
lock down the Foundry account, assume the sandbox inherited that posture, and
be wrong.

**Recommendation:** if customer-authored code runs in these pools, make
`EgressDisabled` the default and require a review for any exception. Check it
per pool; do not assume it.

### F3 — A custom-container pool is not reachable at the managed pools' URL · **Sharp edge**

Managed pools (`PythonLTS`) are called at the regional endpoint:

```
https://<region>.dynamicsessions.io/subscriptions/<sub>/resourceGroups/<rg>/sessionPools/<pool>/executions?...
```

The same URL form against a `CustomContainer` pool returns:

```
DynamicApisNotAllowed: the pool is not dynamic
```

A custom-container pool is only reachable at its own
**`poolManagementEndpoint`**, which is
`https://<pool>.<environment-domain>.azurecontainerapps.io`. The path, the
`api-version=2025-02-02-preview` and the token audience
(`https://dynamicsessions.io/.default`) are unchanged. So is the
`Azure ContainerApps Session Executor` role check.

**Why it bites:** one registry of runtimes now holds two URL shapes, and the
error message does not tell you which one to use.

**Fix:** read `poolManagementEndpoint` from the pool (our
[`sandbox/pool.bicep`](sandbox/pool.bicep) outputs it) and store the full URL
per runtime. Do not build URLs from the pool name. See
[`env.example.ps1`](env.example.ps1).

### F4 — A custom runtime inherits its base image's dependency graph · **Noted**

The `py-contoso-analytics` image is built `FROM` the code-interpreter image, so the
agent can call it with the same `/executions` API as a managed pool. That base
already ships about **670** Python distributions. Freezing our six declared
libraries surfaced a conflict with a package we never asked for
([`01-runtime-image-build.txt`](sample-output/01-runtime-image-build.txt)):

```
gensim 4.3.3 has requirement scipy<1.14.0,>=1.7.0, but you have scipy 1.14.1.
```

Other costs we measured:

- **Build:** 26 minutes end to end. About 11 minutes was the build, dominated
  by pulling the base image, and about 15 minutes was pushing to a **Basic**
  registry.
- **Pool provisioning:** 1,196 s (about 20 minutes) for the Bicep deployment.
  Ready sessions appeared at about minute 15. A separate test of a similar pool
  took about 9 minutes, so treat the time as variable.

Neither matters per session. Both matter for a CI loop that rebuilds a runtime
on every library change.

**Ghost metadata, a second inherited problem.** The manifest upgrades three
packages the base image already has: pandas, scipy and scikit-learn. In the
running session, `importlib.metadata.version()` returns **`None`** for all
three, although the imports load the right versions (2.2.3, 1.14.1 and 1.5.2;
[`02-runtime-probe.txt`](sample-output/02-runtime-probe.txt)). The upgrade
deletes the base's `*.dist-info` folders in a new image layer. The session
filesystem still lists them, but cannot open them. Anything that reads package
metadata then gets the wrong answer, including `pip list`, SBOM and licence
scanners, and libraries that check their dependencies' versions at import time.
`probe_runtime.py` works around it by reporting `__version__`.

**Options:**

1. Pin to the base image's versions where you can. We pinned numpy to 1.26.4
   for this reason. Doing the same for pandas, scipy and scikit-learn would
   also remove the ghost metadata.
2. Accept conflicts in packages that no method claims. The freeze step makes
   them visible.
3. Install the runtime into its own prefix or virtual environment, so nothing
   in the base is upgraded in place.
4. Build from a slim base and serve `/executions` yourself. You then own the
   execution server.

A Premium registry, or one in the same region with geo-replication, shortens
the push.

---

## Identity

### F5 — A hosted agent deploys with no role assignments, and project-scope grants are not enough · **Sharp edge**

A freshly deployed hosted agent has **no role assignments at all**. Its first
call to a session pool returns:

```
{"execution_mode": "aca_dynamic_session", "error": "HTTP 403", "detail": ""}
```

Nothing surfaces this at deploy time; the deploy reports success.

**Fix for the session pool:** grant the agent's **`instance_identity`** the
role, scoped to the pool:

```powershell
az role assignment create --assignee-object-id <instance_identity.principal_id> `
  --assignee-principal-type ServicePrincipal `
  --role "Azure ContainerApps Session Executor" --scope <session pool resource id>
```

It propagated in under 45 seconds.

#### The part that costs real time: reading a connection secret

Calling `connections.get(..., include_credentials=True)` from inside the agent
failed with a message that names no role and no resource:

```
(PermissionDenied) Principal does not have access to API/Operation.
```

The same call **succeeded as an interactive user**, which showed that the code
was right and the identity was not. What eventually fixed it:

| Role | Scope | Sufficient alone? |
| --- | --- | --- |
| `Azure AI Developer` | **project** | No |
| `Foundry User` | **project** | No |
| `Azure AI Developer` | **account** | — |
| `Foundry User` | **account** | — |
| `Cognitive Services User` | **account** | — |

Grants at the *project* scope were not sufficient. The data-plane
authorization check is satisfied at the **account** scope
(`Microsoft.CognitiveServices/accounts/<name>`). We granted the three
account-scope roles at once and did not narrow them to the single minimal
role. A least-privilege answer still needs that bisection.

Allow **up to 5 minutes** for these data-plane roles to propagate. That is much
slower than the session-pool grant.

This is arguably correct behaviour: deny by default is right. It is listed
because it is undocumented, the error names nothing actionable, and the scope
that works is not the scope you would try first.

### F6 — The blueprint identity cannot be used in a role assignment · **Sharp edge**

A hosted agent exposes **two** principals, and they are not interchangeable:

| Field | Usable in RBAC |
| --- | --- |
| `instance_identity.principal_id` | **Yes** |
| `blueprint.principal_id` | **No** |

Granting a role to the blueprint principal fails outright:

```
(PrincipalTypeNotSupported) Principals of type
#microsoft.graph.agentIdentityBlueprintPrincipal cannot validly be used in role assignments.
```

**Why it bites:** both appear next to each other in the agent version document,
with no indication of which one you want. Picking the wrong one costs you a
debugging cycle against a 403 that looks identical to F5.

[`show_agent.py`](show_agent.py) prints both and labels the one to grant.

### F7 — Connection reads intermittently return `PermissionDenied` · **Sharp edge**

With all the F5 roles in place and propagated, and with the same agent version
succeeding minutes earlier, `connections.get(include_credentials=True)` still
returned `(PermissionDenied) Principal does not have access to API/Operation`
on **2 of about 12** requests during testing (1 of 6 in a controlled batch).
The next request from the same agent version succeeded. A later batch of 10
had no failures.

We did not find the cause. The same instance identity is used throughout, so
it does not look like F6. It may be caching or replication in the data-plane
authorization path.

**What the sample does:** `_resolve_backend_key` in
[`agent/main.py`](agent/main.py) retries up to 4 times (0.5, 1.5 and 3 s back-off)
**only** on `PermissionDenied`. Any retry, and the token's `oid` for the failed
attempts, is reported in `_credential_source` rather than hidden.

**Recommendation:** any code path that reads connection secrets at call time
needs the same retry, or a short-lived cache. Do not treat a single
`PermissionDenied` as a real authorization failure.

---

## Skills

### F8 — Publishing a skill changes production behaviour, with no deploy gate · **Sharp edge**

Scenario 4 is also the risk. The same prompt to the same running agent version
produced three different methods, with no build, no deploy and no restart:

| Skill state | Method the agent wrote | TURBINE-014 | Sample output |
| --- | --- | --- | --- |
| none published | `detect_vibration_anomalies`, with the model's own choice of `ruptures` and penalty | change points `[30, 35, 40]` | [`03`](sample-output/03-coding-run-no-skill.txt) |
| v1 published | `detectAnomaly`, robust z-score | `isAnomaly: false` | [`05`](sample-output/05-skill-v1-published.txt) |
| v2 published | `detectAnomaly`, PELT change point | `isAnomaly: true`, shift +4.61 at index 32 | [`06`](sample-output/06-skill-v2-published.txt) |
| default rolled back to v1 | robust z-score again | `isAnomaly: false` | [`07`](sample-output/07-skill-rollback.txt) |

For a coding agent, that is a code-generation standard changing under every
developer at once. The agent loads the *default* version on every call, and
`create(..., default=True)` moves the default at publish time.

Publishing needs `Foundry User` on the project. That is the same role you would
give most people who build or test agents. **Nothing in this path requires a
review or an evaluation before a new default takes effect.**

What exists today:

- **Versions are immutable, and the default is a pointer.**
  `update(name, default_version=...)` rolls back in one call
  ([`07-skill-rollback.txt`](sample-output/07-skill-rollback.txt)).
- **The agent side has an allow-list.** This agent only indexes the skills
  named in `SKILL_NAMES`, which is deploy-time configuration. A new skill cannot
  inject itself, but a new *version* of an allow-listed skill can.
- **You can pin a version.** An agent could load a pinned version
  (`download_version`) instead of the default. You then give up "no redeploy",
  deliberately.

**Decision to make:** who can publish skills, and should publishing a new
default be gated behind an evaluation run?
[`evaluation/`](evaluation/README.md) shows such a run: its anomaly task checks
that the agent followed the published skill, and `compare --fail-on-regression`
turns two runs into a pass or fail for a pipeline.

### F9 — Skill routing is a model judgement, and small models over-trigger · **Sharp edge**

The model decides whether a request matches a skill description. The skill in
this sample describes an *artifact*: *"Use when the user asks to add, write or
change an anomaly-detection method on a platform Type."* It routed correctly on one
run each of three prompts, with both Kimi-K2.7-Code
([`08-skill-routing.txt`](sample-output/08-skill-routing.txt)) and
gpt-4.1-mini
([`gpt-4.1-mini/08-skill-routing.txt`](sample-output/gpt-4.1-mini/08-skill-routing.txt)):

| Prompt | Should load? | Loaded? |
| --- | --- | --- |
| "What methods does the WindTurbine type have, and which runtime do they claim?" | no | no |
| "Add a method to WindTurbine that returns the mean vibration, and test it…" | no (a method, but not anomaly detection) | no |
| "Add an anomaly-detection method to the **Compressor** type and test it…" | yes (a Type the skill never names) | **yes**, and it applied the standard |

**A contrasting test.** A skill described by an *activity* (asset triage) was
tested with gpt-4.1-mini on a prompt that was *not* a triage request:
*"compute its vibration trend and tell me if it has crossed threshold"*.

| Description and system prompt | Skill loaded for the non-triage prompt |
| --- | --- |
| Broad ("triage, assess, diagnose or prioritise…") + "your FIRST tool call MUST be load_skill" | 4 / 5 |
| Narrowed ("Use only when the user asks to triage… Not for general analysis, trend or threshold questions") + the same prompt | 3 / 3 |
| Narrowed again ("…explicitly asks to triage… Do not use for trend, slope, threshold…") + "if the request is not what a description is for, do NOT call load_skill" | 3 / 3 |

In the other direction, a soft instruction ("call load_skill first if a
request matches") **under**-triggered: a plain *"Triage COMPRESSOR-07."* was
answered in prose without loading the skill (one run). That is why this
sample's system prompt is directive.

These are small numbers, not an evaluation. They suggest that describing a
skill by the thing it produces routes better than describing it by a verb.

**Consequences:**

- A skill's description is its routing contract. It needs its own test set,
  with should-load **and** should-not-load prompts, like any classifier.
- Over-triggering is not harmless. In the contrasting test it silently moved a
  computation to a different tool and changed the output format. A skill can
  capture requests it was never written for.
- To see the model's unguided behaviour (scenario 2), make sure no skill is
  published.

Not tested: a larger model (gpt-4.1, gpt-5), or deterministic routing in the
harness instead of in the model.

### F10 — Skills and hosted-agent facts you will need on day one · **Noted**

Established against `azure-ai-projects` 2.4.0, with
`AIProjectClient(..., allow_preview=True).beta.skills`:

- **Skills are preview.** The client must opt in with `allow_preview=True`.
- **The service assigns version numbers** (sequential integers). Your folder
  names (`v1/`, `v2/`) mean nothing to it. Deleting the skill resets numbering
  to 1.
- **`download()` returns a zip** that contains `SKILL.md` with its front
  matter, not raw text.
- **Front-matter values must be unquoted.** Quoted `name` and `description`
  values returned HTTP 500 on create.
- **Agent Framework has a `SkillsProvider`; LangGraph has nothing.** This
  sample hand-rolls the equivalent in about 70 lines
  ([`agent/main.py`](agent/main.py), section 5). A Toolbox MCP endpoint that
  exposes skills as `skill://` resources is the other documented route; it was
  not tested here.
- **An identical redeploy is a no-op.** `create_version_from_code` with the
  same definition and zip returns the *existing* version number and does not
  restart anything. If behaviour depends on start-up state, you need a real
  change to get a new version.
- **Agent code has a traffic selector; skills do not.** The agent endpoint
  routes by rule. The default is `FixedRatio agent_version=@latest
  traffic_percentage=100`, which `show_agent.py` prints. The rule shape
  suggests canary splits are possible; we did not test one. A skill's default
  pointer is all-or-nothing.
- **"Live immediately" is not quite immediate.** After a deploy, the new
  version showed `active` at 100%, yet the next two invocations were still
  answered by the previous version's code: they called tools that the new
  version no longer had. About a minute later, the new version answered. A warm
  previous container keeps serving while the new version's `remote_build`
  completes. If you smoke-test straight after a deploy, ask the agent to list
  its tools first, so you know which code answered.

---

## Harness and hosting

### F11 — LangGraph silently replaces a tool parameter named `runtime` · **Sharp edge**

This one belongs to LangGraph, not Foundry, but a platform harness will hit
it: when methods claim runtimes, "runtime" is the obvious parameter name.

A tool defined as `run_in_runtime(code, runtime)` fails **every** call inside
the hosted agent, with an empty error:

```
Error invoking tool 'run_in_runtime' with kwargs {'code': "...", 'runtime': 'py-generic'} with error:

 Please fix the error and try again.
```

The model sends `runtime="py-generic"`, and the tool schema advertises it.
Calling the tool directly (`tool.invoke(...)`) works. The cause is in
`langgraph/prebuilt/tool_node.py`: any parameter **named** `runtime` is treated
as the injection slot for LangGraph's `ToolRuntime` object ("special case:
parameter named 'runtime'"), so the model's value is overwritten before the
function runs.

**Fix:** rename the parameter. This sample uses `runtime_name`. Add a unit
test that runs your tools through `ToolNode`, not just `tool.invoke`: the two
code paths differ.

### F12 — The hosted runtime is Python-only in practice · **Noted**

`CodeConfiguration(runtime=...)` accepts `python_3_13`. If part of your harness
is not Python, it does not host here today. It runs behind the agent as a
service the agent calls, which is the execution seam in
[`architecture.md`](architecture.md) and a perfectly good answer.

### F13 — Agent deployment is a data-plane operation · **Noted**

`create_version_from_code` goes to the project's data plane, not to Azure
Resource Manager. Consequences:

- It does not appear in ARM deployment history, and it cannot be expressed in
  Bicep or Terraform as a resource.
- The identity running CI needs **data-plane** access to the project, not just
  an ARM role.
- If the project's data plane is network-restricted, CI must run somewhere that
  can reach it. That is a property of the network posture, not of the API.

---

## Models and capacity

### F14 — A model can report a tool result it never obtained · **Sharp edge**

In 1 of 15 runs of the Scenario 3 prompt with Kimi-K2.7-Code, the agent called
`describe_execution_context` in-process, never called it sandboxed, and still
answered *"Here are the two measured results verbatim"*, with a sandboxed
result it had made up
([`09-unverified-tool-claim.txt`](sample-output/09-unverified-tool-claim.txt)).
The made-up result looks plausible, but every field differs from a real one:

| Field | Reported, never measured | Measured in the sandbox |
| --- | --- | --- |
| `execution_mode` | `sandboxed` | `aca_dynamic_session` |
| `hostname` | `adc-sandbox` (the agent's own container) | a generated session name |
| `env_var_count` | 0 | 38–40 |
| `managed_identity_token` | `null` | `no_identity_endpoint_visible` |
| `egress` | `none` | `blocked:URLError` |

The system prompt already says *"report only what was measured"*. That was
not enough. The other 14 runs called both tools. We did not see this with
gpt-4.1-mini, but we ran that model fewer times, so do not read it as a model
ranking.

**Consequences:**

- For a coding agent whose value is "tested in the runtime", one invented test
  result costs more trust than many slow answers.
- A prompt rule is not a control. The check is mechanical: every result the
  answer cites must match a `function_call_output` in the same response, and
  the response contains both. Run it as an evaluator, or in the harness before
  the answer is returned.
- Keep this run as a regression case, and gate a model change or a skill
  publish on it. [`evaluation/`](evaluation/README.md) replays this recording
  as a dataset: the judge that reads only the answer scores it 5 out of 5, and
  the evaluators that read the trace fail it.

### F15 — Every sandboxed call holds a session until its cooldown ends · **Sharp edge**

A session pool gives each session identifier its own session. A session is
released only after it has been idle for the cooldown period, which is 300 s in
[`sandbox/pool.bicep`](sandbox/pool.bicep). This sample uses a fresh identifier
for every execution, so that no state survives between calls. Each
`test_method` or `run_in_runtime` call therefore holds a session for five
minutes. With `maxConcurrentSessions: 10`, the custom pool serves about ten
executions in any five-minute window.

Running the scenarios back to back crossed that line. In Scenario 4a, the
TURBINE-022 test got `HTTP 429 … Error happened when allocating pod for
identifier …` ([`05-skill-v1-published.txt`](sample-output/05-skill-v1-published.txt)).
The model chose to retry, and the retry got a session. That was luck, not
design.

**Consequences:**

- Size the pool for executions per cooldown window, not for concurrent users:
  sessions ≈ peak sandbox calls per minute × 5. A coding turn makes 2–4 calls.
- The identifier is an isolation decision with a capacity cost. One per call
  gives the strongest isolation and uses the most sessions. One per
  conversation reuses a session, and its state, across a task.
- `Timed` pools cannot go below 300 s. A custom container can use the
  `OnContainerExit` lifecycle instead, so the container ends its own session.
  This sample's runtime server does not exit, so that was not tested.
- Retry 429 in the harness (`_execute_in_runtime`), with backoff, instead of
  relying on the model to notice.
- To check a pool's limits:
  `az containerapp sessionpool show -n <pool> -g <rg> --query properties.scaleConfiguration`.

### F16 — A model endpoint can return the answer in the reasoning summary · **Noted**

In about one final turn in four, the Kimi-K2.7-Code endpoint returned an empty
`message` item and put the whole answer in the `reasoning` item's summary. We
saw it in 3 of 12 direct calls to the model, without LangChain or the agent. The
tool calls were all correct, but the agent's response had no message text, so
`invoke.py`, like most clients, printed an empty answer.

**Fix, in the harness:** `_answer_from_reasoning` in
[`agent/main.py`](agent/main.py) is a LangChain `after_model` middleware. If the
model's final turn has no text and no tool calls but does have a reasoning
summary, it adds the summary as the answer. Turns with text are untouched.
After the change, all eight hosted runs of the same prompt ended with a message.

**Consequence:** a model swap changes the shape of responses, not just their
quality. Put a cheap check for "the final answer is not empty" in the
evaluation suite you run when you change models, as `answer_present` does in
[`evaluation/`](evaluation/README.md).

---

## Evaluation

These came up while building [`evaluation/`](evaluation/README.md). Smaller
ones are listed in its [sharp edges](evaluation/README.md#sharp-edges).

### F17 — An agent-target run records every tool call twice · **Sharp edge**

In an agent-target run, Foundry sends each dataset row to the hosted agent and
passes the trace to the evaluators as `sample.output_items`. For this agent,
that trace lists **every tool call and tool result twice**, sometimes once on
its own and once inside a message that bundles several calls, and adds tool
results whose call ID matches no call. The agent's own Responses output has
each call once. For Kimi-K2.7-Code's four rows, the traces went from 6, 11, 11
and 29 items to 3, 5, 5 and 13 after removing the duplicates by call ID.

`tool_call_accuracy` reads the duplicates as wasted calls. It scored **3 out of
5 on every row where every call was right, for both models**. The same traces,
de-duplicated and scored again in a dataset run, scored **5 out of 5** on
those rows. The alternative mapping,
`tool_calls={{sample.tool_calls}}`, which has each call once, returns "Not
applicable: tool definitions for all tool calls must be provided", even with
the tool definitions mapped.

**Consequences:**

- A comparison between two agents on the same harness stays fair, because both
  are capped the same way. An absolute `tool_call_accuracy` score from a target
  run is not meaningful for this agent.
- `evaluate.py rescore` removes the duplicates from a stored run and scores it
  again, without calling the agent.
- The other trace-reading evaluators passed correct rows, so the duplication
  mainly affects `tool_call_accuracy`. Check a new evaluator against a
  de-duplicated trace before you trust its absolute score.

### F18 — A code grader fails with a generic error · **Sharp edge**

A custom code evaluator runs in a restricted Python sandbox. Three different
causes produced the same result: every row reports only **"An error occurred
during grading"**, with no traceback, and the built-in and prompt evaluators in
the same run score normally.

- The eval group's data source config had `include_sample_schema: false`. We
  had turned it off for a dataset run, which has no `sample`. Both code
  graders failed on every row; with `true`, the same versions passed the same
  rows.
- The sandbox rejects `compile()`, including `re.compile`, and access to dunder
  attributes such as an exception's type name.
- The same registered version of `answer_present` passed every row of the
  Kimi-K2.7-Code run and errored on every row of the next run in the same eval
  group, on traces of the same shape. The other code grader in that run scored
  normally. Earlier, one version of `grounded_numbers` failed on every row
  until we registered byte-identical code as a new version, which was probably
  the same intermittent failure. We did not find the cause.

**What works:**

- Keep `include_sample_schema: true` in every eval group that uses a code
  grader, including groups used only for dataset runs.
- Pass pattern strings to `re.*` functions, and avoid dunder access.
- Wrap `grade` in `try/except BaseException` and return a score, so a bug
  lowers one row's score instead of erroring the run.
- Test every change locally first
  ([`evaluation/test_graders.py`](evaluation/test_graders.py)). If a grader
  that works locally still fails with the generic message, run the evaluation
  again, then register it again (`evaluate.py register --force`) before
  debugging further.
- Do not let a grading error count as a failure in a gate. `evaluate.py
  compare` reports errors separately, and `--fail-on-regression` exits with
  code 2, not 1, when any evaluator errored.

### F19 — An unpinned evaluator reference runs version 1 · **Sharp edge**

A testing criterion that names a custom evaluator without `evaluator_version`
ran **version 1**, not the latest version. Fixes registered as later versions
were silently ignored until the run pinned them.

**Consequence:** always pin `evaluator_version` in testing criteria. Treat the
version as part of the eval definition, the same way you pin a skill version or
a model version. `evaluate.py register` records the version it registered, and
every run pins it.

---

## Measured costs, for planning

All measured against a public-endpoint Foundry project in East US 2. Agent
turns are shown for both models; the other numbers do not depend on the model.

**Deploy and start-up**

| Operation | Measured |
| --- | --- |
| `create_version_from_code`, first version (4.5 KB zip) | **42.7 s** |
| `create_version_from_code`, later versions | **3–7 s** |
| Redeploy with an identical definition and zip | returns the existing version |
| First invoke after a deploy (cold, including `424` retries) | **66 s** |
| New version actually answering, after it shows `active` | **≈ 60 s** ([F10](#f10--skills-and-hosted-agent-facts-you-will-need-on-day-one--noted)) |
| First `connections.get(include_credentials=True)`, cold | **20.8 s** |
| Platform app scale-from-zero, first call | **> 30 s** (the tool timeout; fixed with `minReplicas 1`) |

**Custom runtime lifecycle** (once per runtime version, not per session)

| Operation | Measured |
| --- | --- |
| Runtime image build + push to a Basic registry | **26 min** (≈ 11 build, ≈ 15 push) |
| Custom-container pool provisioning (Bicep) | **1,196 s ≈ 20 min** (sessions ready ≈ minute 15) |

**Execution**

| Operation | Measured |
| --- | --- |
| Probe, new session including allocation: `py-generic` / `py-contoso-analytics` | **4.1 s / 4.4 s** |
| Short snippet, managed pool, end to end | **206 ms** (63 ms inside the sandbox) |
| **Isolation overhead** (sandboxed versus in-process, same snippet) | **≈ 37 ms** |
| Context probe, custom pool, end to end | **322–729 ms** (38–42 ms inside the sandbox) |
| Context probe, in-process | **635–703 ms** (it makes real network calls and mints a token; blocked calls in the sandbox fail fast) |
| `test_method`, robust z-score (numpy) | **185–367 ms** (12–23 ms in the sandbox) |
| `test_method`, PELT (first `import ruptures`) | **1.2–1.5 s** (0.75–1.05 s in the sandbox) |

**Agent turns**

| Operation | gpt-4.1-mini | Kimi-K2.7-Code |
| --- | --- | --- |
| `load_skill` (get the default version and download it) | **0.3–0.9 s** | **0.3–0.8 s** |
| Warm turn, one tool call | **15–20 s** | **16.5 s** |
| Turn with two tool calls (Scenario 3) | — | **17–25 s** |
| Coding turn: describe the Type, write, test twice (4–5 tool calls) | **26–33 s** | **35–51 s** |

Kimi-K2.7-Code is a reasoning model: it spends more time per turn, and its
answers carry its own commentary between tool calls.

---

## How to rank these for your deployment

Several of these change status depending on how you use the platform:

- **Is the executed code written by your customers?** If yes, F2 becomes
  blocking, not a sharp edge: `EgressDisabled` and per-tenant isolation are
  requirements.
- **Do you need custom base images, or is `remote_build` of a
  `requirements.txt` enough?** If images are required, F1 becomes blocking.
- **Does CI need to deploy agents from inside a restricted network?** That
  makes F13 a design constraint.
- **Who will author skills: your engineers, or your customers, per tenant?** If
  customers author them, F8 becomes blocking.
- **Will you let teams switch models, including open-weight ones?** Then F14
  and F16 are reasons to put an evaluation gate in front of every model change,
  not a reason to avoid the models.
- **How many sandboxed calls will run at peak?** F15 sets the pool size, and
  per-call isolation multiplies it.

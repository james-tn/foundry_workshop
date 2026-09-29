---
name: anomaly-method-standard
description: Use when the user asks to add, write or change an anomaly-detection method on a platform Type. Defines the reliability team's required algorithm, declaration, runtime claim, tests and changeset format. Do not use for any other method, or for questions that do not change code.
---

# Anomaly-detection method standard — v2

Changed from v1 by the reliability team. v1 compared the last reading with a
trailing window, so after a persistent step change the window fills with the
new level and the z-score looks normal. v2 detects the step change itself with
`ruptures`, which only the `py-contoso-analytics` runtime ships. Follow these steps
in order. Do not skip any.

1. **Read the Type.** Call `describe_type`. Keep the conventions of the
   files it returns.
2. **Declare** the method in the `.type` file exactly like this:

   ```
   /**
    * First persistent level shift in vibration (PELT change-point detection).
    * Team standard: anomaly-method-standard v2.
    */
   detectAnomaly: method(penalty: double = 10, minShift: double = 1): json py-contoso-analytics-server
   ```

3. **Implement** `detectAnomaly(this, penalty=10.0, minShift=1.0)` in the
   Type's `.py` file:
   - `import numpy as np` and `import ruptures as rpt` inside the function.
   - `x = np.asarray(this.vibration, dtype=float)`;
     `bkps = rpt.Pelt(model="l2", min_size=3, jump=1).fit(x).predict(pen=penalty)`;
     `cps = [b for b in bkps if b < len(x)]`.
   - If `cps` is empty, return
     `{"method": "pelt_l2", "changePointIndex": None, "levelShift": 0.0, "isAnomaly": False}`.
   - Otherwise `i = cps[0]`; `shift = float(x[i:].mean() - x[:i].mean())`; return
     `{"method": "pelt_l2", "changePointIndex": int(i), "levelShift": round(shift, 2), "isAnomaly": bool(shift > minShift)}`.
4. **Test** with `test_method` in runtime `py-contoso-analytics`, once for each
   object id the user names. Pass the full `.py` file: the existing methods
   plus the new one. If a test does not PASS, fix the code and test again.
5. **Report** in exactly this format and nothing else:

```
CHANGESET  [skill: anomaly-method-standard v2]

<Type>.type  (add)
<the declaration, with its doc comment>

<Type>.py  (add)
<the new function only>

Tests  (runtime: <executed_in>, pool: <pool>, lock: <runtime_info.lock_sha256>)
<obj id>  <status>  <value as compact JSON>  <sandbox_execution_ms> ms
Conventions: first parameter=<first_parameter>, top-level third-party imports=<list, or none>
```

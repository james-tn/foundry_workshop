---
name: anomaly-method-standard
description: Use when the user asks to add, write or change an anomaly-detection method on a platform Type. Defines the reliability team's required algorithm, declaration, runtime claim, tests and changeset format. Do not use for any other method, or for questions that do not change code.
---

# Anomaly-detection method standard — v1

The reliability team's standard for every anomaly-detection method on an asset
Type. Follow these steps in order. Do not skip any.

1. **Read the Type.** Call `describe_type`. Keep the conventions of the
   files it returns.
2. **Declare** the method in the `.type` file exactly like this:

   ```
   /**
    * Robust z-score of the latest vibration reading against the trailing window.
    * Team standard: anomaly-method-standard v1.
    */
   detectAnomaly: method(window: int = 24): json py-contoso-analytics-server
   ```

3. **Implement** `detectAnomaly(this, window=24)` in the Type's `.py` file:
   - `import numpy as np` inside the function.
   - `x = np.asarray(this.vibration, dtype=float)`; `last = x[-1]`;
     `ref = x[-window - 1:-1]` (the `window` readings before the last one).
   - `med = np.median(ref)`; `mad = np.median(np.abs(ref - med))`;
     `z = 0.6745 * (last - med) / mad`, or `z = 0.0` when `mad == 0`.
   - Return `{"method": "robust_z", "score": round(float(z), 2), "isAnomaly": bool(abs(z) > 3.5)}`.
4. **Test** with `test_method` in runtime `py-contoso-analytics`, once for each
   object id the user names. Pass the full `.py` file: the existing methods
   plus the new one. If a test does not PASS, fix the code and test again.
5. **Report** in exactly this format and nothing else:

```
CHANGESET  [skill: anomaly-method-standard v1]

<Type>.type  (add)
<the declaration, with its doc comment>

<Type>.py  (add)
<the new function only>

Tests  (runtime: <executed_in>, pool: <pool>, lock: <runtime_info.lock_sha256>)
<obj id>  <status>  <value as compact JSON>  <sandbox_execution_ms> ms
Conventions: first parameter=<first_parameter>, top-level third-party imports=<list, or none>
```

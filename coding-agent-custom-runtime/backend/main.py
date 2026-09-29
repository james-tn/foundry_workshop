"""Stand-in for the Contoso platform, as a coding agent sees it.

Two things a platform coding agent needs from the platform before it writes a
line of code, and this service provides exactly those:

* **Type metadata** - the `.type` declaration and the existing `Type.py`
  implementation, so generated code extends a real type instead of guessing.
* **Objects** - real records of that type, so generated methods are tested on
  real data rather than on numbers the model made up.

It also rejects every call that does not carry the right x-api-key. That is
the coexistence seam in the demo: the agent's credential for this service is
resolved at runtime from a Foundry connection, never from source or the image.

The Contoso platform is fictional. Replace this service with your own platform API.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException

API_KEY = os.environ.get("CONTOSO_API_KEY", "")
app = FastAPI(title="contoso-platform")

# ------------------------------------------------------------------ runtimes
# The platform's runtime registry. Method claims name one of these; the agent's
# deploy config maps each name to the session pool that implements it.
RUNTIMES = {
    "py-generic": {
        "claim": "py-generic-server",
        "description": "Managed Python 3.12 sandbox for ad-hoc scripts. Common data libraries only and no private packages, so platform methods cannot claim it.",
    },
    "py-contoso-analytics": {
        "claim": "py-contoso-analytics-server",
        "description": "Custom runtime for anomaly-detection methods on asset types. Frozen at image build.",
        "libraries": [
            "pip numpy==1.26.4",
            "pip pandas==2.2.3",
            "pip scipy==1.14.1",
            "pip scikit-learn==1.5.2",
            "pip ruptures==1.1.9",
            "pip contoso_sdk==0.3.0 (private)",
        ],
    },
}

# --------------------------------------------------------------------- types
_ASSET_TYP = '''/**
 * {doc}
 * Package: assetPerformance (Contoso platform sample)
 */
type {name} extends Asset table "{schema}" {{

  /** Site the asset is installed at. */
  site: string

  /** {rating_doc} */
  {rating_field}: double

  /** Hourly RMS vibration in mm/s, oldest first. */
  vibration: [double]

  /** Vibration alarm threshold, mm/s. */
  vibrationThreshold: double

  /** Fraction of readings above vibrationThreshold. */
  pctOverThreshold: method(): double py-contoso-analytics-server
}}
'''

_ASSET_PY = '''def pctOverThreshold(this):
    readings = this.vibration or []
    if not readings:
        return None
    return sum(v > this.vibrationThreshold for v in readings) / len(readings)
'''

TYPES = {
    "WindTurbine": {
        "doc": "A wind turbine and its hourly vibration telemetry.",
        "schema": "WIND_TURBINE",
        "rating_field": "ratedPowerMw",
        "rating_doc": "Rated power, MW.",
    },
    "Compressor": {
        "doc": "A gas compressor and its hourly vibration telemetry.",
        "schema": "COMPRESSOR",
        "rating_field": "ratedFlowM3h",
        "rating_doc": "Rated flow, m3/h.",
    },
}

# Deterministic telemetry, 48 hourly readings each. TURBINE-014 has a step
# change at reading 32 and crosses threshold at 40; COMPRESSOR-07 drifts
# towards threshold without crossing; TURBINE-022 is healthy.
OBJS = {
    "WindTurbine": {
        "TURBINE-014": {
            "site": "Odessa", "ratedPowerMw": 3.6, "vibrationThreshold": 7.0,
            "vibration": [2.38, 2.35, 1.99, 1.91, 2.18, 2.31, 2.18, 1.98, 2.13, 2.42, 1.99, 2.36,
                          2.28, 2.27, 2.32, 2.12, 2.18, 2.47, 2.14, 2.14, 1.99, 2.13, 2.31, 2.39,
                          2.3, 2.29, 2.29, 2.25, 2.35, 2.18, 2.36, 2.19, 4.28, 4.65, 5.16, 5.41,
                          5.56, 5.95, 6.44, 6.78, 7.17, 7.13, 7.68, 8.11, 8.27, 8.85, 8.71, 9.1],
        },
        "TURBINE-022": {
            "site": "Odessa", "ratedPowerMw": 3.6, "vibrationThreshold": 7.0,
            "vibration": [1.98, 1.88, 2.44, 1.98, 1.93, 1.97, 1.82, 1.79, 1.98, 1.83, 2.03, 1.87,
                          1.62, 1.69, 1.77, 1.59, 1.89, 1.85, 2.05, 1.76, 1.86, 1.99, 2.02, 1.74,
                          2.14, 1.8, 2.09, 1.85, 1.83, 1.7, 1.88, 2.28, 1.8, 2.14, 2.11, 1.84,
                          2.08, 2.12, 1.85, 1.82, 2.08, 1.96, 2.23, 1.97, 1.72, 1.89, 1.71, 2.02],
        },
    },
    "Compressor": {
        "COMPRESSOR-07": {
            "site": "Midland", "ratedFlowM3h": 5200.0, "vibrationThreshold": 7.0,
            "vibration": [2.97, 3.14, 3.12, 3.19, 3.19, 3.35, 3.58, 3.58, 3.72, 3.7, 3.8, 3.85,
                          3.7, 4.08, 4.11, 4.18, 4.0, 4.07, 4.24, 4.37, 4.54, 4.57, 4.71, 4.65,
                          4.84, 4.92, 4.87, 5.23, 5.17, 5.32, 5.18, 5.24, 5.36, 5.46, 5.63, 5.65,
                          5.65, 5.66, 5.79, 6.07, 5.9, 6.1, 6.2, 6.05, 6.31, 6.53, 6.21, 6.49],
        },
    },
}


def _authorize(key: str | None) -> None:
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing x-api-key")


def _type_or_404(name: str) -> str:
    match = next((t for t in TYPES if t.lower() == name.lower()), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"unknown type {name}; known: {sorted(TYPES)}")
    return match


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "utc": datetime.now(timezone.utc).isoformat()}


@app.get("/runtimes")
def list_runtimes(x_api_key: str | None = Header(default=None)) -> dict:
    _authorize(x_api_key)
    return {"runtimes": RUNTIMES}


@app.get("/types")
def list_types(x_api_key: str | None = Header(default=None)) -> dict:
    _authorize(x_api_key)
    return {"types": [{"name": n, "doc": t["doc"], "objs": sorted(OBJS[n])} for n, t in TYPES.items()]}


@app.get("/types/{name}")
def describe_type(name: str, x_api_key: str | None = Header(default=None)) -> dict:
    _authorize(x_api_key)
    name = _type_or_404(name)
    return {
        "type": name,
        "files": {
            f"{name}.type": _ASSET_TYP.format(name=name, **TYPES[name]),
            f"{name}.py": _ASSET_PY,
        },
        "objs": sorted(OBJS[name]),
        "runtimes": RUNTIMES,
    }


@app.get("/types/{name}/objs/{obj_id}")
def get_obj(name: str, obj_id: str, x_api_key: str | None = Header(default=None)) -> dict:
    _authorize(x_api_key)
    name = _type_or_404(name)
    record = OBJS[name].get(obj_id.upper())
    if not record:
        raise HTTPException(status_code=404, detail=f"no {name} with id {obj_id}")
    return {"type": name, "id": obj_id.upper(), **record}

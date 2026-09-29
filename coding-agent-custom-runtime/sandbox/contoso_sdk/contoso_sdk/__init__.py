"""contoso_sdk - the offline half of a platform runtime.

A private package (it is on no public index) baked into the py-contoso-analytics
runtime image. It lets agent-generated Type implementation files be loaded and
executed the way the Contoso platform would load them:

* the file's top-level functions are named after the methods they implement;
* member methods take `this` first, static methods take `cls` first;
* a global `contoso` namespace is available inside the file;
* third-party imports belong inside functions, not at the top of the file.

`invoke()` runs one method against one object and reports the result together
with those convention checks, so a sandbox test also acts as a lint.

The Contoso platform is fictional; this SDK is written for the sample and
models only the conventions above.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import os
import sys
import time
import types

__version__ = "0.3.0"

RUNTIME = os.environ.get("CONTOSO_RUNTIME", "unknown")
LOCK_PATH = f"/opt/contoso/runtime/{RUNTIME}.resolved.json"


class Obj(dict):
    """Attribute access over a dict, like a platform Obj."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


def obj(type_name: str, fields: dict) -> Obj:
    record = Obj(fields)
    record["type"] = type_name
    return record


_FIXTURES: dict[str, list[Obj]] = {}


class _TypeRef:
    def __init__(self, name: str):
        self.name = name

    def get(self, id: str):  # noqa: A002 - mirrors the platform verb
        return next((o for o in _FIXTURES.get(self.name, []) if o.get("id") == id), None)

    def fetch(self, spec: dict | None = None) -> Obj:
        objs = list(_FIXTURES.get(self.name, []))
        limit = (spec or {}).get("limit")
        return Obj({"objs": objs[:limit] if limit else objs, "count": len(objs)})

    def __repr__(self):
        return f"<contoso.{self.name}>"


class _Namespace:
    def __getattr__(self, name: str) -> _TypeRef:
        if name.startswith("_"):
            raise AttributeError(name)
        return _TypeRef(name)


contoso = _Namespace()


def register(type_name: str, *records: dict) -> None:
    """Seed objects so implementation code can call contoso.<Type>.fetch()/get()."""
    _FIXTURES.setdefault(type_name, []).extend(obj(type_name, r) for r in records)


def load_impl(type_name: str, source: str) -> types.ModuleType:
    module = types.ModuleType(type_name)
    module.__dict__["contoso"] = contoso
    exec(compile(source, f"{type_name}.py", "exec"), module.__dict__)  # noqa: S102
    return module


def _top_level_third_party_imports(source: str) -> list[str]:
    stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    found = []
    for node in ast.parse(source).body:
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        found += [n for n in names if n.split(".")[0] not in stdlib]
    return found


def _jsonable(value):
    if hasattr(value, "item") and callable(value.item):  # numpy scalars
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    return value


def invoke(type_name: str, source: str, method: str, this: dict | None = None, **kwargs) -> dict:
    """Load `source` as <type_name>.py and call `method` the way the platform would."""
    module = load_impl(type_name, source)
    fn = getattr(module, method, None)
    if not callable(fn):
        raise AttributeError(f"{type_name}.py defines no top-level function {method!r}")
    first = next(iter(inspect.signature(fn).parameters), None)
    if first not in ("this", "cls"):
        raise TypeError(f"{method}: first parameter must be 'this' (member) or 'cls' (static), got {first!r}")
    target = obj(type_name, this or {}) if first == "this" else getattr(contoso, type_name)
    started = time.perf_counter()
    value = fn(target, **kwargs)
    return {
        "method": f"{type_name}.{method}",
        "kind": "member" if first == "this" else "static",
        "value": _jsonable(value),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "runtime": RUNTIME,
        "conventions": {
            "first_parameter": first,
            "top_level_third_party_imports": _top_level_third_party_imports(source),
        },
    }


def runtime_info() -> dict:
    info = {"runtime": RUNTIME, "contoso_sdk": __version__}
    if os.path.exists(LOCK_PATH):
        with open(LOCK_PATH, encoding="utf-8") as handle:
            resolved = json.load(handle)
        info["lock_sha256"] = resolved.get("sha256", "")[:16]
        info["declared"] = resolved.get("declared", [])
    return info


def fingerprint(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:12]

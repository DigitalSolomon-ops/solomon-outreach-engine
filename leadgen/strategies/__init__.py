"""Lead strategies: the registry (config/strategies.yaml) and how a campaign picks one.

A campaign stores icp.strategy and icp.strategy_params. Campaigns saved before the registry
only have icp.site_filter; strategy_id() maps that to the matching strategy on every read, so
they load and behave exactly as before. apply_patch keeps both fields in step, which is how a
disagreement can be told apart: only a writer that predates the registry changes site_filter
alone, so then site_filter wins.
"""
from __future__ import annotations

import fnmatch
import importlib
import re
from pathlib import Path

import jsonschema
import yaml

from ..config import ROOT

SOURCES = ("maps", "fmcsa", "sam", "jobs", "ai_answer")
PREVIEW = ("required", "allowed", "forbidden")
# Legacy icp.site_filter -> strategy id, and back (new strategies store site_filter "any").
FROM_SITE_FILTER = {"any": "any_site", "weak": "weak_site", "weak_or_none": "weak_or_no_site",
                    "new_listing": "new_business"}
TO_SITE_FILTER = {v: k for k, v in FROM_SITE_FILTER.items()}
# Params that predate the registry and still live at the top of icp.
LEGACY_PARAMS = {"new_business": {"max_reviews": "max_reviews"}}
DEFAULT = "any_site"

PARAM = {
    "type": "object", "additionalProperties": False, "required": ["type", "default", "label"],
    "properties": {"type": {"enum": ["integer", "number", "string"]}, "default": {},
                   "min": {"type": "number"}, "max": {"type": "number"},
                   "max_length": {"type": "integer"}, "pattern": {"type": "string"},
                   "label": {"type": "string"}},
}
SCHEMA = {
    "type": "object", "required": ["strategies"],
    "properties": {"strategies": {"type": "array", "minItems": 1, "items": {
        "type": "object", "additionalProperties": False,
        "required": ["id", "label", "description", "source", "params", "facts", "cites_site_record",
                     "needs_preview", "uses_industry_list", "providers", "status"],
        "properties": {
            "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]+$"},
            "label": {"type": "string", "minLength": 3, "maxLength": 60},
            "description": {"type": "string", "minLength": 10},
            "source": {"enum": list(SOURCES)},
            "params": {"type": "object", "additionalProperties": PARAM},
            "facts": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "cites_site_record": {"type": "boolean"},
            "needs_preview": {"enum": list(PREVIEW)},
            "uses_industry_list": {"type": "boolean"},
            "providers": {"type": "array", "items": {"type": "string"}},
            "status": {"enum": ["live", "building"]},
        }}}},
}


class StrategyError(ValueError):
    pass


_CACHE: dict | None = None


def load(path: str | Path | None = None, registry: dict | None = None) -> dict:
    """Schema, then: unique ids, a module per strategy, providers that exist, params whose
    default is inside its own bounds."""
    global _CACHE
    if path is None and registry is None and _CACHE is not None:
        return _CACHE
    with open(Path(path or ROOT / "config" / "strategies.yaml"), encoding="utf-8") as f:
        data = yaml.safe_load(f)
    try:
        jsonschema.validate(data, SCHEMA)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path)
        raise StrategyError(f"strategies.yaml invalid at {where or '(root)'}: {exc.message}") from exc
    ids = [s["id"] for s in data["strategies"]]
    if len(ids) != len(set(ids)):
        raise StrategyError("strategies.yaml: duplicate id")
    if registry is None:
        from ..capabilities import load_registry
        registry = load_registry()
    known = {p["name"] for p in registry["providers"]}
    for s in data["strategies"]:
        missing = [p for p in s["providers"] if p not in known]
        if missing:
            raise StrategyError(f"strategies.yaml: {s['id']} names unknown providers {missing}")
        for name, spec in s["params"].items():
            problem = param_problem(spec, spec["default"])
            if problem:
                raise StrategyError(f"strategies.yaml: {s['id']}.params.{name} default: {problem}")
        _module(s["id"])
    out = {"strategies": data["strategies"], "by_id": {s["id"]: s for s in data["strategies"]}}
    if path is None:
        _CACHE = out
    return out


def _module(sid: str):
    try:
        return importlib.import_module(f".{sid}", __name__)
    except ModuleNotFoundError as exc:
        raise StrategyError(f"strategy {sid} has no module leadgen/strategies/{sid}.py") from exc


def entry(sid: str) -> dict:
    by_id = load()["by_id"]
    if sid not in by_id:
        raise StrategyError(f"unknown strategy {sid!r}")
    return by_id[sid]


def live() -> list[dict]:
    return [s for s in load()["strategies"] if s["status"] == "live"]


def public_list() -> list[dict]:
    """GET /strategies: what the dropdown and the strategist see."""
    return [{k: s[k] for k in ("id", "label", "description", "source", "params", "needs_preview",
                                "uses_industry_list", "facts")} for s in live()]


# ------------------------------------------------------------------ a campaign's strategy
def strategy_id(c: dict) -> str:
    icp = c.get("icp") or {}
    sid = icp.get("strategy")
    f = icp.get("site_filter")
    if not sid:
        return FROM_SITE_FILTER.get(f or "any", DEFAULT)
    if f is not None and f in FROM_SITE_FILTER and f != TO_SITE_FILTER.get(sid, "any"):
        return FROM_SITE_FILTER[f]   # a pre-registry writer changed site_filter alone
    return sid


def params(c: dict, sid: str | None = None) -> dict:
    sid = sid or strategy_id(c)
    icp = c.get("icp") or {}
    spec = entry(sid)["params"]
    out = {k: v["default"] for k, v in spec.items()}
    out.update({k: v for k, v in (icp.get("strategy_params") or {}).items() if k in spec})
    for name, legacy in LEGACY_PARAMS.get(sid, {}).items():
        if legacy in icp:
            out[name] = icp[legacy]
    return out


def for_campaign(c: dict):
    """The strategy object the prospector calls for this campaign."""
    sid = strategy_id(c)
    return _module(sid).STRATEGY(entry(sid), params(c, sid))


def normalize_icp(icp: dict) -> dict:
    """Both fields in step after an edit: icp.strategy and the legacy icp.site_filter."""
    out = dict(icp)
    sid = strategy_id({"icp": out})
    out["strategy"] = sid
    out["site_filter"] = TO_SITE_FILTER.get(sid, "any")
    sp = dict(out.get("strategy_params") or {})
    for name, legacy in LEGACY_PARAMS.get(sid, {}).items():
        if legacy in out:
            sp[name] = out[legacy]
        elif name in sp:
            out[legacy] = sp[name]
    out["strategy_params"] = sp
    return out


def view(c: dict) -> dict:
    """A campaign as the API returns it: icp.strategy and its params filled in, nothing saved."""
    icp = c.get("icp") or {}
    sid = strategy_id(c)
    try:
        p = params(c, sid)
        label = entry(sid)["label"]
    except StrategyError:
        p, label = icp.get("strategy_params") or {}, sid
    return {**c, "icp": {**icp, "strategy": sid, "strategy_params": p}, "strategy_label": label}


def param_problem(spec: dict, v) -> str | None:
    t = spec["type"]
    if t == "integer" and (not isinstance(v, int) or isinstance(v, bool)):
        return "must be a whole number"
    if t == "number" and (not isinstance(v, (int, float)) or isinstance(v, bool)):
        return "must be a number"
    if t == "string" and not isinstance(v, str):
        return "must be text"
    if t in ("integer", "number"):
        if "min" in spec and v < spec["min"]:
            return f"must be at least {spec['min']:g}"
        if "max" in spec and v > spec["max"]:
            return f"must be at most {spec['max']:g}"
    else:
        if len(v) > spec.get("max_length", 200):
            return f"must be at most {spec.get('max_length', 200)} characters"
        if spec.get("pattern") and not re.match(spec["pattern"], v):
            return "is not in the expected format"
    return None


def validate_campaign(c: dict) -> list[str]:
    """Problems with the campaign's strategy, its params and its fit with the preview offer."""
    icp = c.get("icp") or {}
    raw = icp.get("strategy")
    if raw and raw not in load()["by_id"]:
        return [f"icp.strategy {raw!r} is not a known strategy"]
    sid = strategy_id(c)
    s = entry(sid)
    problems = []
    if raw and s["status"] != "live":
        problems.append(f"icp.strategy {sid} is still being built and can't be used yet")
    for k, v in (icp.get("strategy_params") or {}).items():
        if k not in s["params"]:
            problems.append(f"icp.strategy_params.{k} is not a setting of {sid}")
    for k, v in params(c, sid).items():
        problem = param_problem(s["params"][k], v)
        if problem:
            legacy = next((lg for n, lg in LEGACY_PARAMS.get(sid, {}).items() if n == k), None)
            problems.append(f"icp.{legacy or 'strategy_params.' + k} {problem}")
    pv = c.get("preview") or {}
    if s["needs_preview"] == "required" and not pv.get("enabled"):
        legacy = TO_SITE_FILTER.get(sid)
        what = f"icp.site_filter {legacy}" if legacy and icp.get("site_filter") == legacy else f"icp.strategy {sid}"
        problems.append(f"{what} is for Website Launch campaigns only (preview offer enabled)")
    if s["needs_preview"] == "forbidden" and pv.get("enabled"):
        problems.append(f"{s['label']} can't be used with the preview offer")
    if pv.get("enabled") and not s["uses_industry_list"]:
        problems.append(_template_problem(c, s))
    return [p for p in problems if p]


def _template_problem(c: dict, s: dict) -> str | None:
    """The funnel has a preview template only for the industries list. A strategy whose segments
    come from the data needs every segment to be one of those industries."""
    from .. import industries as inds
    labels = {i["label"].lower(): i["slug"] for i in inds.catalog()["industries"]}
    segs = sorted({x["segment"] for x in (c.get("icp") or {}).get("cells", [])})
    missing = [x for x in segs if x.lower() not in labels]
    if not segs or missing:
        return (f"{s['label']} picks its segments from its own data, and the funnel has no preview template for "
                f"{', '.join(repr(m) for m in missing) or 'a campaign with no segment'}. Turn the preview offer off, "
                "or use segments from the industries list.")
    return None


def allows_fact(s: dict, fact_id: str) -> bool:
    return any(fnmatch.fnmatchcase(fact_id, pat) for pat in s["facts"])


ALWAYS_CITABLE = ("owner:first_name", "email_source")


def filter_evidence(s: dict, evidence: dict) -> dict:
    """The writer sees only what this strategy may say: its facts, and the rest of the lead record
    only when cites_site_record is true."""
    out = {}
    for k, v in evidence.items():
        if k.startswith("fact:"):
            if allows_fact(s, k[5:]):
                out[k] = v
        elif s["cites_site_record"] or k in ALWAYS_CITABLE:
            out[k] = v
    return out

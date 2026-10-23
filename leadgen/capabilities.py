"""Capability registry (config/capabilities.yaml) and the vault inventory.

The registry is the source of truth for every provider: free allowance, unit,
period, card-on-file risk, and rotation priority. It is schema-validated at
load. The inventory matches Secret Manager secret NAMES (never values) against
it: a provider is "connected" when its secret or any alias exists, and the rest
become recommendations naming the exact Credentials slot to paste a key into.
"""
from __future__ import annotations

import math
from pathlib import Path

import jsonschema
import yaml

from .config import ROOT
from .store import utcnow

CAPABILITIES = ("discover_maps", "discover_web", "scrape", "verify_email", "enrich_people",
                "enrich_tech", "enrich_reviews", "market_size", "write")
PERIODS = ("month", "day", "once", "unknown", "unlimited")

SCHEMA = {
    "type": "object",
    "required": ["operator_policy", "providers"],
    "properties": {
        "operator_policy": {
            "type": "object",
            "required": ["paid_monthly_cap_usd", "card_safety_ratio"],
            "properties": {
                "paid_monthly_cap_usd": {"type": "number", "minimum": 0},
                "prefer_recurring_over_once": {"type": "boolean"},
                "card_safety_ratio": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
            },
        },
        "providers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "status"],
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z0-9_]+$"},
                    "status": {"enum": ["wired", "candidate", "skip"]},
                    "secret": {"type": ["string", "null"]},
                    "secret_aliases": {"type": "array", "items": {"type": "string"}},
                    "card_on_file": {"type": "boolean"},
                    "capabilities": {"type": "array", "items": {"enum": list(CAPABILITIES)}},
                    "free": {
                        "type": "object",
                        "required": ["unit", "period"],
                        "properties": {
                            "amount": {"type": ["number", "null"]},
                            "unit": {"type": "string"},
                            "period": {"enum": list(PERIODS)},
                        },
                    },
                    "priority": {"type": "integer"},
                    "verified": {"enum": ["official", "unverified"]},
                    "docs": {"type": "string"},
                },
            },
        },
    },
}

# Which capability puts a provider in which engine stage. Discovery keeps the
# maps/web distinction because campaigns prefer maps for local businesses.
STAGE_CAPS = {
    "discovery": ("discover_maps", "discover_web"),
    "scrape": ("scrape",),
    "verify": ("verify_email",),
    "people": ("enrich_people",),
    "sizing": ("market_size",),
}
# Registry name -> adapter key, where one provider has adapters in two stages.
ADAPTER_KEYS = {("verify", "getprospect"): "getprospect_verify",
                ("verify", "anymailfinder"): "anymailfinder_verify"}
# Providers that draw on one allowance from more than one stage.
POOLS = {"hunter_verify": "hunter", "hunter_domain": "hunter"}


class RegistryError(ValueError):
    pass


def load_registry(path: str | Path | None = None) -> dict:
    path = Path(path or ROOT / "config" / "capabilities.yaml")
    with open(path, encoding="utf-8") as f:
        reg = yaml.safe_load(f)
    return validate_registry(reg)


def validate_registry(reg: dict) -> dict:
    try:
        jsonschema.validate(reg, SCHEMA)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path)
        raise RegistryError(f"capabilities.yaml invalid at {where or '(root)'}: {exc.message}") from exc
    names = [p["name"] for p in reg["providers"]]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise RegistryError(f"capabilities.yaml has duplicate providers: {sorted(dupes)}")
    for p in reg["providers"]:
        if p["status"] != "skip" and "capabilities" not in p:
            raise RegistryError(f"provider {p['name']} needs capabilities")
    return reg


def secret_names_for(p: dict) -> list[str]:
    return [n for n in [p.get("secret"), *p.get("secret_aliases", [])] if n]


def vault_slot(secret: str) -> str:
    """Credentials slot for a secret: custom credential 'global/foo' is stored as 'global-foo'."""
    return secret.replace("global-", "global/", 1) if secret.startswith("global-") else secret


UNASSIGNED: tuple[str, ...] = ()  # both former unknowns are the Anymail Finder key (2026-10-23)
ENGINE_SECRETS = ("global-anthropic", "global-leadgen-hmac", "global-leadgen-notify-smtp",
                  "global-ghl-leadgen")


def inventory(reg: dict, present: set[str]) -> dict:
    """Match secret names to the registry. Values are never read here."""
    providers, recommended = [], []
    for p in reg["providers"]:
        if p["status"] == "skip":
            continue
        names = secret_names_for(p)
        keyless = not names
        used = next((n for n in names if n in present), None)
        connected = (keyless and p["status"] == "wired") or used is not None
        row = {"name": p["name"], "status": p["status"], "connected": connected,
               "secret_used": used, "keyless": keyless, "capabilities": p.get("capabilities", []),
               "free": p.get("free"), "card_on_file": bool(p.get("card_on_file")),
               "priority": p.get("priority", 50), "verified": p.get("verified")}
        providers.append(row)
        if not connected and p.get("secret") and p["status"] == "wired" and "write" not in p.get("capabilities", []):
            recommended.append({
                "name": p["name"], "capabilities": p.get("capabilities", []),
                "free": p.get("free"), "why": _why(p), "docs": p.get("docs"),
                "vault_slot": vault_slot(p["secret"]), "secret": p["secret"],
                "card_on_file": bool(p.get("card_on_file"))})
    mailboxes = sorted(n for n in present if n.startswith("global-leadgen-mailbox-"))
    matched = sorted({r["secret_used"] for r in providers if r["secret_used"]}
                     | {n for n in ENGINE_SECRETS if n in present} | set(mailboxes))
    return {
        "at": utcnow().isoformat(),
        "matched_secret_names": matched,
        "unassigned_secret_names": sorted(n for n in UNASSIGNED if n in present),
        "mailbox_secrets": mailboxes,
        "notify_smtp": "global-leadgen-notify-smtp" in present,
        "total_secret_names_seen": len(present),
        "providers": providers,
        "connected": sorted(r["name"] for r in providers if r["connected"]),
        "recommended": sorted(recommended, key=lambda r: r["name"]),
    }


def _why(p: dict) -> str:
    free = p.get("free") or {}
    amt = free.get("amount")
    allowance = f"{amt:g} {free.get('unit')} per {free.get('period')}" if amt else "free"
    caps = ", ".join(p.get("capabilities", []))
    return f"Adds {caps} at $0 ({allowance})."


def rotation_entries(reg: dict, inv: dict, stage: str, adapters: dict, overrides: dict | None = None,
                     margin: float = 0.95) -> list[dict]:
    """Build the ordered rotation for one stage from connected providers that have an adapter.

    Order: recurring allowances (month/day/unlimited) before one-time credits, then
    registry priority. Caps: the free allowance in the adapter's unit, times
    card_safety_ratio for card-on-file providers (never one unit past free) or a
    small margin for the rest. Overrides convert units (e.g. $ credit -> requests).
    """
    overrides = overrides or {}
    policy = reg["operator_policy"]
    by_name = {p["name"]: p for p in reg["providers"]}
    rows = []
    for r in inv["providers"]:
        if not r["connected"]:
            continue
        p = by_name[r["name"]]
        if not set(p.get("capabilities", [])) & set(STAGE_CAPS[stage]):
            continue
        key = ADAPTER_KEYS.get((stage, p["name"]), p["name"])
        if key not in adapters:
            continue
        ov = overrides.get(p["name"], {})
        free = p.get("free") or {}
        period = ov.get("period", free.get("period", "month"))
        amount = ov.get("cap_units", free.get("amount"))
        entry = {"name": key, "registry_name": p["name"], "secret": r["secret_used"],
                 "period": "month" if period == "unknown" else period,
                 "pool": ov.get("pool", POOLS.get(p["name"], p["name"])),
                 "card_on_file": bool(p.get("card_on_file")), "priority": p.get("priority", 50)}
        if stage == "discovery":
            entry["kind"] = "maps" if "discover_maps" in p["capabilities"] else "web"
        if amount is not None and period not in ("unlimited",):
            ratio = policy["card_safety_ratio"] if entry["card_on_file"] else margin
            entry["cap"] = max(1, math.floor(amount * ratio))
            entry["free_amount"] = amount
        entry.update({k: v for k, v in ov.items() if k not in {"cap_units", "period", "pool"}})
        rows.append(entry)
    recurring_first = policy.get("prefer_recurring_over_once", True)
    rows.sort(key=lambda e: ((e["period"] == "once") if recurring_first else 0, e["priority"]))
    return rows

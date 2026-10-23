"""Metered model calls and the AI budget.

Every call records input/output tokens and dollar cost to leadgen_costs, priced
from config/models.yaml. The budget (settings.budget) is checked before each call:

  non-reply work (planning, briefs, first touches, follow-ups) runs while the
  month is under 100% of ai_monthly_cap_usd AND non-reply spend is under 80% of it
  (20% is held for replies);
  replies (classify, write, critique) run up to 120%; past that they escalate.

At 80% and 100% Marcus gets one notice each per month. Prospecting never falls
back to template blasting when the money runs out: new first touches just stop.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

import httpx

from .store import utcnow

log = logging.getLogger(__name__)

REPLY_PURPOSES = {"reply_classify", "reply_write", "reply_critic", "reply_rewrite"}


class BudgetStop(Exception):
    """This kind of AI work is over budget for the month. The caller stops that work."""


class LLMError(Exception):
    pass


def month_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class Budget:
    def __init__(self, settings: dict, ledger, notifier=None):
        self.cfg = settings["budget"]
        self.ledger = ledger
        self.notifier = notifier

    @property
    def cap(self) -> float:
        override = self.ledger.state("settings").get("ai_monthly_cap_usd")
        return float(override if override is not None else self.cfg["ai_monthly_cap_usd"])

    def spend(self, now: datetime | None = None) -> dict:
        rows = self.ledger.costs_since(month_start(now))
        total = sum(r.get("usd", 0) for r in rows)
        reply = sum(r.get("usd", 0) for r in rows if r.get("purpose") in REPLY_PURPOSES)
        return {"total": round(total, 6), "reply": round(reply, 6), "nonreply": round(total - reply, 6)}

    def allowed(self, purpose: str, now: datetime | None = None) -> tuple[bool, str]:
        s, cap = self.spend(now), self.cap
        if purpose in REPLY_PURPOSES:
            hard = cap * self.cfg["replies_hard_stop"]
            if s["total"] >= hard:
                return False, f"AI spend ${s['total']:.2f} is past {self.cfg['replies_hard_stop']:.0%} of the ${cap:.0f} cap"
            return True, ""
        if s["total"] >= cap:
            return False, f"AI spend ${s['total']:.2f} reached the ${cap:.0f} monthly cap"
        reserve_line = cap * (1 - self.cfg["reply_reserve_share"])
        if s["nonreply"] >= reserve_line:
            return False, (f"non-reply AI spend ${s['nonreply']:.2f} reached ${reserve_line:.2f}; "
                           f"the rest of the cap is held for replies")
        return True, ""

    def check_notices(self, now: datetime | None = None):
        """One email at 80% and one at 100% per month."""
        s, cap = self.spend(now), self.cap
        key = month_start(now).strftime("%Y-%m")
        sent = self.ledger.state(f"budget_notices_{key}")
        for level, label in ((self.cfg["notice_at"], "80"), (1.0, "100")):
            if s["total"] >= cap * level and not sent.get(label):
                self.ledger.set_state(f"budget_notices_{key}", **{label: utcnow().isoformat()})
                what = (f"AI spend reached {label}% of the ${cap:.0f} monthly cap (${s['total']:.2f}).")
                why = ("New first touches and prospecting are stopped until the 1st; replies continue "
                       "up to 120% of the cap." if label == "100" else
                       "Nothing changes yet. At 100% new first touches stop; replies keep running.")
                self.ledger.event("budget", what, why=why,
                                  cost_impact=f"${s['total']:.2f} spent this month", notify=True)
                if self.notifier:
                    self.notifier.flush()


class LLM:
    """Haiku and Sonnet through the Messages API, metered into leadgen_costs."""

    def __init__(self, api_key: str | None, models: dict, ledger, budget: Budget, client=None):
        self.api_key = api_key
        self.models = models
        self.ledger = ledger
        self.budget = budget
        # Injectable for tests: client(model_id, system, user, max_tokens) -> (text, in_tok, out_tok)
        self.client = client

    @property
    def available(self) -> bool:
        return bool(self.api_key or self.client)

    def price(self, tier: str, in_tok: int, out_tok: int) -> float:
        m = self.models[tier]
        return in_tok / 1e6 * m["input_per_mtok"] + out_tok / 1e6 * m["output_per_mtok"]

    def _post(self, model: str, system: str, user: str, max_tokens: int, schema: dict | None = None):
        if self.client:
            return self.client(model, system, user, max_tokens)
        if not self.api_key:
            raise LLMError("no Anthropic key")
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user}]}
        if schema:
            # Structured outputs: the API guarantees JSON matching the schema. Constraints it
            # doesn't support are stripped here and still enforced by our own validation.
            body["output_config"] = {"format": {"type": "json_schema", "schema": api_schema(schema)}}
        try:
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages", timeout=180,
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"anthropic: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise LLMError(f"anthropic HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if data.get("stop_reason") == "refusal":
            raise LLMError("model declined (refusal)")
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        u = data.get("usage", {})
        return text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))

    def call(self, tier: str, system: str, user: str, *, purpose: str,
             campaign_id: str | None = None, max_tokens: int = 900, schema: dict | None = None) -> str:
        ok, why = self.budget.allowed(purpose)
        if not ok:
            raise BudgetStop(why)
        model = self.models[tier]["id"]
        text, in_tok, out_tok = self._post(model, system, user, max_tokens, schema)
        usd = self.price(tier, in_tok, out_tok)
        self.ledger.record_cost({"model": model, "tier": tier, "purpose": purpose,
                                 "campaign_id": campaign_id, "input_tokens": in_tok,
                                 "output_tokens": out_tok, "usd": round(usd, 6)})
        return text

    def json(self, tier: str, system: str, user: str, **kw) -> dict:
        raw = self.call(tier, system, user, **kw)
        return parse_json(raw)


UNSUPPORTED = {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
               "multipleOf", "minItems", "maxItems", "pattern", "uniqueItems"}


def api_schema(schema):
    """Copy of a JSON schema the structured-outputs API accepts: every object closed, no
    numeric/string/array-size constraints (those stay enforced by jsonschema on our side)."""
    if isinstance(schema, list):
        return [api_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: api_schema(v) for k, v in schema.items() if k not in UNSUPPORTED}
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
    return out


def obj(required: dict, optional: dict | None = None) -> dict:
    """Small helper for inline response schemas."""
    props = {**required, **(optional or {})}
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


STR = {"type": "string"}
STRS = {"type": "array", "items": {"type": "string"}}


def parse_json(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        raise LLMError("model returned no JSON object")
    try:
        out = json.loads(match.group(0))
    except ValueError as exc:
        raise LLMError(f"model returned invalid JSON: {exc}") from exc
    if not isinstance(out, dict):
        raise LLMError("model JSON is not an object")
    return out

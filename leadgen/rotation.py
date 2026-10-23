"""Free-cap rotation: use each provider until its allowance is spent, then the next.

Order comes from the capability registry (recurring allowances first, one-time
credits held as reserve, then priority). A provider is skipped when its secret
is missing, its counted usage has reached its cap for the period, or it has
reported quota exhaustion this period. Card-on-file providers stop at 90% of
the free allowance, trust the higher of our count and the provider's own usage
figure, and are disabled for the period on any billing signal. Non-quota errors
fall through to the next provider for that one call without marking anything.
"""
from __future__ import annotations

import logging
from collections import Counter

import httpx

from .providers import BillingSignal, ProviderError, QuotaExhausted

log = logging.getLogger(__name__)


class NoProviderAvailable(Exception):
    """Every provider in the stage is out of budget or unconfigured. Stop the stage for today."""


class CallFailed(ProviderError):
    """Providers were available but each one errored on this input (e.g. the site is down)."""


BOOKKEEPING = {"name", "kind", "secret", "cap", "period", "unit", "pool", "card_on_file",
               "priority", "registry_name", "free_amount", "check_usage"}


class Rotation:
    def __init__(self, stage: str, entries: list[dict], registry: dict, secrets, ledger,
                 factory=None, on_exhausted=None):
        self.stage = stage
        self.entries = entries
        self.registry = registry
        self.secrets = secrets
        self.ledger = ledger
        self.factory = factory or (lambda cls, key, entry: cls(key=key, **_extra(entry))
                                   if key is not None else cls(**_extra(entry)))
        # on_exhausted(provider_name, reason, billing: bool) -> notify Marcus (deduped upstream)
        self.on_exhausted = on_exhausted or (lambda *a, **k: None)
        self._instances: dict[str, object] = {}
        self._usage_read: set[str] = set()
        self.used = Counter()          # units spent this run, per provider
        self.errors: list[str] = []    # human-readable, for the run report
        self.skipped: dict[str, str] = {}

    def _pool(self, entry: dict) -> str:
        return entry.get("pool") or entry["name"]

    def _available(self, entry: dict):
        name = entry["name"]
        if name not in self.registry:
            self.skipped[name] = "no adapter"
            return None
        if name in self._instances:
            inst = self._instances[name]
        else:
            key = None
            if entry.get("secret"):
                key = self.secrets.get(entry["secret"])
                if not key:
                    self.skipped[name] = f"no secret {entry['secret']}"
                    return None
            inst = self.factory(self.registry[name], key, entry)
            self._instances[name] = inst
        if "cap" in entry:
            pool, period = self._pool(entry), entry.get("period", "month")
            # Card-on-file and shared accounts: read the provider's own count before using it.
            if (entry.get("card_on_file") or entry.get("check_usage")) and name not in self._usage_read \
                    and hasattr(inst, "usage"):
                self._usage_read.add(name)
                try:
                    theirs = inst.usage()
                    if theirs is not None:
                        self.ledger.set_provider_usage(pool, period, int(theirs))
                except ProviderError as exc:
                    # Can't confirm their count: refuse a card-on-file provider rather than guess.
                    self.skipped[name] = f"usage check failed ({exc})"[:200]
                    return None
            usage = self.ledger.usage(pool, period)
            if usage.get("exhausted"):
                self.skipped[name] = ("disabled: billing signal" if usage.get("billing_signal")
                                      else "provider reported quota exhausted")
                return None
            used = max(usage.get("used", 0), usage.get("provider_reported", 0))
            if used >= entry["cap"]:
                self.skipped[name] = f"free cap reached ({used}/{entry['cap']})"
                return None
        return inst

    def remaining(self, entry: dict) -> int | None:
        if "cap" not in entry:
            return None
        usage = self.ledger.usage(self._pool(entry), entry.get("period", "month"))
        if usage.get("exhausted"):
            return 0
        return max(0, entry["cap"] - max(usage.get("used", 0), usage.get("provider_reported", 0)))

    def call(self, method: str, *args, kinds: list[str] | None = None):
        """Return (result, provider_name). Raises NoProviderAvailable when every one is spent."""
        entries = self.entries
        if kinds:  # e.g. maps first, then web, per the campaign's preference
            entries = sorted((e for e in entries if e.get("kind") in kinds),
                             key=lambda e: kinds.index(e["kind"]))
        attempted = 0
        for entry in entries:
            inst = self._available(entry)
            if inst is None:
                continue
            name = entry["name"]
            attempted += 1
            try:
                result, units = getattr(inst, method)(*args)
            except BillingSignal as exc:
                log.error("%s/%s billing signal: %s", self.stage, name, exc)
                self.ledger.mark_exhausted(self._pool(entry), entry.get("period", "month"), str(exc),
                                           billing=True)
                self.skipped[name] = "disabled: billing signal"
                self.on_exhausted(name, str(exc), True)
                continue
            except QuotaExhausted as exc:
                log.warning("%s/%s exhausted: %s", self.stage, name, exc)
                self.ledger.mark_exhausted(self._pool(entry), entry.get("period", "month"), str(exc))
                self.skipped[name] = "provider reported quota exhausted"
                self.on_exhausted(name, str(exc), False)
                continue
            except ProviderError as exc:
                log.warning("%s/%s failed: %s", self.stage, name, exc)
                self.errors.append(f"{name}: {exc}"[:300])
                continue
            except httpx.HTTPError as exc:  # timeouts, resets: this provider failed this call
                log.warning("%s/%s network error: %s", self.stage, name, type(exc).__name__)
                self.errors.append(f"{name}: {type(exc).__name__}")
                continue
            if "cap" in entry:
                self.ledger.add_usage(self._pool(entry), entry.get("period", "month"), units)
            self.used[name] += units
            return result, name
        # Quota exhaustion during this call also counts as "not available", so only
        # plain errors from providers that still had budget make it a per-input failure.
        live = [e for e in entries if e["name"] not in self.skipped]
        if attempted and live:
            raise CallFailed(f"{self.stage}: every provider failed on {args[:1]}")
        raise NoProviderAvailable(f"{self.stage}: no provider left ({self.skipped})")

    def all_spent(self) -> bool:
        return all(self._available(e) is None for e in self.entries)


def _extra(entry: dict) -> dict:
    # Pass provider-specific options (e.g. Apify actor) but not rotation bookkeeping keys.
    return {k: v for k, v in entry.items() if k not in BOOKKEEPING}

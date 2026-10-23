"""Firecrawl's own credit figure. The account is shared with other tools (other tools on
the same account), so our monthly counter alone would overspend it: the rotation reads
GET /v2/team/credit-usage (free; observed live 2026-10-23: data.remainingCredits) and
trusts whichever usage is higher, ours or plan_credits - remainingCredits.
"""
from __future__ import annotations

from .base import ProviderError, check_response, http_client


class FirecrawlUsage:
    key: str
    plan_credits: int = 1000

    def usage(self) -> int | None:
        with http_client(timeout=30) as c:
            resp = c.get("https://api.firecrawl.dev/v2/team/credit-usage",
                         headers={"Authorization": f"Bearer {self.key}"})
        data = check_response(resp, "firecrawl usage")
        remaining = (data.get("data") or {}).get("remainingCredits")
        if remaining is None:
            raise ProviderError("firecrawl usage: no remainingCredits in reply")
        return max(0, int(self.plan_credits - remaining))

"""#8 hiring_now: local businesses in the trade with an open job posting.

Source: HasData's Indeed listing. SerpApi google_jobs was the first choice, but no SerpApi key
is connected; HasData is on the same shared-credit rule. Each posting's company is matched to a
map listing with a website (one small maps search per company), and only matched listings go
on. The writer may cite the job title as posted, and nothing else from the posting.
"""
from __future__ import annotations

from datetime import date, timedelta

from ..pilot_nosite import names_match
from ..providers import ProviderError
from .base import Strategy

COMPANIES_PER_CELL = 6


class HiringNow(Strategy):

    def find(self, p, c, cell, limit):
        jobs, provider = p.e.signal("jobs").call("jobs", cell["query"], cell["geo"])
        oldest = (date.today() - timedelta(days=self.params["max_age_days"])).isoformat()
        by_company = {}
        for j in jobs:
            if j["sponsored"] or (j["posted"] and j["posted"] < oldest):
                continue
            by_company.setdefault(j["company"].lower(), j)
        out = []
        for j in list(by_company.values())[:COMPANIES_PER_CELL]:
            try:
                cands, _ = p.e.discover.call("search", j["company"], cell["geo"], 3, kinds=["maps"])
            except ProviderError:
                continue
            match = next((x for x in cands if x.website and names_match(j["company"], x.name, need=0.75)), None)
            if match:
                match.raw = {**(match.raw or {}), "job": {"title": j["title"], "company": j["company"]}}
                out.append(match)
        return out[:limit], provider

    def sample_find(self, p, c, cell, limit):
        return self.find(p, c, cell, limit)

    def drop_listing(self, p, c, cand):
        return None if (cand.raw or {}).get("job") else "no matching job posting"

    def facts(self, p, c, cand, read):
        job = (cand.raw or {}).get("job")
        if not job:
            return []
        return [{"id": "hiring", "pillar": "Operations",
                 "text": f"I saw a job posting from {job['company']} for a {job['title']}."}]


STRATEGY = HiringNow

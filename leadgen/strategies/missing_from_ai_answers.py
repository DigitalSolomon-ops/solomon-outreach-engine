"""#10 missing_from_ai_answers: not named when an AI search tool answers "best <trade> in <town>".

One answer query per cell (Tavily include_answer first, Linkup sourcedAnswer as fallback). The leads are map
businesses in that cell that the answer does not name. Matching is lenient on purpose: a name
that partly appears counts as named and is dropped, so the claim "it did not mention you" is only
made when it is plainly true. The query, date, provider and the names the answer gave are kept
on the lead (fact evidence) so the claim is true for that run.
"""
from __future__ import annotations

import re
from datetime import date

from ..pilot_nosite import names_match
from ._util import city_of, long_date
from .base import Strategy

BOLD = re.compile(r"\*\*([^*]{2,80})\*\*")


def answer_names(text: str) -> list[str]:
    """Business names the answer called out in bold, for the evidence record."""
    out = []
    for n in BOLD.findall(text or ""):
        n = n.strip(" .,")
        if n and n.lower() not in {x.lower() for x in out}:
            out.append(n)
    return out[:30]


class MissingFromAiAnswers(Strategy):

    def __init__(self, entry, params):
        super().__init__(entry, params)
        self.answers: dict[str, dict] = {}

    def ask(self, p, cell) -> dict:
        key = f"{cell['query']}|{cell['geo']}"
        if key not in self.answers:
            q = f"What are the best {cell['query']} businesses in {cell['geo']}?"
            res, provider = p.e.signal("answer").call("answer", q)
            self.answers[key] = {"query": q, "date": date.today().isoformat(), "provider": provider,
                                 "town": city_of(cell["geo"]), "answer": res["answer"],
                                 "names": answer_names(res["answer"])}
        return self.answers[key]

    def _tag(self, p, cell, found):
        ans = self.ask(p, cell)
        cands, provider = found
        for x in cands:
            x.raw = {**(x.raw or {}), "ai_answer": ans}
        return cands, provider

    def find(self, p, c, cell, limit):
        self.ask(p, cell)   # the answer first: no answer, no maps search
        return self._tag(p, cell, super().find(p, c, cell, limit))

    def sample_find(self, p, c, cell, limit):
        self.ask(p, cell)
        return self._tag(p, cell, super().sample_find(p, c, cell, limit))

    def drop_listing(self, p, c, cand):
        ans = (cand.raw or {}).get("ai_answer")
        if not ans or not ans["answer"].strip():
            return "no AI answer for this town"
        if self.named(cand.name, ans):
            return "named in the AI answer"
        return None

    @staticmethod
    def named(name: str, ans: dict) -> bool:
        """Lenient: half of the name's own words appear in the answer. The town and the trade
        words are in every answer, so they don't count; a name made only of them counts as named
        (we can't tell, so no claim is made)."""
        common = set(re.findall(r"[a-z0-9]+", (ans["query"] + " " + ans["town"]).lower()))
        common |= {w.rstrip("s") for w in common} | {w + "s" for w in common}
        common |= {"nc", "sc", "llc", "inc", "co", "the", "and", "of", "auto", "car", "mobile", "services", "service"}
        own = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) > 1 and w not in common]
        if not own:
            return True
        return names_match(" ".join(own), ans["answer"], need=0.5)

    def facts(self, p, c, cand, read):
        ans = (cand.raw or {}).get("ai_answer")
        if not ans:
            return []
        when = long_date(date.fromisoformat(ans["date"]))
        trade = c["icp"]["cells"][0]["segment"].lower() if c["icp"]["cells"] else "businesses"
        return [{"id": "ai_answer_absent", "pillar": "Intelligence",
                 "text": f"On {when} I asked an AI search tool for the best {trade} in {ans['town']}, "
                         "and its answer did not mention your business.",
                 "evidence": {k: ans[k] for k in ("query", "date", "provider", "names")}}]


STRATEGY = MissingFromAiAnswers

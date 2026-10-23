"""Enrichment v2 and the lead brief.

Everything the writer may say about a business is an EVIDENCE entry on the lead
record: a key ("fact:no_chat", "maps:rating", "site:years", "owner:first_name")
and the text it stands for. The brief must cite an evidence key for every
claim; code rejects uncited claims, and the validator later checks that every
personal detail in an email traces back to one.

Google Places content is used in the moment only: from Google we keep the
place_id and our own derived observations, never its rating/review text.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from .llm import STR, STRS, BudgetStop, LLMError, obj

log = logging.getLogger(__name__)

TECH = {
    # vendor: (markers, category)
    "WordPress": (("wp-content", "wp-includes", 'content="wordpress'), "cms"),
    "Wix": (("wixstatic.com", "x-wix", "wix.com website builder"), "cms"),
    "Squarespace": (("squarespace.com", "static1.squarespace"), "cms"),
    "GoDaddy Website Builder": (("img1.wsimg.com", "godaddy website builder"), "cms"),
    "Shopify": (("cdn.shopify.com",), "cms"),
    "Webflow": (("webflow.com", "w-webflow"), "cms"),
    "Duda": (("dudaone", "multiscreensite"), "cms"),
    "GoHighLevel": (("leadconnectorhq.com", "msgsndr", "gohighlevel"), "crm"),
    "HubSpot": (("js.hs-scripts.com", "hs-analytics", "hubspot.com"), "crm"),
    "Calendly": (("calendly.com",), "booking"),
    "Acuity": (("acuityscheduling",), "booking"),
    "Housecall Pro": (("housecallpro",), "booking"),
    "ServiceTitan": (("servicetitan",), "booking"),
    "Jobber": (("getjobber", "jobber.com"), "booking"),
    "Podium": (("podium.com", "podium"), "chat"),
    "Birdeye": (("birdeye",), "chat"),
    "Intercom": (("intercom",), "chat"),
    "Tidio": (("tidio",), "chat"),
    "Tawk.to": (("tawk.to",), "chat"),
    "Drift": (("drift.com",), "chat"),
    "LiveChat": (("livechatinc",), "chat"),
    "Google Analytics": (("googletagmanager.com", "google-analytics.com"), "analytics"),
    "Meta Pixel": (("connect.facebook.net",), "analytics"),
    "CallRail": (("callrail",), "analytics"),
}
SOCIAL = {"facebook": "facebook.com/", "instagram": "instagram.com/", "linkedin": "linkedin.com/",
          "youtube": "youtube.com/", "tiktok": "tiktok.com/@", "x": "twitter.com/", "nextdoor": "nextdoor.com/"}
YEARS_RE = re.compile(r"\b(?:since|established|est\.?|founded in|serving [^.]{0,40} since)\s+(19[5-9]\d|20[0-2]\d)\b", re.I)
YEARS_N_RE = re.compile(r"\b(?:over|more than)?\s*(\d{1,2})\+?\s+years?\s+(?:of\s+)?(?:experience|in business|serving)", re.I)


def tech_stack(pages) -> list[dict]:
    html = "\n".join((p.html or "") for p in pages if p.raw_html).lower()
    out = []
    for vendor, (markers, cat) in TECH.items():
        if any(m in html for m in markers):
            out.append({"vendor": vendor, "category": cat})
    return out


def socials(pages) -> dict:
    found = {}
    for p in pages:
        for link in p.links + re.findall(r'href=["\']([^"\']+)', p.html or ""):
            low = link.lower()
            for net, marker in SOCIAL.items():
                if marker in low and net not in found and "sharer" not in low and "/share" not in low:
                    found[net] = link[:200]
    return found


def years_in_business(text: str, today: date | None = None) -> dict | None:
    today = today or date.today()
    m = YEARS_RE.search(text)
    if m:
        year = int(m.group(1))
        if 1950 <= year <= today.year:
            return {"since": year, "sentence": _sentence(text, m.start())}
    m = YEARS_N_RE.search(text)
    if m and 1 <= int(m.group(1)) <= 80:
        return {"years": int(m.group(1)), "sentence": _sentence(text, m.start())}
    return None


def _sentence(text: str, i: int) -> str:
    start = max(text.rfind(".", 0, i), text.rfind("\n", 0, i)) + 1
    end_candidates = [x for x in (text.find(".", i), text.find("\n", i)) if x != -1]
    end = min(end_candidates) if end_candidates else len(text)
    return re.sub(r"\s+", " ", text[start:end + 1]).strip()[:240]


def page_text(pages) -> str:
    parts = []
    for p in pages:
        if p.markdown:
            parts.append(p.markdown)
        else:
            t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", p.html or "", flags=re.S | re.I)
            parts.append(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"[ \t]+", " ", "\n".join(parts))


OWNER_SYSTEM = """You read text from a small business's own website (about/contact pages).
Find the owner's or founder's FIRST NAME only if the text states it plainly (e.g. "Owner John Smith", "founded by Maria").
Return JSON only: {"first_name": "...", "role": "owner|founder|president|other", "source_sentence": "exact sentence copied from the text"}
If the text does not clearly name an owner or founder, return {"first_name": "", "role": "", "source_sentence": ""}. Never guess."""


def owner_name(llm, text: str, campaign_id: str | None) -> dict | None:
    """Haiku extracts; code keeps it only if the quoted sentence is really in the text and names them."""
    if not llm or not llm.available or not re.search(r"\b(owner|founder|founded|president|proprietor)\b", text, re.I):
        return None
    snippet = text[:6000]
    try:
        out = llm.json("haiku", OWNER_SYSTEM, snippet, purpose="enrich_owner",
                       campaign_id=campaign_id, max_tokens=600, schema=OWNER_SCHEMA)
    except (LLMError, BudgetStop) as exc:
        log.info("owner extraction skipped: %s", exc)
        return None
    name, sent = (out.get("first_name") or "").strip(), (out.get("source_sentence") or "").strip()
    norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()
    if not name or not sent or norm(sent) not in norm(snippet) or name.lower() not in sent.lower():
        return None
    if not re.fullmatch(r"[A-Z][a-zA-Z'\-]{1,20}", name):
        return None
    return {"first_name": name, "role": out.get("role", ""), "source_sentence": sent[:240]}


def build_evidence(lead: dict, cand=None, transient_reviews: list[str] | None = None) -> dict:
    """Flatten everything we know into citable evidence entries."""
    ev = {}
    for f in lead.get("facts", []):
        ev[f"fact:{f['id']}"] = f["text"]
    maps = lead.get("maps") or {}
    if maps.get("rating") is not None:
        ev["maps:rating"] = f"Their map listing shows a {maps['rating']} star rating."
    if maps.get("review_count"):
        ev["maps:review_count"] = f"Their map listing shows {maps['review_count']} reviews."
    if maps.get("categories"):
        ev["maps:categories"] = "Listed categories: " + ", ".join(maps["categories"][:4])
    for t in lead.get("tech", []):
        ev[f"site:tech:{t['vendor']}"] = f"Their website uses {t['vendor']} ({t['category']})."
    for net in (lead.get("social") or {}):
        ev[f"site:social:{net}"] = f"Their website links to a {net} page."
    yrs = lead.get("years_in_business")
    if yrs:
        ev["site:years"] = yrs["sentence"]
    owner = lead.get("owner")
    if owner:
        ev["owner:first_name"] = f"{owner['first_name']} ({owner.get('role') or 'owner'}): {owner['source_sentence']}"
    for i, r in enumerate((transient_reviews or [])[:5]):
        ev[f"review:{i}"] = r[:300]
    if lead.get("email_source"):
        ev["email_source"] = lead["email_source"]
    return ev


BRIEF_SYSTEM = """You prepare a short research brief for one cold email to a local business.
Offer pillars: {pillars}.
You get the offer facts and a list of EVIDENCE entries (key: text) about this business.
Every claim you make must cite an evidence key from the list. Do not invent anything.
Return JSON only:
{{"pillar": "one pillar name", "pillar_why": "one sentence",
  "strongest_observation": {{"text": "...", "evidence": "<key>"}},
  "opening_angle": "one sentence on how to open",
  "claims": [{{"text": "...", "evidence": "<key>"}}],
  "do_not_say": ["things the email must not say or imply"]}}"""

PILLARS = ("Presence", "Conversation", "Intelligence", "Operations")


CLAIM = obj({"text": STR, "evidence": STR})
BRIEF_SCHEMA = obj({"pillar": {"type": "string", "enum": list(PILLARS)}, "pillar_why": STR,
                    "strongest_observation": CLAIM, "opening_angle": STR,
                    "claims": {"type": "array", "items": CLAIM}, "do_not_say": STRS})
OWNER_SCHEMA = obj({"first_name": STR, "role": STR, "source_sentence": STR})


class BriefError(ValueError):
    pass


def validate_brief(brief: dict, evidence: dict) -> list[str]:
    problems = []
    if brief.get("pillar") not in PILLARS:
        problems.append(f"pillar must be one of {PILLARS}")
    so = brief.get("strongest_observation") or {}
    if so.get("evidence") not in evidence:
        problems.append("strongest_observation cites no known evidence key")
    for c in brief.get("claims") or []:
        if not isinstance(c, dict) or c.get("evidence") not in evidence:
            problems.append(f"uncited claim: {str(c.get('text') if isinstance(c, dict) else c)[:80]}")
    if not isinstance(brief.get("do_not_say", []), list):
        problems.append("do_not_say must be a list")
    return problems


def fallback_brief(lead: dict, evidence: dict) -> dict:
    """Deterministic brief from the observation facts, used when the model's brief won't validate."""
    order = ["fact:after_hours", "fact:no_chat", "fact:no_booking", "fact:no_form", "fact:no_schema"]
    key = next((k for k in order if k in evidence), next(iter(evidence), None))
    pillar = next((f["pillar"] for f in lead.get("facts", []) if f"fact:{f['id']}" == key), "Conversation")
    return {"pillar": pillar, "pillar_why": "Chosen from the strongest site observation.",
            "strongest_observation": {"text": evidence.get(key, ""), "evidence": key} if key else {},
            "opening_angle": "Open with what I saw on their website.",
            "claims": [{"text": evidence[key], "evidence": key}] if key else [],
            "do_not_say": ["that the business is losing money or customers", "any number not in the facts"],
            "source": "fallback"}


def lead_brief(llm, campaign: dict, lead: dict, evidence: dict) -> dict:
    if not evidence:
        raise BriefError("no evidence to brief from")
    if not llm or not llm.available:
        return fallback_brief(lead, evidence)
    system = BRIEF_SYSTEM.format(pillars=", ".join(PILLARS))
    user = "\n".join([f"Business: {lead['name']} ({lead.get('segment')}, {lead.get('geo')})",
                      "Offer: " + campaign["offer"]["name"],
                      "Offer facts:", *[f"- {f}" for f in campaign["offer"]["facts"]],
                      "EVIDENCE:", *[f"- {k}: {v}" for k, v in evidence.items()]])
    feedback = ""
    for _ in range(2):
        try:
            brief = llm.json("haiku", system, user + feedback, purpose="brief",
                             campaign_id=campaign["id"], max_tokens=1500, schema=BRIEF_SCHEMA)
        except LLMError as exc:
            feedback = f"\n\nYour last answer failed: {exc}. Return JSON only."
            continue
        problems = validate_brief(brief, evidence)
        if not problems:
            brief["source"] = "model"
            return brief
        feedback = "\n\nRejected: " + "; ".join(problems) + ". Cite only keys from EVIDENCE."
    return fallback_brief(lead, evidence)

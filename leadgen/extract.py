"""Read a prospect's own site: published email, observable signals, and do-not-harvest notices.

Only addresses the business publishes on its own website are used. No guessing
of name@ patterns: a guessed address is not one the business chose to publish,
and it bounces more.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from .providers.base import Page, registrable_domain

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.I)

JUNK_LOCAL = {"noreply", "no-reply", "donotreply", "do-not-reply", "example", "test", "your",
              "yourname", "name", "email", "user", "privacy", "abuse", "postmaster", "webmaster",
              "careers", "jobs", "hr", "billing", "accounting", "ap", "invoices"}
JUNK_DOMAINS = {"example.com", "domain.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
                "godaddy.com", "squarespace.com", "wordpress.com", "email.com", "yourdomain.com",
                "mysite.com", "company.com"}
JUNK_TLDS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js"}
PREFERRED_LOCAL = ("owner", "info", "office", "contact", "hello", "service", "sales", "admin",
                   "frontdesk", "appointments", "team")

# CAN-SPAM treats harvesting from a site that says its addresses are not to be
# used for email as an aggravated violation. Any such notice = skip the business.
HARVEST_NOTICE = re.compile(
    r"(not\s+(?:be\s+)?(?:used|harvested|collected)[^.]{0,60}(?:e-?mail|commercial|solicit)"
    r"|do\s+not\s+(?:send\s+)?(?:unsolicited|spam|solicit)"
    r"|no\s+(?:unsolicited|solicitations?)"
    r"|(?:harvest|collect)ing\s+(?:of\s+)?e-?mail\s+addresses\s+(?:is\s+)?prohibited)",
    re.I)

CHAT_MARKERS = ("intercom", "drift.com", "tidio", "livechatinc", "tawk.to", "crisp.chat",
                "hubspot.com/conversations", "zendesk", "olark", "podium", "birdeye",
                "leadconnectorhq.com/loader", "msgsndr", "widgets.leadconnectorhq", "freshchat",
                "smith.ai", "ngage", "apexchat", "chatbot", "webchat")
BOOKING_MARKERS = ("calendly.com", "acuityscheduling", "squareup.com/appointments", "vagaro",
                   "mindbodyonline", "housecallpro", "servicetitan", "jobber", "booksy",
                   "schedulicity", "zocdoc", "nexhealth", "localmed", "setmore", "book online",
                   "book now", "schedule online", "request an appointment")
FORM_MARKERS = ("<form", "wpcf7", "gravityforms", "hs-form", "typeform", "jotform")
AFTER_HOURS = re.compile(r"24\s*/\s*7|24\s*hours?\b|around the clock|after[-\s]hours", re.I)


@dataclass
class SiteRead:
    domain: str
    emails: list[str] = field(default_factory=list)
    best_email: str | None = None
    harvest_notice: bool = False
    facts: list[dict] = field(default_factory=list)   # {"id","text","pillar"}
    pages: list[str] = field(default_factory=list)


def contact_page(home: Page) -> str | None:
    """Pick the site's own contact page from the homepage links, if there is one."""
    base = registrable_domain(home.url)
    for word in ("contact", "about"):
        for link in home.links:
            absolute = urljoin(home.url, link)
            if word in urlparse(absolute).path.lower() and registrable_domain(absolute) == base:
                return absolute
    return None


def find_emails(page: Page) -> list[str]:
    text = htmllib.unescape(page.html or "") + "\n" + (page.markdown or "")
    found = MAILTO_RE.findall(text) + EMAIL_RE.findall(text)
    out = []
    for raw in found:
        email = raw.strip().strip(".").lower()
        if "%" in email:
            continue
        local, _, dom = email.partition("@")
        if not local or not dom or dom.rsplit(".", 1)[-1] in JUNK_TLDS:
            continue
        if local in JUNK_LOCAL or dom in JUNK_DOMAINS or re.search(r"@\d+x\.", email):
            continue
        if email not in out:
            out.append(email)
    return out


def choose_email(emails: list[str], site_domain: str) -> str | None:
    """Prefer an address on the business's own domain, then a preferred role, then any."""
    def rank(e: str):
        local, _, dom = e.partition("@")
        on_domain = dom == site_domain or dom.endswith("." + site_domain)
        role = next((i for i, p in enumerate(PREFERRED_LOCAL) if local.startswith(p)), 50)
        return (0 if on_domain else 1, role, len(e))
    return min(emails, key=rank) if emails else None


def _has_localbusiness_schema(html: str) -> bool:
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                t = node.get("@type")
                types = t if isinstance(t, list) else [t]
                if any(isinstance(x, str) and ("LocalBusiness" in x or x in {
                        "Dentist", "Plumber", "HVACBusiness", "RoofingContractor", "LegalService",
                        "Attorney", "InsuranceAgency", "HealthAndBeautyBusiness", "MedicalClinic",
                        "HomeAndConstructionBusiness", "DaySpa"}) for x in types):
                    return True
                stack.extend(node.get("@graph", []) if isinstance(node.get("@graph"), list) else [])
    return False


def observe(pages: list[Page]) -> list[dict]:
    """Facts we can state truthfully because we saw them (or did not see them) on the site.

    Absence facts are worded as what we saw ("I did not see"), never as a claim
    about the business, because a widget can load late or sit on another page.
    """
    raw_html = "\n".join((p.html or "") for p in pages)
    html = raw_html.lower()  # for marker matching only; schema types are case-sensitive
    text = "\n".join((p.markdown or p.html or "") for p in pages)
    facts = []
    if AFTER_HOURS.search(text):
        facts.append({"id": "after_hours", "pillar": "Conversation",
                      "text": "The website says the business is available 24/7 or after hours."})
    # Absence claims need the page as a browser got it. Simplified or markdown-only
    # output has scripts stripped, which would make every widget look missing.
    if "<script" not in html:
        return facts
    if not any(m in html for m in CHAT_MARKERS):
        facts.append({"id": "no_chat", "pillar": "Conversation",
                      "text": "When I looked at the website I did not see a way to chat or text with the business from the page."})
    if not any(m in html for m in BOOKING_MARKERS):
        facts.append({"id": "no_booking", "pillar": "Conversation",
                      "text": "I did not see a way to book or request an appointment online on the pages I looked at."})
    if not _has_localbusiness_schema(raw_html):
        facts.append({"id": "no_schema", "pillar": "Presence",
                      "text": "The pages I looked at do not include the structured business information (schema markup) that search engines and AI assistants read to describe a business."})
    if not any(m in html for m in FORM_MARKERS):
        facts.append({"id": "no_form", "pillar": "Conversation",
                      "text": "I did not see a contact form on the pages I looked at."})
    return facts


def read_site(pages: list[Page]) -> SiteRead:
    domain = registrable_domain(pages[0].url) if pages else ""
    sr = SiteRead(domain=domain, pages=[p.url for p in pages])
    joined = "\n".join((p.markdown or "") + "\n" + re.sub(r"<[^>]+>", " ", p.html or "") for p in pages)
    sr.harvest_notice = bool(HARVEST_NOTICE.search(joined))
    for p in pages:
        for e in find_emails(p):
            if e not in sr.emails:
                sr.emails.append(e)
    sr.best_email = choose_email(sr.emails, domain)
    sr.facts = observe(pages)
    return sr

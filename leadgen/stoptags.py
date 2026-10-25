"""Stop tags: the funnel tags a GHL contact presence:booked or presence:customer; leadgen reads them.

Checked before EVERY send (first touch, follow-up, AI reply, Email 2) in every campaign. A tagged
contact is suppressed exactly like an opt-out, so the check also sticks after the first hit.

The funnel tags the contact whose email booked or paid, which can differ from the address we wrote to
(an owner pays from a personal address). It also writes the preview id to the contact field PW Preview ID,
so once a lead has a preview, any contact carrying that preview id and a stop tag stops the lead too
(PREVIEW-API.md, "Stop tags").

Fail closed: if GHL can't be read, the send is held this tick (never suppressed, never sent).
The email address travels in a POST body, never a URL, so it never lands in request logs.
"""
from __future__ import annotations

import logging

import httpx

from .ghl import BASE

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


class StopTagUnavailable(Exception):
    """GHL could not be read; the caller holds the send."""


def _search(creds: dict, http, field: str, value: str) -> list[dict]:
    try:
        resp = http.post(f"{BASE}/contacts/search", timeout=20,
                         headers={"Authorization": f"Bearer {creds['token']}",
                                  "Version": "2021-07-28", "Accept": "application/json"},
                         json={"locationId": creds["location_id"], "pageLimit": 20,
                               "filters": [{"field": field, "operator": "eq", "value": value}]})
    except httpx.HTTPError as exc:
        raise StopTagUnavailable(f"GHL search failed: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        raise StopTagUnavailable(f"GHL search HTTP {resp.status_code}")
    return (resp.json() or {}).get("contacts") or []


def ghl_lookup(creds: dict, client=None):
    """email -> list of tags on the GHL contact ([] when there is no contact)."""
    http = client or httpx

    def lookup(email: str) -> list[str]:
        tags = []
        for contact in _search(creds, http, "email", email):
            if (contact.get("email") or "").lower() == email.lower():
                tags.extend(contact.get("tags") or [])
        return tags
    return lookup


def ghl_preview_lookup(creds: dict, field_id: str, client=None):
    """preview_id -> tags on every contact whose PW Preview ID field equals it (filter customFields.<id>,
    verified live 2026-10-23: a real field id returns 200, an unknown one 422)."""
    http = client or httpx

    def lookup(preview_id: str) -> list[str]:
        tags = []
        for contact in _search(creds, http, f"customFields.{field_id}", preview_id):
            tags.extend(contact.get("tags") or [])
        return tags
    return lookup


class StopTags:
    def __init__(self, settings: dict, ledger, lookup=None, preview_lookup=None):
        cfg = settings.get("stop_tags") or {}
        self.tags = {t.lower() for t in cfg.get("tags", ["presence:booked", "presence:customer"])}
        self.ledger = ledger
        self.lookup = lookup            # None = no GHL credentials: every check is unavailable
        self.preview_lookup = preview_lookup
        self.cache: dict[str, str | None] = {}

    def check(self, email: str, preview_id: str | None = None) -> str | None:
        """Returns the stop tag found (and suppresses the address), or None if clear to send.
        Raises StopTagUnavailable when GHL can't answer."""
        email = email.strip().lower()
        key = f"{email}|{preview_id or ''}"
        if key in self.cache:
            return self.cache[key]
        if self.lookup is None:
            raise StopTagUnavailable("no GHL credentials for the stop-tag check")
        hit = next((t for t in self.lookup(email) if t.lower() in self.tags), None)
        if not hit and preview_id:
            if self.preview_lookup is None:
                raise StopTagUnavailable("no GHL preview-id lookup configured")
            hit = next((t for t in self.preview_lookup(preview_id) if t.lower() in self.tags), None)
        if hit:
            self.ledger.suppress(email, f"GHL tag {hit}", "ghl")
            self.ledger.event("stop_tag", f"Stopped all sends to a contact tagged {hit}.",
                              why="The funnel tagged them booked or customer; suppressed like an opt-out.")
        self.cache[key] = hit
        return hit

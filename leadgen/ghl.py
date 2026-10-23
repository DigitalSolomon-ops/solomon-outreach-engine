"""Optional: keep the record in GHL. GHL keeps the record; this job keeps the logic."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)
BASE = "https://services.leadconnectorhq.com"


def upsert_contact(creds: dict, lead: dict, tags: list[str]) -> str | None:
    """Create or update the contact. Returns the contact id, or None on failure (logged, not fatal)."""
    body = {
        "locationId": creds["location_id"],
        "email": lead["email"],
        "companyName": lead["name"][:100],
        "website": lead["website"],
        "phone": lead.get("phone") or None,
        "address1": lead.get("address") or None,
        "tags": tags + [f"leadgen-seg-{lead['segment'].lower().replace(' ', '-')}"],
        "source": "Solomon Leadgen",
    }
    try:
        resp = httpx.post(f"{BASE}/contacts/upsert", timeout=30,
                          headers={"Authorization": f"Bearer {creds['token']}",
                                   "Version": "2021-07-28", "Accept": "application/json"},
                          json={k: v for k, v in body.items() if v is not None})
    except httpx.HTTPError as exc:
        log.warning("GHL upsert failed for %s: %s", lead["domain"], type(exc).__name__)
        return None
    if resp.status_code >= 400:
        log.warning("GHL upsert HTTP %s for %s: %s", resp.status_code, lead["domain"], resp.text[:200])
        return None
    return (resp.json().get("contact") or {}).get("id")

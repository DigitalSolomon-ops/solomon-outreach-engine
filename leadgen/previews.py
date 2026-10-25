"""Preview requests: a preview_yes reply asks the website funnel to build the prospect's preview.

The funnel owns building and hosting (pw-site-factory, contract in its site-factory/previews.mjs and
service.mjs). Leadgen only calls it:

  POST {base}/previews        X-Preview-Key; 202 {preview_id, status} (200 when already ready).
                              Same lead_id -> same preview_id.
  GET  {base}/previews/<id>   {status: building|ready|failed|expired, preview_url, screenshot_url,
                              checkout_url, expires_at, error}

The worker runs every tick over leadgen_preview_queue:
  queued     suppression and stop tags first, then exactly one POST -> requested
  requested  GET -> ready (urls stored with leadgen's UTMs)
                 -> failed / expired, or not ready 15 minutes after the yes -> held, Marcus emailed
  ready      Email 2 in the same thread (email2.py) -> sent. While previews.email2_live is false it is
             rendered and emailed to Marcus for review instead, and waits here.
Ratings and reviews are never sent: the body is built from a whitelist and checked again before POST.
No image or build cost is metered here; the funnel pays for its own builds.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from .sender import stop_tag_hold
from .store import lead_id, utcnow

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

QUEUE = "preview_queue"
OPEN = ("queued", "requested", "ready")
REVIEWISH = re.compile(r"rating|review|stars|testimonial", re.I)   # the same test the funnel applies


class PreviewError(Exception):
    """The API could not be reached or refused the request."""


class PreviewClient:
    def __init__(self, base: str, key: str, http=None, timeout: float = 30):
        self.base = base.rstrip("/")
        self.key = key
        self.http = http or httpx
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            resp = self.http.request(method, self.base + path, timeout=self.timeout, json=body,
                                     headers={"X-Preview-Key": self.key, "Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise PreviewError(f"{method} {path}: {type(exc).__name__}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            raise PreviewError(f"{method} {path}: HTTP {resp.status_code} {str(data.get('error', ''))[:200]}")
        return data

    def request(self, body: dict) -> dict:
        bad = review_keys(body)
        if bad:  # never reaches the wire
            raise PreviewError(f"refusing to send review fields: {bad}")
        return self._call("POST", "/previews", body)

    def status(self, preview_id: str) -> dict:
        return self._call("GET", f"/previews/{preview_id}")


def review_keys(v, path: str = "") -> list[str]:
    if isinstance(v, dict):
        return [k2 for k, x in v.items() for k2 in ([path + k] if REVIEWISH.search(k) else []) + review_keys(x, path + k + ".")]
    if isinstance(v, list):
        return [k for x in v for k in review_keys(x, path)]
    return []


def utms(c: dict, domain: str) -> dict:
    return {"utm_source": "leadgen", "utm_medium": "email", "utm_campaign": c["id"],
            "utm_content": domain, "utm_term": "preview"}


def with_utm(url: str, params: dict) -> str:
    if not url:
        return url
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q.update(params)
    return urlunparse(p._replace(query=urlencode(q)))


def _years(lead: dict) -> int | None:
    y = lead.get("years_in_business") or {}
    if y.get("years"):
        return int(y["years"])
    if y.get("since"):
        return max(0, utcnow().year - int(y["since"]))
    return None


def request_body(c: dict, lead: dict) -> dict:
    """The enrichment record in the API's shape. Whitelist only: no rating, no reviews, nothing Google
    would not let us keep (Google-sourced leads have no phone or hours stored, so none is sent)."""
    maps = lead.get("maps") or {}
    city = lead.get("geo") or ""
    body = {
        "lead_id": lead_id(c["id"], lead["domain"]),
        "campaign_id": c["id"],
        "industry": (c.get("preview") or {}).get("industry") or "_general",
        "business_name": (lead.get("name") or lead["domain"])[:120],
        "city": city,
        "service_area": [city.split(",")[0].strip()] if city else [],
        "services": [str(s) for s in (lead.get("services") or [])][:12],
        "phone": lead.get("phone") or None,
        "years_in_business": _years(lead),
        "hours": "; ".join(maps.get("hours") or []) or None,
        "current_website": lead.get("website") or None,
        "gbp_place_id": lead.get("place_id") or None,
        "utm": utms(c, lead["domain"]),
    }
    return {k: v for k, v in body.items() if v not in (None, [], "")}


class PreviewWorker:
    def __init__(self, settings: dict, ledger, client: PreviewClient | None, *, stop_tags=None, email2=None,
                 now=utcnow):
        self.cfg = settings["previews"]
        self.email2 = email2          # Email2Sender; None leaves ready previews waiting
        self.ledger = ledger
        self.client = client
        self.stop_tags = stop_tags
        self.now = now
        self.counts = {k: 0 for k in ("requested", "ready", "building", "held", "cancelled", "errors",
                                      "email2_sent", "email2_review")}

    def run(self) -> dict:
        for tid, item in self.ledger.s.all(QUEUE):
            if item.get("status") not in OPEN:
                continue
            try:
                self._step(tid, item)
            except PreviewError as exc:
                self.counts["errors"] += 1
                self._update(tid, item, {"last_error": str(exc)[:300]})
                if self._late(item):
                    self._hold(tid, item, f"the preview API kept failing: {exc}")
        return dict(self.counts)

    def _late(self, item: dict) -> bool:
        at = datetime.fromisoformat(item["requested_at"])
        return self.now() - at > timedelta(minutes=self.cfg["ready_timeout_minutes"])

    def _step(self, tid: str, item: dict):
        thread = self.ledger.thread(tid) or {}
        c = self.ledger.campaign(thread.get("campaign_id", "")) or {}
        lead = self.ledger.lead(c.get("id", ""), thread.get("domain", "")) or {}
        to = item.get("to") or thread.get("email")
        if self.ledger.is_suppressed(email=to, domain=thread.get("domain")):
            return self._cancel(tid, item, "suppressed")
        held = stop_tag_hold(self.stop_tags, to, item.get("preview_id"))
        if held == "tagged":
            return self._cancel(tid, item, "GHL stop tag")
        if held:
            raise PreviewError(held)
        if item["status"] == "ready":
            return self._deliver(tid, item)
        if self.client is None:
            raise PreviewError("preview API key or base URL not configured")
        if item["status"] == "queued":
            body = request_body(c, lead)
            out = self.client.request(body)
            item = self._update(tid, item, {"status": "requested", "preview_id": out["preview_id"],
                                            "api_requested_at": self.now().isoformat(),
                                            "industry_fallback": out.get("industry_fallback")})
            self.counts["requested"] += 1
        view = self.client.status(item["preview_id"])
        st = view.get("status")
        if st == "ready":
            u = utms(c, thread.get("domain", ""))
            self._update(tid, item, {"status": "ready", "ready_at": self.now().isoformat(),
                                     "preview_url": with_utm(view.get("preview_url"), u),
                                     "screenshot_url": view.get("screenshot_url"),
                                     "checkout_url": with_utm(view.get("checkout_url"), u),
                                     "expires_at": view.get("expires_at")})
            self.counts["ready"] += 1
            self._deliver(tid, self.ledger.s.get(QUEUE, tid))
        elif st in ("failed", "expired"):
            self._hold(tid, item, f"the preview {st}: {view.get('error') or 'no detail'}")
        elif self._late(item):
            self._hold(tid, item, f"the preview was not ready {self.cfg['ready_timeout_minutes']} minutes after the yes")
        else:
            self.counts["building"] += 1

    def _deliver(self, tid: str, item: dict):
        if self.email2 is None:
            return
        outcome, why = self.email2.send(tid, item, self.stop_tags)
        if outcome == "sent":
            self._update(tid, item, {"status": "sent", "email2_message_id": why, "sent_at": self.now().isoformat()})
            self.counts["email2_sent"] += 1
            self.ledger.event("email2_sent", f"Email 2 with the preview went to {item.get('domain')}.",
                              why="They said yes and the preview was ready.", cost_impact="none",
                              campaign_id=item.get("campaign_id"), thread_id=tid)
        elif outcome == "review":
            if not item.get("review_notified"):
                self._update(tid, item, {"review_notified": True, "email2_draft": why})
                self.ledger.event("email2_review", f"Email 2 for {item.get('domain')} is ready for your review; not sent.",
                                  why="previews.email2_live is off, so Email 2 waits for you.", cost_impact="none",
                                  campaign_id=item.get("campaign_id"), notify=True, thread_id=tid,
                                  detail=why + f"\n\nScreenshot: {item.get('screenshot_url')}")
                self.counts["email2_review"] += 1
        elif outcome == "cancelled":
            self._cancel(tid, item, why)
        elif outcome == "held":
            self._hold(tid, item, why)
        # "wait": try again next tick

    def _update(self, tid: str, item: dict, fields: dict) -> dict:
        item = {**item, **fields}
        self.ledger.s.set(QUEUE, tid, item, merge=False)
        thread = self.ledger.thread(tid)
        if thread is not None:
            thread["preview"] = {**(thread.get("preview") or {}), **fields}
            self.ledger.save_thread(tid, thread)
        if item.get("campaign_id") and item.get("domain"):
            self.ledger.save_lead(item["campaign_id"], item["domain"], {"preview": thread["preview"] if thread else item})
        return item

    def _cancel(self, tid: str, item: dict, why: str):
        self._update(tid, item, {"status": "cancelled", "cancelled_reason": why})
        self.counts["cancelled"] += 1

    def _hold(self, tid: str, item: dict, why: str):
        """No Email 2. The thread is held for Marcus, who is emailed."""
        self._update(tid, item, {"status": "held", "held_reason": why})
        thread = self.ledger.thread(tid)
        if thread is not None:
            thread["state"] = "escalated"
            self.ledger.save_thread(tid, thread)
        self.ledger.event("preview_held", f"A preview for {item.get('domain')} needs you; Email 2 was not sent.",
                          why=why, cost_impact="none", campaign_id=item.get("campaign_id"), notify=True,
                          thread_id=tid, detail=f"Preview id: {item.get('preview_id') or '(not created)'}\n"
                                                f"The prospect said yes at {item.get('requested_at')}.")
        self.counts["held"] += 1

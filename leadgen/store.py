"""State: campaigns, leads, sends, threads, mailboxes, events, costs, usage, suppression, runs.

Two backends behind one small interface: Firestore (cloud) and a JSON file
(local dry runs and tests). Ledger holds the business rules on top of it.
Every collection is prefixed leadgen_ in Firestore; the engine owns that schema
and nothing else reads or writes it directly (the Creator goes through the API).
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

PREFIX = "leadgen_"

# Send record states. A record is created as "sending" BEFORE SMTP and marked
# "sent" after. A "sending" record older than STALE_SENDING is marked "unknown"
# and never retried: missing one email beats sending it twice.
SEND_STATES = ("sending", "sent", "unknown", "refused", "held")
STALE_SENDING = timedelta(minutes=10)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def email_key(email: str) -> str:
    # Suppression is keyed by a hash so the list itself holds no addresses in its ids.
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def msgid_key(message_id: str) -> str:
    return hashlib.sha256(message_id.strip().strip("<>").lower().encode()).hexdigest()[:40]


def _match(doc: dict, filters: list[tuple[str, str, object]]) -> bool:
    for f, op, v in filters:
        x = doc.get(f)
        if op == "==" and x != v:
            return False
        if op == "in" and x not in v:
            return False
        if op == ">=" and (x is None or str(x) < str(v)):
            return False
        if op == "<=" and (x is None or str(x) > str(v)):
            return False
    return True


class LocalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data: dict[str, dict[str, dict]] = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def _flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, default=str), encoding="utf-8")
        # Windows: a scanner or indexer briefly holding the file makes replace() fail. Retry, then raise.
        import time
        for attempt in range(20):
            try:
                tmp.replace(self.path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)

    def get(self, coll: str, doc_id: str) -> dict | None:
        d = self.data.get(coll, {}).get(doc_id)
        return json.loads(json.dumps(d, default=str)) if d is not None else None

    def set(self, coll: str, doc_id: str, doc: dict, merge: bool = True):
        doc = _to_fs(doc)
        with self._lock:
            c = self.data.setdefault(coll, {})
            c[doc_id] = {**c.get(doc_id, {}), **doc} if merge else dict(doc)
            self._flush()

    def create(self, coll: str, doc_id: str, doc: dict) -> bool:
        """Write only if absent. False means someone already holds this id."""
        with self._lock:
            c = self.data.setdefault(coll, {})
            if doc_id in c:
                return False
            c[doc_id] = _to_fs(doc)
            self._flush()
            return True

    def increment(self, coll: str, doc_id: str, fieldname: str, n: float):
        with self._lock:
            c = self.data.setdefault(coll, {})
            d = c.setdefault(doc_id, {})
            d[fieldname] = d.get(fieldname, 0) + n
            self._flush()

    def list_since(self, coll: str, fieldname: str, since: datetime) -> list[dict]:
        out = []
        for d in self.data.get(coll, {}).values():
            v = d.get(fieldname)
            if v and datetime.fromisoformat(str(v)) >= since:
                out.append(d)
        return out

    def first(self, coll: str, fieldname: str) -> dict | None:
        docs = [d for d in self.data.get(coll, {}).values() if d.get(fieldname)]
        return min(docs, key=lambda d: str(d[fieldname]), default=None)

    def where(self, coll: str, *filters) -> list[tuple[str, dict]]:
        return [(k, json.loads(json.dumps(v, default=str)))
                for k, v in self.data.get(coll, {}).items() if _match(v, list(filters))]

    def all(self, coll: str) -> list[tuple[str, dict]]:
        return self.where(coll)

    def lease(self, name: str, holder: str, ttl: timedelta, release: bool = False) -> bool:
        with self._lock:
            c = self.data.setdefault("locks", {})
            cur = c.get(name)
            now = utcnow()
            if release:
                if cur and cur.get("holder") == holder:
                    del c[name]
                    self._flush()
                return True
            if cur and cur.get("holder") != holder and datetime.fromisoformat(cur["until"]) > now:
                return False
            c[name] = {"holder": holder, "until": (now + ttl).isoformat()}
            self._flush()
            return True


class FirestoreStore:
    def __init__(self, project: str):
        from google.cloud import firestore
        self._fs = firestore
        self.db = firestore.Client(project=project)

    def _ref(self, coll, doc_id):
        return self.db.collection(PREFIX + coll).document(doc_id)

    def get(self, coll, doc_id):
        snap = self._ref(coll, doc_id).get()
        return snap.to_dict() if snap.exists else None

    def set(self, coll, doc_id, doc, merge=True):
        self._ref(coll, doc_id).set(_to_fs(doc), merge=merge)

    def create(self, coll, doc_id, doc) -> bool:
        from google.api_core import exceptions as gexc
        try:
            self._ref(coll, doc_id).create(_to_fs(doc))
            return True
        except gexc.AlreadyExists:
            return False

    def increment(self, coll, doc_id, fieldname, n):
        self._ref(coll, doc_id).set({fieldname: self._fs.Increment(n)}, merge=True)

    def list_since(self, coll, fieldname, since):
        q = self.db.collection(PREFIX + coll).where(
            filter=self._fs.FieldFilter(fieldname, ">=", since.isoformat()))
        return [s.to_dict() for s in q.stream()]

    def first(self, coll, fieldname):
        q = self.db.collection(PREFIX + coll).order_by(fieldname).limit(1)
        snaps = list(q.stream())
        return snaps[0].to_dict() if snaps else None

    def where(self, coll, *filters):
        q = self.db.collection(PREFIX + coll)
        for f, op, v in filters:
            q = q.where(filter=self._fs.FieldFilter(f, op, v))
        return [(s.id, s.to_dict()) for s in q.stream()]

    def all(self, coll):
        return [(s.id, s.to_dict()) for s in self.db.collection(PREFIX + coll).stream()]

    def lease(self, name, holder, ttl, release=False) -> bool:
        ref = self._ref("locks", name)
        if release:
            snap = ref.get()
            if snap.exists and snap.to_dict().get("holder") == holder:
                ref.delete()
            return True
        transaction = self.db.transaction()

        @self._fs.transactional
        def take(tx):
            snap = ref.get(transaction=tx)
            now = utcnow()
            if snap.exists:
                cur = snap.to_dict()
                if cur.get("holder") != holder and datetime.fromisoformat(cur["until"]) > now:
                    return False
            tx.set(ref, {"holder": holder, "until": (now + ttl).isoformat()})
            return True

        return take(transaction)


def _to_fs(doc: dict) -> dict:
    # Store datetimes as ISO strings so both backends compare the same way.
    def conv(v):
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v
    return {k: conv(v) for k, v in doc.items()}


def period_key(period: str, now: datetime | None = None) -> str:
    """Bucket a usage count belongs to. 'once' never resets: for one-time signup credits."""
    now = now or utcnow()
    if period == "day":
        return now.strftime("%Y-%m-%d")
    if period in ("month", "unknown"):
        return now.strftime("%Y-%m")
    if period in ("once", "unlimited"):
        return "lifetime"
    raise ValueError(f"unknown cap period {period!r} (use day, month or once)")


def lead_id(campaign_id: str, domain: str) -> str:
    return f"{campaign_id}__{domain}"


def send_id(campaign_id: str, domain: str, step: int) -> str:
    # Deterministic: the same lead + step can only ever hold one send record.
    return f"{campaign_id}:{domain}:{step}"


class Ledger:
    """Business rules over the store. Everything that must survive between runs lives here."""

    def __init__(self, store):
        self.s = store

    # --- provider budgets -------------------------------------------------
    def usage(self, provider: str, period: str) -> dict:
        return self.s.get("usage", f"{provider}_{period_key(period)}") or {}

    def add_usage(self, provider: str, period: str, n: int):
        if n:
            self.s.increment("usage", f"{provider}_{period_key(period)}", "used", n)

    def set_provider_usage(self, provider: str, period: str, n: int):
        """The provider's own count (usage API). The rotation trusts the higher of ours and theirs."""
        self.s.set("usage", f"{provider}_{period_key(period)}",
                   {"provider_reported": n, "provider_reported_at": utcnow().isoformat()})

    def mark_exhausted(self, provider: str, period: str, reason: str, billing: bool = False):
        self.s.set("usage", f"{provider}_{period_key(period)}",
                   {"exhausted": True, "exhausted_reason": reason[:300], "billing_signal": billing,
                    "exhausted_at": utcnow().isoformat()})

    # --- campaigns ----------------------------------------------------------
    def campaign(self, cid: str) -> dict | None:
        return self.s.get("campaigns", cid)

    def campaigns(self, *statuses: str) -> list[dict]:
        rows = self.s.all("campaigns")
        return [d for _, d in rows if not statuses or d.get("status") in statuses]

    def save_campaign(self, cid: str, doc: dict):
        self.s.set("campaigns", cid, {**doc, "id": cid, "updated_at": utcnow().isoformat()})

    # --- leads (one per campaign x domain) ----------------------------------
    def lead(self, campaign_id: str, domain: str) -> dict | None:
        return self.s.get("leads", lead_id(campaign_id, domain))

    def save_lead(self, campaign_id: str, domain: str, doc: dict):
        self.s.set("leads", lead_id(campaign_id, domain),
                   {**doc, "campaign_id": campaign_id, "domain": domain,
                    "updated_at": utcnow().isoformat()})

    def leads(self, campaign_id: str, *statuses: str) -> list[dict]:
        rows = self.s.where("leads", ("campaign_id", "==", campaign_id))
        return [d for _, d in rows if not statuses or d.get("status") in statuses]

    def domain_contacted_recently(self, domain: str, days: int) -> bool:
        """Across ALL campaigns: never email the same domain twice inside the window."""
        now = utcnow().isoformat()
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        for _, d in self.s.where("leads", ("domain", "==", domain)):
            if d.get("status") in {"suppressed", "bounced", "excluded"}:
                return True
            if d.get("status") == "not_now" and str(d.get("not_now_until", "")) > now:
                return True
            if d.get("contacted_at") and str(d["contacted_at"]) > cutoff:
                return True
        return False

    # --- suppression (one list across every campaign) -----------------------
    def is_suppressed(self, email: str | None = None, domain: str | None = None) -> bool:
        if email and self.s.get("suppression", email_key(email)):
            return True
        if domain and self.s.get("suppression", "domain_" + domain.lower()):
            return True
        return False

    def suppress(self, email: str, reason: str, source: str):
        self.s.set("suppression", email_key(email),
                   {"reason": reason, "source": source, "at": utcnow().isoformat()}, merge=False)
        # Stop every live sequence for this address, in every campaign.
        ref = self.s.get("by_email", email.strip().lower())
        for c in (ref or {}).get("campaigns", []):
            self.s.set("leads", lead_id(c, ref["domain"]),
                       {"status": "suppressed", "sequence_stopped": reason})

    def suppress_domain(self, domain: str, reason: str):
        self.s.set("suppression", "domain_" + domain.lower(),
                   {"reason": reason, "at": utcnow().isoformat()}, merge=False)

    def index_email(self, email: str, campaign_id: str, domain: str):
        key = email.strip().lower()
        ref = self.s.get("by_email", key) or {"campaigns": []}
        camps = sorted(set(ref.get("campaigns", [])) | {campaign_id})
        self.s.set("by_email", key, {"domain": domain, "campaigns": camps, "last_campaign": campaign_id})

    # --- sends (exactly-once) ----------------------------------------------
    def begin_send(self, sid: str, doc: dict) -> bool:
        """Claim the send BEFORE SMTP. False = already claimed (sent, sending or unknown)."""
        return self.s.create("sends", sid, {**doc, "id": sid, "status": "sending",
                                            "claimed_at": utcnow().isoformat()})

    def finish_send(self, sid: str, status: str, **fields):
        assert status in SEND_STATES
        doc = {"status": status, **fields}
        if status == "sent":
            doc["sent_at"] = utcnow().isoformat()
        self.s.set("sends", sid, doc)

    def expire_stale_sending(self, now: datetime | None = None) -> int:
        """A claim with no outcome after 10 minutes is 'unknown' forever. Never resent."""
        now = now or utcnow()
        n = 0
        for sid, d in self.s.where("sends", ("status", "==", "sending")):
            if datetime.fromisoformat(d["claimed_at"]) < now - STALE_SENDING:
                self.s.set("sends", sid, {"status": "unknown", "expired_at": now.isoformat()})
                n += 1
        return n

    def send(self, sid: str) -> dict | None:
        return self.s.get("sends", sid)

    def sends_since(self, since: datetime, campaign_id: str | None = None,
                    mailbox: str | None = None) -> list[dict]:
        out = [d for d in self.s.list_since("sends", "sent_at", since) if d.get("status") == "sent"]
        if campaign_id:
            out = [d for d in out if d.get("campaign_id") == campaign_id]
        if mailbox:
            out = [d for d in out if d.get("mailbox") == mailbox]
        return out

    def first_send_at(self, mailbox: str | None = None) -> datetime | None:
        if mailbox:
            mb = self.s.get("mailboxes", mailbox) or {}
            return datetime.fromisoformat(mb["first_send_at"]) if mb.get("first_send_at") else None
        doc = self.s.first("sends", "sent_at")
        return datetime.fromisoformat(doc["sent_at"]) if doc else None

    # --- threads ------------------------------------------------------------
    def thread(self, tid: str) -> dict | None:
        return self.s.get("threads", tid)

    def save_thread(self, tid: str, doc: dict):
        self.s.set("threads", tid, {**doc, "id": tid, "updated_at": utcnow().isoformat()})

    def index_message(self, message_id: str, tid: str):
        self.s.set("msgids", msgid_key(message_id), {"thread_id": tid})

    def thread_for_message(self, message_id: str) -> str | None:
        ref = self.s.get("msgids", msgid_key(message_id))
        return ref["thread_id"] if ref else None

    # --- events + costs ------------------------------------------------------
    def event(self, kind: str, what: str, why: str = "", cost_impact: str = "",
              campaign_id: str | None = None, notify: bool = False, **extra) -> str:
        eid = utcnow().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.s.set("events", eid, {"id": eid, "at": utcnow().isoformat(), "kind": kind, "what": what,
                                   "why": why, "cost_impact": cost_impact,
                                   "campaign_id": campaign_id, "notify": notify,
                                   "notified": False, **extra}, merge=False)
        return eid

    def events(self, since: datetime | None = None, campaign_id: str | None = None,
               limit: int = 100) -> list[dict]:
        since = since or utcnow() - timedelta(days=30)
        rows = self.s.list_since("events", "at", since)
        if campaign_id:
            rows = [r for r in rows if r.get("campaign_id") == campaign_id]
        return sorted(rows, key=lambda r: r["at"], reverse=True)[:limit]

    def record_cost(self, doc: dict):
        cid = utcnow().strftime("%Y%m%dT%H%M%S%f") + "-" + uuid.uuid4().hex[:6]
        self.s.set("costs", cid, {"id": cid, "at": utcnow().isoformat(), **doc}, merge=False)
        return cid

    def costs_since(self, since: datetime) -> list[dict]:
        return self.s.list_since("costs", "at", since)

    # --- misc state ---------------------------------------------------------
    def state(self, key: str) -> dict:
        return self.s.get("state", key) or {}

    def set_state(self, key: str, **fields):
        self.s.set("state", key, fields)

    def save_run(self, run_id: str, doc: dict):
        self.s.set("runs", run_id, doc, merge=False)

    def kill_switch_on(self) -> bool:
        return bool(self.state("kill_switch").get("on"))

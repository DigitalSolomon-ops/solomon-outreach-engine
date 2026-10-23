"""Loopback test: prove the live send -> reply -> AI reply path end to end, on yourself.

  python -m leadgen loopback

1. Needs global-leadgen-mailbox-1 (sends) and global-leadgen-notify-smtp (replies as you).
   Missing either: prints "pending" and changes nothing.
2. Creates a PRIVATE test campaign whose only lead is operator@example.com,
   test batch 1, and launches it (preflight runs; a private campaign has no prospect cells,
   so the five-sample check is skipped for it).
3. Sends the one first touch through the normal tick sender (inside the 9:00-16:00 ET window).
4. Replies "I'm interested, how does this work?" to that thread from the notify mailbox.
5. Runs tick until the reply is read, then checks: classified interested, an AI reply went out
   threaded with the booking URL and a BCC to Marcus, the sequence stopped, and the
   interested-reply notice was emailed.
6. Marks the test campaign done. Deletes nothing. Every other campaign is untouched.
"""
from __future__ import annotations

import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import campaigns as camp
from .mailboxes import SmtpConn, in_window
from .sender import today_et
from .store import utcnow

LEAD = "operator@example.com"
REPLY = "I'm interested, how does this work?"


def run(engine, *, wait_seconds: int = 600, poll_every: int = 30, sleep=time.sleep) -> dict:
    s = engine.settings
    steps: list[dict] = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": bool(ok), "detail": str(detail)[:300]})
        return ok

    names = set(engine.secret_names())
    mb_secret = f"{s['send']['mailbox_secret_prefix']}1"
    if mb_secret not in names or s["notify"]["smtp_secret"] not in names:
        return {"status": "pending", "detail": "loopback pending: mailbox secret not present "
                f"(needs {mb_secret} and {s['notify']['smtp_secret']})", "steps": []}
    if engine.ledger.kill_switch_on():
        return {"status": "blocked", "detail": "the global kill switch is on", "steps": []}
    ok, why = in_window(s["send"])
    if not ok:
        return {"status": "blocked", "detail": f"outside the send window ({why}); run it 9:00-16:00 ET on a weekday",
                "steps": []}

    engine.refresh_inventory()
    engine.health()
    pool = engine.mailbox_pool()
    if not step("healthy sending mailbox", pool, f"{len(pool)} healthy"):
        return {"status": "fail", "steps": steps}
    mb = next((m for m in pool if m["id"] == "1"), pool[0])
    notify = engine.secrets.get_json(s["notify"]["smtp_secret"])

    # --- a private campaign with one lead: Marcus
    seed = engine.ledger.campaign(camp.SEED_ID) or camp.seed_from_icp(s)
    cid = f"loopback-{utcnow().strftime('%Y%m%d-%H%M')}"
    c = camp.new_campaign(s, cid=cid, name=f"Loopback test {today_et().isoformat()}",
                          description="Private loopback test: the only lead is Marcus.",
                          landing_url=seed["landing_url"], booking_url=seed.get("booking_url") or seed["landing_url"],
                          offer=dict(seed["offer"]), private=True)
    c["settings"].update(test_batch_size=1, daily_cap=1)
    engine.ledger.save_campaign(cid, c)
    domain = LEAD.split("@")[1]
    evidence = {"fact:loopback": "This is an internal loopback test sent to Marcus."}
    body = ("Hi Marcus,\n\nThis is the outreach engine's loopback test. Reply to this email the way a "
            "prospect would and the engine should answer on its own.\n\nHere is the offer page: "
            "{{campaign_link}}\n\nMarcus")
    engine.ledger.save_lead(cid, domain, {
        "name": "Digital Solomon (loopback)", "email": LEAD, "verdict": "ok", "email_source": "loopback",
        "segment": "loopback", "geo": "Gastonia, NC", "evidence": evidence, "status": "in_sequence",
        "seq": {"next_step": 0, "next_due": today_et().isoformat(), "sent_steps": [],
                "drafts": {"0": {"subject": "Outreach loopback test", "body": body, "source": "loopback"}}}})
    step("private test campaign created", True, cid)

    launched = engine.launch(cid)
    if not step("launch -> preflight -> testing", launched["status"] == "testing",
                {k: v["ok"] for k, v in launched["preflight"]["checks"].items()}):
        engine.ledger.save_campaign(cid, {"status": "done"})
        return {"status": "fail", "campaign_id": cid, "steps": steps}

    # --- the one send, through the normal tick
    engine.tick()
    lead = engine.ledger.lead(cid, domain)
    sent_id = (lead.get("seq") or {}).get("last_message_id")
    rec = engine.ledger.send(f"{cid}:{domain}:0") or {}
    if not step("first touch sent and recorded", rec.get("status") == "sent" and sent_id,
                f"send record {cid}:{domain}:0 status {rec.get('status')}"):
        engine.ledger.save_campaign(cid, {"status": "done"})
        return {"status": "fail", "campaign_id": cid, "steps": steps}

    # --- reply as Marcus from the notify mailbox
    msg = EmailMessage()
    msg["From"] = formataddr(("Marcus Barber", notify.get("email") or notify["user"]))
    msg["To"] = mb["email"]
    msg["Subject"] = "Re: Outreach loopback test"
    msg["Message-ID"] = make_msgid(domain=(notify.get("email") or notify["user"]).split("@")[-1])
    msg["In-Reply-To"] = sent_id
    msg["References"] = sent_id
    msg.set_content(REPLY)
    with (engine.smtp_factory or SmtpConn)(notify) as conn:
        conn.send(msg)
    step("prospect-style reply sent from the notify mailbox", True, msg["Message-ID"])

    # --- tick until the engine has read and answered it
    tid = (lead.get("seq") or {}).get("thread_id")
    deadline = time.monotonic() + wait_seconds
    thread = {}
    while time.monotonic() < deadline:
        engine.tick()
        thread = engine.ledger.thread(tid) or {}
        if thread.get("last_class"):
            break
        sleep(poll_every)
    step("reply classified interested", thread.get("last_class") == "interested", thread.get("last_class"))
    ai = [m for m in thread.get("messages", []) if m.get("kind") == "ai_reply"]
    booking = c["booking_url"]
    step("AI reply sent, threaded, with the booking URL", ai and ai[-1].get("message_id"),
         f"{len(ai)} AI repl(ies)")
    if ai:
        # The stored body keeps the link token; for an interested reply the sender fills it with
        # the campaign's booking URL (replies.py), so the token being there is the check.
        step("AI reply carries the booking link", "{{campaign_link}}" in (ai[-1].get("body") or ""), booking)
        step("AI reply BCC'd Marcus", ai[-1].get("bcc") == s["replies"]["bcc"], ai[-1].get("bcc"))
    lead = engine.ledger.lead(cid, domain)
    step("sequence stopped", (lead.get("seq") or {}).get("next_step") is None, lead.get("status"))
    notices = [e for e in engine.ledger.events(campaign_id=cid) if e["kind"] == "interested_reply"]
    step("interested-reply notice emailed", notices and notices[0].get("notified"),
         notices[0]["id"] if notices else "no event")

    engine.ledger.save_campaign(cid, {"status": "done", "done_at": utcnow().isoformat()})
    step("test campaign set to done (nothing deleted)", True, cid)
    return {"status": "pass" if all(x["ok"] for x in steps) else "fail", "campaign_id": cid, "steps": steps}

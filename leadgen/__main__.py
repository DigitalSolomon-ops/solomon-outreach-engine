"""CLI. One image, these entrypoints:

  python -m leadgen health              daily: key inventory, mailbox DNS, landing checks, test evals, scout
  python -m leadgen prospect            daily: discover, enrich, brief, first touches, follow-ups, digest
  python -m leadgen tick                every 10 min: replies, preflight, kill rules, sends
  python -m leadgen serve-api           private engine API (Cloud Run service solomon-leadgen-api)
  python -m leadgen serve               public unsubscribe service (solomon-leadgen-unsub)
  python -m leadgen check-keys          which provider secrets exist (names only)
  python -m leadgen kill-switch on|off  stop or allow all sending

Add --local to run against .state/leadgen.json and env-var keys (GLOBAL_FIRECRAWL etc.).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, and several providers take their key as a
    # query parameter. Never let a key reach the logs.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="leadgen")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("health", "prospect", "tick", "check-keys"):
        p = sub.add_parser(name)
        p.add_argument("--local", action="store_true")
    ks = sub.add_parser("kill-switch")
    ks.add_argument("state", choices=["on", "off"])
    ks.add_argument("--local", action="store_true")
    lb = sub.add_parser("loopback")
    lb.add_argument("--local", action="store_true")
    lb.add_argument("--wait", type=int, default=600, help="seconds to wait for the reply to arrive")
    sub.add_parser("serve-api")
    sub.add_parser("serve")
    a = ap.parse_args(argv)
    port = int(os.environ.get("PORT", 8080))

    if a.cmd == "serve":
        import uvicorn
        from .unsubscribe import app_from_env
        uvicorn.run(app_from_env(), host="0.0.0.0", port=port)
        return 0
    if a.cmd == "serve-api":
        import uvicorn
        from .api import app_from_env
        uvicorn.run(app_from_env(), host="0.0.0.0", port=port, timeout_keep_alive=120)
        return 0

    from .engine import make_engine
    from .store import utcnow
    engine = make_engine(local=getattr(a, "local", False))
    if a.cmd in ("health", "prospect", "tick"):
        result = getattr(engine, a.cmd)()
        print(json.dumps(result, indent=2, default=str)[:20000])
        return 0
    if a.cmd == "loopback":
        from .loopback import run
        result = run(engine, wait_seconds=a.wait)
        for st in result.get("steps", []):
            print(f"{'PASS' if st['ok'] else 'FAIL'}  {st['step']}  {st['detail']}")
        print(f"\nloopback: {result['status'].upper()}  {result.get('detail', '')} {result.get('campaign_id', '')}".rstrip())
        return 0 if result["status"] == "pass" else (2 if result["status"] in ("pending", "blocked") else 1)
    if a.cmd == "check-keys":
        inv = engine.refresh_inventory()
        for p in inv["providers"]:
            print(f"{p['name']:20} {'connected' if p['connected'] else 'missing':10} {p['secret_used'] or ''}")
        print("unassigned:", ", ".join(inv["unassigned_secret_names"]) or "none")
        return 0
    if a.cmd == "kill-switch":
        engine.ledger.set_state("kill_switch", on=(a.state == "on"), reason="set by operator (CLI)",
                                at=utcnow().isoformat())
        print(f"kill switch {a.state}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

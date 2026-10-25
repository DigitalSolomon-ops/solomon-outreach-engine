"""CLI. One image, these entrypoints:

  python -m leadgen health              daily: key inventory, mailbox DNS, landing checks, test evals, scout
  python -m leadgen prospect            daily: discover, enrich, brief, first touches, follow-ups, digest
  python -m leadgen tick                every 10 min: replies, preflight, kill rules, sends
  python -m leadgen preflight <id>      one campaign's preflight now ("Run preflight now" in Outreach)
  python -m leadgen serve-api           private engine API (Cloud Run service solomon-leadgen-api)
  python -m leadgen serve               public unsubscribe service (solomon-leadgen-unsub)
  python -m leadgen check-keys          which provider secrets exist (names only)
  python -m leadgen kill-switch on|off  stop or allow all sending
  python -m leadgen pilot-nosite        no-website pilot: N listings, emails from FB/Yelp/BBB, find-rate report
  python -m leadgen strategy-probe <id> one strategy live on N candidates, no campaign, no sends; find-rate report
  python -m leadgen api-get <url>       GET the private engine API with this runtime's ID token (a job execution's
                                        live read: gcloud run jobs execute solomon-leadgen --args=api-get,<url>)

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
    pl = sub.add_parser("pilot-nosite")
    pl.add_argument("--local", action="store_true")
    pl.add_argument("--query", default="roofing contractor")
    pl.add_argument("--geo", action="append", required=True, help="repeat for more areas")
    pl.add_argument("--n", type=int, default=50)
    pl.add_argument("--out", default="docs/pilots")
    pl.add_argument("--source", action="append", choices=["bbb", "facebook", "yelp"],
                    help="where to look for emails (default: bbb only)")
    sp = sub.add_parser("strategy-probe")
    sp.add_argument("strategy")
    sp.add_argument("--local", action="store_true")
    sp.add_argument("--geo", action="append", required=True, help="repeat for more areas")
    sp.add_argument("--n", type=int, default=50)
    sp.add_argument("--industry", help="industries.yaml slug for industry strategies (default auto-detailing)")
    sp.add_argument("--segment", help="segment name for strategies that don't use the industries list")
    sp.add_argument("--param", action="append", default=[], help="name=value, repeatable")
    sp.add_argument("--out", default="docs/strategies")
    ag = sub.add_parser("api-get")
    ag.add_argument("url")
    pf = sub.add_parser("preflight")
    pf.add_argument("campaign_id")
    pf.add_argument("--local", action="store_true")
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

    if a.cmd == "api-get":
        return api_get(a.url)

    from .engine import make_engine
    from .store import utcnow
    engine = make_engine(local=getattr(a, "local", False))
    if a.cmd in ("health", "prospect", "tick"):
        result = getattr(engine, a.cmd)()
        print(json.dumps(result, indent=2, default=str)[:20000])
        return 0
    if a.cmd == "preflight":
        result = engine.run_preflight(a.campaign_id)
        print(json.dumps(result, indent=2, default=str)[:20000])
        return 0
    if a.cmd == "loopback":
        from .loopback import run
        result = run(engine, wait_seconds=a.wait)
        for st in result.get("steps", []):
            print(f"{'PASS' if st['ok'] else 'FAIL'}  {st['step']}  {st['detail']}")
        print(f"\nloopback: {result['status'].upper()}  {result.get('detail', '')} {result.get('campaign_id', '')}".rstrip())
        return 0 if result["status"] == "pass" else (2 if result["status"] in ("pending", "blocked") else 1)
    if a.cmd == "pilot-nosite":
        from .pilot_nosite import NoSitePilot, write_report
        rep = NoSitePilot(engine, a.source).run(a.query, a.geo, a.n)
        print(json.dumps(rep, indent=2, default=str))
        print("report:", write_report(rep, a.out))
        return 0
    if a.cmd == "strategy-probe":
        from .probe import Probe, write_report
        params = {}
        for kv in a.param:
            k, _, v = kv.partition("=")
            try:
                params[k] = json.loads(v)
            except ValueError:
                params[k] = v
        rep = Probe(engine, a.strategy, a.geo, a.n, a.industry, a.segment, params).run()
        print(json.dumps(rep, indent=2, default=str))
        print("report:", write_report(rep, a.out))
        return 0
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


def api_get(url: str) -> int:
    """Read-only GET against the private engine API, authenticated as this runtime's account
    (the metadata server's ID token; only works on Cloud Run, which holds run.invoker)."""
    from urllib.parse import urlparse

    import httpx
    p = urlparse(url)
    if p.scheme != "https" or not p.netloc.endswith(".run.app"):
        print("api-get only reads https://*.run.app URLs")
        return 2
    audience = f"{p.scheme}://{p.netloc}"
    tok = httpx.get("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity",
                    params={"audience": audience}, headers={"Metadata-Flavor": "Google"}, timeout=10).text
    r = httpx.get(url, headers={"Authorization": f"Bearer {tok}"}, timeout=120)
    print(f"HTTP {r.status_code}")
    print(r.text[:30000])
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())

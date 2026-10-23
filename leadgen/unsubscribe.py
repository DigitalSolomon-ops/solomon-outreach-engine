"""Unsubscribe service (Cloud Run service, public). One page, one click, recorded at once.

GET  /u/{token}  -> confirmation page with a button (link scanners can't opt people out)
POST /u/{token}  -> suppress; also the RFC 8058 one-click target mail clients POST to
"""
from __future__ import annotations

import html
import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from .compliance import verify_token

log = logging.getLogger(__name__)

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font-family:Inter,Arial,sans-serif;background:#F8F6F1;color:#181613;display:grid;
place-items:center;min-height:100vh;margin:0;padding:16px}}main{{max-width:440px;background:#fff;
border:1px solid #EAE6DC;border-radius:12px;padding:32px}}h1{{font-family:Poppins,Arial,sans-serif;
font-size:22px;margin:0 0 12px}}button{{background:#2F4858;color:#fff;border:0;border-radius:8px;
padding:12px 20px;font-size:16px;cursor:pointer}}p{{line-height:1.5}}</style></head>
<body><main><h1>{title}</h1>{body}</main></body></html>"""


def create_app(ledger, hmac_secret: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def page(title: str, body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(PAGE.format(title=html.escape(title), body=body), status_code=status)

    # Cloud Run's front end answers /healthz itself (a Google 404) before the container
    # sees it, so the probe path that actually works in production is /health.
    @app.get("/health")
    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.get("/u/{token}")
    def confirm(token: str):
        email = verify_token(token, hmac_secret)
        if not email:
            return page("Link not recognized", "<p>This unsubscribe link is not valid. "
                        "Reply to the email with the word remove and we will take you off the list.</p>", 400)
        return page("Unsubscribe", f"<p>Stop emails from Digital Solomon to "
                    f"<strong>{html.escape(email)}</strong>?</p>"
                    f'<form method="post"><button type="submit">Unsubscribe</button></form>')

    @app.post("/u/{token}")
    async def unsubscribe(token: str, request: Request):
        email = verify_token(token, hmac_secret)
        if not email:
            return page("Link not recognized", "<p>This unsubscribe link is not valid.</p>", 400)
        ledger.suppress(email, "unsubscribe link", "web")  # global: every campaign, every sequence
        log.info("unsubscribed one address")  # never log the address itself
        return page("You're unsubscribed", "<p>Done. You won't get any more emails from us.</p>")

    return app


def app_from_env() -> FastAPI:
    from .config import load_settings
    from .secrets import Secrets
    from .store import FirestoreStore, Ledger
    settings = load_settings()
    secrets = Secrets(settings["gcp_project"])
    key = secrets.get(settings["unsubscribe"]["hmac_secret"])
    if not key:
        raise RuntimeError(f"secret {settings['unsubscribe']['hmac_secret']} is required")
    return create_app(Ledger(FirestoreStore(settings["gcp_project"])), key)

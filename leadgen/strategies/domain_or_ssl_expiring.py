"""#11 domain_or_ssl_expiring: the domain registration expires soon, or the certificate has
expired or expires soon.

Domain dates come from RDAP (rdap.org bootstraps to the registry); the certificate date from a
direct TLS handshake with the site. Both are checked before the site is read, so a business
that doesn't qualify costs no scrape. The fact gives the exact date and never the domain name
(the validator allows no domain names in the email body).
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlparse

from ..providers import signals
from ._util import long_date
from .base import Strategy


class DomainOrSslExpiring(Strategy):

    def drop_listing(self, p, c, cand):
        today = date.today()
        dom, _ = p.e.signal("domains").call("expiry", cand.domain)
        url = cand.website if "://" in cand.website else "https://" + cand.website
        cert = signals.cert_expiry(urlparse(url).hostname or cand.domain)
        found = {}
        if dom and dom <= today + timedelta(days=self.params["domain_days"]):
            found["domain"] = dom.isoformat()
        if cert and cert <= today + timedelta(days=self.params["ssl_days"]):
            found["ssl"] = cert.isoformat()
        if not found:
            return "domain and certificate are not expiring soon"
        cand.raw = {**(cand.raw or {}), "expiring": found}
        return None

    def facts(self, p, c, cand, read):
        found = (cand.raw or {}).get("expiring") or {}
        today = date.today()
        out = []
        if found.get("domain"):
            d = date.fromisoformat(found["domain"])
            verb = "expired" if d < today else "expires"
            out.append({"id": "domain_expiring", "pillar": "Presence",
                        "text": f"Public domain registration records (RDAP) show the registration for your "
                                f"website's domain {verb} on {long_date(d)}."})
        if found.get("ssl"):
            d = date.fromisoformat(found["ssl"])
            verb = "expired" if d < today else "expires"
            out.append({"id": "ssl_expiring", "pillar": "Presence",
                        "text": f"The security certificate your website serves {verb} on {long_date(d)}, "
                                "after which browsers warn visitors that the site is not secure."})
        return out


STRATEGY = DomainOrSslExpiring

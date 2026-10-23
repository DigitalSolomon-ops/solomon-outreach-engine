"""Email verification adapters. Each returns (verdict, units).

Verdicts: ok | catch_all | invalid | disposable | unknown | mx_only | no_mx
"""
from __future__ import annotations

from .base import ProviderError, check_response, http_client


class MillionVerifier:
    def __init__(self, key: str, **_):
        self.key = key

    def verify(self, email: str):
        with http_client(timeout=30) as c:
            resp = c.get("https://api.millionverifier.com/api/v3/",
                         params={"api": self.key, "email": email, "timeout": 20})
        data = check_response(resp, "millionverifier")
        if data.get("error"):
            raise ProviderError(f"millionverifier: {data['error']}")
        result = (data.get("result") or "unknown").lower()
        return {"ok": "ok", "catch_all": "catch_all", "invalid": "invalid",
                "disposable": "disposable"}.get(result, "unknown"), 1


class MxCheck:
    """Free fallback: the domain has mail servers. Says nothing about the mailbox itself."""

    def __init__(self, **_):
        pass

    def verify(self, email: str):
        import dns.exception
        import dns.resolver
        domain = email.rsplit("@", 1)[-1]
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=10)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return "no_mx", 0
        except dns.exception.DNSException as exc:
            raise ProviderError(f"mx lookup {domain}: {type(exc).__name__}") from exc
        return ("mx_only" if len(answers) else "no_mx"), 0


VERIFY = {"millionverifier": MillionVerifier, "mx_check": MxCheck}

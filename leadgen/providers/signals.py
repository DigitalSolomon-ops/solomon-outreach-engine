"""Adapters for the lead-strategy signal sources (one engine stage each, free allowances only).

  carriers     fmcsa_census     new_carriers(states, days, limit, offset) -> (rows, units)
  registrants  sam_entity       new_registrants(state, days, page) -> ({total, rows}, units)
  jobs         hasdata_indeed   jobs(keyword, location) -> (list[job], units)
  reviews      hasdata_reviews  recent_replies(place_id, n) -> (list[{has_reply, date}], units)
  answer       tavily_answer,   answer(question) -> ({answer, sources}, units)
               linkup_answer
  domains      rdap             expiry(domain) -> (date | None, units)

Same error contract as every other adapter: QuotaExhausted when the free allowance is spent,
BillingSignal when money would be charged, ProviderError for the rest. Notes per source are
in docs/providers/strategy-sources.md.
"""
from __future__ import annotations

import socket
import ssl
from datetime import date, datetime, timedelta, timezone

from .base import BillingSignal, ProviderError, QuotaExhausted, check_response, http_client
from .discovery_more import _hasdata_check
from .websearch_more import LinkupSearch

# --------------------------------------------------------------------------- FMCSA

FMCSA_DATASET = "kjg3-diqy"   # SMS Input - Motor Carrier Census: carries email_address.
# NOT az4n-8mr2 (Company Census): it has no email column (a past build lost a week to it).
FMCSA_COLUMNS = ("dot_number,legal_name,dba_name,phy_city,phy_state,email_address,add_date,"
                 "authorized_for_hire,nbr_power_unit")


def _ddmonyy(d: date) -> str:
    return d.strftime("%d-%b-%y").upper()


class FmcsaCensus:
    """data.transportation.gov Socrata (SODA 2.1), keyless. add_date is TEXT ("25-SEP-26"),
    so a date window is an explicit list of day strings. The window ends at the dataset's own
    newest add_date (it is a monthly run), not today."""

    URL = "https://data.transportation.gov/resource/{dataset}.json"

    def __init__(self, key: str | None = None, dataset: str = FMCSA_DATASET, **_):
        assert dataset == "kjg3-diqy", "FMCSA source must be kjg3-diqy (az4n-8mr2 has no email column)"
        self.dataset = dataset
        self.token = key
        self._latest: date | None = None

    def _get(self, params: dict) -> list:
        headers = {"X-App-Token": self.token} if self.token else {}
        with http_client() as c:
            resp = c.get(self.URL.format(dataset=self.dataset), params=params, headers=headers)
        data = check_response(resp, "fmcsa_census")
        if not isinstance(data, list):
            raise ProviderError(f"fmcsa_census returned {type(data).__name__}, not rows")
        return data

    def latest_add_date(self) -> date:
        if self._latest:
            return self._latest
        yy = date.today().strftime("%y")
        prev = f"{int(yy) - 1:02d}"
        rows = self._get({"$select": "add_date,count(*)", "$where": f"add_date like '%-{yy}' OR add_date like '%-{prev}'",
                          "$group": "add_date", "$limit": 1000})
        days = []
        for r in rows:
            try:
                days.append(datetime.strptime(r["add_date"], "%d-%b-%y").date())
            except (KeyError, ValueError):
                continue
        if not days:
            raise ProviderError("fmcsa_census: no add_date values this year or last")
        self._latest = max(days)
        return self._latest

    def window(self, days: int) -> tuple[date, list[str]]:
        end = self.latest_add_date()
        return end, [_ddmonyy(end - timedelta(days=i)) for i in range(days)]

    def _where(self, states: list[str], days: int) -> str:
        _, window = self.window(days)
        st = ",".join(f"'{s}'" for s in states if s.isalpha() and len(s) == 2)
        dates = ",".join(f"'{d}'" for d in window)
        return f"add_date in ({dates}) AND phy_state in ({st}) AND authorized_for_hire='true'"

    def count(self, states: list[str], days: int) -> tuple[dict, int]:
        """count(*) and count(email_address) for the window: checked before trusting any ingest."""
        rows = self._get({"$select": "count(*) AS n, count(email_address) AS with_email",
                          "$where": self._where(states, days)})
        r = rows[0] if rows else {}
        return {"rows": int(r.get("n", 0)), "with_email": int(r.get("with_email", 0))}, 1

    def new_carriers(self, states: list[str], days: int, limit: int, offset: int = 0):
        # Higher USDOT numbers are newer; add_date can't be sorted (text).
        rows = self._get({"$select": FMCSA_COLUMNS, "$where": self._where(states, days),
                          "$order": "dot_number DESC", "$limit": limit, "$offset": offset})
        return rows, 1


# --------------------------------------------------------------------------- SAM.gov

class SamEntity:
    """SAM.gov Entity Management API v3, public data, with a SAM.gov account API key (the
    api.data.gov key returns 404 here). Public data has no point-of-contact email; the lead's
    address comes from the website the entity listed, through the normal site flow."""

    URL = "https://api.sam.gov/entity-information/v3/entities"
    PAGE = 10   # the API's maximum page size

    def __init__(self, key: str, **_):
        self.key = key

    def new_registrants(self, state: str, days: int, page: int = 0):
        end = date.today()
        start = end - timedelta(days=days)
        params = {"api_key": self.key, "registrationDate": f"[{start:%m/%d/%Y},{end:%m/%d/%Y}]",
                  "physicalAddressProvinceOrStateCode": state, "registrationStatus": "A",
                  "includeSections": "entityRegistration,coreData,assertions",
                  "page": page, "size": self.PAGE}
        with http_client() as c:
            resp = c.get(self.URL, params=params)
        if resp.status_code == 429:
            raise QuotaExhausted(f"sam_entity HTTP 429 (daily request limit): {resp.text[:200]}")
        if resp.status_code in (401, 403):
            raise ProviderError(f"sam_entity HTTP {resp.status_code}: key not accepted")
        data = check_response(resp, "sam_entity")
        rows = []
        for e in data.get("entityData") or []:
            reg = e.get("entityRegistration") or {}
            core = e.get("coreData") or {}
            gs = (e.get("assertions") or {}).get("goodsAndServices") or {}
            naics = gs.get("naicsList") or []
            rows.append({"uei": reg.get("ueiSAM") or "", "name": reg.get("legalBusinessName") or "",
                         "dba": reg.get("dbaName") or "", "registered": reg.get("registrationDate") or "",
                         "url": (core.get("entityInformation") or {}).get("entityURL") or "",
                         "city": (core.get("physicalAddress") or {}).get("city") or "",
                         "state": (core.get("physicalAddress") or {}).get("stateOrProvinceCode") or "",
                         "primary_naics": gs.get("primaryNaics") or "",
                         "small": any(n.get("sbaSmallBusiness") == "Y" for n in naics)})
        return {"total": int(data.get("totalRecords") or 0), "rows": rows}, 1


# --------------------------------------------------------------------------- HasData

class HasDataIndeed:
    """GET /scrape/indeed/listing (10 credits, shared HasData balance)."""
    URL = "https://api.hasdata.com/scrape/indeed/listing"
    CREDITS_PER_REQUEST = 10

    def __init__(self, key: str, **_):
        self.key = key

    def jobs(self, keyword: str, location: str):
        with http_client(timeout=300) as c:
            resp = c.get(self.URL, headers={"x-api-key": self.key},
                         params={"keyword": keyword, "location": location})
        data = _hasdata_check(resp)
        out = [{"title": (j.get("title") or "").strip(), "company": (j.get("company") or "").strip(),
                "location": j.get("location") or "", "posted": (j.get("isoDate") or "")[:10],
                "sponsored": str(j.get("sponsored")).lower() == "true"}
               for j in data.get("jobs") or [] if j.get("company") and j.get("title")]
        return out, self.CREDITS_PER_REQUEST


class HasDataReviews:
    """GET /scrape/google-maps/reviews?sortBy=newestFirst (10 credits). Only whether each recent
    review has an owner reply, and its date, ever leave this adapter: no text, no names."""
    URL = "https://api.hasdata.com/scrape/google-maps/reviews"
    CREDITS_PER_REQUEST = 10

    def __init__(self, key: str, **_):
        self.key = key

    def recent_replies(self, place_id: str, n: int):
        with http_client(timeout=300) as c:
            resp = c.get(self.URL, headers={"x-api-key": self.key},
                         params={"placeId": place_id, "sortBy": "newestFirst", "hl": "en"})
        data = _hasdata_check(resp)
        out = [{"has_reply": bool(r.get("response")), "date": (r.get("isoDate") or "")[:10]}
               for r in (data.get("reviews") or [])[:n]]
        return out, self.CREDITS_PER_REQUEST


# --------------------------------------------------------------------------- Linkup answer

class LinkupAnswer(LinkupSearch):
    """POST /v1/search outputType=sourcedAnswer: the answer text an AI search tool gives."""

    def answer(self, question: str):
        with http_client(timeout=120) as c:
            resp = c.post(self.URL, headers={"Authorization": f"Bearer {self.key}"},
                          json={"q": question, "depth": "standard", "outputType": "sourcedAnswer"})
        data = self._check(resp)
        if data is None:
            return {"answer": "", "sources": []}, 1
        return {"answer": data.get("answer") or "",
                "sources": [s.get("name") or s.get("url") or "" for s in data.get("sources") or []]}, 1


class TavilyAnswer:
    """POST https://api.tavily.com/search with include_answer (basic = 1 credit). The brief's first
    choice for missing_from_ai_answers; Linkup is the fallback. Same 1,000-credit pool as tavily."""
    URL = "https://api.tavily.com/search"

    def __init__(self, key: str, **_):
        self.key = key

    def answer(self, question: str):
        with http_client(timeout=120) as c:
            resp = c.post(self.URL, headers={"Authorization": f"Bearer {self.key}"},
                          json={"query": question, "include_answer": "advanced", "search_depth": "basic",
                                "max_results": 10})
        data = check_response(resp, "tavily")   # 432/433 = plan or pay-as-you-go limit -> QuotaExhausted
        return {"answer": data.get("answer") or "",
                "sources": [r.get("title") or r.get("url") or "" for r in data.get("results") or []]}, 1


# --------------------------------------------------------------------------- RDAP + TLS

class Rdap:
    """rdap.org bootstrap redirect to the registry's RDAP server. Keyless; 429 = slow down."""
    URL = "https://rdap.org/domain/{domain}"

    def __init__(self, key: str | None = None, **_):
        pass

    def expiry(self, domain: str):
        with http_client(timeout=30) as c:
            resp = c.get(self.URL.format(domain=domain), headers={"Accept": "application/rdap+json"})
        if resp.status_code == 404:
            return None, 1
        if resp.status_code == 429:
            raise QuotaExhausted(f"rdap HTTP 429: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"rdap HTTP {resp.status_code} for {domain}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"rdap returned non-JSON for {domain}") from exc
        for ev in data.get("events") or []:
            if ev.get("eventAction") == "expiration" and ev.get("eventDate"):
                try:
                    return datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00")).date(), 1
                except ValueError:
                    return None, 1
        return None, 1


def cert_expiry(host: str, timeout: float = 10.0) -> date | None:
    """The served certificate's notAfter, read from the TLS handshake even when it has expired
    (verification off on purpose: we only read the date). None when there is no TLS on 443."""
    from cryptography import x509
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return None
    if not der:
        return None
    cert = x509.load_der_x509_certificate(der)
    return cert.not_valid_after_utc.astimezone(timezone.utc).date()


SIGNALS = {
    "carriers": {"fmcsa_census": FmcsaCensus},
    "registrants": {"sam_entity": SamEntity},
    "jobs": {"hasdata_indeed": HasDataIndeed},
    "reviews": {"hasdata_reviews": HasDataReviews},
    "answer": {"tavily_answer": TavilyAnswer, "linkup_answer": LinkupAnswer},
    "domains": {"rdap": Rdap},
}

__all__ = ["SIGNALS", "FmcsaCensus", "SamEntity", "HasDataIndeed", "HasDataReviews", "LinkupAnswer",
           "Rdap", "cert_expiry", "FMCSA_DATASET", "BillingSignal"]

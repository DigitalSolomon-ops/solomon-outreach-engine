"""More maps-style discovery adapters, each written from the provider's own docs (2026-10-22).

Docs used per provider are in docs/providers/discovery.md. Every adapter maps its provider's
documented quota / billing responses explicitly (base.check_response is only the fallback),
because several providers use codes the generic table gets wrong:
  - HasData 403 = "used up all your API credits", 429 = concurrency (transient, not quota)
  - Outscraper 402 = past-due invoice / no card, body has none of base.BILLING_WORDS
  - Google 403 with ErrorInfo reason BILLING_DISABLED = billing problem, not a bad key
"""
from __future__ import annotations

from .base import (BillingSignal, Candidate, ProviderError, QuotaExhausted, check_response,
                   http_client)

# --------------------------------------------------------------------------- helpers


def _json(resp, provider: str) -> dict:
    """Parsed body if it is a JSON object, else {} (error bodies are not always JSON)."""
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _brief(resp) -> str:
    return resp.text[:500]


# --------------------------------------------------------------------------- Google Places (New)

_GOOGLE_FIELDS = ",".join([
    "places.id", "places.displayName", "places.websiteUri", "places.nationalPhoneNumber",
    "places.formattedAddress", "places.rating", "places.userRatingCount", "places.types",
    "places.regularOpeningHours.weekdayDescriptions", "places.businessStatus",
])
_GOOGLE_BILLING_REASONS = {"BILLING_DISABLED"}
_GOOGLE_QUOTA_REASONS = {"RATE_LIMIT_EXCEEDED", "RESOURCE_QUOTA_EXCEEDED"}


def _google_check(resp) -> dict:
    """Google AIP-193 error model: {"error": {"code", "message", "status", "details": [ErrorInfo]}}."""
    if resp.status_code < 400:
        return check_response(resp, "google_places")
    err = (_json(resp, "google_places").get("error") or {})
    status = err.get("status", "")
    reasons = {d.get("reason", "") for d in err.get("details") or [] if isinstance(d, dict)}
    msg = f"google_places HTTP {resp.status_code} {status} {sorted(reasons)}: {err.get('message', _brief(resp))}"
    if reasons & _GOOGLE_BILLING_REASONS or "billing" in (err.get("message") or "").lower():
        raise BillingSignal(msg)
    if resp.status_code == 429 or status == "RESOURCE_EXHAUSTED" or reasons & _GOOGLE_QUOTA_REASONS:
        raise QuotaExhausted(msg)
    raise ProviderError(msg)


class GooglePlaces:
    """POST places:searchText. 1 request = 1 unit (Text Search Enterprise SKU: websiteUri,
    phone, rating, hours are Enterprise fields). Google terms: only place_id may be stored."""
    kind = "maps"
    persist = "place_id_only"
    URL = "https://places.googleapis.com/v1/places:searchText"

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int, _no_site: bool = False):
        with http_client() as c:
            resp = c.post(self.URL, headers={"X-Goog-Api-Key": self.key,
                                             "X-Goog-FieldMask": _GOOGLE_FIELDS},
                          json={"textQuery": f"{query} in {geo}" if geo else query,
                                "pageSize": max(1, min(int(limit), 20))})
        data = _google_check(resp)
        out = []
        for p in data.get("places") or []:
            if p.get("businessStatus") == "CLOSED_PERMANENTLY" or bool(p.get("websiteUri")) == _no_site:
                continue
            types = p.get("types") or []
            out.append(Candidate(
                name=(p.get("displayName") or {}).get("text", ""), website=p.get("websiteUri", ""),
                phone=p.get("nationalPhoneNumber", ""), address=p.get("formattedAddress", ""),
                category=types[0] if types else "", source="google_places",
                place_id=p.get("id", ""), rating=p.get("rating"),
                review_count=p.get("userRatingCount"), categories=list(types),
                hours=list((p.get("regularOpeningHours") or {}).get("weekdayDescriptions") or [])))
        return out, 1

    def search_nosite(self, query: str, geo: str, limit: int):
        """Only listings WITHOUT a website (the no-website pilot), keyed by Google listing id.

        A limit over 20 reads further result pages (Text Search returns at most 60 in 3 pages,
        1 request each): the first page is mostly established businesses with websites."""
        if limit <= 20:
            return self.search(query, geo, limit, _no_site=True)
        out, units, token = [], 0, None
        for _ in range(min(3, -(-int(limit) // 20))):
            body = {"textQuery": f"{query} in {geo}" if geo else query, "pageSize": 20}
            if token:
                body["pageToken"] = token
            with http_client() as c:
                resp = c.post(self.URL, headers={"X-Goog-Api-Key": self.key,
                                                 "X-Goog-FieldMask": _GOOGLE_FIELDS + ",nextPageToken"}, json=body)
            data = _google_check(resp)
            units += 1
            for p in data.get("places") or []:
                if p.get("businessStatus") == "CLOSED_PERMANENTLY" or p.get("websiteUri"):
                    continue
                types = p.get("types") or []
                out.append(Candidate(
                    name=(p.get("displayName") or {}).get("text", ""), website="", source="google_places",
                    category=types[0] if types else "", place_id=p.get("id", ""), rating=p.get("rating"),
                    review_count=p.get("userRatingCount"), categories=list(types)))
            token = data.get("nextPageToken")
            if not token:
                break
        return out, units


# --------------------------------------------------------------------------- Outscraper


def _outscraper_check(resp) -> dict:
    """Outscraper errors: {"error": true, "errorMessage": "..."}; 401 bad key, 402 past-due
    invoices / no card, 422 bad params. A 2xx can also carry error=true."""
    body = _json(resp, "outscraper_maps")
    msg = f"outscraper_maps HTTP {resp.status_code}: {body.get('errorMessage') or _brief(resp)}"
    if resp.status_code == 402:
        raise BillingSignal(msg)
    if resp.status_code == 429:
        raise QuotaExhausted(msg)
    if resp.status_code >= 400:
        raise ProviderError(msg)
    if body.get("error"):
        low = str(body.get("errorMessage", "")).lower()
        if "invoice" in low or "card" in low or "payment" in low:
            raise BillingSignal(msg)
        raise ProviderError(msg)
    if resp.status_code == 202 or body.get("status") == "Pending":
        raise ProviderError("outscraper_maps: request went async (status Pending); send async=false")
    if body.get("status") == "Failure" or resp.status_code == 204:
        raise ProviderError(f"outscraper_maps: request failed: {_brief(resp)}")
    return body


class OutscraperMaps:
    """GET /google-maps-search (async=false). Billed per place returned; a search that finds
    nothing still uses 1 place. Overage past the free tier is invoiced, never refused."""
    kind = "maps"
    URL = "https://api.outscraper.cloud/google-maps-search"
    FIELDS = ("name,place_id,site,phone,full_address,type,subtypes,rating,reviews,"
              "working_hours,business_status")

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int, _no_site: bool = False):
        with http_client(timeout=300) as c:
            resp = c.get(self.URL, headers={"X-API-KEY": self.key}, params={
                "query": f"{query}, {geo}" if geo else query, "limit": int(limit),
                "async": "false", "language": "en", "fields": self.FIELDS})
        data = _outscraper_check(resp)
        places = [p for group in data.get("data") or [] for p in (group or [])]
        out = []
        for p in places:
            if p.get("business_status") == "CLOSED_PERMANENTLY" or bool(p.get("site")) == _no_site:
                continue
            subtypes = [s.strip() for s in (p.get("subtypes") or "").split(",") if s.strip()]
            wh = p.get("working_hours") or {}
            hours = [f"{d}: {t if isinstance(t, str) else ', '.join(t)}" for d, t in wh.items()] \
                if isinstance(wh, dict) else []
            out.append(Candidate(
                name=p.get("name", ""), website=p.get("site") or "", phone=p.get("phone") or "",
                address=p.get("full_address") or "", category=p.get("type") or "",
                source="outscraper_maps", place_id=p.get("place_id") or "", rating=p.get("rating"),
                review_count=p.get("reviews"), categories=subtypes, hours=hours))
        return out, max(1, len(places))

    def search_nosite(self, query: str, geo: str, limit: int):
        """Only listings WITHOUT a website (the no-website pilot), keyed by Google listing id."""
        return self.search(query, geo, limit, _no_site=True)


# --------------------------------------------------------------------------- HasData


def _hasdata_check(resp) -> dict:
    """HasData status table: 401 invalid key, 403 out of credits, 422 validation,
    429 concurrency limit (transient - NOT quota), 400/500 retryable failures (not charged)."""
    msg = f"hasdata_maps HTTP {resp.status_code}: {_brief(resp)}"
    if resp.status_code == 403:
        raise QuotaExhausted(msg)
    if resp.status_code == 402:
        raise BillingSignal(msg)
    if resp.status_code >= 400:
        raise ProviderError(msg)
    try:
        body = resp.json()
    except ValueError as exc:
        raise ProviderError(f"hasdata_maps returned non-JSON: {_brief(resp)}") from exc
    if not isinstance(body, dict):
        raise ProviderError(f"hasdata_maps returned non-object JSON: {_brief(resp)}")
    return body


class HasDataMaps:
    """GET /scrape/google-maps/search. 10 credits per successful request (shared balance)."""
    kind = "maps"
    URL = "https://api.hasdata.com/scrape/google-maps/search"
    CREDITS_PER_REQUEST = 10

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int, _no_site: bool = False):
        with http_client(timeout=300) as c:
            resp = c.get(self.URL, headers={"x-api-key": self.key},
                         params={"q": f"{query} {geo}".strip(), "hl": "en", "gl": "us"})
        data = _hasdata_check(resp)
        results = data.get("localResults")
        if results is None and isinstance(data.get("placeResults"), dict):
            results = [data["placeResults"]]
        out = []
        for r in (results or [])[:limit]:
            if bool(r.get("website")) == _no_site:
                continue
            days = (r.get("workingHours") or {}).get("days") or []
            out.append(Candidate(
                name=r.get("title", ""), website=r.get("website", ""), phone=r.get("phone", ""),
                address=r.get("address", ""), category=r.get("type", ""), source="hasdata_maps",
                place_id=r.get("placeId", ""), rating=r.get("rating"),
                review_count=r.get("reviews"), categories=list(r.get("types") or []),
                hours=[f"{d.get('day')}: {d.get('time')}" for d in days if d.get("day")]))
        return out, self.CREDITS_PER_REQUEST

    def search_nosite(self, query: str, geo: str, limit: int):
        """Only listings WITHOUT a website (the no-website pilot), keyed by Google listing id."""
        return self.search(query, geo, limit, _no_site=True)


# --------------------------------------------------------------------------- Foursquare

_FSQ_FIELDS = "fsq_place_id,name,categories,location,tel,website,date_closed"  # Pro fields only


def _fsq_check(resp) -> dict:
    """Foursquare: {"message": "..."}; documented 400/401/403/404/405/409/500. No documented
    out-of-credits code for the new API; the legacy v2 table uses 429 quota_exceeded."""
    if resp.status_code == 429:
        raise QuotaExhausted(f"foursquare_places HTTP 429: {_brief(resp)}")
    return check_response(resp, "foursquare_places")


class FoursquarePlaces:
    """GET places-api.foursquare.com/places/search, 1 request = 1 unit. Requests Pro fields
    only: rating/hours/stats are Premium fields that are billed from the first call."""
    kind = "maps"
    URL = "https://places-api.foursquare.com/places/search"
    VERSION = "2025-06-17"

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int):
        params = {"query": query, "limit": max(1, min(int(limit), 50)), "fields": _FSQ_FIELDS}
        if geo:
            params["near"] = geo
        with http_client() as c:
            resp = c.get(self.URL, params=params, headers={
                "Authorization": f"Bearer {self.key}", "X-Places-Api-Version": self.VERSION,
                "Accept": "application/json"})
        data = _fsq_check(resp)
        out = []
        for r in data.get("results") or []:
            if r.get("date_closed") or not r.get("website"):
                continue
            cats = [c.get("name", "") for c in r.get("categories") or [] if c.get("name")]
            out.append(Candidate(
                name=r.get("name", ""), website=r["website"], phone=r.get("tel", ""),
                address=(r.get("location") or {}).get("formatted_address", ""),
                category=cats[0] if cats else "", source="foursquare_places", categories=cats))
        return out, 1


# --------------------------------------------------------------------------- Geoapify

# Best-effort map from common segment words to documented Geoapify category keys. Geoapify has
# no free-text place search: Places needs >=1 category. Unmapped queries fall back to broad
# business categories plus the documented `name` filter.
_GEOAPIFY_CATEGORIES = {
    "electric": "service.electrician", "electrician": "service.electrician",
    "carpent": "service.carpenter", "locksmith": "service.locksmith",
    "cleaning": "service.cleaning", "laundry": "service.cleaning.laundry",
    "auto repair": "service.vehicle.repair", "mechanic": "service.vehicle.repair",
    "car wash": "service.vehicle.car_wash", "salon": "service.beauty",
    "hair": "service.beauty.hairdresser", "spa": "service.beauty.spa",
    "massage": "service.beauty.massage", "tattoo": "service.beauty.tattoo",
    "funeral": "service.funeral_directors", "photograph": "service.photographer",
    "dentist": "healthcare.dentist", "dental": "healthcare.dentist",
    "orthodont": "healthcare.dentist.orthodontics", "clinic": "healthcare.clinic_or_praxis",
    "pharmacy": "healthcare.pharmacy", "accountant": "office.accountant",
    "cpa": "office.accountant", "tax": "office.tax_advisor", "lawyer": "office.lawyer",
    "attorney": "office.lawyer", "law firm": "office.lawyer", "insurance": "office.insurance",
    "real estate": "office.estate_agent", "realtor": "office.estate_agent",
    "architect": "office.architect", "financial advisor": "office.financial_advisor",
    "marketing": "office.advertising_agency", "restaurant": "catering.restaurant",
    "cafe": "catering.cafe", "coffee": "catering.cafe", "bar": "catering.bar",
    "florist": "commercial.florist", "hardware": "commercial.houseware_and_hardware",
}
_GEOAPIFY_FALLBACK = "service,office,commercial,healthcare"


def _geoapify_categories(query: str) -> tuple[str, bool]:
    low = query.lower()
    hits = sorted({cat for word, cat in _GEOAPIFY_CATEGORIES.items() if word in low})
    return (",".join(hits), True) if hits else (_GEOAPIFY_FALLBACK, False)


def _geoapify_check(resp, provider: str = "geoapify_places") -> dict:
    """Geoapify documents no out-of-credits code: limits are 'soft' (they email, then may
    block). 429 = rate block, 402 treated as money; the rest via check_response."""
    if resp.status_code == 429:
        raise QuotaExhausted(f"{provider} HTTP 429: {_brief(resp)}")
    if resp.status_code == 402:
        raise BillingSignal(f"{provider} HTTP 402: {_brief(resp)}")
    return check_response(resp, provider)


class GeoapifyPlaces:
    """Geocode geo -> place_id (1 credit), then Places filter=place:<id> (1 credit, limit<=20).
    OSM data: websites are sparse; places without one are dropped."""
    kind = "maps"
    GEOCODE = "https://api.geoapify.com/v1/geocode/search"
    PLACES = "https://api.geoapify.com/v2/places"

    def __init__(self, key: str, **_):
        self.key = key

    def search(self, query: str, geo: str, limit: int):
        cats, mapped = _geoapify_categories(query)
        units = 0
        params = {"categories": cats, "limit": max(1, min(int(limit), 20)), "lang": "en",
                  "apiKey": self.key}
        if not mapped:
            params["name"] = query
        with http_client() as c:
            if geo:
                g = _geoapify_check(c.get(self.GEOCODE, params={
                    "text": geo, "limit": 1, "format": "json", "apiKey": self.key}))
                units += 1
                results = g.get("results") or []
                if not results or not results[0].get("place_id"):
                    raise ProviderError(f"geoapify_places: could not geocode {geo!r}")
                params["filter"] = f"place:{results[0]['place_id']}"
            else:
                raise ProviderError("geoapify_places: a geo is required (Places needs a filter or bias)")
            data = _geoapify_check(c.get(self.PLACES, params=params))
            units += 1
        out = []
        for f in data.get("features") or []:
            p = f.get("properties") or {}
            raw = (p.get("datasource") or {}).get("raw") or {}
            site = p.get("website") or raw.get("website") or raw.get("contact:website") or ""
            if not site:
                continue
            contact = p.get("contact") or {}
            phone = contact.get("phone") or raw.get("phone") or raw.get("contact:phone") or ""
            hours = p.get("opening_hours") or raw.get("opening_hours") or ""
            cats_out = list(p.get("categories") or [])
            out.append(Candidate(
                name=p.get("name", ""), website=site, phone=phone,
                address=p.get("formatted", ""), category=cats_out[-1] if cats_out else "",
                source="geoapify_places", categories=cats_out,
                hours=[hours] if hours else []))
        return out, units


# --------------------------------------------------------------------------- HERE

# 403 causes from HERE IAM "troubleshoot 403" table that mean a plan/subscription problem.
_HERE_PLAN_WORDS = ("resource plan", "plan attachment", "subscription", "billing", "payment")


def _here_check(resp, provider: str = "here_discover") -> dict:
    """HERE ErrorResponse: {"status", "title", "code", "cause", "action", "correlationId"}.
    Discover documents 400/405/429/503; IAM adds 401 (auth) and 403403 (with a cause)."""
    if resp.status_code < 400:
        return check_response(resp, provider)
    body = _json(resp, provider)
    text = f"{body.get('title', '')} {body.get('code', '')} {body.get('cause', '')} {body.get('action', '')}"
    msg = f"{provider} HTTP {resp.status_code}: {text.strip() or _brief(resp)}"
    if resp.status_code == 402 or (resp.status_code == 403 and any(w in text.lower() for w in _HERE_PLAN_WORDS)):
        raise BillingSignal(msg)
    if resp.status_code == 429:
        raise QuotaExhausted(msg)
    raise ProviderError(msg)


class HereDiscover:
    """Geocode geo (1 transaction) then /discover at that position (1 transaction).
    Discover needs `at` or in=circle/bbox; a country code alone is not accepted."""
    kind = "maps"
    GEOCODE = "https://geocode.search.hereapi.com/v1/geocode"
    DISCOVER = "https://discover.search.hereapi.com/v1/discover"

    def __init__(self, key: str, **_):
        self.key = key

    def usage(self) -> int | None:
        # HERE's Cost Management Usage API (usage.bam.api.here.com/v2/usage/realms/{realmId})
        # accepts only an OAuth 2.0 bearer token plus the org realm ID - not an API key.
        # The engine's own ledger enforces the 90%-of-5,000 stop instead.
        return None

    def search(self, query: str, geo: str, limit: int):
        if not geo:
            raise ProviderError("here_discover: a geo is required (discover needs a position)")
        with http_client() as c:
            g = _here_check(c.get(self.GEOCODE, params={"q": geo, "limit": 1, "apiKey": self.key}))
            items = g.get("items") or []
            pos = (items[0].get("position") if items else None) or {}
            if "lat" not in pos or "lng" not in pos:
                raise ProviderError(f"here_discover: could not geocode {geo!r}")
            data = _here_check(c.get(self.DISCOVER, params={
                "q": query, "at": f"{pos['lat']},{pos['lng']}", "limit": max(1, min(int(limit), 100)),
                "lang": "en", "apiKey": self.key}))
        out = []
        for it in data.get("items") or []:
            if it.get("resultType") not in (None, "place"):
                continue
            contacts = it.get("contacts") or []
            www = [w.get("value") for ct in contacts for w in ct.get("www") or [] if w.get("value")]
            if not www:
                continue
            phones = [p.get("value") for ct in contacts for p in ct.get("phone") or [] if p.get("value")]
            cats = [c.get("name", "") for c in it.get("categories") or [] if c.get("name")]
            primary = next((c.get("name", "") for c in it.get("categories") or [] if c.get("primary")),
                           cats[0] if cats else "")
            hours = [t for oh in it.get("openingHours") or [] for t in oh.get("text") or []]
            out.append(Candidate(
                name=it.get("title", ""), website=www[0], phone=phones[0] if phones else "",
                address=(it.get("address") or {}).get("label", ""), category=primary,
                source="here_discover", categories=cats, hours=hours))
        return out, 2


DISCOVERY_MORE = {
    "google_places": GooglePlaces,
    "outscraper_maps": OutscraperMaps,
    "hasdata_maps": HasDataMaps,
    "foursquare_places": FoursquarePlaces,
    "geoapify_places": GeoapifyPlaces,
    "here_discover": HereDiscover,
}

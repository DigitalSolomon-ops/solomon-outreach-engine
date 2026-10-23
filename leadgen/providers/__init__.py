"""Adapter registries per engine stage. Keys match provider names in config/capabilities.yaml."""
from .anymailfinder import AnymailFinder, AnymailFinderVerify
from .quickenrich import QuickEnrich
from .base import (BillingSignal, Candidate, Page, Person, ProviderError, QuotaExhausted,
                   VERDICTS, registrable_domain)
from .discovery import DISCOVERY as _DISCOVERY_WIRED
from .discovery_more import DISCOVERY_MORE
from .people import PEOPLE
from .scrape import SCRAPE as _SCRAPE_WIRED
from .scrape_more import SCRAPE_MORE
from .sizing import SIZING
from .verify import VERIFY as _VERIFY_WIRED
from .verify_more import VERIFY_MORE
from .websearch_more import WEB_MORE

DISCOVERY = {**_DISCOVERY_WIRED, **DISCOVERY_MORE, **WEB_MORE}
SCRAPE = {**_SCRAPE_WIRED, **SCRAPE_MORE}
VERIFY = {**_VERIFY_WIRED, **VERIFY_MORE, "anymailfinder_verify": AnymailFinderVerify}

PEOPLE = {**PEOPLE, "anymailfinder": AnymailFinder, "quickenrich": QuickEnrich}

STAGES = {"discovery": DISCOVERY, "scrape": SCRAPE, "verify": VERIFY, "people": PEOPLE,
          "sizing": SIZING}

__all__ = ["BillingSignal", "Candidate", "Page", "Person", "ProviderError", "QuotaExhausted",
           "VERDICTS", "registrable_domain", "DISCOVERY", "SCRAPE", "VERIFY", "PEOPLE", "SIZING",
           "STAGES"]

"""Small helpers shared by strategy modules."""
from __future__ import annotations

import re
from datetime import date

_SMALL_UPPER = {"llc", "inc", "lp", "llp", "pllc", "pc", "usa", "us", "dba", "ii", "iii", "iv"}


def state_of(geo: str) -> str:
    """'Gastonia, NC' -> 'NC'."""
    m = re.search(r",\s*([A-Za-z]{2})\s*$", geo or "")
    return m.group(1).upper() if m else ""


def city_of(geo: str) -> str:
    return (geo or "").split(",")[0].strip()


def tidy_name(name: str) -> str:
    """'JL PUBLIC SAFETY RADIO LLC' -> 'JL Public Safety Radio LLC'. Mixed-case names are kept."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name or name != name.upper():
        return name
    out = []
    for w in name.split(" "):
        core = re.sub(r"[^A-Za-z]", "", w).lower()
        if core in _SMALL_UPPER or (len(core) <= 3 and w.isalpha() and not set(core) & set("aeiouy")):
            out.append(w.upper())
        else:
            out.append(w[:1] + w[1:].lower())
    return " ".join(out)


def long_date(d: date) -> str:
    return f"{d:%B} {d.day}, {d.year}"

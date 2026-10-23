"""Self-adjusting ICP inside a campaign's own segment x geo cells.

The day's discovery query budget is split across cells by Thompson sampling on
Beta(1 + positive replies, 1 + sends - positive replies), positive = interested,
question or referral. A cell keeps at least 5% of queries until it has 100 sends.
The engine never adds cells by itself; the strategist may propose them.
"""
from __future__ import annotations

import random

from .replies import POSITIVE


def cell_stats(ledger, campaign: dict) -> dict[str, dict]:
    stats = {c["id"]: {"sends": 0, "positive": 0} for c in campaign["icp"]["cells"]}
    for lead in ledger.leads(campaign["id"]):
        cid = lead.get("cell_id")
        if cid not in stats:
            continue
        if 0 in (lead.get("seq") or {}).get("sent_steps", []):
            stats[cid]["sends"] += 1
        if lead.get("reply_class") in POSITIVE:
            stats[cid]["positive"] += 1
    return stats


def thompson_shares(stats: dict[str, dict], floor: float = 0.05, floor_until: int = 100,
                    draws: int = 2000, rng: random.Random | None = None) -> dict[str, float]:
    if not stats:
        return {}
    rng = rng or random.Random()
    wins = dict.fromkeys(stats, 0)
    for _ in range(draws):
        best = max(stats, key=lambda k: rng.betavariate(1 + stats[k]["positive"],
                                                        1 + max(0, stats[k]["sends"] - stats[k]["positive"])))
        wins[best] += 1
    raw = {k: v / draws for k, v in wins.items()}
    floored = {k for k, s in stats.items() if s["sends"] < floor_until}
    floor = min(floor, 1 / len(stats))
    # Pin floored cells that sample under the floor at the floor; the rest share what's left
    # in proportion to their sampled share. Repeat until nothing new drops under.
    fixed: dict[str, float] = {}
    while True:
        free = [k for k in stats if k not in fixed]
        mass = 1 - sum(fixed.values())
        tot = sum(raw[k] for k in free)
        shares = {k: (mass * raw[k] / tot if tot else mass / len(free)) for k in free}
        low = [k for k in free if k in floored and shares[k] < floor]
        if not low:
            return {**fixed, **shares}
        for k in low:
            fixed[k] = floor


def allocate_queries(shares: dict[str, float], n: int) -> dict[str, int]:
    """Largest-remainder split of n queries by share."""
    raw = {k: s * n for k, s in shares.items()}
    out = {k: int(v) for k, v in raw.items()}
    left = n - sum(out.values())
    for k in sorted(raw, key=lambda k: raw[k] - out[k], reverse=True)[:left]:
        out[k] += 1
    return out


def rebalance(settings: dict, ledger, campaign: dict, n_queries: int, rng=None) -> tuple[dict, list[str]]:
    """Return ({cell_id: queries}, notices). Notices: >15pp moves and cells starved to the floor."""
    cfg = settings["thompson"]
    stats = cell_stats(ledger, campaign)
    shares = thompson_shares(stats, cfg["floor_share"], cfg["floor_until_sends"], rng=rng)
    prev = {k: v.get("share") for k, v in (campaign.get("cells_state") or {}).items()}
    notices = []
    floor = min(cfg["floor_share"], 1 / max(1, len(stats)))
    for k, s in shares.items():
        p = prev.get(k)
        if p is not None and abs(s - p) * 100 > cfg["shift_notice_pp"]:
            notices.append(f"cell {k} moved from {p:.0%} to {s:.0%} of queries")
        if p is not None and p > floor + 0.01 and abs(s - floor) < 0.005 and len(stats) > 1:
            notices.append(f"cell {k} was starved to the {floor:.0%} floor")
    state = {k: {"share": round(shares[k], 4), **stats[k]} for k in shares}
    ledger.save_campaign(campaign["id"], {"cells_state": state})
    return allocate_queries(shares, n_queries), notices

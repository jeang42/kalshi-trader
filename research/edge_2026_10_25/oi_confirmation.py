"""Open-interest confirmation study — preregistered docs/edge-research-log.md 2026-10-25.

Read-only. No venue calls, no writes under data/, no trading imports.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from datetime import UTC, datetime

import numpy as np

DB = os.getenv("CANDLE_PANEL_DB", "data/shared/candle_panel.sqlite")
OUT = os.path.dirname(os.path.abspath(__file__))

BANDS = [(0.05, 0.15), (0.15, 0.35), (0.35, 0.65), (0.65, 0.85), (0.85, 0.95)]
BAND_NAMES = ["5-15c", "15-35c", "35-65c", "65-85c", "85-95c"]
PRIMARY_LEAD, PRIMARY_W = 12.0, 6.0
EXPLORATORY = [(2.0, 6.0), (24.0, 24.0)]
OOS_SPLIT = "2026-10-16"
RNG = np.random.default_rng(20261025)


def con():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def close_ts(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00"))
                   .astimezone(UTC).timestamp())
    except Exception:
        return None


def fee(p, c=10):
    return math.ceil(0.07 * p * (1 - p) * c * 100) / 100


def build(lead, w):
    """One row per market: entry candle at close-lead, prior candle at close-(lead+w)."""
    cx = con()
    mk = cx.execute(
        "SELECT ticker, event_id, category, close_time, resolved_yes "
        "FROM markets WHERE fetched_at IS NOT NULL AND resolved_yes IS NOT NULL"
    ).fetchall()
    rows = []
    q = ("SELECT ts, yes_bid, yes_ask, open_interest FROM candles "
         "WHERE ticker=? AND ts BETWEEN ? AND ? AND open_interest>0 "
         "ORDER BY ABS(ts-?) LIMIT 1")
    for m in mk:
        ct = close_ts(m["close_time"])
        if ct is None:
            continue
        a = ct - lead * 3600
        b = ct - (lead + w) * 3600
        now = cx.execute(q, (m["ticker"], a - 3600, a + 3600, a)).fetchone()
        if now is None:
            continue
        prev = cx.execute(q, (m["ticker"], b - 3600, b + 3600, b)).fetchone()
        if prev is None or prev["ts"] >= now["ts"]:
            continue
        bid, ask = now["yes_bid"], now["yes_ask"]
        if not (bid > 0 and ask > 0) or (ask - bid) > 0.05:
            continue
        pbid, pask = prev["yes_bid"], prev["yes_ask"]
        if not (pbid > 0 and pask > 0):
            continue
        mid, pmid = (bid + ask) / 2, (pbid + pask) / 2
        oi_prev = prev["open_interest"]
        rows.append({
            "ticker": m["ticker"], "event_id": m["event_id"] or m["ticker"],
            "category": m["category"] or "", "close_time": m["close_time"],
            "y": int(m["resolved_yes"]), "mid": mid, "bid": bid, "ask": ask,
            "drift": mid - pmid,
            "oi_growth": (now["open_interest"] - oi_prev) / max(oi_prev, 25.0),
        })
    cx.close()
    return rows


def cluster_boot(vals, events, n=4000):
    """Bootstrap the mean, resampling whole event clusters."""
    uniq = np.unique(events)
    if len(uniq) < 2:
        return float("nan"), float("nan")
    idx = {e: np.where(events == e)[0] for e in uniq}
    out = np.empty(n)
    for i in range(n):
        pick = RNG.choice(uniq, size=len(uniq), replace=True)
        sel = np.concatenate([idx[e] for e in pick])
        out[i] = vals[sel].mean()
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def holm(pvals):
    order = np.argsort(pvals)
    m = len(pvals)
    adj = np.empty(m)
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * pvals[i]))
        adj[i] = run
    return adj


def p_from_boot(vals, events, n=4000):
    """Two-sided cluster-bootstrap p for mean != 0."""
    uniq = np.unique(events)
    if len(uniq) < 2:
        return 1.0
    idx = {e: np.where(events == e)[0] for e in uniq}
    obs = vals.mean()
    draws = np.empty(n)
    for i in range(n):
        pick = RNG.choice(uniq, size=len(uniq), replace=True)
        sel = np.concatenate([idx[e] for e in pick])
        draws[i] = vals[sel].mean()
    centered = draws - obs
    return float(min(1.0, 2 * min((centered >= obs).mean(), (centered <= obs).mean()) + 1e-9))


def analyse(rows, lead, w, label, primary):
    funded = [r for r in rows if r["category"] not in ("Sports", "Crypto")]
    cells = []
    for (lo, hi), bname in zip(BANDS, BAND_NAMES, strict=False):
        inband = [r for r in funded if lo <= r["mid"] < hi]
        if len(inband) < 30:
            continue
        g = np.array([r["oi_growth"] for r in inband])
        cut = np.percentile(g, 100 * 2 / 3)
        top = [r for r, gv in zip(inband, g, strict=False) if gv >= cut]
        for sign, sname in ((1, "up"), (-1, "down")):
            sel = [r for r in top
                   if abs(r["drift"]) >= 0.01 and np.sign(r["drift"]) == sign]
            if len(sel) < 10:
                continue
            for half in ("ALL", "IN", "OUT"):
                if half == "IN":
                    s = [r for r in sel if (r["close_time"] or "") <= OOS_SPLIT]
                elif half == "OUT":
                    s = [r for r in sel if (r["close_time"] or "") > OOS_SPLIT]
                else:
                    s = sel
                if len(s) < 10:
                    continue
                ev = np.array([r["event_id"] for r in s])
                y = np.array([r["y"] for r in s], float)
                mid = np.array([r["mid"] for r in s], float)
                dev = y - mid                      # stage 1: realized vs mid
                lo_ci, hi_ci = cluster_boot(dev, ev)
                pv = p_from_boot(dev, ev) if (primary and half == "ALL") else float("nan")
                # stage 2: buy the signalled side at the executable quote
                if sign == 1:
                    cost = np.array([r["ask"] for r in s])
                    payoff = y
                else:
                    cost = np.array([1 - r["bid"] for r in s])
                    payoff = 1 - y
                net = (payoff - cost) * 10 - np.array([fee(c) for c in cost])
                n_lo, n_hi = cluster_boot(net, ev)
                cells.append({
                    "lead": lead, "window": w, "band": bname, "side": sname,
                    "half": half, "n": len(s), "events": int(len(np.unique(ev))),
                    "realized_yes": float(y.mean()), "mean_mid": float(mid.mean()),
                    "dev_pp": float(dev.mean() * 100),
                    "dev_ci_pp": [lo_ci * 100, hi_ci * 100],
                    "p_raw": pv,
                    "net_dollars_per_event": float(net.mean()),
                    "net_ci": [n_lo, n_hi],
                })
    prim = [c for c in cells if c["half"] == "ALL" and primary]
    if prim:
        adj = holm(np.array([c["p_raw"] for c in prim]))
        for c, a in zip(prim, adj, strict=False):
            c["p_holm"] = float(a)
    return {"label": label, "lead": lead, "window": w,
            "n_rows_funded": len(funded), "n_rows_all": len(rows),
            "cells": cells}


def main():
    res = {"generated_at": datetime.now(UTC).isoformat(), "db": DB,
           "oos_split": OOS_SPLIT, "families": []}
    rows = build(PRIMARY_LEAD, PRIMARY_W)
    print(f"primary lead={PRIMARY_LEAD}h rows={len(rows)}", file=sys.stderr)
    res["families"].append(analyse(rows, PRIMARY_LEAD, PRIMARY_W, "PRIMARY", True))
    for lead, w in EXPLORATORY:
        r = build(lead, w)
        print(f"exploratory lead={lead}h rows={len(r)}", file=sys.stderr)
        res["families"].append(analyse(r, lead, w, "EXPLORATORY", False))
    path = os.path.join(OUT, "oi_confirmation.json")
    with open(path, "w") as f:
        json.dump(res, f, indent=2)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()

"""The Appraiser: grades real, currently active pump.fun coins from real chain and X data.

  python appraise.py run       grade the most active living coins (cron, every 30 minutes)
  python appraise.py review    how each grade really did afterwards

A grade describes the quality of the evidence we found about a coin. It is not a buy or sell call, and it says
nothing about the future until `review` has enough real outcomes behind it.
"""
import glob
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "copilot"))
import coin_report as cr  # noqa: E402
import x_check  # noqa: E402
from executor import prices  # noqa: E402
from village import DB, log  # noqa: E402

GRADES = [("F", 0, "white"), ("E", 35, "blue"), ("D", 50, "green"), ("C", 62, "purple"),
          ("B", 72, "orange"), ("A", 82, "red"), ("S", 92, "rainbow")]
PER_RUN = 5            # Helius free plan: about 50 calls per coin
MIN_CAP_SOL = 60       # free DexScreener filter first: no Helius calls on coins that are already dead
X_PER_RUN = 1
X_PER_DAY = 2          # one check costs about $0.03
X_MIN_SCORE = 60      # X checks only for coins that already score well on chain data
REGRADE_AFTER_S = 6 * 3600
XLOG = HERE / "appraise_x.json"
SCHEMA = """CREATE TABLE IF NOT EXISTS coins(id INTEGER PRIMARY KEY, ts INT, mint TEXT, name TEXT, symbol TEXT, grade TEXT,
  score INT, mcap_sol REAL, reasons TEXT, narrative TEXT, x_checked INT, peak_mcap REAL, last_mcap REAL, dev_pct REAL, smart TEXT);
CREATE INDEX IF NOT EXISTS ix_coins_mint ON coins(mint, ts);"""


def chain_score(r):
    """Score from real chain data. Returns (score, good points, bad points, critical?)."""
    F, L, D, C = r["flow"], r["launch"], r.get("dev") or {}, r.get("copycats") or {}
    V, K, U, meta = r.get("live") or {}, r.get("chart") or {}, r.get("funding") or {}, r.get("meta") or {}
    s, good, bad, critical = 45, [], [], False

    def add(pts, why):
        nonlocal s
        s += pts
        (good if pts > 0 else bad).append(why)

    if F.get("unique_buyers", 0) >= 100:
        add(min(12, F["unique_buyers"] // 40 + 4), f"{F['unique_buyers']} buyers")
    if F.get("buy_sol", 0) - F.get("sell_sol", 0) >= 5:
        add(6, "more buying than selling")
    if r["graduated"]:
        add(10, "graduated")
    if F.get("last_trade_age_s", 0) > 3600:
        add(-15, "gone quiet")
    t10 = V.get("top10_pct")
    if t10 is not None:
        # tokens on the curve belong to nobody: the top 10 is measured against the sold part
        if "curve_pct" in V:
            t10 = min(1.0, t10 / max(0.01, V["curve_pct"] * cr.CURVE_TOKENS / cr.SUPPLY))
        if t10 <= 0.2:
            add(8, f"spread out, top 10 hold {t10:.0%}")
        elif t10 > 0.5:
            add(-25, f"top 10 hold {t10:.0%}")
        elif t10 > 0.3:
            add(-15, f"top 10 hold {t10:.0%}")
    b0 = L.get("block0_supply_pct", 0)
    if b0 >= 0.2:
        add(-15, f"launch buyers took {b0:.0%}")
    elif b0 >= 0.1:
        add(-7, f"launch buyers took {b0:.0%}")
    if L.get("dev_sold_all"):
        add(-20, "dev sold everything")
        critical = True
    elif L.get("dev_sold_pct", 0) > 0:
        add(-8, "dev sold some")
    if V.get("dev_holds_pct", 0) >= 0.05:
        add(-10, "dev holds a lot")
    if D.get("other_coins", 0) >= 5 and not D.get("graduated"):
        add(-15, "serial deployer")
    elif D.get("graduated"):
        add(5, "dev has graduated before")
    if C.get("same_uri"):
        add(-5, "reused metadata")
    if r.get("meta") is not None and "error" not in meta and not any(k in meta for k in ("twitter", "website", "telegram")):
        add(-8, "no socials")
    if K.get("enough") and K["top3_share"] >= 0.6:
        add(-20, "chart may be fake")
        critical = True
    elif K.get("enough") and K["fee_per_sol"] < 0.0001:
        add(-15, "very low fees for the volume")
        critical = True
    elif K.get("enough") and K["top3_share"] <= 0.3:
        add(5, "many different traders")
    if U.get("same_funder", 0) >= 3:
        add(-20, "top holders share a funder")
        critical = True
    if U.get("low_sol_wallets", 0) >= 6:
        add(-10, "top holders nearly empty")
    return s, good, bad, critical


def narrative_score(x):
    """Points from the X check. Returns (points, good, bad, authentic?, credible count)."""
    d = (x or {}).get("data")
    if not isinstance(d, dict) or x.get("degraded"):
        return 0, [], [], False, 0
    pts, good, bad = 0, [], []
    conf = str(d.get("confidence", "")).lower()
    if conf == "high":
        pts += 10
    elif conf == "medium":
        pts += 5
    auth = d.get("origin_authentic")
    if auth is True:
        pts += 10
        good.append("real story confirmed on X")
    elif auth is False:
        pts -= 20
        bad.append("story looks false")
    cred = d.get("credible_mentions") or []
    n = len(cred) if isinstance(cred, list) else 0
    if n:
        pts += min(10, 4 * n)
        good.append(f"{n} credible mention(s)")

    def real(v):
        return bool(v) and str(v).strip().lower() not in ("none found", "none", "null", "[]")
    if real(d.get("shill_signals")):
        pts -= 8
        bad.append("signs of paid shilling")
    if real(d.get("contradictions")):
        pts -= 12
        bad.append("contradictions found")
    return pts, good, bad, auth is True, n


def grade_of(score, critical, has_x, authentic, credible, r):
    idx = max(i for i, (_, low, _) in enumerate(GRADES) if score >= low)
    if critical:
        idx = min(idx, 1)      # a serious red flag caps the coin at E
    if not has_x:
        idx = min(idx, 3)      # no narrative evidence, no higher than C
    elif not (authentic or credible):
        idx = min(idx, 3)
    if idx == 6 and not (authentic and credible >= 2 and r["graduated"]):
        idx = 5
    return GRADES[idx][0]


def candidates(db):
    since = time.time() - 5400
    rows = {}
    for f in sorted(glob.glob(str(cr.DATA / "pump_*.sqlite")))[-2:]:
        d = sqlite3.connect(f"file:{Path(f).as_posix()}?mode=ro", uri=True, timeout=30)
        for mint, n in d.execute("SELECT mint, count(DISTINCT user) FROM pump_trades WHERE ts>? AND mayhem=0 AND "
                                 "quote_mint IS NULL GROUP BY mint HAVING count(*)>=30", (since,)):
            rows[mint] = max(rows.get(mint, 0), n)
        d.close()
    recent = {m for (m,) in db.execute("SELECT mint FROM coins WHERE ts>?", (time.time() - REGRADE_AFTER_S,))}
    ranked = [m for m, _ in sorted(rows.items(), key=lambda kv: -kv[1]) if m not in recent][:30]
    mk = prices(ranked) if ranked else {}
    alive = [m for m in ranked if m in mk and mk[m]["mcap"] >= MIN_CAP_SOL and mk[m]["buys"] + mk[m]["sells"] >= 5]
    return alive[:PER_RUN]


def x_allowed():
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        b = json.loads(XLOG.read_text())
    except (OSError, ValueError):
        b = {}
    return b if b.get("day") == day else {"day": day, "n": 0}


def grade_one(db, mint, smart=()):
    """Deep check and grade for one coin, used by the live watcher."""
    r = cr.build_report(mint)
    s, good, bad, crit = chain_score(r)
    budget, x = x_allowed(), None
    npts, ng, nb, auth, cred = 0, [], [], False, 0
    if not crit and budget["n"] < X_PER_DAY and s >= X_MIN_SCORE:
        try:
            x = x_check.coin_check(r)
            budget["n"] += 1
            XLOG.write_text(json.dumps(budget))
            npts, ng, nb, auth, cred = narrative_score(x)
        except x_check.XError as e:
            log("appraiser", "skip", str(e)[:100])
    score = max(0, min(100, s + npts))
    grade = grade_of(score, crit, x is not None, auth, cred, r)
    c = r.get("coin") or {}
    narrative = (x or {}).get("data", {}).get("narrative") if isinstance((x or {}).get("data"), dict) else None
    db.execute("INSERT INTO coins(ts,mint,name,symbol,grade,score,mcap_sol,reasons,narrative,x_checked,dev_pct,smart) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (int(time.time()), mint, c.get("name"), c.get("symbol"), grade, score, r["flow"].get("mcap_sol"),
                json.dumps({"good": good + ng, "bad": bad + nb}, ensure_ascii=False), narrative, int(x is not None),
                (r.get("live") or {}).get("dev_holds_pct"), json.dumps(list(smart), ensure_ascii=False)))
    db.commit()
    log("appraiser", "grade", f"{c.get('symbol') or mint[:6]}: {grade} ({score})")


def run():
    db = sqlite3.connect(DB)
    db.executescript(SCHEMA)
    mints = candidates(db)
    log("appraiser", "working", f"Appraising {len(mints)} active coins")
    done = []
    for mint in mints:
        try:
            r = cr.build_report(mint)
        except Exception as e:  # noqa: BLE001 — one bad coin must not stop the run
            log("appraiser", "skip", f"{mint[:6]}: {type(e).__name__}")
            continue
        s, good, bad, crit = chain_score(r)
        done.append([r, s, good, bad, crit])
    done.sort(key=lambda x: -x[1])
    budget, x_left = x_allowed(), X_PER_RUN
    for row in done:
        r, s, good, bad, crit = row
        x, npts, auth, cred, ng, nb = None, 0, False, 0, [], []
        if x_left and not crit and budget["n"] < X_PER_DAY and s >= X_MIN_SCORE:
            try:
                x = x_check.coin_check(r)
                budget["n"] += 1
                x_left -= 1
                npts, ng, nb, auth, cred = narrative_score(x)
            except x_check.XError as e:
                log("appraiser", "skip", str(e)[:100])
        score = max(0, min(100, s + npts))
        grade = grade_of(score, crit, x is not None, auth, cred, r)
        c = r.get("coin") or {}
        narrative = ((x or {}).get("data") or {}).get("narrative") if isinstance((x or {}).get("data"), dict) else None
        db.execute("INSERT INTO coins(ts,mint,name,symbol,grade,score,mcap_sol,reasons,narrative,x_checked) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (int(time.time()), r["mint"], c.get("name"), c.get("symbol"), grade, score, r["flow"].get("mcap_sol"),
                    json.dumps({"good": good + ng, "bad": bad + nb}, ensure_ascii=False), narrative, int(x is not None)))
        db.commit()
        log("appraiser", "grade", f"{c.get('symbol') or r['mint'][:6]}: {grade} ({score})")
    XLOG.write_text(json.dumps(budget))
    log("appraiser", "done", f"{len(done)} coins graded")


def review():
    sys.path.insert(0, str(HERE.parent / "copilot"))
    from copilot import outcome
    db = sqlite3.connect(DB)
    now, by = time.time(), {}
    for ts, mint, grade in db.execute("SELECT ts,mint,grade FROM coins"):
        if now - ts >= 3600 and (o := outcome(mint, ts)):
            by.setdefault(grade, []).append(o)
    print("How coins did in the hour after grading (real recorded prices, before fees):")
    for name, _, color in GRADES:
        v = by.get(name)
        if v:
            med = sorted(o["end"] for o in v)[len(v) // 2]
            print(f"  {name:9s} ({color}) n={len(v):3d} median {med:+.0%}, doubled {sum(o['peak'] >= 1 for o in v) / len(v):.0%}, "
                  f"halved {sum(o['trough'] <= -.5 for o in v) / len(v):.0%}")
    print("Small samples prove nothing. Treat grades as unproven until each has 50+ coins.")


if __name__ == "__main__":
    {"run": run, "review": review}[sys.argv[1] if len(sys.argv) > 1 else "run"]()

"""Trade simulator on live prices. No wallet, no orders, no AI calls. Rules: docs/rules.md

  python executor.py        runs forever (tmux "executor")
"""
import glob
import json
import sqlite3
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "copilot"))
from chain_checks import DECODERS, sigs, tx  # noqa: E402
from coin_report import DATA, env_file  # noqa: E402
from village import DB, log  # noqa: E402

START_SOL = 1.0
SIZE_SOL = 0.05
MAX_OPEN = 3
FEE = 0.0125
PRIO_SOL = 0.0005
STOP = -0.20
TAKE_1, TAKE_2 = 0.50, 1.5   # +50%: stake back, +150%: half of the rest, the remainder trails
TRAIL = 0.30
MAX_HOLD_S = 3600
RUNNER_HOLD_S = 3 * 3600
SIZE_PCT, SIZE_MAX = 0.05, 0.25   # position size grows with the balance
RULES = env_file().get("RULES_TAG") or "default"
MIN_AGE_S = 1800       # the coin must have been at least this old when graded
PUMP_DEXES = ("pumpfun", "pumpswap")  # pump.fun coins only: the curve or PumpSwap
MIN_CAP_USD = 7000     # minimum market cap
MIN_VOL5_SOL = 5       # minimum volume in the last 5 minutes
MAX_DEV_PCT = .05      # maximum share of supply held by the developer
MAX_RISE_5M = 40       # %: skip coins that are spiking right now
GRADE_SIZE = {"D": 0.4, "C": 1.0, "B": 1.5, "A": 1.5, "S": 1.5}
DAILY_STOP = -0.3
STREAK = 4
PAUSE_S = 7200
ENTRY_GRADES = ("D", "C", "B", "A", "S")
FRESH_S = 1200
TICK_S = 30
FAST_S = 3             # how often stops and trailing exits are checked, on fresh recorder prices
FAST_MAX_AGE_S = 60    # do not act on a last trade older than this
LAST_MK = {}           # latest DexScreener market per coin: depth and price for fills
POOLS = {}
SCALE = {}             # DexScreener price / recorder price per coin, last 10 rounds
MIN_V_SOL = 30.0

SCHEMA = """CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY, mint TEXT, symbol TEXT, grade TEXT, opened_ts INT,
  entry_price REAL, spent_sol REAL, tokens REAL, peak REAL, half_sold INT DEFAULT 0, received_sol REAL DEFAULT 0,
  last_value REAL, closed_ts INT, reason TEXT, last_price REAL, rules TEXT DEFAULT 'default');
CREATE TABLE IF NOT EXISTS equity(ts INT PRIMARY KEY, sol REAL);"""


def prices(mints):
    """Best SOL pair per mint from DexScreener's free API: {mint: market dict}."""
    out = {}
    for i in range(0, len(mints), 30):
        req = urllib.request.Request("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(mints[i:i + 30]),
                                     headers={"User-Agent": "pumptown"})
        with urllib.request.urlopen(req, timeout=15) as r:
            pairs = json.load(r)
        for p in pairs:
            if p.get("quoteToken", {}).get("symbol") != "SOL" or not p.get("priceNative"):
                continue
            m = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if m not in out or liq > out[m]["liq"]:
                price, usd = float(p["priceNative"]), float(p.get("priceUsd") or 0)
                sol_usd = usd / price if price else 0
                mcap = (p.get("marketCap") or 0) / sol_usd if sol_usd else 0
                # reserve for price impact: from pool liquidity, or on the curve mcap = vSol^2 / 32.19
                depth = liq / 2 / sol_usd if liq and sol_usd else max(MIN_V_SOL, (mcap * 32.19) ** .5)
                t = (p.get("txns") or {}).get("m5") or {}
                out[m] = {"price": price, "mcap": mcap, "depth": depth, "buys": t.get("buys", 0), "sells": t.get("sells", 0),
                          "chg5": (p.get("priceChange") or {}).get("m5") or 0, "dex": p.get("dexId"), "pair": p.get("pairAddress"),
                          "mcap_usd": p.get("marketCap") or 0, "born": (p.get("pairCreatedAt") or 0) / 1000,
                          "vol5": ((p.get("volume") or {}).get("m5") or 0) / sol_usd if sol_usd else 0}
    return out


def chain_price(mint):
    """Fresh price from the latest chain trades. DexScreener can lag by seconds."""
    got = []
    for s in sigs(mint, 12):
        if s.get("err") or len(got) >= 3:
            continue
        t = tx(s["signature"])
        for line in ((t or {}).get("meta") or {}).get("logMessages") or []:
            if not line.startswith("Program data: "):
                continue
            for dec in DECODERS:
                r = dec.decode_b64(line[14:])
                if not r:
                    continue
                n, f = r
                if n in ("BuyEvent", "SellEvent") and f.get("pool_base_token_reserves"):
                    got.append((f["pool_quote_token_reserves"] / 1e9) / (f["pool_base_token_reserves"] / 1e6))
                elif n == "TradeEvent" and f.get("virtual_token_reserves"):
                    got.append((f["virtual_sol_reserves"] / 1e9) / (f["virtual_token_reserves"] / 1e6))
    return statistics.median(got) if got else None


def fresh(mint, m, worse):
    """Fills use the worse price: the lower one when selling, the higher one when buying."""
    cp = chain_price(mint)
    return {**m, "price": worse(m["price"], cp)} if cp else m


def value(tokens, mk):
    """What selling these tokens would really bring in, after impact, fee and priority fee."""
    gross = tokens * mk["price"]
    gross *= 1 - min(.5, gross / mk["depth"])
    return max(0.0, gross * (1 - FEE) - PRIO_SOL)


def day_start():
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def pnl_of(row):
    return row[0] + (row[2] or 0) - row[1]


def risk_ok(db):
    closed = db.execute("SELECT received_sol, spent_sol, 0, closed_ts FROM positions WHERE closed_ts IS NOT NULL "
                        "ORDER BY closed_ts DESC LIMIT 200").fetchall()
    if sum(pnl_of(r) for r in closed if r[3] >= day_start()) <= DAILY_STOP:
        return False, "daily stop"
    last = closed[:STREAK]
    if len(last) == STREAK and all(pnl_of(r) < 0 for r in last) and time.time() - last[0][3] < PAUSE_S:
        return False, "losing streak pause"
    return True, ""


NOTED = set()


def note(mint, name, why):
    """Log each reason once per coin. These filters may still pass within the fresh window."""
    if (mint, why) not in NOTED:
        NOTED.add((mint, why))
        log("trader", "pass", f"{name}: {why}")


def born(mint, m):
    """Coin creation time: the earlier of the DexScreener pair date and the first recorded trade."""
    ts = [m["born"]] if m.get("born") else []
    for f in sorted(glob.glob(str(DATA / "pump_*.sqlite")))[-3:]:
        d = sqlite3.connect(f"file:{Path(f).as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            r = d.execute("SELECT min(ts) FROM pump_trades WHERE mint=?", (mint,)).fetchone()
            if r and r[0]:
                ts.append(r[0])
        finally:
            d.close()
    return min(ts) if ts else None


def enter(db):
    open_now = db.execute("SELECT count(*) FROM positions WHERE closed_ts IS NULL").fetchone()[0]
    if open_now >= MAX_OPEN:
        return
    ok, why = risk_ok(db)
    if not ok:
        return
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='coins'").fetchone():
        return
    marks = ",".join("?" * len(ENTRY_GRADES))
    cands = db.execute(f"SELECT mint,symbol,grade,mcap_sol,dev_pct,ts FROM coins WHERE ts>? AND grade IN ({marks}) ORDER BY score DESC",
                       (time.time() - FRESH_S, *ENTRY_GRADES)).fetchall()
    held = {m for (m,) in db.execute("SELECT mint FROM positions")}
    cands = [c for c in cands if c[0] not in held]
    if not cands:
        return
    mk = prices([c[0] for c in cands])
    for mint, sym, grade, mcap, dev_pct, graded in cands:
        m = mk.get(mint)
        name = sym or mint[:6]
        if not m or open_now >= MAX_OPEN:
            continue
        if mcap and m["mcap"] >= mcap * 1.5:
            log("trader", "pass", f"{name}: already ran too far")
            db.execute("INSERT INTO positions(mint,symbol,grade,opened_ts,closed_ts,reason,spent_sol) VALUES(?,?,?,?,?,?,0)",
                       (mint, sym, grade, int(time.time()), int(time.time()), "skipped: ran too far"))
            continue
        if mcap and m["mcap"] <= mcap * .5:
            log("trader", "pass", f"{name}: collapsed since grading")
            db.execute("INSERT INTO positions(mint,symbol,grade,opened_ts,closed_ts,reason,spent_sol) VALUES(?,?,?,?,?,?,0)",
                       (mint, sym, grade, int(time.time()), int(time.time()), "skipped: collapsed"))
            continue
        if m["dex"] not in PUMP_DEXES or (dev_pct or 0) > MAX_DEV_PCT:
            why = "not a pump.fun market" if m["dex"] not in PUMP_DEXES else f"dev holds {dev_pct:.0%}"
            log("trader", "pass", f"{name}: {why}")
            db.execute("INSERT INTO positions(mint,symbol,grade,opened_ts,closed_ts,reason,spent_sol) VALUES(?,?,?,?,?,?,0)",
                       (mint, sym, grade, int(time.time()), int(time.time()), f"skipped: {why}"))
            continue
        if dev_pct is None:
            note(mint, name, "no dev holding data")
            continue
        if m["mcap_usd"] < MIN_CAP_USD:
            note(mint, name, "cap under the minimum")
            continue
        if m["vol5"] < MIN_VOL5_SOL:
            note(mint, name, "5-minute volume too low")
            continue
        if m["buys"] + m["sells"] < 5 or m["buys"] < m["sells"] * .8:
            continue
        if m["chg5"] > MAX_RISE_5M:
            continue  # not excluded: checked again while the grade is fresh
        b = born(mint, m)
        if not b or graded - b < MIN_AGE_S:
            note(mint, name, "under 30 minutes old when graded" if b else "no coin age data")
            continue  # not final: the coin will be older if graded again
        m = fresh(mint, m, max)
        real = db.execute("SELECT coalesce(sum(received_sol-spent_sol),0) FROM positions WHERE closed_ts IS NOT NULL").fetchone()[0]
        size = round(min(SIZE_MAX, max(SIZE_SOL, (START_SOL + real) * SIZE_PCT) * GRADE_SIZE[grade]), 4)
        net = size * (1 - FEE)
        fill = m["price"] * (1 + min(.5, net / m["depth"]))
        db.execute("INSERT INTO positions(mint,symbol,grade,opened_ts,entry_price,spent_sol,tokens,peak,last_value,rules) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (mint, sym, grade, int(time.time()), fill, size + PRIO_SOL, net / fill,
                                                   m["price"], size, RULES))
        open_now += 1
        LAST_MK[mint] = m
        log("trader", "trade", f"Bought {name}: {size:.3f} SOL at {m['mcap']:.0f} SOL cap ({grade})")
    db.commit()


def live_price(mint):
    """Latest recorded price (curve or PumpSwap) in SOL per token. None without a recent trade."""
    files = sorted(glob.glob(str(DATA / "pump_*.sqlite")))[-2:]
    best = None
    for f in files:
        d = sqlite3.connect(f"file:{Path(f).as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            r = d.execute("SELECT ts,vsr,vtr,mayhem FROM pump_trades WHERE mint=? AND vtr>0 ORDER BY slot DESC LIMIT 1", (mint,)).fetchone()
            if r and not r[3]:
                best = max(best or (0, 0), (r[0], (r[1] / 1e9) / (r[2] / 1e6)))
            mk = LAST_MK.get(mint) or {}
            if mint not in POOLS and mk.get("dex") == "pumpswap" and mk.get("pair"):
                POOLS[mint] = mk["pair"]   # the pool event can be missing, the DexScreener pair address fills the gap
            if mint not in POOLS:
                p = d.execute("SELECT json_extract(body,'$.pool') FROM events_json WHERE name='CreatePoolEvent' AND mint=?", (mint,)).fetchone()
                if p:
                    POOLS[mint] = p[0]
            if POOLS.get(mint):
                r = d.execute("SELECT ts,pool_quote,pool_base FROM amm_trades WHERE pool=? AND pool_base>0 ORDER BY slot DESC LIMIT 1",
                              (POOLS[mint],)).fetchone()
                if r:
                    best = max(best or (0, 0), (r[0], (r[1] / 1e9) / (r[2] / 1e6)))
        finally:
            d.close()
    if best and time.time() - best[0] <= FAST_MAX_AGE_S:
        return best[1]
    return None


def fast_stop(db):
    """Exits only: stops and trailing exits should not wait for the 30 second DexScreener round."""
    rows = db.execute("SELECT id,mint,symbol,entry_price,spent_sol,tokens,peak,half_sold,received_sol "
                      "FROM positions WHERE closed_ts IS NULL").fetchall()
    for pid, mint, sym, entry, spent, tokens, peak, half, recv in rows:
        m, p, k = LAST_MK.get(mint), live_price(mint), SCALE.get(mint)
        if not m or not p or not k:
            continue
        p *= statistics.median(k)  # same units as the entry price
        if not half and p / entry - 1 <= STOP:
            reason = "stop loss (fast)"
        elif half and p <= peak * (1 - TRAIL):
            reason = "trailing exit (fast)"
        else:
            continue
        m = {**m, "price": min(p, m["price"])}
        recv += value(tokens, m)
        pnl = recv - spent
        db.execute("UPDATE positions SET tokens=0,half_sold=?,received_sol=?,last_value=0,closed_ts=?,reason=?,last_price=? WHERE id=?",
                   (half, recv, int(time.time()), reason, m["price"], pid))
        db.commit()
        log("guard", "trade", f"Sold {sym or mint[:6]}: {reason}")
        log("ledger", "book", f"{sym or mint[:6]} {pnl:+.4f} SOL ({pnl / spent:+.0%})")


def manage(db):
    rows = db.execute("SELECT id,mint,symbol,opened_ts,entry_price,spent_sol,tokens,peak,half_sold,received_sol "
                      "FROM positions WHERE closed_ts IS NULL").fetchall()
    if not rows:
        return
    mk = prices([r[1] for r in rows])
    LAST_MK.update(mk)
    for mint, m in mk.items():
        lp = live_price(mint)
        if lp:
            SCALE.setdefault(mint, []).append(m["price"] / lp)
            del SCALE[mint][:-10]
    now = int(time.time())
    for pid, mint, sym, opened, entry, spent, tokens, peak, half, recv in rows:
        m = mk.get(mint)
        name = sym or mint[:6]
        if not m:
            continue
        peak = max(peak, m["price"])
        change = m["price"] / entry - 1
        reason = None
        # half_sold: 0 = full position, 1 = stake back, 2 = second sale done
        if (half == 0 and change >= TAKE_1) or (half == 1 and change >= TAKE_2):
            m = fresh(mint, m, min)
            change = m["price"] / entry - 1
        if half == 0 and change >= TAKE_1:
            # sell enough to recover the stake, the rest runs for free
            part = tokens * min(1.0, (spent - recv) / max(value(tokens, m), 1e-9) * 1.03)
            tokens, recv, half = tokens - part, recv + value(part, m), 1
            log("guard", "trade", f"Stake back from {name} at {change:+.0%}")
        if half == 1 and change >= TAKE_2:
            part = tokens / 2
            tokens, recv, half = tokens - part, recv + value(part, m), 2
            log("guard", "trade", f"Took profit on {name} at {change:+.0%}")
        if change <= STOP and not half:
            reason = "stop loss"
        elif half and m["price"] <= peak * (1 - TRAIL):
            reason = "trailing exit"
        elif not half and m["buys"] + m["sells"] < 3:
            reason = "attention left"
        elif half and m["buys"] + m["sells"] == 0:
            reason = "attention left"
        elif now - opened >= (RUNNER_HOLD_S if half else MAX_HOLD_S):
            reason = "time exit"
        if reason:
            m = fresh(mint, m, min)
            recv += value(tokens, m)
            pnl = recv - spent
            db.execute("UPDATE positions SET tokens=0,peak=?,half_sold=?,received_sol=?,last_value=0,closed_ts=?,reason=?,last_price=? WHERE id=?",
                       (peak, half, recv, now, reason, m["price"], pid))
            log("guard", "trade", f"Sold {name}: {reason}")
            log("ledger", "book", f"{name} {pnl:+.4f} SOL ({pnl / spent:+.0%})")
        else:
            db.execute("UPDATE positions SET tokens=?,peak=?,half_sold=?,received_sol=?,last_value=?,last_price=? WHERE id=?",
                       (tokens, peak, half, recv, value(tokens, m), m["price"], pid))
    db.commit()


def track_coins(db):
    """Follows every graded coin for 24 hours, to show what was missed and which grades were wrong."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='coins'").fetchone():
        return
    cols = [c[1] for c in db.execute("PRAGMA table_info(coins)")]
    if "peak_mcap" not in cols:
        db.execute("ALTER TABLE coins ADD COLUMN peak_mcap REAL")
        db.execute("ALTER TABLE coins ADD COLUMN last_mcap REAL")
    rows = db.execute("SELECT mint FROM coins WHERE ts>? GROUP BY mint ORDER BY max(ts) DESC LIMIT 120", (time.time() - 86400,)).fetchall()
    mk = prices([r[0] for r in rows]) if rows else {}
    for mint, m in mk.items():
        db.execute("UPDATE coins SET last_mcap=?, peak_mcap=max(coalesce(peak_mcap,0),?) WHERE mint=?", (m["mcap"], m["mcap"], mint))
    db.commit()


def snapshot(db):
    real = db.execute("SELECT coalesce(sum(received_sol-spent_sol),0) FROM positions WHERE closed_ts IS NOT NULL").fetchone()[0]
    opn = db.execute("SELECT coalesce(sum(received_sol+coalesce(last_value,0)-spent_sol),0) FROM positions WHERE closed_ts IS NULL").fetchone()[0]
    db.execute("INSERT OR REPLACE INTO equity VALUES(?,?)", (int(time.time() // 300 * 300), START_SOL + real + opn))
    db.commit()


def main():
    db = sqlite3.connect(DB, timeout=30)
    db.executescript(SCHEMA)
    if "last_price" not in [c[1] for c in db.execute("PRAGMA table_info(positions)")]:
        db.execute("ALTER TABLE positions ADD COLUMN last_price REAL")
    if "rules" not in [c[1] for c in db.execute("PRAGMA table_info(positions)")]:
        db.execute("ALTER TABLE positions ADD COLUMN rules TEXT DEFAULT 'default'")
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='coins'").fetchone():
        have = [c[1] for c in db.execute("PRAGMA table_info(coins)")]
        for col, kind in (("dev_pct", "REAL"), ("smart", "TEXT")):
            if col not in have:
                db.execute(f"ALTER TABLE coins ADD COLUMN {col} {kind}")
    log("ledger", "start", "Books open: 1 SOL starting balance")
    tick = 0
    while True:
        try:
            manage(db)
            enter(db)
            snapshot(db)
            tick += 1
            if tick % 4 == 1:
                track_coins(db)
        except Exception as e:  # noqa: BLE001 — one bad tick must not stop the team
            log("guard", "skip", f"tick error: {type(e).__name__}")
        for _ in range(TICK_S // FAST_S):
            time.sleep(FAST_S)
            try:
                fast_stop(db)
            except Exception as e:  # noqa: BLE001
                log("guard", "skip", f"fast check error: {type(e).__name__}")


if __name__ == "__main__":
    main()

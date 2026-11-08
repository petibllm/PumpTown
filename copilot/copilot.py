"""PumpTown copilot: coin checks, a decision journal, and whether the checks help.

  python copilot.py check <mint> [--x]             report (+X), saved to the journal
  python copilot.py decide <mint> buy|skip [--size 0.1] [--note "..."]
  python copilot.py close <mint> <pnl_sol>         close a manual trade
  python copilot.py digest [--hours 24]            X accounts from watchlist.txt
  python copilot.py review                         outcomes of decisions and flags

Never gives buy or sell advice and never touches a wallet: facts, flags and a journal.
"""
import argparse
import glob
import json
import sqlite3
import time
from pathlib import Path

import coin_report as cr
import x_check

HERE = Path(__file__).resolve().parent
JOURNAL = HERE / "journal.sqlite"
WATCHLIST = HERE / "watchlist.txt"
HORIZON_S = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks(id INTEGER PRIMARY KEY, ts INT, mint TEXT, symbol TEXT, red_flags INT,
  flags TEXT, curve_pct REAL, mcap_sol REAL, coverage REAL, x_json TEXT);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, ts INT, mint TEXT, action TEXT, size_sol REAL,
  note TEXT, pnl_sol REAL, closed_ts INT);
"""


def db():
    c = sqlite3.connect(JOURNAL)
    c.executescript(SCHEMA)
    return c


def cmd_check(a):
    r = cr.build_report(a.mint)
    print(cr.render(r))
    x = None
    if a.x:
        try:
            x = x_check.coin_check(r)
            print(x_check.render(x))
        except x_check.XError as e:
            print(f"X check skipped: {e}")
    f = r["flow"]
    with db() as c:
        c.execute("INSERT INTO checks(ts,mint,symbol,red_flags,flags,curve_pct,mcap_sol,coverage,x_json) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (r["generated_ts"], a.mint, (r.get("coin") or {}).get("symbol"), len(r["red_flags"]),
                   json.dumps(r["red_flags"], ensure_ascii=False), f.get("curve_pct"), f.get("mcap_sol"),
                   r.get("coverage"), json.dumps(x, ensure_ascii=False) if x else None))


def cmd_decide(a):
    with db() as c:
        c.execute("INSERT INTO decisions(ts,mint,action,size_sol,note) VALUES(?,?,?,?,?)",
                  (int(time.time()), a.mint, a.action, a.size, a.note))
    print(f"Logged: {a.action} {a.mint}")


def cmd_close(a):
    with db() as c:
        row = c.execute("SELECT id FROM decisions WHERE mint=? AND action='buy' AND pnl_sol IS NULL "
                        "ORDER BY ts DESC LIMIT 1", (a.mint,)).fetchone()
        if not row:
            print("No open buy for this coin.")
            return
        c.execute("UPDATE decisions SET pnl_sol=?, closed_ts=? WHERE id=?", (a.pnl, int(time.time()), row[0]))
    print(f"Closed {a.mint}: {a.pnl:+.4f} SOL")


def cmd_digest(a):
    if not WATCHLIST.exists():
        print(f"Create {WATCHLIST} with one X handle per line.")
        return
    handles = [h.strip() for h in WATCHLIST.read_text().splitlines() if h.strip() and not h.startswith("#")]
    try:
        print(x_check.render(x_check.digest(handles, a.hours)))
    except x_check.XError as e:
        print(f"Digest skipped: {e}")


def outcome(mint, ts):
    """Coin price for HORIZON_S after the check, relative to the entry price."""
    prices = []
    for f in sorted(glob.glob(str(cr.DATA / "pump_*.sqlite"))):
        d = sqlite3.connect(f"file:{Path(f).as_posix()}?mode=ro", uri=True, timeout=30)
        prices += d.execute("SELECT ts, 1.0*vsr/vtr FROM pump_trades WHERE mint=? AND vtr>0 AND ts>=? AND ts<=? "
                            "AND mayhem=0 AND quote_mint IS NULL", (mint, ts - 600, ts + HORIZON_S)).fetchall()
        d.close()
    prices.sort()
    before = [p for t, p in prices if t <= ts]
    after = [p for t, p in prices if t > ts]
    if not before or not after:
        return None
    p0 = before[-1]
    return {"peak": max(after) / p0 - 1, "trough": min(after) / p0 - 1, "end": after[-1] / p0 - 1}


def cmd_review(a):
    c = db()
    now = int(time.time())
    groups = {"0 red flags": [], "1-2 red flags": [], "3+ red flags": []}
    pending = 0
    for ts, mint, n in c.execute("SELECT min(ts), mint, red_flags FROM checks GROUP BY mint"):
        if now - ts < HORIZON_S:
            pending += 1
            continue
        o = outcome(mint, ts)
        if o:
            groups["0 red flags" if n == 0 else "1-2 red flags" if n <= 2 else "3+ red flags"].append(o)
    print(f"Checked coins, outcome {HORIZON_S // 60} min later (before fees; {pending} still too recent):")
    for k, v in groups.items():
        if v:
            med = sorted(o["end"] for o in v)[len(v) // 2]
            print(f"  {k:14s} n={len(v):3d} median end {med:+.0%}, "
                  f"share that doubled {sum(o['peak'] >= 1 for o in v) / len(v):.0%}, "
                  f"share down 50%+ {sum(o['trough'] <= -0.5 for o in v) / len(v):.0%}")
    rows = c.execute("SELECT action, count(*), count(pnl_sol), coalesce(sum(pnl_sol),0), "
                     "coalesce(sum(pnl_sol>0),0) FROM decisions GROUP BY action").fetchall()
    for action, n, closed, pnl, wins in rows:
        line = f"Decisions {action}: {n}"
        if action == "buy":
            line += f", closed {closed}, total PnL {pnl:+.4f} SOL, wins {wins}"
        print(line)
    print("Small samples prove nothing: treat this as evidence only after 100+ coins.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    q = s.add_parser("check"); q.add_argument("mint"); q.add_argument("--x", action="store_true")
    q.set_defaults(fn=cmd_check)
    q = s.add_parser("decide"); q.add_argument("mint"); q.add_argument("action", choices=["buy", "skip"])
    q.add_argument("--size", type=float); q.add_argument("--note", default=""); q.set_defaults(fn=cmd_decide)
    q = s.add_parser("close"); q.add_argument("mint"); q.add_argument("pnl", type=float); q.set_defaults(fn=cmd_close)
    q = s.add_parser("digest"); q.add_argument("--hours", type=int, default=24); q.set_defaults(fn=cmd_digest)
    q = s.add_parser("review"); q.set_defaults(fn=cmd_review)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()

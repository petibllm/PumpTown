"""Live watch: follows the recorder's stream every 15 seconds and grades coins as they heat up.

  python watcher.py        runs forever (tmux "watcher")

Reads only new rows (by rowid), so it stays instant however large the daily files get. Screening is free. Only coins
that heat up get the deep check, which uses Helius calls, so that part has a daily limit.
"""
import csv
import glob
import json
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import appraise as ap
import coin_report as cr
from executor import prices
from village import DB, log

TICK_S = 15
WINDOW_S = 600
MIN_BUYERS = 25        # different wallets buying within the window
MIN_NET_SOL = 3.0      # more bought than sold within the window
DEEP_GAP_S = 60        # at most one deep check a minute
DEEP_PER_DAY = 200     # keeps Helius inside the free plan


def tracked():
    """Tracked wallets: address -> name. They are only noted on the coin, they never trigger or change a grade."""
    path = Path(__file__).resolve().parent / "wallets.csv"
    if not path.exists():  # your own list, see wallets.example.csv
        return {}
    with open(path, encoding="utf-8", newline="") as f:
        return {r["address"]: r["name"] for r in csv.DictReader(f)}


class Index:
    """Dev history and copycat counts in memory, so a report needs no scan of every daily file."""

    def __init__(self):
        self.by_creator = defaultdict(list)
        self.symbols = defaultdict(int)
        self.uris = defaultdict(int)
        self.grads = set()

    def add_create(self, body):
        b = json.loads(body)
        self.by_creator[b.get("creator")].append({"mint": b.get("mint"), "ts": b.get("timestamp"), "name": b.get("name"),
                                                  "symbol": b.get("symbol")})
        self.symbols[(b.get("symbol") or "").lower()] += 1
        self.uris[b.get("uri")] += 1

    def dev_history(self, creator, mint):
        coins = [c for c in self.by_creator.get(creator, []) if c["mint"] != mint]
        return {"other_coins": len(coins), "graduated": sum(c["mint"] in self.grads for c in coins), "last": coins[-3:]}

    def copycats(self, create, mint):
        return {"same_symbol": max(0, self.symbols[(create.get("symbol") or "").lower()] - 1),
                "same_uri": max(0, self.uris[create.get("uri")] - 1)}


class Tail:
    """New rows from the two newest daily files, remembered by rowid."""

    def __init__(self, index):
        self.pos, self.index = {}, index
        self.flow = defaultdict(deque)
        self.my, self.mine = set(), []

    def read(self, first=False):
        for f in sorted(glob.glob(str(cr.DATA / "pump_*.sqlite")))[-2:]:
            db = sqlite3.connect(f"file:{Path(f).as_posix()}?mode=ro", uri=True, timeout=30)
            t = self.pos.get(f) or {}
            if not t:
                t = {"tr": db.execute("SELECT max(rowid) FROM pump_trades").fetchone()[0] or 0, "ev": 0}
                self.pos[f] = t
            for rid, name, body, mint in db.execute(
                    "SELECT rowid,name,body,mint FROM events_json WHERE rowid>? AND name IN ('CreateEvent','CompleteEvent')", (t["ev"],)):
                t["ev"] = rid
                if name == "CreateEvent":
                    self.index.add_create(body)
                else:
                    self.index.grads.add(mint)
            if not first:
                for rid, mint, user, buy, sol, ts in db.execute(
                        "SELECT rowid,mint,user,is_buy,sol_amount,ts FROM pump_trades WHERE rowid>? AND mayhem=0 AND quote_mint IS NULL",
                        (t["tr"],)):
                    t["tr"] = rid
                    self.flow[mint].append((ts, user, buy, sol or 0))
                    if user in self.my:
                        self.mine.append((ts, user, mint, buy, sol or 0))
            db.close()

    def smart(self, mint, wallets):
        return sorted({wallets[u] for _, u, b, _ in self.flow.get(mint, ()) if b and u in wallets})

    def hot(self):
        cut, out = time.time() - WINDOW_S, []
        for mint in list(self.flow):
            q = self.flow[mint]
            while q and q[0][0] < cut:
                q.popleft()
            if not q:
                del self.flow[mint]
                continue
            buyers = {u for _, u, b, _ in q if b}
            net = sum(s if b else -s for _, _, b, s in q) / cr.LAMPORTS
            if len(buyers) >= MIN_BUYERS and net >= MIN_NET_SOL:
                out.append((len(buyers), mint))
        return [m for _, m in sorted(out, reverse=True)]


def main():
    db = sqlite3.connect(DB, timeout=30)
    db.executescript(ap.SCHEMA)
    db.execute("CREATE TABLE IF NOT EXISTS my_trades(ts INT, wallet TEXT, mint TEXT, is_buy INT, sol INT)")
    index = Index()
    tail = Tail(index)
    wallets = tracked()
    log("appraiser", "working", "Loading coin history")
    for f in sorted(glob.glob(str(cr.DATA / "pump_*.sqlite")))[:-2]:
        d = sqlite3.connect(f"file:{Path(f).as_posix()}?mode=ro", uri=True, timeout=30)
        for name, body, mint in d.execute("SELECT name,body,mint FROM events_json WHERE name IN ('CreateEvent','CompleteEvent')"):
            index.add_create(body) if name == "CreateEvent" else index.grads.add(mint)
        d.close()
    tail.read(first=True)
    cr.dev_history = index.dev_history
    cr.copycats = index.copycats
    log("appraiser", "start", "Live watch started")
    day, deep, last_deep = None, 0, 0.0
    while True:
        try:
            tail.my = {a for a in cr.env_file().get("MY_WALLETS", "").split(",") if a}
            tail.read()
            if tail.mine:
                db.executemany("INSERT INTO my_trades(ts,wallet,mint,is_buy,sol) VALUES(?,?,?,?,?)", tail.mine)
                db.commit()
                tail.mine.clear()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != day:
                day, deep = today, 0
            if deep < DEEP_PER_DAY and time.time() - last_deep >= DEEP_GAP_S:
                recent = {m for (m,) in db.execute("SELECT mint FROM coins WHERE ts>?", (time.time() - ap.REGRADE_AFTER_S,))}
                hot = [m for m in tail.hot() if m not in recent][:10]
                mk = prices(hot) if hot else {}
                alive = [m for m in hot if m in mk and mk[m]["mcap"] >= ap.MIN_CAP_SOL]
                if alive:
                    deep, last_deep = deep + 1, time.time()
                    ap.grade_one(db, alive[0], tail.smart(alive[0], wallets))
        except Exception as e:  # noqa: BLE001 — one bad tick must not stop the watch
            log("appraiser", "skip", f"watch error: {type(e).__name__}")
        time.sleep(TICK_S)


if __name__ == "__main__":
    main()

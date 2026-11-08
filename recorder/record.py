"""PumpTown recorder: stores pump.fun bonding curve and PumpSwap events.

Source: Solana WebSocket `logsSubscribe` (mentions of the two programs), confirmed.
Output: data/pump_YYYYMMDD.sqlite (one file per UTC day) with a heartbeat every minute.
Read-only: no funds, no keys, no transactions.

Run:  PUMPTOWN_WSS=wss://a,wss://b python record.py
"""
import asyncio
import collections
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import websockets

from idl import IdlEvents

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("PUMPTOWN_DATA", ROOT / "data"))
# Several comma-separated endpoints: subscribe to all and deduplicate by signature.
ENDPOINTS = [u.strip() for u in os.environ.get(
    "PUMPTOWN_WSS", "wss://solana-rpc.publicnode.com,wss://api.mainnet-beta.solana.com").split(",")
    if u.strip()]
# Standby endpoints connect only while the primary endpoint is silent, which saves metered credits.
STANDBY_FROM = len(ENDPOINTS)
ENDPOINTS += [u.strip() for u in os.environ.get("PUMPTOWN_WSS_STANDBY", "").split(",") if u.strip()]
STANDBY_MIN_S = 300  # minimum run time once switched on

PUMP = IdlEvents(str(ROOT / "idl" / "pump.json"), "pump")
AMM = IdlEvents(str(ROOT / "idl" / "pump_amm.json"), "amm")
PROGRAMS = {PUMP.address: PUMP, AMM.address: AMM}
WSOL = "So11111111111111111111111111111111111111112"
DEFAULT_PK = "11111111111111111111111111111111"

log = logging.getLogger("recorder")

RE_INVOKE = re.compile(r"^Program (\w+) invoke \[\d+\]$")
RE_EXIT = re.compile(r"^Program (\w+) (success|failed)")
DATA_PREFIX = "Program data: "

SCHEMA = """
CREATE TABLE IF NOT EXISTS pump_trades(
  slot INT, sig TEXT, ev_idx INT, recv_ms INT, ts INT, mint TEXT, user TEXT,
  is_buy INT, sol_amount INT, token_amount INT, vsr INT, vtr INT, rsr INT, rtr INT,
  fee INT, fee_bps INT, creator_fee INT, creator_fee_bps INT,
  quote_mint TEXT, quote_amount INT, vqr TEXT, ix_name TEXT, mayhem INT, partial INT, extra_hex TEXT,
  PRIMARY KEY(sig, ev_idx));
CREATE TABLE IF NOT EXISTS amm_trades(
  slot INT, sig TEXT, ev_idx INT, recv_ms INT, ts INT, pool TEXT, user TEXT,
  is_buy INT, base_amount INT, quote_amount INT, user_quote_amount INT,
  pool_base INT, pool_quote INT, lp_fee INT, protocol_fee INT, creator_fee INT,
  vqr TEXT, ix_name TEXT, partial INT, extra_hex TEXT,
  PRIMARY KEY(sig, ev_idx));
CREATE TABLE IF NOT EXISTS events_json(
  slot INT, sig TEXT, ev_idx INT, recv_ms INT, program TEXT, name TEXT, mint TEXT, body TEXT,
  PRIMARY KEY(sig, ev_idx));
CREATE TABLE IF NOT EXISTS heartbeat(
  minute INT PRIMARY KEY, connected_s INT, notifs INT, events INT, failed_tx INT,
  truncated INT, unknown_data INT, partial INT, extra INT, reconnects INT, last_slot INT);
CREATE TABLE IF NOT EXISTS endpoint_stats(
  minute INT, endpoint INT, host TEXT, notifs INT, first INT, connected_s INT,
  PRIMARY KEY(minute, endpoint));
CREATE INDEX IF NOT EXISTS ix_pt_mint ON pump_trades(mint, slot);
CREATE INDEX IF NOT EXISTS ix_at_pool ON amm_trades(pool, slot);
CREATE INDEX IF NOT EXISTS ix_ej_name ON events_json(name);
"""


def _nz(x):
    return None if x in (None, DEFAULT_PK, WSOL) else x


class Store:
    def __init__(self):
        DATA.mkdir(parents=True, exist_ok=True)
        self.day = None
        self.db = None
        self.rows = collections.defaultdict(list)

    def _open(self):
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        if day != self.day:
            if self.db:
                self.db.close()
            self.db = sqlite3.connect(DATA / f"pump_{day}.sqlite")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            self.day = day
        return self.db

    def add(self, table, row):
        self.rows[table].append(row)

    def flush(self):
        if not self.rows:
            return
        db = self._open()
        for table, rows in self.rows.items():
            ph = ",".join("?" * len(rows[0]))
            db.executemany(f"INSERT OR IGNORE INTO {table} VALUES({ph})", rows)
        db.commit()
        self.rows.clear()


class Stats:
    KEYS = ("notifs", "events", "failed_tx", "truncated", "unknown_data", "partial", "extra", "reconnects")

    def __init__(self):
        self.connected = set()
        self.reset()
        self.last_slot = 0

    def reset(self):
        self.c = dict.fromkeys(self.KEYS, 0)
        self.connected_s = 0.0
        self.minute = int(time.time() // 60)
        # per endpoint: all notifications, how many arrived first, connected seconds
        self.ep = {i: [0, 0, 0] for i in range(len(ENDPOINTS))}


def handle_notification(value, slot, store: Store, stats: Stats, recv_ms: int):
    sig = value["signature"]
    logs = value.get("logs") or []
    if value.get("err") is not None:
        stats.c["failed_tx"] += 1
        return
    stack, ev_idx = [], 0
    for line in logs:
        m = RE_INVOKE.match(line)
        if m:
            stack.append(m.group(1))
            continue
        m = RE_EXIT.match(line)
        if m:
            if stack:
                stack.pop()
            continue
        if line.startswith("Log truncated"):
            stats.c["truncated"] += 1
            continue
        if not line.startswith(DATA_PREFIX) or not stack:
            continue
        prog = PROGRAMS.get(stack[-1])
        if prog is None:
            continue
        res = prog.decode_b64(line[len(DATA_PREFIX):])
        if res is None:
            stats.c["unknown_data"] += 1
            continue
        name, f = res
        partial = int(bool(f.get("_missing")))
        stats.c["partial"] += partial
        stats.c["extra"] += int(bool(f.get("_extra_bytes")))
        stats.c["events"] += 1
        if prog is PUMP and name == "TradeEvent":
            store.add("pump_trades", (
                slot, sig, ev_idx, recv_ms, f.get("timestamp"), f.get("mint"), f.get("user"),
                int(f.get("is_buy", 0)), f.get("sol_amount"), f.get("token_amount"),
                f.get("virtual_sol_reserves"), f.get("virtual_token_reserves"),
                f.get("real_sol_reserves"), f.get("real_token_reserves"),
                f.get("fee"), f.get("fee_basis_points"), f.get("creator_fee"),
                f.get("creator_fee_basis_points"), _nz(f.get("quote_mint")),
                f.get("quote_amount"), str(f.get("virtual_quote_reserves")),
                f.get("ix_name"), int(bool(f.get("mayhem_mode"))), partial, f.get("_extra_hex")))
        elif prog is AMM and name in ("BuyEvent", "SellEvent"):
            buy = name == "BuyEvent"
            store.add("amm_trades", (
                slot, sig, ev_idx, recv_ms, f.get("timestamp"), f.get("pool"), f.get("user"),
                int(buy), f.get("base_amount_out" if buy else "base_amount_in"),
                f.get("quote_amount_in" if buy else "quote_amount_out"),
                f.get("user_quote_amount_in" if buy else "user_quote_amount_out"),
                f.get("pool_base_token_reserves"), f.get("pool_quote_token_reserves"),
                f.get("lp_fee"), f.get("protocol_fee"), f.get("coin_creator_fee"),
                str(f.get("virtual_quote_reserves")), f.get("ix_name"), partial, f.get("_extra_hex")))
        else:
            mint = f.get("mint") or f.get("base_mint")
            store.add("events_json", (slot, sig, ev_idx, recv_ms, prog.label, name, mint,
                                      json.dumps(f, separators=(",", ":"))))
        ev_idx += 1


async def heartbeat_loop(store: Store, stats: Stats):
    last = time.time()
    while True:
        await asyncio.sleep(1)
        now = time.time()
        # wall-clock time, not a loop count: a large flush can make one round longer than 1 s
        dt, last = now - last, now
        if stats.connected:
            stats.connected_s += dt
        for i in stats.connected:
            stats.ep[i][2] += dt
        store.flush()
        minute = int(now // 60)
        if minute != stats.minute:
            c = stats.c
            store.add("heartbeat", (stats.minute * 60, min(60, int(stats.connected_s)), c["notifs"],
                                    c["events"], c["failed_tx"], c["truncated"], c["unknown_data"],
                                    c["partial"], c["extra"], c["reconnects"], stats.last_slot))
            for i, (n, first, cs) in stats.ep.items():
                store.add("endpoint_stats", (stats.minute * 60, i, _host(ENDPOINTS[i]), n, first, min(60, int(cs))))
            store.flush()
            log.info("minute: %s | endpoints (all/first): %s", c,
                     {i: (v[0], v[1]) for i, v in stats.ep.items()})
            stats.reset()


def _host(url: str) -> str:
    """Only the host is logged, never an API key in the query or path."""
    return url.split("//", 1)[-1].split("/", 1)[0].split("?", 1)[0]


SEEN = collections.OrderedDict()  # shared by all endpoints
SILENCE_S = 30
LAST_RX = {}  # endpoint -> time of last notification


def primary_ok() -> bool:
    return time.time() - LAST_RX.get(0, 0) < SILENCE_S


class StandbyDone(Exception):
    pass


async def stream(store: Store, stats: Stats, idx: int, url: str):
    backoff = 1
    seen = SEEN
    standby = idx >= STANDBY_FROM
    while True:
        if standby:
            while primary_ok():
                await asyncio.sleep(5)
            log.info("[%d] primary endpoint silent, standby on", idx)
        started = time.time()
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          max_size=2 ** 24, open_timeout=15) as ws:
                for i, addr in enumerate(PROGRAMS):
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": i + 1, "method": "logsSubscribe",
                        "params": [{"mentions": [addr]}, {"commitment": "confirmed"}]}))
                log.info("connected [%d]: %s", idx, _host(url))
                stats.connected.add(idx)
                while True:
                    # a connection can stay open while the subscription sends nothing.
                    # pump.fun always has traffic, so 30 s of silence means reconnect.
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=SILENCE_S)
                    except asyncio.TimeoutError:
                        raise ConnectionError(f"no notification for {SILENCE_S}s")
                    msg = json.loads(raw)
                    if msg.get("method") != "logsNotification":
                        if "error" in msg:
                            log.error("RPC hiba: %s", msg["error"])
                        continue
                    backoff = 1  # reset only after real data, so a silent endpoint is not retried every 30 s
                    res = msg["params"]["result"]
                    value, slot = res["value"], res["context"]["slot"]
                    sig = value["signature"]
                    stats.ep[idx][0] += 1
                    LAST_RX[idx] = time.time()
                    if standby and primary_ok() and time.time() - started > STANDBY_MIN_S:
                        raise StandbyDone
                    if sig in seen:  # a transaction mentioning both programs arrives twice
                        continue
                    seen[sig] = None
                    if len(seen) > 50_000:
                        seen.popitem(last=False)
                    stats.ep[idx][1] += 1
                    stats.c["notifs"] += 1
                    stats.last_slot = max(stats.last_slot, slot)
                    handle_notification(value, slot, store, stats, int(time.time() * 1000))
        except StandbyDone:
            log.info("[%d] primary endpoint back, standby off", idx)
            stats.connected.discard(idx)
            backoff = 1
            continue
        except Exception as e:  # noqa: BLE001 - reconnect after any error
            log.warning("[%d] connection lost: %r, retrying in %ss", idx, e, backoff)
        stats.connected.discard(idx)
        stats.c["reconnects"] += 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def main():
    store, stats = Store(), Stats()
    await asyncio.gather(heartbeat_loop(store, stats),
                         *(stream(store, stats, i, u) for i, u in enumerate(ENDPOINTS)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())

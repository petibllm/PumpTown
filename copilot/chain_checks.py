"""Two deeper checks on chain data, on request, with a few dozen RPC calls.

chart_check:  samples recent transactions: fees paid relative to volume,
              and how many traders make the volume. High volume, low fees and few traders = suspicious chart.
              Jito tips are not visible, only the transaction fee (meta.fee).
funder_check: where the top holders got their first SOL, and how much SOL they hold now.
              A shared funder within a short time = probably one person. An exchange wallet can also be
              a shared source, so the time window is part of the test.
"""
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "recorder"))
from idl import IdlEvents  # noqa: E402

from coin_report import LAMPORTS, _rpc  # noqa: E402

DECODERS = (IdlEvents(str(ROOT / "idl" / "pump.json"), "pump"), IdlEvents(str(ROOT / "idl" / "pump_amm.json"), "amm"))
SAMPLE = 25
FUND_WINDOW_S = 3600
LOW_SOL = 0.02
PAUSE_S = 0.11  # free RPC plans allow about 10 requests a second


def tx(sig):
    time.sleep(PAUSE_S)
    return _rpc("getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1,
                                         "commitment": "confirmed"}])


def sigs(addr, limit, before=None):
    time.sleep(PAUSE_S)
    opts = {"limit": limit, "commitment": "confirmed"}
    if before:
        opts["before"] = before
    return _rpc("getSignaturesForAddress", [addr, opts]) or []


def traded_sol(logs):
    """SOL volume of a transaction, from its program events."""
    total = 0
    for line in logs or []:
        if not line.startswith("Program data: "):
            continue
        for d in DECODERS:
            r = d.decode_b64(line[14:])
            if not r:
                continue
            name, f = r
            if name == "TradeEvent":
                total += f.get("sol_amount") or 0
            elif name == "BuyEvent":
                total += f.get("quote_amount_in") or 0
            elif name == "SellEvent":
                total += f.get("quote_amount_out") or 0
    return total


def chart_check(mint):
    ok = [s["signature"] for s in sigs(mint, 300) if not s.get("err")]
    if len(ok) < 8:
        return {"sampled": len(ok), "enough": False}
    step = max(1, len(ok) // SAMPLE)
    fees, vol, signers, trades = [], 0, Counter(), 0
    for sig in ok[::step][:SAMPLE]:
        t = tx(sig)
        if not t:
            continue
        v = traded_sol(t["meta"].get("logMessages"))
        if not v:
            continue
        trades += 1
        fees.append(t["meta"]["fee"])
        vol += v
        signers[t["transaction"]["message"]["accountKeys"][0]] += 1
    if trades < 8:
        return {"sampled": trades, "enough": False}
    top3 = sum(n for _, n in signers.most_common(3)) / trades
    return {"sampled": trades, "enough": True, "fee_per_sol": sum(fees) / vol, "median_fee_sol": statistics.median(fees) / LAMPORTS,
            "traders": len(signers), "top3_share": top3, "avg_trade_sol": vol / trades / LAMPORTS}


def first_funding(wallet):
    """(funder, time) from the wallet's oldest visible transaction. None if it has too many transactions."""
    page = sigs(wallet, 1000)
    if not page or len(page) >= 1000:
        return None
    first = page[-1]
    t = tx(first["signature"])
    if not t:
        return None
    keys = t["transaction"]["message"]["accountKeys"]
    pre, post = t["meta"]["preBalances"], t["meta"]["postBalances"]
    if wallet not in keys:
        return None
    i = keys.index(wallet)
    if post[i] <= pre[i]:
        return ("self", first.get("blockTime"))  # it paid its own fee, so this is not the funding transaction
    n = min(len(keys), len(pre), len(post))
    src = min(range(n), key=lambda k: post[k] - pre[k])
    return (keys[src], first.get("blockTime"))


def funder_check(wallets):
    wallets = [w for w in wallets if w][:10]
    if not wallets:
        return None
    time.sleep(PAUSE_S)
    acc = _rpc("getMultipleAccounts", [wallets, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])
    low = sum(1 for a in (acc or {}).get("value", []) if (a or {}).get("lamports", 0) / LAMPORTS < LOW_SOL)
    groups, traced = defaultdict(list), 0
    for w in wallets:
        f = first_funding(w)
        if f and f[0] != "self":
            traced += 1
            groups[f[0]].append(f[1] or 0)
    best, best_n = None, 0
    for src, times in groups.items():
        times.sort()
        for t0 in times:
            n = sum(1 for t in times if t0 <= t <= t0 + FUND_WINDOW_S)
            if n > best_n:
                best, best_n = src, n
    return {"checked": len(wallets), "traced": traced, "low_sol_wallets": low, "same_funder": best_n, "funder": best}

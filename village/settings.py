"""Settings behind the setup page: .env, tracked wallets, X watchlist, connection tests.

Secrets are write-only for the browser: the page only learns whether a secret is set.
"""
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENV = ROOT / ".env"
TRACKED = HERE / "wallets.csv"
WATCHLIST = ROOT / "copilot" / "watchlist.txt"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def f(key, section, en, hu, secret=False, help_en="", help_hu="", hint="", kind="text", show=None):
    return {"key": key, "section": section, "en": en, "hu": hu, "secret": secret, "help_en": help_en, "help_hu": help_hu,
            "hint": hint, "kind": kind, "show": show}


FIELDS = [
    f("LLM_PROVIDER", "ai", "AI provider", "AI szolgáltató", kind="provider"),
    f("ANTHROPIC_API_KEY", "ai", "Anthropic API key", "Anthropic API-kulcs", True, show="anthropic"),
    f("OPENAI_API_KEY", "ai", "OpenAI API key", "OpenAI API-kulcs", True, show="openai"),
    f("XAI_API_KEY", "ai", "xAI (Grok) API key", "xAI (Grok) API-kulcs", True, show="xai"),
    f("GEMINI_API_KEY", "ai", "Google Gemini API key", "Google Gemini API-kulcs", True, show="gemini"),
    f("OPENROUTER_API_KEY", "ai", "OpenRouter API key", "OpenRouter API-kulcs", True, show="openrouter"),
    f("GROQ_API_KEY", "ai", "Groq API key", "Groq API-kulcs", True, show="groq"),
    f("MISTRAL_API_KEY", "ai", "Mistral API key", "Mistral API-kulcs", True, show="mistral"),
    f("DEEPSEEK_API_KEY", "ai", "DeepSeek API key", "DeepSeek API-kulcs", True, show="deepseek"),
    f("LLM_BASE_URL", "ai", "Base URL", "Alap URL", hint="https://your-server/v1", show="custom",
      help_en="Any OpenAI-compatible API.", help_hu="Bármilyen OpenAI-kompatibilis API."),
    f("LLM_API_KEY", "ai", "API key", "API-kulcs", True, show="custom"),
    f("LLM_MODEL", "ai", "Model", "Modell", help_en="Leave empty for the provider's default.", help_hu="Üresen hagyva a szolgáltató alapértelmezett modellje."),
    f("VILLAGE_DAILY_USD", "ai", "Daily AI budget (USD)", "Napi AI-keret (USD)", kind="number", hint="1.5"),
    f("VILLAGE_RUN_HOUR_UTC", "ai", "Daily agent run (UTC hour)", "Napi ügynök-futás (UTC óra)", kind="number", hint="14"),

    f("X_BEARER_TOKEN", "x", "X API bearer token", "X API bearer token", True,
      "From developer.x.com. Not needed if you set an xAI key.", "A developer.x.com oldalról. xAI-kulcs mellett nem kell."),
    f("X_SEARCH_QUERY", "x", "X search query", "X keresés", hint="(memecoin OR solana) lang:en -is:retweet"),
    f("X_DAILY_USD", "x", "Daily coin check budget (USD)", "Napi coin-ellenőrzési keret (USD)", kind="number", hint="1.0"),

    f("PUMPTOWN_RPC", "solana", "Solana RPC URL", "Solana RPC URL", True,
      "Optional. Gives exact top holders, otherwise developer holding is estimated. A free Helius key works.", "Opcionális. Pontos top tartókat ad, nélküle a fejlesztő részesedése becsült. Egy ingyenes Helius-kulcs is jó."),
    f("PUMPTOWN_WSS", "solana", "WebSocket endpoints", "WebSocket végpontok", hint="wss://...,wss://...",
      help_en="Comma separated. Empty uses free public nodes. Applies after a restart.", help_hu="Vesszővel elválasztva. Üresen ingyenes nyilvános végpontok. Újraindítás után él."),
    f("RECORDER_KEEP_DAYS", "solana", "Keep recorded data (days)", "Rögzített adat megőrzése (nap)", kind="number", hint="7",
      help_en="The recorder writes about 4 GB a day.", help_hu="A recorder naponta kb. 4 GB-ot ír."),

    f("MY_WALLETS", "wallet", "Your wallet addresses", "A te wallet-címeid", hint="Public addresses, comma separated",
      help_en="Your pump.fun account is your wallet. Public addresses only: this app never asks for a private key or seed phrase.",
      help_hu="A pump.fun fiókod maga a walleted. Csak nyilvános cím kell: az app sosem kér privát kulcsot vagy seed phrase-t."),

    f("VPS_HOST", "server", "Server address", "Szerver címe", hint="203.0.113.10"),
    f("VPS_USER", "server", "SSH user", "SSH felhasználó", hint="root"),
    f("VPS_PORT", "server", "SSH port", "SSH port", kind="number", hint="22"),
    f("VPS_KEY_PATH", "server", "SSH key file", "SSH-kulcs fájl", hint="~/.ssh/id_ed25519",
      help_en="The path to your key on this computer. The key itself is not copied.", help_hu="A kulcs elérési útja ezen a gépen. Magát a kulcsot nem másoljuk."),
    f("VPS_DIR", "server", "Folder on the server", "Mappa a szerveren", hint="PumpTown"),

    f("OWNER_NAME", "general", "Your name in the chat", "A neved a chatben"),
]
BY_KEY = {x["key"]: x for x in FIELDS}
EXTRA_KEYS = {"SETUP_DONE"}


class Invalid(Exception):
    pass


def read_env():
    out = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def b58len(s):
    n = 0
    for c in s:
        if c not in B58:
            return -1
        n = n * 58 + B58.index(c)
    return (n.bit_length() + 7) // 8 + len(s) - len(s.lstrip("1"))


def wallet_list(text):
    """Valid public addresses from text. A private key is rejected, not stored."""
    out = []
    for a in re.split(r"[,\s]+", text.strip()):
        if not a:
            continue
        size = b58len(a)
        if size >= 64 or a.startswith("["):
            raise Invalid("That looks like a private key. Do not paste it here. Only the public address is needed.")
        if size != 32:
            raise Invalid(f"Not a valid Solana address: {a[:12]}...")
        out.append(a)
    return out


def check(key, value):
    if re.search(r"[\r\n'\x00]", value):
        raise Invalid(f"{key}: line breaks and single quotes are not allowed.")
    if not value:
        return value
    kind = BY_KEY[key]["kind"] if key in BY_KEY else "text"
    if kind == "number" and not re.fullmatch(r"\d+(\.\d+)?", value):
        raise Invalid(f"{key} must be a number.")
    if key == "MY_WALLETS":
        return ",".join(wallet_list(value))
    if key == "VPS_HOST" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]*", value):
        raise Invalid("Server address can only contain letters, numbers, dots and dashes.")
    if key == "VPS_USER" and not re.fullmatch(r"[a-z_][a-z0-9_\-]*", value):
        raise Invalid("Invalid SSH user.")
    if key == "VPS_DIR" and not re.fullmatch(r"[\w./\-]+", value):
        raise Invalid("Invalid server folder.")
    if key == "LLM_BASE_URL" and not value.startswith(("http://", "https://")):
        raise Invalid("Base URL must start with http:// or https://")
    return value


def write_env(changes):
    """Change or add keys, keeping every other line. An empty value removes the key."""
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    left = dict(changes)
    out = []
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if k in left:
            v = left.pop(k)
            if v:
                out.append(f"{k}={quote(v)}")
        else:
            out.append(line)
    out += [f"{k}={quote(v)}" for k, v in left.items() if v]
    tmp = ENV.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV)


def quote(v):
    return v if re.fullmatch(r"[\w.,:/\-+=@~]*", v) else f"'{v}'"


def public():
    """What the page may see: values of normal fields, and only set/not set for secrets."""
    env = read_env()
    fields = []
    for x in FIELDS:
        value = env.get(x["key"], "")
        fields.append({**x, "value": "" if x["secret"] else value, "set": bool(value)})
    return {"fields": fields, "done": env.get("SETUP_DONE") == "1", "tracked": _read(TRACKED), "watchlist": _read(WATCHLIST)}


def _read(p):
    return p.read_text(encoding="utf-8") if p.exists() else ""


def save(values, clear=(), tracked=None, watchlist=None, finish=False):
    changes = {}
    for k, v in (values or {}).items():
        if k not in BY_KEY:
            continue
        v = str(v).strip()
        if BY_KEY[k]["secret"] and not v:  # an empty secret field means keep the current value
            continue
        changes[k] = check(k, v)
    for k in clear or ():
        if k in BY_KEY and BY_KEY[k]["secret"]:
            changes[k] = ""
    if tracked is not None:
        write_tracked(tracked)
    if watchlist is not None:
        names = [h.strip().lstrip("@") for h in re.split(r"[,\s]+", watchlist) if h.strip()]
        if any(not re.fullmatch(r"\w{1,15}", h) for h in names):
            raise Invalid("The watchlist can only contain X usernames.")
        WATCHLIST.write_text("\n".join(names) + ("\n" if names else ""), encoding="utf-8")
    if finish:
        changes["SETUP_DONE"] = "1"
    if changes:
        write_env(changes)


def write_tracked(text):
    rows = ["address,name,source"]
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("address,"):
            continue
        parts = [p.strip() for p in line.split(",")]
        addr = wallet_list(parts[0])
        if addr:
            name = re.sub(r"[,\r\n]", " ", parts[1]) if len(parts) > 1 and parts[1] else addr[0][:6]
            rows.append(f"{addr[0]},{name},{parts[2] if len(parts) > 2 else 'manual'}")
    TRACKED.write_text("\n".join(rows) + "\n", encoding="utf-8")


def rpc(method, params):
    env = read_env()
    last = "no endpoint"
    for url in [u for u in (env.get("PUMPTOWN_RPC"), PUBLIC_RPC) if u]:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        try:
            req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "pumptown"})
            with urllib.request.urlopen(req, timeout=10) as r:
                out = json.load(r)
            if "result" in out:
                return out["result"]
            last = "the endpoint returned an error"
        except Exception:  # noqa: BLE001
            last = "cannot reach the endpoint"
    raise Invalid(last)


def ssh_base(env=None):
    env = read_env() if env is None else env
    if not env.get("VPS_HOST"):
        raise Invalid("Set the server address first.")
    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", "-p", env.get("VPS_PORT") or "22"]
    if env.get("VPS_KEY_PATH"):
        cmd += ["-i", os.path.expanduser(env["VPS_KEY_PATH"])]
    return cmd, f"{env.get('VPS_USER') or 'root'}@{env['VPS_HOST']}"


def test_server():
    cmd, target = ssh_base()
    try:
        r = subprocess.run([*cmd, target, "echo connected"], capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Invalid(f"SSH failed: {type(e).__name__}") from e
    if r.returncode or "connected" not in r.stdout:
        raise Invalid((r.stderr.strip() or "SSH failed")[:200])
    return "connected"


def my_wallets(db):
    addrs = [a for a in read_env().get("MY_WALLETS", "").split(",") if a]
    out = []
    for a in addrs:
        try:
            sol = round(rpc("getBalance", [a])["value"] / 1e9, 4)
        except Invalid:
            sol = None
        out.append({"address": a, "sol": sol, "profile": f"https://pump.fun/profile/{a}"})
    trades = []
    if addrs and db.execute("SELECT 1 FROM sqlite_master WHERE name='my_trades'").fetchone():
        trades = [{"ts": t, "wallet": w, "mint": m, "buy": bool(b), "sol": round(s / 1e9, 4)} for t, w, m, b, s in db.execute(
            "SELECT ts,wallet,mint,is_buy,sol FROM my_trades ORDER BY ts DESC LIMIT 50")]
    return {"wallets": out, "trades": trades}

"""Small MCP client: connects to servers from mcp_servers.json over stdio or HTTP.

Only the Chief chat uses these tools. Tools marked read-only are allowed; other tools need allow_write on the server.
Tool results are untrusted data.
"""
import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "mcp_servers.json"
PROTOCOL = "2025-06-18"
TIMEOUT_S = 30
CACHE_S = 300
MASK = "********"
_cache = {"ts": 0.0, "tools": []}


class McpError(Exception):
    pass


def load():
    try:
        servers = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return servers if isinstance(servers, list) else []


def public():
    """What the settings page may see: header names, never header values."""
    return [{**s, "headers": {k: MASK for k in s.get("headers") or {}}} for s in load()]


def save(servers):
    clean = []
    old = {s["name"]: s.get("headers") or {} for s in load()}
    for s in servers:
        name, url, command = str(s.get("name", "")).strip(), str(s.get("url", "")).strip(), str(s.get("command", "")).strip()
        if not name or not (url or command):
            raise McpError("Each server needs a name and a URL or a command.")
        if not name.replace("-", "").replace("_", "").isalnum():
            raise McpError(f"Server name '{name}' can only use letters, numbers, - and _.")
        if url and not url.startswith(("http://", "https://")):
            raise McpError(f"Server '{name}' has an invalid URL.")
        headers = s.get("headers") if isinstance(s.get("headers"), dict) else {}
        headers = {str(k): old.get(name, {}).get(k, "") if v == MASK else str(v) for k, v in headers.items()}
        clean.append({"name": name, "url": url, "command": command, "headers": headers,
                      "enabled": bool(s.get("enabled", True)), "allow_write": bool(s.get("allow_write", False))})
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)
    _cache["ts"] = 0.0


class Stdio:
    def __init__(self, server):
        try:
            argv = [a.strip('"') for a in shlex.split(server["command"], posix=os.name != "nt")]
            argv[0] = shutil.which(argv[0]) or argv[0]
            self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        except (OSError, ValueError) as e:
            raise McpError(f"cannot start {server['name']}: {e}") from e
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                pass
        self.q.put(None)

    def notify(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def request(self, msg):
        self.notify(msg)
        end = time.time() + TIMEOUT_S
        while True:
            try:
                r = self.q.get(timeout=max(.1, end - time.time()))
            except queue.Empty as e:
                raise McpError("the server did not answer in time") from e
            if r is None:
                raise McpError("the server closed the connection")
            if r.get("id") == msg["id"]:
                return r

    def close(self):
        self.p.terminate()


class Http:
    def __init__(self, server):
        self.url, self.headers, self.session = server["url"], server.get("headers") or {}, None

    def _send(self, msg):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": PROTOCOL, "User-Agent": "pumptown", **self.headers}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, json.dumps(msg).encode(), h)
        try:
            r = urllib.request.urlopen(req, timeout=TIMEOUT_S)
        except urllib.error.HTTPError as e:
            raise McpError(f"HTTP {e.code} from the server") from e
        except urllib.error.URLError as e:
            raise McpError(f"cannot reach the server: {e.reason}") from e
        self.session = r.headers.get("Mcp-Session-Id") or self.session
        return r

    def notify(self, msg):
        self._send(msg).close()

    def request(self, msg):
        with self._send(msg) as r:
            body = r.read().decode("utf-8", "replace")
            if "text/event-stream" not in (r.headers.get("Content-Type") or ""):
                return json.loads(body)
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    d = json.loads(line[5:])
                except ValueError:
                    continue
                if d.get("id") == msg["id"]:
                    return d
        raise McpError("no answer in the event stream")

    def close(self):
        pass


class Session:
    def __enter__(self):
        return self

    def __init__(self, server):
        self.t = Stdio(server) if server.get("command") else Http(server)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "pumptown", "version": "1"}})
        self.t.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def rpc(self, method, params=None):
        self.n += 1
        r = self.t.request({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        if r.get("error"):
            raise McpError(str(r["error"].get("message", "server error"))[:200])
        return r.get("result") or {}

    def __exit__(self, *a):
        self.t.close()


def list_tools(server):
    with Session(server) as s:
        return s.rpc("tools/list").get("tools", [])


def allowed(server, tool):
    return server.get("allow_write") or (tool.get("annotations") or {}).get("readOnlyHint") is True


def available():
    """Tools the Chief may use: [{server, name, description, schema}]. Servers that fail are left out."""
    if time.time() - _cache["ts"] < CACHE_S:
        return _cache["tools"]
    out = []
    for server in load():
        if not server.get("enabled"):
            continue
        try:
            for t in list_tools(server):
                if allowed(server, t):
                    out.append({"server": server["name"], "name": t["name"], "description": (t.get("description") or "")[:300],
                                "schema": t.get("inputSchema") or {}})
        except (McpError, OSError, ValueError, KeyError):
            continue
    _cache.update(ts=time.time(), tools=out)
    return out


def call(server_name, tool, arguments):
    server = next((s for s in load() if s["name"] == server_name and s.get("enabled")), None)
    if not server:
        raise McpError(f"no enabled server named {server_name}")
    with Session(server) as s:
        info = next((t for t in s.rpc("tools/list").get("tools", []) if t["name"] == tool), None)
        if not info or not allowed(server, info):
            raise McpError(f"tool {tool} is not allowed")
        r = s.rpc("tools/call", {"name": tool, "arguments": arguments or {}})
    text = "\n".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text")
    return text[:6000]

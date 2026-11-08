"""AI providers, settings and MCP against local fakes. No real key and no real API call."""
import http.server
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
seen = []


class Fake(http.server.BaseHTTPRequestHandler):
    def _send(self, d):
        b = json.dumps(d).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        seen.append(("x-api", self.path, self.headers.get("Authorization")))
        self._send({"data": [{"id": "9", "author_id": "1", "text": "real post", "created_at": "2026-11-08T00:00:00Z", "public_metrics": {"like_count": 3}}],
                    "includes": {"users": [{"id": "1", "username": "alice"}]}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append((self.path, body, self.headers.get("Authorization") or self.headers.get("x-api-key")))
        if "chat/completions" in self.path:
            self._send({"choices": [{"message": {"content": '{"who": "chat"}'}}]})
        elif "messages" in self.path:
            self._send({"id": "m", "type": "message", "role": "assistant", "model": body["model"], "stop_reason": "end_turn", "stop_sequence": None,
                        "content": [{"type": "text", "text": '{"who": "claude"}'}], "usage": {"input_tokens": 100, "output_tokens": 10}})
        else:
            self._send({"output": [{"content": [{"type": "output_text", "text": '{"who": "responses"}'}]}], "usage": {}})

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
os.environ.update(XAI_BASE=base, OPENAI_BASE=base, GROQ_BASE=base, ANTHROPIC_BASE_URL=base, X_API_BASE=base + "/2")
sys.path[:0] = [str(ROOT / "village"), str(ROOT / "copilot")]
import server  # noqa: E402
import village  # noqa: E402

village.BUDGET = Path(tempfile.mkdtemp()) / "b.json"
ALL = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "X_BEARER_TOKEN", "LLM_PROVIDER", "GROQ_API_KEY", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
X = {"type": "x_search", "from_date": "2026-11-07"}


def setenv(**kw):
    for k in ALL:
        os.environ.pop(k, None)
    os.environ.update(kw)
    seen.clear()


def expect_stop(tool, frag):
    try:
        village.ask("p", tool)
    except village.Stop as e:
        assert frag in str(e), e
        return
    raise AssertionError("no Stop")


setenv(XAI_API_KEY="k-x")
r = village.ask("hello")
assert r["data"] == {"who": "responses"} and seen[0][1]["model"] == "grok-4.3" and seen[0][2] == "Bearer k-x", seen
village.ask("hello", X)
assert seen[1][1]["tools"] == [X] and server.chief_name() == "Grok (xAI)", seen
print("OK xAI only: text and x_search, same payload as before")

setenv(OPENAI_API_KEY="k-o")
r = village.ask("hello", system="be short")
assert seen[0][1]["model"] == "gpt-5" and "be short" in seen[0][1]["input"][0]["content"] and seen[0][2] == "Bearer k-o" and server.chief_name() == "ChatGPT (OpenAI)", seen
expect_stop(X, "XAI_API_KEY or X_BEARER_TOKEN")
print("OK OpenAI only: text works, X search refuses clearly")

setenv(ANTHROPIC_API_KEY="k-a")
r = village.ask("hello", system="sys")
assert r["data"] == {"who": "claude"} and seen[0][1]["system"] == "sys" and seen[0][1]["model"] == "claude-opus-5-5" and "k-a" in seen[0][2], seen
assert server.chief_name() == "Claude (Anthropic)" and village.budget()["usd"] > 0, seen
print("OK Anthropic only: text, system prompt, real token cost charged")

setenv(ANTHROPIC_API_KEY="k-a", X_BEARER_TOKEN="k-b")
r = village.ask("find", X)
assert seen[0][0] == "x-api" and seen[0][2] == "Bearer k-b" and "start_time" in seen[0][1], seen
assert "real post" in seen[1][1]["messages"][0]["content"] and "x.com/alice/status/9" in seen[1][1]["messages"][0]["content"], seen
print("OK official X API: posts reach the model as data with links")

setenv(ANTHROPIC_API_KEY="k-a", XAI_API_KEY="k-x")
village.ask("find", X)
assert seen[0][2] == "Bearer k-x" and seen[0][0] == "/responses", seen
village.ask("text")
assert "messages" in seen[1][0], seen
print("OK both keys: Grok for X search, Claude for text")

setenv(LLM_PROVIDER="openai", XAI_API_KEY="k-x")
expect_stop(None, "No model key")
setenv(LLM_PROVIDER="nonsense", XAI_API_KEY="k-x")
expect_stop(None, "Unknown LLM_PROVIDER")
setenv()
expect_stop(None, "No model key")
assert server.chief_name() == "no model key"
print("OK missing or wrong settings stop with a clear message")

setenv(XAI_API_KEY="k-x")
assert '"who": "responses"' in server.chief_reply("hi") and "the owner says: hi" in seen[0][1]["input"][0]["content"], seen
print("OK Chief reply, no personal name")

setenv(GROQ_API_KEY="k-g")
r = village.ask("hello", system="sys")
assert r["data"] == {"who": "chat"} and seen[0][0] == "/chat/completions" and seen[0][1]["messages"][0]["role"] == "system" and seen[0][2] == "Bearer k-g", seen
setenv(LLM_PROVIDER="custom", LLM_BASE_URL=base, LLM_MODEL="my-model")
r = village.ask("hello")
assert r["data"] == {"who": "chat"} and seen[0][1]["model"] == "my-model" and seen[0][2] is None, seen
print("OK OpenAI-compatible and custom providers")

import settings  # noqa: E402

tmp = Path(tempfile.mkdtemp())
settings.ENV, settings.TRACKED, settings.WATCHLIST = tmp / ".env", tmp / "w.csv", tmp / "x.txt"
good = "So11111111111111111111111111111111111111112"
settings.save({"OPENAI_API_KEY": "sk-test", "MY_WALLETS": good, "X_SEARCH_QUERY": "(a OR b) lang:en", "UNKNOWN": "x"}, tracked=good + ",whale", watchlist="@alice", finish=True)
env = settings.read_env()
assert env["OPENAI_API_KEY"] == "sk-test" and env["X_SEARCH_QUERY"] == "(a OR b) lang:en" and env["SETUP_DONE"] == "1" and "UNKNOWN" not in env, env
pub = settings.public()
assert "sk-test" not in json.dumps(pub) and next(f for f in pub["fields"] if f["key"] == "OPENAI_API_KEY")["set"], pub
settings.save({"OPENAI_API_KEY": ""})
assert settings.read_env()["OPENAI_API_KEY"] == "sk-test"
settings.save({}, clear=["OPENAI_API_KEY"])
assert "OPENAI_API_KEY" not in settings.read_env()
for bad, why in (({"MY_WALLETS": "5" * 88}, "private key"), ({"MY_WALLETS": "nope"}, "Not a valid"), ({"VPS_HOST": "-oProxyCommand=x"}, "Server address"), ({"LLM_MODEL": "a'b"}, "line breaks")):
    try:
        settings.save(bad)
    except settings.Invalid as e:
        assert why in str(e), e
    else:
        raise AssertionError(bad)
print("OK settings: secrets hidden, private keys and bad values refused")

import mcp_client  # noqa: E402

fake = tmp / "fake_mcp.py"
fake.write_text("""import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    r = {}
    if m["method"] == "tools/list":
        r = {"tools": [{"name": "read", "annotations": {"readOnlyHint": True}, "inputSchema": {}}, {"name": "write", "inputSchema": {}}]}
    if m["method"] == "tools/call":
        r = {"content": [{"type": "text", "text": "result of " + m["params"]["name"]}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}), flush=True)
""")
mcp_client.CONFIG = tmp / "mcp.json"
cmd = f'"{Path(sys.executable).as_posix()}" "{fake.as_posix()}"'
mcp_client.save([{"name": "demo", "command": cmd}])
assert [t["name"] for t in mcp_client.available()] == ["read"]
assert mcp_client.call("demo", "read", {}) == "result of read"
try:
    mcp_client.call("demo", "write", {})
except mcp_client.McpError:
    pass
else:
    raise AssertionError("write tool was allowed")
setenv(XAI_API_KEY="k-x")


class Script:
    n = 0


real_ask = village.ask


def fake_ask(prompt, tool=None, system=None):
    Script.n += 1
    if Script.n == 1:
        return {"text": '{"server": "demo", "tool": "read", "arguments": {}}'}
    assert "result of read" in prompt
    return {"text": "done"}


village.ask = fake_ask
assert server.chief_reply("use it") == "done" and Script.n == 2
village.ask = real_ask
print("OK MCP: read tools work, write tools blocked, the Chief can call tools")
srv.shutdown()

#!/usr/bin/env python3
"""Starts PumpTown.

  python run.py                start everything and open the browser
  python run.py --no-browser   same, for servers
  python run.py deploy         copy this folder to your server and start it there
  python run.py tunnel         open the town running on your server
"""
import glob
import io
import os
import shlex
import subprocess
import sys
import tarfile
import time
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "copilot"), str(ROOT / "village")]
import settings  # noqa: E402

IDL = "https://raw.githubusercontent.com/pump-fun/pump-public-docs/main/idl/"
SERVICES = {"recorder": ("recorder", "record.py"), "simulator": ("village", "executor.py"),
            "watcher": ("village", "watcher.py"), "town": ("village", "server.py")}
SKIP_DIRS = {".git", ".venv", "venv", "data", "logs", "__pycache__", "backups"}
SKIP_SUFFIX = (".log", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".jsonl")
REMOTE_SESSION = "pumptown-app"


def ensure_idl():
    (ROOT / "idl").mkdir(exist_ok=True)
    for name in ("pump.json", "pump_amm.json"):
        path = ROOT / "idl" / name
        if not path.exists():
            print(f"Downloading {name}")
            req = urllib.request.Request(IDL + name, headers={"User-Agent": "pumptown"})
            with urllib.request.urlopen(req, timeout=60) as r:
                path.write_bytes(r.read())


def child_env():
    return {**os.environ, **settings.read_env(), "PYTHONPATH": str(ROOT / "copilot"), "PYTHONUNBUFFERED": "1"}


def spawn(name, folder, script, *args):
    (ROOT / "logs").mkdir(exist_ok=True)
    log = open(ROOT / "logs" / f"{name}.log", "ab")
    return subprocess.Popen([sys.executable, script, *args], cwd=ROOT / folder, env=child_env(), stdout=log, stderr=log)


def clean_old_data():
    keep = int(settings.read_env().get("RECORDER_KEEP_DAYS") or 7)
    data = Path(settings.read_env().get("PUMPTOWN_DATA") or ROOT / "data")
    days = sorted(glob.glob(str(data / "pump_*.sqlite")))
    for old in days[:-keep] if keep > 0 else []:
        for f in glob.glob(old + "*"):
            os.remove(f)


def agents_due(last_day):
    """The agent cycle runs once a day, and only with an AI provider."""
    import llm
    now = datetime.now(timezone.utc)
    hour = int(settings.read_env().get("VILLAGE_RUN_HOUR_UTC") or 14)
    day = now.strftime("%Y-%m-%d")
    return now.hour == hour and day != last_day and llm.provider() is not None, day


def start(open_browser):
    ensure_idl()
    subprocess.run([sys.executable, "server.py", "--init"], cwd=ROOT / "village", env=child_env(), check=True)
    procs = {n: spawn(n, *s) for n, s in SERVICES.items()}
    port = settings.read_env().get("PUMPTOWN_PORT") or "8421"
    url = f"http://127.0.0.1:{port}/"
    print(f"PumpTown is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        time.sleep(2)
        webbrowser.open(url)
    last_day, cleaned, cycle = None, 0.0, None
    try:
        while True:
            time.sleep(5)
            for name, p in procs.items():
                if p.poll() is not None:
                    print(f"{name} stopped, restarting")
                    procs[name] = spawn(name, *SERVICES[name])
            if time.time() - cleaned > 3600:
                clean_old_data()
                cleaned = time.time()
            due, day = agents_due(last_day)
            if due and (cycle is None or cycle.poll() is not None):
                last_day = day
                cycle = spawn("agents", "village", "village.py", "run")
    except KeyboardInterrupt:
        print("Stopping")
    finally:
        for p in [*procs.values(), *([cycle] if cycle else [])]:
            if p.poll() is None:
                p.terminate()


def bundle():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                if not name.endswith(SKIP_SUFFIX):
                    path = Path(folder) / name
                    tar.add(path, arcname=str(path.relative_to(ROOT)))
    return buf.getvalue()


def deploy():
    env = settings.read_env()
    cmd, target = settings.ssh_base(env)
    folder = env.get("VPS_DIR") or "PumpTown"
    q = shlex.quote(folder)
    print(f"Copying to {target}:{folder}")
    subprocess.run([*cmd, target, f"mkdir -p {q} && tar xzf - -C {q}"], input=bundle(), check=True)
    print("Installing and starting")
    remote = (f"cd {q} && chmod 600 .env 2>/dev/null; python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt && "
              f"(tmux kill-session -t {REMOTE_SESSION} 2>/dev/null; tmux new-session -d -s {REMOTE_SESSION} '.venv/bin/python run.py --no-browser')")
    subprocess.run([*cmd, target, remote], check=True)
    print("Started. Open it with: python run.py tunnel")


def tunnel():
    env = settings.read_env()
    cmd, target = settings.ssh_base(env)
    port = env.get("PUMPTOWN_PORT") or "8421"
    p = subprocess.Popen([*cmd, "-N", "-L", f"{port}:127.0.0.1:{port}", target])
    time.sleep(3)
    webbrowser.open(f"http://127.0.0.1:{port}/")
    print("Tunnel open (Ctrl+C to close)")
    try:
        p.wait()
    except KeyboardInterrupt:
        p.terminate()


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if args[:1] == ["deploy"]:
            deploy()
        elif args[:1] == ["tunnel"]:
            tunnel()
        else:
            start("--no-browser" not in args)
    except (settings.Invalid, subprocess.CalledProcessError) as e:
        sys.exit(f"Error: {e}")

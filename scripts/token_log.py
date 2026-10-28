#!/usr/bin/env python3
"""Count the tokens Claude Code has used on this Mac and publish the total.

Claude Code keeps two records: lifetime totals in ~/.claude/stats-cache.json,
and per-request usage in ~/.claude/projects/**/*.jsonl, which it deletes after
about 30 days. The total is the stats cache plus every transcript day the cache
has not caught up with yet, counted the way /stats counts them, so each laptop
adds exactly what its /stats shows.

A ledger in ~/Library/Application Support/token-log/ remembers the highest value
ever seen for each source, so cleaned-up transcripts never make the number drop.
Only aggregate numbers leave the machine, never log content. Every laptop
publishes its own data/tokens/<machine>.json and the card adds them up, so the
work and the private laptop never overwrite each other.

launchd wakes the script every hour, and it pushes on the first run of each day
that gets through. A laptop that was asleep, shut or offline catches up within
an hour of being back.

    python3 scripts/token_log.py --write /tmp/tokens.json             # preview locally
    python3 scripts/token_log.py --push --machine work                # update data/tokens/work.json on GitHub
    python3 scripts/token_log.py --install --machine work             # push once a day via launchd
    python3 scripts/token_log.py --uninstall

Stdlib only, Python 3.9+ (runs on the /usr/bin/python3 that ships with macOS).
"""

import argparse
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

REPO = "Kisame76/Kisame76"
BRANCH = "main"
REMOTE_DIR = "data/tokens"
LABEL = "com.kisame76.token-log"
INTERVAL = 60 * 60  # how often launchd checks; --daily keeps it to one push a day


def support_dir(home):
    return os.path.join(home, "Library", "Application Support", "token-log")


def usage_total(u):
    """(all, fresh) for one usage block; fresh leaves out tokens read back from the prompt cache."""
    cached = u.get("cache_read_input_tokens") or 0
    rest = (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    return rest + cached, rest


def read_transcripts(home):
    """{day: {all, fresh}} from Claude Code transcripts.

    Every logged usage block counts, as in /stats and its cache. That repeats a
    response split over several lines, but deduplicating only the days after the
    cache would make the total depend on when /stats last ran.
    """
    days = {}
    for path in glob.glob(os.path.join(home, ".claude", "projects", "**", "*.jsonl"), recursive=True):
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                msg = entry.get("message")
                usage = msg.get("usage") if isinstance(msg, dict) else None
                stamp = entry.get("timestamp")
                if not usage or not stamp:
                    continue
                every, fresh = usage_total(usage)
                day = days.setdefault(stamp[:10], {"all": 0, "fresh": 0})
                day["all"] += every
                day["fresh"] += fresh
    return days


def read_stats_cache(home):
    """Lifetime totals from ~/.claude/stats-cache.json, or None."""
    path = os.path.join(home, ".claude", "stats-cache.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    every = cached = 0
    for model in (data.get("modelUsage") or {}).values():
        for key in ("inputTokens", "outputTokens", "cacheCreationInputTokens", "cacheReadInputTokens"):
            every += int(model.get(key) or 0)
        cached += int(model.get("cacheReadInputTokens") or 0)
    return {
        "through": (data.get("lastComputedDate") or "")[:10],
        "since": (data.get("firstSessionDate") or "")[:10],
        "all": every,
        "fresh": every - cached,
    }


def load_ledger(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {"cache": None, "days": {}}


def save_ledger(ledger, path):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def remember(ledger, cache, days):
    """Fold a fresh scan into the ledger, keeping the high-water mark of everything."""
    old = ledger.get("cache")
    # a reset stats cache comes back smaller; keep the old one until it overtakes it
    if cache and (old is None or (cache["through"] >= old["through"] and cache["all"] >= old["all"])):
        ledger["cache"] = cache
    stored = ledger.setdefault("days", {})
    for day, counts in days.items():
        known = stored.setdefault(day, {"all": 0, "fresh": 0})
        known["all"] = max(known["all"], counts["all"])
        known["fresh"] = max(known["fresh"], counts["fresh"])
    return ledger


def tally(ledger):
    """(all, fresh, first day) over the stats cache and the transcript days after it."""
    cache = ledger.get("cache") or {"through": "", "since": "", "all": 0, "fresh": 0}
    every, fresh = cache["all"], cache["fresh"]
    for day, counts in ledger.get("days", {}).items():
        if day > cache["through"]:
            every += counts["all"]
            fresh += counts["fresh"]
    firsts = [d for d in [cache["since"]] + list(ledger.get("days", {})) if d]
    return every, fresh, min(firsts) if firsts else None


def snapshot(home, ledger_path):
    ledger = remember(load_ledger(ledger_path), read_stats_cache(home), read_transcripts(home))
    save_ledger(ledger, ledger_path)
    every, fresh, since = tally(ledger)
    return {
        "total": every,
        "fresh": fresh,
        "since": since,
        "source": "Claude Code",
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def as_text(data):
    return json.dumps(data, indent=2) + "\n"


def gh_binary():
    found = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if os.path.exists(p)), None)
    if not found:
        sys.exit("gh CLI not found")
    return found


def push(data, machine):
    """Write data/tokens/<machine>.json on GitHub through the contents API, but never lower it."""
    gh = gh_binary()
    remote_path = f"{REMOTE_DIR}/{machine}.json"
    endpoint = f"repos/{REPO}/contents/{remote_path}"
    got = subprocess.run([gh, "api", f"{endpoint}?ref={BRANCH}"], capture_output=True, text=True)
    sha, current = None, {}
    if got.returncode == 0:
        remote = json.loads(got.stdout)
        sha = remote["sha"]
        try:
            current = json.loads(base64.b64decode(remote.get("content") or ""))
        except ValueError:
            current = {}
    elif "404" not in got.stderr:
        sys.exit(f"reading {remote_path} failed: {got.stderr.strip()}")
    if data["total"] <= (current.get("total") or 0):
        print(f"remote already shows {current['total']:,}, nothing to push")
        return
    body = {"message": f"Update token log ({machine})", "content": base64.b64encode(as_text(data).encode()).decode(), "branch": BRANCH}
    if sha:
        body["sha"] = sha
    put = subprocess.run([gh, "api", "-X", "PUT", endpoint, "--input", "-"], input=json.dumps(body), capture_output=True, text=True)
    if put.returncode != 0:
        sys.exit(f"pushing {remote_path} failed: {put.stderr.strip()}")
    print(f"pushed {data['total']:,} tokens to {REPO}/{remote_path}")


def plist_path(home):
    return os.path.join(home, "Library", "LaunchAgents", f"{LABEL}.plist")


def install(home, machine):
    import plistlib

    # launchd runs a copy, so moving or editing the repo never breaks the job
    script = os.path.join(support_dir(home), "token_log.py")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    if os.path.abspath(__file__) != script:
        shutil.copyfile(os.path.abspath(__file__), script)
    log = os.path.join(home, "Library", "Logs", "token-log.log")
    python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    with open(plist_path(home), "wb") as fh:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": [python, script, "--push", "--daily", "--machine", machine],
            "StartInterval": INTERVAL,
            "RunAtLoad": True,
            "StandardOutPath": log,
            "StandardErrorPath": log,
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        }, fh)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, plist_path(home)], check=True)
    print(f"installed {plist_path(home)}, log in {log}")


def uninstall(home):
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if os.path.exists(plist_path(home)):
        os.remove(plist_path(home))
    print(f"removed {plist_path(home)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", metavar="PATH", help="write tokens.json to PATH")
    ap.add_argument("--push", action="store_true", help=f"update {REMOTE_DIR}/<machine>.json in {REPO} via gh")
    ap.add_argument("--machine", metavar="NAME", help="name of this laptop in the repo, e.g. work or private")
    ap.add_argument("--daily", action="store_true", help="do nothing if this laptop already pushed today")
    ap.add_argument("--install", action="store_true", help="push once a day via launchd")
    ap.add_argument("--uninstall", action="store_true", help="remove the launchd job")
    ap.add_argument("--ledger", metavar="PATH", help="ledger file (default: ~/Library/Application Support/token-log)")
    args = ap.parse_args(argv)

    home = os.path.expanduser("~")
    if (args.push or args.install) and not re.fullmatch(r"[a-z0-9-]+", args.machine or ""):
        sys.exit("--push and --install need --machine, e.g. --machine work (lowercase letters, digits, dashes)")
    if args.install:
        return install(home, args.machine)
    if args.uninstall:
        return uninstall(home)

    ledger_path = args.ledger or os.path.join(support_dir(home), "ledger.json")
    today = datetime.now().strftime("%Y-%m-%d")
    if args.daily and load_ledger(ledger_path).get("pushed") == today:
        return
    data = snapshot(home, ledger_path)
    print(f"{data['total']:,} tokens ({data['fresh']:,} fresh) since {data['since']}")
    if args.write:
        os.makedirs(os.path.dirname(os.path.abspath(args.write)), exist_ok=True)
        with open(args.write, "w", encoding="utf-8") as fh:
            fh.write(as_text(data))
    if args.push:
        # push() exits on any failure, so an offline run is simply tried again next hour
        push(data, args.machine)
        ledger = load_ledger(ledger_path)
        ledger["pushed"] = today
        save_ledger(ledger, ledger_path)


if __name__ == "__main__":
    main()

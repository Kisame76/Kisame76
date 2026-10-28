#!/usr/bin/env python3
"""Draw the stats under the hero: assets/stats-dark.svg and assets/stats-light.svg.

Fetches public numbers (contributions, languages, merged upstream PRs,
Packagist installs) into data/stats.json, adds up data/tokens/*.json, which
scripts/token_log.py pushes from each of my laptops, and renders both colour schemes.
The card reads like a sea chart: each section sits one isobath deeper, and the
last 30 days of contributions are drawn as the seabed an echo sounder sees.

    python3 scripts/build_stats.py            # fetch (GITHUB_TOKEN or `gh auth token`), then draw
    python3 scripts/build_stats.py --offline  # draw from data/stats.json only

Stdlib only, Python 3.9+.
"""

import argparse
import json
import math
import re
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
STATS = ROOT / "data" / "stats.json"
TOKENS = ROOT / "data" / "tokens"
STACK = ROOT / "data" / "stack.json"
ASSETS = ROOT / "assets"

LOGIN = "Kisame76"
VENDOR = "kisame76"
MIN_UPSTREAM_STARS = 50
CHART_DAYS = 30
TZ = ZoneInfo("Europe/Berlin")
UA = "kisame76-profile-stats"

W = 1000
FONT = '"Avenir Next Condensed", "DIN Condensed", Bahnschrift, "Roboto Condensed", "DejaVu Sans Condensed", "Arial Narrow", sans-serif'
SCHEMES = {
    # same ink and teal as the hero, so card and shark read as one picture
    "dark": {"ink": "#e6f3f7", "sea": "#43b6cc", "glow": "#b4f4ff"},
    "light": {"ink": "#0b2e40", "sea": "#02599d", "glow": "#1fc4e6"},
}


# ---------- fetch ----------

def get_json(url, token=None, body=None):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=30) as res:
        return json.load(res)


def graphql(token, query, **variables):
    payload = get_json("https://api.github.com/graphql", token, {"query": query, "variables": variables})
    if payload.get("errors"):
        raise RuntimeError(f"graphql: {payload['errors']}")
    return payload["data"]


CALENDAR = """query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) { contributionsCollection(from: $from, to: $to) {
    contributionCalendar { weeks { contributionDays { date contributionCount } } } } } }"""

REPOS = """query($login: String!) {
  user(login: $login) {
    createdAt
    repositories(first: 100, ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC) {
      nodes { name stargazerCount languages(first: 20) { edges { size node { name } } } } } } }"""

UPSTREAM = """query($q: String!) {
  search(type: ISSUE, query: $q, first: 100) {
    nodes { ... on PullRequest { repository { nameWithOwner isPrivate stargazerCount } } } } }"""


def fetch_days(token, since):
    """{date: count} from the first year on GitHub until today, one calendar year per query."""
    days = {}
    today = datetime.now(timezone.utc)
    for year in range(since.year, today.year + 1):
        start = datetime(year, 1, 1, tzinfo=timezone.utc)
        end = min(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc), today)
        cal = graphql(token, CALENDAR, login=LOGIN, **{"from": start.isoformat(), "to": end.isoformat()})
        for week in cal["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]:
            for d in week["contributionDays"]:
                days[d["date"]] = d["contributionCount"]
    return days


def streaks(days, today):
    """(current, longest) run of days with at least one contribution."""
    longest = run = 0
    for day in sorted(days):
        run = run + 1 if days[day] else 0
        longest = max(longest, run)
    current = 0
    cursor = today if days.get(today.isoformat()) else today - timedelta(days=1)  # today is not over yet
    while days.get(cursor.isoformat()):
        current += 1
        cursor -= timedelta(days=1)
    return current, longest


def languages(repos, top=6):
    sizes = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            sizes[edge["node"]["name"]] = sizes.get(edge["node"]["name"], 0) + edge["size"]
    total = sum(sizes.values()) or 1
    ranked = sorted(sizes.items(), key=lambda kv: -kv[1])[:top]
    return [[name, round(size / total * 100, 1)] for name, size in ranked]


def upstream(token):
    """[[repo, stars, merged PRs]] for public repos I don't own with enough stars to matter."""
    q = f"is:pr is:merged author:{LOGIN} -user:{LOGIN}"
    counts = {}
    for node in graphql(token, UPSTREAM, q=q)["search"]["nodes"]:
        repo = (node or {}).get("repository")
        if not repo or repo["isPrivate"] or repo["stargazerCount"] < MIN_UPSTREAM_STARS:
            continue
        row = counts.setdefault(repo["nameWithOwner"], [repo["nameWithOwner"], repo["stargazerCount"], 0])
        row[2] += 1
    return sorted(counts.values(), key=lambda r: (-r[1], r[0]))


def projects(repos):
    """[[repo, stars, Packagist installs or None]] for my public repos, most starred first."""
    installs = {}
    for name in get_json(f"https://packagist.org/packages/list.json?vendor={VENDOR}")["packageNames"]:
        installs[name.split("/", 1)[1]] = get_json(f"https://packagist.org/packages/{name}/stats.json")["downloads"]["total"]
    rows = [[r["name"], r["stargazerCount"], installs.get(r["name"])] for r in repos]
    return sorted(rows, key=lambda r: (-r[1], -(r[2] or 0), r[0]))


def fetch(token):
    today = datetime.now(timezone.utc).date()
    user = graphql(token, REPOS, login=LOGIN)["user"]
    # this profile repo is not a project
    repos = [r for r in user["repositories"]["nodes"] if r["name"].lower() != LOGIN.lower()]
    days = fetch_days(token, datetime.fromisoformat(user["createdAt"].replace("Z", "+00:00")))
    current, longest = streaks(days, today)
    year_ago = (today - timedelta(days=365)).isoformat()
    recent = [today - timedelta(days=i) for i in range(CHART_DAYS - 1, -1, -1)]
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contributions": {
            "days": [[d.isoformat(), days.get(d.isoformat(), 0)] for d in recent],
            "year": sum(c for d, c in days.items() if d > year_ago),
            "streak": current,
            "longest": longest,
        },
        "languages": languages(repos),
        "projects": projects(repos),
        "upstream": upstream(token),
    }


def token_sum():
    """One total over every laptop's data/tokens/<machine>.json, or None before the first push."""
    files = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(TOKENS.glob("*.json"))]
    if not files:
        return None
    return {
        "total": sum(t["total"] for t in files),
        "fresh": sum(t["fresh"] for t in files),
        "since": min(t["since"] for t in files),
        "source": files[0].get("source", "Claude Code"),
    }


def find_token():
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("set GITHUB_TOKEN or log in with `gh auth login`")


# ---------- draw ----------

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def f(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


def text(x, y, s, cls, anchor=None):
    a = f' text-anchor="{anchor}"' if anchor else ""
    return f'<text class="{cls}" x="{f(x)}" y="{f(y)}"{a}>{esc(s)}</text>'


def short(n):
    for size, unit in ((1e9, " billion"), (1e6, " million"), (1e3, "k")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


def day_label(iso):
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%b}"


def smooth(xs, ys):
    """Monotone cubic through the points, so the seabed never dips below zero between days."""
    n = len(xs)
    d = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(n - 1)]
    m = [d[0]] + [0.0 if d[i - 1] * d[i] <= 0 else (d[i - 1] + d[i]) / 2 for i in range(1, n - 1)] + [d[-1]]
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / d[i], m[i + 1] / d[i]
        if a * a + b * b > 9:
            t = 3 / math.hypot(a, b)
            m[i], m[i + 1] = t * a * d[i], t * b * d[i]
    path = f"M{f(xs[0])},{f(ys[0])}"
    for i in range(n - 1):
        h = xs[i + 1] - xs[i]
        path += (f" C{f(xs[i] + h / 3)},{f(ys[i] + m[i] * h / 3)} "
                 f"{f(xs[i + 1] - h / 3)},{f(ys[i + 1] - m[i + 1] * h / 3)} {f(xs[i + 1])},{f(ys[i + 1])}")
    return path


def soundings(counts, keep=3, gap=4):
    """Indexes of the highest days, spread out so their numbers never collide."""
    picked = []
    for i in sorted(range(len(counts)), key=lambda i: -counts[i]):
        if counts[i] and all(abs(i - j) >= gap for j in picked):
            picked.append(i)
        if len(picked) == keep:
            break
    return picked


def isobath(depth, phase):
    """A contour line across the card with its depth written into a gap, like on a sea chart."""
    wave, amp, gap_x = 250, 3, 920
    ys = [10 + amp * math.sin((x / wave + phase) * 2 * math.pi) for x in range(0, W + 1, 25)]
    path = "M" + " L".join(f"{x},{f(y)}" for x, y in zip(range(0, W + 1, 25), ys))
    label = f"{depth:,} m"
    return 24, (
        f'<mask id="gap{depth}" maskUnits="userSpaceOnUse" x="0" y="0" width="{W}" height="24">'
        f'<rect width="{W}" height="24" fill="#fff"/><rect x="{gap_x - 44}" width="88" height="24" fill="#000"/></mask>'
        f'<path class="iso" d="{path}" mask="url(#gap{depth})"/>'
        + text(gap_x, 15, label, "depth", "middle")
    )


def sec_stack(stack):
    """Tool logos in groups, one row edge to edge when they fit; names without a logo are set as type."""
    icon, gap, min_group_gap = 30, 14, 36
    groups = []
    for g in stack["groups"]:
        tools, x = [], 0
        for t in g["tools"]:
            w = icon if t.get("path") else round(len(t["name"]) * 11)
            tools.append((x, w, t))
            x += w + gap
        groups.append((x - gap, g["name"], tools))
    rows, row = [], []
    for g in groups:
        if row and sum(w for w, _, _ in row) + len(row) * min_group_gap + g[0] > W:
            rows.append(row)
            row = []
        row.append(g)
    rows.append(row)

    count = sum(len(g["tools"]) for g in stack["groups"])
    o = [text(0, 40, "Stack", "title"), text(0, 68, f"{count} tools I build with", "sub")]
    y = 100
    for row in rows:
        spare = W - sum(w for w, _, _ in row)
        between = spare / (len(row) - 1) if len(row) > 1 and len(rows) == 1 else min_group_gap
        x = 0
        for width, name, tools in row:
            for tx, w, t in tools:
                if t.get("path"):
                    o.append(f'<g transform="translate({f(x + tx)} {y}) scale({icon / 24:g})"><title>{esc(t["name"])}</title>'
                             f'<path class="tool" d="{t["path"]}"/></g>')
                else:
                    o.append(f'<text class="mark" x="{f(x + tx)}" y="{y + 24}" textLength="{w}" lengthAdjust="spacingAndGlyphs">{esc(t["name"])}</text>')
            o.append(text(x, y + icon + 28, name, "sounding"))
            x += width + between
        y += icon + 70
    return y - 24, "".join(o)


def sec_activity(data):
    c = data["contributions"]
    days = c["days"]
    counts = [n for _, n in days]
    x0, y0, cw, ch = 0, 92, 620, 132
    top = max(max(counts), 1)
    xs = [x0 + cw * i / (len(counts) - 1) for i in range(len(counts))]
    base = y0 + ch
    ys = [base - ch * 0.86 * n / top for n in counts]
    ridge = smooth(xs, ys)
    floor = f"{ridge} L{f(x0 + cw)},{base} L{x0},{base} Z"
    o = [
        text(0, 40, "Activity", "title"),
        text(0, 68, "Contributions per day, last 30 days", "sub"),
        f'<clipPath id="seabed"><path d="{floor}"/></clipPath>',
        f'<path d="{floor}" fill="url(#deep)"/>',
    ]
    # inner contours give the ridge some relief
    for k in (0.62, 0.3):
        inner = smooth(xs, [base - (base - y) * k for y in ys])
        o.append(f'<path class="contour" d="{inner}" clip-path="url(#seabed)"/>')
    o.append(f'<g clip-path="url(#seabed)"><rect class="sweep" x="{x0 - 120}" y="{y0}" width="120" height="{ch}" fill="url(#ping)"/></g>')
    o.append(f'<path class="ridge" d="{ridge}"/>')
    o.append(f'<line class="floor" x1="{x0}" x2="{x0 + cw}" y1="{base + .5}" y2="{base + .5}"/>')
    for i in soundings(counts):
        anchor = "start" if i == 0 else "end" if i == len(counts) - 1 else "middle"
        o.append(text(xs[i], ys[i] - 12, counts[i], "sounding", anchor))
    o.append(text(x0, base + 30, day_label(days[0][0]), "note"))
    o.append(text(x0 + cw, base + 30, "today", "note", "end"))

    # numbers share a right edge, labels hang off it on two lines; the last row sits on the seabed floor
    nx = 830
    for i, (n, first, second) in enumerate([
        (f"{c['year']:,}", "contributions", "in the last year"),
        (str(c["streak"]), "day streak", "right now"),
        (str(c["longest"]), "longest", "streak"),
    ]):
        y = base - (2 - i) * 46
        o.append(text(nx, y, n, "num", "end"))
        o.append(text(nx + 16, y - 19, first, "note"))
        o.append(text(nx + 16, y, second, "note"))
    return base + 52, "".join(o)


def sec_depths(data):
    o = [text(0, 40, "Languages", "title"), text(0, 68, "By bytes across my public repos", "sub")]
    for i, (name, pct) in enumerate(data["languages"]):
        y = 108 + i * 30
        bx, bw = 118, 290
        o.append(text(0, y, name, "label"))
        o.append(f'<rect class="track" x="{bx}" y="{y - 7}" width="{bw}" height="4" rx="2"/>')
        o.append(f'<rect class="bar" x="{bx}" y="{y - 7}" width="{f(max(bw * pct / 100, 3))}" height="4" rx="2"/>')
        o.append(text(470, y, f"{pct:g} %", "sounding", "end"))
    ly = 108 + 30 * (len(data["languages"]) - 1)
    if data["upstream"]:
        ly += 44
        o.append(text(0, ly, "Merged upstream", "sub"))
        for repo, stars, prs in data["upstream"]:
            ly += 30
            o.append(text(0, ly, repo, "label"))
            o.append(text(470, ly, f"{short(stars)} stars", "sounding", "end"))

    rx, sx, ix = 560, 900, W
    rows = data["projects"]
    o += [text(rx, 40, "Shipped", "title"), text(rx, 68, "My public projects on GitHub and Packagist", "sub")]
    for (n, label), x in zip([
        (str(len(rows)), "open-source projects"),
        (f"{sum(r[1] for r in rows):,}", "stars on GitHub"),
        (short(sum(r[2] or 0 for r in rows)), "Packagist installs"),
    ], (rx, rx + 160, rx + 300)):
        o.append(text(x, 124, n, "num"))
        o.append(text(x, 148, label, "note"))
    ry = 194
    o.append(text(sx, ry, "stars", "note", "end"))
    o.append(text(ix, ry, "installs", "note", "end"))
    for name, stars, installs in rows:
        ry += 30
        o.append(text(rx, ry, name, "label"))
        o.append(text(sx, ry, stars, "sounding", "end"))
        o.append(text(ix, ry, "–" if installs is None else f"{installs:,}", "sounding", "end"))
    return max(ly, ry) + 24, "".join(o)


def sec_tokens(tokens):
    total, fresh = tokens["total"], tokens["fresh"]
    since = date.fromisoformat(tokens["since"])
    share = fresh / total if total else 0
    o = [
        text(0, 40, "Tokens", "title"),
        text(0, 68, f"Used with {tokens.get('source', 'Claude Code')} since {since:%B %Y}", "sub"),
        text(0, 168, f"{total:,}", "huge"),
        text(W, 168, f"≈ {short(total)}", "sub", "end"),
    ]
    by = 196
    fw = max(W * share, 4)
    o.append(f'<rect class="track" x="0" y="{by}" width="{W}" height="6" rx="3"/>')
    o.append(f'<rect class="cached" x="{f(fw)}" y="{by}" width="{f(W - fw)}" height="6" rx="3"/>')
    o.append(f'<rect class="fresh" x="0" y="{by}" width="{f(fw)}" height="6" rx="3"/>')
    o.append(text(0, by + 32, f"{share * 100:.1f} % new input and output", "note"))
    o.append(text(W, by + 32, f"{(1 - share) * 100:.1f} % read back from the prompt cache", "note", "end"))
    return by + 52, "".join(o)


def sec_footer(data):
    stamp = datetime.fromisoformat(data["generated"].replace("Z", "+00:00")).astimezone(TZ)
    line = f"Redrawn every 6 hours by GitHub Actions. Last sounding {stamp.day} {stamp:%b %Y, %H:%M %Z}."
    return 34, text(0, 26, line, "note")


def style(p):
    return f"""
text {{ font-family: {FONT}; font-stretch: condensed; fill: {p['ink']}; }}
.title {{ font-size: 30px; font-weight: 600; letter-spacing: .5px; }}
.sub {{ font-size: 19px; opacity: .6; }}
.label {{ font-size: 19px; opacity: .85; }}
.note {{ font-size: 17px; opacity: .5; }}
.num {{ font-size: 46px; font-weight: 700; }}
.huge {{ font-size: 104px; font-weight: 700; letter-spacing: -1px; }}
.sounding {{ font-size: 18px; font-style: italic; opacity: .75; }}
.depth {{ font-size: 16px; font-style: italic; fill: {p['sea']}; }}
.iso {{ fill: none; stroke: {p['sea']}; stroke-opacity: .45; stroke-width: 1; }}
.ridge {{ fill: none; stroke: {p['sea']}; stroke-width: 2.5; stroke-linejoin: round; }}
.contour {{ fill: none; stroke: {p['sea']}; stroke-opacity: .35; stroke-width: 1; }}
.floor {{ stroke: {p['ink']}; stroke-opacity: .15; }}
.tool {{ fill: {p['ink']}; opacity: .85; }}
.mark {{ font-size: 25px; font-weight: 700; opacity: .85; }}
.track {{ fill: {p['ink']}; opacity: .1; }}
.bar {{ fill: {p['sea']}; }}
.cached {{ fill: {p['sea']}; opacity: .55; }}
.fresh {{ fill: {p['glow']}; }}
/* one sonar pass over the seabed, then quiet */
.sweep {{ animation: sweep 9s ease-in-out infinite; }}
@keyframes sweep {{ 0% {{ transform: translateX(0); }} 55%, 100% {{ transform: translateX(760px); }} }}
@media (prefers-reduced-motion: reduce) {{ .sweep {{ animation: none; opacity: 0; }} }}
"""


def render(data, tokens, stack, scheme):
    p = SCHEMES[scheme]
    parts = [sec_stack(stack)] if stack else []
    parts += [isobath(200, 0), sec_activity(data), isobath(1000, .35), sec_depths(data)]
    if tokens:
        parts += [isobath(4000, .7), sec_tokens(tokens)]
    parts.append(sec_footer(data))
    y, body = 0, []
    for h, svg in parts:
        body.append(f'<g transform="translate(0 {f(y)})">{svg}</g>')
        y += h
    title = "Stack, activity, languages, open-source projects and token use of Kisame76"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-2 0 {W + 4} {f(y)}" width="{W + 4}" height="{f(y)}" role="img" aria-label="{title}">'
        f"<title>{title}</title><style>{style(p)}</style><defs>"
        f'<linearGradient id="deep" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{p["sea"]}" stop-opacity=".42"/>'
        f'<stop offset="1" stop-color="{p["sea"]}" stop-opacity=".03"/></linearGradient>'
        f'<linearGradient id="ping" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{p["glow"]}" stop-opacity="0"/>'
        f'<stop offset=".92" stop-color="{p["glow"]}" stop-opacity=".55"/><stop offset="1" stop-color="{p["glow"]}" stop-opacity=".9"/></linearGradient>'
        f"</defs>{''.join(body)}</svg>\n"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="Draw assets/stats-{dark,light}.svg for the profile README.")
    ap.add_argument("--offline", action="store_true", help="skip fetching and draw from data/stats.json")
    args = ap.parse_args(argv)

    if args.offline:
        data = json.loads(STATS.read_text(encoding="utf-8"))
    else:
        # any failure exits non-zero, so the Action keeps the last good card
        data = fetch(find_token())
        STATS.parent.mkdir(exist_ok=True)
        # one row per line keeps the Action's diffs readable
        pretty = re.sub(r"\[[^\[\]{}]*\]", lambda m: re.sub(r"\[\s+|\s+\]|\s*\n\s*", lambda w: w.group().strip() or " ", m.group()), json.dumps(data, indent=2))
        STATS.write_text(pretty + "\n", encoding="utf-8")
    tokens = token_sum()
    stack = json.loads(STACK.read_text(encoding="utf-8")) if STACK.exists() else None

    for scheme in SCHEMES:
        out = ASSETS / f"stats-{scheme}.svg"
        out.write_text(render(data, tokens, stack, scheme), encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}")
    c = data["contributions"]
    print(f"{c['year']:,} contributions, streak {c['streak']}/{c['longest']}, "
          f"{len(data['projects'])} projects, {sum(p[1] for p in data['projects'])} stars, "
          f"{sum(p[2] or 0 for p in data['projects']):,} installs, {len(data['upstream'])} upstream repos")


if __name__ == "__main__":
    main()

"""Update stats.json, the repo statistics the About dialog shows (run by stats.yml).

    python update_stats.py <path to stats.json>

Clone counts come from the Traffic API, which needs a token with read access to the
repo's administration (``TRAFFIC_TOKEN``) and keeps only the last 14 days, so each run
merges the days it sees into the history already in the file. Release downloads are the
public ``download_count`` of every release zip (``GITHUB_TOKEN`` only raises the rate
limit). Without ``TRAFFIC_TOKEN`` the clone history is kept as it was and a warning says why.

Standard library only, so the workflow needs no installs.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

API = "https://api.github.com"
SCHEMA = 1


def get(path: str, token: Optional[str]) -> Any:
    req = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "KiPI-stats",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def merge_clones(daily: Dict[str, Dict[str, int]], days: List[dict]) -> Dict[str, Dict[str, int]]:
    """Fold the Traffic API's per-day entries into the history. A day seen again replaces
    the earlier record: today's entry grows until the day is over."""
    out = dict(daily)
    for d in days:
        out[d["timestamp"][:10]] = {"count": int(d["count"]), "uniques": int(d["uniques"])}
    return dict(sorted(out.items()))


def release_downloads(releases: List[dict]) -> Dict[str, int]:
    """Downloads of each release's uploaded zips (the manual-install zip and the PCM one),
    by tag. Other assets, such as the PCM ``metadata.json``, are not installs. GitHub does
    not count the automatic "Source code" archives, so a release without zips reads 0."""
    return {r["tag_name"]: sum(int(a.get("download_count", 0)) for a in r.get("assets", [])
                               if str(a.get("name", "")).lower().endswith(".zip"))
            for r in releases}


def build(old: dict, clone_days: Optional[List[dict]], uniques_14d: Optional[int],
          downloads: Dict[str, int], now: str) -> dict:
    """The new stats.json from the old one and what the API returned. ``clone_days`` is
    None when the traffic could not be read: the history and 14-day uniques carry over."""
    old_clones = old.get("clones", {}) if old.get("schema") == SCHEMA else {}
    daily = old_clones.get("daily", {})
    if clone_days is not None:
        daily = merge_clones(daily, clone_days)
        uniques = int(uniques_14d or 0)
    else:
        uniques = int(old_clones.get("uniques_14d", 0))
    return {
        "schema": SCHEMA,
        "updated": now,
        "clones": {
            "total": sum(d["count"] for d in daily.values()),
            "since": next(iter(daily), now[:10]),
            "uniques_14d": uniques,
            "daily": daily,
        },
        "release_downloads": {"total": sum(downloads.values()), "by_release": downloads},
    }


def main(argv: List[str]) -> int:
    path = Path(argv[1])
    repo = os.environ["GITHUB_REPOSITORY"]
    old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    traffic_token = os.environ.get("TRAFFIC_TOKEN")
    clone_days = uniques = None
    if traffic_token:
        t = get(f"/repos/{repo}/traffic/clones?per=day", traffic_token)
        clone_days, uniques = t.get("clones", []), t.get("uniques", 0)
    else:
        print("::warning title=No TRAFFIC_TOKEN::Clone counts not updated: add a TRAFFIC_TOKEN "
              "repository secret (fine-grained token, this repo, Administration: read-only).")

    releases: List[dict] = []
    page = 1
    while True:
        batch = get(f"/repos/{repo}/releases?per_page=100&page={page}", os.environ.get("GITHUB_TOKEN"))
        releases += batch
        if len(batch) < 100:
            break
        page += 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = build(old, clone_days, uniques, release_downloads(releases), now)
    path.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    c, r = stats["clones"], stats["release_downloads"]
    print(f"{c['total']} clones since {c['since']} ({c['uniques_14d']} unique in 14 days), "
          f"{r['total']} release downloads")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

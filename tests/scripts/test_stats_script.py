"""The stats workflow's script keeps the clone history GitHub forgets after 14 days (#59)."""

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "update_stats.py"
_spec = importlib.util.spec_from_file_location("update_stats", _PATH)
stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats)


def day(date, count, uniques=1):
    return {"timestamp": f"{date}T00:00:00Z", "count": count, "uniques": uniques}


def test_days_merge_and_a_day_seen_again_is_replaced():
    old = {"2026-10-01": {"count": 5, "uniques": 2}, "2026-10-02": {"count": 1, "uniques": 1}}
    out = stats.merge_clones(old, [day("2026-10-02", 4), day("2026-10-03", 7)])
    assert list(out) == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert [d["count"] for d in out.values()] == [5, 4, 7]


def test_total_grows_past_the_14_day_window():
    first = stats.build({}, [day("2026-10-01", 10), day("2026-10-02", 3)], 6, {}, "2026-10-02T03:00:00Z")
    # Two weeks later the API no longer reports 10-01 or 10-02.
    later = stats.build(first, [day("2026-10-16", 2)], 2, {"v0.7.0": 9}, "2026-10-16T03:00:00Z")
    c = later["clones"]
    assert c["total"] == 15 and c["since"] == "2026-10-01" and c["uniques_14d"] == 2
    assert later["release_downloads"] == {"total": 9, "by_release": {"v0.7.0": 9}}


def test_without_a_traffic_token_the_history_carries_over():
    first = stats.build({}, [day("2026-10-01", 10)], 6, {}, "2026-10-01T03:00:00Z")
    out = stats.build(first, None, None, {"v0.7.0": 1}, "2026-10-02T03:00:00Z")
    assert out["clones"]["total"] == 10 and out["clones"]["uniques_14d"] == 6
    assert out["updated"] == "2026-10-02T03:00:00Z"


def test_release_downloads_count_uploaded_zips_only():
    releases = [{"tag_name": "v0.7.0", "assets": [{"name": "KiPI-v0.7.0.zip", "download_count": 3},
                                                  {"name": "KiPI-v0.7.0-pcm.zip", "download_count": 4},
                                                  {"name": "metadata.json", "download_count": 50}]},
                {"tag_name": "v0.6.5", "assets": []}]
    assert stats.release_downloads(releases) == {"v0.7.0": 7, "v0.6.5": 0}


def test_output_is_what_the_about_dialog_reads():
    pytest.importorskip("qtpy")
    from kipi.gui import about
    out = stats.build({}, [day("2026-10-01", 10)], 6, {"v0.7.0": 2}, "2026-10-01T03:00:00Z")
    assert about.parse_stats(out) == about.RepoStats(10, "2026-10-01", 6, 2)

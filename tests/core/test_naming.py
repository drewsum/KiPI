"""Time-stamped names for the files a solve writes (#66)."""

import re
from datetime import datetime

from kipi.core.naming import stamped, timestamp
from kipi.dc.report.html import report_name

WHEN = datetime(2026, 10, 15, 9, 5, 7)


def test_timestamp_sorts_and_suits_file_names():
    assert timestamp(WHEN) == "2026-10-15_090507"
    assert timestamp(WHEN) < timestamp(datetime(2026, 10, 15, 10, 0, 0))
    assert not re.search(r'[<>:"/\\|?*\s]', timestamp())


def test_stamped():
    assert stamped("board", WHEN) == "board_2026-10-15_090507"


def test_dc_report_name():
    assert report_name("board", WHEN) == "board_kipi_report_2026-10-15_090507.html"

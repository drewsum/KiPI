"""Results tables that sort when a column header is clicked.

Each click on a header sorts by that column, alternating ascending and descending. Cells
sort by number where they hold one (or by an explicit key), otherwise by text, and blank
cells go last either way. Rows can be kept together: a child row (e.g. a VRM phase) stays
directly under its parent row, and pinned rows (e.g. a total) stay at the bottom.

Numbers take SI prefixes (10 µF sorts above 100 nF, 4u7 is 4.7µ) and text sorts naturally
(C9 before C10); a check box cell sorts ticked first. Tables of editors (cell widgets)
sort through a ``sorter`` that rebuilds their rows in the clicked order (#85).
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QTableWidget, QTableWidgetItem

_NUMBER = re.compile(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?")
# An SI prefix right after the number: before a unit, the end, or digits (4u7 = 4.7µ).
_PREFIX = re.compile(r"\s?([pnuµμmkKMG])(?=$|[FHΩRVAWsSz]|Hz|\d|\s)(\d*)")
_SCALE = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "k": 1e3,
          "K": 1e3, "M": 1e6, "G": 1e9}


def number(text: str) -> Optional[float]:
    """The number ``text`` starts with, scaled by an SI prefix after it; None if none."""
    m = _NUMBER.match(text)
    if not m:
        return None
    x = float(m.group(0))
    p = _PREFIX.match(text, m.end())
    if p and m.group(2) is None:
        frac = p.group(2)
        if frac and "." not in m.group(0):
            x = float(f"{m.group(0)}.{frac}")
        x *= _SCALE[p.group(1)]
    return x


def natural(text: str):
    """Text key that orders the numbers in it by value: C9 before C10."""
    parts = re.split(r"(\d+)", text.casefold())
    return tuple(int(t) if i % 2 else t for i, t in enumerate(parts))


def sort_key(text: str):
    """(0, number), (1, natural text) or (2, ()) for a blank: numbers first, blanks last."""
    text = text.replace("±", "").strip()
    if not text:
        return (2, ())
    x = number(text)
    return (0, x) if x is not None else (1, natural(text))


class SortItem(QTableWidgetItem):
    """A table cell that sorts by ``key`` if given, else by the number its text starts with."""

    def __init__(self, text: str = "", key: Optional[float] = None):
        super().__init__(text)
        self.key = key
        self.parent: Optional[SortItem] = None   # the cell this one's row stays under
        self.sub = 0                             # order under the parent, or among pins
        self.pin = 0                             # > 0: stays at the bottom
        self.row0 = 0                            # row when filled, to tell groups apart

    def value(self):
        """(0, number), (1, natural text) or (2, ()) for a blank cell; a check box sorts
        ticked first."""
        if self.key is not None:
            return (0, float(self.key))
        if self.flags() & Qt.ItemIsUserCheckable and not self.text():
            return (0, 0.0 if self.checkState() == Qt.Checked else 1.0)
        return sort_key(self.text())

    def __lt__(self, other):
        if not isinstance(other, SortItem):
            return super().__lt__(other)
        t = self.tableWidget()
        desc = t is not None and t.horizontalHeader().sortIndicatorOrder() == Qt.DescendingOrder
        # Qt sorts descending by asking "other < self", so a comparison that must keep
        # its direction whichever way the column sorts is flipped when descending.
        fixed = lambda a, b: (a < b) != desc       # noqa: E731
        if self.pin != other.pin:
            return fixed(self.pin, other.pin)
        if self.pin:
            return fixed(self.sub, other.sub)
        a, b = self.parent or self, other.parent or other
        va, vb = a.value(), b.value()
        if (va[0] == 2) != (vb[0] == 2):           # blanks last
            return fixed(va[0] == 2, vb[0] == 2)
        if (va, a.row0) != (vb, b.row0):
            return (va, a.row0) < (vb, b.row0)
        return fixed(self.sub, other.sub)        # same group: parent first, then children


def make_sortable(t: QTableWidget, sorter: Optional[Callable[[int, bool], None]] = None,
                  columns: Optional[Iterable[int]] = None):
    """Sort ``t`` by a column when its header is clicked, alternating the direction.

    ``sorter(column, descending)``, if given, does the sorting instead (for a table of
    editors, which rebuilds its rows); ``columns`` limits the headers that sort."""
    header = t.horizontalHeader()
    header.setSectionsClickable(True)
    header.setSortIndicatorShown(True)
    reset_sort(t)
    allowed = None if columns is None else set(columns)

    # The direction is kept here, not read from the indicator: Qt flips the indicator
    # itself on a click, before sectionClicked.
    def clicked(col):
        last, desc = header.property("kipi_sorted"), bool(header.property("kipi_desc"))
        if allowed is not None and col not in allowed:
            header.setSortIndicator(last, Qt.DescendingOrder if desc else Qt.AscendingOrder)
            return
        desc = last == col and not desc
        order = Qt.DescendingOrder if desc else Qt.AscendingOrder
        header.setSortIndicator(col, order)
        header.setProperty("kipi_sorted", col)
        header.setProperty("kipi_desc", desc)
        if sorter is not None:
            sorter(col, order == Qt.DescendingOrder)
        else:
            t.sortItems(col, order)
    header.sectionClicked.connect(clicked)


def resort(t: QTableWidget):
    """Sort a refilled table again by the column and direction last clicked, if any."""
    header = t.horizontalHeader()
    col = header.property("kipi_sorted")
    if col is not None and col >= 0:
        order = Qt.DescendingOrder if header.property("kipi_desc") else Qt.AscendingOrder
        header.setSortIndicator(col, order)
        t.sortItems(col, order)


def sorted_rows(rows, col: int, descending: bool, keys=None):
    """``rows`` (lists of cell values) ordered by column ``col`` for a sorter: by
    ``keys[col](value)`` if given, else by :func:`sort_key` of the text; blanks last."""
    def key(row):
        v = row[col]
        if keys is not None and col in keys:
            return (0, keys[col](v))
        return sort_key(str(v))
    filled = [r for r in rows if key(r)[0] != 2]
    blank = [r for r in rows if key(r)[0] == 2]
    return sorted(filled, key=key, reverse=descending) + blank


def reset_sort(t: QTableWidget):
    """Forget the sort column, e.g. when the table is refilled in its natural order."""
    header = t.horizontalHeader()
    header.setSortIndicator(-1, Qt.AscendingOrder)
    header.setProperty("kipi_sorted", -1)
    header.setProperty("kipi_desc", False)


def set_row(t: QTableWidget, r: int, cells, parent_row: Optional[int] = None, sub: int = 0,
            pin: int = 0):
    """Fill row ``r`` with ``cells`` (text, or (text, sort key)). Rows that sort with a
    parent pass its ``parent_row`` (already filled) and their order ``sub`` under it;
    pinned rows pass ``pin`` > 0 and their order ``sub`` among pinned rows."""
    for c in range(t.columnCount()):
        cell = cells[c] if c < len(cells) else ""
        text, key = cell if isinstance(cell, tuple) else (cell, None)
        it = SortItem(text, key)
        it.row0, it.sub, it.pin = r, sub, pin
        if parent_row is not None:
            it.parent = t.item(parent_row, c)
        t.setItem(r, c, it)

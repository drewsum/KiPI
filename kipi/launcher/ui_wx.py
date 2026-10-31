"""The setup screen as a wxPython window, for Pythons with wx but no Tk (#7).

KiCad's own Python on Windows has no tkinter but ships wxPython (KiCad is built on it),
and KiCad makes plugin environments that see it (``--system-site-packages``); Linux
distributions' KiCad packages bring python3-wxgtk too. So the plugin's first start shows
this, the same pages as :mod:`kipi.launcher.ui_tk`.

wx runs on the main thread; the install runs on a worker thread and hands its lines and
progress over with ``wx.CallAfter``.
"""

from __future__ import annotations

import sys
import threading
from typing import Optional

import wx

from kipi.launcher import deps
from kipi.launcher.install import Canceled
from kipi.launcher.paths import KIPI_ROOT
from kipi.launcher.setup_env import PARDISO_CONS, PARDISO_PROS, PARDISO_TITLE, Summary
from kipi.launcher.state import State

PAD = 10
WRAP = 600


def available() -> bool:
    """Whether a wx window can open here: wx imported (this module did) and a display."""
    if sys.platform.startswith("linux"):
        import os
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def _icon_bundle() -> Optional["wx.IconBundle"]:
    res = KIPI_ROOT / "resources"
    bundle = wx.IconBundle()
    if sys.platform == "win32" and (res / "kipi.ico").is_file():
        bundle.AddIcon(str(res / "kipi.ico"), wx.BITMAP_TYPE_ICO)
    for name in ("kipi_48.png", "kipi_256.png"):
        if (res / name).is_file():
            bundle.AddIcon(str(res / name), wx.BITMAP_TYPE_PNG)
    return bundle if not bundle.IsEmpty() else None


class WxUI:
    def __init__(self):
        if sys.platform == "win32":
            try:            # sharp on a scaled display; KiPI's taskbar button (#127)
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
                from kipi.launcher.appid import set_process_app_id
                set_process_app_id()
            except Exception:  # noqa: BLE001 - cosmetic
                pass
        self.app = wx.App.Get() or wx.App(False)
        self.app.SetAppName("KiPI")
        self._icons = _icon_bundle()
        self._status: Optional[wx.Frame] = None

    # --- plumbing -----------------------------------------------------------------------
    def _dress(self, win: wx.TopLevelWindow) -> None:
        if self._icons is not None:
            win.SetIcons(self._icons)

    def _drop_status(self) -> None:
        if self._status is not None:
            self._status.Destroy()
            self._status = None
            wx.Yield()

    @staticmethod
    def _heading(parent, text: str, size: int = 2) -> wx.StaticText:
        st = wx.StaticText(parent, label=text)
        font = st.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        font.SetPointSize(font.GetPointSize() + size)
        st.SetFont(font)
        return st

    @staticmethod
    def _text(parent, text: str, wrap: int = WRAP, grey: bool = False) -> wx.StaticText:
        st = wx.StaticText(parent, label=text)
        st.Wrap(parent.FromDIP(wrap))
        if grey:
            st.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        return st

    # --- the interface the launcher uses ---------------------------------------------------
    def status(self, text: str) -> None:
        self._drop_status()
        f = wx.Frame(None, title="KiPI setup",
                     style=wx.CAPTION | wx.SYSTEM_MENU | wx.MINIMIZE_BOX)
        self._dress(f)
        panel = wx.Panel(f)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self._heading(panel, "Setting up KiPI"), 0, wx.BOTTOM, PAD)
        sizer.Add(self._text(panel, text))
        gauge = wx.Gauge(panel, range=100, size=panel.FromDIP(wx.Size(WRAP, -1)))
        gauge.Pulse()
        sizer.Add(gauge, 0, wx.EXPAND | wx.TOP, PAD)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(sizer, 1, wx.EXPAND | wx.ALL, PAD * 2)
        panel.SetSizer(outer)
        outer.Fit(f)
        f.Centre()
        f.Show()
        f.Raise()
        f.Update()
        wx.Yield()
        self._status = f

    def info(self, text: str) -> None:
        self._drop_status()
        wx.MessageBox(text, "KiPI", wx.OK | wx.ICON_INFORMATION)

    def error(self, text: str) -> None:
        self._drop_status()
        wx.MessageBox("KiPI could not start: " + text, "KiPI", wx.OK | wx.ICON_ERROR)

    def close(self) -> None:
        self._drop_status()

    def confirm(self, s: Summary) -> Optional[bool]:
        self._drop_status()
        dlg = wx.Dialog(None, title="KiPI setup",
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._dress(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self._heading(dlg, "Set up KiPI"), 0, wx.BOTTOM, PAD)
        sizer.Add(self._text(dlg, f"{s.intro_text()} Nothing is installed until you click "
                                  "Install."))
        # Paths don't wrap: one line each, shortened in the middle, the whole in a tooltip.
        for i, line in enumerate((f"Location: {s.env}",
                                  f"Python {s.python.version_text}: {s.python.executable}")):
            st = wx.StaticText(dlg, label=line, size=dlg.FromDIP(wx.Size(WRAP, -1)),
                               style=wx.ST_ELLIPSIZE_MIDDLE)
            st.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
            st.SetToolTip(line)
            sizer.Add(st, 0, wx.EXPAND | (wx.TOP if i == 0 else 0), 4)

        source = s.source_line()
        sizer.Add(self._heading(dlg, "Packages to install" + (f" ({source})" if source else ""),
                                0), 0, wx.TOP, PAD)
        table = wx.ListCtrl(dlg, style=wx.LC_REPORT | wx.LC_HRULES | wx.LC_SINGLE_SEL,
                            size=dlg.FromDIP(wx.Size(WRAP, 220)))
        table.InsertColumn(0, "Package", width=dlg.FromDIP(280))
        table.InsertColumn(1, "Version", width=dlg.FromDIP(170))
        table.InsertColumn(2, "Download", wx.LIST_FORMAT_RIGHT, width=dlg.FromDIP(120))
        for i, p in enumerate(sorted(s.plan.packages, key=lambda p: -(p.size or 0))):
            table.InsertItem(i, p.name)
            table.SetItem(i, 1, p.version)
            table.SetItem(i, 2, deps.fmt_size(p.size) if p.size else "")
        sizer.Add(table, 1, wx.EXPAND | wx.TOP, 2)
        total = self._heading(dlg, "", 0)
        sizer.Add(total, 0, wx.TOP, 4)

        box = wx.StaticBoxSizer(wx.VERTICAL, dlg, PARDISO_TITLE)
        pane = box.GetStaticBox()
        tick = None
        if s.pardiso_offered:
            tick = wx.CheckBox(pane, label=f"Install PARDISO ({s.pardiso_line()})")
            tick.SetValue(s.pardiso_default)
            box.Add(tick, 0, wx.ALL, 4)
            for mark, items in (("+", PARDISO_PROS), ("−", PARDISO_CONS)):
                for item in items:
                    box.Add(self._text(pane, f"{mark}  {item}", WRAP - 40), 0,
                            wx.LEFT | wx.RIGHT, 24)
        else:
            box.Add(self._text(pane, f"Not available here: {s.pardiso_why_not}. KiPI uses "
                                     "SciPy's solver, which handles every board.", WRAP - 20),
                    0, wx.ALL, 4)
        sizer.Add(box, 0, wx.EXPAND | wx.TOP, PAD)

        def update(_evt=None):
            with_p = bool(tick and tick.GetValue())
            total.SetLabel(("With PARDISO: " if with_p else "Total: ") + s.size_line(with_p))
            dlg.Layout()

        if tick is not None:
            tick.Bind(wx.EVT_CHECKBOX, update)
        buttons = wx.StdDialogButtonSizer()
        install = wx.Button(dlg, wx.ID_OK, "Install")
        install.SetDefault()
        buttons.AddButton(install)
        buttons.AddButton(wx.Button(dlg, wx.ID_CANCEL, "Cancel"))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.TOP, PAD)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(sizer, 1, wx.EXPAND | wx.ALL, PAD * 2)
        dlg.SetSizer(outer)
        update()
        outer.Fit(dlg)
        dlg.Centre()
        install.SetFocus()
        try:
            if dlg.ShowModal() != wx.ID_OK:
                return None
            return bool(tick and tick.GetValue())
        finally:
            dlg.Destroy()

    def install(self, task) -> State:
        """Run ``task(on_line, on_fraction, register)`` on a worker thread while the window
        shows its progress; Cancel (or closing the window) stops it."""
        self._drop_status()
        dlg = wx.Dialog(None, title="KiPI setup",
                        style=wx.CAPTION | wx.SYSTEM_MENU | wx.RESIZE_BORDER)
        self._dress(dlg)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self._heading(dlg, "Installing KiPI's packages"), 0, wx.BOTTOM, PAD)
        sizer.Add(self._text(dlg, "This takes a few minutes the first time. KiPI starts when "
                                  "it is done."))
        gauge = wx.Gauge(dlg, range=1000, size=dlg.FromDIP(wx.Size(WRAP, -1)))
        sizer.Add(gauge, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, PAD)
        current = wx.StaticText(dlg, label="", style=wx.ST_ELLIPSIZE_END)
        current.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        sizer.Add(current, 0, wx.EXPAND)
        details = wx.TextCtrl(dlg, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL,
                              size=dlg.FromDIP(wx.Size(WRAP, 200)))
        details.SetFont(wx.Font(wx.FontInfo(8).Family(wx.FONTFAMILY_TELETYPE)))
        details.Hide()
        sizer.Add(details, 1, wx.EXPAND | wx.TOP, 4)
        row = wx.BoxSizer(wx.HORIZONTAL)
        toggle = wx.Button(dlg, label="Details")
        cancel_btn = wx.Button(dlg, wx.ID_CANCEL, "Cancel")
        row.Add(toggle)
        row.AddStretchSpacer()
        row.Add(cancel_btn)
        sizer.Add(row, 0, wx.EXPAND | wx.TOP, PAD)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(sizer, 1, wx.EXPAND | wx.ALL, PAD * 2)
        dlg.SetSizer(outer)
        outer.Fit(dlg)
        dlg.Centre()

        installers = []
        outcome = {}
        pulse = wx.Timer(dlg)
        dlg.Bind(wx.EVT_TIMER, lambda e: gauge.Pulse(), pulse)

        def on_toggle(_evt):
            details.Show(not details.IsShown())
            outer.Fit(dlg)

        def cancel(_evt=None):
            cancel_btn.Disable()
            cancel_btn.SetLabel("Canceling…")
            outcome.setdefault("canceled", True)
            for inst in list(installers):
                inst.cancel()

        toggle.Bind(wx.EVT_BUTTON, on_toggle)
        cancel_btn.Bind(wx.EVT_BUTTON, cancel)
        dlg.Bind(wx.EVT_CLOSE, cancel)
        dlg.SetEscapeId(wx.ID_NONE)         # Escape must not end the dialog mid-install

        def register(inst):
            installers.append(inst)
            if outcome.get("canceled"):     # Cancel came before this installer started
                inst.cancel()

        def show_line(text):
            current.SetLabel(text[:110])
            details.AppendText(text + "\n")

        def show_fraction(f):
            if f is None:
                if not pulse.IsRunning():
                    pulse.Start(80)
            else:
                pulse.Stop()
                gauge.SetValue(int(f * 1000))

        def work():
            try:
                outcome["result"] = task(lambda line: wx.CallAfter(show_line, line),
                                         lambda f: wx.CallAfter(show_fraction, f), register)
            except BaseException as e:  # noqa: BLE001 - handed to the UI thread
                outcome["error"] = e
            wx.CallAfter(lambda: dlg.IsModal() and dlg.EndModal(wx.ID_OK))

        pulse.Start(80)
        threading.Thread(target=work, name="kipi-setup", daemon=True).start()
        dlg.ShowModal()
        pulse.Stop()
        dlg.Destroy()
        if "error" in outcome:
            err = outcome["error"]
            if outcome.get("canceled") and not isinstance(err, Canceled):
                raise Canceled() from err
            raise err
        return outcome["result"]

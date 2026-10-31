"""The setup screen as a window (tkinter, part of the standard library on Windows and
macOS, and on Linux where python3-tk is installed).

Tk runs on the main thread; the install runs on a worker thread and hands its lines and
progress over through a queue the window polls.
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from kipi.launcher import deps
from kipi.launcher.install import Canceled
from kipi.launcher.paths import KIPI_ROOT
from kipi.launcher.setup_env import PARDISO_CONS, PARDISO_PROS, PARDISO_TITLE, Summary
from kipi.launcher.state import State

PAD = 10
WRAP = 600


def _sharp_on_high_dpi() -> None:
    """Without this Windows draws the window blurry on a scaled display."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001 - cosmetic
            pass


def _kipi_on_taskbar() -> None:
    """Windows: KiPI's taskbar button and icon for this window, not Python's (#127)."""
    if sys.platform == "win32":
        try:
            from kipi.launcher.appid import set_process_app_id
            set_process_app_id()
        except Exception:  # noqa: BLE001 - cosmetic
            pass


class TkUI:
    def __init__(self):
        _sharp_on_high_dpi()
        _kipi_on_taskbar()
        # WM_CLASS ("kipi", "Kipi") names the window for Linux panels, which match it to
        # kipi.desktop by name. Raises TclError with no display: the caller falls back.
        self.root = tk.Tk(baseName="kipi", className="KiPI")
        self.root.title("KiPI setup")
        self.root.minsize(560, 0)
        self._set_icon()
        self._tagged = False
        self.root.protocol("WM_DELETE_WINDOW", self._close_clicked)
        self._on_close = self.root.quit
        self.frame: Optional[ttk.Frame] = None
        self.root.withdraw()

    # --- window plumbing ----------------------------------------------------------------
    def _set_icon(self) -> None:
        """The KiPI icon in the title bar, taskbar, panel or Dock (macOS takes it from
        here too). Windows gets the .ico, with sizes made for it."""
        res = KIPI_ROOT / "resources"
        try:
            self._icons = [tk.PhotoImage(file=str(res / n))
                           for n in ("kipi_256.png", "kipi_48.png") if (res / n).is_file()]
            if self._icons:
                self.root.iconphoto(True, *self._icons)
        except (tk.TclError, OSError):
            pass
        if sys.platform == "win32" and (res / "kipi.ico").is_file():
            try:
                self.root.iconbitmap(default=str(res / "kipi.ico"))
            except tk.TclError:
                pass

    def _tag_taskbar_button(self) -> None:
        """Windows: pinning the setup window's button pins the launcher, as KiPI's does."""
        self._tagged = True
        if sys.platform != "win32":
            return
        try:
            from kipi.launcher.appid import tag_window
            from kipi.launcher.shortcut import windows_target
            target, args = windows_target(KIPI_ROOT)
            tag_window(int(self.root.wm_frame(), 16), f'"{target}" {args}', "KiPI",
                       KIPI_ROOT / "resources" / "kipi.ico")
        except Exception:  # noqa: BLE001 - cosmetic
            pass

    def _page(self) -> ttk.Frame:
        """A fresh, empty page in the window."""
        if self.frame is not None:
            self.frame.destroy()
        self.frame = ttk.Frame(self.root, padding=PAD)
        self.frame.pack(fill="both", expand=True)
        return self.frame

    def _show(self) -> None:
        self.root.deiconify()
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.root.lift()
        self.root.update()
        if not self._tagged:
            self._tag_taskbar_button()

    def _close_clicked(self) -> None:
        self._on_close()

    def _label(self, parent, text, **kw) -> ttk.Label:
        lb = ttk.Label(parent, text=text, wraplength=WRAP, justify="left", **kw)
        lb.pack(anchor="w", fill="x")
        return lb

    # --- the interface the launcher uses ---------------------------------------------------
    def status(self, text: str) -> None:
        page = self._page()
        ttk.Label(page, text="Setting up KiPI", font=("TkDefaultFont", 12, "bold")).pack(
            anchor="w")
        self._label(page, text)
        bar = ttk.Progressbar(page, mode="indeterminate", length=WRAP)
        bar.pack(fill="x", pady=(PAD, 0))
        bar.start(15)
        self._show()

    def info(self, text: str) -> None:
        messagebox.showinfo("KiPI", text, parent=self.root)

    def error(self, text: str) -> None:
        self.root.withdraw()
        messagebox.showerror("KiPI", "KiPI could not start: " + text, parent=self.root)

    def close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def confirm(self, s: Summary) -> Optional[bool]:
        page = self._page()
        answer = {"value": None, "done": False}
        ttk.Label(page, text="Set up KiPI", font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        self._label(page, f"{s.intro_text()} Nothing is installed until you click Install.")
        self._label(page, f"Location: {s.env}\nPython {s.python.version_text}: "
                          f"{s.python.executable}", foreground="gray25")

        # Every package, biggest first.
        source = s.source_line()
        ttk.Label(page, text="Packages to install" + (f" ({source})" if source else ""),
                  font=("TkDefaultFont", 9, "bold"), wraplength=WRAP, justify="left").pack(
            anchor="w", pady=(PAD, 2))
        box = ttk.Frame(page)
        box.pack(fill="both", expand=True)
        table = ttk.Treeview(box, columns=("version", "size"), height=10, selectmode="none")
        table.heading("#0", text="Package", anchor="w")
        table.heading("version", text="Version", anchor="w")
        table.heading("size", text="Download", anchor="e")
        table.column("#0", width=260)
        table.column("version", width=160)
        table.column("size", width=110, anchor="e")
        scroll = ttk.Scrollbar(box, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for p in sorted(s.plan.packages, key=lambda p: -(p.size or 0)):
            table.insert("", "end", text=p.name,
                         values=(p.version, deps.fmt_size(p.size) if p.size else ""))
        total = ttk.Label(page, text="", font=("TkDefaultFont", 9, "bold"))
        total.pack(anchor="w", pady=(4, 0))

        # PARDISO, if it can be had here.
        pardiso = tk.BooleanVar(value=s.pardiso_offered and s.pardiso_default)
        group = ttk.LabelFrame(page, text=PARDISO_TITLE, padding=PAD)
        group.pack(fill="x", pady=(PAD, 0))
        if s.pardiso_offered:
            ttk.Checkbutton(group, text=f"Install PARDISO ({s.pardiso_line()})",
                            variable=pardiso, command=lambda: update()).pack(anchor="w")
            for mark, items in (("+", PARDISO_PROS), ("−", PARDISO_CONS)):
                for item in items:
                    ttk.Label(group, text=f"{mark}  {item}", wraplength=WRAP - 30,
                              justify="left").pack(anchor="w", padx=(20, 0))
        else:
            ttk.Label(group, text=f"Not available here: {s.pardiso_why_not}. KiPI uses "
                                  "SciPy's solver, which handles every board.",
                      wraplength=WRAP - 20, justify="left").pack(anchor="w")

        def update():
            with_p = pardiso.get()
            total.configure(text=("With PARDISO: " if with_p else "Total: ")
                            + s.size_line(with_p))

        def done(value):
            answer["value"], answer["done"] = value, True
            self.root.quit()

        buttons = ttk.Frame(page)
        buttons.pack(fill="x", pady=(PAD, 0))
        ttk.Button(buttons, text="Cancel", command=lambda: done(None)).pack(side="right")
        install = ttk.Button(buttons, text="Install", command=lambda: done(pardiso.get()))
        install.pack(side="right", padx=(0, 6))
        install.focus_set()
        self.root.bind("<Return>", lambda e: done(pardiso.get()))
        self.root.bind("<Escape>", lambda e: done(None))
        update()
        self._on_close = lambda: done(None)
        self._show()
        # A click (or Return) handled while the window was drawn has answered already. Its
        # quit() came before the loop, which would then never end (#115).
        if not answer["done"]:
            self.root.mainloop()
        self.root.unbind("<Return>")
        self.root.unbind("<Escape>")
        self._on_close = self.root.quit
        return answer["value"]

    def install(self, task) -> State:
        """Run ``task(on_line, on_fraction, register)`` on a worker thread while the window
        shows its progress; Cancel (or closing the window) stops it."""
        page = self._page()
        ttk.Label(page, text="Installing KiPI's packages",
                  font=("TkDefaultFont", 12, "bold")).pack(anchor="w")
        self._label(page, "This takes a few minutes the first time. KiPI starts when it is "
                          "done.")
        bar = ttk.Progressbar(page, mode="indeterminate", length=WRAP, maximum=1000)
        bar.pack(fill="x", pady=(PAD, 4))
        current = ttk.Label(page, text="", foreground="gray25", width=90)
        current.pack(anchor="w")
        details = tk.Text(page, height=12, width=90, wrap="none", font=("TkFixedFont", 8))
        shown = {"details": False}

        def toggle():
            shown["details"] = not shown["details"]
            if shown["details"]:
                details.pack(fill="both", expand=True, pady=(4, 0))
            else:
                details.pack_forget()

        buttons = ttk.Frame(page)
        buttons.pack(side="bottom", fill="x", pady=(PAD, 0))
        cancel_btn = ttk.Button(buttons, text="Cancel")
        cancel_btn.pack(side="right")
        ttk.Button(buttons, text="Details", command=toggle).pack(side="left")

        events: "queue.Queue" = queue.Queue()
        installers = []
        outcome = {}

        def cancel():
            cancel_btn.configure(state="disabled", text="Canceling…")
            for inst in list(installers):
                inst.cancel()
            outcome.setdefault("canceled", True)

        cancel_btn.configure(command=cancel)
        self._on_close = cancel

        def register(inst):
            installers.append(inst)
            if outcome.get("canceled"):    # Cancel came before this installer started
                inst.cancel()

        def work():
            try:
                outcome["result"] = task(lambda line: events.put(("line", line)),
                                         lambda f: events.put(("fraction", f)), register)
            except BaseException as e:  # noqa: BLE001 - handed to the UI thread
                outcome["error"] = e
            events.put(("done", None))

        def poll():
            try:
                while True:
                    kind, value = events.get_nowait()
                    if kind == "line":
                        current.configure(text=value[:110])
                        details.insert("end", value + "\n")
                        details.see("end")
                    elif kind == "fraction":
                        if value is None:
                            bar.configure(mode="indeterminate")
                            bar.start(15)
                        else:
                            bar.stop()
                            bar.configure(mode="determinate", value=value * 1000)
                    elif kind == "done":
                        self.root.quit()
                        return
            except queue.Empty:
                pass
            self.root.after(100, poll)

        bar.start(15)
        self._show()
        threading.Thread(target=work, name="kipi-setup", daemon=True).start()
        self.root.after(100, poll)
        self.root.mainloop()
        self._on_close = self.root.quit
        if "error" in outcome:
            err = outcome["error"]
            if outcome.get("canceled") and not isinstance(err, Canceled):
                raise Canceled() from err
            raise err
        return outcome["result"]

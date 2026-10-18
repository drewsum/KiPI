"""KiCad IPC plugin action entry point (see plugin.json).

KiCad starts this in the plugin's own venv with KICAD_API_SOCKET / KICAD_API_TOKEN
set, so kipy connects to the instance that launched us.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    from kipi.gui import app as kipi_app
    from kipi.gui.app import run, setup_logging
    from kipi.sources.kicad_ipc.extract import ExtractionError, connect
    from kipi.sources.kicad_ipc.source import InstalledKiCad, KiCadLiveSource
    from kipi.core.progress import ProgressReporter

    setup_logging(console=False)
    # KiCad captures plugin stdout/stderr through a pipe it does not always drain; a full
    # pipe blocks the writer forever. Send everything to this session's log folder.
    kipi_app.detach_std_streams()
    os.environ.setdefault("QT_API", "pyside6")
    from kipi.host import Host, set_current
    set_current(Host.PLUGIN)
    from kipi.gui import single_instance
    instance = single_instance.claim()
    if instance is None:        # another KiPI is open; it was brought to the front
        return 0
    try:
        # The welcome screen first, so the click in KiCad is answered at once (#55).
        _app, splash = kipi_app.start_splash()
        # Before anything loads MKL: PARDISO is not in requirements.txt, so KiPI offers
        # it here, with an opt-out, rather than KiCad installing it unasked (#129).
        from kipi.gui import pardiso_setup
        pardiso_setup.startup(splash)
        kipi_app.set_status(splash, "Connecting to KiCad…")
        try:
            kicad, board = connect(progress=ProgressReporter())
        except ExtractionError as e:
            from qtpy.QtWidgets import QMessageBox
            if splash is not None:
                splash.close()
            QMessageBox.critical(None, "KiPI", f"{e}\n\nLog: {kipi_app.LOG_PATH}")
            return 1
        source = KiCadLiveSource(kicad, board)
        return run(source=source, tool=InstalledKiCad(source), instance=instance,
                   splash=splash)
    finally:
        instance.release()


if __name__ == "__main__":
    sys.exit(main())

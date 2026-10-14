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
    from kipi.core.kicad_io.extract import ExtractionError, connect
    from kipi.core.progress import ProgressReporter

    setup_logging(console=False)
    # KiCad captures plugin stdout/stderr through a pipe it does not always drain; a full
    # pipe blocks the writer forever. Send everything to this session's log folder.
    kipi_app.detach_std_streams()
    try:
        kicad, board = connect(progress=ProgressReporter())
    except ExtractionError as e:
        os.environ.setdefault("QT_API", "pyside6")
        from qtpy.QtWidgets import QMessageBox

        from kipi.gui.identity import make_app
        _app = make_app()  # must outlive the box
        QMessageBox.critical(None, "KiPI", f"{e}\n\nLog: {kipi_app.LOG_PATH}")
        return 1
    return run(kicad=kicad, board=board)


if __name__ == "__main__":
    sys.exit(main())

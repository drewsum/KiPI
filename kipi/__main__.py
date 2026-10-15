"""Standalone entry point.

    python -m kipi                      # connect to the running KiCad PCB editor
    python -m kipi --snapshot b.json.gz # open a saved snapshot, no KiCad needed
    python -m kipi --dump out.json.gz   # extract from KiCad and save, no GUI
"""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kipi", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", help="open a saved board snapshot instead of connecting")
    ap.add_argument("--dump", metavar="PATH", help="extract from KiCad, save snapshot, exit")
    ap.add_argument("--refill", action="store_true", help="refill zones before extracting")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    from kipi.gui.app import setup_logging
    setup_logging(logging.DEBUG if args.debug else logging.INFO)

    if args.dump:
        from kipi.core.kicad_io.extract import connect, extract_board
        from kipi.core.kicad_io.snapshot import save
        from kipi.core.progress import ProgressReporter
        prog = ProgressReporter()
        kicad, board = connect(progress=prog)
        snap = extract_board(board, refill_zones=args.refill, progress=prog)
        snap.kicad_version = str(kicad.get_version())
        save(snap, args.dump)
        print(f"Saved {args.dump}")
        return 0

    from kipi.gui import single_instance
    instance = single_instance.claim()
    if instance is None:
        print("KiPI is already running", file=sys.stderr)
        return 0
    try:
        if args.snapshot:
            from kipi.gui.app import run
            from kipi.core.kicad_io.snapshot import load
            return run(snapshot=load(args.snapshot), instance=instance)
        return _run_live(instance)
    finally:
        instance.release()


def _run_live(instance) -> int:
    from kipi.gui.app import run
    from kipi.core.kicad_io.extract import ExtractionError, connect
    from kipi.core.progress import ProgressReporter
    try:
        kicad, board = connect(progress=ProgressReporter())
    except ExtractionError as e:
        print(e, file=sys.stderr)
        return 1
    return run(kicad=kicad, board=board, instance=instance)


if __name__ == "__main__":
    sys.exit(main())

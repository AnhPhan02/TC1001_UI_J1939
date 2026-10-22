from __future__ import annotations

import argparse
import sys

from PySide6.QtWidgets import QApplication

from j1939_control.ui.main_window import MainWindow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="J1939 P3-A 40-SPN control UI")
    parser.add_argument(
        "--backend",
        choices=("mock", "tsmaster"),
        default="mock",
        help="Communication backend. Start with mock until the native TSMaster API is ready.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("J1939 Control App")
    window = MainWindow(initial_backend=args.backend)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

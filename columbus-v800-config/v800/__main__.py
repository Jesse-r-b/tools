"""Entry point: ``python -m v800``."""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .ui.main_window import APP_NAME, MainWindow


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("columbus-v800-config")

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

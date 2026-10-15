"""PySide6 evidence workbench entry point."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from sourcebridge.reporting import diagnostic_groups, diagnostic_occurrences


def main() -> int:
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (
            QApplication,
            QHBoxLayout,
            QLabel,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QVBoxLayout,
            QWidget,
        )
    except ImportError:
        print(
            "The workbench requires the optional dependency: pip install 'sourcebridge[workbench]'",
            file=sys.stderr,
        )
        return 1

    if len(sys.argv) != 2:
        print("usage: sourcebridge-workbench EVIDENCE_DATABASE", file=sys.stderr)
        return 2
    database = Path(sys.argv[1])
    try:
        groups = diagnostic_groups(database)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1

    application = QApplication(sys.argv)
    window = QMainWindow()
    window.setWindowTitle("Sourcebridge workbench")
    central = QWidget()
    layout = QHBoxLayout(central)
    groups_list = QListWidget()
    details = QPlainTextEdit()
    details.setReadOnly(True)
    status = QLabel(f"{len(groups)} diagnostic group(s)")
    activity = QPushButton("Execution activity")
    activity.setToolTip("Execution adapters populate durable worker activity records.")

    for group in groups:
        item = QListWidgetItem(
            f"{group['occurrence_count']:>5}  {group['failure_signature']}"
        )
        item.setData(Qt.ItemDataRole.UserRole, group["failure_signature"])
        groups_list.addItem(item)

    def show_group(item: QListWidgetItem) -> None:
        signature = item.data(Qt.ItemDataRole.UserRole)
        details.setPlainText(
            json.dumps(diagnostic_occurrences(database, signature), indent=2, sort_keys=True)
        )

    def show_activity() -> None:
        QMessageBox.information(
            window,
            "Execution activity",
            "Execution adapters write run, worker, and event records. "
            "No approved adapter is installed in this engine-only package.",
        )

    groups_list.itemSelectionChanged.connect(
        lambda: show_group(groups_list.currentItem()) if groups_list.currentItem() else None
    )
    activity.clicked.connect(show_activity)
    left = QVBoxLayout()
    left.addWidget(status)
    left.addWidget(groups_list)
    left.addWidget(activity)
    layout.addLayout(left, 1)
    layout.addWidget(details, 3)
    window.setCentralWidget(central)
    window.resize(1200, 720)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())

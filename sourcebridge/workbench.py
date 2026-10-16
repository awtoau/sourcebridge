"""PySide6 evidence workbench entry point."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from sourcebridge.reporting import diagnostic_groups, diagnostic_occurrences, execution_activity


def main() -> int:
    try:
        from PySide6.QtCore import QProcess, Qt, QTimer
        from PySide6.QtWidgets import (
            QApplication,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
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
    run_id = QLineEdit()
    run_id.setPlaceholderText("Queued execution run ID")
    start = QPushButton("Start queued run")
    process = QProcess(window)

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
        details.setPlainText(json.dumps(execution_activity(database), indent=2, sort_keys=True))
        status.setText("Execution runs, workers, and events")

    def start_run() -> None:
        selected_run = run_id.text().strip()
        if not selected_run:
            status.setText("Enter a queued execution run ID before starting.")
            return
        start.setEnabled(False)
        status.setText(f"Starting approved run {selected_run}...")
        process.start(
            sys.executable,
            [
                "-m",
                "sourcebridge.cli",
                "run-approved",
                "--database",
                str(database),
                selected_run,
            ],
        )

    def completed_run() -> None:
        start.setEnabled(True)
        show_activity()
        status.setText(
            "Run completed."
            if process.exitCode() == 0
            else process.readAllStandardError().data().decode().strip() or "Run failed."
        )

    groups_list.itemSelectionChanged.connect(
        lambda: show_group(groups_list.currentItem()) if groups_list.currentItem() else None
    )
    activity.clicked.connect(show_activity)
    start.clicked.connect(start_run)
    process.finished.connect(completed_run)
    refresh = QTimer(window)
    refresh.setInterval(1_000)
    refresh.timeout.connect(show_activity)
    refresh.start()
    left = QVBoxLayout()
    left.addWidget(status)
    left.addWidget(groups_list)
    left.addWidget(activity)
    left.addWidget(run_id)
    left.addWidget(start)
    layout.addLayout(left, 1)
    layout.addWidget(details, 3)
    window.setCentralWidget(central)
    window.resize(1200, 720)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())

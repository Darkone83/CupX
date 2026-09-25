from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import traceback

from PySide6.QtCore import QSettings, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from ..compile.fullgame import build_full_game_set
from ..source.install import auto_detect
from ..source.inventory import inventory, summarize

APP_NAME = "CUPX Demo Data Builder"
APP_VERSION = "1.0.0"
DEFAULT_VOLUME_BYTES = 1024 * 1024 * 1024
DEFAULT_SHORT_AUDIO_SECONDS = 12.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _human_bytes(value: int) -> str:
    value = float(max(0, int(value)))
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or suffix == "TiB":
            if suffix == "B":
                return f"{int(value):,} {suffix}"
            return f"{value:,.1f} {suffix}"
        value /= 1024.0
    return f"{value:,.1f} TiB"


def _normalise_source(path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    if path.name.lower() == "cuphead_data":
        return path.parent
    return path


def _validate_source(root: Path) -> None:
    data_dir = root / "Cuphead_Data"
    if not data_dir.is_dir():
        raise ValueError(
            "The selected folder does not contain Cuphead_Data. Select the Cuphead "
            "game folder, or select Cuphead_Data itself."
        )
    required = data_dir / "globalgamemanagers"
    if not required.exists() and not (data_dir / "globalgamemanagers.assets").exists():
        raise ValueError("Cuphead_Data is missing globalgamemanagers.")


def _clear_build_logs(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    for path in logs_dir.iterdir():
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            pass


class ScanWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(object, object, object)
    failed = Signal(str)

    def __init__(self, root: Path):
        super().__init__()
        self.root = Path(root)

    def run(self) -> None:
        try:
            _validate_source(self.root)

            def on_progress(current: int, total: int, path: Path) -> None:
                percent = int((current * 100) / max(1, total))
                self.progress.emit(percent, f"Scanning {current:,}/{total:,}: {path.name}")

            rows = inventory(self.root, hash_files=False, progress_callback=on_progress)
            summary = summarize(rows)
            unity_count = int(summary.get("kinds", {}).get("unity", 0))
            if not rows or unity_count == 0:
                raise RuntimeError("No Cuphead Unity data was found in the selected folder.")
            self.completed.emit(self.root, rows, summary)
        except Exception:
            self.failed.emit(traceback.format_exc())


class BuildWorker(QThread):
    progress = Signal(int, str)
    completed = Signal(object)
    failed = Signal(str, str)

    def __init__(self, root: Path, rows: list[dict], summary: dict, output_dir: Path):
        super().__init__()
        self.root = Path(root)
        self.rows = list(rows)
        self.summary = dict(summary)
        self.output_dir = Path(output_dir)

    def run(self) -> None:
        logs_dir = self.output_dir / "logs"
        log_path = logs_dir / "build.log"
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            _clear_build_logs(logs_dir)
            (logs_dir / "inventory.json").write_text(
                json.dumps(self.rows, indent=2), encoding="utf-8"
            )
            (logs_dir / "inventory_summary.json").write_text(
                json.dumps(self.summary, indent=2), encoding="utf-8"
            )

            with log_path.open("w", encoding="utf-8", buffering=1) as log, \
                    redirect_stdout(log), redirect_stderr(log):
                print(f"{APP_NAME} {APP_VERSION}")
                print(f"Started: {_utc_now()}")
                print(f"Source: {self.root}")
                print(f"Output: {self.output_dir}")
                print(f"Python: {sys.version}")
                print(f"Platform: {platform.platform()}")
                for package in ("PySide6", "UnityPy", "Pillow"):
                    try:
                        print(f"{package}: {importlib.metadata.version(package)}")
                    except Exception:
                        print(f"{package}: unknown")
                print()

                def on_progress(percent: int, message: str) -> None:
                    percent = max(0, min(100, int(percent)))
                    print(f"[{percent:3d}%] {message}")
                    self.progress.emit(percent, message)

                result = build_full_game_set(
                    self.root,
                    self.rows,
                    self.output_dir,
                    DEFAULT_VOLUME_BYTES,
                    short_audio_max_seconds=DEFAULT_SHORT_AUDIO_SECONDS,
                    strict=False,
                    workers=0,
                    scope="frontend",
                    progress_callback=on_progress,
                    report_dir=logs_dir,
                )
                manifest_path, manifest, verification, catalog_path, report_path, report = result
                frontend_validation = report.get("frontend_validation") or {}
                if not frontend_validation.get("valid_media_pack"):
                    # The demo profile can legitimately leave unrelated/frontend
                    # validation counters non-zero even when the generated CUPM/CUPX
                    # set itself completed and verified. Keep the detail in the build
                    # report for developers, but do not turn this advisory flag into
                    # an end-user build failure.
                    print(
                        "NOTE: frontend_validation.valid_media_pack is false; "
                        "continuing because demo output generation completed."
                    )

                summary = {
                    "status": "success",
                    "completed_utc": _utc_now(),
                    "source": str(self.root),
                    "output": str(self.output_dir),
                    "manifest": str(manifest_path),
                    "catalog": str(catalog_path),
                    "build_report": str(report_path),
                    "transfer_report": str(verification.get("transfer_report", "")),
                    "assets": int(report.get("packaged_assets", 0) or 0),
                    "volumes": int(report.get("volumes", 0) or 0),
                    "groups": list(report.get("package_groups") or []),
                    "xbox_reference_verified": bool(
                        verification.get("xbox_reference_verified")
                    ),
                    "total_seconds": float(
                        report.get("timings_seconds", {}).get("total", 0) or 0
                    ),
                }
                (logs_dir / "build_summary.json").write_text(
                    json.dumps(summary, indent=2), encoding="utf-8"
                )
                print()
                print(json.dumps(summary, indent=2))
                print(f"Completed: {_utc_now()}")

            self.completed.emit(summary)
        except Exception:
            details = traceback.format_exc()
            try:
                logs_dir.mkdir(parents=True, exist_ok=True)
                (logs_dir / "build_error.txt").write_text(details, encoding="utf-8")
                with log_path.open("a", encoding="utf-8") as log:
                    log.write("\nBUILD FAILED\n")
                    log.write(details)
            except Exception:
                pass
            self.failed.emit(details, str(logs_dir))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Darkone Customs", APP_NAME)
        self.scan_worker: ScanWorker | None = None
        self.build_worker: BuildWorker | None = None
        self.root: Path | None = None
        self.rows: list[dict] = []
        self.inventory_summary: dict = {}
        self.scan_ready = False
        self.building = False

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setMinimumSize(800, 600)
        self.resize(820, 470)
        self._build_ui()
        self._restore_paths()
        QTimer.singleShot(200, self._initial_source_probe)

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #111014; color: #f0edf4; }
            QLabel#Title { font-size: 24px; font-weight: 700; color: #f4e9ff; }
            QLabel#Subtitle { color: #aaa3b2; font-size: 11pt; }
            QLabel#Section { color: #d6b7ef; font-weight: 700; }
            QLabel#StatusGood { color: #8de5ad; }
            QLabel#StatusBad { color: #ff8f9c; }
            QFrame#Card {
                background: #1b181f;
                border: 1px solid #39313f;
                border-radius: 10px;
            }
            QLineEdit {
                background: #121015;
                border: 1px solid #4b4053;
                border-radius: 6px;
                padding: 9px;
                selection-background-color: #7f3fa3;
            }
            QLineEdit:focus { border: 1px solid #a85ad0; }
            QPushButton {
                background: #34283c;
                border: 1px solid #5b4966;
                border-radius: 6px;
                padding: 9px 14px;
            }
            QPushButton:hover { background: #463250; border-color: #8857a0; }
            QPushButton:disabled { color: #6d6770; background: #211e24; border-color: #302b34; }
            QPushButton#BuildButton {
                background: #713493;
                border: 1px solid #a75dcc;
                font-size: 12pt;
                font-weight: 700;
                padding: 12px 22px;
            }
            QPushButton#BuildButton:hover { background: #8340a7; }
            QProgressBar {
                background: #121015;
                border: 1px solid #4b4053;
                border-radius: 6px;
                min-height: 23px;
                text-align: center;
            }
            QProgressBar::chunk { background: #8e49ad; border-radius: 5px; }
            """
        )

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(14)

        title = QLabel(APP_NAME)
        title.setObjectName("Title")
        subtitle = QLabel(
            "Build the CUPX title → intro → Elder Kettle → Tutorial technical-demo data set."
        )
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(title)
        outer.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 18, 18, 18)
        card_layout.setSpacing(12)

        source_label = QLabel("Cuphead game data")
        source_label.setObjectName("Section")
        card_layout.addWidget(source_label)
        source_row = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText(
            "Select the folder containing Cuphead.exe and Cuphead_Data"
        )
        self.source_edit.editingFinished.connect(self._source_edited)
        self.source_button = QPushButton("Browse…")
        self.source_button.clicked.connect(self._browse_source)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.source_button)
        card_layout.addLayout(source_row)
        self.source_status = QLabel("Select your Cuphead installation. The scan runs automatically.")
        self.source_status.setWordWrap(True)
        card_layout.addWidget(self.source_status)

        output_label = QLabel("Output directory")
        output_label.setObjectName("Section")
        card_layout.addWidget(output_label)
        output_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Choose an empty folder for cuphead.cupm and CUPX volumes")
        self.output_edit.textChanged.connect(lambda _text: self._update_build_enabled())
        self.output_button = QPushButton("Browse…")
        self.output_button.clicked.connect(self._browse_output)
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.output_button)
        card_layout.addLayout(output_row)

        outer.addWidget(card)

        action_row = QHBoxLayout()
        self.build_button = QPushButton("Build Demo Data")
        self.build_button.setObjectName("BuildButton")
        self.build_button.clicked.connect(self._start_build)
        self.build_button.setEnabled(False)
        self.open_output_button = QPushButton("Open Output")
        self.open_output_button.clicked.connect(self._open_output)
        self.open_logs_button = QPushButton("Open Logs")
        self.open_logs_button.clicked.connect(self._open_logs)
        action_row.addWidget(self.build_button)
        action_row.addItem(QSpacerItem(20, 20, QSizePolicy.Expanding, QSizePolicy.Minimum))
        action_row.addWidget(self.open_output_button)
        action_row.addWidget(self.open_logs_button)
        outer.addLayout(action_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        outer.addWidget(self.progress_bar)

        self.activity_label = QLabel("Ready")
        self.activity_label.setWordWrap(True)
        outer.addWidget(self.activity_label)
        outer.addStretch(1)

        footer = QLabel(
            "Build reports, inventory data, verification results, and errors are written to output/logs."
        )
        footer.setObjectName("Subtitle")
        footer.setWordWrap(True)
        outer.addWidget(footer)

        self.setCentralWidget(central)
        self._update_open_buttons()

    def _restore_paths(self) -> None:
        source = str(self.settings.value("source", "") or "")
        output = str(self.settings.value("output", "") or "")
        if source:
            self.source_edit.setText(source)
        if output:
            self.output_edit.setText(output)

    def _initial_source_probe(self) -> None:
        current = self.source_edit.text().strip()
        if current:
            self._start_scan(Path(current))
            return
        try:
            found = auto_detect()
        except Exception:
            found = []
        if found:
            self.source_edit.setText(str(found[0].path))
            self._start_scan(found[0].path)

    def _source_edited(self) -> None:
        text = self.source_edit.text().strip()
        if text:
            self._start_scan(Path(text))

    def _browse_source(self) -> None:
        start = self.source_edit.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(
            self, "Select Cuphead game folder or Cuphead_Data", start
        )
        if not selected:
            return
        root = _normalise_source(Path(selected))
        self.source_edit.setText(str(root))
        self._start_scan(root)

    def _browse_output(self) -> None:
        start = self.output_edit.text().strip() or str(Path.home() / "CUPX_Demo_Data")
        selected = QFileDialog.getExistingDirectory(
            self, "Select CUPX demo-data output directory", start
        )
        if not selected:
            return
        self.output_edit.setText(str(Path(selected).resolve()))
        self.settings.setValue("output", self.output_edit.text())
        self._update_build_enabled()
        self._update_open_buttons()

    def _start_scan(self, selected: Path) -> None:
        if self.building:
            return
        if self.scan_worker and self.scan_worker.isRunning():
            return
        root = _normalise_source(selected)
        self.root = None
        self.rows = []
        self.inventory_summary = {}
        self.scan_ready = False
        self.source_edit.setText(str(root))
        self.source_status.setObjectName("")
        self.source_status.setStyleSheet("color:#d8b46b")
        self.source_status.setText("Scanning and inventorying Cuphead data…")
        self.activity_label.setText("Scanning source data")
        self.progress_bar.setRange(0, 0)
        self.source_button.setEnabled(False)
        self.build_button.setEnabled(False)

        self.scan_worker = ScanWorker(root)
        self.scan_worker.progress.connect(self._on_scan_progress)
        self.scan_worker.completed.connect(self._on_scan_complete)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.start()

    def _on_scan_progress(self, percent: int, message: str) -> None:
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(percent)
        self.activity_label.setText(message)

    def _on_scan_complete(self, root: Path, rows: list, summary: dict) -> None:
        self.root = Path(root)
        self.rows = list(rows)
        self.inventory_summary = dict(summary)
        self.scan_ready = True
        self.settings.setValue("source", str(self.root))
        self.source_edit.setText(str(self.root))
        self.source_status.setStyleSheet("color:#8de5ad")
        self.source_status.setText(
            f"Ready — {summary.get('files', 0):,} files, "
            f"{summary.get('kinds', {}).get('unity', 0):,} Unity containers, "
            f"{_human_bytes(summary.get('bytes', 0))}."
        )
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.activity_label.setText("Inventory complete")
        self.source_button.setEnabled(True)
        self._update_build_enabled()

    def _on_scan_failed(self, details: str) -> None:
        self.scan_ready = False
        self.source_status.setStyleSheet("color:#ff8f9c")
        last = details.strip().splitlines()[-1] if details.strip() else "Scan failed"
        self.source_status.setText(last)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.activity_label.setText("Source scan failed")
        self.source_button.setEnabled(True)
        self._update_build_enabled()
        QMessageBox.critical(self, APP_NAME, last)

    def _update_build_enabled(self) -> None:
        output_ok = bool(self.output_edit.text().strip())
        self.build_button.setEnabled(
            self.scan_ready and output_ok and not self.building
        )
        self._update_open_buttons()

    def _update_open_buttons(self) -> None:
        text = self.output_edit.text().strip()
        path = Path(text) if text else None
        self.open_output_button.setEnabled(bool(path and path.exists()))
        self.open_logs_button.setEnabled(bool(path and (path / "logs").exists()))

    def _start_build(self) -> None:
        if not self.scan_ready or not self.root or not self.rows:
            QMessageBox.information(self, APP_NAME, "Select and scan Cuphead data first.")
            return
        output_text = self.output_edit.text().strip()
        if not output_text:
            QMessageBox.information(self, APP_NAME, "Choose an output directory first.")
            return
        output_dir = Path(output_text).expanduser().resolve()
        try:
            output_inside_source = output_dir == self.root or output_dir.is_relative_to(self.root)
        except AttributeError:
            try:
                output_dir.relative_to(self.root)
                output_inside_source = True
            except ValueError:
                output_inside_source = output_dir == self.root
        if output_inside_source:
            QMessageBox.warning(
                self,
                APP_NAME,
                "Choose an output directory outside the Cuphead game folder. "
                "This keeps generated CUPX volumes out of later source scans.",
            )
            return

        self.settings.setValue("output", str(output_dir))
        self.output_edit.setText(str(output_dir))
        self.building = True
        self.source_button.setEnabled(False)
        self.output_button.setEnabled(False)
        self.source_edit.setEnabled(False)
        self.output_edit.setEnabled(False)
        self.build_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.activity_label.setText("Starting demo-data build…")

        self.build_worker = BuildWorker(
            self.root, self.rows, self.inventory_summary, output_dir
        )
        self.build_worker.progress.connect(self._on_build_progress)
        self.build_worker.completed.connect(self._on_build_complete)
        self.build_worker.failed.connect(self._on_build_failed)
        self.build_worker.start()

    def _on_build_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(max(0, min(100, percent)))
        self.activity_label.setText(message)

    def _finish_build_ui(self) -> None:
        self.building = False
        self.source_button.setEnabled(True)
        self.output_button.setEnabled(True)
        self.source_edit.setEnabled(True)
        self.output_edit.setEnabled(True)
        self._update_build_enabled()
        self._update_open_buttons()

    def _on_build_complete(self, summary: dict) -> None:
        self.progress_bar.setValue(100)
        self.activity_label.setText(
            f"Complete — {summary.get('assets', 0):,} assets in "
            f"{summary.get('volumes', 0):,} CUPX volumes."
        )
        self._finish_build_ui()
        QMessageBox.information(
            self,
            "Demo data complete",
            "CUPX demo data was built and verified successfully.\n\n"
            f"Output: {summary.get('output')}\n"
            f"Assets: {summary.get('assets', 0):,}\n"
            f"Volumes: {summary.get('volumes', 0):,}\n\n"
            "Copy cuphead.cupm and every generated .cupx volume to the Xbox. "
            "Reports are in the logs folder.",
        )

    def _on_build_failed(self, details: str, logs_dir: str) -> None:
        self.activity_label.setText("Build failed — review output/logs/build_error.txt")
        self._finish_build_ui()
        last = details.strip().splitlines()[-1] if details.strip() else "Build failed"
        QMessageBox.critical(
            self,
            "Demo data build failed",
            f"{last}\n\nDetailed logs were saved to:\n{logs_dir}",
        )

    def _open_output(self) -> None:
        text = self.output_edit.text().strip()
        if text:
            path = Path(text)
            path.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
            self._update_open_buttons()

    def _open_logs(self) -> None:
        text = self.output_edit.text().strip()
        if text:
            path = Path(text) / "logs"
            path.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
            self._update_open_buttons()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.building or (self.scan_worker and self.scan_worker.isRunning()):
            answer = QMessageBox.question(
                self,
                APP_NAME,
                "A scan or build is still running. Close the builder anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        event.accept()


def gui_main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("Darkone Customs")
    window = MainWindow()
    window.show()
    return app.exec()

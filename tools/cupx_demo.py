#!/usr/bin/env python3
from __future__ import annotations

import importlib.metadata
import importlib.util
import multiprocessing
from pathlib import Path
import re
import subprocess
import sys
import traceback

REQUIRED = {
    "PySide6": ("PySide6", "6.7.0"),
    "UnityPy": ("UnityPy", "1.10.18"),
    "PIL": ("Pillow", "11.2.1"),
}


def _version_tuple(text: str) -> tuple[int, int, int]:
    values = [int(value) for value in re.findall(r"\d+", str(text))[:3]]
    return tuple((values + [0, 0, 0])[:3])


def _missing_requirements() -> list[str]:
    missing: list[str] = []
    for module, (package, minimum) in REQUIRED.items():
        if importlib.util.find_spec(module) is None:
            missing.append(f"{package}>={minimum}")
            continue
        try:
            installed = importlib.metadata.version(package)
        except Exception:
            missing.append(f"{package}>={minimum}")
            continue
        if _version_tuple(installed) < _version_tuple(minimum):
            missing.append(f"{package}>={minimum}")
    return missing


def _install(packages: list[str]) -> None:
    if not packages:
        return
    print("CUPX Demo Data Builder: installing/updating Python dependencies:")
    for package in packages:
        print(f"  {package}")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--disable-pip-version-check",
            *packages,
        ]
    )


def _show_bootstrap_error(details: str) -> None:
    error_path = Path(__file__).resolve().with_name("bootstrap_error.log")
    try:
        error_path.write_text(details, encoding="utf-8")
    except Exception:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "CUPX Demo Data Builder",
            "The required Python modules could not be installed.\n\n"
            f"Details were written to:\n{error_path}",
        )
        root.destroy()
    except Exception:
        print(details, file=sys.stderr)


def main() -> int:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required.")
    _install(_missing_requirements())
    from cupx.gui.demo_app import gui_main

    return int(gui_main() or 0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        _show_bootstrap_error(traceback.format_exc())
        raise SystemExit(1)

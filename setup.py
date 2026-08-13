#!/usr/bin/env python3
"""
OpenTune Sync - Setup & Launch
Place in project root. Run: python3 setup.py
"""

import sys, os, subprocess, importlib, tempfile, shutil, time
from pathlib import Path

MIN_PY = (3, 8)
PROJECT_ROOT = Path(__file__).resolve().parent
ENTRY_FILE   = "1786653076755_opentune_sync.py"

REQUIRED_MODULES = [
    ("flask", "flask"),
]

LOG_DIR_TEMP = Path(tempfile.gettempdir()) / "opentune_sync_setup"
LOG_DIR_TEMP.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR_TEMP / f"setup_{int(time.time())}.log"
_log_lines = []


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _log_lines.append(line)


def flush_log(final_dir=None):
    LOG_PATH.write_text("\n".join(_log_lines), encoding="utf-8")
    if final_dir:
        try:
            final_dir = Path(final_dir)
            final_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(LOG_PATH, final_dir / LOG_PATH.name)
            log(f"Log copied to {final_dir / LOG_PATH.name}")
        except Exception as exc:
            log(f"[warn] Could not copy log to project logs dir: {exc}")
    print(f"\nFull log: {LOG_PATH}")


def progress_bar(step, total, width=30):
    filled = int(width * step / total)
    bar = "#" * filled + "." * (width - filled)
    pct = int(100 * step / total)
    print(f"\r[{bar}] {pct}%", end="", flush=True)
    if step == total:
        print()


def fail(msg, final_dir=None):
    log(f"[ERROR] {msg}")
    flush_log(final_dir)
    sys.exit(1)


def check_python_version():
    log(f"Checking Python version (need >= {'.'.join(map(str, MIN_PY))})...")
    if sys.version_info < MIN_PY:
        fail(f"Python {'.'.join(map(str, MIN_PY))}+ required, found "
             f"{sys.version_info.major}.{sys.version_info.minor}.")
    log(f"Python {sys.version.split()[0]} OK.")


def check_pip():
    log("Checking pip availability...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "--version"],
                        check=True, capture_output=True, text=True)
        log("pip OK.")
    except Exception:
        fail("pip is not available for this Python interpreter. "
             "Install pip and re-run this script.")


def ensure_module(import_name, pip_name):
    try:
        importlib.import_module(import_name)
        log(f"{pip_name}: already installed.")
        return True
    except ImportError:
        log(f"{pip_name}: not found, installing...")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", pip_name,
             "--break-system-packages", "--quiet"],
            capture_output=True, text=True)
        if r.returncode != 0:
            log(f"[ERROR] Failed to install {pip_name}: {r.stderr[:500]}")
            return False
        importlib.invalidate_caches()
        log(f"{pip_name}: installed successfully.")
        return True


def install_dependencies():
    log("Installing/verifying required libraries...")
    total = len(REQUIRED_MODULES)
    ok = True
    for i, (import_name, pip_name) in enumerate(REQUIRED_MODULES, start=1):
        if not ensure_module(import_name, pip_name):
            ok = False
        progress_bar(i, total)
    return ok


def locate_entry_file():
    candidate = PROJECT_ROOT / ENTRY_FILE
    if candidate.exists():
        return candidate
    matches = list(PROJECT_ROOT.glob("*opentune_sync*.py"))
    matches = [m for m in matches if m.name != Path(__file__).name]
    if matches:
        return matches[0]
    return None


def main():
    log("=== OpenTune Sync setup starting ===")
    log(f"Project root: {PROJECT_ROOT}")

    check_python_version()
    check_pip()

    if not install_dependencies():
        fail("One or more dependencies failed to install.", final_dir=PROJECT_ROOT / "logs")

    entry = locate_entry_file()
    if entry is None:
        fail(f"Could not find entry file '{ENTRY_FILE}' in {PROJECT_ROOT}.",
             final_dir=PROJECT_ROOT / "logs")

    log(f"Entry file located: {entry.name}")
    flush_log(final_dir=PROJECT_ROOT / "logs")

    log("Starting OpenTune Sync...")
    try:
        subprocess.run([sys.executable, str(entry)], cwd=str(PROJECT_ROOT), check=True)
    except subprocess.CalledProcessError as exc:
        fail(f"OpenTune Sync exited with error code {exc.returncode}.",
             final_dir=PROJECT_ROOT / "logs")
    except KeyboardInterrupt:
        log("Stopped by user.")
        flush_log(final_dir=PROJECT_ROOT / "logs")


if __name__ == "__main__":
    main()

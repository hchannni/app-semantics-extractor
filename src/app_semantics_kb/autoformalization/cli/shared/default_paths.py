"""Shared filesystem defaults for autoformalization CLIs."""

from __future__ import annotations

from pathlib import Path

AUTOFORMALIZATION_DIR = Path(__file__).resolve().parents[2]
PYTHON_DIR = AUTOFORMALIZATION_DIR
PACKAGE_DIR = AUTOFORMALIZATION_DIR.parent
SRC_DIR = PACKAGE_DIR.parent
REPO_ROOT = SRC_DIR.parent

DEFAULT_RUNS_DIR = REPO_ROOT / "runs"
DEFAULT_APP_SOURCE_ROOT = REPO_ROOT / "samples" / "simple_alarm_clock" / "app"
DEFAULT_STATIC_SEMANTICS_RUNS_DIR = REPO_ROOT / "runs" / "static_analysis"

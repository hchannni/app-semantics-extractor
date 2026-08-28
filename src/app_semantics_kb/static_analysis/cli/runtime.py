from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StaticAnalysisResult:
    output_root: Path
    manifest_path: Path
    outputs: dict[str, Path]


def default_joern_bin() -> str | None:
    return os.environ.get("JOERN_BIN") or shutil.which("joern")


def default_cpg_path() -> str | None:
    return os.environ.get("CPG_PATH")

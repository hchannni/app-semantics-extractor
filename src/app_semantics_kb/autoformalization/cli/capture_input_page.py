"""Capture replay-compatible screenshot/a11y inputs without running LLM calls."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..extractors.a11y_tree_parser import A11yTreeParser
from ..extractors.screenshot_maker import ScreenshotMaker
from ..utils import iso_now, log, sha256_file, write_json


_PYTHON_DIR = Path(__file__).resolve().parents[1]
_AUTOFORMALIZATION_DIR = _PYTHON_DIR.parent
_ENGINES_DIR = _AUTOFORMALIZATION_DIR.parent
_SYSTEM_ROOT = _ENGINES_DIR.parent
_REPO_ROOT = _SYSTEM_ROOT.parent

DEFAULT_INPUT_BANK_ROOT = _REPO_ROOT / "experiments" / "input_bank"
_CANONICAL_INPUT_FILES = (
    "input_screenshot.png",
    "input_a11y.xml",
    "input_meta.json",
)


@dataclass(frozen=True)
class CaptureInputPageConfig:
    app_id: str
    page: int
    output_root: Path = DEFAULT_INPUT_BANK_ROOT
    device_serial: str | None = None
    force: bool = False


@dataclass(frozen=True)
class CapturedInputPage:
    input_dir: Path
    screenshot_path: Path
    a11y_path: Path
    meta_path: Path
    meta: dict[str, Any]


def _parse_app_id(value: str) -> str:
    app_id = value.strip()
    if not app_id:
        raise argparse.ArgumentTypeError("app_id cannot be empty")
    path = Path(app_id)
    if path.is_absolute() or len(path.parts) != 1 or app_id in {".", ".."}:
        raise argparse.ArgumentTypeError(
            "app_id must be a single directory name, not a path"
        )
    return app_id


def _parse_positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"page must be a positive integer, got {value}"
        ) from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            f"page must be a positive integer, got {value}"
        )
    return parsed


def _input_dir(config: CaptureInputPageConfig) -> Path:
    return config.output_root / config.app_id / "pages" / f"page_{config.page}"


def _assert_can_write(input_dir: Path, *, force: bool) -> None:
    existing = [
        input_dir / name
        for name in _CANONICAL_INPUT_FILES
        if (input_dir / name).exists()
    ]
    if existing and not force:
        existing_names = ", ".join(path.name for path in existing)
        raise FileExistsError(
            f"{input_dir} already contains {existing_names}; pass --force to overwrite"
        )


def capture_input_page(config: CaptureInputPageConfig) -> CapturedInputPage:
    """Capture the current emulator page into a run_reproduction replay layout."""
    input_dir = _input_dir(config)
    _assert_can_write(input_dir, force=config.force)
    input_dir.mkdir(parents=True, exist_ok=True)

    screenshot_path = ScreenshotMaker(temp_dir=input_dir).capture_png(
        serial=config.device_serial,
        filename="input_screenshot.png",
    )
    a11y_path = input_dir / "input_a11y.xml"
    A11yTreeParser().dump_to_file_from_adb(
        a11y_path,
        serial=config.device_serial,
    )

    meta = {
        "captured_at": iso_now(),
        "app_id": config.app_id,
        "page": config.page,
        "device_serial": config.device_serial,
        "input_screenshot_path": str(screenshot_path),
        "input_a11y_path": str(a11y_path),
        "input_screenshot_sha256": sha256_file(screenshot_path),
        "input_a11y_sha256": sha256_file(a11y_path),
        "replay_from_dir": str(input_dir.parent),
    }
    meta_path = input_dir / "input_meta.json"
    write_json(meta_path, meta)

    return CapturedInputPage(
        input_dir=input_dir,
        screenshot_path=screenshot_path,
        a11y_path=a11y_path,
        meta_path=meta_path,
        meta=meta,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture the current emulator screen into replay-compatible "
            "input_bank/<app>/pages/page_N files without calling any LLM API"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("app_id", type=_parse_app_id)
    parser.add_argument("page", type=_parse_positive_int)
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_INPUT_BANK_ROOT),
        help="Root directory for app/page input banks",
    )
    parser.add_argument(
        "--device-serial",
        default=None,
        help="adb device serial. If omitted, adb's default device is used.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing page_N input capture",
    )
    return parser


def _config_from_args(argv: Sequence[str] | None = None) -> CaptureInputPageConfig:
    args = _build_parser().parse_args(argv)
    return CaptureInputPageConfig(
        app_id=args.app_id,
        page=args.page,
        output_root=Path(args.output_root),
        device_serial=args.device_serial,
        force=args.force,
    )


def main(argv: Sequence[str] | None = None) -> None:
    result = capture_input_page(_config_from_args(argv))
    log(f"[capture_input_page] saved inputs -> {result.input_dir}", "green")
    log(
        "[capture_input_page] later replay with "
        f"--replay-from {result.input_dir.parent}",
        "blue",
    )


if __name__ == "__main__":
    main()

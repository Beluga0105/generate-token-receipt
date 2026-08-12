#!/usr/bin/env python3
"""Render a generated receipt HTML to PDF and/or PNG with local Chrome."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
)


def find_chrome(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        resolved = shutil.which(explicit)
        if resolved:
            return resolved
        raise FileNotFoundError(f"Chrome executable not found: {explicit}")

    for command in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        resolved = shutil.which(command)
        if resolved:
            return resolved
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError(
        "No Chrome/Chromium executable found. Pass --chrome /absolute/path/to/chrome."
    )


def run_chrome(chrome: str, args: list[str], timeout: int) -> None:
    command = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--run-all-compositor-stages-before-draw",
        *args,
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Chrome render failed ({result.returncode}): {details}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a token receipt HTML file to a local PDF and/or PNG preview."
    )
    parser.add_argument("html", help="Absolute or relative path to the receipt HTML")
    parser.add_argument("--pdf", help="Output PDF path")
    parser.add_argument("--png", help="Output PNG preview path")
    parser.add_argument("--chrome", help="Chrome/Chromium executable path")
    parser.add_argument(
        "--viewport-width",
        type=int,
        default=900,
        help="PNG viewport width in pixels; use at least 500 for complete 80 mm previews",
    )
    parser.add_argument("--viewport-height", type=int, default=1200)
    parser.add_argument("--timeout", type=int, default=45, help="Seconds per render")
    args = parser.parse_args()
    if not args.pdf and not args.png:
        parser.error("pass --pdf, --png, or both")
    if args.viewport_width < 320 or args.viewport_height < 480:
        parser.error("viewport must be at least 320 x 480")
    return args


def main() -> int:
    args = parse_args()
    source = Path(args.html).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Receipt HTML not found: {source}")

    chrome = find_chrome(args.chrome)
    source_url = source.as_uri()
    outputs: list[Path] = []

    if args.pdf:
        pdf_path = Path(args.pdf).expanduser().resolve()
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        run_chrome(
            chrome,
            [
                "--no-pdf-header-footer",
                "--print-to-pdf-no-header",
                f"--print-to-pdf={pdf_path}",
                source_url,
            ],
            args.timeout,
        )
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            raise RuntimeError(f"Chrome did not create the requested PDF: {pdf_path}")
        outputs.append(pdf_path)

    if args.png:
        png_path = Path(args.png).expanduser().resolve()
        png_path.parent.mkdir(parents=True, exist_ok=True)
        run_chrome(
            chrome,
            [
                f"--window-size={args.viewport_width},{args.viewport_height}",
                f"--screenshot={png_path}",
                source_url,
            ],
            args.timeout,
        )
        if not png_path.is_file() or png_path.stat().st_size == 0:
            raise RuntimeError(f"Chrome did not create the requested PNG: {png_path}")
        outputs.append(png_path)

    for output in outputs:
        print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)

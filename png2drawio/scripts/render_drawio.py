#!/usr/bin/env python3
"""Render a draw.io diagram to a PNG without requiring Xvfb or FUSE."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import warnings
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, UnidentifiedImageError


DEFAULT_APPIMAGE = Path("/opt/app/draw.io/current")
MAX_DRAWIO_BYTES = 64 * 1024 * 1024


class RenderError(RuntimeError):
    """An actionable draw.io discovery or rendering failure."""


@dataclass(frozen=True)
class CanvasSpec:
    width: int
    height: int
    transparent: bool
    background_rgba: Tuple[int, int, int, int]


def _canvas_dimension(raw: Optional[str], name: str) -> int:
    try:
        value = float(raw) if raw is not None else math.nan
    except ValueError as exc:
        raise RenderError(f"mxGraphModel.{name} must be a positive integer") from exc
    if not math.isfinite(value) or value <= 0 or not value.is_integer():
        raise RenderError(f"mxGraphModel.{name} must be a positive integer")
    result = int(value)
    if result > 1_000_000:
        raise RenderError(f"mxGraphModel.{name} is unreasonably large: {result}")
    return result


def parse_canvas(input_path: Path) -> CanvasSpec:
    """Safely read canvas metadata from a single-page uncompressed draw.io file."""
    if not input_path.is_file():
        raise RenderError(f"input draw.io file does not exist: {input_path}")
    try:
        with input_path.open("rb") as stream:
            data = stream.read(MAX_DRAWIO_BYTES + 1)
    except OSError as exc:
        raise RenderError(f"could not read input draw.io file {input_path}: {exc}") from exc
    if len(data) > MAX_DRAWIO_BYTES:
        raise RenderError(f"input draw.io XML exceeds {MAX_DRAWIO_BYTES} bytes")
    lowered = data.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise RenderError("DTD and ENTITY declarations are forbidden in draw.io input")
    try:
        document = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RenderError(f"malformed draw.io XML: {exc}") from exc
    if document.tag != "mxfile":
        raise RenderError(f"draw.io root must be mxfile, got {document.tag!r}")
    if document.get("compressed", "false").strip().lower() not in {"false", "0"}:
        raise RenderError("compressed draw.io documents are unsupported")
    diagrams = document.findall("diagram")
    if len(diagrams) != 1:
        raise RenderError(f"draw.io input must contain exactly one diagram (found {len(diagrams)})")
    diagram = diagrams[0]
    models = diagram.findall("mxGraphModel")
    if len(models) != 1 or (diagram.text or "").strip():
        raise RenderError("diagram must contain one direct, uncompressed mxGraphModel")
    model = models[0]
    width = _canvas_dimension(model.get("pageWidth"), "pageWidth")
    height = _canvas_dimension(model.get("pageHeight"), "pageHeight")
    if Image.MAX_IMAGE_PIXELS is not None and width * height > Image.MAX_IMAGE_PIXELS:
        raise RenderError(f"canvas dimensions are too large to render safely: {width}x{height}")
    background = (model.get("background") or "none").strip().lower()
    transparent = background in {"", "none", "transparent"}
    background_rgba = (0, 0, 0, 0) if transparent else (255, 255, 255, 255)
    if not transparent and len(background) == 7 and background.startswith("#"):
        try:
            background_rgba = tuple(
                int(background[index : index + 2], 16) for index in (1, 3, 5)
            ) + (255,)
        except ValueError:
            pass
    return CanvasSpec(
        width=width,
        height=height,
        transparent=transparent,
        background_rgba=background_rgba,
    )


def _resolve_executable(value: str, explicit: bool = False) -> Optional[Path]:
    candidate = Path(value).expanduser()
    if os.sep in value or (os.altsep and os.altsep in value) or candidate.is_absolute():
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
        if explicit:
            raise RenderError(f"draw.io executable is missing or not executable: {candidate}")
        return None
    found = shutil.which(value)
    if found:
        return Path(found).resolve()
    if explicit:
        raise RenderError(f"draw.io executable was not found on PATH: {value}")
    return None


def find_renderer(override: Optional[str]) -> Path:
    """Find draw.io in the documented precedence order."""
    if override:
        renderer = _resolve_executable(override, explicit=True)
        assert renderer is not None
        return renderer
    for name in ("drawio", "draw.io"):
        renderer = _resolve_executable(name)
        if renderer is not None:
            return renderer
    if DEFAULT_APPIMAGE.is_file() and os.access(DEFAULT_APPIMAGE, os.X_OK):
        return DEFAULT_APPIMAGE.resolve()
    raise RenderError(
        "draw.io renderer not found; install the drawio/draw.io CLI, or pass "
        "--drawio-bin /path/to/drawio.AppImage"
    )


def is_appimage(path: Path) -> bool:
    """Recognize AppImage type 1/2 from its ELF identification bytes."""
    try:
        with path.open("rb") as stream:
            header = stream.read(11)
    except OSError:
        return False
    return header[:4] == b"\x7fELF" and header[8:10] == b"AI" and header[10:11] in (b"\x01", b"\x02")


def _run(
    command: Sequence[str],
    *,
    timeout: float,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        raise RenderError(f"could not execute {command[0]}: {exc}") from exc
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        else:
            # The leader may exit while descendants remain in its process group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        details = _format_output(stdout or "", stderr or "")
        raise RenderError(f"draw.io timed out after {timeout:g} seconds{details}") from exc
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _format_output(stdout: str, stderr: str) -> str:
    parts: List[str] = []
    if stdout.strip():
        parts.append(f"stdout:\n{stdout.strip()}")
    if stderr.strip():
        parts.append(f"stderr:\n{stderr.strip()}")
    return "\n" + "\n".join(parts) if parts else ""


def _sandbox_hint(output: str) -> str:
    lowered = output.lower()
    indicators = (
        "sandbox",
        "permission denied",
        "operation not permitted",
        "eperm",
        "eacces",
        "zygote",
    )
    if not any(item in lowered for item in indicators):
        return ""
    return (
        "\nThe failure appears permission/sandbox related. Ensure the input is readable and "
        "the output directory and extraction cache are writable. If an outer container or "
        "job sandbox blocks Electron, grant it permission to execute the extracted AppRun, "
        "or retry with --cache-dir pointing to a writable executable filesystem."
    )


def _cache_key(appimage: Path) -> str:
    digest = hashlib.sha256()
    try:
        with appimage.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RenderError(f"could not hash AppImage {appimage}: {exc}") from exc
    return digest.hexdigest()


def validate_cache_root(cache_root: Path) -> None:
    try:
        status = cache_root.stat()
    except OSError as exc:
        raise RenderError(
            f"could not inspect AppImage cache directory {cache_root}: {exc}"
        ) from exc
    if not cache_root.is_dir():
        raise RenderError(f"AppImage cache path is not a directory: {cache_root}")
    if status.st_uid != os.getuid():
        raise RenderError(
            f"AppImage cache directory must be owned by uid {os.getuid()}: {cache_root}"
        )
    if status.st_mode & 0o022:
        raise RenderError(
            "AppImage cache directory must not be writable by group or other users: "
            f"{cache_root}"
        )


def _valid_cached_appimage(destination: Path, digest: str) -> Optional[Path]:
    if destination.is_symlink() or not destination.is_dir():
        return None
    marker = destination / ".complete"
    squashfs_root = destination / "squashfs-root"
    app_run = squashfs_root / "AppRun"
    if squashfs_root.is_symlink() or app_run.is_symlink():
        return None
    trusted_permissions = False
    try:
        destination_status = destination.stat()
        trusted_permissions = (
            destination_status.st_uid == os.getuid()
            and not destination_status.st_mode & 0o022
        )
        complete = marker.read_text(encoding="ascii").strip() == digest
        resolved_destination = destination.resolve(strict=True)
        resolved_app_run = app_run.resolve(strict=True)
        contained = resolved_app_run.is_relative_to(resolved_destination)
    except (OSError, RuntimeError, UnicodeError):
        complete = False
        contained = False
    if (
        complete
        and contained
        and trusted_permissions
        and marker.is_file()
        and not marker.is_symlink()
        and squashfs_root.is_dir()
        and app_run.is_file()
        and os.access(app_run, os.X_OK)
    ):
        return app_run
    return None


def extract_appimage(appimage: Path, cache_root: Path, timeout: float) -> Path:
    """Extract an AppImage and return its AppRun path."""
    validate_cache_root(cache_root)
    digest = _cache_key(appimage)
    destination = cache_root / f"drawio-{digest}"
    lock_path = cache_root / f".drawio-{digest}.lock"
    try:
        lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, lock_flags, 0o600)
    except OSError as exc:
        raise RenderError(f"could not create AppImage cache lock {lock_path}: {exc}") from exc
    staging: Optional[Path] = None
    try:
        with os.fdopen(lock_fd, "r+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            cached = _valid_cached_appimage(destination, digest)
            if cached is not None:
                return cached

            staging = Path(
                tempfile.mkdtemp(prefix=f".drawio-{digest}.", dir=cache_root)
            )
            result = _run(
                [str(appimage), "--appimage-extract"], cwd=staging, timeout=timeout
            )
            if result.returncode != 0:
                combined = f"{result.stdout}\n{result.stderr}"
                raise RenderError(
                    f"failed to extract AppImage {appimage} (exit {result.returncode})"
                    f"{_format_output(result.stdout, result.stderr)}{_sandbox_hint(combined)}"
                )
            staged_app_run = staging / "squashfs-root" / "AppRun"
            if not staged_app_run.is_file() or not os.access(staged_app_run, os.X_OK):
                raise RenderError(
                    "AppImage extraction did not create an executable launcher at "
                    f"{staged_app_run}"
                )
            (staging / ".complete").write_text(digest + "\n", encoding="ascii")
            os.chmod(staging, 0o700)
            if _valid_cached_appimage(staging, digest) is None:
                raise RenderError(f"extracted AppImage staging failed validation: {staging}")
            if destination.is_symlink():
                destination.unlink()
            elif destination.exists():
                shutil.rmtree(destination)
            os.replace(staging, destination)
            staging = None
            published = _valid_cached_appimage(destination, digest)
            if published is None:
                raise RenderError(f"published AppImage cache failed validation: {destination}")
            return published
    except OSError as exc:
        raise RenderError(f"could not prepare AppImage cache in {cache_root}: {exc}") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def prepare_renderer(
    renderer: Path,
    *,
    cache_root: Path,
    timeout: float,
) -> Tuple[List[str], Dict[str, str], Optional[Path]]:
    env = os.environ.copy()
    env["ELECTRON_DISABLE_SANDBOX"] = "1"
    if not is_appimage(renderer):
        return [str(renderer)], env, None

    app_run = extract_appimage(renderer, cache_root, timeout)
    app_dir = app_run.parent
    env["APPDIR"] = str(app_dir)
    return [str(app_run), "--no-sandbox"], env, app_dir


def inspect_png(path: Path) -> Tuple[Tuple[int, int], str]:
    """Fully validate a PNG and return its dimensions and decoded mode."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format != "PNG":
                    raise RenderError("draw.io output is not a PNG file")
                image.verify()
            with Image.open(path) as image:
                image.load()
                return image.size, image.mode
    except RenderError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
    ) as exc:
        raise RenderError(f"draw.io produced an invalid or truncated PNG: {exc}") from exc


def normalize_exporter_rounding(path: Path, canvas: CanvasSpec) -> None:
    """Correct only draw.io's at-most-one-pixel-per-edge bounds rounding."""
    with Image.open(path) as source:
        source.load()
        source_width, source_height = source.size
        if abs(source_width - canvas.width) > 2 or abs(source_height - canvas.height) > 2:
            raise RenderError(
                "draw.io produced the wrong PNG dimensions: "
                f"expected {canvas.width}x{canvas.height}, got {source_width}x{source_height}"
            )
        mode = "RGBA" if canvas.transparent else "RGB"
        decoded = source.convert(mode)
        fill = canvas.background_rgba if mode == "RGBA" else canvas.background_rgba[:3]
        normalized = Image.new(mode, (canvas.width, canvas.height), fill)
        source_left = max(0, (source_width - canvas.width) // 2)
        source_top = max(0, (source_height - canvas.height) // 2)
        target_left = max(0, (canvas.width - source_width) // 2)
        target_top = max(0, (canvas.height - source_height) // 2)
        copy_width = min(source_width, canvas.width)
        copy_height = min(source_height, canvas.height)
        region = decoded.crop(
            (source_left, source_top, source_left + copy_width, source_top + copy_height)
        )
        normalized.paste(region, (target_left, target_top))
        normalized.save(path, format="PNG")


def render(
    input_path: Path,
    output_path: Path,
    canvas: CanvasSpec,
    command_prefix: Sequence[str],
    env: Dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    if input_path == output_path:
        raise RenderError("input and output must be different paths")
    if output_path.exists():
        try:
            if input_path.samefile(output_path):
                raise RenderError("input and output refer to the same file")
        except OSError as exc:
            raise RenderError(f"could not compare input/output paths: {exc}") from exc
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_path.stem}.render-", dir=output_path.parent)
    )
    temporary_path = temporary_dir / "output.png"
    command = [
        *command_prefix,
        "--export",
        "--format",
        "png",
        "--border",
        "0",
        "--scale",
        "1",
        "--width",
        str(canvas.width),
        "--height",
        str(canvas.height),
        *(["--transparent"] if canvas.transparent else []),
        "--output",
        str(temporary_path),
        str(input_path),
    ]
    try:
        result = _run(command, timeout=timeout, env=env)
        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}"
            raise RenderError(
                f"draw.io export failed with exit code {result.returncode}"
                f"{_format_output(result.stdout, result.stderr)}{_sandbox_hint(combined)}"
            )
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise RenderError(
                "draw.io reported success but did not produce a non-empty PNG"
                f"{_format_output(result.stdout, result.stderr)}"
            )
        actual_size, actual_mode = inspect_png(temporary_path)
        if canvas.transparent and actual_mode != "RGBA":
            raise RenderError(
                f"transparent canvas export must be RGBA, got PNG mode {actual_mode}"
            )
        if actual_size != (canvas.width, canvas.height):
            normalize_exporter_rounding(temporary_path, canvas)
            actual_size, actual_mode = inspect_png(temporary_path)
        if actual_size != (canvas.width, canvas.height):
            raise RenderError("internal error while normalizing draw.io bounds rounding")
        if canvas.transparent and actual_mode != "RGBA":
            raise RenderError(
                f"transparent canvas export must be RGBA, got PNG mode {actual_mode}"
            )
        temporary_path.replace(output_path)
        return result
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, help="input .drawio file")
    parser.add_argument("output", nargs="?", type=Path, help="output PNG path")
    parser.add_argument("--drawio-bin", help="explicit draw.io executable or AppImage")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        help="persistent directory for extracted AppImage contents (default: temporary)",
    )
    parser.add_argument(
        "--keep-extracted",
        action="store_true",
        help="retain the temporary AppImage extraction directory",
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="per-command timeout in seconds (default: 120)"
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="locate and, for AppImages, extract the renderer without exporting",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if not args.preflight and (args.input is None or args.output is None):
        parser.error("input and output are required unless --preflight is used")

    temp_root: Optional[Path] = None
    try:
        input_path: Optional[Path] = None
        output_path: Optional[Path] = None
        canvas: Optional[CanvasSpec] = None
        if not args.preflight:
            assert args.input is not None and args.output is not None
            input_path = args.input.expanduser().resolve()
            output_path = args.output.expanduser().resolve()
            canvas = parse_canvas(input_path)

        renderer = find_renderer(args.drawio_bin)
        if args.cache_dir is not None:
            cache_root = args.cache_dir.expanduser().resolve()
            cache_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        else:
            temp_root = Path(tempfile.mkdtemp(prefix="drawio-appimage-"))
            cache_root = temp_root
        validate_cache_root(cache_root)

        prefix, env, app_dir = prepare_renderer(
            renderer, cache_root=cache_root, timeout=args.timeout
        )
        if args.preflight:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "renderer": str(renderer),
                        "kind": "appimage" if app_dir else "native",
                        "launcher": prefix[0],
                        "extracted_appdir": str(app_dir) if app_dir else None,
                        "extraction_retained": bool(args.cache_dir or args.keep_extracted),
                    },
                    indent=2,
                )
            )
            return 0

        assert input_path is not None and output_path is not None and canvas is not None
        result = render(input_path, output_path, canvas, prefix, env, args.timeout)
        print(
            json.dumps(
                {
                    "ok": True,
                    "input": str(input_path),
                    "output": str(output_path),
                    "renderer": str(renderer),
                    "launcher": prefix[0],
                    "canvas": {
                        "width": canvas.width,
                        "height": canvas.height,
                        "transparent": canvas.transparent,
                    },
                    "stdout": result.stdout.strip(),
                    "stderr": result.stderr.strip(),
                },
                indent=2,
            )
        )
        return 0
    except RenderError as exc:
        print(f"render_drawio: error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"render_drawio: error: {exc}", file=sys.stderr)
        return 2
    finally:
        if temp_root is not None:
            if args.keep_extracted:
                print(f"render_drawio: retained extraction at {temp_root}", file=sys.stderr)
            else:
                shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

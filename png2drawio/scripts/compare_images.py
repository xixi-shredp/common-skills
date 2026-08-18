#!/usr/bin/env python3
"""Compare two PNG images with a strict normalized-MAE pass/fail gate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import warnings
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError


class CompareError(RuntimeError):
    """An image loading or output error."""


def load_rgba_on_white(path: Path) -> np.ndarray:
    """Load an image and normalize hidden transparent RGB by compositing on white."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                if source.format != "PNG":
                    raise CompareError(f"not a PNG image: {path}")
                source.verify()
            with Image.open(path) as source:
                source.load()
                rgba = source.convert("RGBA")
                white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                normalized = Image.alpha_composite(white, rgba)
                return np.asarray(normalized, dtype=np.uint8).copy()
    except FileNotFoundError as exc:
        raise CompareError(f"image does not exist: {path}") from exc
    except CompareError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
    ) as exc:
        raise CompareError(f"could not read PNG {path}: {exc}") from exc


def normalized_mae(reference: np.ndarray, candidate: np.ndarray) -> float:
    difference = np.abs(reference.astype(np.float32) - candidate.astype(np.float32))
    return float(np.mean(difference, dtype=np.float64) / 255.0)


def ssim(reference: np.ndarray, candidate: np.ndarray) -> float:
    """Compute mean channel SSIM using an 11x11 Gaussian window."""
    first = reference.astype(np.float64)
    second = candidate.astype(np.float64)
    mu_first = cv2.GaussianBlur(first, (11, 11), 1.5)
    mu_second = cv2.GaussianBlur(second, (11, 11), 1.5)
    mu_first_sq = mu_first * mu_first
    mu_second_sq = mu_second * mu_second
    mu_product = mu_first * mu_second

    sigma_first_sq = cv2.GaussianBlur(first * first, (11, 11), 1.5) - mu_first_sq
    sigma_second_sq = cv2.GaussianBlur(second * second, (11, 11), 1.5) - mu_second_sq
    sigma_product = cv2.GaussianBlur(first * second, (11, 11), 1.5) - mu_product
    # Small negative variances are possible from floating-point cancellation.
    sigma_first_sq = np.maximum(sigma_first_sq, 0.0)
    sigma_second_sq = np.maximum(sigma_second_sq, 0.0)

    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    numerator = (2.0 * mu_product + c1) * (2.0 * sigma_product + c2)
    denominator = (mu_first_sq + mu_second_sq + c1) * (
        sigma_first_sq + sigma_second_sq + c2
    )
    score_map = np.divide(
        numerator,
        denominator,
        out=np.ones_like(numerator),
        where=denominator != 0,
    )
    return float(np.clip(np.mean(score_map, dtype=np.float64), -1.0, 1.0))


def pixel_errors(reference: np.ndarray, candidate: np.ndarray) -> np.ndarray:
    absolute = np.abs(reference.astype(np.float32) - candidate.astype(np.float32))
    return np.mean(absolute, axis=2, dtype=np.float32) / 255.0


def region_diagnostics(
    errors: np.ndarray, pixel_threshold: float, min_region_area: int
) -> Tuple[List[Dict[str, Any]], Dict[str, int], np.ndarray]:
    mask = (errors > pixel_threshold).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    regions: List[Dict[str, Any]] = []
    ignored_count = 0
    ignored_area = 0
    for label in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[label])
        selected = errors[labels == label]
        item = {
            "bbox": [x, y, width, height],
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "x2_exclusive": x + width,
            "y2_exclusive": y + height,
            "area": area,
            "mean_error": float(np.mean(selected, dtype=np.float64)),
            "max_error": float(np.max(selected)),
        }
        if area >= min_region_area:
            regions.append(item)
        else:
            ignored_count += 1
            ignored_area += area
    regions.sort(key=lambda item: (-item["area"], item["y"], item["x"]))
    return regions, {"count": ignored_count, "area": ignored_area}, mask


def save_heatmap(errors: np.ndarray, path: Path) -> None:
    intensity = np.clip(np.rint(errors * 255.0), 0, 255).astype(np.uint8)
    bgr = cv2.applyColorMap(intensity, cv2.COLORMAP_TURBO)
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    atomic_save_image(Image.fromarray(rgb, mode="RGB"), path)


def save_mask(mask: np.ndarray, path: Path) -> None:
    atomic_save_image(Image.fromarray(mask * np.uint8(255), mode="L"), path)


def atomic_save_image(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".png", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        image.save(temporary, format="PNG")
        with Image.open(temporary) as check:
            check.verify()
        os.replace(temporary, path)
    except (OSError, ValueError) as exc:
        raise CompareError(f"could not write diagnostic PNG {path}: {exc}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def dimensions(array: np.ndarray) -> Dict[str, int]:
    return {"width": int(array.shape[1]), "height": int(array.shape[0])}


def emit(payload: Dict[str, Any], output: Optional[Path]) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".json", dir=output.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(serialized + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, output)
        except OSError as exc:
            raise CompareError(f"could not write JSON output {output}: {exc}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    print(serialized)


def paths_alias(first: Path, second: Path) -> bool:
    if first == second:
        return True
    if first.exists() and second.exists():
        try:
            return first.samefile(second)
        except OSError as exc:
            raise CompareError(f"could not compare paths {first} and {second}: {exc}") from exc
    return False


def reject_path_conflicts(paths: Sequence[Tuple[str, Path]]) -> None:
    for index, (first_name, first) in enumerate(paths):
        for second_name, second in paths[index + 1 :]:
            if paths_alias(first, second):
                raise CompareError(
                    f"path conflict: {first_name} and {second_name} refer to the same file: {first}"
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="reference PNG")
    parser.add_argument("candidate", type=Path, help="candidate PNG")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.05,
        help="pass only when normalized MAE is strictly below this value (default: 0.05)",
    )
    parser.add_argument(
        "--resize-candidate",
        action="store_true",
        help="resize a mismatched candidate for diagnostics; a resized comparison can never pass",
    )
    parser.add_argument(
        "--pixel-threshold",
        type=float,
        default=0.0,
        help="per-pixel error threshold for the diagnostic mask/regions (default: 0)",
    )
    parser.add_argument(
        "--min-region-area",
        type=int,
        default=1,
        help="omit smaller connected regions from reporting only (default: 1)",
    )
    parser.add_argument(
        "--diff-heatmap",
        "--heatmap",
        dest="diff_heatmap",
        type=Path,
        help="save an absolute-difference heatmap PNG",
    )
    parser.add_argument(
        "--diff-mask",
        "--mask",
        dest="diff_mask",
        type=Path,
        help="save the binary difference mask PNG",
    )
    parser.add_argument(
        "--json-output",
        "--output-json",
        dest="json_output",
        type=Path,
        help="also save metrics as JSON",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be between 0 and 1")
    if not 0.0 <= args.pixel_threshold <= 1.0:
        parser.error("--pixel-threshold must be between 0 and 1")
    if args.min_region_area < 1:
        parser.error("--min-region-area must be at least 1")

    try:
        reference_path = args.reference.expanduser().resolve()
        candidate_path = args.candidate.expanduser().resolve()
        heatmap_path = args.diff_heatmap.expanduser().resolve() if args.diff_heatmap else None
        mask_path = args.diff_mask.expanduser().resolve() if args.diff_mask else None
        json_path = args.json_output.expanduser().resolve() if args.json_output else None
        named_paths: List[Tuple[str, Path]] = [
            ("reference", reference_path),
            ("candidate", candidate_path),
        ]
        if heatmap_path is not None:
            named_paths.append(("diff heatmap", heatmap_path))
        if mask_path is not None:
            named_paths.append(("diff mask", mask_path))
        if json_path is not None:
            named_paths.append(("JSON output", json_path))
        reject_path_conflicts(named_paths)

        reference = load_rgba_on_white(reference_path)
        candidate = load_rgba_on_white(candidate_path)
        reference_size = dimensions(reference)
        candidate_size = dimensions(candidate)
        sizes_match = reference.shape == candidate.shape
        resized = False

        base: Dict[str, Any] = {
            "reference": str(reference_path),
            "candidate": str(candidate_path),
            "reference_size": reference_size,
            "candidate_size": candidate_size,
            "dimensions_match": sizes_match,
            "threshold": args.threshold,
            "comparison_rule": "normalized_mae < threshold",
            "hard_gate_channels": "RGBA composited on opaque white",
            "diagnostic_channels": "RGB composited on opaque white",
            "ssim_channels": "RGB",
            "heatmap_channels": "RGB",
            "min_region_area": args.min_region_area,
            "pixel_threshold": args.pixel_threshold,
            "small_region_filter_affects_gate": False,
        }
        if not sizes_match and not args.resize_candidate:
            base.update(
                {
                    "passed": False,
                    "eligible_to_pass": False,
                    "reason": "image dimensions differ; use --resize-candidate for diagnostics only",
                    "normalized_mae": None,
                    "ssim": None,
                    "regions": [],
                }
            )
            emit(base, json_path)
            return 1

        if not sizes_match:
            resized = True
            candidate_image = Image.fromarray(candidate, mode="RGBA")
            candidate = np.asarray(
                candidate_image.resize(
                    (reference.shape[1], reference.shape[0]), resample=Image.Resampling.LANCZOS
                ),
                dtype=np.uint8,
            ).copy()

        mae = normalized_mae(reference, candidate)
        reference_rgb = reference[:, :, :3]
        candidate_rgb = candidate[:, :, :3]
        ssim_score = ssim(reference_rgb, candidate_rgb)
        errors = pixel_errors(reference_rgb, candidate_rgb)
        regions, ignored, mask = region_diagnostics(
            errors, args.pixel_threshold, args.min_region_area
        )
        eligible = not resized
        passed = eligible and mae < args.threshold
        reason: Optional[str] = None
        if resized:
            reason = "candidate was resized for diagnostics; resized comparisons cannot pass"
        elif not passed:
            reason = "normalized MAE is not strictly below threshold"

        if heatmap_path is not None:
            save_heatmap(errors, heatmap_path)
        if mask_path is not None:
            save_mask(mask, mask_path)

        base.update(
            {
                "passed": passed,
                "eligible_to_pass": eligible,
                "reason": reason,
                "diagnostic_resize_applied": resized,
                "normalized_mae": mae,
                "ssim": ssim_score,
                "differing_pixels": int(np.count_nonzero(mask)),
                "total_pixels": int(mask.size),
                "max_pixel_error": float(np.max(errors)),
                "mean_pixel_error": float(np.mean(errors, dtype=np.float64)),
                "regions": regions,
                "reported_region_count": len(regions),
                "ignored_small_regions": ignored,
                "diff_heatmap": str(heatmap_path) if heatmap_path else None,
                "diff_mask": str(mask_path) if mask_path else None,
            }
        )
        emit(base, json_path)
        return 0 if passed else 1
    except CompareError as exc:
        print(f"compare_images: error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, cv2.error) as exc:
        print(f"compare_images: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

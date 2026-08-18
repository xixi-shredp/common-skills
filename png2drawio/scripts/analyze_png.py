#!/usr/bin/env python3
"""Create a conservative, editable scene description from a PNG image.

The analyzer deliberately reports geometric/OCR candidates rather than trying to
infer diagram semantics.  The resulting JSON is intended to be refined by a
later conversion step or by a human.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import warnings
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import cv2  # type: ignore[import-not-found]
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
except ImportError as exc:  # pragma: no cover - depends on the calling environment
    raise SystemExit(
        "analyze_png.py requires Pillow, NumPy, and OpenCV (cv2): " + str(exc)
    ) from exc


JsonDict = dict[str, Any]
Point = tuple[float, float]
DEFAULT_MAX_PIXELS = 12_000_000
DEFAULT_OCR_TIMEOUT = 15.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze a PNG into a conservative scene.json draft. The source "
            "bitmap is referenced by path and is never embedded in the JSON."
        )
    )
    parser.add_argument("input", type=Path, help="input PNG file")
    parser.add_argument("output", type=Path, help="output scene JSON file")
    parser.add_argument(
        "--overlay",
        type=Path,
        help="optional diagnostic PNG with detected bounds and labels",
    )
    parser.add_argument(
        "--languages",
        default="eng+chi_sim",
        help="Tesseract language expression (default: %(default)s)",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="skip Tesseract OCR and text/geometry reconciliation",
    )
    parser.add_argument(
        "--confidence-threshold",
        type=float,
        default=40.0,
        metavar="PERCENT",
        help="minimum Tesseract word confidence, 0..100 (default: %(default)s)",
    )
    parser.add_argument(
        "--ocr-timeout",
        type=float,
        default=DEFAULT_OCR_TIMEOUT,
        metavar="SECONDS",
        help="timeout for each Tesseract invocation (default: %(default)s)",
    )
    parser.add_argument(
        "--max-pixels",
        type=int,
        default=DEFAULT_MAX_PIXELS,
        metavar="COUNT",
        help=(
            "analysis-memory safety limit in pixels; override explicitly for "
            "larger trusted images (default: %(default)s)"
        ),
    )
    return parser


def _hex(rgb: Sequence[int | float]) -> str:
    values = [max(0, min(255, int(round(float(value))))) for value in rgb[:3]]
    return "#" + "".join(f"{value:02x}" for value in values)


def _round(value: float) -> float:
    return round(float(value), 2)


def _bbox_dict(x: float, y: float, width: float, height: float) -> JsonDict:
    return {
        "height": _round(max(0.0, height)),
        "width": _round(max(0.0, width)),
        "x": _round(x),
        "y": _round(y),
    }


def _point_value(point: Point) -> list[float]:
    return [_round(point[0]), _round(point[1])]


def _dominant_background(rgba: "np.ndarray") -> tuple[int, int, int]:
    """Estimate the background from border pixels, falling back to all pixels."""
    height, width = rgba.shape[:2]
    edge_width = max(1, min(8, min(width, height) // 50))
    border = np.concatenate(
        (
            rgba[:edge_width].reshape(-1, 4),
            rgba[-edge_width:].reshape(-1, 4),
            rgba[:, :edge_width].reshape(-1, 4),
            rgba[:, -edge_width:].reshape(-1, 4),
        )
    )
    opaque = border[border[:, 3] >= 16]
    if opaque.size == 0:
        opaque = rgba.reshape(-1, 4)
        opaque = opaque[opaque[:, 3] >= 16]
    if opaque.size == 0:
        return (255, 255, 255)

    # Quantization makes anti-aliased and lightly compressed-looking edges vote
    # for the same color. The median of the winning bucket restores a natural
    # representative rather than returning the quantized color itself.
    rgb = opaque[:, :3].astype(np.int16)
    buckets = (rgb // 16).astype(np.int16)
    keys, counts = np.unique(buckets, axis=0, return_counts=True)
    winner = keys[int(np.argmax(counts))]
    members = rgb[np.all(buckets == winner, axis=1)]
    color = np.median(members, axis=0)
    return tuple(int(value) for value in color)  # type: ignore[return-value]


def _transparent_detection_background(rgba: "np.ndarray") -> tuple[int, int, int]:
    """Choose a neutral backdrop that contrasts with visible alpha content."""
    flattened = rgba.reshape(-1, 4)
    stride = max(1, len(flattened) // 250_000)
    sampled = flattened[::stride]
    sampled = sampled[sampled[:, 3] >= 32]
    if sampled.size == 0:
        return (255, 255, 255)
    pixels = sampled[:, :3].astype(np.float32)
    luminance = pixels @ np.asarray((0.2126, 0.7152, 0.0722), dtype=np.float32)
    return (255, 255, 255) if float(np.median(luminance)) < 140.0 else (0, 0, 0)


def _extract_palette(
    rgba: "np.ndarray", background: tuple[int, int, int], limit: int = 8
) -> list[JsonDict]:
    pixels = rgba.reshape(-1, 4)
    pixels = pixels[pixels[:, 3] >= 16]
    if pixels.size == 0:
        return [{"color": _hex(background), "fraction": 1.0}]

    # Bound memory/time on screenshots while keeping the sampling deterministic.
    stride = max(1, len(pixels) // 250_000)
    rgb = pixels[::stride, :3].astype(np.int16)
    buckets = (rgb // 24).astype(np.int16)
    keys, counts = np.unique(buckets, axis=0, return_counts=True)
    order = np.argsort(-counts, kind="stable")[:limit]
    total = float(counts.sum())
    palette: list[JsonDict] = []
    for index in order:
        key = keys[index]
        members = rgb[np.all(buckets == key, axis=1)]
        representative = np.median(members, axis=0)
        palette.append(
            {
                "color": _hex(representative),
                "fraction": round(float(counts[index]) / total, 4),
            }
        )
    return palette


def _join_words(words: Sequence[str]) -> str:
    def is_cjk(character: str) -> bool:
        codepoint = ord(character)
        return (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        )

    result = ""
    for word in words:
        if not result:
            result = word
        elif is_cjk(result[-1]) and is_cjk(word[0]):
            result += word
        else:
            result += " " + word
    return result


def _image_png_bytes(rgb: "np.ndarray") -> bytes:
    stream = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(stream, format="PNG")
    return stream.getvalue()


def _invoke_tesseract(
    executable: str,
    png_bytes: bytes,
    languages: str,
    psm: int,
    timeout: float,
) -> tuple[str, list[str]]:
    command = [
        executable,
        "stdin",
        "stdout",
        "-l",
        languages,
        "--psm",
        str(psm),
        "tsv",
    ]
    try:
        process = subprocess.run(
            command,
            input=png_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Tesseract timed out after {timeout:g}s (PSM {psm})"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"could not run Tesseract: {exc}") from exc
    stderr = process.stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise RuntimeError(
            f"Tesseract failed (exit {process.returncode}, PSM {psm}) for "
            f"languages {languages!r}: {stderr or 'no error details'}"
        )
    messages = [line for line in stderr.splitlines() if line.strip()]
    return process.stdout.decode("utf-8", errors="replace"), messages


def _meaningful_text_length(text: str) -> int:
    return sum(character.isalnum() for character in text)


def _parse_tsv_lines(
    tsv: str,
    threshold: float,
    *,
    offset: tuple[int, int],
    canvas_area: int,
    region_area: int,
    source: str,
) -> list[JsonDict]:
    reader = csv.DictReader(io.StringIO(tsv), delimiter="\t")
    grouped: dict[tuple[str, str, str, str], list[JsonDict]] = defaultdict(list)
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf", "-1"))
            left = int(row.get("left", "0"))
            top = int(row.get("top", "0"))
            width = int(row.get("width", "0"))
            height = int(row.get("height", "0"))
        except (TypeError, ValueError):
            continue
        if confidence < threshold or width <= 0 or height <= 0:
            continue
        key = tuple(
            row.get(name, "0")
            for name in ("page_num", "block_num", "par_num", "line_num")
        )
        grouped[key].append(
            {
                "confidence": confidence,
                "height": height,
                "left": left,
                "text": text,
                "top": top,
                "width": width,
            }
        )

    lines: list[JsonDict] = []
    for words in grouped.values():
        words.sort(key=lambda item: (item["left"], item["top"]))
        x1 = min(item["left"] for item in words)
        y1 = min(item["top"] for item in words)
        x2 = max(item["left"] + item["width"] for item in words)
        y2 = max(item["top"] + item["height"] for item in words)
        weight = sum(max(1, item["width"] * item["height"]) for item in words)
        confidence = sum(
            item["confidence"] * max(1, item["width"] * item["height"])
            for item in words
        ) / weight
        text = _join_words([item["text"] for item in words])
        box_area = max(1, (x2 - x1) * (y2 - y1))
        canvas_coverage = box_area / max(1, canvas_area)
        region_coverage = box_area / max(1, region_area)
        # Shapes frequently become a single punctuation token or an enormous,
        # weak OCR box. Such candidates must not erase otherwise valid geometry.
        meaningful_length = _meaningful_text_length(text)
        high_confidence_single = (
            meaningful_length == 1
            and len(text) == 1
            and text.isalnum()
            and confidence >= max(80.0, threshold)
        )
        if meaningful_length < 2 and not high_confidence_single:
            continue
        if canvas_coverage > 0.22:
            continue
        if region_coverage > 0.9 and confidence < 82.0:
            continue
        lines.append(
            {
                "confidence": confidence / 100.0,
                "height": y2 - y1,
                "source": source,
                "text": text,
                "width": x2 - x1,
                "word_count": len(words),
                "x": x1 + offset[0],
                "y": y1 + offset[1],
            }
        )
    return lines


def _bbox_overlap_ratio(first: Sequence[float], second: Sequence[float]) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    intersection = max(0.0, min(ax + aw, bx + bw) - max(ax, bx)) * max(
        0.0, min(ay + ah, by + bh) - max(ay, by)
    )
    return intersection / max(1.0, min(aw * ah, bw * bh))


def _deduplicate_ocr_lines(lines: Sequence[JsonDict]) -> list[JsonDict]:
    ordered = sorted(
        lines,
        key=lambda item: (
            -float(item["confidence"]),
            -float(item["width"] * item["height"]),
            item["y"],
            item["x"],
        ),
    )
    kept: list[JsonDict] = []
    for item in ordered:
        bbox = (item["x"], item["y"], item["width"], item["height"])
        if any(
            _bbox_overlap_ratio(
                bbox, (other["x"], other["y"], other["width"], other["height"])
            )
            >= 0.65
            for other in kept
        ):
            continue
        kept.append(item)
    kept.sort(key=lambda item: (item["y"], item["x"], item["text"]))
    return kept


def _run_ocr(
    rgb: "np.ndarray",
    shapes: Sequence[JsonDict],
    languages: str,
    threshold: float,
    timeout: float,
) -> tuple[list[JsonDict], JsonDict]:
    executable = shutil.which("tesseract")
    if executable is None:
        raise RuntimeError(
            "Tesseract was not found in PATH; install it or rerun with --no-ocr"
        )
    height, width = rgb.shape[:2]
    canvas_area = width * height
    messages: list[str] = []
    tsv, invocation_messages = _invoke_tesseract(
        executable, _image_png_bytes(rgb), languages, 11, timeout
    )
    messages.extend(invocation_messages)
    lines = _parse_tsv_lines(
        tsv,
        threshold,
        offset=(0, 0),
        canvas_area=canvas_area,
        region_area=canvas_area,
        source="global_psm_11",
    )
    single_char_fallback = False
    if not lines:
        fallback_tsv, fallback_messages = _invoke_tesseract(
            executable, _image_png_bytes(rgb), languages, 10, timeout
        )
        messages.extend(fallback_messages)
        fallback_lines = _parse_tsv_lines(
            fallback_tsv,
            threshold,
            offset=(0, 0),
            canvas_area=canvas_area,
            region_area=canvas_area,
            source="global_single_char_psm_10",
        )
        lines.extend(
            item
            for item in fallback_lines
            if _meaningful_text_length(item["text"]) == 1
            and float(item["confidence"]) >= 0.8
        )
        single_char_fallback = True

    crop_count = 0
    eligible = sorted(
        (
            shape
            for shape in shapes
            if shape["type"] in {"rectangle", "ellipse", "diamond", "hexagon"}
            and float(shape["width"]) >= 80
            and float(shape["height"]) >= 30
            and float(shape["width"] * shape["height"]) >= canvas_area * 0.008
        ),
        key=lambda shape: -(float(shape["width"]) * float(shape["height"])),
    )[:32]
    for shape in eligible:
        inset = max(
            3,
            int(round(float(shape["style"].get("stroke_width", 1.0)) * 2)),
            int(round(min(float(shape["width"]), float(shape["height"])) * 0.025)),
        )
        x1 = max(0, int(math.floor(float(shape["x"]))) + inset)
        y1 = max(0, int(math.floor(float(shape["y"]))) + inset)
        x2 = min(width, int(math.ceil(float(shape["x"] + shape["width"]))) - inset)
        y2 = min(height, int(math.ceil(float(shape["y"] + shape["height"]))) - inset)
        if x2 - x1 < 20 or y2 - y1 < 12:
            continue
        crop = rgb[y1:y2, x1:x2]
        crop_tsv, crop_messages = _invoke_tesseract(
            executable, _image_png_bytes(crop), languages, 7, timeout
        )
        messages.extend(crop_messages)
        lines.extend(
            _parse_tsv_lines(
                crop_tsv,
                threshold,
                offset=(x1, y1),
                canvas_area=canvas_area,
                region_area=(x2 - x1) * (y2 - y1),
                source=f"shape_crop_psm_7:{shape['id']}",
            )
        )
        crop_count += 1

    lines = _deduplicate_ocr_lines(lines)
    info: JsonDict = {
        "accepted_lines": len(lines),
        "accepted_words": sum(int(item["word_count"]) for item in lines),
        "confidence_threshold": threshold,
        "crop_invocations": crop_count,
        "enabled": True,
        "languages": languages,
        "psm": {"global": 11, "shape_crops": 7, "single_char_fallback": 10},
        "single_char_fallback_invoked": single_char_fallback,
        "timeout_seconds": timeout,
    }
    unique_messages = list(dict.fromkeys(messages))
    if unique_messages:
        info["messages"] = unique_messages
    return lines, info


def _composite_rgba(
    rgba: "np.ndarray", background: tuple[int, int, int]
) -> "np.ndarray":
    source = Image.fromarray(rgba, mode="RGBA")
    backdrop = Image.new("RGBA", source.size, (*background, 255))
    composited = Image.alpha_composite(backdrop, source).convert("RGB")
    return np.asarray(composited, dtype=np.uint8).copy()


def _angle_cosine(a: "np.ndarray", b: "np.ndarray", c: "np.ndarray") -> float:
    first = a.astype(np.float64) - b.astype(np.float64)
    second = c.astype(np.float64) - b.astype(np.float64)
    denominator = max(1e-9, float(np.linalg.norm(first) * np.linalg.norm(second)))
    return abs(float(np.dot(first, second)) / denominator)


def _is_axis_aligned(points: "np.ndarray", tolerance_degrees: float = 12.0) -> bool:
    tolerance = math.sin(math.radians(tolerance_degrees))
    flattened = points.reshape(-1, 2)
    for index in range(len(flattened)):
        delta = flattened[(index + 1) % len(flattened)] - flattened[index]
        length = float(np.linalg.norm(delta))
        if length < 1.0:
            continue
        if min(abs(float(delta[0])) / length, abs(float(delta[1])) / length) > tolerance:
            return False
    return True


def _cluster_colors(pixels: "np.ndarray") -> list[tuple["np.ndarray", int]]:
    if pixels.size == 0:
        return []
    rgb = pixels.reshape(-1, 3).astype(np.int16)
    buckets = (rgb // 20).astype(np.int16)
    keys, counts = np.unique(buckets, axis=0, return_counts=True)
    clusters: list[tuple["np.ndarray", int]] = []
    for key, count in zip(keys, counts):
        members = rgb[np.all(buckets == key, axis=1)]
        clusters.append((np.median(members, axis=0).astype(np.float32), int(count)))
    return clusters


def _dominant_cluster_color(
    pixels: "np.ndarray", fallback: Sequence[int | float]
) -> "np.ndarray":
    clusters = _cluster_colors(pixels)
    if not clusters:
        return np.asarray(fallback, dtype=np.float32)
    return max(clusters, key=lambda item: item[1])[0]


def _contrasting_cluster_color(
    pixels: "np.ndarray",
    reference: Sequence[int | float],
    fallback: Sequence[int | float],
) -> "np.ndarray":
    clusters = _cluster_colors(pixels)
    if not clusters:
        return np.asarray(fallback, dtype=np.float32)
    reference_array = np.asarray(reference, dtype=np.float32)
    minimum_count = max(2, int(pixels.reshape(-1, 3).shape[0] * 0.002))
    eligible = [cluster for cluster in clusters if cluster[1] >= minimum_count]
    if not eligible:
        eligible = clusters
    target_count = max(3.0, pixels.reshape(-1, 3).shape[0] * 0.04)
    return max(
        eligible,
        key=lambda item: (
            float(np.linalg.norm(item[0] - reference_array))
            * min(1.0, item[1] / target_count),
            math.log1p(item[1]),
        ),
    )[0]


def _sample_style(
    rgb: "np.ndarray",
    contour: "np.ndarray",
    background: tuple[int, int, int],
) -> JsonDict:
    height, width = rgb.shape[:2]
    x, y, box_width, box_height = cv2.boundingRect(contour)
    padding = 8
    left, top = max(0, x - padding), max(0, y - padding)
    right, bottom = min(width, x + box_width + padding), min(height, y + box_height + padding)
    roi = rgb[top:bottom, left:right]
    shifted = contour.astype(np.int32).copy()
    shifted[:, 0, 0] -= left
    shifted[:, 0, 1] -= top
    contour_mask = np.zeros(roi.shape[:2], dtype=np.uint8)
    cv2.drawContours(contour_mask, [shifted], -1, 255, thickness=1)
    interior_mask = np.zeros(roi.shape[:2], dtype=np.uint8)
    cv2.drawContours(interior_mask, [shifted], -1, 255, thickness=-1)
    if cv2.contourArea(contour) > 30:
        interior_mask = cv2.erode(interior_mask, np.ones((5, 5), np.uint8), iterations=1)
    interior_pixels = roi[interior_mask > 0]
    fill = _dominant_cluster_color(interior_pixels, background)
    distance = float(np.linalg.norm(fill.astype(float) - np.asarray(background, dtype=float)))
    reference = fill if distance >= 18.0 else np.asarray(background, dtype=np.float32)

    stroke_band = cv2.dilate(contour_mask, np.ones((13, 13), np.uint8), iterations=1)
    stroke_pixels = roi[stroke_band > 0]
    stroke = _contrasting_cluster_color(stroke_pixels, reference, background)
    stroke_distance = np.linalg.norm(
        roi.astype(np.float32) - stroke.reshape(1, 1, 3), axis=2
    )
    stroke_region = ((stroke_band > 0) & (stroke_distance <= 34.0)).astype(np.uint8)
    distance_map = cv2.distanceTransform(stroke_region, cv2.DIST_L2, 3)
    positive = distance_map[distance_map > 0]
    if len(positive):
        radius = float(np.percentile(positive, 98))
        stroke_width = max(1.0, min(16.0, round(max(1.0, 2.0 * radius - 1.0), 1)))
    else:
        stroke_width = 1.0
    return {
        "fill": "none" if distance < 18.0 else _hex(fill),
        "stroke": _hex(stroke),
        "stroke_width": stroke_width,
    }


def _bbox_iou(first: Sequence[float], second: Sequence[float]) -> float:
    ax1, ay1, aw, ah = first
    bx1, by1, bw, bh = second
    ax2, ay2, bx2, by2 = ax1 + aw, ay1 + ah, bx1 + bw, by1 + bh
    intersection = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(
        0.0, min(ay2, by2) - max(ay1, by1)
    )
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0 else 0.0


def _check_bbox_iou_regression() -> None:
    cases = (
        ((0.0, 0.0, 10.0, 10.0), (0.0, 0.0, 10.0, 10.0), 1.0),
        ((0.0, 0.0, 10.0, 10.0), (20.0, 20.0, 5.0, 5.0), 0.0),
        ((0.0, 0.0, 10.0, 10.0), (5.0, 5.0, 10.0, 10.0), 1.0 / 7.0),
    )
    try:
        valid = all(
            math.isclose(_bbox_iou(first, second), expected, rel_tol=1e-12, abs_tol=1e-12)
            for first, second, expected in cases
        )
    except Exception as exc:
        raise RuntimeError("internal bbox IoU regression check failed") from exc
    if not valid:
        raise RuntimeError("internal bbox IoU regression check failed")


def _rounded_edge_evidence(
    edges: "np.ndarray", x: int, y: int, width: int, height: int
) -> bool:
    # At small sizes anti-aliasing shortens even square edge runs by several
    # pixels, making corner-radius inference unreliable. Stay conservative.
    if min(width, height) < 50:
        return False

    def longest_run(values: "np.ndarray") -> int:
        longest = current = 0
        for value in values:
            current = current + 1 if bool(value) else 0
            longest = max(longest, current)
        return longest

    image_height, image_width = edges.shape
    band = 4
    ratios: list[float] = []
    for center_y in (y, y + height - 1):
        top, bottom = max(0, center_y - band), min(image_height, center_y + band + 1)
        left, right = max(0, x), min(image_width, x + width)
        rows = edges[top:bottom, left:right] > 0
        if rows.size:
            best_row = rows[int(np.argmax(np.count_nonzero(rows, axis=1)))]
            positions = np.where(best_row)[0]
            if len(positions) >= 2:
                ratios.append(float(longest_run(best_row)) / max(1, width))
    for center_x in (x, x + width - 1):
        left, right = max(0, center_x - band), min(image_width, center_x + band + 1)
        top, bottom = max(0, y), min(image_height, y + height)
        columns = edges[top:bottom, left:right] > 0
        if columns.size:
            best_column = columns[:, int(np.argmax(np.count_nonzero(columns, axis=0)))]
            positions = np.where(best_column)[0]
            if len(positions) >= 2:
                ratios.append(float(longest_run(best_column)) / max(1, height))
    return sum(ratio < 0.95 for ratio in ratios) >= 2


def _detect_shapes(
    rgb: "np.ndarray",
    background: tuple[int, int, int],
) -> tuple[list[JsonDict], list[JsonDict], "np.ndarray"]:
    height, width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, 45, 140, apertureSize=3)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    contours, hierarchy = cv2.findContours(
        edges, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE
    )
    minimum_area = max(120.0, width * height * 0.00025)
    raw: list[JsonDict] = []
    hierarchy_rows = hierarchy[0] if hierarchy is not None else None
    for contour_index, contour in enumerate(contours):
        area = float(abs(cv2.contourArea(contour)))
        perimeter = float(cv2.arcLength(contour, True))
        if area < minimum_area or perimeter < 24.0:
            continue
        x, y, box_width, box_height = cv2.boundingRect(contour)
        if box_width < 8 or box_height < 8:
            continue
        if (
            x <= 1
            and y <= 1
            and x + box_width >= width - 1
            and y + box_height >= height - 1
        ):
            continue
        box_area = float(box_width * box_height)
        extent = area / max(1.0, box_area)
        approximation = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        coarse = cv2.approxPolyDP(contour, 0.045 * perimeter, True)
        convex = bool(cv2.isContourConvex(approximation))
        circularity = 4.0 * math.pi * area / max(1.0, perimeter * perimeter)

        candidate_type: str | None = None
        confidence = 0.0
        rounded = False
        points: list[Point] | None = None

        if (
            len(approximation) >= 6
            and circularity >= 0.79
            and len(contour) >= 5
        ):
            (_center, axes, _rotation) = cv2.fitEllipse(contour)
            ellipse_area = math.pi * max(axes[0], 1.0) * max(axes[1], 1.0) / 4.0
            agreement = min(area, ellipse_area) / max(area, ellipse_area)
            if agreement >= 0.68:
                candidate_type = "ellipse"
                confidence = max(0.45, min(0.95, 0.45 + agreement * 0.4 + circularity * 0.1))
        if candidate_type is None and len(coarse) == 4 and cv2.isContourConvex(coarse):
            vertices = coarse.reshape(-1, 2)
            max_cosine = max(
                _angle_cosine(
                    vertices[(index - 1) % 4],
                    vertices[index],
                    vertices[(index + 1) % 4],
                )
                for index in range(4)
            )
            axis_aligned = _is_axis_aligned(coarse)
            if max_cosine <= 0.35 and axis_aligned:
                rounded = len(approximation) > 4 and extent < 0.965
                candidate_type = "rounded_rectangle" if rounded else "rectangle"
                confidence = max(0.45, min(0.96, 0.98 - max_cosine - abs(0.9 - extent) * 0.25))
            elif max_cosine <= 0.42:
                center = np.mean(vertices, axis=0)
                diagonal_score = float(
                    np.mean(
                        [
                            min(abs(point[0] - center[0]), abs(point[1] - center[1]))
                            for point in vertices
                        ]
                    )
                    / max(1.0, min(box_width, box_height))
                )
                if diagonal_score < 0.18:
                    candidate_type = "diamond"
                    confidence = max(0.45, min(0.91, 0.9 - max_cosine * 0.6))
                    points = [(float(px), float(py)) for px, py in vertices]
                else:
                    candidate_type = "polygon"
                    confidence = max(0.4, min(0.82, 0.78 - max_cosine * 0.4))
                    points = [(float(px), float(py)) for px, py in vertices]
        if (
            candidate_type is None
            and len(approximation) >= 6
            and circularity >= 0.56
            and len(contour) >= 5
        ):
            (_center, axes, _rotation) = cv2.fitEllipse(contour)
            ellipse_area = math.pi * max(axes[0], 1.0) * max(axes[1], 1.0) / 4.0
            agreement = min(area, ellipse_area) / max(area, ellipse_area)
            if agreement >= 0.68:
                candidate_type = "ellipse"
                confidence = max(0.45, min(0.95, 0.45 + agreement * 0.4 + circularity * 0.1))
        if candidate_type is None and convex and 3 <= len(approximation) <= 8:
            candidate_type = "polygon"
            confidence = max(0.38, min(0.78, 0.5 + extent * 0.25))
            points = [
                (float(point[0][0]), float(point[0][1])) for point in approximation
            ]

        if candidate_type is None:
            continue
        style = _sample_style(rgb, contour, background)
        if candidate_type in {"rectangle", "rounded_rectangle"}:
            rounded = rounded or _rounded_edge_evidence(
                edges, x, y, box_width, box_height
            )
            candidate_type = "rounded_rectangle" if rounded else "rectangle"
            style["rounded"] = rounded
        raw.append(
            {
                "bbox": (float(x), float(y), float(box_width), float(box_height)),
                "candidate_type": candidate_type,
                "confidence": confidence,
                "contour_index": contour_index,
                "parent_contour": (
                    int(hierarchy_rows[contour_index][3])
                    if hierarchy_rows is not None
                    else -1
                ),
                "points": points,
                "style": style,
            }
        )

    # Stroke edges normally appear as a nested parent/child contour pair. Keep
    # the larger representative when boxes strongly contain one another; the
    # size-ratio guard preserves genuinely nested diagram nodes.
    raw.sort(
        key=lambda item: (
            -(item["bbox"][2] * item["bbox"][3]),
            -item["confidence"],
            item["bbox"][1],
            item["bbox"][0],
        )
    )
    selected: list[JsonDict] = []
    for candidate in raw:
        candidate_area = candidate["bbox"][2] * candidate["bbox"][3]
        duplicate = False
        for kept in selected:
            kept_area = kept["bbox"][2] * kept["bbox"][3]
            size_ratio = min(candidate_area, kept_area) / max(candidate_area, kept_area)
            containment = _bbox_overlap_ratio(candidate["bbox"], kept["bbox"])
            related = (
                candidate["parent_contour"] == kept["contour_index"]
                or kept["parent_contour"] == candidate["contour_index"]
            )
            if size_ratio >= 0.55 and containment >= 0.86 and (
                related or _bbox_iou(candidate["bbox"], kept["bbox"]) >= 0.68
            ):
                duplicate = True
                break
        if duplicate:
            continue
        selected.append(candidate)

    retained: list[JsonDict] = []
    suppressed_fragments: list[tuple[JsonDict, JsonDict]] = []
    for candidate in selected:
        candidate_bbox = candidate["bbox"]
        candidate_area = candidate_bbox[2] * candidate_bbox[3]
        short_side = max(1.0, min(candidate_bbox[2], candidate_bbox[3]))
        slenderness = max(candidate_bbox[2], candidate_bbox[3]) / short_side
        containing_shape: JsonDict | None = None
        for outer in retained:
            outer_bbox = outer["bbox"]
            outer_area = outer_bbox[2] * outer_bbox[3]
            edge_tolerance = max(
                5.0, float(outer["style"].get("stroke_width", 1.0)) * 2.0 + 2.0
            )
            near_boundary = min(
                abs(candidate_bbox[0] - outer_bbox[0]),
                abs(candidate_bbox[0] + candidate_bbox[2] - outer_bbox[0] - outer_bbox[2]),
                abs(candidate_bbox[1] - outer_bbox[1]),
                abs(candidate_bbox[1] + candidate_bbox[3] - outer_bbox[1] - outer_bbox[3]),
            ) <= edge_tolerance
            hierarchy_related = candidate["parent_contour"] == outer["contour_index"]
            if (
                candidate["candidate_type"] in {"polygon", "ellipse"}
                and hierarchy_related
                and candidate_area / max(1.0, outer_area) <= 0.12
                and slenderness >= 4.0
                and near_boundary
                and _bbox_overlap_ratio(candidate_bbox, outer_bbox) >= 0.9
            ):
                containing_shape = outer
                break
        if containing_shape is None:
            retained.append(candidate)
        else:
            suppressed_fragments.append((candidate, containing_shape))
    selected = retained

    selected.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    elements: list[JsonDict] = []
    diagnostics: list[JsonDict] = []
    for index, candidate in enumerate(selected, start=1):
        x, y, box_width, box_height = candidate["bbox"]
        candidate_type = candidate["candidate_type"]
        if candidate_type == "rounded_rectangle":
            element_type = "rectangle"
        elif candidate_type == "polygon":
            # The shared converter intentionally supports only a small set of
            # regular primitives. Preserve the observed candidate in diagnostics
            # while emitting the closest supported, editable primitive.
            point_count = len(candidate["points"] or [])
            element_type = "hexagon" if point_count == 6 else "rectangle"
        else:
            element_type = candidate_type
        identifier = f"shape_{index:03d}"
        element: JsonDict = {
            "id": identifier,
            "style": candidate["style"],
            "text": "",
            "type": element_type,
            **_bbox_dict(x, y, box_width, box_height),
        }
        elements.append(element)
        diagnostic = {
            "candidate_type": candidate_type,
            "confidence": round(candidate["confidence"], 3),
            "element_id": identifier,
            "stroke_width_estimated": candidate["style"]["stroke_width"],
        }
        if element_type == "rectangle":
            diagnostic["rounded_detected"] = bool(candidate["style"].get("rounded", False))
        if element_type != candidate_type and candidate_type != "rounded_rectangle":
            diagnostic["mapped_type"] = element_type
        diagnostics.append(diagnostic)
    for index, (candidate, outer) in enumerate(suppressed_fragments, start=1):
        diagnostics.append(
            {
                "accepted": False,
                "candidate_type": candidate["candidate_type"],
                "confidence": round(candidate["confidence"], 3),
                "contained_by_candidate_type": outer["candidate_type"],
                "element_id": f"shape_rejected_{index:03d}",
                "rejected_reason": "slender_boundary_contour_fragment",
            }
        )
    return elements, diagnostics, edges


def _distance(point_a: Point, point_b: Point) -> float:
    return math.hypot(point_a[0] - point_b[0], point_a[1] - point_b[1])


def _segment_angle(segment: tuple[Point, Point]) -> float:
    return math.atan2(
        segment[1][1] - segment[0][1], segment[1][0] - segment[0][0]
    )


def _angle_difference(first: float, second: float) -> float:
    difference = abs(first - second) % math.pi
    return min(difference, math.pi - difference)


def _segment_is_shape_edge(
    segment: tuple[Point, Point], shapes: Sequence[JsonDict], tolerance: float = 14.0
) -> bool:
    (x1, y1), (x2, y2) = segment
    for shape in shapes:
        left, top = float(shape["x"]), float(shape["y"])
        right, bottom = left + float(shape["width"]), top + float(shape["height"])
        if not (
            min(x1, x2) >= left - tolerance
            and max(x1, x2) <= right + tolerance
            and min(y1, y2) >= top - tolerance
            and max(y1, y2) <= bottom + tolerance
        ):
            continue
        # A Hough segment fully contained by a detected shape is normally one of
        # its straight/curved boundary fragments. Connectors crossing a shape's
        # box have an endpoint outside and therefore survive this conservative
        # filter.
        fully_contained = (
            min(x1, x2) >= left - tolerance
            and max(x1, x2) <= right + tolerance
            and min(y1, y2) >= top - tolerance
            and max(y1, y2) <= bottom + tolerance
        )
        if fully_contained:
            return True
        horizontal = abs(y1 - y2) <= tolerance and (
            abs((y1 + y2) / 2 - top) <= tolerance
            or abs((y1 + y2) / 2 - bottom) <= tolerance
        )
        vertical = abs(x1 - x2) <= tolerance and (
            abs((x1 + x2) / 2 - left) <= tolerance
            or abs((x1 + x2) / 2 - right) <= tolerance
        )
        if horizontal or vertical:
            return True
    return False


def _deduplicate_segments(
    segments: Iterable[tuple[Point, Point]], minimum_length: float
) -> list[tuple[Point, Point]]:
    ordered = sorted(segments, key=lambda item: -_distance(item[0], item[1]))
    kept: list[tuple[Point, Point]] = []
    for segment in ordered:
        if _distance(segment[0], segment[1]) < minimum_length:
            continue
        angle = _segment_angle(segment)
        duplicate = False
        for existing in kept:
            if _angle_difference(angle, _segment_angle(existing)) > math.radians(6):
                continue
            # Treat substantially overlapping, nearly parallel detections as
            # the two Canny sides of the same visual stroke.
            ex0 = np.asarray(existing[0], dtype=np.float64)
            ex1 = np.asarray(existing[1], dtype=np.float64)
            direction = ex1 - ex0
            existing_length = float(np.linalg.norm(direction))
            if existing_length > 0:
                unit = direction / existing_length
                normal = np.asarray((-unit[1], unit[0]))
                candidate_points = [np.asarray(point, dtype=np.float64) for point in segment]
                perpendicular = max(
                    abs(float(np.dot(point - ex0, normal))) for point in candidate_points
                )
                projections = [float(np.dot(point - ex0, unit)) for point in candidate_points]
                overlap = max(
                    0.0,
                    min(existing_length, max(projections)) - max(0.0, min(projections)),
                )
                shorter = min(existing_length, _distance(segment[0], segment[1]))
                if perpendicular <= 5.0 and overlap >= shorter * 0.55:
                    duplicate = True
                    break
            direct = _distance(segment[0], existing[0]) + _distance(segment[1], existing[1])
            reverse = _distance(segment[0], existing[1]) + _distance(segment[1], existing[0])
            if min(direct, reverse) <= max(8.0, minimum_length * 0.45):
                duplicate = True
                break
        if not duplicate:
            kept.append(segment)
    return kept


def _segments_to_paths(
    segments: list[tuple[Point, Point]], join_distance: float
) -> list[list[Point]]:
    unused = list(segments)
    paths: list[list[Point]] = []
    while unused:
        first = unused.pop(0)
        path = [first[0], first[1]]
        changed = True
        while changed:
            changed = False
            best: tuple[float, int, str, bool] | None = None
            for index, segment in enumerate(unused):
                options = (
                    (_distance(path[-1], segment[0]), "back", False),
                    (_distance(path[-1], segment[1]), "back", True),
                    (_distance(path[0], segment[1]), "front", False),
                    (_distance(path[0], segment[0]), "front", True),
                )
                distance, end, reverse = min(options, key=lambda item: item[0])
                if distance <= join_distance and (best is None or distance < best[0]):
                    best = (distance, index, end, reverse)
            if best is None:
                continue
            _gap, index, end, reverse = best
            segment = unused.pop(index)
            start, finish = segment
            if reverse:
                start, finish = finish, start
            if end == "back":
                path.append(finish)
            else:
                path.insert(0, start)
            changed = True

        # Remove nearly-collinear middle points, preserving genuine corners.
        simplified: list[Point] = []
        for point in path:
            if simplified and _distance(simplified[-1], point) < 2.0:
                continue
            simplified.append(point)
        index = 1
        while index < len(simplified) - 1:
            first_angle = _segment_angle((simplified[index - 1], simplified[index]))
            second_angle = _segment_angle((simplified[index], simplified[index + 1]))
            if _angle_difference(first_angle, second_angle) < math.radians(5):
                simplified.pop(index)
            else:
                index += 1
        paths.append(simplified)
    return paths


def _line_style(rgb: "np.ndarray", points: Sequence[Point]) -> JsonDict:
    height, width = rgb.shape[:2]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    padding = 8
    left = max(0, int(math.floor(min(xs))) - padding)
    top = max(0, int(math.floor(min(ys))) - padding)
    right = min(width, int(math.ceil(max(xs))) + padding + 1)
    bottom = min(height, int(math.ceil(max(ys))) + padding + 1)
    roi = rgb[top:bottom, left:right]
    core = np.zeros(roi.shape[:2], dtype=np.uint8)
    integer_points = np.asarray(
        [[int(round(x)) - left, int(round(y)) - top] for x, y in points],
        dtype=np.int32,
    )
    cv2.polylines(core, [integer_points], False, 255, thickness=3)
    outer = cv2.dilate(core, np.ones((13, 13), np.uint8), iterations=1)
    inner = cv2.dilate(core, np.ones((5, 5), np.uint8), iterations=1)
    ring = (outer > 0) & (inner == 0)
    local_background = _dominant_cluster_color(roi[ring], (255, 255, 255))
    stroke = _contrasting_cluster_color(roi[core > 0], local_background, (0, 0, 0))
    width_samples: list[int] = []
    for start, finish in zip(points, points[1:]):
        delta_x, delta_y = finish[0] - start[0], finish[1] - start[1]
        length = math.hypot(delta_x, delta_y)
        if length < 8:
            continue
        normal_x, normal_y = -delta_y / length, delta_x / length
        middle_x = (start[0] + finish[0]) / 2.0 - left
        middle_y = (start[1] + finish[1]) / 2.0 - top
        matches: list[bool] = []
        for offset in range(-12, 13):
            sample_x = int(round(middle_x + normal_x * offset))
            sample_y = int(round(middle_y + normal_y * offset))
            if 0 <= sample_x < roi.shape[1] and 0 <= sample_y < roi.shape[0]:
                distance = float(
                    np.linalg.norm(roi[sample_y, sample_x].astype(np.float32) - stroke)
                )
                matches.append(distance <= 38.0)
            else:
                matches.append(False)
        longest = current = 0
        for matches_stroke in matches:
            current = current + 1 if matches_stroke else 0
            longest = max(longest, current)
        if longest:
            width_samples.append(longest)
    stroke_width = (
        max(1.0, min(16.0, round(float(np.median(width_samples)), 1)))
        if width_samples
        else 1.0
    )
    return {"stroke": _hex(stroke), "stroke_width": stroke_width}


def _endpoint_has_arrow(
    edges: "np.ndarray",
    rgb: "np.ndarray",
    stroke_hex: str,
    endpoint: Point,
    adjacent: Point,
) -> bool:
    height, width = edges.shape
    radius = max(12, int(round(min(width, height) * 0.045)))
    center_x, center_y = int(round(endpoint[0])), int(round(endpoint[1]))
    left, top = max(0, center_x - radius), max(0, center_y - radius)
    right, bottom = min(width, center_x + radius + 1), min(height, center_y + radius + 1)
    roi = edges[top:bottom, left:right]
    detected = cv2.HoughLinesP(
        roi,
        1,
        np.pi / 180.0,
        threshold=5,
        minLineLength=max(5, radius // 3),
        maxLineGap=3,
    )
    if detected is None:
        return False
    shaft_angle = _segment_angle((adjacent, endpoint))
    local_endpoint = (endpoint[0] - left, endpoint[1] - top)
    side_angles: list[float] = []
    for x1, y1, x2, y2 in detected[:, 0, :]:
        segment = ((float(x1), float(y1)), (float(x2), float(y2)))
        angle = _segment_angle(segment)
        difference = _angle_difference(angle, shaft_angle)
        if not math.radians(18) <= difference <= math.radians(78):
            continue
        if min(
            _distance(local_endpoint, segment[0]),
            _distance(local_endpoint, segment[1]),
        ) <= radius * 0.9:
            side_angles.append(angle)
    if len(side_angles) < 2:
        return False
    signed = [math.sin(angle - shaft_angle) for angle in side_angles]
    if not (any(value > 0.2 for value in signed) and any(value < -0.2 for value in signed)):
        return False
    direction = np.asarray(endpoint, dtype=np.float32) - np.asarray(adjacent, dtype=np.float32)
    direction /= max(1e-6, float(np.linalg.norm(direction)))
    normal = np.asarray((-direction[1], direction[0]), dtype=np.float32)
    stroke = np.asarray(
        [int(stroke_hex[index : index + 2], 16) for index in (1, 3, 5)],
        dtype=np.float32,
    )
    matches = total = 0
    density_radius = max(16, radius)
    for sample_y in range(max(0, center_y - density_radius), min(height, center_y + density_radius + 1)):
        for sample_x in range(max(0, center_x - density_radius), min(width, center_x + density_radius + 1)):
            delta = np.asarray((sample_x - endpoint[0], sample_y - endpoint[1]), dtype=np.float32)
            forward = float(np.dot(delta, direction))
            sideways = abs(float(np.dot(delta, normal)))
            if 0.0 <= forward <= density_radius and sideways <= density_radius * 0.58:
                total += 1
                if float(np.linalg.norm(rgb[sample_y, sample_x].astype(np.float32) - stroke)) <= 42.0:
                    matches += 1
    return total > 0 and matches / total >= 0.16


def _point_to_segment_distance(point: Point, start: Point, finish: Point) -> float:
    point_array = np.asarray(point, dtype=np.float64)
    start_array = np.asarray(start, dtype=np.float64)
    finish_array = np.asarray(finish, dtype=np.float64)
    delta = finish_array - start_array
    denominator = float(np.dot(delta, delta))
    if denominator <= 1e-9:
        return float(np.linalg.norm(point_array - start_array))
    position = max(0.0, min(1.0, float(np.dot(point_array - start_array, delta)) / denominator))
    projection = start_array + position * delta
    return float(np.linalg.norm(point_array - projection))


def _detect_lines(
    rgb: "np.ndarray",
    edges: "np.ndarray",
    shapes: Sequence[JsonDict],
) -> tuple[list[JsonDict], list[JsonDict]]:
    height, width = edges.shape
    minimum_length = max(12.0, min(width, height) * 0.035)
    threshold = max(12, int(minimum_length * 0.8))
    detected = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180.0,
        threshold=threshold,
        minLineLength=int(minimum_length),
        maxLineGap=max(3, int(min(width, height) * 0.012)),
    )
    if detected is None:
        return [], []
    segments: list[tuple[Point, Point]] = []
    for values in detected[:, 0, :]:
        segment = (
            (float(values[0]), float(values[1])),
            (float(values[2]), float(values[3])),
        )
        if not _segment_is_shape_edge(segment, shapes):
            segments.append(segment)
    segments = _deduplicate_segments(segments, minimum_length)
    paths = _segments_to_paths(segments, max(5.0, min(width, height) * 0.012))
    paths.sort(key=lambda path: (min(point[1] for point in path), min(point[0] for point in path)))

    elements: list[JsonDict] = []
    diagnostics: list[JsonDict] = []
    for index, points in enumerate(paths, start=1):
        if len(points) < 2:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        x, y = min(xs), min(ys)
        box_width, box_height = max(xs) - x, max(ys) - y
        identifier = f"line_{index:03d}"
        kind = "polyline" if len(points) > 2 else "line_segment"
        total_length = sum(_distance(a, b) for a, b in zip(points, points[1:]))
        confidence = min(0.86, 0.5 + total_length / max(width, height) * 0.18)
        style = _line_style(rgb, points)
        start_arrow = _endpoint_has_arrow(
            edges, rgb, style["stroke"], points[0], points[1]
        )
        end_arrow = _endpoint_has_arrow(
            edges, rgb, style["stroke"], points[-1], points[-2]
        )
        element: JsonDict = {
            "height": _round(box_height),
            "id": identifier,
            "points": [_point_value(point) for point in points],
            "style": style,
            "text": "",
            "type": "line",
            "width": _round(box_width),
            "x": _round(x),
            "y": _round(y),
        }
        if start_arrow:
            element["start_arrow"] = "classic"
        if end_arrow:
            element["end_arrow"] = "classic"
        elements.append(element)
        diagnostics.append(
            {
                "arrow_detection": (
                    "both"
                    if start_arrow and end_arrow
                    else "start"
                    if start_arrow
                    else "end"
                    if end_arrow
                    else "none"
                ),
                "candidate_type": kind,
                "confidence": round(confidence, 3),
                "element_id": identifier,
                "stroke_width_estimated": style["stroke_width"],
            }
        )

    lengths = {
        element["id"]: sum(
            _distance(tuple(first), tuple(second))
            for first, second in zip(element["points"], element["points"][1:])
        )
        for element in elements
    }
    rejected_ids: dict[str, str] = {}
    for short in elements:
        short_length = lengths[short["id"]]
        short_points = [tuple(point) for point in short["points"]]
        for long in elements:
            long_length = lengths[long["id"]]
            if short["id"] == long["id"] or short_length >= long_length * 0.45:
                continue
            long_points = [tuple(point) for point in long["points"]]
            near_path = all(
                min(
                    _point_to_segment_distance(point, start, finish)
                    for start, finish in zip(long_points, long_points[1:])
                )
                <= 7.0
                for point in short_points
            )
            arrow_endpoints: list[Point] = []
            if long.get("start_arrow"):
                arrow_endpoints.append(long_points[0])
            if long.get("end_arrow"):
                arrow_endpoints.append(long_points[-1])
            near_arrow = any(
                min(_distance(point, arrow) for point in short_points) <= 24.0
                for arrow in arrow_endpoints
            )
            if near_path or near_arrow:
                rejected_ids[short["id"]] = (
                    "duplicate_stroke_fragment" if near_path else "arrowhead_component"
                )
                break
    if rejected_ids:
        elements = [element for element in elements if element["id"] not in rejected_ids]
        for diagnostic in diagnostics:
            if diagnostic["element_id"] in rejected_ids:
                diagnostic["accepted"] = False
                diagnostic["rejected_reason"] = rejected_ids[diagnostic["element_id"]]
    return elements, diagnostics


def _intersection_area(first: Sequence[float], second: Sequence[float]) -> float:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return max(0.0, min(ax + aw, bx + bw) - max(ax, bx)) * max(
        0.0, min(ay + ah, by + bh) - max(ay, by)
    )


def _reconcile_geometry_with_text(
    shapes: list[JsonDict],
    shape_diagnostics: list[JsonDict],
    lines: list[JsonDict],
    line_diagnostics: list[JsonDict],
    text_lines: Sequence[JsonDict],
) -> tuple[list[JsonDict], list[JsonDict], list[JsonDict], list[JsonDict], list[JsonDict]]:
    text_boxes = [
        (float(item["x"]), float(item["y"]), float(item["width"]), float(item["height"]))
        for item in text_lines
    ]
    diagnostics_by_id = {
        item["element_id"]: item for item in shape_diagnostics + line_diagnostics
    }
    rejected: list[JsonDict] = [
        item for item in shape_diagnostics + line_diagnostics if item.get("accepted") is False
    ]
    kept_shapes: list[JsonDict] = []
    kept_shape_diagnostics: list[JsonDict] = []
    for shape in shapes:
        bbox = (
            float(shape["x"]),
            float(shape["y"]),
            float(shape["width"]),
            float(shape["height"]),
        )
        area = max(1.0, bbox[2] * bbox[3])
        is_glyph = any(
            _intersection_area(bbox, text_box) / area >= 0.62
            and area <= max(1.0, text_box[2] * text_box[3]) * 0.55
            for text_box in text_boxes
        )
        diagnostic = diagnostics_by_id[shape["id"]]
        if is_glyph:
            rejected.append(
                {
                    **diagnostic,
                    "accepted": False,
                    "rejected_reason": "overlaps_ocr_text_glyph_region",
                }
            )
        else:
            kept_shapes.append(shape)
            kept_shape_diagnostics.append(diagnostic)

    kept_lines: list[JsonDict] = []
    kept_line_diagnostics: list[JsonDict] = []
    for line in lines:
        points = [(float(point[0]), float(point[1])) for point in line["points"]]
        total_length = sum(_distance(a, b) for a, b in zip(points, points[1:]))
        is_glyph = False
        for tx, ty, tw, th in text_boxes:
            padding = max(2.0, th * 0.12)
            if all(
                tx - padding <= px <= tx + tw + padding
                and ty - padding <= py <= ty + th + padding
                for px, py in points
            ) and total_length <= max(tw, th) * 1.8:
                is_glyph = True
                break
        diagnostic = diagnostics_by_id[line["id"]]
        if is_glyph:
            rejected.append(
                {
                    **diagnostic,
                    "accepted": False,
                    "rejected_reason": "overlaps_ocr_text_glyph_region",
                }
            )
        else:
            kept_lines.append(line)
            kept_line_diagnostics.append(diagnostic)
    return (
        kept_shapes,
        kept_shape_diagnostics,
        kept_lines,
        kept_line_diagnostics,
        rejected,
    )


def _text_color(
    rgb: "np.ndarray", item: JsonDict, background: tuple[int, int, int]
) -> str:
    height, width = rgb.shape[:2]
    x1 = max(0, int(item["x"]))
    y1 = max(0, int(item["y"]))
    x2 = min(width, int(item["x"] + item["width"]))
    y2 = min(height, int(item["y"] + item["height"]))
    foreground_pixels = rgb[y1:y2, x1:x2].reshape(-1, 3)
    if len(foreground_pixels) == 0:
        return "#000000"
    padding = max(3, int(round(max(1, y2 - y1) * 0.28)))
    outer_x1, outer_y1 = max(0, x1 - padding), max(0, y1 - padding)
    outer_x2, outer_y2 = min(width, x2 + padding), min(height, y2 + padding)
    expanded = rgb[outer_y1:outer_y2, outer_x1:outer_x2]
    ring_mask = np.ones(expanded.shape[:2], dtype=bool)
    ring_mask[y1 - outer_y1 : y2 - outer_y1, x1 - outer_x1 : x2 - outer_x1] = False
    local_background = _dominant_cluster_color(expanded[ring_mask], background)
    foreground = _contrasting_cluster_color(
        foreground_pixels, local_background, (0, 0, 0)
    )
    return _hex(foreground)


def _text_elements(
    lines: Sequence[JsonDict],
    rgb: "np.ndarray",
    background: tuple[int, int, int],
) -> tuple[list[JsonDict], list[JsonDict]]:
    elements: list[JsonDict] = []
    diagnostics: list[JsonDict] = []
    for index, item in enumerate(lines, start=1):
        identifier = f"text_{index:03d}"
        font_size = max(6.0, float(item["height"]) * 0.82)
        elements.append(
            {
                "id": identifier,
                "style": {
                    "align": "left",
                    "font_color": _text_color(rgb, item, background),
                    "font_family": "sans-serif",
                    "font_size": _round(font_size),
                    "vertical_align": "middle",
                },
                "text": item["text"],
                "type": "text",
                **_bbox_dict(item["x"], item["y"], item["width"], item["height"]),
            }
        )
        diagnostics.append(
            {
                "candidate_type": "ocr_text",
                "confidence": round(float(item["confidence"]), 3),
                "element_id": identifier,
                "ocr_source": item.get("source", "unknown"),
            }
        )
    return elements, diagnostics


def _render_overlay(rgba: "np.ndarray", elements: Sequence[JsonDict]) -> bytes:
    image = Image.fromarray(rgba, mode="RGBA")
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    colors = {
        "diamond": (255, 128, 0, 220),
        "ellipse": (160, 32, 240, 220),
        "line": (0, 170, 255, 230),
        "polygon": (255, 64, 64, 220),
        "rectangle": (0, 200, 90, 220),
        "text": (255, 40, 180, 220),
    }
    for element in elements:
        color = colors.get(element["type"], (255, 220, 0, 220))
        if element["type"] == "line":
            points = [(item[0], item[1]) for item in element["points"]]
            draw.line(points, fill=color, width=2, joint="curve")
            label_x, label_y = points[0]
        else:
            x1, y1 = element["x"], element["y"]
            x2, y2 = x1 + element["width"], y1 + element["height"]
            draw.rectangle((x1, y1, x2, y2), outline=color, width=2)
            label_x, label_y = x1, y1
        label = f"{element['id']}:{element['type']}"
        text_box = draw.textbbox((label_x, label_y), label, font=font)
        draw.rectangle(text_box, fill=(255, 255, 255, 210))
        draw.text((label_x, label_y), label, fill=color, font=font)
    result = Image.alpha_composite(image, overlay)
    stream = io.BytesIO()
    result.save(stream, format="PNG")
    return stream.getvalue()


def _atomic_publish(artifacts: Sequence[tuple[Path, bytes]]) -> None:
    """Publish all artifacts together, restoring previous files on failure."""
    destinations = [path.resolve() for path, _payload in artifacts]
    if len(set(destinations)) != len(destinations):
        raise ValueError("output artifact paths must be distinct")
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    published: list[Path] = []
    try:
        for destination, payload in artifacts:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            staged[destination] = temporary
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        for destination, _payload in artifacts:
            if destination.exists():
                descriptor, backup_name = tempfile.mkstemp(
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".backup",
                )
                os.close(descriptor)
                backup = Path(backup_name)
                backup.unlink()
                os.replace(destination, backup)
                backups[destination] = backup
        for destination, _payload in artifacts:
            os.replace(staged[destination], destination)
            published.append(destination)
    except Exception:
        for destination in reversed(published):
            destination.unlink(missing_ok=True)
        for destination, backup in backups.items():
            if backup.exists():
                os.replace(backup, destination)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def analyze(
    input_path: Path,
    *,
    run_ocr: bool,
    languages: str,
    confidence_threshold: float,
    ocr_timeout: float,
    max_pixels: int,
) -> tuple[JsonDict, "np.ndarray"]:
    if not input_path.exists():
        raise ValueError(f"input file does not exist: {input_path}")
    if not input_path.is_file():
        raise ValueError(f"input path is not a file: {input_path}")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(input_path) as opened:
                if opened.format != "PNG":
                    raise ValueError(
                        f"input must be a PNG image (detected {opened.format or 'unknown'})"
                    )
                source_width, source_height = opened.size
                pixel_count = source_width * source_height
                if pixel_count > max_pixels:
                    raise ValueError(
                        f"input image has {pixel_count:,} pixels, exceeding "
                        f"--max-pixels {max_pixels:,}"
                    )
                image = opened.convert("RGBA")
                rgba = np.asarray(image, dtype=np.uint8).copy()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError(f"input PNG exceeds Pillow's safe decompression limit: {exc}") from exc
    except UnidentifiedImageError as exc:
        raise ValueError(f"input is not a readable PNG image: {input_path}") from exc
    except OSError as exc:
        raise ValueError(f"could not read input image {input_path}: {exc}") from exc

    height, width = rgba.shape[:2]
    if width <= 0 or height <= 0:
        raise ValueError("input image has invalid dimensions")
    transparent = bool(np.any(rgba[:, :, 3] < 255))
    detection_background = (
        _transparent_detection_background(rgba)
        if transparent
        else _dominant_background(rgba)
    )
    scene_background = "none" if transparent else _hex(detection_background)
    rgb = _composite_rgba(rgba, detection_background)
    palette = _extract_palette(rgba, detection_background)

    # Geometry is detected from the complete normalized image. OCR is reconciled
    # afterwards, so a mistaken text box can never erase a real shape or edge.
    shape_elements, shape_diagnostics, edges = _detect_shapes(
        rgb, detection_background
    )
    line_elements, line_diagnostics = _detect_lines(rgb, edges, shape_elements)

    if run_ocr:
        ocr_lines, ocr_info = _run_ocr(
            rgb,
            shape_elements,
            languages,
            confidence_threshold,
            ocr_timeout,
        )
    else:
        ocr_lines = []
        ocr_info = {
            "accepted_lines": 0,
            "accepted_words": 0,
            "confidence_threshold": confidence_threshold,
            "enabled": False,
            "languages": languages,
            "timeout_seconds": ocr_timeout,
        }
    (
        shape_elements,
        shape_diagnostics,
        line_elements,
        line_diagnostics,
        rejected_geometry,
    ) = _reconcile_geometry_with_text(
        shape_elements,
        shape_diagnostics,
        line_elements,
        line_diagnostics,
        ocr_lines,
    )
    text_elements, text_diagnostics = _text_elements(
        ocr_lines, rgb, detection_background
    )

    # Reading order for text and spatial order for geometry are stable within
    # each category. Text is emitted last so later renderers naturally place it
    # above fills without needing a separate z-index convention.
    elements = shape_elements + line_elements + text_elements
    candidate_diagnostics = (
        shape_diagnostics + line_diagnostics + text_diagnostics
    )
    counts = Counter(item["candidate_type"] for item in candidate_diagnostics)
    scene: JsonDict = {
        "canvas": {
            "background": scene_background,
            "height": height,
            "transparent": transparent,
            "width": width,
        },
        "diagnostics": {
            "candidates": candidate_diagnostics,
            "counts_by_candidate_type": dict(sorted(counts.items())),
            "detection_background": _hex(detection_background),
            "max_pixels": max_pixels,
            "notes": [
                "Candidates are geometric/OCR estimates, not inferred diagram semantics.",
                "Coordinates are in source-image pixels.",
                "Geometry is detected independently before OCR reconciliation.",
            ],
            "ocr": ocr_info,
            "palette": palette,
            "rejected_candidates": rejected_geometry,
        },
        "elements": elements,
        "source": {"path": str(input_path.resolve())},
        "version": 1,
    }
    return scene, rgba


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0.0 <= args.confidence_threshold <= 100.0:
        parser.error("--confidence-threshold must be between 0 and 100")
    if not args.no_ocr and not args.languages.strip():
        parser.error("--languages cannot be empty unless --no-ocr is used")
    if args.ocr_timeout <= 0:
        parser.error("--ocr-timeout must be greater than zero")
    if args.max_pixels <= 0:
        parser.error("--max-pixels must be greater than zero")
    if args.output.resolve() == args.input.resolve():
        parser.error("output JSON path must differ from the input PNG path")
    if args.overlay is not None and args.overlay.resolve() == args.input.resolve():
        parser.error("--overlay path must differ from the input PNG path")
    if args.overlay is not None and args.overlay.resolve() == args.output.resolve():
        parser.error("--overlay path must differ from the output JSON path")

    try:
        _check_bbox_iou_regression()
        scene, rgba = analyze(
            args.input,
            run_ocr=not args.no_ocr,
            languages=args.languages,
            confidence_threshold=args.confidence_threshold,
            ocr_timeout=args.ocr_timeout,
            max_pixels=args.max_pixels,
        )
        artifacts = [
            (
                args.output,
                (json.dumps(scene, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
        ]
        if args.overlay is not None:
            artifacts.append((args.overlay, _render_overlay(rgba, scene["elements"])))
        _atomic_publish(artifacts)
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(1, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Safely validate canonical, editable, vector-only draw.io XML."""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote

from defusedxml import ElementTree as SafeET  # type: ignore[import-not-found]
from defusedxml.common import DefusedXmlException  # type: ignore[import-not-found]


MAX_XML_BYTES = 20 * 1024 * 1024
FORBIDDEN_DECL_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
ENCODING_RE = re.compile(r"^\s*<\?xml\s+[^>]*encoding\s*=\s*(['\"])([^'\"]+)\1", re.IGNORECASE)
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
UNSAFE_CONTENT_RE = re.compile(
    r"(?<![A-Za-z0-9_])data\s*:(?:[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+)?(?:;[^,\s]*)?,|"
    r"<\s*/?\s*(?:img|svg|image|foreignobject|object|embed|iframe)\b|"
    r"(?<![A-Za-z0-9_])(?:https?|ftp|file|javascript|vbscript|blob)\s*:\s*(?://)?|"
    r"(?<!:)//(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:[/:?#][^\s<]*)?|"
    r"url\s*\(|@import\b|iVBORw0KGgo|R0lGOD(?:lh|dh)|"
    r"/9j/[A-Za-z0-9+/]|PHN2Zy[A-Za-z0-9+/=]",
    re.IGNORECASE,
)
IMAGE_TAGS = {"img", "image", "svg", "foreignobject", "embed", "iframe"}
WRAPPER_TAGS = {"object", "userobject"}
LINK_ATTRIBUTE_NAMES = {"href", "src", "link", "url", "image", "backgroundimage"}
IMMUTABLE_FALSE_PROPERTIES = {
    "editable",
    "movable",
    "resizable",
    "deletable",
    "cloneable",
    "rotatable",
    "bendable",
    "labelMovable",
}
IMMUTABLE_TRUE_PROPERTIES = {"locked"}


def _empty_report(path: str) -> dict[str, Any]:
    return {
        "path": path,
        "valid": False,
        "errors": [],
        "warnings": [],
        "canvas": None,
        "stats": {"cells": 0, "vertices": 0, "edges": 0, "text_cells": 0, "image_cells": 0},
    }


def _add(items: list[str], message: str) -> None:
    if message not in items:
        items.append(message)


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _style_map(style: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in style.split(";"):
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
        else:
            result[part.strip()] = "1"
    return result


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].lower()


def _decode_layers(value: str) -> str:
    """Decode nested HTML entities and percent escapes used to hide active content."""

    decoded = value
    for _ in range(32):
        next_value = html.unescape(unquote(decoded))
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _contains_unsafe_content(value: str) -> bool:
    decoded = value
    for _ in range(32):
        next_value = html.unescape(unquote(decoded))
        if next_value == decoded:
            return bool(UNSAFE_CONTENT_RE.search(decoded))
        decoded = next_value
    # Excessive nested escaping is unnecessary in canonical draw.io labels and
    # is conservatively rejected instead of allowing an encoding-depth bypass.
    return True


def _is_false(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"0", "false", "no"}


def _is_true(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes"}


def _point_in_bounds(x: float, y: float, width: float, height: float) -> bool:
    epsilon = 1e-6
    return -epsilon <= x <= width + epsilon and -epsilon <= y <= height + epsilon


def _rotated_bounds(
    x: float,
    y: float,
    item_width: float,
    item_height: float,
    rotation: float,
) -> tuple[float, float, float, float]:
    """Return axis-aligned bounds for a center-rotated vertex."""

    radians = math.radians(math.fmod(rotation, 360.0))
    cosine = abs(math.cos(radians))
    sine = abs(math.sin(radians))
    half_width = (item_width * cosine + item_height * sine) / 2.0
    half_height = (item_width * sine + item_height * cosine) / 2.0
    center_x = x + item_width / 2.0
    center_y = y + item_height / 2.0
    return (
        center_x - half_width,
        center_y - half_height,
        center_x + half_width,
        center_y + half_height,
    )


def _parse_utf8_xml(data: bytes) -> Any:
    if len(data) > MAX_XML_BYTES:
        raise ValueError(f"XML is too large ({len(data)} bytes; maximum is {MAX_XML_BYTES})")
    if data.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")) or b"\x00" in data:
        raise ValueError("only UTF-8 XML is supported; UTF-16/UTF-32 and NUL bytes are rejected")
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"only valid UTF-8 XML is supported: {exc}") from exc
    encoding_match = ENCODING_RE.search(decoded)
    if encoding_match and encoding_match.group(2).lower().replace("_", "-") not in {"utf-8", "utf8", "us-ascii", "ascii"}:
        raise ValueError(f"unsupported XML encoding declaration: {encoding_match.group(2)!r}; use UTF-8")
    if FORBIDDEN_DECL_RE.search(decoded):
        raise ValueError("DTD and ENTITY declarations are forbidden")
    try:
        return SafeET.fromstring(data)
    except DefusedXmlException as exc:
        raise ValueError(f"unsafe XML construct rejected: {exc}") from exc
    except SafeET.ParseError as exc:
        raise ValueError(f"malformed XML: {exc}") from exc


def _read_xml(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat {path}: {exc}") from exc
    if size > MAX_XML_BYTES:
        raise ValueError(f"XML is too large ({size} bytes; maximum is {MAX_XML_BYTES})")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc


def _scan_active_content(document: Any, errors: list[str]) -> None:
    for node in document.iter():
        tag = _local_name(str(node.tag))
        if tag in IMAGE_TAGS:
            _add(errors, f"image/SVG element <{tag}> is forbidden")
        if tag in WRAPPER_TAGS:
            _add(errors, f"draw.io wrapper element <{tag}> is forbidden; mxCell nodes must be direct")
        if node.text and _contains_unsafe_content(node.text):
            _add(errors, f"element <{tag}> contains image, SVG, URL, or external-link content")
        if node.tail and _contains_unsafe_content(node.tail):
            _add(errors, f"content after <{tag}> contains image, SVG, URL, or external-link content")
        for name, value in node.attrib.items():
            local_attribute = _local_name(name).replace("_", "").lower()
            if local_attribute in LINK_ATTRIBUTE_NAMES and value.strip():
                _add(errors, f"attribute {name!r} on <{tag}> is an image/link embedding and is forbidden")
            if _contains_unsafe_content(value):
                _add(errors, f"attribute {name!r} on <{tag}> contains image, SVG, URL, or external-link content")


def _check_editable(cell_id: str, cell: Any, style: dict[str, str], errors: list[str]) -> None:
    for properties in (style, cell.attrib):
        for property_name in sorted(IMMUTABLE_FALSE_PROPERTIES):
            if _is_false(properties.get(property_name)):
                _add(errors, f"cell {cell_id!r} disables {property_name} and is not fully editable")
        for property_name in sorted(IMMUTABLE_TRUE_PROPERTIES):
            if _is_true(properties.get(property_name)):
                _add(errors, f"cell {cell_id!r} is locked/non-editable")


def _cell_has_image(cell: Any, style: dict[str, str]) -> bool:
    normalized = {key.replace("_", "").replace("-", "").lower(): value for key, value in style.items()}
    if normalized.get("shape", "").lower() == "image":
        return True
    if any(key in normalized for key in {"image", "backgroundimage"}):
        return True
    value = _decode_layers(cell.get("value", ""))
    return bool(re.search(r"<\s*/?\s*(?:img|svg|image|foreignobject)\b|data\s*:\s*image/", value, re.IGNORECASE))


def _visible_label(cell: Any, style: dict[str, str], *, is_canvas: bool) -> bool:
    if is_canvas or not cell.get("value", "").strip():
        return False
    if _is_false(cell.get("visible")) or _is_false(style.get("visible")):
        return False
    opacity = _number(style.get("opacity"))
    label_opacity = _number(style.get("textOpacity") or style.get("labelOpacity"))
    font_size = _number(style.get("fontSize"))
    return (
        opacity != 0
        and label_opacity != 0
        and font_size != 0
        and style.get("fontColor", "#000000").lower() != "none"
    )


def _geometry_children_are_canonical(cell_id: str, geometry: Any, is_edge: bool, errors: list[str]) -> None:
    if not is_edge:
        if list(geometry):
            _add(errors, f"vertex {cell_id!r} geometry must not contain nested elements")
        return
    arrays = 0
    point_roles: set[str] = set()
    for child in list(geometry):
        if child.tag == "mxPoint":
            role = child.get("as", "")
            if role not in {"sourcePoint", "targetPoint"}:
                _add(errors, f"edge {cell_id!r} has an invalid direct mxPoint role {role!r}")
            elif role in point_roles:
                _add(errors, f"edge {cell_id!r} has duplicate {role}")
            point_roles.add(role)
        elif child.tag == "Array" and child.get("as") == "points":
            arrays += 1
            if arrays > 1:
                _add(errors, f"edge {cell_id!r} has duplicate waypoint arrays")
            for point in list(child):
                if point.tag != "mxPoint" or list(point):
                    _add(errors, f"edge {cell_id!r} waypoint Array must contain only direct mxPoint elements")
        else:
            _add(errors, f"edge {cell_id!r} geometry contains unsupported nested element <{child.tag}>")


def validate_drawio_bytes(data: bytes, *, path: str = "<memory>") -> dict[str, Any]:
    """Validate UTF-8 *data* and return a JSON-serializable report."""

    report = _empty_report(path)
    errors: list[str] = report["errors"]
    warnings: list[str] = report["warnings"]
    stats: dict[str, int] = report["stats"]
    try:
        document = _parse_utf8_xml(data)
    except ValueError as exc:
        errors.append(str(exc))
        return report

    decoded_xml = data.decode("utf-8-sig")
    if _contains_unsafe_content(decoded_xml):
        errors.append("XML contains an image, SVG/HTML embed, data URI, URL, or external-link payload")
    _scan_active_content(document, errors)
    for node in document.iter():
        if node.text and node.text.strip():
            _add(errors, f"canonical draw.io XML stores content in attributes, not text inside <{node.tag}>")
        if node.tail and node.tail.strip():
            _add(errors, f"canonical draw.io XML forbids non-whitespace content after <{node.tag}>")
    if document.tag != "mxfile":
        errors.append(f"root element must be mxfile, got {document.tag!r}")
        return report
    if document.get("compressed", "false").lower() not in {"false", "0"}:
        errors.append("mxfile declares compressed content; only uncompressed XML is accepted")
    document_children = list(document)
    if len(document_children) != 1 or document_children[0].tag != "diagram":
        errors.append("mxfile must contain exactly one direct diagram and no wrapper/extra elements")
        return report
    diagram = document_children[0]
    if (diagram.text or "").strip():
        errors.append("diagram contains a text/compressed payload; only uncompressed XML is accepted")
    diagram_children = list(diagram)
    if len(diagram_children) != 1 or diagram_children[0].tag != "mxGraphModel":
        errors.append("diagram must contain exactly one direct mxGraphModel and no wrapper/extra elements")
        return report
    model = diagram_children[0]
    model_children = list(model)
    if len(model_children) != 1 or model_children[0].tag != "root":
        errors.append("mxGraphModel must contain exactly one direct root and no wrapper/extra elements")
        return report

    width = _number(model.get("pageWidth"))
    height = _number(model.get("pageHeight"))
    if width is None or width <= 0:
        errors.append("mxGraphModel.pageWidth must be a positive finite number")
    if height is None or height <= 0:
        errors.append("mxGraphModel.pageHeight must be a positive finite number")
    background = model.get("background", "none")
    if background.lower() != "none" and not COLOR_RE.fullmatch(background):
        errors.append("mxGraphModel.background must be #RRGGBB or omitted/none; images and URLs are forbidden")
    if width is None or width <= 0 or height is None or height <= 0:
        return report
    report["canvas"] = {"width": width, "height": height, "background": background}

    graph_root = model_children[0]
    if any(child.tag != "mxCell" for child in list(graph_root)):
        errors.append("root may contain only direct mxCell children; object/UserObject wrappers are forbidden")
        return report
    cells = list(graph_root)
    stats["cells"] = len(cells)
    ids: dict[str, Any] = {}
    for position, cell in enumerate(cells):
        cell_id = cell.get("id")
        label = repr(cell_id) if cell_id else f"at index {position}"
        if not cell_id:
            errors.append(f"mxCell {label} has no non-empty id")
            continue
        if cell_id in ids:
            errors.append(f"duplicate mxCell id: {cell_id!r}")
        else:
            ids[cell_id] = cell
    if "0" not in ids or "1" not in ids:
        errors.append("canonical root cells with ids '0' and '1' are required")
    else:
        if ids["0"].attrib != {"id": "0"} or list(ids["0"]):
            errors.append("root mxCell '0' must contain only id='0' and no geometry/content")
        if ids["1"].attrib != {"id": "1", "parent": "0"} or list(ids["1"]):
            errors.append("root mxCell '1' must contain only id='1', parent='0', and no geometry/content")

    canvas_cells: list[Any] = []
    drawable_count = 0
    for cell in cells:
        cell_id = cell.get("id", "<missing>")
        if cell_id in {"0", "1"}:
            continue
        is_vertex = cell.get("vertex") == "1"
        is_edge = cell.get("edge") == "1"
        is_canvas = cell_id == "canvas" or cell.get("data-role") == "canvas-boundary"
        if is_canvas:
            canvas_cells.append(cell)
        if is_vertex == is_edge:
            errors.append(f"cell {cell_id!r} must be exactly one of vertex='1' or edge='1'")
            continue
        drawable_count += 0 if is_canvas else 1
        if cell.get("parent") != "1":
            errors.append(f"drawable cell {cell_id!r} must have canonical parent '1'")
        if is_vertex:
            stats["vertices"] += 1
        else:
            stats["edges"] += 1

        style_text = cell.get("style", "")
        style = _style_map(style_text)
        normalized_style = {
            key.replace("_", "").replace("-", "").lower(): value for key, value in style.items()
        }
        if any(normalized_style.get(key, "").strip() for key in {"href", "src", "link", "url"}):
            errors.append(f"cell {cell_id!r} contains a style-level link/URL and is forbidden")
        if _is_true(style.get("html")):
            errors.append(f"cell {cell_id!r} uses html=1; only plain editable labels (html=0) are accepted")
        _check_editable(cell_id, cell, style, errors)
        if not is_canvas and (_is_false(cell.get("visible")) or _is_false(style.get("visible"))):
            errors.append(f"cell {cell_id!r} is hidden and is not a visible reconstruction")
        if not is_canvas and _number(style.get("opacity")) == 0:
            errors.append(f"cell {cell_id!r} has zero opacity and is not a visible reconstruction")
        if _cell_has_image(cell, style):
            stats["image_cells"] += 1
            errors.append(f"cell {cell_id!r} contains an image/SVG embedding; vector-only diagrams are required")
        if _visible_label(cell, style, is_canvas=is_canvas):
            stats["text_cells"] += 1

        direct_geometries = [child for child in list(cell) if child.tag == "mxGeometry"]
        all_geometries = list(cell.iter("mxGeometry"))
        if len(direct_geometries) != 1 or len(all_geometries) != 1 or len(list(cell)) != 1:
            errors.append(f"drawable cell {cell_id!r} must contain exactly one direct mxGeometry and no other children")
            continue
        geometry = direct_geometries[0]
        _geometry_children_are_canonical(cell_id, geometry, is_edge, errors)

        if is_vertex:
            x = _number(geometry.get("x"))
            y = _number(geometry.get("y"))
            item_width = _number(geometry.get("width"))
            item_height = _number(geometry.get("height"))
            if None in (x, y, item_width, item_height):
                errors.append(f"vertex {cell_id!r} geometry must have finite x, y, width, and height")
                continue
            assert x is not None and y is not None and item_width is not None and item_height is not None
            if item_width <= 0 or item_height <= 0:
                errors.append(f"vertex {cell_id!r} width and height must be positive")
            if not _point_in_bounds(x, y, width, height) or not _point_in_bounds(
                x + item_width, y + item_height, width, height
            ):
                errors.append(f"vertex {cell_id!r} lies outside the {width:g}x{height:g} canvas")
            rotation_text = style.get("rotation")
            if rotation_text is not None:
                rotation = _number(rotation_text)
                if rotation is None:
                    errors.append(f"vertex {cell_id!r} rotation must be a finite number of degrees")
                elif "text" not in style:
                    errors.append(f"vertex {cell_id!r} uses rotation but is not a text box")
                else:
                    left, top, right, bottom = _rotated_bounds(
                        x,
                        y,
                        item_width,
                        item_height,
                        rotation,
                    )
                    if not _point_in_bounds(left, top, width, height) or not _point_in_bounds(
                        right, bottom, width, height
                    ):
                        errors.append(
                            f"rotated text vertex {cell_id!r} lies outside the "
                            f"{width:g}x{height:g} canvas"
                        )
            if (
                not is_canvas
                and style.get("fillColor", "#FFFFFF").lower() == "none"
                and style.get("strokeColor", "#000000").lower() == "none"
                and not _visible_label(cell, style, is_canvas=False)
            ):
                errors.append(f"vertex {cell_id!r} has no visible fill, stroke, or label")
            stroke_width = _number(style.get("strokeWidth", "1"))
            if not is_canvas and style.get("strokeColor", "#000000").lower() != "none" and stroke_width is not None:
                margin = stroke_width / 2
                if x < margin or y < margin or x + item_width + margin > width or y + item_height + margin > height:
                    _add(warnings, f"vertex {cell_id!r} stroke may render outside the canvas")
        else:
            if geometry.get("relative") != "1":
                errors.append(f"edge {cell_id!r} geometry must have relative='1'")
            if style.get("strokeColor", "#000000").lower() == "none" and not _visible_label(
                cell, style, is_canvas=False
            ):
                errors.append(f"edge {cell_id!r} has no visible stroke or label")
            for terminal in ("source", "target"):
                reference = cell.get(terminal)
                if reference is not None:
                    if reference not in ids:
                        errors.append(f"edge {cell_id!r} has missing {terminal} id {reference!r}")
                    elif ids[reference].get("vertex") != "1":
                        errors.append(f"edge {cell_id!r} {terminal} {reference!r} is not a vertex")
                    elif reference == "canvas":
                        errors.append(f"edge {cell_id!r} cannot use the canvas boundary as its {terminal}")
                    anchor_prefix = "exit" if terminal == "source" else "entry"
                    if _number(style.get(f"{anchor_prefix}X")) is None or _number(
                        style.get(f"{anchor_prefix}Y")
                    ) is None:
                        errors.append(
                            f"attached edge {cell_id!r} requires finite {anchor_prefix}X/{anchor_prefix}Y anchors"
                        )
            role_points: dict[str, list[Any]] = {"sourcePoint": [], "targetPoint": []}
            for point in geometry.findall("mxPoint"):
                role = point.get("as", "")
                if role in role_points:
                    role_points[role].append(point)
            for terminal, role in (("source", "sourcePoint"), ("target", "targetPoint")):
                if cell.get(terminal) is None and len(role_points[role]) != 1:
                    errors.append(f"unattached edge {cell_id!r} requires exactly one direct {role}")
            all_points = list(geometry.findall("mxPoint")) + list(geometry.findall("./Array[@as='points']/mxPoint"))
            for point_index, point in enumerate(all_points):
                point_x = _number(point.get("x"))
                point_y = _number(point.get("y"))
                if point_x is None or point_y is None:
                    errors.append(f"edge {cell_id!r} point {point_index} must have finite x and y")
                elif not _point_in_bounds(point_x, point_y, width, height):
                    errors.append(f"edge {cell_id!r} point {point_index} lies outside the canvas")
            stroke_width = _number(style.get("strokeWidth", "1")) or 1
            for role, arrow_key in (("sourcePoint", "startArrow"), ("targetPoint", "endArrow")):
                arrow = style.get(arrow_key, "none").lower()
                if arrow == "none" or not role_points[role]:
                    continue
                point_x = _number(role_points[role][0].get("x"))
                point_y = _number(role_points[role][0].get("y"))
                margin = max(6.0, stroke_width * 3)
                if point_x is not None and point_y is not None and (
                    point_x < margin or point_y < margin or point_x + margin > width or point_y + margin > height
                ):
                    _add(warnings, f"edge {cell_id!r} {arrow_key} may render outside the canvas")

    if len(canvas_cells) != 1:
        errors.append(f"exactly one editable canvas-boundary vertex is required (found {len(canvas_cells)})")
    else:
        canvas_cell = canvas_cells[0]
        if canvas_cell.get("id") != "canvas" or canvas_cell.get("data-role") != "canvas-boundary":
            errors.append("canvas-boundary must use id='canvas' and data-role='canvas-boundary'")
        if canvas_cell.get("value", ""):
            errors.append("canvas-boundary must have an empty label")
        if canvas_cell.get("vertex") != "1" or canvas_cell.get("edge") == "1":
            errors.append("canvas-boundary must be a vertex")
        geometries = [child for child in list(canvas_cell) if child.tag == "mxGeometry"]
        if len(geometries) == 1:
            geometry = geometries[0]
            actual = tuple(_number(geometry.get(name)) for name in ("x", "y", "width", "height"))
            expected = (0.0, 0.0, width, height)
            if any(value is None for value in actual) or any(
                abs(float(value) - wanted) > 1e-6 for value, wanted in zip(actual, expected) if value is not None
            ):
                errors.append("canvas-boundary geometry must exactly cover (0, 0, pageWidth, pageHeight)")

    if drawable_count == 0:
        errors.append("diagram contains no editable reconstructed vertices or edges")
    if stats["image_cells"]:
        _add(errors, f"diagram contains {stats['image_cells']} image cell(s); all images are forbidden")

    report["valid"] = not errors
    return report


def validate_drawio(path: Path) -> dict[str, Any]:
    """Read and validate a draw.io file, returning a report."""

    try:
        data = _read_xml(path)
    except ValueError as exc:
        report = _empty_report(str(path))
        report["errors"].append(str(exc))
        return report
    return validate_drawio_bytes(data, path=str(path))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("drawio", type=Path, help="canonical uncompressed UTF-8 .drawio XML file")
    parser.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = validate_drawio(args.drawio)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    elif report["valid"]:
        stats = report["stats"]
        canvas = report["canvas"]
        print(
            f"VALID: {args.drawio} ({canvas['width']:g}x{canvas['height']:g}, "
            f"{stats['vertices']} vertices, {stats['edges']} edges, {stats['text_cells']} visible labels)"
        )
        for warning in report["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
    else:
        print(f"INVALID: {args.drawio}", file=sys.stderr)
        for error in report["errors"]:
            print(f"error: {error}", file=sys.stderr)
        for warning in report["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

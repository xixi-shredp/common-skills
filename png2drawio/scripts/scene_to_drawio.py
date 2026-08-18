#!/usr/bin/env python3
"""Convert a version-1 editable scene JSON file to uncompressed draw.io XML."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Sequence

from defusedxml import ElementTree as SafeET  # type: ignore[import-not-found]
from defusedxml.common import DefusedXmlException  # type: ignore[import-not-found]


COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
RESERVED_IDS = {"0", "1", "canvas"}
VERTEX_TYPES = {
    "rectangle",
    "rounded_rectangle",
    "ellipse",
    "diamond",
    "cylinder",
    "hexagon",
    "text",
}
EDGE_TYPES = {"line", "edge"}
ARROWS = {
    "none": "none",
    "classic": "classic",
    "arrow": "classic",
    "block": "block",
    "triangle": "block",
    "open": "open",
    "oval": "oval",
    "diamond": "diamond",
    "diamond_thin": "diamondThin",
}
SCENE_KEYS = {"version", "source", "canvas", "elements", "diagnostics"}
SOURCE_KEYS = {"path"}
CANVAS_KEYS = {"width", "height", "background", "transparent"}
VERTEX_KEYS = {"id", "type", "x", "y", "width", "height", "text", "style"}
EDGE_KEYS = {
    "id",
    "type",
    "x",
    "y",
    "width",
    "height",
    "points",
    "source",
    "target",
    "start_arrow",
    "end_arrow",
    "text",
    "style",
}
STYLE_KEYS = {
    "fill",
    "stroke",
    "stroke_width",
    "dashed",
    "opacity",
    "font_family",
    "font_size",
    "font_color",
    "align",
    "vertical_align",
    "bold",
    "italic",
}
TEXT_STYLE_KEYS = {"rotation"}


class SceneError(ValueError):
    """Raised when a scene does not conform to the supported schema."""


def _check_keys(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted((str(key) for key in value if key not in allowed))
    if unknown:
        raise SceneError(f"{field} contains unknown key(s): {', '.join(unknown)}")


def _is_xml10_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        codepoint in {0x9, 0xA, 0xD}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _reject_illegal_xml_characters(value: Any, field: str = "scene") -> None:
    if isinstance(value, str):
        for index, character in enumerate(value):
            if not _is_xml10_character(character):
                raise SceneError(
                    f"{field} contains XML 1.0-illegal character U+{ord(character):04X} at offset {index}"
                )
    elif isinstance(value, Mapping):
        for key, child in value.items():
            _reject_illegal_xml_characters(key, f"{field}.<key>")
            _reject_illegal_xml_characters(child, f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_illegal_xml_characters(child, f"{field}[{index}]")


def _number(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SceneError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise SceneError(f"{field} must be a finite number")
    if minimum is not None and result < minimum:
        raise SceneError(f"{field} must be >= {minimum:g}")
    return result


def _fmt(value: float) -> str:
    if value == 0:
        return "0"
    return f"{value:.12g}"


def _color(value: Any, field: str, *, allow_none: bool = True) -> str:
    if not isinstance(value, str):
        raise SceneError(f"{field} must be a color string")
    if allow_none and value.lower() == "none":
        return "none"
    if not COLOR_RE.fullmatch(value):
        suffix = " or 'none'" if allow_none else ""
        raise SceneError(f"{field} must be #RRGGBB{suffix}")
    return value.upper()


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise SceneError(f"{field} must be a boolean")
    return value


def _enum(value: Any, field: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise SceneError(f"{field} must be one of: {', '.join(sorted(choices))}")
    return value


def _safe_style_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SceneError(f"{field} must be a non-empty string")
    if any(ch in value for ch in ";=\r\n"):
        raise SceneError(f"{field} contains a character that is unsafe in draw.io style syntax")
    return value.strip()


def _style_string(entries: Sequence[tuple[str, Any]]) -> str:
    return ";".join(f"{key}={value}" if value != "" else key for key, value in entries) + ";"


def _common_style(style: Mapping[str, Any], field: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    if "fill" in style:
        result.append(("fillColor", _color(style["fill"], f"{field}.fill")))
    if "stroke" in style:
        result.append(("strokeColor", _color(style["stroke"], f"{field}.stroke")))
    if "stroke_width" in style:
        result.append(
            ("strokeWidth", _fmt(_number(style["stroke_width"], f"{field}.stroke_width", minimum=0)))
        )
    if "dashed" in style:
        result.append(("dashed", "1" if _bool(style["dashed"], f"{field}.dashed") else "0"))
    if "opacity" in style:
        opacity = _number(style["opacity"], f"{field}.opacity", minimum=0)
        if opacity > 100:
            raise SceneError(f"{field}.opacity must be <= 100")
        result.append(("opacity", _fmt(opacity)))
    if "font_family" in style:
        result.append(("fontFamily", _safe_style_text(style["font_family"], f"{field}.font_family")))
    if "font_size" in style:
        result.append(("fontSize", _fmt(_number(style["font_size"], f"{field}.font_size", minimum=0.1))))
    if "font_color" in style:
        result.append(("fontColor", _color(style["font_color"], f"{field}.font_color", allow_none=False)))
    if "align" in style:
        result.append(("align", _enum(style["align"], f"{field}.align", {"left", "center", "right"})))
    if "vertical_align" in style:
        result.append(
            (
                "verticalAlign",
                _enum(style["vertical_align"], f"{field}.vertical_align", {"top", "middle", "bottom"}),
            )
        )
    font_style = 0
    if "bold" in style and _bool(style["bold"], f"{field}.bold"):
        font_style |= 1
    if "italic" in style and _bool(style["italic"], f"{field}.italic"):
        font_style |= 2
    if "bold" in style or "italic" in style:
        result.append(("fontStyle", str(font_style)))
    return result


def _text_rotation(style: Mapping[str, Any], field: str) -> float:
    if "rotation" not in style:
        return 0.0
    return _number(style["rotation"], f"{field}.rotation")


def _rotated_bounds(
    x: float,
    y: float,
    width: float,
    height: float,
    rotation: float,
) -> tuple[float, float, float, float]:
    """Return the axis-aligned bounds after center-based draw.io rotation."""

    radians = math.radians(math.fmod(rotation, 360.0))
    cosine = abs(math.cos(radians))
    sine = abs(math.sin(radians))
    half_width = (width * cosine + height * sine) / 2.0
    half_height = (width * sine + height * cosine) / 2.0
    center_x = x + width / 2.0
    center_y = y + height / 2.0
    return (
        center_x - half_width,
        center_y - half_height,
        center_x + half_width,
        center_y + half_height,
    )


def _element_style(element_type: str, style: Mapping[str, Any], field: str) -> str:
    if element_type == "text":
        base = [
            ("text", ""),
            ("html", "0"),
            ("strokeColor", "none"),
            ("fillColor", "none"),
            ("whiteSpace", "wrap"),
            ("overflow", "hidden"),
        ]
        if "rotation" in style:
            base.append(("rotation", _fmt(_text_rotation(style, field))))
    else:
        shape = {
            "rectangle": "rectangle",
            "rounded_rectangle": "rectangle",
            "ellipse": "ellipse",
            "diamond": "rhombus",
            "cylinder": "cylinder3",
            "hexagon": "hexagon",
        }[element_type]
        base = [("shape", shape), ("html", "0"), ("whiteSpace", "wrap")]
        if element_type == "rounded_rectangle" or (
            element_type == "rectangle" and style.get("rounded") is True
        ):
            base.append(("rounded", "1"))
        if "rounded" in style and not isinstance(style["rounded"], bool):
            raise SceneError(f"{field}.rounded must be a boolean")
        if element_type == "hexagon":
            base.append(("perimeter", "hexagonPerimeter2"))
        elif element_type == "cylinder":
            base.extend((("boundedLbl", "1"), ("backgroundOutline", "1")))
        base.extend((("fillColor", "#FFFFFF"), ("strokeColor", "#000000")))
    base.extend(_common_style(style, field))
    return _style_string(base)


def _edge_style(element: Mapping[str, Any], style: Mapping[str, Any], field: str) -> str:
    base: list[tuple[str, str]] = [
        ("edgeStyle", "none"),
        ("orthogonalLoop", "1"),
        ("jettySize", "auto"),
        ("html", "0"),
        ("rounded", "0"),
        ("strokeColor", "#000000"),
        ("endArrow", "none"),
    ]
    for scene_key, drawio_key in (("start_arrow", "startArrow"), ("end_arrow", "endArrow")):
        if scene_key in element:
            value = element[scene_key]
            if not isinstance(value, str) or value not in ARROWS:
                raise SceneError(f"{field}.{scene_key} must be one of: {', '.join(sorted(ARROWS))}")
            base.append((drawio_key, ARROWS[value]))
    base.extend(_common_style(style, f"{field}.style"))
    return _style_string(base)


def _scene_identifier(element: Mapping[str, Any], index: int) -> str:
    if "id" not in element:
        return f"__anonymous_{index + 1}"
    value = element["id"]
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SceneError(f"elements[{index}].id must be a string or integer")
    text = str(value)
    if not text:
        raise SceneError(f"elements[{index}].id must not be empty")
    return text


def _xml_identifier(scene_id: str, index: int, used: set[str], *, anonymous: bool) -> str:
    if anonymous:
        candidate = f"element-{index + 1:04d}"
    elif scene_id not in RESERVED_IDS and SAFE_ID_RE.fullmatch(scene_id) and scene_id not in used:
        return scene_id
    else:
        digest = hashlib.sha256(scene_id.encode("utf-8")).hexdigest()[:12]
        candidate = f"scene-{digest}"
    serial = 2
    base = candidate
    while candidate in used or candidate in RESERVED_IDS:
        candidate = f"{base}-{serial}"
        serial += 1
    return candidate


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SceneError(f"{field} must be an object")
    return value


def _point(value: Any, field: str) -> tuple[float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise SceneError(f"{field} must be [x, y]")
    return _number(value[0], f"{field}[0]"), _number(value[1], f"{field}[1]")


def _anchor_entries(prefix: str, point: tuple[float, float], bounds: tuple[float, float, float, float]) -> str:
    x, y, width, height = bounds
    anchor_x = (point[0] - x) / width
    anchor_y = (point[1] - y) / height
    return _style_string(
        [
            (f"{prefix}X", _fmt(anchor_x)),
            (f"{prefix}Y", _fmt(anchor_y)),
            (f"{prefix}Dx", "0"),
            (f"{prefix}Dy", "0"),
            (f"{prefix}Perimeter", "0"),
        ]
    )


def scene_to_drawio(scene: Mapping[str, Any]) -> str:
    """Return deterministic, uncompressed draw.io XML for *scene*.

    ``SceneError`` is raised for unsupported or malformed input.
    """

    scene = _mapping(scene, "scene")
    _reject_illegal_xml_characters(scene)
    _check_keys(scene, SCENE_KEYS, "scene")
    if scene.get("version") != 1:
        raise SceneError("version must be 1")
    if "source" in scene:
        source = _mapping(scene["source"], "source")
        _check_keys(source, SOURCE_KEYS, "source")
        if "path" in source and not isinstance(source["path"], str):
            raise SceneError("source.path must be a string")
    if "diagnostics" in scene:
        _mapping(scene["diagnostics"], "diagnostics")
    canvas = _mapping(scene.get("canvas"), "canvas")
    _check_keys(canvas, CANVAS_KEYS, "canvas")
    width = _number(canvas.get("width"), "canvas.width", minimum=0.000001)
    height = _number(canvas.get("height"), "canvas.height", minimum=0.000001)
    transparent = canvas.get("transparent", False)
    transparent = _bool(transparent, "canvas.transparent")
    background = _color(canvas.get("background", "none"), "canvas.background")
    if background == "none":
        transparent = True

    elements = scene.get("elements")
    if not isinstance(elements, list):
        raise SceneError("elements must be an array")

    id_map: dict[str, str] = {}
    element_types: dict[str, str] = {}
    vertex_bounds: dict[str, tuple[float, float, float, float]] = {}
    scene_ids: list[str] = []
    used_ids = set(RESERVED_IDS)
    for index, raw_element in enumerate(elements):
        element = _mapping(raw_element, f"elements[{index}]")
        scene_id = _scene_identifier(element, index)
        scene_ids.append(scene_id)
        if "id" in element and scene_id in RESERVED_IDS:
            raise SceneError(f"elements[{index}].id {scene_id!r} is reserved by the draw.io document")
        if scene_id in id_map:
            raise SceneError(f"duplicate scene element id: {scene_id!r}")
        element_type = element.get("type")
        if not isinstance(element_type, str):
            raise SceneError(f"elements[{index}].type must be a string")
        if element_type not in VERTEX_TYPES | EDGE_TYPES:
            raise SceneError(f"elements[{index}].type is unsupported: {element_type!r}")
        _check_keys(element, VERTEX_KEYS if element_type in VERTEX_TYPES else EDGE_KEYS, f"elements[{index}]")
        style = _mapping(element.get("style", {}), f"elements[{index}].style")
        allowed_style_keys = (
            STYLE_KEYS
            | ({"rounded"} if element_type in {"rectangle", "rounded_rectangle"} else set())
            | (TEXT_STYLE_KEYS if element_type == "text" else set())
        )
        _check_keys(style, allowed_style_keys, f"elements[{index}].style")
        if element_type in VERTEX_TYPES:
            vertex_x = _number(element.get("x"), f"elements[{index}].x")
            vertex_y = _number(element.get("y"), f"elements[{index}].y")
            vertex_width = _number(element.get("width"), f"elements[{index}].width", minimum=0.000001)
            vertex_height = _number(element.get("height"), f"elements[{index}].height", minimum=0.000001)
            if vertex_x < 0 or vertex_y < 0 or vertex_x + vertex_width > width or vertex_y + vertex_height > height:
                raise SceneError(f"elements[{index}] lies outside the 0..{width:g} by 0..{height:g} canvas")
            if element_type == "text":
                rotation = _text_rotation(style, f"elements[{index}].style")
                left, top, right, bottom = _rotated_bounds(
                    vertex_x,
                    vertex_y,
                    vertex_width,
                    vertex_height,
                    rotation,
                )
                epsilon = 1e-6
                if left < -epsilon or top < -epsilon or right > width + epsilon or bottom > height + epsilon:
                    raise SceneError(
                        f"elements[{index}] rotated text bounds lie outside the "
                        f"0..{width:g} by 0..{height:g} canvas"
                    )
            vertex_bounds[scene_id] = (vertex_x, vertex_y, vertex_width, vertex_height)
        else:
            for coordinate in ("x", "y"):
                if coordinate in element:
                    _number(element[coordinate], f"elements[{index}].{coordinate}")
            for extent in ("width", "height"):
                if extent in element:
                    _number(element[extent], f"elements[{index}].{extent}", minimum=0)
        xml_id = _xml_identifier(scene_id, index, used_ids, anonymous="id" not in element)
        id_map[scene_id] = xml_id
        element_types[scene_id] = element_type
        used_ids.add(xml_id)

    mxfile = ET.Element(
        "mxfile",
        {
            "host": "app.diagrams.net",
            "modified": "1970-01-01T00:00:00.000Z",
            "agent": "png2drawio scene_to_drawio.py",
            "version": "1",
            "compressed": "false",
        },
    )
    diagram = ET.SubElement(mxfile, "diagram", {"id": "page-1", "name": "Page-1"})
    model_attributes = {
        "dx": "0",
        "dy": "0",
        "grid": "1",
        "gridSize": "10",
        "guides": "1",
        "tooltips": "1",
        "connect": "1",
        "arrows": "1",
        "fold": "1",
        "page": "1",
        "pageScale": "1",
        "pageWidth": _fmt(width),
        "pageHeight": _fmt(height),
        "math": "0",
        "shadow": "0",
    }
    if not transparent:
        model_attributes["background"] = background
    model = ET.SubElement(diagram, "mxGraphModel", model_attributes)
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", {"id": "0"})
    ET.SubElement(root, "mxCell", {"id": "1", "parent": "0"})

    canvas_style = _style_string(
        [
            ("shape", "rectangle"),
            ("html", "0"),
            ("fillColor", "none" if transparent else background),
            ("strokeColor", "none"),
            ("opacity", "0" if transparent else "100"),
            ("connectable", "0"),
        ]
    )
    canvas_cell = ET.SubElement(
        root,
        "mxCell",
        {
            "id": "canvas",
            "value": "",
            "style": canvas_style,
            "vertex": "1",
            "parent": "1",
            "data-role": "canvas-boundary",
        },
    )
    ET.SubElement(
        canvas_cell,
        "mxGeometry",
        {"x": "0", "y": "0", "width": _fmt(width), "height": _fmt(height), "as": "geometry"},
    )

    for index, raw_element in enumerate(elements):
        element = _mapping(raw_element, f"elements[{index}]")
        field = f"elements[{index}]"
        scene_id = scene_ids[index]
        xml_id = id_map[scene_id]
        element_type = str(element["type"])
        text_value = element.get("text", "")
        if text_value is None:
            text_value = ""
        if not isinstance(text_value, str):
            raise SceneError(f"{field}.text must be a string")
        style = _mapping(element.get("style", {}), f"{field}.style")

        if element_type in VERTEX_TYPES:
            x = _number(element.get("x"), f"{field}.x")
            y = _number(element.get("y"), f"{field}.y")
            item_width = _number(element.get("width"), f"{field}.width", minimum=0.000001)
            item_height = _number(element.get("height"), f"{field}.height", minimum=0.000001)
            if x < 0 or y < 0 or x + item_width > width or y + item_height > height:
                raise SceneError(f"{field} lies outside the 0..{width:g} by 0..{height:g} canvas")
            cell = ET.SubElement(
                root,
                "mxCell",
                {
                    "id": xml_id,
                    "value": text_value,
                    "style": _element_style(element_type, style, f"{field}.style"),
                    "vertex": "1",
                    "parent": "1",
                    "data-scene-id": scene_id,
                },
            )
            ET.SubElement(
                cell,
                "mxGeometry",
                {
                    "x": _fmt(x),
                    "y": _fmt(y),
                    "width": _fmt(item_width),
                    "height": _fmt(item_height),
                    "as": "geometry",
                },
            )
            continue

        points_raw = element.get("points")
        if not isinstance(points_raw, list) or len(points_raw) < 2:
            raise SceneError(f"{field}.points must contain at least two [x, y] points")
        points = [_point(value, f"{field}.points[{point_index}]") for point_index, value in enumerate(points_raw)]
        for point_index, (point_x, point_y) in enumerate(points):
            if point_x < 0 or point_x > width or point_y < 0 or point_y > height:
                raise SceneError(f"{field}.points[{point_index}] lies outside the canvas")
        terminals: dict[str, str] = {}
        for terminal in ("source", "target"):
            if terminal not in element or element[terminal] is None:
                continue
            ref = element[terminal]
            if isinstance(ref, bool) or not isinstance(ref, (str, int)):
                raise SceneError(f"{field}.{terminal} must be a scene element id")
            ref_key = str(ref)
            if ref_key not in id_map:
                raise SceneError(f"{field}.{terminal} references unknown id {ref_key!r}")
            if element_types[ref_key] not in VERTEX_TYPES:
                raise SceneError(f"{field}.{terminal} must reference a vertex, not an edge")
            terminals[terminal] = id_map[ref_key]

        attributes = {
            "id": xml_id,
            "value": text_value,
            "style": _edge_style(element, style, field),
            "edge": "1",
            "parent": "1",
            "data-scene-id": scene_id,
            **terminals,
        }
        if "source" in terminals:
            source_key = str(element["source"])
            attributes["style"] += _anchor_entries("exit", points[0], vertex_bounds[source_key])
        if "target" in terminals:
            target_key = str(element["target"])
            attributes["style"] += _anchor_entries("entry", points[-1], vertex_bounds[target_key])
        cell = ET.SubElement(root, "mxCell", attributes)
        geometry = ET.SubElement(cell, "mxGeometry", {"relative": "1", "as": "geometry"})
        if "source" not in terminals:
            ET.SubElement(
                geometry,
                "mxPoint",
                {"x": _fmt(points[0][0]), "y": _fmt(points[0][1]), "as": "sourcePoint"},
            )
        if "target" not in terminals:
            ET.SubElement(
                geometry,
                "mxPoint",
                {"x": _fmt(points[-1][0]), "y": _fmt(points[-1][1]), "as": "targetPoint"},
            )
        if len(points) > 2:
            waypoint_array = ET.SubElement(geometry, "Array", {"as": "points"})
            for point_x, point_y in points[1:-1]:
                ET.SubElement(waypoint_array, "mxPoint", {"x": _fmt(point_x), "y": _fmt(point_y)})

    ET.indent(mxfile, space="  ")
    xml_bytes = ET.tostring(mxfile, encoding="utf-8", xml_declaration=True) + b"\n"
    try:
        reparsed = SafeET.fromstring(xml_bytes)
    except (ET.ParseError, DefusedXmlException) as exc:  # pragma: no cover - defensive serialization invariant
        raise SceneError(f"generated XML failed safe reparse: {exc}") from exc
    if reparsed.tag != "mxfile" or b"<!DOCTYPE" in xml_bytes.upper() or b"<!ENTITY" in xml_bytes.upper():
        raise SceneError("generated XML failed safety invariants")
    return xml_bytes.decode("utf-8")


def _load_scene(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except OSError as exc:
        raise SceneError(f"cannot read {path}: {exc}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SceneError(f"invalid JSON in {path}: {exc}") from exc
    return _mapping(value, "scene")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path, help="input version-1 scene JSON")
    parser.add_argument("output", type=Path, help="output uncompressed .drawio XML")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        xml = scene_to_drawio(_load_scene(args.scene))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(xml, encoding="utf-8")
    except (SceneError, OSError) as exc:
        print(f"scene_to_drawio: error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote editable draw.io XML: {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

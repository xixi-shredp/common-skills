# Scene JSON schema (version 1)

`scene.json` is the editable intermediate representation consumed by `scene_to_drawio.py`. Coordinates are source-image pixels with origin at the top left, `x` increasing rightward, and `y` increasing downward.

## Minimal example

```json
{
  "version": 1,
  "canvas": {
    "width": 640,
    "height": 360,
    "background": "#FFFFFF",
    "transparent": false
  },
  "elements": [
    {
      "id": "box-1",
      "type": "rounded_rectangle",
      "x": 40,
      "y": 50,
      "width": 180,
      "height": 70,
      "text": "Input",
      "style": {
        "fill": "#EAF2FF",
        "stroke": "#315E9B",
        "stroke_width": 2,
        "font_size": 16,
        "font_color": "#111111",
        "align": "center",
        "vertical_align": "middle",
        "bold": true,
        "rotation": 90
      }
    },
    {
      "id": "flow-1",
      "type": "edge",
      "points": [[220, 85], [320, 85]],
      "source": "box-1",
      "end_arrow": "classic",
      "style": {"stroke": "#315E9B", "stroke_width": 2}
    }
  ]
}
```

## Top level and canvas

| Field | Required | Constraint |
| --- | --- | --- |
| `version` | yes | integer `1` |
| `canvas` | yes | object |
| `canvas.width`, `canvas.height` | yes | positive integers; exactly the source PNG dimensions |
| `canvas.background` | no | `#RRGGBB` or `"none"`; defaults to `"none"` |
| `canvas.transparent` | no | boolean; defaults to `false`, but `background: "none"` implies transparency |
| `elements` | yes | array, in back-to-front draw order |

The top-level schema is closed: only `version`, `source`, `canvas`, `elements`, and `diagnostics` are accepted. Optional `source` may contain only string `path`; `diagnostics` must be an object but is ignored for rendering. The converter always creates a separate editable `canvas` boundary cell at `(0, 0, width, height)`. Do not duplicate it as a scene element. Element IDs may be strings or integers but must be unique when converted to strings; prefer stable human-readable string IDs. IDs `0`, `1`, and `canvas` are reserved by the output document.

## Vertex elements

Supported `type` values are `rectangle`, `rounded_rectangle`, `ellipse`, `diamond`, `cylinder`, `hexagon`, and `text`.

Every vertex accepts only `id`, `type`, `x`, `y`, `width`, `height`, `text`, and `style`. It requires finite numeric geometry inside the canvas; width and height must be positive. Optional `text` must be a Unicode string. Preserve visible line breaks. Keep text as a plain editable label or `text` vertex—never as HTML, paths, or a raster crop.

The optional `style` object accepts:

| Field | Values |
| --- | --- |
| `fill`, `stroke` | `#RRGGBB` or `"none"` |
| `stroke_width` | finite number `>= 0` |
| `dashed`, `rounded`, `bold`, `italic` | boolean |
| `opacity` | number from `0` through `100` |
| `font_family` | non-empty text without `;`, `=`, or line breaks |
| `font_size` | finite number `>= 0.1` |
| `font_color` | `#RRGGBB` |
| `align` | `left`, `center`, or `right` |
| `vertical_align` | `top`, `middle`, or `bottom` |
| `rotation` | finite degrees; accepted only for `type: "text"` |

`rounded_rectangle` is always rounded. `rectangle` can also set `style.rounded: true`. Record every style that materially changes pixels; relying on renderer defaults makes comparisons unstable.

### Rotated text boxes

Represent a rotated label as a separate editable `type: "text"` vertex and set `style.rotation`. Positive values rotate clockwise and negative values rotate counterclockwise around the text box center. The `x`, `y`, `width`, and `height` fields describe the unrotated text box; draw.io applies the rotation afterward. For a 90-degree rotation, the visible axis-aligned width and height are therefore swapped relative to the unrotated box.

```json
{
  "id": "vertical-label",
  "type": "text",
  "x": 286,
  "y": 110,
  "width": 120,
  "height": 28,
  "text": "Processing",
  "style": {
    "font_size": 16,
    "font_color": "#111111",
    "align": "center",
    "vertical_align": "middle",
    "rotation": -90
  }
}
```

Keep the rotated visible bounds inside the canvas; the converter rejects text whose rotated corners extend outside it. OCR may miss or fragment rotated labels, so inspect the PNG and create or repair these text vertices manually. Use rotation for a line whose baseline is rotated. For upright characters stacked vertically, keep `rotation: 0` and put line breaks between characters instead.

## Edge elements

Use `type: "edge"` or `type: "line"`. Edge objects accept only `id`, `type`, optional diagnostic bounds (`x`, `y`, `width`, `height`), `points`, `source`, `target`, `start_arrow`, `end_arrow`, `text`, and `style`. Each edge requires `points`, an ordered array of at least two in-canvas `[x, y]` finite-number pairs. The first and last points are explicit endpoints when the edge is unattached; interior points become fixed waypoints. Do not auto-route or auto-layout them.

Optional `source` and `target` contain an existing vertex ID (never another edge ID). For an attached terminal, put the corresponding first/last point at the exact desired connection coordinate. The converter turns it into `exitX/exitY` or `entryX/entryY` relative to that vertex, with perimeter snapping disabled, so the coordinate remains the anchor. For an unattached terminal, it emits the point as `sourcePoint` or `targetPoint`. Optional `text` becomes a plain edge label.

`start_arrow` and `end_arrow` accept `none`, `classic`, `arrow`, `block`, `triangle`, `open`, `oval`, `diamond`, or `diamond_thin`. Edge `style` accepts the common fields above; geometry-oriented styles such as fill or font may be accepted but should be included only when they have a visible purpose.

## LLM correction checklist

`analyze_png.py` emits converter-native version-1 fields, so no mechanical schema translation is needed. It remains a conservative detector: inspect `diagnostics.candidates`, especially low-confidence entries and entries whose observed `candidate_type: "polygon"` has a `mapped_type`. Verify the mapped `rectangle` or `hexagon` visually and change it to the actual supported primitive when needed.

Correct the draft:

1. Match canvas dimensions, background, and transparency to the PNG.
2. Make IDs unique and ensure edge terminal references resolve to vertices.
3. Merge fragmented detections; correct bounds, geometry, colors, opacity, typography, text rotation, arrows, and array order.
4. Keep major components as supported vertices, text, and edges; keep connector points in visual order.
5. Keep every coordinate within the canvas because validation rejects out-of-bounds geometry.
6. Keep `source` and `diagnostics` only as provenance; they do not affect conversion.
7. Do not add unknown top-level, canvas, source, element, or style fields: the closed schema rejects them.

## Images are unsupported

Version-1 scene JSON intentionally has no image element. Never put the source PNG, a crop, or rasterized shapes/text/connectors into the scene or generated XML. Reconstruct special icons with supported shapes or an optional draw.io library stencil. If that is genuinely impossible, report the limitation and request user authorization before extending the implementation; the current workflow cannot claim compliant output containing an image.

For exact parsing behavior, the executable authority is `scripts/scene_to_drawio.py`; run it on every manually corrected scene before rendering.

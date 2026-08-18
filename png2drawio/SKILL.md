---
name: png2drawio
description: Reconstruct a PNG of a flowchart, architecture diagram, block diagram, or other regular geometric schematic as an editable draw.io file, then render and refine it against the source with a strict pixel-error gate. Use when the input is a diagram PNG and the required output is an editable .drawio rather than an image-backed tracing.
---

# PNG to editable draw.io

Rebuild the diagram from editable shapes, connectors, and text. Never place the source PNG, a full-page crop, or another whole-diagram image behind or over the reconstruction. A result is successful only when validation passes and its same-size, transparency-normalized RGBA MAE is **strictly below 0.05**.

## Single-agent execution

Complete the entire reconstruction with the current agent. Do not create, spawn, delegate to, or use subagents for any part of the task, including image inspection, OCR review, scene correction, draw.io generation, rendering, pixel-difference analysis, iteration, or final validation. Use the bundled scripts and ordinary tool calls directly as the same agent. If blocked, diagnose the issue and report or request the required input instead of delegating work to another agent. Treat this as a hard constraint even when the diagram is complex or the surrounding project normally recommends parallel delegation.

## Before starting

Create a dedicated work directory and preserve the source unchanged. Read:

- [references/scene-schema.md](references/scene-schema.md) before editing `scene.json`.
- [references/drawio-format.md](references/drawio-format.md) before diagnosing XML, geometry, editability, or rendering.
- [references/pixel-comparison.md](references/pixel-comparison.md) before interpreting metrics or deciding when to stop.

Run every script with `--help` if an option is uncertain. Invoke commands from this skill directory so the relative `scripts/` paths resolve. Use the Python environment that already provides Pillow, NumPy, OpenCV, and `defusedxml`; if a dependency is missing, check the repository's conda/venv environments before installing anything.

## Required workflow

### 1. Preflight

Confirm that the input is a readable PNG dominated by regular diagram primitives. Record its absolute path, pixel dimensions, color mode, and SHA-256 digest in the work log. Run `python3 scripts/render_drawio.py --preflight --cache-dir <work>/render-cache` to verify the local renderer before analysis. Reject a request for a merely image-backed `.drawio`: this skill produces independently editable content.

Choose one evidence directory per attempt, for example `work/png2drawio/<stem>/`. Never overwrite earlier iteration artifacts.

### 2. Analyze the PNG

Preserve the analyzer's raw output and overlay:

```bash
python3 scripts/analyze_png.py <source.png> <work>/iter-00/scene.auto.json \
  --overlay <work>/iter-00/analysis-overlay.png
```

Use `--languages` when the source needs a different Tesseract language set; use `--no-ocr` only when OCR is unavailable or intentionally excluded. `--ocr-timeout` and `--max-pixels` bound expensive inputs. Inspect the source at native scale and compare it with the detected canvas, shapes, lines, labels, stroke widths, colors, and layer order.

### 3. Correct `scene.json`

Copy `scene.auto.json` to the iteration's `scene.json`, then use LLM visual reasoning to repair detection errors according to [references/scene-schema.md](references/scene-schema.md). This correction is mandatory: the analyzer emits converter-valid, conservative estimates and can miss, merge, split, misclassify, or misread elements. It does not establish final diagram semantics. In particular:

- merge fragmented borders and split text runs;
- correct geometry, z-order, fills, strokes, arrowheads, connector waypoints, fonts, and alignment;
- identify horizontal, vertical, and diagonal labels; recreate baseline-rotated labels as editable `text` vertices with `style.rotation` instead of rasterizing them;
- preserve the original canvas size and coordinate system;
- assign stable, unique IDs and valid references;
- inspect low-confidence diagnostics and every `mapped_type`, correcting conservatively mapped polygon candidates to their actual supported shape;
- attach connectors to the correct `source`/`target` and place their first/last points at the exact visual anchors;
- represent the canvas background as a full-size editable solid rectangle;
- keep every major diagram element editable.

Treat rotation as part of typography. Tesseract may omit or fragment rotated labels, so inspect them visually and transcribe them manually when needed. Use positive degrees for clockwise rotation around the text box center and negative degrees for counterclockwise rotation. Distinguish a rotated text line from upright characters stacked vertically: rotate the former, but represent the latter with line breaks and `rotation: 0`. Keep the rotated visible bounds inside the canvas and use the rendered diff to tune the unrotated box, angle, font size, and alignment.

Do not add automatic layout: pixel reconstruction needs explicit coordinates. The current implementation prohibits every image component. Rebuild special icons with standard/extended draw.io shapes; optionally use the host MCP's `search_shapes` to find a stencil. If an icon truly cannot be vectorized, stop and ask the user to authorize an implementation extension—do not silently embed a crop.

### 4. Generate and render

Run `python3 scripts/scene_to_drawio.py <scene.json> <candidate.drawio>`, then `python3 scripts/render_drawio.py --cache-dir <work>/render-cache <candidate.drawio> <render.png>`. Keep the `.drawio` and rendered PNG for every iteration. The renderer reads `pageWidth`, `pageHeight`, and transparency from the generated XML and must return exactly that size and the correct transparent mode; do not post-resize the PNG.

The generated document must be canonical uncompressed UTF-8 XML with editable `mxCell` vertices/edges and plain labels (`html=0`). The generator uses a closed scene schema and encodes attached endpoint coordinates as exact entry/exit anchors. Do not hand-add wrapper objects, HTML, links, embedded backgrounds, or flattened screenshots. The host's draw.io MCP is optional: when available, `search_shapes` may help find a special library stencil, but the workflow must not depend on MCP availability.

### 5. Compare and iterate

Run the comparison and persist its diagnostics, for example:

```bash
python3 scripts/compare_images.py <source.png> <render.png> \
  --threshold 0.05 \
  --json-output <iteration>/metrics.json \
  --diff-heatmap <iteration>/diff-heatmap.png \
  --diff-mask <iteration>/diff-mask.png
```

Require identical width and height; never use `--resize-candidate` for an acceptance run. Keep reference, candidate, JSON, heatmap, and mask paths distinct—the comparison rejects aliases and writes each requested output atomically. Review the white-composited RGBA MAE hard gate. SSIM, heatmap, mask, and difference regions use white-composited RGB only and remain diagnostic.

Save each iteration under a new number (`iter-00`, `iter-01`, ...). Correct the highest-impact regions first, regenerate, render, and compare again. For each iteration record the command, input hashes, MAE, SSIM if reported, changed scene elements, and artifact paths.

Stop successfully only when normalized RGBA MAE `< 0.05`. Use at most **8 correction iterations after the initial render**. Stop early for stagnation when two consecutive corrections each improve MAE by less than **0.001 absolute**; retain the best-scoring iteration, diagnose the persistent regions, and do not burn iterations on unmeasured tweaks.

### 6. Validate the deliverable

Run `python3 scripts/validate_drawio.py --json <best.drawio> > <work>/validation.json` and preserve the report. The `defusedxml`-based validator accepts only canonical, uncompressed UTF-8, vector-only XML and rejects every image/SVG/HTML/embed/link payload. Do not suppress or bypass a validation error.

The final file passes only if all of these hold:

- the `.drawio` parses and has the expected canvas dimensions;
- major shapes, text, and connectors are editable cells;
- `stats.image_cells == 0` and there is no image data hidden in values/styles;
- render dimensions exactly match the source;
- transparency-normalized RGBA MAE is `< 0.05`;
- the validator reports success.

## Handoff

Return the `.drawio` plus the evidence directory or a compact manifest linking to it. State the source and output dimensions, final MAE, validator result, image-cell count, and iteration count.

If the threshold or validation is not met after the iteration/stagnation limit, explicitly report **not compliant**. Provide the best MAE, remaining diff regions, validation failures, best candidate path, and evidence paths. Never describe a visually plausible but failing result as completed or successful.

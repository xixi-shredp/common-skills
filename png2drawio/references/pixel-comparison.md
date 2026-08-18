# Pixel comparison and stopping rules

Pixel comparison is a hard acceptance gate, not a substitute for editability validation. Always compare the source PNG with a fresh render of the candidate `.drawio`.

## Canonical comparison

The two images must have exactly the same width and height. A size mismatch is a failure; do not rescale one image inside the comparison step.

Convert both images to RGBA and alpha-composite them on opaque white before computing error. This normalizes hidden RGB values throughout transparent and partially transparent pixels so invisible channel data cannot create arbitrary error. The hard-gate MAE uses all four resulting RGBA channels. Use the normalization implemented by `compare_images.py` consistently for every iteration.

For source array `A` and candidate array `B`, with 8-bit RGBA channels normalized to `[0,1]`, normalized RGBA MAE is conceptually:

```text
MAE = mean(abs(A_normalized - B_normalized))
```

The only similarity pass threshold is:

```text
same dimensions AND normalized RGBA MAE < 0.05
```

The inequality is strict: `0.0500` does not pass. Never substitute rounded display text for the full-precision value used by the tool.

`compare_images.py` exits `0` only when the gate passes, `1` for a valid comparison that does not pass (including a size mismatch), and `2` for a tool/input error. Exit `1` is an expected iteration signal, not evidence that the diagnostic artifacts are unusable. `--resize-candidate` is diagnostic-only and can never pass.

SSIM, per-pixel error, heatmap, mask, and connected regions use only the white-composited RGB channels. They may reveal structural or antialiasing differences but are diagnostic only. A high SSIM cannot override the RGBA MAE gate, a size mismatch, or validation failure.

Keep the reference, candidate, heatmap, mask, and JSON output paths distinct. The tool rejects identical paths and aliases (including existing hard-link aliases) so evidence cannot overwrite an input or another artifact. Requested diagnostic PNGs and JSON are written to temporary files, verified where applicable, and atomically replaced at their final paths.

## Reading difference evidence

Use the diff image and connected difference regions (reported largest-area first) to find the largest remaining error. Typical signatures are:

| Diff pattern | Likely correction |
| --- | --- |
| Broad uniform page error | background color/alpha or page bounds |
| Double border/edge halo | position, size, stroke width, or antialiasing mismatch |
| Error concentrated at corners | corner radius or line join/cap |
| Localized glyph-shaped error | text content, font, size, weight, line height, or alignment |
| Long thin region | connector path, dash, width, or arrowhead |
| Correct shape but shifted block | parent offset, coordinate origin, or unintended auto-layout |

Prioritize regions by their error contribution rather than by visual salience alone. Change a small set of related scene properties per iteration so the metric delta remains attributable.

## Iteration accounting

`iter-00` is the initial analysis/correction render. After it, allow at most eight correction iterations (`iter-01` through `iter-08`). Each iteration should retain:

- the exact `scene.json` and `.drawio` candidate;
- the rendered PNG, metrics JSON (including its region report), heatmap, and mask;
- commands and tool versions when available;
- source/candidate hashes;
- the previous and current full-precision MAE;
- a short note naming changed elements and the reason.

Stop early for stagnation if two consecutive correction iterations each reduce MAE by less than `0.001` absolute. Example: improvements of `0.0007` and `0.0004` trigger stagnation. A regression counts as improvement below `0.001`. Keep the lowest-MAE candidate, not necessarily the final one.

If stopped by the iteration cap or stagnation, the outcome is **not compliant** unless the best candidate already meets every acceptance condition. Report the best MAE, remaining high-error regions, validation result, and evidence locations; do not call the reconstruction successful.

## Evidence manifest

A compact manifest should identify the immutable source (path, dimensions, SHA-256), every iteration and its artifacts, the selected best iteration, its exact MAE, dimension check, validator outcome, and zero image-cell count. This makes the claim reproducible without relying on screenshots or subjective visual judgment.

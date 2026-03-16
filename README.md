# DeltaE2000

Semi-automatic evaluation tool for **ColorChecker Digital SG / ColorChecker SG** using the **CIEDE2000 (ΔE00)** metric.

This script is designed for digitization and QA workflows where a chart image must be compared against target-specific reference values in **CIE Lab**.

---

## What the current version does

The current script version:

- loads an input image such as **TIFF**, **JPEG**, or **PNG**,
- uses an **embedded ICC profile** if available,
- can use a user-supplied fallback ICC profile via `--icc`,
- lets the user manually select the 4 chart corners,
- rectifies the chart using a perspective transform,
- samples central ROIs from a regular **14 × 10** CCSG grid by default,
- converts measured RGB values to **CIE Lab** via **LittleCMS** through `Pillow.ImageCms`,
- compares measured Lab values to a reference table,
- computes **ΔE00** for each patch,
- computes **ΔL\***, **Δa\***, and **Δb\*** for each patch,
- derives a **neutral scale subset** from reference chroma,
- creates plots, CSV/JSON outputs, and a bilingual HTML report,
- optionally evaluates the result against **Metamorfoze** thresholds. fileciteturn12file1

---

## Supported reference formats

The current version supports:

- `.csv`
- `.txt`
- `.xlsx`
- `.xlsm`
- `.xltx`
- `.xltm`
- `.xls` fileciteturn12file1

### Supported TXT variants

The TXT parser supports both:

1. **Simple tabular TXT** with columns such as:
   - `Patch`
   - `LAB_L`
   - `LAB_A`
   - `LAB_B`

2. **CGATS-like TXT** with:
   - `BEGIN_DATA_FORMAT`
   - `BEGIN_DATA`
   - patch/sample column names such as `Sample_NAME`, `SAMPLE_NAME`, `SampleID`, `patch`, or `name`
   - LAB columns such as `LAB_L`, `LAB_A`, `LAB_B`. fileciteturn12file1turn12file7

### Supported table structures

#### Explicit table format

Example:

```text
patch,L,a,b,row,col
A1,96.29,-0.54,1.50,0,0
A2,10.69,-0.33,-1.01,1,0
```

#### Excel-style format with patch names and Lab columns

Example:

```text
patch | L* | a* | b*
A1    | 96,2985 | -0,5458 | 1,5096
B10   | 49,3193 | -0,3254 | 0,3267
```

The loader:

- normalizes decimal commas,
- recognizes columns such as `L*`, `a*`, `b*`, `LAB_L`, `LAB_A`, `LAB_B`,
- derives `row` and `col` from patch names like `A1`, `B10`, `N10`. fileciteturn12file1

---

## Patch naming and grid model

The script assumes CCSG naming in this form:

- **letters = columns**
- **numbers = rows**

Examples:

- `A1` → row `0`, col `0`
- `B10` → row `9`, col `1`
- `N10` → row `9`, col `13`

Default grid:

- `--grid-cols 14`
- `--grid-rows 10`

This corresponds to:

- columns: **A–N**
- rows: **1–10**. fileciteturn12file1

---

## ICC handling

The script uses ICC-based conversion to Lab.

Behavior:

- if the image contains an **embedded ICC profile**, that profile is used,
- otherwise the script expects a fallback ICC profile via:

```bash
--icc profile.icc
```

If neither an embedded profile nor `--icc` is available, the script stops with an error. fileciteturn12file1

---

## How the measurement works

1. The image is loaded.
2. An ICC profile is selected.
3. The user selects 4 chart corners in this order:
   - top-left
   - top-right
   - bottom-right
   - bottom-left
4. The chart is rectified with a perspective transform.
5. A regular patch grid is applied.
6. A central ROI is sampled for each patch.
7. Median RGB is computed from the ROI.
8. RGB is converted to **Lab** using the ICC transform.
9. Measured Lab is compared with reference Lab.
10. **ΔE00** is computed for each patch.
11. ΔL\*, Δa\*, Δb\*, and neutral-scale statistics are derived.
12. Outputs are written to disk. fileciteturn12file1

---

## Command-line arguments

### Required arguments

- `--image` — input image
- `--reference` — reference table
- `--output-dir` — output directory

### Optional arguments

- `--icc` — fallback ICC profile
- `--grid-cols` — number of grid columns, default `14`
- `--grid-rows` — number of grid rows, default `10`
- `--rectified-width` — width of the rectified chart image, default `1400`
- `--patch-fill` — fraction of each patch cell used as the sampling ROI, default `0.50`
- `--neutral-chroma-threshold` — threshold used to derive neutral patches from reference chroma, default `5.0`
- `--metamorfoze-level` — one of `full`, `light`, `extra-light`, `none`
- `--no-gui` — disables interactive corner selection
- `--corners` — manually provide 4 corner coordinates
- `--debug` — enables extra debug output
- `--run-tests` — runs built-in self-tests and exits
- `--skip-colourspace-plot` — skips the chromaticity plot
- `--skip-rgb-bars-plot` — skips the measured RGB bar chart
- `--skip-html-report` — skips HTML report generation. fileciteturn12file1

---

## Example usage

### Interactive use

```bash
python deltae2000.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --icc eciRGB_v2.icc
```

### TXT reference input

```bash
python deltae2000.py \
  --image "ColorChecker SG.tif" \
  --reference "x-rite_ColorCheckerSG-0716_LAB_61025-1508.txt" \
  --output-dir out
```

### Non-interactive use with explicit corners

```bash
python deltae2000.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --no-gui \
  --corners 100 100 1200 100 1200 900 100 900
```

### Metamorfoze evaluation

```bash
python deltae2000.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --metamorfoze-level full
```

---

## Metamorfoze thresholds

The current implementation supports these threshold sets:

### Full

- Mean ΔE00 ≤ **3.0**
- Max ΔE00 ≤ **7.0**

### Light

- Mean ΔE00 ≤ **4.0**
- Max ΔE00 ≤ **14.0**

### Extra-light / none

These modes do not currently apply a numerical pass/fail decision in the script. fileciteturn12file1

---

## Output files

The current version writes the following outputs into the directory specified by `--output-dir`.

### Images and plots

- `rectified.png` — perspective-corrected chart image
- `overlay.png` — rectified chart with ROIs and ΔE00 labels
- `deltae_heatmap.png` — ΔE00 heatmap
- `deltaL_heatmap.png` — ΔL\* heatmap
- `deltaa_heatmap.png` — Δa\* heatmap
- `deltab_heatmap.png` — Δb\* heatmap
- `top_patches.png` — worst-patch bar chart
- `lstar_scatter.png` — reference vs measured L\* scatter plot
- `neutral_scale_plot.png` — neutral-scale plot
- `colourspace_chromaticity.png` — chromaticity plot against **sRGB**, **ECI RGB v2**, and **Adobe RGB (1998)**
- `measured_rgb_bars.png` — grouped 2D bar chart of measured RGB values. fileciteturn12file1turn12file5

### Data outputs

- `measurements.csv` — detailed per-patch numeric output
- `summary.json` — summary statistics, generated plot list, Metamorfoze status, neutral-scale summary, and worst patches
- `report.html` — bilingual HTML report with CZE/ENG language switch. fileciteturn12file1turn12file9

---

## HTML report

The current script generates an HTML report unless `--skip-html-report` is used.

The report includes:

- input metadata
- summary statistics
- Metamorfoze block
- neutral-scale summary
- explanatory text for each plot in Czech and English
- plot sections shown as full-width stacked blocks
- worst-patches table
- patch legend table with:
  - reference swatch
  - measured swatch
  - reference Lab
  - measured Lab
  - measured RGB
  - ΔE00
- a language switch between **CZE** and **ENG**. fileciteturn12file1turn12file4

---

## Neutral-scale evaluation

The script derives neutral patches from the **reference table** using reference chroma:

- `reference_chroma = sqrt(a² + b²)`
- a patch is treated as neutral when `reference_chroma <= --neutral-chroma-threshold`

The neutral summary includes:

- patch count
- patch list
- mean / max ΔE00
- mean / max absolute ΔL\*
- mean / max absolute Δa\*
- mean / max absolute Δb\*. fileciteturn12file1

---

## Notes on Lab decoding

The script contains an important fix for `Pillow.ImageCms` LAB output.

For LAB images returned by ImageCms:

- `L` is stored as unsigned 8-bit and must be rescaled from `0..255` to `0..100`
- `a` and `b` must be interpreted as **signed int8** values

Treating `a` and `b` incorrectly as unsigned values produces invalid Lab data and unrealistically high ΔE values. fileciteturn12file1

---

## Internal tests

Run built-in tests with:

```bash
python deltae2000.py --run-tests
```

The current tests cover:

- patch name parsing
- decimal-comma parsing
- neutral chroma computation
- European Excel-style reference parsing
- signed LAB decoding
- CSV reference loading
- simple TXT reference loading
- minimal CGATS TXT loading
- extended CGATS TXT loading. fileciteturn12file7

---

## Practical recommendations

- Use a **target-specific reference file** for the exact chart instance whenever available.
- Verify chart orientation before measuring.
- Check the ROI overlay first if ΔE values look suspicious.
- If results are unexpectedly high, verify:
  - the ICC profile,
  - the reference file,
  - corner selection,
  - chart orientation,
  - grid dimensions,
  - `--patch-fill`,
  - the neutral chroma threshold if you rely on the neutral subset. fileciteturn12file1

---

## Current limitations

- Corner selection is still manual unless `--corners` is provided.
- The chart is modeled as a regular grid.
- No automatic target detection is implemented.
- The script is optimized for CCSG naming and default geometry. fileciteturn12file1

---

## Summary

`deltae2000.py` is a semi-automatic CCSG evaluation script that combines:

- manual geometric selection,
- ICC-aware RGB-to-Lab conversion,
- per-patch ΔE00 / ΔL\* / Δa\* / Δb\* analysis,
- TXT/CSV/Excel reference loading,
- neutral-scale evaluation,
- Metamorfoze threshold checking,
- chromaticity and RGB visualizations,
- CSV/JSON/HTML reporting.

# DeltaE2000

Semi-automatic evaluation tool for **ColorChecker SG** using the **CIEDE2000 (ΔE00)** metric.

`DeltaE2000` is intended for digitization and imaging QA workflows in which a chart image is compared against target-specific reference values in **CIE Lab**.

---

## Features

The current version of the script:

* loads input images such as **TIFF**, **JPEG**, or **PNG**
* uses an **embedded ICC profile** when available
* accepts a user-supplied fallback ICC profile via `--icc`
* supports manual selection of the 4 chart corners
* rectifies the chart using a perspective transform
* samples central ROIs from a regular **14 × 10** CCSG grid by default
* converts measured RGB values to **CIE Lab** using **LittleCMS** through `Pillow.ImageCms`
* compares measured Lab values against a reference table
* computes **ΔE00** for each patch
* supports both:

  * standard **CIEDE2000**
  * **Metamorfoze-oriented CIEDE2000 with `S_L = 1`**
* computes **ΔL***, **Δa***, and **Δb***
* derives a neutral-patch subset from reference chroma
* generates plots, CSV/JSON outputs, and a bilingual HTML report
* optionally evaluates results against **Metamorfoze** thresholds

---

## Supported Reference Formats

The script supports the following reference file types:

* `.csv`
* `.txt`
* `.xlsx`
* `.xlsm`
* `.xltx`
* `.xltm`
* `.xls`

### Supported TXT Variants

Two TXT layouts are supported:

#### 1. Simple tabular TXT

Expected columns may include:

* `Patch`
* `LAB_L`
* `LAB_A`
* `LAB_B`

#### 2. CGATS-like TXT

Supported structure includes:

* `BEGIN_DATA_FORMAT`
* `BEGIN_DATA`

Supported patch/sample field names may include:

* `Sample_NAME`
* `SAMPLE_NAME`
* `SampleID`
* `patch`
* `name`

Supported Lab field names may include:

* `LAB_L`
* `LAB_A`
* `LAB_B`

### Supported Table Structures

#### Explicit table format

```text
patch,L,a,b,row,col
A1,96.29,-0.54,1.50,0,0
A2,10.69,-0.33,-1.01,1,0
```

#### Excel-style format with patch names

```text
patch | L* | a* | b*
A1    | 96,2985 | -0,5458 | 1,5096
B10   | 49,3193 | -0,3254 | 0,3267
```

The loader:

* normalizes decimal commas
* recognizes column names such as `L*`, `a*`, `b*`, `LAB_L`, `LAB_A`, `LAB_B`
* derives `row` and `col` from patch names such as `A1`, `B10`, `N10`

---

## Patch Naming and Grid Model

The script assumes CCSG naming in the following form:

* **letters = columns**
* **numbers = rows**

Examples:

* `A1` → row `0`, col `0`
* `B10` → row `9`, col `1`
* `N10` → row `9`, col `13`

Default grid configuration:

* `--grid-cols 14`
* `--grid-rows 10`

This corresponds to:

* columns: **A–N**
* rows: **1–10**

---

## ICC Handling

The script performs ICC-based conversion to Lab.

Behavior:

* if the input image contains an **embedded ICC profile**, that profile is used
* otherwise a fallback profile must be supplied with:

```bash
--icc profile.icc
```

If neither an embedded profile nor `--icc` is available, the script exits with an error.

---

## Measurement Workflow

1. Load the image.
2. Select the ICC profile.
3. Select 4 chart corners in this order:

   * top-left
   * top-right
   * bottom-right
   * bottom-left
4. Rectify the chart with a perspective transform.
5. Apply a regular patch grid.
6. Sample a central ROI for each patch.
7. Compute median RGB from each ROI.
8. Convert RGB to **Lab** using the ICC transform.
9. Compare measured Lab with reference Lab.
10. Apply the selected **Delta E** method.
11. Derive ΔL*, Δa*, Δb*, and neutral-scale statistics.
12. Write outputs to disk.

---

## Delta E Methods

Two Delta E calculation modes are available.

### `cie2000`

Standard **CIEDE2000** calculation.

This is the default mode.

### `metamorfoze-sl1`

Metamorfoze-oriented variant of **CIEDE2000** in which:

* `S_L` is fixed to **1**
* chroma and hue components follow the usual CIEDE2000 structure

This mode is intended for workflows that require the Metamorfoze interpretation of the formula.

### Default

```bash
--deltae-method cie2000
```

---

## Command-Line Arguments

### Required arguments

* `--image` — input image
* `--reference` — reference table
* `--output-dir` — output directory

### Optional arguments

* `--icc` — fallback ICC profile
* `--grid-cols` — number of grid columns, default `14`
* `--grid-rows` — number of grid rows, default `10`
* `--rectified-width` — width of the rectified image, default `1400`
* `--patch-fill` — central ROI fill ratio, default `0.50`
* `--neutral-chroma-threshold` — neutral-patch threshold, default `5.0`
* `--deltae-method` — `cie2000` or `metamorfoze-sl1`
* `--metamorfoze-level` — `full`, `light`, `extra-light`, or `none`
* `--no-gui` — disable interactive corner selection
* `--corners` — manually specify 4 corner coordinates
* `--debug` — enable extra debug output
* `--run-tests` — run built-in tests and exit
* `--skip-colourspace-plot` — skip chromaticity plot generation
* `--skip-rgb-bars-plot` — skip measured RGB bar chart generation
* `--skip-html-report` — skip HTML report generation

---

## Example Usage

### Interactive mode

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

### Non-interactive mode with explicit corners

```bash
python deltae2000.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --no-gui \
  --corners 100 100 1200 100 1200 900 100 900
```

### Standard CIEDE2000

```bash
python deltae2000.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --deltae-method cie2000
```

### Metamorfoze-oriented Delta E

```bash
python deltae2000.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --deltae-method metamorfoze-sl1 \
  --metamorfoze-level full
```

---

## Metamorfoze Thresholds

The script currently supports the following threshold sets:

### Full

* Mean ΔE00 ≤ **3.0**
* Max ΔE00 ≤ **7.0**

### Light

* Mean ΔE00 ≤ **4.0**
* Max ΔE00 ≤ **14.0**

### Extra-light / none

These modes currently do not apply a numerical pass/fail decision.

---

## Output Files

All outputs are written into the directory specified by `--output-dir`.

### Images and plots

* `rectified.png` — perspective-corrected chart image
* `overlay.png` — rectified chart with sampled ROIs and ΔE00 labels
* `deltae_heatmap.png` — ΔE00 heatmap
* `deltaL_heatmap.png` — ΔL* heatmap
* `deltaa_heatmap.png` — Δa* heatmap
* `deltab_heatmap.png` — Δb* heatmap
* `top_patches.png` — worst-patch bar chart
* `lstar_scatter.png` — reference vs measured L* scatter plot
* `neutral_scale_plot.png` — neutral-scale plot
* `colourspace_chromaticity.png` — chromaticity plot against **sRGB**, **eciRGB v2**, and **Adobe RGB (1998)**
* `measured_rgb_bars.png` — grouped 2D bar chart of measured RGB values

### Data outputs

* `measurements.csv` — detailed per-patch measurements
* `summary.json` — summary statistics, selected ΔE method, plot list, Metamorfoze status, neutral-scale summary, and worst patches
* `report.html` — bilingual HTML report with CZE/ENG language switch

---

## HTML Report

Unless `--skip-html-report` is used, the script generates a bilingual HTML report.

The report includes:

* input metadata
* summary statistics
* selected **ΔE method**
* Metamorfoze evaluation block
* neutral-scale summary
* explanatory plot descriptions in Czech and English
* full-width stacked plot sections
* worst-patches table
* patch legend table containing:

  * reference swatch
  * measured swatch
  * reference Lab
  * measured Lab
  * measured RGB
  * ΔE00
* language switch between **CZE** and **ENG**

---

## Neutral-Scale Evaluation

Neutral patches are derived from the **reference table** using reference chroma:

* `reference_chroma = sqrt(a² + b²)`
* a patch is treated as neutral when:

```text
reference_chroma <= --neutral-chroma-threshold
```

The neutral summary includes:

* patch count
* patch list
* mean / max ΔE00
* mean / max absolute ΔL*
* mean / max absolute Δa*
* mean / max absolute Δb*

---

## Notes on Lab Decoding

The script includes an important correction for `Pillow.ImageCms` Lab output.

For Lab images returned by ImageCms:

* `L` is stored as unsigned 8-bit and must be rescaled from `0..255` to `0..100`
* `a` and `b` must be interpreted as **signed int8**

If `a` and `b` are incorrectly interpreted as unsigned values, the resulting Lab values are invalid and ΔE results become unrealistically high.

---

## Internal Tests

Run built-in tests with:

```bash
python deltae2000.py --run-tests
```

The current tests cover:

* patch name parsing
* decimal-comma parsing
* neutral chroma computation
* European Excel-style reference parsing
* signed Lab decoding
* CSV reference loading
* simple TXT reference loading
* minimal CGATS TXT loading
* extended CGATS TXT loading
* difference between standard CIEDE2000 and the `metamorfoze-sl1` variant

---

## Practical Recommendations

* Use a **target-specific reference file** for the exact chart instance whenever available.
* Verify chart orientation before measurement.
* Check the ROI overlay first if ΔE values appear suspicious.
* If results are unexpectedly high, verify:

  * ICC profile
  * reference file
  * corner selection
  * chart orientation
  * grid dimensions
  * `--patch-fill`
  * selected `--deltae-method`
  * neutral chroma threshold if the neutral subset is important

---

## Limitations

* Corner selection remains manual unless `--corners` is provided.
* The chart is modeled as a regular grid.
* No automatic target detection is implemented.
* The script is optimized for CCSG naming and default geometry.
* `metamorfoze-sl1` is a workflow-specific implementation and should not be confused with the default CIEDE2000 formula.

---

## Summary

`deltae2000.py` is a semi-automatic CCSG evaluation script that combines:

* manual geometric selection
* ICC-aware RGB-to-Lab conversion
* per-patch ΔE00 / ΔL* / Δa* / Δb* analysis
* selectable Delta E method (`cie2000` or `metamorfoze-sl1`)
* TXT/CSV/Excel reference loading
* neutral-scale evaluation
* Metamorfoze threshold checking
* chromaticity and RGB visualizations
* CSV/JSON/HTML reporting

---

## AI-Generated Code Disclosure

This code was generated using **ChatGPT 5.4**.

# DeltaE2000

Semi-automatic evaluation tool for **ColorChecker Digital SG (CCSG)** in digitization and imaging QA workflows.

`DeltaE2000` compares measured patch values against target-specific **CIE Lab** references and supports both general **CIEDE2000** reporting and **Metamorfoze-oriented** evaluation from a single centre-position chart capture.

---

## What the Script Evaluates

From a single **centre-position CCSG capture**, the script can evaluate:

* **color accuracy** using **CIE2000 SL = 1**
* **white balance in the image centre** using **CIE2000 without luminance** = **ΔE(ab)***
* **tone reproduction / exposure** on neutral patches using **ΔL***
* **approximate gain modulation** on neutral CCSG patches
* per-patch **ΔL***, **Δa***, **Δb***
* per-patch standard **CIEDE2000 (ΔE00)** for reference and diagnostics

It also generates visualizations, CSV/JSON outputs, and an HTML report.

---

## Important Scope Limitation

This script is designed for evaluation from a **single CCSG image in the image centre**.

That means it can evaluate central chart-based metrics well, but it does **not** fully evaluate the following whole-image-plane requirements on its own:

* white balance across the entire image plane
* illumination non-uniformity across the entire image plane
* frame-filling white-sheet measurements
* noise from repeated captures
* file-format / metadata policy compliance
* MTF, sharpening, or geometric distortion validation

For full Metamorfoze compliance, additional targets and measurements may still be required.

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
* computes per-patch:
  * **ΔE00** (standard CIEDE2000)
  * **ΔE*** with **CIE2000 SL = 1**
  * **ΔE(ab)*** using **CIE2000 without luminance**
  * **ΔL***, **Δa***, and **Δb***
* derives a neutral-patch subset from reference chroma
* evaluates Metamorfoze levels:
  * `full`
  * `light`
  * `extra-light`
* generates plots, CSV/JSON outputs, and an HTML report
* includes built-in self-tests

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

Two TXT layouts are supported.

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
10. Compute:
    * standard **ΔE00**
    * **ΔE*** using **CIE2000 SL = 1**
    * **ΔE(ab)*** using **CIE2000 without luminance**
    * **ΔL***, **Δa***, **Δb***
11. Derive the neutral-patch subset.
12. Apply optional Metamorfoze evaluation.
13. Write outputs to disk.

---

## Computed Metrics

### 1. Standard CIEDE2000

The script computes per-patch **ΔE00** using standard **CIEDE2000**.

This is included mainly for diagnostic and comparison purposes.

### 2. Metamorfoze Color Accuracy

For Metamorfoze color reproduction, the script computes **ΔE*** using **CIE2000 SL = 1**.

This is used for:

* overall mean color accuracy
* maximum color error
* worst-patch ranking
* Metamorfoze **Full** and **Light** evaluation

### 3. Metamorfoze White Balance

For Metamorfoze white balance, the script computes **ΔE(ab)*** using **CIE2000 without luminance**.

This is evaluated on the neutral-patch subset in the image centre.

### 4. Tone Reproduction / Exposure

The script computes **ΔL*** for each patch and evaluates absolute **ΔL*** on neutral patches.

### 5. Gain Modulation

The script approximates gain modulation from neighboring neutral CCSG steps.

This is useful as an internal diagnostic, but it remains an approximation because dedicated linear gray targets are preferable for formal gain-modulation assessment.

---

## Neutral-Patch Subset

Neutral patches are derived from the **reference table** using reference chroma:

```text
reference_chroma = sqrt(a² + b²)
```

A patch is treated as neutral when:

```text
reference_chroma <= --neutral-chroma-threshold
```

Default:

```bash
--neutral-chroma-threshold 5.0
```

For Metamorfoze evaluation, the script then applies the level-specific neutral range:

* **Full** → neutrals analysed up to **L* 5**
* **Light** → neutrals analysed up to **L* 20**
* **Extra-light** → neutrals analysed up to **L* 30**

In script terms, this means the neutral subset is limited according to the selected Metamorfoze level before white-balance, exposure, and gain-modulation checks are applied.

---

## Metamorfoze Evaluation Logic

The script supports the following Metamorfoze-oriented checks from a single CCSG capture.

### Full

* **White balance**: ΔE(ab)* ≤ **3**
* **Tone reproduction / exposure**: ΔL* ≤ **2**
* **Gain modulation highlights**: **80% – 110%**
* **Gain modulation other neutral steps**: **60% – 140%**
* **Color reproduction**: mean ΔE* ≤ **3**, max ΔE* ≤ **7**

### Light

* **White balance**: ΔE(ab)* ≤ **3**
* **Tone reproduction / exposure**: ΔL* ≤ **2**
* **Gain modulation highlights**: **80% – 110%**
* **Gain modulation other neutral steps**: **60% – 140%**
* **Color reproduction**: mean ΔE* ≤ **4**, max ΔE* ≤ **14**

### Extra-light

* **White balance**: ΔE(ab)* ≤ **5**
* **Tone reproduction / exposure**: ΔL* ≤ **4**
* **Gain modulation highlights**: **80% – 110%**
* **Gain modulation other neutral steps**: **60% – 140%**
* **Color reproduction**: **not technically specified** as a formal pass/fail threshold in this script

### Notes

* **Color reproduction** pass/fail is only applied for **Full** and **Light**.
* **Extra-light** reports color statistics, but does not apply a formal numeric pass/fail decision for color accuracy.
* Whole-image-plane white balance and illumination uniformity are **outside the scope** of a single centre-position CCSG capture.

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
python deltae_metamorfoze.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --icc eciRGB_v2.icc
```

### TXT reference input

```bash
python deltae_metamorfoze.py \
  --image "ColorChecker SG.tif" \
  --reference "x-rite_ColorCheckerSG-0716_LAB_61025-1508.txt" \
  --output-dir out
```

### Non-interactive mode with explicit corners

```bash
python deltae_metamorfoze.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --no-gui \
  --corners 100 100 1200 100 1200 900 100 900
```

### Metamorfoze Full evaluation

```bash
python deltae_metamorfoze.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --metamorfoze-level full
```

### Metamorfoze Light evaluation

```bash
python deltae_metamorfoze.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --metamorfoze-level light
```

### Metamorfoze Extra-light evaluation

```bash
python deltae_metamorfoze.py \
  --image "ColorChecker SG.tif" \
  --reference "Profile_ColorCheckerSG_6.xlsx" \
  --output-dir out \
  --metamorfoze-level extra-light
```

---

## Output Files

All outputs are written into the directory specified by `--output-dir`.

### Images and plots

* `rectified.png` — perspective-corrected chart image
* `overlay.png` — rectified chart with sampled ROIs and short labels
* `deltaE_sl1_heatmap.png` — **ΔE*** heatmap using **CIE2000 SL = 1**
* `deltaE_ab_heatmap.png` — **ΔE(ab)*** heatmap for white balance
* `deltaL_heatmap.png` — **ΔL*** heatmap
* `deltaa_heatmap.png` — **Δa*** heatmap
* `deltab_heatmap.png` — **Δb*** heatmap
* `top_patches.png` — worst-patch bar chart by **ΔE*** (SL=1)
* `lstar_scatter.png` — reference vs measured **L*** scatter plot
* `neutral_scale_plot.png` — neutral-scale plot
* `colourspace_chromaticity.png` — chromaticity plot against **sRGB**, **eciRGB v2**, and **Adobe RGB (1998)**
* `measured_rgb_bars.png` — grouped 2D bar chart of measured RGB values

### Data outputs

* `measurements.csv` — detailed per-patch measurements
* `summary.json` — summary statistics and Metamorfoze evaluation blocks
* `report.html` — HTML report

---

## CSV Output

`measurements.csv` includes per-patch fields such as:

* patch name
* grid position
* measured RGB
* reference Lab
* measured Lab
* `deltaL`, `deltaa`, `deltab`
* `deltaE_cie2000`
* `deltaE_sl1`
* `deltaEab`
* neutral flag
* ROI coordinates

---

## JSON Output

`summary.json` includes:

* script version
* input image path
* reference file path
* profile used
* patch count
* mean standard **ΔE00**
* mean / max **ΔE*** with **SL = 1**
* neutral summary
* full Metamorfoze evaluation block:
  * white balance
  * exposure
  * gain modulation
  * color accuracy
  * overall pass status
* selected chart corner coordinates

---

## HTML Report

Unless `--skip-html-report` is used, the script generates an HTML report.

The report includes:

* input metadata
* summary statistics
* Metamorfoze evaluation block
* heatmaps and diagnostic plots
* worst-patches table
* patch table with:
  * reference swatch
  * measured swatch
  * reference Lab
  * measured Lab
  * measured RGB
  * ΔE* (SL=1)
  * ΔE(ab)*
  * ΔL*

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
python deltae_metamorfoze.py --run-tests
```

The current tests cover:

* patch name parsing
* decimal-comma parsing
* **ΔE(ab)*** behavior for pure lightness differences
* difference between standard CIEDE2000 and the **SL = 1** variant

---

## Practical Recommendations

* Use a **target-specific reference file** for the exact chart instance whenever available.
* Verify chart orientation before measurement.
* Check the ROI overlay first if results appear suspicious.
* If results are unexpectedly high, verify:
  * ICC profile
  * reference file
  * corner selection
  * chart orientation
  * grid dimensions
  * `--patch-fill`
  * neutral chroma threshold
* Treat CCSG-based gain modulation as an **approximation**.
* Do not interpret centre-chart measurements as a full replacement for whole-field uniformity tests.

---

## Limitations

* Corner selection remains manual unless `--corners` is provided.
* The chart is modeled as a regular grid.
* No automatic target detection is implemented.
* The script is optimized for CCSG naming and default geometry.
* White balance and illumination evaluation across the full image plane require additional targets or frame-filling measurements.
* Gain modulation from CCSG neutrals is informative, but not a full substitute for dedicated tonal targets.

---

## Summary

`deltae_metamorfoze.py` is a semi-automatic CCSG evaluation script that combines:

* manual geometric selection
* ICC-aware RGB-to-Lab conversion
* per-patch **ΔE00 / ΔE* (SL=1) / ΔE(ab)* / ΔL* / Δa* / Δb*** analysis
* TXT/CSV/Excel reference loading
* neutral-scale filtering
* Metamorfoze-oriented white balance, exposure, gain modulation, and color-accuracy checks
* chromaticity and RGB visualizations
* CSV/JSON/HTML reporting

---

## AI-Generated Code Disclosure

This code was generated with assistance from **ChatGPT**.


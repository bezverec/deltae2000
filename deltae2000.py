#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeltaE2000

What this script evaluates reliably from a single CCSG capture:
- Color accuracy (CIE2000 SL=1)
- White balance on neutrals in the image centre (CIE2000 without luminance = ΔE(ab)*)
- Exposure on neutrals in the image centre (ΔL* using CIE2000SL=1 semantics; numerically abs(L_meas-L_ref))
- Approximate gain modulation on CCSG neutrals in the image centre

What this script does NOT fully evaluate:
- White balance across the entire image plane
- Illumination non-uniformity across the entire image plane
- File format / metadata compliance
- MTF / sharpening / geometric distortion / noise across repeated captures

Author: Jan Houserek + revised Metamorfoze logic
License: GPLv3
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import math
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from PIL import Image, ImageCms

try:
    import colour
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Package 'colour-science' is required. Install with: pip install colour-science"
    ) from exc


APP_TITLE = "DeltaE2000 v0.0.3"
SCRIPT_VERSION = "2026-03-19-deltae2000-v0.0.3"

METAMORFOZE_SPECS: Dict[str, Dict[str, object]] = {
    "full": {
        "neutral_lstar_floor": 5.0,
        "white_balance_limit": 3.0,      # ΔE(ab)*
        "exposure_limit": 2.0,           # ΔL*
        "gain_highlights_min": 80.0,     # %
        "gain_highlights_max": 110.0,    # %
        "gain_other_min": 60.0,          # %
        "gain_other_max": 140.0,         # %
        "color_mean_limit": 3.0,         # mean ΔE*
        "color_max_limit": 7.0,          # max ΔE*
    },
    "light": {
        "neutral_lstar_floor": 20.0,
        "white_balance_limit": 3.0,
        "exposure_limit": 2.0,
        "gain_highlights_min": 80.0,
        "gain_highlights_max": 110.0,
        "gain_other_min": 60.0,
        "gain_other_max": 140.0,
        "color_mean_limit": 4.0,
        "color_max_limit": 14.0,
    },
    "extra-light": {
        "neutral_lstar_floor": 30.0,
        "white_balance_limit": 5.0,
        "exposure_limit": 4.0,
        "gain_highlights_min": 80.0,
        "gain_highlights_max": 110.0,
        "gain_other_min": 60.0,
        "gain_other_max": 140.0,
        "color_mean_limit": None,
        "color_max_limit": None,
    },
    "none": {},
}


@dataclass
class PatchReference:
    patch: str
    L: float
    a: float
    b: float
    row: int
    col: int


@dataclass
class PatchMeasurement:
    patch: str
    row: int
    col: int
    rgb_mean_8bit: Tuple[float, float, float]
    lab_measured: Tuple[float, float, float]
    lab_reference: Tuple[float, float, float]
    delta_e_cie2000: float
    delta_e_sl1: float
    delta_e_ab: float
    delta_L: float
    delta_a: float
    delta_b: float
    reference_chroma: float
    is_neutral_reference: bool
    roi_rectified_xywh: Tuple[int, int, int, int]


@dataclass
class GainPair:
    patch_hi: str
    patch_lo: str
    L_ref_hi: float
    L_ref_lo: float
    L_meas_hi: float
    L_meas_lo: float
    ref_diff: float
    meas_diff: float
    gain_percent: float
    bucket: str  # "highlights" or "other"


def stderr_write(text: str) -> None:
    sys.stderr.write(text)
    if not text.endswith("\n"):
        sys.stderr.write("\n")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DeltaE2000 / Metamorfoze evaluator for ColorChecker Digital SG",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python deltae_metamorfoze.py --image sample.tif --reference ref.csv --output-dir out --icc eciRGB_v2.icc\n"
            "  python deltae_metamorfoze.py --image sample.tif --reference ref.txt --output-dir out --metamorfoze-level full\n"
            "  python deltae_metamorfoze.py --image sample.tif --reference ref.csv --output-dir out --corners 10 10 200 10 200 150 10 150 --no-gui\n"
        ),
    )
    parser.add_argument("--image", help="Input image path")
    parser.add_argument(
        "--reference",
        help=(
            "Reference table (.csv/.txt/.xlsx/.xls) with either columns patch,L,a,b,row,col, "
            "TXT variants with Patch/LAB_L/LAB_A/LAB_B or CGATS BEGIN_DATA_FORMAT/BEGIN_DATA blocks, "
            "or Excel-style columns patch/L*/a*/b* where row,col are derived from CCSG patch names like A1."
        ),
    )
    parser.add_argument("--output-dir", help="Directory for outputs")
    parser.add_argument(
        "--icc",
        default=None,
        help="Fallback ICC profile path if image has no embedded ICC profile",
    )
    parser.add_argument(
        "--grid-cols",
        type=int,
        default=14,
        help="Number of patch columns in the rectified chart grid (CCSG default: 14)",
    )
    parser.add_argument(
        "--grid-rows",
        type=int,
        default=10,
        help="Number of patch rows in the rectified chart grid (CCSG default: 10)",
    )
    parser.add_argument(
        "--rectified-width",
        type=int,
        default=1400,
        help="Output width for rectified target image",
    )
    parser.add_argument(
        "--patch-fill",
        type=float,
        default=0.50,
        help="Fraction of each patch cell used as central sampling ROI (0<value<=1)",
    )
    parser.add_argument(
        "--neutral-chroma-threshold",
        type=float,
        default=5.0,
        help="Reference chroma threshold for neutral-scale detection from reference Lab values",
    )
    parser.add_argument(
        "--metamorfoze-level",
        choices=["full", "light", "extra-light", "none"],
        default="none",
        help="Optional Metamorfoze evaluation thresholds",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Disable interactive corner picking; requires --corners",
    )
    parser.add_argument(
        "--corners",
        nargs=8,
        type=float,
        metavar=("x1", "y1", "x2", "y2", "x3", "y3", "x4", "y4"),
        help="Corner coordinates in image pixels: TL TR BR BL",
    )
    parser.add_argument("--debug", action="store_true", help="Enable extra console output")
    parser.add_argument("--run-tests", action="store_true", help="Run built-in self-tests and exit")
    parser.add_argument(
        "--skip-colourspace-plot",
        action="store_true",
        help="Skip 2D chromaticity plot comparing measured/reference patches against sRGB, eciRGB v2 and Adobe RGB (1998)",
    )
    parser.add_argument(
        "--skip-rgb-bars-plot",
        action="store_true",
        help="Skip grouped 2D bar chart of measured RGB values for all patches",
    )
    parser.add_argument(
        "--skip-html-report",
        action="store_true",
        help="Skip HTML report generation",
    )
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.run_tests:
        return args

    missing = [
        flag
        for flag, value in (
            ("--image", args.image),
            ("--reference", args.reference),
            ("--output-dir", args.output_dir),
        )
        if not value
    ]
    if missing:
        parser.print_help(sys.stderr)
        parser.exit(
            0 if argv is not None and len(list(argv)) == 0 else 2,
            "\nMissing required arguments: " + ", ".join(missing) + "\n",
        )

    if args.no_gui and not args.corners:
        parser.error("--no-gui requires --corners x1 y1 x2 y2 x3 y3 x4 y4")
    if args.grid_cols <= 0 or args.grid_rows <= 0:
        parser.error("--grid-cols and --grid-rows must be positive integers")
    if not (0 < args.patch_fill <= 1.0):
        parser.error("--patch-fill must be > 0 and <= 1.0")
    if args.neutral_chroma_threshold < 0:
        parser.error("--neutral-chroma-threshold must be >= 0")

    return args


def excel_column_label_to_index(label: str) -> int:
    value = 0
    for ch in label.upper():
        if not ("A" <= ch <= "Z"):
            raise ValueError(f"Invalid row label prefix: {label}")
        value = value * 26 + (ord(ch) - ord("A") + 1)
    return value - 1


def patch_name_to_row_col(patch_name: str) -> Tuple[int, int]:
    patch_name = str(patch_name).strip()
    if not patch_name:
        raise ValueError("Empty patch name")

    letters = ""
    digits = ""
    for ch in patch_name:
        if ch.isalpha() and not digits:
            letters += ch
        elif ch.isdigit():
            digits += ch
        else:
            raise ValueError(f"Unsupported patch name format: {patch_name}")

    if not letters or not digits:
        raise ValueError(f"Unsupported patch name format: {patch_name}")

    col = excel_column_label_to_index(letters)
    row = int(digits) - 1
    if row < 0:
        raise ValueError(f"Patch row must be >= 1 in patch name: {patch_name}")
    return row, col


def parse_float_maybe_comma(value: object) -> float:
    if pd.isna(value):
        raise ValueError("Unexpected empty numeric value in reference table")
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        raise ValueError("Unexpected empty numeric value in reference table")
    return float(text.replace(",", "."))

def load_reference_txt(path: str) -> pd.DataFrame:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = [line.rstrip("\n\r") for line in f]

    def normalize_name(name: str) -> str:
        return str(name).strip().lower().replace("*", "").replace("-", "_")

    def is_patch_name(text: str) -> bool:
        text = str(text).strip()
        return bool(text) and any(ch.isalpha() for ch in text) and any(ch.isdigit() for ch in text)

    begin_format_idx = None
    end_format_idx = None
    begin_data_idx = None
    end_data_idx = None

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "BEGIN_DATA_FORMAT":
            begin_format_idx = idx
        elif stripped == "END_DATA_FORMAT":
            end_format_idx = idx
        elif stripped == "BEGIN_DATA":
            begin_data_idx = idx
        elif stripped == "END_DATA":
            end_data_idx = idx

    if (
        begin_format_idx is not None
        and end_format_idx is not None
        and begin_data_idx is not None
        and end_data_idx is not None
        and begin_format_idx < end_format_idx < begin_data_idx < end_data_idx
    ):
        format_lines = [line.strip() for line in lines[begin_format_idx + 1 : end_format_idx] if line.strip()]
        if not format_lines:
            raise ValueError(f"TXT reference '{path}' contains BEGIN_DATA_FORMAT but no column definition.")

        format_tokens = re.split(r"\s+", " ".join(format_lines).strip())
        normalized_tokens = [normalize_name(token) for token in format_tokens]

        sample_name_candidates = {"sample_name", "patch", "name"}
        sample_id_candidates = {"sampleid", "sample_id"}
        l_candidates = {"lab_l", "l", "lstar"}
        a_candidates = {"lab_a", "a", "astar"}
        b_candidates = {"lab_b", "b", "bstar"}

        def find_index(candidates: set[str]) -> Optional[int]:
            for i, token in enumerate(normalized_tokens):
                if token in candidates:
                    return i
            return None

        patch_idx = find_index(sample_name_candidates)
        if patch_idx is None:
            patch_idx = find_index(sample_id_candidates)
        l_idx = find_index(l_candidates)
        a_idx = find_index(a_candidates)
        b_idx = find_index(b_candidates)

        if patch_idx is None or l_idx is None or a_idx is None or b_idx is None:
            raise ValueError(
                f"TXT reference '{path}' has unsupported CGATS columns: {format_tokens}. "
                "Expected a patch/sample column and LAB_L/LAB_A/LAB_B."
            )

        rows = []
        for line in lines[begin_data_idx + 1 : end_data_idx]:
            stripped = line.strip()
            if not stripped:
                continue
            parts = re.split(r"\s+", stripped)
            needed = max(patch_idx, l_idx, a_idx, b_idx)
            if len(parts) <= needed:
                continue

            patch = parts[patch_idx].strip()
            if not is_patch_name(patch):
                continue

            try:
                L = parse_float_maybe_comma(parts[l_idx])
                a = parse_float_maybe_comma(parts[a_idx])
                b = parse_float_maybe_comma(parts[b_idx])
            except Exception:
                continue

            rows.append({"patch": patch, "L": L, "a": a, "b": b})

        if rows:
            return pd.DataFrame(rows)

        raise ValueError(
            f"TXT reference '{path}' contains a CGATS data block, but no readable patch rows were found."
        )

    rows = []
    for line in lines:
        text = line.strip()
        if not text:
            continue

        lower = text.lower()
        if lower.startswith("patch"):
            continue
        if text.startswith("The data in this file"):
            break

        parts = re.split(r"\s+", text)
        if len(parts) < 4:
            continue

        patch = parts[0]
        if not is_patch_name(patch):
            continue

        try:
            L = parse_float_maybe_comma(parts[1])
            a = parse_float_maybe_comma(parts[2])
            b = parse_float_maybe_comma(parts[3])
        except Exception:
            continue

        rows.append({"patch": patch, "L": L, "a": a, "b": b})

    if rows:
        return pd.DataFrame(rows)

    raise ValueError(
        f"Reference TXT file '{path}' does not contain readable patch rows. "
        "Supported TXT variants are: "
        "(1) simple Patch/LAB_L/LAB_A/LAB_B table, "
        "(2) CGATS-like BEGIN_DATA_FORMAT / BEGIN_DATA with patch/sample and LAB columns."
    )


def normalize_reference_columns(df: pd.DataFrame, source_label: str) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(col).strip() for col in df.columns]

    rename_map = {}
    for col in df.columns:
        normalized = str(col).strip()
        lower = normalized.lower()
        if lower in {"patch", "id", "name", "sample", "tile"}:
            rename_map[col] = "patch"
        elif normalized in {"LAB_L", "lab_l", "lab l", "lab-l"}:
            rename_map[col] = "L"
        elif normalized in {"LAB_A", "lab_a", "lab a", "lab-a"}:
            rename_map[col] = "a"
        elif normalized in {"LAB_B", "lab_b", "lab b", "lab-b"}:
            rename_map[col] = "b"
        elif normalized in {"L*", "L", "l", "l*"}:
            rename_map[col] = "L"
        elif normalized in {"a*", "a", "A", "A*"}:
            rename_map[col] = "a"
        elif normalized in {"b*", "b", "B", "B*"}:
            rename_map[col] = "b"
        elif lower == "row":
            rename_map[col] = "row"
        elif lower == "col":
            rename_map[col] = "col"

    df = df.rename(columns=rename_map)

    unnamed_cols = [col for col in df.columns if str(col).lower().startswith("unnamed:")]
    if "patch" not in df.columns and unnamed_cols:
        df = df.rename(columns={unnamed_cols[0]: "patch"})

    if "patch" not in df.columns and len(df.columns) >= 4:
        first_col = df.columns[0]
        probe_values = df[first_col].dropna().astype(str).head(5).tolist()
        if probe_values and all(any(ch.isdigit() for ch in v) and any(ch.isalpha() for ch in v) for v in probe_values):
            df = df.rename(columns={first_col: "patch"})

    required = {"patch", "L", "a", "b"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"Reference file '{source_label}' is missing required columns: {sorted(missing)}. "
            "Supported formats are either: (1) patch, L, a, b, row, col or "
            "(2) patch plus L*/a*/b* columns, "
            "(3) TXT with Patch/LAB_L/LAB_A/LAB_B or CGATS Sample_NAME/LAB columns, "
            "where row/col are derived from patch names like A1."
        )

    df = df[[col for col in df.columns if col in {"patch", "L", "a", "b", "row", "col", "D"}]].copy()
    df["patch"] = df["patch"].astype(str).str.strip()
    df = df[df["patch"] != ""].copy()

    for numeric_col in ["L", "a", "b"]:
        df[numeric_col] = df[numeric_col].apply(parse_float_maybe_comma)

    if "row" in df.columns and "col" in df.columns:
        df["row"] = df["row"].apply(lambda v: int(parse_float_maybe_comma(v)))
        df["col"] = df["col"].apply(lambda v: int(parse_float_maybe_comma(v)))
    else:
        parsed_positions = df["patch"].apply(patch_name_to_row_col)
        df["row"] = parsed_positions.apply(lambda rc: rc[0])
        df["col"] = parsed_positions.apply(lambda rc: rc[1])

    return df[["patch", "L", "a", "b", "row", "col"]]


def dataframe_to_references(df: pd.DataFrame) -> List[PatchReference]:
    refs: List[PatchReference] = []
    for _, row in df.iterrows():
        refs.append(
            PatchReference(
                patch=str(row["patch"]),
                L=float(row["L"]),
                a=float(row["a"]),
                b=float(row["b"]),
                row=int(row["row"]),
                col=int(row["col"]),
            )
        )
    return refs


def load_reference_table(path: str) -> List[PatchReference]:
    reference_path = Path(path)
    suffix = reference_path.suffix.lower()

    try:
        if suffix == ".csv":
            df = pd.read_csv(reference_path)
        elif suffix == ".txt":
            df = load_reference_txt(str(reference_path))
        elif suffix in {".xlsx", ".xlsm", ".xltx", ".xltm", ".xls"}:
            df = pd.read_excel(reference_path)
        else:
            raise ValueError(
                f"Unsupported reference file format '{suffix or '[no extension]'}'. "
                "Use .csv, .txt, .xlsx, .xlsm, .xltx, .xltm or .xls."
            )
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Could not decode reference file '{path}' as CSV/TXT text. "
            "If this is an Excel workbook, use .xlsx/.xls and the loader will read it automatically."
        ) from exc
    except zipfile.BadZipFile as exc:
        raise ValueError(
            f"Reference file '{path}' looks like an invalid or corrupted Excel workbook."
        ) from exc
    except ImportError as exc:
        raise ValueError(
            "Reading Excel files requires the optional dependency 'openpyxl'. "
            "Install it with: pip install openpyxl"
        ) from exc
    except Exception as exc:
        raise ValueError(f"Failed to read reference file '{path}': {exc}") from exc

    df = normalize_reference_columns(df, str(reference_path.name))
    return dataframe_to_references(df)


def read_image_with_pillow(image_path: str) -> Image.Image:
    img = Image.open(image_path)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    elif img.mode == "RGBA":
        img = img.convert("RGB")
    return img


def pillow_to_bgr_np(img: Image.Image) -> np.ndarray:
    rgb = np.array(img)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def get_embedded_icc_bytes(img: Image.Image) -> Optional[bytes]:
    icc = img.info.get("icc_profile")
    return icc if isinstance(icc, (bytes, bytearray)) else None


def build_rgb_to_lab_transform(
    img: Image.Image, fallback_icc_path: Optional[str]
) -> Tuple[ImageCms.ImageCmsTransform, str]:
    embedded_icc = get_embedded_icc_bytes(img)

    if embedded_icc:
        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(embedded_icc))
        profile_name = "embedded_icc"
    elif fallback_icc_path:
        src_profile = ImageCms.getOpenProfile(fallback_icc_path)
        profile_name = str(Path(fallback_icc_path).name)
    else:
        raise ValueError("No embedded ICC profile found and no fallback ICC supplied via --icc")

    dst_profile = ImageCms.createProfile("LAB")
    transform = ImageCms.buildTransformFromOpenProfiles(
        src_profile,
        dst_profile,
        "RGB",
        "LAB",
        renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
        flags=ImageCms.Flags.BLACKPOINTCOMPENSATION,
    )
    return transform, profile_name


def decode_pillow_lab_pixel(lab_pixel: np.ndarray) -> Tuple[float, float, float]:
    if lab_pixel.shape != (3,):
        raise ValueError(f"Expected LAB pixel shape (3,), got {lab_pixel.shape}")

    lab_u8 = np.asarray(lab_pixel, dtype=np.uint8)
    L = float(lab_u8[0]) * 100.0 / 255.0
    a = float(np.asarray(lab_u8[1:2]).view(np.int8)[0])
    b = float(np.asarray(lab_u8[2:3]).view(np.int8)[0])
    return L, a, b


def rgb_triplet_to_lab(
    rgb: Tuple[float, float, float],
    rgb_to_lab_transform: ImageCms.ImageCmsTransform,
) -> Tuple[float, float, float]:
    patch = np.zeros((1, 1, 3), dtype=np.uint8)
    patch[0, 0, :] = np.clip(np.round(rgb), 0, 255).astype(np.uint8)
    pil_rgb = Image.fromarray(patch, mode="RGB")
    pil_lab = ImageCms.applyTransform(pil_rgb, rgb_to_lab_transform)
    lab_arr = np.array(pil_lab, dtype=np.uint8)
    return decode_pillow_lab_pixel(lab_arr[0, 0, :])


def pick_corners_gui(image_bgr: np.ndarray, window_name: str = "Pick corners") -> List[Tuple[float, float]]:
    display = image_bgr.copy()
    h, w = display.shape[:2]
    max_dim = 1600
    scale = min(1.0, max_dim / max(h, w))
    if scale < 1.0:
        shown = cv2.resize(display, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    else:
        shown = display.copy()

    points: List[Tuple[int, int]] = []
    instructions = "Click corners in order: TL, TR, BR, BL. Press Enter to confirm, Backspace to undo, Esc to cancel."

    def redraw() -> np.ndarray:
        canvas = shown.copy()
        cv2.putText(
            canvas,
            instructions,
            (20, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 255, 255),
            2,
            cv2.LINE_AA,
        )
        labels = ["TL", "TR", "BR", "BL"]
        for i, (x, y) in enumerate(points):
            cv2.circle(canvas, (x, y), 7, (0, 0, 255), -1)
            cv2.putText(
                canvas,
                labels[i],
                (x + 10, y - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 0, 255),
                2,
                cv2.LINE_AA,
            )
        if len(points) > 1:
            cv2.polylines(canvas, [np.array(points, dtype=np.int32)], False, (0, 255, 0), 2)
        if len(points) == 4:
            cv2.polylines(canvas, [np.array(points, dtype=np.int32)], True, (255, 0, 0), 2)
        return canvas

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 4:
            points.append((x, y))

    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_name, on_mouse)

    while True:
        cv2.imshow(window_name, redraw())
        key = cv2.waitKey(30) & 0xFF
        if key in (13, 10):
            if len(points) == 4:
                break
        elif key in (8, 127):
            if points:
                points.pop()
        elif key == 27:
            cv2.destroyWindow(window_name)
            raise KeyboardInterrupt("Corner selection cancelled")

    cv2.destroyWindow(window_name)
    return [(x / scale, y / scale) for x, y in points]

def rectified_size_from_grid(rectified_width: int, grid_cols: int, grid_rows: int) -> Tuple[int, int]:
    cell = rectified_width / grid_cols
    rectified_height = int(round(cell * grid_rows))
    return rectified_width, rectified_height


def rectify_chart(
    image_bgr: np.ndarray,
    corners_xy: Sequence[Tuple[float, float]],
    rectified_width: int,
    grid_cols: int,
    grid_rows: int,
) -> Tuple[np.ndarray, np.ndarray]:
    dst_w, dst_h = rectified_size_from_grid(rectified_width, grid_cols, grid_rows)
    src = np.array(corners_xy, dtype=np.float32)
    dst = np.array([[0, 0], [dst_w - 1, 0], [dst_w - 1, dst_h - 1], [0, dst_h - 1]], dtype=np.float32)
    H = cv2.getPerspectiveTransform(src, dst)
    rectified = cv2.warpPerspective(image_bgr, H, (dst_w, dst_h), flags=cv2.INTER_CUBIC)
    return rectified, H


def compute_patch_roi(
    row: int,
    col: int,
    grid_rows: int,
    grid_cols: int,
    rectified_shape: Tuple[int, int, int],
    patch_fill: float,
) -> Tuple[int, int, int, int]:
    if not (0 < patch_fill <= 1.0):
        raise ValueError("patch_fill must be >0 and <=1")

    h, w = rectified_shape[:2]
    cell_w = w / grid_cols
    cell_h = h / grid_rows

    x0 = col * cell_w
    y0 = row * cell_h
    x1 = (col + 1) * cell_w
    y1 = (row + 1) * cell_h

    roi_w = (x1 - x0) * patch_fill
    roi_h = (y1 - y0) * patch_fill
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0

    rx0 = int(round(cx - roi_w / 2.0))
    ry0 = int(round(cy - roi_h / 2.0))
    rx1 = int(round(cx + roi_w / 2.0))
    ry1 = int(round(cy + roi_h / 2.0))

    rx0 = max(0, min(w - 1, rx0))
    ry0 = max(0, min(h - 1, ry0))
    rx1 = max(rx0 + 1, min(w, rx1))
    ry1 = max(ry0 + 1, min(h, ry1))

    return rx0, ry0, rx1 - rx0, ry1 - ry0


def sample_roi_rgb_mean(rectified_bgr: np.ndarray, roi_xywh: Tuple[int, int, int, int]) -> Tuple[float, float, float]:
    x, y, w, h = roi_xywh
    roi = rectified_bgr[y : y + h, x : x + w]
    if roi.size == 0:
        raise ValueError(f"Empty ROI at {roi_xywh}")
    roi_rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
    flat = roi_rgb.reshape(-1, 3).astype(np.float32)
    rgb = np.median(flat, axis=0)
    return float(rgb[0]), float(rgb[1]), float(rgb[2])


def reference_chroma(lab_ref: Tuple[float, float, float]) -> float:
    return float(np.hypot(lab_ref[1], lab_ref[2]))


def clamp_u8(value: float) -> int:
    return int(max(0, min(255, round(value))))


def rgb_tuple_to_hex(rgb: Tuple[float, float, float]) -> str:
    r, g, b = (clamp_u8(v) for v in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def lab_to_srgb_hex(lab: Tuple[float, float, float]) -> str:
    xyz = colour.Lab_to_XYZ(np.asarray(lab, dtype=float))
    srgb = colour.XYZ_to_sRGB(xyz)
    srgb = np.clip(np.asarray(srgb, dtype=float), 0.0, 1.0)
    rgb255 = tuple(float(v * 255.0) for v in srgb)
    return rgb_tuple_to_hex(rgb255)


def image_file_to_data_uri(path: str) -> str:
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def delta_e_2000_custom(
    lab1: Tuple[float, float, float],
    lab2: Tuple[float, float, float],
    *,
    force_sl1: bool = False,
    ignore_luminance: bool = False,
    kL: float = 1.0,
    kC: float = 1.0,
    kH: float = 1.0,
) -> float:
    """
    Generic CIEDE2000 implementation with two Metamorfoze-related options:
    - force_sl1=True     -> CIE2000SL=1
    - ignore_luminance=True -> CIE2000 without luminance = ΔE(ab)*
    """
    L1, a1, b1 = [float(v) for v in lab1]
    L2, a2, b2 = [float(v) for v in lab2]

    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    C_bar = 0.5 * (C1 + C2)

    C_bar7 = C_bar ** 7
    G = 0.5 * (1.0 - np.sqrt(C_bar7 / (C_bar7 + 25.0 ** 7))) if C_bar > 0 else 0.0

    a1p = (1.0 + G) * a1
    a2p = (1.0 + G) * a2

    C1p = np.hypot(a1p, b1)
    C2p = np.hypot(a2p, b2)

    def hp_fun(a_prime: float, b_val: float) -> float:
        if a_prime == 0.0 and b_val == 0.0:
            return 0.0
        angle = np.degrees(np.arctan2(b_val, a_prime))
        return angle + 360.0 if angle < 0 else angle

    h1p = hp_fun(a1p, b1)
    h2p = hp_fun(a2p, b2)

    dLp = 0.0 if ignore_luminance else (L2 - L1)
    dCp = C2p - C1p

    if C1p * C2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180.0:
            dhp -= 360.0
        elif dhp < -180.0:
            dhp += 360.0

    dHp = 2.0 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp) / 2.0)

    Lp_bar = 0.5 * (L1 + L2)
    Cp_bar = 0.5 * (C1p + C2p)

    if C1p * C2p == 0:
        hp_bar = h1p + h2p
    else:
        hsum = h1p + h2p
        if abs(h1p - h2p) > 180.0:
            if hsum < 360.0:
                hp_bar = 0.5 * (hsum + 360.0)
            else:
                hp_bar = 0.5 * (hsum - 360.0)
        else:
            hp_bar = 0.5 * hsum

    T = (
        1.0
        - 0.17 * np.cos(np.radians(hp_bar - 30.0))
        + 0.24 * np.cos(np.radians(2.0 * hp_bar))
        + 0.32 * np.cos(np.radians(3.0 * hp_bar + 6.0))
        - 0.20 * np.cos(np.radians(4.0 * hp_bar - 63.0))
    )

    delta_theta = 30.0 * np.exp(-((hp_bar - 275.0) / 25.0) ** 2)
    Rc = 2.0 * np.sqrt((Cp_bar ** 7) / (Cp_bar ** 7 + 25.0 ** 7)) if Cp_bar > 0 else 0.0
    Rt = -np.sin(np.radians(2.0 * delta_theta)) * Rc

    if force_sl1:
        Sl = 1.0
    else:
        Sl = 1.0 + (0.015 * (Lp_bar - 50.0) ** 2) / np.sqrt(20.0 + (Lp_bar - 50.0) ** 2)

    Sc = 1.0 + 0.045 * Cp_bar
    Sh = 1.0 + 0.015 * Cp_bar * T

    term_L = 0.0 if ignore_luminance else (dLp / (kL * Sl))
    term_C = dCp / (kC * Sc)
    term_H = dHp / (kH * Sh)

    delta_e = np.sqrt(
        term_L * term_L
        + term_C * term_C
        + term_H * term_H
        + Rt * term_C * term_H
    )
    return float(delta_e)


def delta_e_ab_metamorfoze(
    lab_ref: Tuple[float, float, float],
    lab_meas: Tuple[float, float, float],
) -> float:
    return delta_e_2000_custom(lab_ref, lab_meas, ignore_luminance=True)


def delta_e_sl1_metamorfoze(
    lab_ref: Tuple[float, float, float],
    lab_meas: Tuple[float, float, float],
) -> float:
    return delta_e_2000_custom(lab_ref, lab_meas, force_sl1=True)


def compute_measurements(
    rectified_bgr: np.ndarray,
    references: Sequence[PatchReference],
    grid_rows: int,
    grid_cols: int,
    patch_fill: float,
    rgb_to_lab_transform: ImageCms.ImageCmsTransform,
    neutral_chroma_threshold: float,
) -> List[PatchMeasurement]:
    results: List[PatchMeasurement] = []
    for ref in references:
        roi = compute_patch_roi(
            ref.row,
            ref.col,
            grid_rows,
            grid_cols,
            rectified_bgr.shape,
            patch_fill,
        )
        rgb = sample_roi_rgb_mean(rectified_bgr, roi)
        lab_meas = rgb_triplet_to_lab(rgb, rgb_to_lab_transform)
        lab_ref = (ref.L, ref.a, ref.b)

        de_cie2000 = float(colour.delta_E(np.array(lab_ref), np.array(lab_meas), method="CIE 2000"))
        de_sl1 = delta_e_sl1_metamorfoze(lab_ref, lab_meas)
        de_ab = delta_e_ab_metamorfoze(lab_ref, lab_meas)

        dL = float(lab_meas[0] - lab_ref[0])
        da = float(lab_meas[1] - lab_ref[1])
        db = float(lab_meas[2] - lab_ref[2])

        chroma = reference_chroma(lab_ref)
        is_neutral = chroma <= neutral_chroma_threshold

        results.append(
            PatchMeasurement(
                patch=ref.patch,
                row=ref.row,
                col=ref.col,
                rgb_mean_8bit=rgb,
                lab_measured=lab_meas,
                lab_reference=lab_ref,
                delta_e_cie2000=de_cie2000,
                delta_e_sl1=de_sl1,
                delta_e_ab=de_ab,
                delta_L=dL,
                delta_a=da,
                delta_b=db,
                reference_chroma=chroma,
                is_neutral_reference=is_neutral,
                roi_rectified_xywh=roi,
            )
        )
    return results


def filter_metamorfoze_neutrals(
    measurements: Sequence[PatchMeasurement],
    level: str,
) -> List[PatchMeasurement]:
    if level not in METAMORFOZE_SPECS or level == "none":
        return [m for m in measurements if m.is_neutral_reference]

    floor = float(METAMORFOZE_SPECS[level]["neutral_lstar_floor"])
    return [
        m for m in measurements
        if m.is_neutral_reference and m.lab_reference[0] >= floor
    ]


def summarize_neutral_scale(measurements: Sequence[PatchMeasurement], level: str = "none") -> Dict[str, object]:
    neutrals = filter_metamorfoze_neutrals(measurements, level)
    if not neutrals:
        return {
            "patch_count": 0,
            "patches": [],
            "mean_deltaEab": None,
            "max_deltaEab": None,
            "mean_abs_deltaL": None,
            "max_abs_deltaL": None,
            "mean_abs_deltaa": None,
            "max_abs_deltaa": None,
            "mean_abs_deltab": None,
            "max_abs_deltab": None,
        }

    dEab = np.array([m.delta_e_ab for m in neutrals], dtype=float)
    dL = np.array([m.delta_L for m in neutrals], dtype=float)
    da = np.array([m.delta_a for m in neutrals], dtype=float)
    db = np.array([m.delta_b for m in neutrals], dtype=float)

    return {
        "patch_count": len(neutrals),
        "patches": [m.patch for m in neutrals],
        "mean_deltaEab": float(np.mean(dEab)),
        "max_deltaEab": float(np.max(dEab)),
        "mean_abs_deltaL": float(np.mean(np.abs(dL))),
        "max_abs_deltaL": float(np.max(np.abs(dL))),
        "mean_abs_deltaa": float(np.mean(np.abs(da))),
        "max_abs_deltaa": float(np.max(np.abs(da))),
        "mean_abs_deltab": float(np.mean(np.abs(db))),
        "max_abs_deltab": float(np.max(np.abs(db))),
    }


def evaluate_white_balance(
    measurements: Sequence[PatchMeasurement],
    level: str,
) -> Dict[str, object]:
    if level == "none":
        return {"applicable": False}

    specs = METAMORFOZE_SPECS[level]
    limit = float(specs["white_balance_limit"])
    neutrals = filter_metamorfoze_neutrals(measurements, level)

    per_patch = [{"patch": m.patch, "deltaEab": m.delta_e_ab, "pass": m.delta_e_ab <= limit} for m in neutrals]
    values = [m.delta_e_ab for m in neutrals]

    return {
        "applicable": True,
        "formula": "CIE2000 without luminance",
        "unit": "ΔE(ab)*",
        "neutral_lstar_floor": specs["neutral_lstar_floor"],
        "patch_count": len(neutrals),
        "limit": limit,
        "mean": float(np.mean(values)) if values else None,
        "max": float(np.max(values)) if values else None,
        "all_patches_pass": bool(all(v <= limit for v in values)) if values else None,
        "per_patch": per_patch,
    }


def evaluate_exposure(
    measurements: Sequence[PatchMeasurement],
    level: str,
) -> Dict[str, object]:
    if level == "none":
        return {"applicable": False}

    specs = METAMORFOZE_SPECS[level]
    limit = float(specs["exposure_limit"])
    neutrals = filter_metamorfoze_neutrals(measurements, level)

    per_patch = [{"patch": m.patch, "deltaL": m.delta_L, "abs_deltaL": abs(m.delta_L), "pass": abs(m.delta_L) <= limit} for m in neutrals]
    values = [abs(m.delta_L) for m in neutrals]

    highlight_patch = None
    if neutrals:
        highlight_patch = min(neutrals, key=lambda m: abs(m.lab_reference[0] - 95.0))

    return {
        "applicable": True,
        "formula": "CIE2000SL=1 (ΔL*)",
        "unit": "ΔL*",
        "neutral_lstar_floor": specs["neutral_lstar_floor"],
        "patch_count": len(neutrals),
        "limit": limit,
        "mean_abs": float(np.mean(values)) if values else None,
        "max_abs": float(np.max(values)) if values else None,
        "all_patches_pass": bool(all(v <= limit for v in values)) if values else None,
        "highlight_patch": None if highlight_patch is None else {
            "patch": highlight_patch.patch,
            "L_ref": highlight_patch.lab_reference[0],
            "L_meas": highlight_patch.lab_measured[0],
            "deltaL": highlight_patch.delta_L,
            "abs_deltaL": abs(highlight_patch.delta_L),
            "pass": abs(highlight_patch.delta_L) <= limit,
        },
        "per_patch": per_patch,
    }


def build_gain_pairs(neutrals: Sequence[PatchMeasurement]) -> List[GainPair]:
    ordered = sorted(neutrals, key=lambda m: m.lab_reference[0], reverse=True)

    pairs: List[GainPair] = []
    for i in range(len(ordered) - 1):
        hi = ordered[i]
        lo = ordered[i + 1]
        ref_diff = hi.lab_reference[0] - lo.lab_reference[0]
        if ref_diff <= 0:
            continue
        if not (7.0 <= ref_diff <= 13.0):
            continue

        meas_diff = hi.lab_measured[0] - lo.lab_measured[0]
        gain_percent = 100.0 * meas_diff / ref_diff

        bucket = "other"
        if abs(hi.lab_reference[0] - 95.0) <= 3.0 and abs(lo.lab_reference[0] - 85.0) <= 3.0:
            bucket = "highlights"

        pairs.append(
            GainPair(
                patch_hi=hi.patch,
                patch_lo=lo.patch,
                L_ref_hi=hi.lab_reference[0],
                L_ref_lo=lo.lab_reference[0],
                L_meas_hi=hi.lab_measured[0],
                L_meas_lo=lo.lab_measured[0],
                ref_diff=ref_diff,
                meas_diff=meas_diff,
                gain_percent=gain_percent,
                bucket=bucket,
            )
        )
    return pairs


def evaluate_gain_modulation(
    measurements: Sequence[PatchMeasurement],
    level: str,
) -> Dict[str, object]:
    if level == "none":
        return {"applicable": False}

    specs = METAMORFOZE_SPECS[level]
    neutrals = filter_metamorfoze_neutrals(measurements, level)
    pairs = build_gain_pairs(neutrals)

    hi_min = float(specs["gain_highlights_min"])
    hi_max = float(specs["gain_highlights_max"])
    other_min = float(specs["gain_other_min"])
    other_max = float(specs["gain_other_max"])

    hi_pairs = [p for p in pairs if p.bucket == "highlights"]
    other_pairs = [p for p in pairs if p.bucket == "other"]

    def serialize_pair(p: GainPair) -> Dict[str, object]:
        limit_min = hi_min if p.bucket == "highlights" else other_min
        limit_max = hi_max if p.bucket == "highlights" else other_max
        return {
            "patch_hi": p.patch_hi,
            "patch_lo": p.patch_lo,
            "L_ref_hi": p.L_ref_hi,
            "L_ref_lo": p.L_ref_lo,
            "L_meas_hi": p.L_meas_hi,
            "L_meas_lo": p.L_meas_lo,
            "ref_diff": p.ref_diff,
            "meas_diff": p.meas_diff,
            "gain_percent": p.gain_percent,
            "bucket": p.bucket,
            "pass": limit_min <= p.gain_percent <= limit_max,
        }

    return {
        "applicable": True,
        "formula": "CIE2000SL=1-based ΔL* step comparison",
        "unit": "percent",
        "note": "Approximation for CCSG neutrals; preferred targets for gain modulation are UTT / linear gray scales.",
        "highlight_limits": [hi_min, hi_max],
        "other_limits": [other_min, other_max],
        "highlight_pairs_count": len(hi_pairs),
        "other_pairs_count": len(other_pairs),
        "highlights_all_pass": bool(all(hi_min <= p.gain_percent <= hi_max for p in hi_pairs)) if hi_pairs else None,
        "other_all_pass": bool(all(other_min <= p.gain_percent <= other_max for p in other_pairs)) if other_pairs else None,
        "pairs": [serialize_pair(p) for p in pairs],
    }


def evaluate_color_accuracy(
    measurements: Sequence[PatchMeasurement],
    level: str,
) -> Dict[str, object]:
    if level == "none":
        return {"applicable": False}

    specs = METAMORFOZE_SPECS[level]
    mean_limit = specs.get("color_mean_limit")
    max_limit = specs.get("color_max_limit")

    values = np.array([m.delta_e_sl1 for m in measurements], dtype=float)
    mean_value = float(np.mean(values)) if len(values) else None
    max_value = float(np.max(values)) if len(values) else None

    if mean_limit is None or max_limit is None:
        return {
            "applicable": False,
            "formula": "CIE2000SL=1",
            "unit": "ΔE*",
            "note": "Metamorfoze Extra Light does not specify a technical color accuracy tolerance; only visual primary-color plausibility is required.",
            "mean": mean_value,
            "max": max_value,
        }

    return {
        "applicable": True,
        "formula": "CIE2000SL=1",
        "unit": "ΔE*",
        "mean": mean_value,
        "max": max_value,
        "mean_limit": float(mean_limit),
        "max_limit": float(max_limit),
        "mean_pass": bool(mean_value <= float(mean_limit)) if mean_value is not None else None,
        "max_pass": bool(max_value <= float(max_limit)) if max_value is not None else None,
        "overall_pass": bool((mean_value <= float(mean_limit)) and (max_value <= float(max_limit))) if mean_value is not None and max_value is not None else None,
        "worst_patches": [
            {"patch": m.patch, "deltaE_sl1": m.delta_e_sl1}
            for m in sorted(measurements, key=lambda m: m.delta_e_sl1, reverse=True)[:15]
        ],
    }


def evaluate_metamorfoze(
    measurements: Sequence[PatchMeasurement],
    level: str,
) -> Dict[str, object]:
    if level == "none":
        return {"level": "none", "applicable": False}

    wb = evaluate_white_balance(measurements, level)
    exposure = evaluate_exposure(measurements, level)
    gain = evaluate_gain_modulation(measurements, level)
    color = evaluate_color_accuracy(measurements, level)

    overall_flags: List[bool] = []

    for section in (wb, exposure):
        if section.get("applicable") and section.get("all_patches_pass") is not None:
            overall_flags.append(bool(section["all_patches_pass"]))

    if gain.get("applicable"):
        if gain.get("highlights_all_pass") is not None:
            overall_flags.append(bool(gain["highlights_all_pass"]))
        if gain.get("other_all_pass") is not None:
            overall_flags.append(bool(gain["other_all_pass"]))

    if color.get("applicable") and color.get("overall_pass") is not None:
        overall_flags.append(bool(color["overall_pass"]))

    return {
        "level": level,
        "applicable": True,
        "white_balance": wb,
        "exposure": exposure,
        "gain_modulation": gain,
        "color_accuracy": color,
        "overall_pass": bool(all(overall_flags)) if overall_flags else None,
        "limitations": [
            "This evaluation is based on a single centre-position CCSG capture.",
            "Whole-image-plane white balance / illumination require additional targets or frame-filling white sheets.",
            "Gain modulation on CCSG neutrals is approximate; UTT or linear gray scales are preferable.",
        ],
    }

def save_rectified_with_rois(
    rectified_bgr: np.ndarray,
    measurements: Sequence[PatchMeasurement],
    output_path: str,
) -> None:
    vis = rectified_bgr.copy()

    for m in measurements:
        x, y, w, h = m.roi_rectified_xywh

        # ROI rectangle
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 255), 2)

        # Short label: patch + main metric only
        # Prefer color accuracy in overlay, because it is the main per-patch chart diagnostic.
        label = f"{m.patch} {m.delta_e_sl1:.2f}"

        # Put label inside the patch near the top-left corner
        tx = x + 4
        ty = y + 16

        # Small white background for readability
        (text_w, text_h), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1
        )
        bg_x0 = max(0, tx - 2)
        bg_y0 = max(0, ty - text_h - 2)
        bg_x1 = min(vis.shape[1], tx + text_w + 2)
        bg_y1 = min(vis.shape[0], ty + baseline + 2)

        cv2.rectangle(vis, (bg_x0, bg_y0), (bg_x1, bg_y1), (255, 255, 255), -1)
        cv2.putText(
            vis,
            label,
            (tx, ty),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (0, 0, 255),
            1,
            cv2.LINE_AA,
        )

    cv2.imwrite(output_path, vis)


def make_ccsg_axis_labels(grid_rows: int, grid_cols: int) -> Tuple[List[str], List[str]]:
    x_labels = [chr(ord("A") + i) for i in range(grid_cols)]
    y_labels = [str(i + 1) for i in range(grid_rows)]
    return x_labels, y_labels


def measurement_grid(
    measurements: Sequence[PatchMeasurement],
    grid_rows: int,
    grid_cols: int,
    value_getter,
) -> Tuple[np.ndarray, np.ndarray]:
    arr = np.full((grid_rows, grid_cols), np.nan, dtype=float)
    labels = np.empty((grid_rows, grid_cols), dtype=object)
    labels[:] = ""
    for m in measurements:
        if 0 <= m.row < grid_rows and 0 <= m.col < grid_cols:
            arr[m.row, m.col] = float(value_getter(m))
            labels[m.row, m.col] = m.patch
    return arr, labels


def save_heatmap_from_values(
    values: np.ndarray,
    labels: np.ndarray,
    grid_rows: int,
    grid_cols: int,
    title: str,
    colorbar_label: str,
    output_path: str,
    value_format: str = "{:.2f}",
    cmap: Optional[str] = None,
) -> None:
    x_labels, y_labels = make_ccsg_axis_labels(grid_rows, grid_cols)
    fig, ax = plt.subplots(figsize=(max(8, grid_cols * 0.7), max(6, grid_rows * 0.7)))
    im = ax.imshow(values, interpolation="nearest", cmap=cmap)
    ax.set_title(title)
    ax.set_xlabel("Columns (A–N)")
    ax.set_ylabel("Rows (1–10)")
    ax.set_xticks(np.arange(grid_cols))
    ax.set_yticks(np.arange(grid_rows))
    ax.set_xticklabels(x_labels)
    ax.set_yticklabels(y_labels)

    for r in range(grid_rows):
        for c in range(grid_cols):
            if np.isfinite(values[r, c]):
                cell_text = f"{labels[r, c]}\n{value_format.format(values[r, c])}"
                ax.text(c, r, cell_text, ha="center", va="center", fontsize=8)

    cbar = fig.colorbar(im, ax=ax)
    cbar.set_label(colorbar_label)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def save_delta_sl1_heatmap(measurements: Sequence[PatchMeasurement], grid_rows: int, grid_cols: int, output_path: str) -> None:
    arr, labels = measurement_grid(measurements, grid_rows, grid_cols, lambda m: m.delta_e_sl1)
    save_heatmap_from_values(arr, labels, grid_rows, grid_cols, "ΔE* heatmap (CIE2000SL=1)", "ΔE*", output_path)


def save_delta_ab_heatmap(measurements: Sequence[PatchMeasurement], grid_rows: int, grid_cols: int, output_path: str) -> None:
    arr, labels = measurement_grid(measurements, grid_rows, grid_cols, lambda m: m.delta_e_ab)
    save_heatmap_from_values(arr, labels, grid_rows, grid_cols, "ΔE(ab)* heatmap (white balance)", "ΔE(ab)*", output_path)


def save_delta_component_heatmap(
    measurements: Sequence[PatchMeasurement],
    grid_rows: int,
    grid_cols: int,
    component_name: str,
    output_path: str,
) -> None:
    mapping = {
        "deltaL": (lambda m: m.delta_L, "ΔL* heatmap", "ΔL*"),
        "deltaa": (lambda m: m.delta_a, "Δa* heatmap", "Δa*"),
        "deltab": (lambda m: m.delta_b, "Δb* heatmap", "Δb*"),
    }
    getter, title, label = mapping[component_name]
    arr, labels = measurement_grid(measurements, grid_rows, grid_cols, getter)
    save_heatmap_from_values(arr, labels, grid_rows, grid_cols, title, label, output_path, cmap="coolwarm")


def get_rgb_colourspace_by_name(name: str):
    aliases = {
        "eciRGBv2": ["ECI RGB v2", "eciRGB v2", "eciRGBv2"],
        "AdobeRGB1998": ["Adobe RGB (1998)", "Adobe RGB 1998", "AdobeRGB1998"],
        "sRGB": ["sRGB", "sRGB IEC61966-2.1", "IEC 61966-2-1"],
    }
    candidates = aliases.get(name, [name])
    for candidate in candidates:
        if candidate in colour.RGB_COLOURSPACES:
            return colour.RGB_COLOURSPACES[candidate]
    available = sorted(colour.RGB_COLOURSPACES.keys())
    raise KeyError(f"Could not find RGB colourspace '{name}'. Available examples include: {available[:10]}")


def lab_to_xy(lab: Tuple[float, float, float]) -> Tuple[float, float]:
    XYZ = colour.Lab_to_XYZ(np.asarray(lab, dtype=float))
    xy = colour.XYZ_to_xy(XYZ)
    return float(xy[0]), float(xy[1])


def save_colourspace_chromaticity_plot(
    measurements: Sequence[PatchMeasurement],
    output_path: str,
) -> None:
    measured_xy = np.array([lab_to_xy(m.lab_measured) for m in measurements], dtype=float)
    reference_xy = np.array([lab_to_xy(m.lab_reference) for m in measurements], dtype=float)

    srgb = get_rgb_colourspace_by_name("sRGB")
    eci = get_rgb_colourspace_by_name("eciRGBv2")
    adobe = get_rgb_colourspace_by_name("AdobeRGB1998")

    fig, ax = plt.subplots(figsize=(8, 8))
    ax.scatter(reference_xy[:, 0], reference_xy[:, 1], s=28, marker="o", label="Reference patches")
    ax.scatter(measured_xy[:, 0], measured_xy[:, 1], s=28, marker="x", label="Measured patches")

    for cs, label in [(srgb, "sRGB"), (eci, "eciRGB v2"), (adobe, "Adobe RGB (1998)")]:
        primaries = np.asarray(cs.primaries, dtype=float)
        triangle = np.vstack([primaries, primaries[0]])
        ax.plot(triangle[:, 0], triangle[:, 1], linewidth=2, label=label)

    ax.set_title("Measured/reference chromaticity vs RGB colour spaces")
    ax.set_xlabel("x chromaticity")
    ax.set_ylabel("y chromaticity")
    ax.legend(loc="best")
    ax.set_xlim(0.0, 0.8)
    ax.set_ylim(0.0, 0.9)
    ax.set_aspect("equal", adjustable="box")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def save_top_patches_chart(measurements: Sequence[PatchMeasurement], output_path: str, top_n: int = 15) -> None:
    ordered = sorted(measurements, key=lambda m: m.delta_e_sl1, reverse=True)[:top_n]
    labels = [m.patch for m in ordered][::-1]
    values = [m.delta_e_sl1 for m in ordered][::-1]

    fig, ax = plt.subplots(figsize=(10, max(6, len(labels) * 0.35 + 2)))
    ax.barh(labels, values)
    ax.set_title(f"Top {len(labels)} worst patches by ΔE* (SL=1)")
    ax.set_xlabel("ΔE*")
    ax.set_ylabel("Patch")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def save_lstar_scatter(measurements: Sequence[PatchMeasurement], output_path: str) -> None:
    L_ref = [m.lab_reference[0] for m in measurements]
    L_meas = [m.lab_measured[0] for m in measurements]

    lo = min(min(L_ref), min(L_meas))
    hi = max(max(L_ref), max(L_meas))

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.scatter(L_ref, L_meas)
    ax.plot([lo, hi], [lo, hi])
    ax.set_xlabel("L* reference")
    ax.set_ylabel("L* measured")
    ax.set_title("L* reference vs measured")
    ax.set_xlim(lo - 1, hi + 1)
    ax.set_ylim(lo - 1, hi + 1)
    ax.set_aspect("equal", adjustable="box")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def save_neutral_scale_plot(measurements: Sequence[PatchMeasurement], output_path: str) -> None:
    neutrals = sorted([m for m in measurements if m.is_neutral_reference], key=lambda m: m.lab_reference[0], reverse=True)
    if not neutrals:
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.text(0.5, 0.5, "No neutral patches detected", ha="center", va="center")
        ax.set_axis_off()
        fig.tight_layout()
        fig.savefig(output_path, dpi=150)
        plt.close(fig)
        return

    x = np.arange(len(neutrals))
    labels = [m.patch for m in neutrals]
    L_ref = [m.lab_reference[0] for m in neutrals]
    L_meas = [m.lab_measured[0] for m in neutrals]
    wb = [m.delta_e_ab for m in neutrals]

    fig, ax = plt.subplots(figsize=(max(8, len(neutrals) * 0.7), 6))
    ax.plot(x, L_ref, marker="o", label="L* reference")
    ax.plot(x, L_meas, marker="o", label="L* measured")
    ax.plot(x, wb, marker="x", label="ΔE(ab)*")
    ax.set_title("Neutral scale summary")
    ax.set_xlabel("Neutral patch")
    ax.set_ylabel("Value")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def save_measured_rgb_bars(measurements: Sequence[PatchMeasurement], output_path: str) -> None:
    ordered = sorted(measurements, key=lambda m: (m.row, m.col))
    patch_labels = [m.patch for m in ordered]
    r_values = np.array([m.rgb_mean_8bit[0] for m in ordered], dtype=float)
    g_values = np.array([m.rgb_mean_8bit[1] for m in ordered], dtype=float)
    b_values = np.array([m.rgb_mean_8bit[2] for m in ordered], dtype=float)

    x = np.arange(len(patch_labels), dtype=float)
    width = 0.27

    fig_width = max(16, len(patch_labels) * 0.22)
    fig, ax = plt.subplots(figsize=(fig_width, 7))
    ax.bar(x - width, r_values, width=width, label="R")
    ax.bar(x, g_values, width=width, label="G")
    ax.bar(x + width, b_values, width=width, label="B")

    ax.set_title("Measured RGB values by patch")
    ax.set_xlabel("Patch")
    ax.set_ylabel("Measured RGB (8-bit)")
    ax.set_xticks(x)
    ax.set_xticklabels(patch_labels, rotation=90)
    ax.set_ylim(0, 255)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def write_measurements_csv(measurements: Sequence[PatchMeasurement], output_path: str) -> None:
    rows = []
    for m in measurements:
        rows.append(
            {
                "patch": m.patch,
                "row": m.row,
                "col": m.col,
                "R_mean_8bit": m.rgb_mean_8bit[0],
                "G_mean_8bit": m.rgb_mean_8bit[1],
                "B_mean_8bit": m.rgb_mean_8bit[2],
                "L_ref": m.lab_reference[0],
                "a_ref": m.lab_reference[1],
                "b_ref": m.lab_reference[2],
                "L_meas": m.lab_measured[0],
                "a_meas": m.lab_measured[1],
                "b_meas": m.lab_measured[2],
                "deltaL": m.delta_L,
                "deltaa": m.delta_a,
                "deltab": m.delta_b,
                "deltaE_cie2000": m.delta_e_cie2000,
                "deltaE_sl1": m.delta_e_sl1,
                "deltaEab": m.delta_e_ab,
                "reference_chroma": m.reference_chroma,
                "is_neutral_reference": m.is_neutral_reference,
                "roi_x": m.roi_rectified_xywh[0],
                "roi_y": m.roi_rectified_xywh[1],
                "roi_w": m.roi_rectified_xywh[2],
                "roi_h": m.roi_rectified_xywh[3],
            }
        )
    pd.DataFrame(rows).to_csv(output_path, index=False)


def write_summary_json(
    output_path: str,
    image_path: str,
    reference_path: str,
    profile_name: str,
    measurements: Sequence[PatchMeasurement],
    metamorfoze_eval: Dict[str, object],
    neutral_summary: Dict[str, object],
    corners_xy: Sequence[Tuple[float, float]],
) -> None:
    summary = {
        "script_version": SCRIPT_VERSION,
        "image": str(Path(image_path).resolve()),
        "reference": str(Path(reference_path).resolve()),
        "profile_used": profile_name,
        "patch_count": len(measurements),
        "mean_deltaE_cie2000": float(np.mean([m.delta_e_cie2000 for m in measurements])) if measurements else None,
        "mean_deltaE_sl1": float(np.mean([m.delta_e_sl1 for m in measurements])) if measurements else None,
        "max_deltaE_sl1": float(np.max([m.delta_e_sl1 for m in measurements])) if measurements else None,
        "mean_deltaEab_neutrals_info": neutral_summary.get("mean_deltaEab"),
        "max_deltaEab_neutrals_info": neutral_summary.get("max_deltaEab"),
        "metamorfoze": metamorfoze_eval,
        "neutral_scale": neutral_summary,
        "corners_xy": [[float(x), float(y)] for x, y in corners_xy],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)


def write_html_report(
    output_path: str,
    image_path: str,
    reference_path: str,
    profile_name: str,
    measurements: Sequence[PatchMeasurement],
    metamorfoze_eval: Dict[str, object],
    plot_paths: Dict[str, str],
) -> None:
    mean_sl1 = float(np.mean([m.delta_e_sl1 for m in measurements])) if measurements else float("nan")
    max_sl1 = float(np.max([m.delta_e_sl1 for m in measurements])) if measurements else float("nan")
    mean_cie2000 = float(np.mean([m.delta_e_cie2000 for m in measurements])) if measurements else float("nan")
    mean_wb = float(np.mean([m.delta_e_ab for m in measurements])) if measurements else float("nan")

    worst_measurements = sorted(measurements, key=lambda m: m.delta_e_sl1, reverse=True)[:15]

    def plot_section(plot_key: str, title: str, desc_cz: str, desc_en: str) -> str:
        if plot_key not in plot_paths or not Path(plot_paths[plot_key]).exists():
            return ""
        img_uri = image_file_to_data_uri(plot_paths[plot_key])
        return f'''
        <section class="plot-card">
          <h3>{html.escape(title)}</h3>
          <p class="lang lang-cze">{html.escape(desc_cz)}</p>
          <p class="lang lang-eng">{html.escape(desc_en)}</p>
          <img src="{img_uri}" alt="{html.escape(title)}">
        </section>
        '''

    worst_rows = []
    for m in worst_measurements:
        worst_rows.append(
            f"<tr>"
            f"<td>{html.escape(m.patch)}</td>"
            f"<td>{m.delta_e_sl1:.2f}</td>"
            f"<td>{m.delta_e_ab:.2f}</td>"
            f"<td>{m.delta_L:.2f}</td>"
            f"</tr>"
        )

    legend_rows = []
    for m in sorted(measurements, key=lambda item: (item.row, item.col)):
        ref_hex = lab_to_srgb_hex(m.lab_reference)
        meas_hex = rgb_tuple_to_hex(m.rgb_mean_8bit)
        legend_rows.append(
            f"<tr>"
            f"<td>{html.escape(m.patch)}</td>"
            f"<td><span class='swatch' style='background:{ref_hex}'></span> {ref_hex}</td>"
            f"<td><span class='swatch' style='background:{meas_hex}'></span> {meas_hex}</td>"
            f"<td>{m.lab_reference[0]:.2f}, {m.lab_reference[1]:.2f}, {m.lab_reference[2]:.2f}</td>"
            f"<td>{m.lab_measured[0]:.2f}, {m.lab_measured[1]:.2f}, {m.lab_measured[2]:.2f}</td>"
            f"<td>{clamp_u8(m.rgb_mean_8bit[0])}, {clamp_u8(m.rgb_mean_8bit[1])}, {clamp_u8(m.rgb_mean_8bit[2])}</td>"
            f"<td>{m.delta_e_sl1:.2f}</td>"
            f"<td>{m.delta_e_ab:.2f}</td>"
            f"<td>{m.delta_L:.2f}</td>"
            f"</tr>"
        )

    wb = metamorfoze_eval.get("white_balance", {})
    ex = metamorfoze_eval.get("exposure", {})
    gm = metamorfoze_eval.get("gain_modulation", {})
    ca = metamorfoze_eval.get("color_accuracy", {})

    html_text = f"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Metamorfoze report</title>
<style>
body {{
  font-family: Arial, sans-serif;
  margin: 24px;
  line-height: 1.45;
}}
h1, h2, h3 {{
  margin-bottom: 0.4em;
}}
.controls {{
  position: sticky;
  top: 0;
  background: white;
  padding: 10px 0;
  border-bottom: 1px solid #ddd;
  margin-bottom: 20px;
  z-index: 10;
}}
.summary-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 16px;
}}
.card {{
  border: 1px solid #ddd;
  border-radius: 8px;
  padding: 12px;
}}
.plot-card {{
  border: 1px solid #ddd;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 24px;
}}
.plot-card img {{
  display: block;
  width: 100%;
  max-width: 100%;
  height: auto;
  border: 1px solid #ccc;
}}
table {{
  border-collapse: collapse;
  width: 100%;
  margin: 16px 0;
}}
th, td {{
  border: 1px solid #ccc;
  padding: 6px 8px;
  text-align: left;
  vertical-align: top;
}}
.swatch {{
  display: inline-block;
  width: 1.8em;
  height: 1.1em;
  border: 1px solid #444;
  vertical-align: middle;
  margin-right: 0.4em;
}}
.lang-eng {{
  display: none;
}}
body[data-lang="eng"] .lang-cze {{
  display: none;
}}
body[data-lang="eng"] .lang-eng {{
  display: block;
}}
body[data-lang="cze"] .lang-cze {{
  display: block;
}}
body[data-lang="cze"] .lang-eng {{
  display: none;
}}
.metric-pass {{
  color: #0a7f2e;
  font-weight: bold;
}}
.metric-fail {{
  color: #b00020;
  font-weight: bold;
}}
</style>
<script>
function setLang(lang) {{
  document.body.setAttribute("data-lang", lang);
}}
</script>
</head>
<body data-lang="cze">

<div class="controls">
  <label for="lang-switch" class="lang-cze">Jazyk:</label>
  <label for="lang-switch" class="lang-eng">Language:</label>
  <select id="lang-switch" onchange="setLang(this.value)">
    <option value="cze">CZE</option>
    <option value="eng">ENG</option>
  </select>
</div>

<h1 class="lang-cze">Metamorfoze / DeltaE report</h1>
<h1 class="lang-eng">Metamorfoze / DeltaE report</h1>

<div class="summary-grid">
  <div class="card">
    <h2 class="lang-cze">Vstupy</h2>
    <h2 class="lang-eng">Inputs</h2>
    <p><strong>Image:</strong> {html.escape(str(image_path))}</p>
    <p><strong>Reference:</strong> {html.escape(str(reference_path))}</p>
    <p><strong>Profile used:</strong> {html.escape(profile_name)}</p>
    <p><strong>Patch count:</strong> {len(measurements)}</p>
  </div>

  <div class="card">
    <h2 class="lang-cze">Souhrn</h2>
    <h2 class="lang-eng">Summary</h2>
    <p><strong>Mean ΔE00:</strong> {mean_cie2000:.3f}</p>
    <p><strong>Mean ΔE* (SL=1):</strong> {mean_sl1:.3f}</p>
    <p><strong>Max ΔE* (SL=1):</strong> {max_sl1:.3f}</p>
    <p><strong>Mean ΔE(ab)*:</strong> {mean_wb:.3f}</p>
    <p><strong>Overall pass:</strong> {metamorfoze_eval.get("overall_pass")}</p>
  </div>

  <div class="card">
    <h2>White balance</h2>
    <p><strong>Formula:</strong> {html.escape(str(wb.get("formula", "")))}</p>
    <p><strong>Limit:</strong> {wb.get("limit", "-")}</p>
    <p><strong>Mean:</strong> {wb.get("mean", "-")}</p>
    <p><strong>Max:</strong> {wb.get("max", "-")}</p>
    <p><strong>Pass:</strong> {wb.get("all_patches_pass", "-")}</p>
  </div>

  <div class="card">
    <h2>Exposure / Tone reproduction</h2>
    <p><strong>Limit:</strong> {ex.get("limit", "-")}</p>
    <p><strong>Mean |ΔL*|:</strong> {ex.get("mean_abs", "-")}</p>
    <p><strong>Max |ΔL*|:</strong> {ex.get("max_abs", "-")}</p>
    <p><strong>Pass:</strong> {ex.get("all_patches_pass", "-")}</p>
  </div>

  <div class="card">
    <h2>Gain modulation</h2>
    <p><strong>Highlights:</strong> {gm.get("highlight_limits", "-")}</p>
    <p><strong>Other:</strong> {gm.get("other_limits", "-")}</p>
    <p><strong>Highlights pass:</strong> {gm.get("highlights_all_pass", "-")}</p>
    <p><strong>Other pass:</strong> {gm.get("other_all_pass", "-")}</p>
  </div>

  <div class="card">
    <h2>Color accuracy</h2>
    <p><strong>Formula:</strong> {html.escape(str(ca.get("formula", "")))}</p>
    <p><strong>Mean:</strong> {ca.get("mean", "-")}</p>
    <p><strong>Max:</strong> {ca.get("max", "-")}</p>
    <p><strong>Mean limit:</strong> {ca.get("mean_limit", "-")}</p>
    <p><strong>Max limit:</strong> {ca.get("max_limit", "-")}</p>
    <p><strong>Pass:</strong> {ca.get("overall_pass", "-")}</p>
  </div>
</div>

<h2 class="lang-cze">Grafy</h2>
<h2 class="lang-eng">Plots</h2>

{plot_section("overlay", "Overlay", "Narovnaný target s ROI a stručnými popisky patchů.", "Rectified target with ROIs and short patch labels.")}
{plot_section("delta_sl1_heatmap", "ΔE* heatmap (SL=1)", "Mapa barevné odchylky pro color accuracy.", "Color-accuracy heatmap.")}
{plot_section("delta_ab_heatmap", "ΔE(ab)* heatmap", "Mapa white balance bez luminance.", "White-balance heatmap without luminance.")}
{plot_section("deltaL_heatmap", "ΔL* heatmap", "Mapa rozdílu světlosti.", "Lightness-difference heatmap.")}
{plot_section("deltaa_heatmap", "Δa* heatmap", "Posun na ose zelená–červená.", "Shift on green–red axis.")}
{plot_section("deltab_heatmap", "Δb* heatmap", "Posun na ose modrá–žlutá.", "Shift on blue–yellow axis.")}
{plot_section("top_patches", "Top patches", "Nejhorší patchy podle ΔE* (SL=1).", "Worst patches by ΔE* (SL=1).")}
{plot_section("lstar_scatter", "L* scatter", "Porovnání referenční a naměřené L*.", "Reference vs measured L*.")}
{plot_section("neutral_scale_plot", "Neutral scale", "Souhrn neutrální škály.", "Neutral-scale summary.")}
{plot_section("colourspace_chromaticity", "Chromaticity", "Chromatičnost vůči pracovním RGB prostorům.", "Chromaticity against working RGB spaces.")}
{plot_section("measured_rgb_bars", "Measured RGB bars", "Naměřené RGB hodnoty všech patchů.", "Measured RGB values for all patches.")}

<h2 class="lang-cze">Nejhorší patchy</h2>
<h2 class="lang-eng">Worst patches</h2>
<table>
<thead>
<tr>
  <th>Patch</th>
  <th>ΔE* (SL=1)</th>
  <th>ΔE(ab)*</th>
  <th>ΔL*</th>
</tr>
</thead>
<tbody>
{''.join(worst_rows)}
</tbody>
</table>

<h2 class="lang-cze">Legenda patchů</h2>
<h2 class="lang-eng">Patch legend</h2>
<table>
<thead>
<tr>
  <th>Patch</th>
  <th>Reference</th>
  <th>Measured</th>
  <th>Reference Lab</th>
  <th>Measured Lab</th>
  <th>Measured RGB</th>
  <th>ΔE* (SL=1)</th>
  <th>ΔE(ab)*</th>
  <th>ΔL*</th>
</tr>
</thead>
<tbody>
{''.join(legend_rows)}
</tbody>
</table>

</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_text)


def run_pipeline(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    references = load_reference_table(args.reference)
    if not references:
        raise SystemExit("Reference file contains no patches")

    pil_img = read_image_with_pillow(args.image)
    rgb_to_lab_transform, profile_name = build_rgb_to_lab_transform(pil_img, args.icc)
    image_bgr = pillow_to_bgr_np(pil_img)

    if args.corners:
        corners_xy = [
            (args.corners[0], args.corners[1]),
            (args.corners[2], args.corners[3]),
            (args.corners[4], args.corners[5]),
            (args.corners[6], args.corners[7]),
        ]
    elif args.no_gui:
        raise SystemExit("--no-gui requires --corners x1 y1 x2 y2 x3 y3 x4 y4")
    else:
        corners_xy = pick_corners_gui(image_bgr)

    rectified_bgr, _ = rectify_chart(
        image_bgr=image_bgr,
        corners_xy=corners_xy,
        rectified_width=args.rectified_width,
        grid_cols=args.grid_cols,
        grid_rows=args.grid_rows,
    )

    measurements = compute_measurements(
        rectified_bgr=rectified_bgr,
        references=references,
        grid_rows=args.grid_rows,
        grid_cols=args.grid_cols,
        patch_fill=args.patch_fill,
        rgb_to_lab_transform=rgb_to_lab_transform,
        neutral_chroma_threshold=args.neutral_chroma_threshold,
    )

    neutral_summary = summarize_neutral_scale(measurements, args.metamorfoze_level)
    metamorfoze_eval = evaluate_metamorfoze(measurements, args.metamorfoze_level)

    rectified_path = output_dir / "rectified.png"
    overlay_path = output_dir / "overlay.png"
    delta_sl1_heatmap_path = output_dir / "deltaE_sl1_heatmap.png"
    delta_ab_heatmap_path = output_dir / "deltaE_ab_heatmap.png"
    deltaL_path = output_dir / "deltaL_heatmap.png"
    deltaa_path = output_dir / "deltaa_heatmap.png"
    deltab_path = output_dir / "deltab_heatmap.png"
    top_path = output_dir / "top_patches.png"
    lstar_path = output_dir / "lstar_scatter.png"
    neutral_plot_path = output_dir / "neutral_scale_plot.png"
    chromaticity_path = output_dir / "colourspace_chromaticity.png"
    rgb_bars_path = output_dir / "measured_rgb_bars.png"
    csv_path = output_dir / "measurements.csv"
    json_path = output_dir / "summary.json"
    html_path = output_dir / "report.html"

    cv2.imwrite(str(rectified_path), rectified_bgr)
    save_rectified_with_rois(rectified_bgr, measurements, str(overlay_path))
    save_delta_sl1_heatmap(measurements, args.grid_rows, args.grid_cols, str(delta_sl1_heatmap_path))
    save_delta_ab_heatmap(measurements, args.grid_rows, args.grid_cols, str(delta_ab_heatmap_path))
    save_delta_component_heatmap(measurements, args.grid_rows, args.grid_cols, "deltaL", str(deltaL_path))
    save_delta_component_heatmap(measurements, args.grid_rows, args.grid_cols, "deltaa", str(deltaa_path))
    save_delta_component_heatmap(measurements, args.grid_rows, args.grid_cols, "deltab", str(deltab_path))
    save_top_patches_chart(measurements, str(top_path))
    save_lstar_scatter(measurements, str(lstar_path))
    save_neutral_scale_plot(measurements, str(neutral_plot_path))

    if not args.skip_colourspace_plot:
        save_colourspace_chromaticity_plot(measurements, str(chromaticity_path))
    if not args.skip_rgb_bars_plot:
        save_measured_rgb_bars(measurements, str(rgb_bars_path))

    write_measurements_csv(measurements, str(csv_path))
    write_summary_json(
        str(json_path),
        args.image,
        args.reference,
        profile_name,
        measurements,
        metamorfoze_eval,
        neutral_summary,
        corners_xy,
    )

    plot_paths = {
        "overlay": str(overlay_path),
        "delta_sl1_heatmap": str(delta_sl1_heatmap_path),
        "delta_ab_heatmap": str(delta_ab_heatmap_path),
        "deltaL_heatmap": str(deltaL_path),
        "deltaa_heatmap": str(deltaa_path),
        "deltab_heatmap": str(deltab_path),
        "top_patches": str(top_path),
        "lstar_scatter": str(lstar_path),
        "neutral_scale_plot": str(neutral_plot_path),
    }
    if not args.skip_colourspace_plot:
        plot_paths["colourspace_chromaticity"] = str(chromaticity_path)
    if not args.skip_rgb_bars_plot:
        plot_paths["measured_rgb_bars"] = str(rgb_bars_path)

    if not args.skip_html_report:
        write_html_report(
            str(html_path),
            args.image,
            args.reference,
            profile_name,
            measurements,
            metamorfoze_eval,
            plot_paths,
        )

    print(f"Image:           {args.image}")
    print(f"Reference:       {args.reference}")
    print(f"Profile used:    {profile_name}")
    print(f"Patch count:     {len(measurements)}")
    print(f"Mean ΔE00:       {np.mean([m.delta_e_cie2000 for m in measurements]):.3f}")
    print(f"Mean ΔE* SL=1:   {np.mean([m.delta_e_sl1 for m in measurements]):.3f}")
    print(f"Max  ΔE* SL=1:   {np.max([m.delta_e_sl1 for m in measurements]):.3f}")
    print(f"Neutral patches: {neutral_summary['patch_count']}")

    if args.metamorfoze_level != "none":
        print(f"Metamorfoze level: {args.metamorfoze_level}")
        wb = metamorfoze_eval["white_balance"]
        ex = metamorfoze_eval["exposure"]
        gm = metamorfoze_eval["gain_modulation"]
        ca = metamorfoze_eval["color_accuracy"]

        if wb.get("applicable"):
            print(
                f"  White balance ΔE(ab)*: max={wb['max']:.3f} "
                f"limit={wb['limit']} overall={wb['all_patches_pass']}"
            )
        if ex.get("applicable"):
            print(
                f"  Exposure ΔL*: max_abs={ex['max_abs']:.3f} "
                f"limit={ex['limit']} overall={ex['all_patches_pass']}"
            )
        if gm.get("applicable"):
            print(
                f"  Gain modulation: highlights={gm['highlights_all_pass']} "
                f"other={gm['other_all_pass']}"
            )
        if ca.get("applicable"):
            print(
                f"  Color accuracy ΔE*: mean={ca['mean']:.3f}<= {ca['mean_limit']} => {ca['mean_pass']} | "
                f"max={ca['max']:.3f}<= {ca['max_limit']} => {ca['max_pass']} | "
                f"overall => {ca['overall_pass']}"
            )
        else:
            print(f"  Color accuracy: not technically specified for {args.metamorfoze_level}")

        print(f"  Overall pass:   {metamorfoze_eval['overall_pass']}")

    print("\nOutputs:")
    for path in [
        rectified_path,
        overlay_path,
        delta_sl1_heatmap_path,
        delta_ab_heatmap_path,
        deltaL_path,
        deltaa_path,
        deltab_path,
        top_path,
        lstar_path,
        neutral_plot_path,
        csv_path,
        json_path,
    ]:
        print(f"  {path}")
    if not args.skip_colourspace_plot:
        print(f"  {chromaticity_path}")
    if not args.skip_rgb_bars_plot:
        print(f"  {rgb_bars_path}")
    if not args.skip_html_report:
        print(f"  {html_path}")

    return 0


def run_self_tests() -> int:
    def assert_true(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def test_patch_name_to_row_col() -> None:
        assert_true(patch_name_to_row_col("A1") == (0, 0), "Unexpected A1 mapping")
        assert_true(patch_name_to_row_col("N10") == (9, 13), "Unexpected N10 mapping")

    def test_parse_float_maybe_comma() -> None:
        assert_true(abs(parse_float_maybe_comma("96,25") - 96.25) < 1e-9, "Comma parsing failed")

    def test_delta_e_ab_ignores_lightness() -> None:
        lab1 = (50.0, 0.0, 0.0)
        lab2 = (60.0, 0.0, 0.0)
        de = delta_e_ab_metamorfoze(lab1, lab2)
        assert_true(abs(de) < 1e-9, "ΔE(ab)* should ignore pure lightness difference")

    def test_delta_e_sl1_and_standard_differ() -> None:
        lab1 = (50.0, 2.0, 3.0)
        lab2 = (55.0, 4.0, 1.0)
        de_std = float(colour.delta_E(np.array(lab1), np.array(lab2), method="CIE 2000"))
        de_sl1 = delta_e_sl1_metamorfoze(lab1, lab2)
        assert_true(np.isfinite(de_std) and np.isfinite(de_sl1), "Delta E values must be finite")
        assert_true(abs(de_std - de_sl1) > 1e-9, "SL=1 variant should differ from standard CIEDE2000")

    tests = [
        test_patch_name_to_row_col,
        test_parse_float_maybe_comma,
        test_delta_e_ab_ignores_lightness,
        test_delta_e_sl1_and_standard_differ,
    ]

    failures = []
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
        except Exception as exc:
            failures.append((test.__name__, str(exc)))
            print(f"FAIL: {test.__name__}: {exc}")

    if failures:
        print("\nSelf-tests failed:")
        for name, message in failures:
            print(f"- {name}: {message}")
        return 1

    print("\nAll self-tests passed.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    effective_argv = sys.argv[1:] if argv is None else list(argv)

    if len(effective_argv) == 0:
        parser = build_arg_parser()
        parser.print_help(sys.stderr)
        stderr_write(
            "\nTip: add --image, --reference and --output-dir.\n"
            "Example:\n"
            "  python deltae_metamorfoze.py --image sample.tif --reference ref.csv --output-dir out --icc eciRGB_v2.icc --metamorfoze-level full"
        )
        return 0

    args = parse_args(effective_argv)
    if args.run_tests:
        return run_self_tests()
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())

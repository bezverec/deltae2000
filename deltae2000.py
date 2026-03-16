#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeltaE2000: semi-automatic ColorChecker Digital SG evaluation script.
Author: Jan Houserek
License: GPLv3
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import re
import sys
import zipfile
from dataclasses import dataclass
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


APP_TITLE = "DeltaE2000 v0.0.1"
SCRIPT_VERSION = "2026-03-16-deltae2000-v0.0.1"

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
    delta_e_00: float
    delta_L: float
    delta_a: float
    delta_b: float
    reference_chroma: float
    is_neutral_reference: bool
    roi_rectified_xywh: Tuple[int, int, int, int]


def stderr_write(text: str) -> None:
    sys.stderr.write(text)
    if not text.endswith("\n"):
        sys.stderr.write("\n")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DeltaE2000: semi-automatic CCSG Delta E evaluator",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python deltae2000.py --image sample.tif --reference ref.csv --output-dir out --icc eciRGB_v2.icc\n"
            "  python deltae2000.py --image sample.tif --reference ref.txt --output-dir out\n"
            "  python deltae2000.py --image sample.tif --reference ref.csv --output-dir out --corners 10 10 200 10 200 150 10 150 --no-gui"
        ),
    )
    parser.add_argument("--image", help="Input image path")
    parser.add_argument(
        "--reference",
        help="Reference table (.csv/.txt/.xlsx/.xls) with either columns patch,L,a,b,row,col, TXT variants with Patch/LAB_L/LAB_A/LAB_B or CGATS BEGIN_DATA_FORMAT/BEGIN_DATA blocks, or Excel-style columns patch/L*/a*/b* where row,col are derived from CCSG patch names like A1 (letter=column, number=row)",
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
        help="Number of patch columns in the rectified chart grid (CCSG default: 14 columns)",
    )
    parser.add_argument(
        "--grid-rows",
        type=int,
        default=10,
        help="Number of patch rows in the rectified chart grid (CCSG default: 10 rows)",
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
        help="Optional PASS/FAIL evaluation thresholds",
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

        sample_candidates = {"sample_name", "sampleid", "sample_id", "patch", "name"}
        l_candidates = {"lab_l", "l", "lstar"}
        a_candidates = {"lab_a", "a", "astar"}
        b_candidates = {"lab_b", "b", "bstar"}

        def find_index(candidates: set[str]) -> Optional[int]:
            for i, token in enumerate(normalized_tokens):
                if token in candidates:
                    return i
            return None

        patch_idx = find_index(sample_candidates)
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
    instructions = (
        "Click corners in order: TL, TR, BR, BL. "
        "Press Enter to confirm, Backspace to undo, Esc to cancel."
    )

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
    dst = np.array(
        [[0, 0], [dst_w - 1, 0], [dst_w - 1, dst_h - 1], [0, dst_h - 1]],
        dtype=np.float32,
    )
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
        de = float(colour.delta_E(np.array(lab_ref), np.array(lab_meas), method="CIE 2000"))
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
                delta_e_00=de,
                delta_L=dL,
                delta_a=da,
                delta_b=db,
                reference_chroma=chroma,
                is_neutral_reference=is_neutral,
                roi_rectified_xywh=roi,
            )
        )
    return results


def evaluate_metamorfoze(mean_de: float, max_de: float, level: str) -> Dict[str, Optional[bool]]:
    level = level.lower()
    result: Dict[str, Optional[bool]] = {
        "level": level,
        "mean_limit": None,
        "max_limit": None,
        "mean_pass": None,
        "max_pass": None,
        "overall_pass": None,
    }

    if level == "full":
        result["mean_limit"] = 3.0
        result["max_limit"] = 7.0
    elif level == "light":
        result["mean_limit"] = 4.0
        result["max_limit"] = 14.0
    elif level in ("extra-light", "none"):
        return result
    else:
        raise ValueError(f"Unknown Metamorfoze level: {level}")

    result["mean_pass"] = mean_de <= float(result["mean_limit"])
    result["max_pass"] = max_de <= float(result["max_limit"])
    result["overall_pass"] = bool(result["mean_pass"] and result["max_pass"])
    return result


def summarize_neutral_scale(measurements: Sequence[PatchMeasurement]) -> Dict[str, object]:
    neutrals = [m for m in measurements if m.is_neutral_reference]
    if not neutrals:
        return {
            "patch_count": 0,
            "patches": [],
            "mean_deltaE00": None,
            "max_deltaE00": None,
            "mean_abs_deltaL": None,
            "max_abs_deltaL": None,
            "mean_abs_deltaa": None,
            "max_abs_deltaa": None,
            "mean_abs_deltab": None,
            "max_abs_deltab": None,
        }

    dE = np.array([m.delta_e_00 for m in neutrals], dtype=float)
    dL = np.array([m.delta_L for m in neutrals], dtype=float)
    da = np.array([m.delta_a for m in neutrals], dtype=float)
    db = np.array([m.delta_b for m in neutrals], dtype=float)

    return {
        "patch_count": len(neutrals),
        "patches": [m.patch for m in neutrals],
        "mean_deltaE00": float(np.mean(dE)),
        "max_deltaE00": float(np.max(dE)),
        "mean_abs_deltaL": float(np.mean(np.abs(dL))),
        "max_abs_deltaL": float(np.max(np.abs(dL))),
        "mean_abs_deltaa": float(np.mean(np.abs(da))),
        "max_abs_deltaa": float(np.max(np.abs(da))),
        "mean_abs_deltab": float(np.mean(np.abs(db))),
        "max_abs_deltab": float(np.max(np.abs(db))),
    }


def save_rectified_with_rois(
    rectified_bgr: np.ndarray,
    measurements: Sequence[PatchMeasurement],
    output_path: str,
) -> None:
    vis = rectified_bgr.copy()
    for m in measurements:
        x, y, w, h = m.roi_rectified_xywh
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 255, 255), 2)
        label = f"{m.patch} {m.delta_e_00:.2f}"
        cv2.putText(
            vis,
            label,
            (x + 4, y + 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
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


def save_delta_e_heatmap(measurements: Sequence[PatchMeasurement], grid_rows: int, grid_cols: int, output_path: str) -> None:
    arr, labels = measurement_grid(measurements, grid_rows, grid_cols, lambda m: m.delta_e_00)
    save_heatmap_from_values(arr, labels, grid_rows, grid_cols, "ΔE00 heatmap", "ΔE00", output_path)


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

    for cs, label in [
        (srgb, "sRGB"),
        (eci, "eciRGB v2"),
        (adobe, "Adobe RGB (1998)"),
    ]:
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
    ordered = sorted(measurements, key=lambda m: m.delta_e_00, reverse=True)[:top_n]
    labels = [m.patch for m in ordered][::-1]
    values = [m.delta_e_00 for m in ordered][::-1]

    fig, ax = plt.subplots(figsize=(10, max(6, len(labels) * 0.35 + 2)))
    ax.barh(labels, values)
    ax.set_title(f"Top {len(labels)} worst patches by ΔE00")
    ax.set_xlabel("ΔE00")
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
    neutrals = sorted([m for m in measurements if m.is_neutral_reference], key=lambda m: (m.row, m.col))
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
    a_meas = [m.lab_measured[1] for m in neutrals]
    b_meas = [m.lab_measured[2] for m in neutrals]

    fig, ax = plt.subplots(figsize=(max(8, len(neutrals) * 0.7), 6))
    ax.plot(x, L_ref, marker="o", label="L* reference")
    ax.plot(x, L_meas, marker="o", label="L* measured")
    ax.plot(x, a_meas, marker="x", label="a* measured")
    ax.plot(x, b_meas, marker="x", label="b* measured")
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
                "reference_chroma": m.reference_chroma,
                "is_neutral_reference": m.is_neutral_reference,
                "deltaE00": m.delta_e_00,
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
    metamorfoze_eval: Dict[str, Optional[bool]],
    neutral_summary: Dict[str, object],
    corners_xy: Sequence[Tuple[float, float]],
) -> None:
    delta_e_values = [m.delta_e_00 for m in measurements]
    summary = {
        "image": str(Path(image_path).resolve()),
        "reference": str(Path(reference_path).resolve()),
        "profile_used": profile_name,
        "generated_plots": {
            "heatmap": "deltae_heatmap.png",
            "deltaL_heatmap": "deltaL_heatmap.png",
            "deltaa_heatmap": "deltaa_heatmap.png",
            "deltab_heatmap": "deltab_heatmap.png",
            "top_patches": "top_patches.png",
            "lstar_scatter": "lstar_scatter.png",
            "neutral_scale_plot": "neutral_scale_plot.png",
            "colourspace_chromaticity": "colourspace_chromaticity.png",
            "measured_rgb_bars": "measured_rgb_bars.png",
            "html_report": "report.html",
        },
        "patch_count": len(measurements),
        "mean_deltaE00": float(np.mean(delta_e_values)) if delta_e_values else None,
        "max_deltaE00": float(np.max(delta_e_values)) if delta_e_values else None,
        "metamorfoze": metamorfoze_eval,
        "neutral_scale": neutral_summary,
        "corners_xy": [[float(x), float(y)] for x, y in corners_xy],
        "worst_patches": [
            {"patch": m.patch, "deltaE00": m.delta_e_00}
            for m in sorted(measurements, key=lambda m: m.delta_e_00, reverse=True)[:10]
        ],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)


def report_strings() -> Dict[str, Dict[str, str]]:
    return {
        "cze": {
            "title": "DeltaE2000 report",
            "lang_label": "Jazyk",
            "summary": "Souhrn",
            "inputs": "Vstupy",
            "image": "Obraz",
            "reference": "Reference",
            "profile": "Použitý profil",
            "patch_count": "Počet polí",
            "mean_de": "Průměrná ΔE00",
            "max_de": "Maximální ΔE00",
            "metamorfoze": "Metamorfoze",
            "plots": "Grafy",
            "legend": "Legenda patchů",
            "worst": "Nejhorší pole",
            "patch": "Patch",
            "deltae": "ΔE00",
            "deltaL": "ΔL*",
            "deltaa": "Δa*",
            "deltab": "Δb*",
            "ref_swatch": "Reference",
            "meas_swatch": "Měření",
            "lab_ref": "Lab reference",
            "lab_meas": "Lab měření",
            "rgb_meas": "RGB měření",
            "overlay_desc": "Narovnaný target s vyznačenými měřicími oblastmi. Slouží ke kontrole, že segmentace sedí na správná pole a měří se střed patchů.",
            "de_heatmap_desc": "Celková barevná odchylka po jednotlivých polích. Nízké hodnoty znamenají dobrou shodu s referencí, vyšší hodnoty ukazují problémová pole nebo systematickou chybu.",
            "dl_heatmap_desc": "Rozdíl světlosti vůči referenci. Kladné hodnoty znamenají světlejší výsledek, záporné tmavší.",
            "da_heatmap_desc": "Posun na ose zelená–červená. Kladné hodnoty znamenají posun do červena, záporné do zelena.",
            "db_heatmap_desc": "Posun na ose modrá–žlutá. Kladné hodnoty znamenají posun do žluta, záporné do modra.",
            "top_desc": "Přehled polí s nejvyšší ΔE00. Užitečné pro rychlou identifikaci nejproblematičtějších patchů.",
            "lstar_desc": "Porovnání referenční a naměřené světlosti. Body blízko diagonály znamenají dobrou shodu tonalit.",
            "neutral_desc": "Samostatné vyhodnocení neutrálních polí odvozených z referenční tabulky. Pomáhá rychle zkontrolovat tonalitu, neutralitu a případný barevný nádech šedé škály.",
            "chroma_desc": "Rozložení referenčních a naměřených barev v rovině x,y vůči gamutům sRGB, eciRGB v2 a Adobe RGB (1998). Slouží orientačně, ne jako náhrada ΔE.",
            "rgb_desc": "Naměřené RGB hodnoty všech patchů. Pomáhá odhalit clipping, nerovnováhu kanálů nebo nečekané trendy.",
        },
        "eng": {
            "title": "DeltaE2000 report",
            "lang_label": "Language",
            "summary": "Summary",
            "inputs": "Inputs",
            "image": "Image",
            "reference": "Reference",
            "profile": "Profile used",
            "patch_count": "Patch count",
            "mean_de": "Mean ΔE00",
            "max_de": "Max ΔE00",
            "metamorfoze": "Metamorfoze",
            "plots": "Plots",
            "legend": "Patch legend",
            "worst": "Worst patches",
            "patch": "Patch",
            "deltae": "ΔE00",
            "deltaL": "ΔL*",
            "deltaa": "Δa*",
            "deltab": "Δb*",
            "ref_swatch": "Reference",
            "meas_swatch": "Measured",
            "lab_ref": "Reference Lab",
            "lab_meas": "Measured Lab",
            "rgb_meas": "Measured RGB",
            "overlay_desc": "Rectified target with sampling ROIs. Useful for confirming that segmentation is aligned with the intended patch centers.",
            "de_heatmap_desc": "Overall color difference per patch. Low values indicate good agreement with the reference, while higher values highlight problematic patches or systematic drift.",
            "dl_heatmap_desc": "Lightness difference relative to the reference. Positive values mean lighter output, negative values darker.",
            "da_heatmap_desc": "Shift on the green–red axis. Positive values indicate a red shift, negative values a green shift.",
            "db_heatmap_desc": "Shift on the blue–yellow axis. Positive values indicate a yellow shift, negative values a blue shift.",
            "top_desc": "Patches with the highest ΔE00 values. Useful for quickly identifying the most problematic areas.",
            "lstar_desc": "Reference versus measured lightness. Points near the diagonal indicate good tonal agreement.",
            "neutral_desc": "Separate evaluation of neutral patches derived from the reference table. Useful for quickly checking tonality, neutrality, and possible color cast in the gray scale.",
            "chroma_desc": "Reference and measured chromaticities in the x,y plane relative to the sRGB, eciRGB v2 and Adobe RGB (1998) gamuts. This is an orientation aid, not a substitute for ΔE.",
            "rgb_desc": "Measured RGB values for all patches. Useful for detecting clipping, channel imbalance, or unexpected trends.",
        },
    }


def write_html_report(
    output_path: str,
    image_path: str,
    reference_path: str,
    profile_name: str,
    measurements: Sequence[PatchMeasurement],
    metamorfoze_eval: Dict[str, Optional[bool]],
    neutral_summary: Dict[str, object],
    plot_paths: Dict[str, str],
) -> None:
    strings = report_strings()
    s_cz = strings["cze"]
    s_en = strings["eng"]

    mean_de = float(np.mean([m.delta_e_00 for m in measurements])) if measurements else float("nan")
    max_de = float(np.max([m.delta_e_00 for m in measurements])) if measurements else float("nan")
    worst_measurements = sorted(measurements, key=lambda m: m.delta_e_00, reverse=True)[:15]

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
            f"<tr><td>{html.escape(m.patch)}</td><td>{m.delta_e_00:.2f}</td><td>{m.delta_L:.2f}</td><td>{m.delta_a:.2f}</td><td>{m.delta_b:.2f}</td></tr>"
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
            f"<td>{m.delta_e_00:.2f}</td>"
            f"</tr>"
        )

    metamorfoze_pretty = html.escape(json.dumps(metamorfoze_eval, ensure_ascii=False, indent=2))
    neutral_pretty = html.escape(json.dumps(neutral_summary, ensure_ascii=False, indent=2))

    html_text = f"""<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(s_cz["title"])}</title>
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
.plot-list {{
  display: block;
}}
.plot-card {{
  border: 1px solid #ddd;
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 24px;
  width: 100%;
  box-sizing: border-box;
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
pre {{
  background: #f7f7f7;
  padding: 10px;
  overflow-x: auto;
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
</style>
<script>
function setLang(lang) {{
  document.body.setAttribute("data-lang", lang);
}}
</script>
</head>
<body data-lang="cze">
<div class="controls">
  <label for="lang-switch" class="lang-cze">{html.escape(s_cz["lang_label"])}:</label>
  <label for="lang-switch" class="lang-eng">{html.escape(s_en["lang_label"])}:</label>
  <select id="lang-switch" onchange="setLang(this.value)">
    <option value="cze">CZE</option>
    <option value="eng">ENG</option>
  </select>
</div>

<h1 class="lang-cze">{html.escape(s_cz["title"])}</h1>
<h1 class="lang-eng">{html.escape(s_en["title"])}</h1>

<div class="summary-grid">
  <div class="card">
    <h2 class="lang-cze">{html.escape(s_cz["inputs"])}</h2>
    <h2 class="lang-eng">{html.escape(s_en["inputs"])}</h2>
    <p><strong class="lang-cze">{html.escape(s_cz["image"])}:</strong><strong class="lang-eng">{html.escape(s_en["image"])}:</strong> {html.escape(str(image_path))}</p>
    <p><strong class="lang-cze">{html.escape(s_cz["reference"])}:</strong><strong class="lang-eng">{html.escape(s_en["reference"])}:</strong> {html.escape(str(reference_path))}</p>
    <p><strong class="lang-cze">{html.escape(s_cz["profile"])}:</strong><strong class="lang-eng">{html.escape(s_en["profile"])}:</strong> {html.escape(profile_name)}</p>
  </div>

  <div class="card">
    <h2 class="lang-cze">{html.escape(s_cz["summary"])}</h2>
    <h2 class="lang-eng">{html.escape(s_en["summary"])}</h2>
    <p><strong class="lang-cze">{html.escape(s_cz["patch_count"])}:</strong><strong class="lang-eng">{html.escape(s_en["patch_count"])}:</strong> {len(measurements)}</p>
    <p><strong class="lang-cze">{html.escape(s_cz["mean_de"])}:</strong><strong class="lang-eng">{html.escape(s_en["mean_de"])}:</strong> {mean_de:.3f}</p>
    <p><strong class="lang-cze">{html.escape(s_cz["max_de"])}:</strong><strong class="lang-eng">{html.escape(s_en["max_de"])}:</strong> {max_de:.3f}</p>
  </div>

  <div class="card">
    <h2 class="lang-cze">{html.escape(s_cz["metamorfoze"])}</h2>
    <h2 class="lang-eng">{html.escape(s_en["metamorfoze"])}</h2>
    <pre>{metamorfoze_pretty}</pre>
  </div>

  <div class="card">
    <h2>Neutral scale</h2>
    <pre>{neutral_pretty}</pre>
  </div>
</div>

<h2 class="lang-cze">{html.escape(s_cz["plots"])}</h2>
<h2 class="lang-eng">{html.escape(s_en["plots"])}</h2>

<div class="plot-list">
  {plot_section("overlay", "Overlay", s_cz["overlay_desc"], s_en["overlay_desc"])}
  {plot_section("deltae_heatmap", "ΔE00 heatmap", s_cz["de_heatmap_desc"], s_en["de_heatmap_desc"])}
  {plot_section("deltaL_heatmap", "ΔL* heatmap", s_cz["dl_heatmap_desc"], s_en["dl_heatmap_desc"])}
  {plot_section("deltaa_heatmap", "Δa* heatmap", s_cz["da_heatmap_desc"], s_en["da_heatmap_desc"])}
  {plot_section("deltab_heatmap", "Δb* heatmap", s_cz["db_heatmap_desc"], s_en["db_heatmap_desc"])}
  {plot_section("top_patches", "Top patches", s_cz["top_desc"], s_en["top_desc"])}
  {plot_section("lstar_scatter", "L* scatter", s_cz["lstar_desc"], s_en["lstar_desc"])}
  {plot_section("neutral_scale_plot", "Neutral scale", s_cz["neutral_desc"], s_en["neutral_desc"])}
  {plot_section("colourspace_chromaticity", "Chromaticity", s_cz["chroma_desc"], s_en["chroma_desc"])}
  {plot_section("measured_rgb_bars", "Measured RGB bars", s_cz["rgb_desc"], s_en["rgb_desc"])}
</div>

<h2 class="lang-cze">{html.escape(s_cz["worst"])}</h2>
<h2 class="lang-eng">{html.escape(s_en["worst"])}</h2>
<table>
<thead>
<tr>
  <th>{html.escape(s_cz["patch"])}</th>
  <th>{html.escape(s_cz["deltae"])}</th>
  <th>{html.escape(s_cz["deltaL"])}</th>
  <th>{html.escape(s_cz["deltaa"])}</th>
  <th>{html.escape(s_cz["deltab"])}</th>
</tr>
</thead>
<tbody>
{''.join(worst_rows)}
</tbody>
</table>

<h2 class="lang-cze">{html.escape(s_cz["legend"])}</h2>
<h2 class="lang-eng">{html.escape(s_en["legend"])}</h2>
<table>
<thead>
<tr>
  <th>{html.escape(s_cz["patch"])}</th>
  <th>{html.escape(s_cz["ref_swatch"])}</th>
  <th>{html.escape(s_cz["meas_swatch"])}</th>
  <th>{html.escape(s_cz["lab_ref"])}</th>
  <th>{html.escape(s_cz["lab_meas"])}</th>
  <th>{html.escape(s_cz["rgb_meas"])}</th>
  <th>{html.escape(s_cz["deltae"])}</th>
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

    delta_e_values = np.array([m.delta_e_00 for m in measurements], dtype=float)
    mean_de = float(np.mean(delta_e_values))
    max_de = float(np.max(delta_e_values))
    metamorfoze_eval = evaluate_metamorfoze(mean_de, max_de, args.metamorfoze_level)
    neutral_summary = summarize_neutral_scale(measurements)

    rectified_path = output_dir / "rectified.png"
    overlay_path = output_dir / "overlay.png"
    heatmap_path = output_dir / "deltae_heatmap.png"
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
    save_delta_e_heatmap(measurements, args.grid_rows, args.grid_cols, str(heatmap_path))
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
        "deltae_heatmap": str(heatmap_path),
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
            neutral_summary,
            plot_paths,
        )

    print(f"Image:           {args.image}")
    print(f"Reference:       {args.reference}")
    print(f"Profile used:    {profile_name}")
    print(f"Patch count:     {len(measurements)}")
    print(f"Mean ΔE00:       {mean_de:.3f}")
    print(f"Max  ΔE00:       {max_de:.3f}")
    print(f"Neutral patches: {neutral_summary['patch_count']}")

    if metamorfoze_eval["overall_pass"] is not None:
        print(
            "Metamorfoze:     "
            f"{metamorfoze_eval['level']} | "
            f"mean<= {metamorfoze_eval['mean_limit']} => {metamorfoze_eval['mean_pass']} | "
            f"max<= {metamorfoze_eval['max_limit']} => {metamorfoze_eval['max_pass']} | "
            f"overall => {metamorfoze_eval['overall_pass']}"
        )

    print("\nOutputs:")
    for path in [
        rectified_path,
        overlay_path,
        heatmap_path,
        deltaL_path,
        deltaa_path,
        deltab_path,
        top_path,
        lstar_path,
        neutral_plot_path,
    ]:
        print(f"  {path}")
    if not args.skip_colourspace_plot:
        print(f"  {chromaticity_path}")
    if not args.skip_rgb_bars_plot:
        print(f"  {rgb_bars_path}")
    print(f"  {csv_path}")
    print(f"  {json_path}")
    if not args.skip_html_report:
        print(f"  {html_path}")

    return 0


def run_self_tests() -> int:
    import tempfile

    def assert_true(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def test_patch_name_to_row_col() -> None:
        assert_true(patch_name_to_row_col("A1") == (0, 0), "Unexpected A1 mapping")
        assert_true(patch_name_to_row_col("N10") == (9, 13), "Unexpected N10 mapping")

    def test_parse_float_maybe_comma() -> None:
        assert_true(abs(parse_float_maybe_comma("96,25") - 96.25) < 1e-9, "Comma parsing failed")

    def test_reference_chroma_neutral_detection() -> None:
        lab = (50.0, 1.0, 2.0)
        chroma = reference_chroma(lab)
        assert_true(abs(chroma - np.hypot(1.0, 2.0)) < 1e-9, "Chroma computation failed")

    def test_normalize_reference_columns_european_excel_style() -> None:
        df = pd.DataFrame([
            {"Unnamed: 0": "A1", "L*": "96,2985", "a*": "-0,5458", "b*": "1,5096"},
            {"Unnamed: 0": "B10", "L*": "49,3193", "a*": "-0,3254", "b*": "0,3267"},
        ])
        normalized = normalize_reference_columns(df, "reference.xlsx")
        assert_true(list(normalized.columns) == ["patch", "L", "a", "b", "row", "col"], "Unexpected columns")
        assert_true(int(normalized.iloc[1]["row"]) == 9 and int(normalized.iloc[1]["col"]) == 1, "Unexpected B10 position")

    def test_decode_pillow_lab_pixel_signed_ab() -> None:
        pixel = np.array([245, 0, 3], dtype=np.uint8)
        L, a, b = decode_pillow_lab_pixel(pixel)
        assert_true(a == 0.0 and b == 3.0, "Signed LAB decoding failed")
        assert_true(L > 90.0, "Unexpected L decoding")

    def test_load_reference_csv() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "ref.csv"
            pd.DataFrame([
                {"patch": "A1", "L": 50, "a": 0, "b": 0, "row": 0, "col": 0},
                {"patch": "A2", "L": 60, "a": 1, "b": 2, "row": 0, "col": 1},
            ]).to_csv(csv_path, index=False)
            refs = load_reference_table(str(csv_path))
            assert_true(len(refs) == 2, "Failed to load CSV reference")

    def test_load_reference_txt_simple() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            txt_path = Path(tmpdir) / "ref_simple.txt"
            txt_path.write_text(
                "Patch\tLAB_L\tLAB_A\tLAB_B\n"
                "A1\t96.55\t-0.91\t0.57\n"
                "B10\t49.69\t-0.20\t0.01\n"
                "\n"
                "The data in this file is reported in CIE L* a* b* data\n",
                encoding="utf-8",
            )
            refs = load_reference_table(str(txt_path))
            assert_true(len(refs) == 2, f"Expected 2 references from simple txt, got {len(refs)}")
            assert_true(refs[0].patch == "A1", f"Unexpected first simple txt patch: {refs[0].patch}")
            assert_true(refs[1].row == 9 and refs[1].col == 1, f"Unexpected B10 position: {refs[1].row}, {refs[1].col}")

    def test_load_reference_txt_cgats_minimal() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            txt_path = Path(tmpdir) / "ref_cgats.txt"
            txt_path.write_text(
                "NUMBER_OF_FIELDS 4\n"
                "BEGIN_DATA_FORMAT\n"
                "Sample_NAME   Lab_L   Lab_a   Lab_b\n"
                "END_DATA_FORMAT\n"
                "NUMBER_OF_SETS 2\n"
                "BEGIN_DATA\n"
                "A1 95.81 -0.11 2.34\n"
                "B10 50.28 -0.19 1.43\n"
                "END_DATA\n",
                encoding="utf-8",
            )
            refs = load_reference_table(str(txt_path))
            assert_true(len(refs) == 2, f"Expected 2 references from CGATS txt, got {len(refs)}")
            assert_true(refs[0].patch == "A1", f"Unexpected first CGATS txt patch: {refs[0].patch}")
            assert_true(refs[1].row == 9 and refs[1].col == 1, f"Unexpected B10 CGATS position: {refs[1].row}, {refs[1].col}")

    def test_load_reference_txt_cgats_extended() -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            txt_path = Path(tmpdir) / "ref_cgats_extended.txt"
            txt_path.write_text(
                "NUMBER_OF_FIELDS 11\n"
                "BEGIN_DATA_FORMAT\n"
                "SampleID SAMPLE_NAME RGB_R RGB_G RGB_B XYZ_X XYZ_Y XYZ_Z LAB_L LAB_A LAB_B\n"
                "END_DATA_FORMAT\n"
                "NUMBER_OF_SETS 2\n"
                "BEGIN_DATA\n"
                "1 A1 242.81 244.08 241.52 86.28 89.54 71.21 95.81 -0.11 2.34\n"
                "2 B10 110.59 110.69 110.39 17.95 18.65 14.82 50.28 -0.19 1.43\n"
                "END_DATA\n",
                encoding="utf-8",
            )
            refs = load_reference_table(str(txt_path))
            assert_true(len(refs) == 2, f"Expected 2 references from extended CGATS txt, got {len(refs)}")
            assert_true(refs[0].patch == "A1", f"Unexpected first extended CGATS txt patch: {refs[0].patch}")
            assert_true(refs[1].row == 9 and refs[1].col == 1, f"Unexpected B10 extended CGATS position: {refs[1].row}, {refs[1].col}")

    tests = [
        test_patch_name_to_row_col,
        test_parse_float_maybe_comma,
        test_reference_chroma_neutral_detection,
        test_normalize_reference_columns_european_excel_style,
        test_decode_pillow_lab_pixel_signed_ab,
        test_load_reference_csv,
        test_load_reference_txt_simple,
        test_load_reference_txt_cgats_minimal,
        test_load_reference_txt_cgats_extended,
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
            "  python deltae2000.py --image sample.tif --reference ref.csv --output-dir out --icc eciRGB_v2.icc"
        )
        return 0

    args = parse_args(effective_argv)
    if args.run_tests:
        return run_self_tests()
    return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
#!/usr/bin/env python
"""Test script to render IFC file with 2D preview, 3D Blender render, and ZIP batch processing.

Usage:
    cd /Users/saman.anvari/Documents/GitHub/cerp/services/ifc-renderer-python
    source .venv/bin/activate
    python src/test/test_render_ifc.py

Outputs are saved to src/test/results/
"""

from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ifc_handler import (
    export_3d_model,
    generate_preview,
    get_metadata,
    render_with_blender,
)
from src.models import ExportFormat

RESULTS_DIR = Path(__file__).resolve().parent / "results"
IFC_FILE = Path(__file__).resolve().parent / "Building-Structural.ifc"
ZIP_FILE = Path(__file__).resolve().parent / "NBU_Duplex_ifc.zip"


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Results: {RESULTS_DIR}")
    print(f"IFC: {IFC_FILE} ({IFC_FILE.stat().st_size} bytes)")
    print(f"ZIP: {ZIP_FILE} ({ZIP_FILE.stat().st_size} bytes)")
    _test_single_ifc()
    _test_zip_extraction()


def _test_single_ifc():
    """Run existing single-IFC tests."""
    print("=" * 60)
    print("PART A - Single IFC file")
    print("=" * 60)
    ifc_bytes = IFC_FILE.read_bytes()
    print(f"[1/5] Read: {len(ifc_bytes)} bytes")
    print("[2/5] Metadata...")
    t0 = time.time()
    metadata = get_metadata(ifc_bytes)
    t1 = time.time()
    print(f"  Done in {t1-t0:.2f}s - Name: {metadata.name}, Elements: {metadata.total_elements}")
    meta_path = RESULTS_DIR / "metadata.json"
    with open(meta_path, "w") as f:
        json.dump(metadata.model_dump(), f, indent=2)
    print(f"  Saved -> {meta_path}")
    print("[3/5] Export OBJ...")
    t0 = time.time()
    obj_data = export_3d_model(ifc_bytes, ExportFormat.OBJ)
    t1 = time.time()
    print(f"  Done in {t1-t0:.2f}s - {len(obj_data)} bytes")
    obj_path = RESULTS_DIR / "model.obj"
    obj_path.write_bytes(obj_data)
    print(f"  Saved -> {obj_path}")
    print("[4/5] Preview...")
    t0 = time.time()
    preview_data = generate_preview(ifc_bytes, target_size=(800, 600))
    t1 = time.time()
    print(f"  Done in {t1-t0:.2f}s - {len(preview_data)} bytes")
    preview_path = RESULTS_DIR / "preview_2d.png"
    preview_path.write_bytes(preview_data)
    print(f"  Saved -> {preview_path}")
    _analyze_image(preview_data, "Preview")
    print("[5/5] Blender render...")
    try:
        render_data = render_with_blender(ifc_bytes, 1920, 1080, 256)
        print(f"  Done - {len(render_data)} bytes")
        render_path = RESULTS_DIR / "render_3d.png"
        render_path.write_bytes(render_data)
        print(f"  Saved -> {render_path}")
        _analyze_image(render_data, "3D Render")
    except RuntimeError as e:
        print(f"  Failed: {e}")
    print("=" * 60)
    print(f"  Metadata: {meta_path}")
    print(f"  OBJ:      {obj_path}")
    print(f"  Preview:  {preview_path}")


def _test_zip_extraction():
    """Extract and process all IFC files from the ZIP."""
    print()
    print("=" * 60)
    print("PART B - ZIP batch processing")
    print("=" * 60)
    if not ZIP_FILE.exists():
        print(f"ZIP not found: {ZIP_FILE}")
        return
    zip_data = ZIP_FILE.read_bytes()
    print(f"Read ZIP: {len(zip_data)} bytes")
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        ifc_entries = [n for n in zf.namelist() if n.lower().endswith(".ifc") and not n.endswith("/")]
        print(f"Found {len(ifc_entries)} IFC files in ZIP:")
        for i, name in enumerate(ifc_entries, 1):
            info = zf.getinfo(name)
            print(f"  {i:2d}. {name:<65s} {info.file_size:>12,} bytes")
        sample = ifc_entries[6]  # NBU_Duplex-Apt_Arch.ifc (2.4MB, should have real geometry)
        print(f"\nProcessing sample: {sample}")
        ifc_bytes = zf.read(sample)
        print(f"  Extracted: {len(ifc_bytes):,} bytes")
        print("  [1/3] Metadata...")
        t0 = time.time()
        metadata = get_metadata(ifc_bytes)
        t1 = time.time()
        print(f"    Done in {t1-t0:.2f}s - Elements: {metadata.total_elements}")
        stem = Path(sample).stem
        meta_out = RESULTS_DIR / f"metadata_{stem}.json"
        with open(meta_out, "w") as f:
            json.dump(metadata.model_dump(), f, indent=2)
        print(f"    Saved -> {meta_out}")
        print("  [2/3] Preview...")
        t0 = time.time()
        preview_data = generate_preview(ifc_bytes, target_size=(800, 600))
        t1 = time.time()
        print(f"    Done in {t1-t0:.2f}s - {len(preview_data):,} bytes")
        preview_out = RESULTS_DIR / f"preview_{stem}.png"
        preview_out.write_bytes(preview_data)
        print(f"    Saved -> {preview_out}")
        _analyze_image(preview_data, f"Preview[{stem}]")
        print("  [3/3] OBJ export...")
        t0 = time.time()
        try:
            obj_data = export_3d_model(ifc_bytes, ExportFormat.OBJ)
            t1 = time.time()
            print(f"    Done in {t1-t0:.2f}s - {len(obj_data):,} bytes")
            obj_out = RESULTS_DIR / f"model_{stem}.obj"
            obj_out.write_bytes(obj_data)
            print(f"    Saved -> {obj_out}")
        except ValueError as e:
            print(f"    SKIPPED (no geometry): {e}")
    print()
    print("=" * 60)
    print(f"PART B SUMMARY - ZIP OK ({len(ifc_entries)} files, sample: {sample})")
    print("=" * 60)


def _analyze_image(png_data: bytes, label: str) -> None:
    """Analyze a PNG image to detect placeholder vs real render."""
    try:
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(png_data))
        pixels = list(img.getdata())
        if pixels:
            unique_colors = len(set(pixels))
            avg_color = tuple(int(sum(c) / len(pixels)) for c in zip(*pixels))
            print(f"  {label} unique colors: {unique_colors}")
            print(f"  {label} average color: RGB{avg_color}")
            if unique_colors < 10:
                print(f"  {label} WARNING: Very few unique colors - likely a placeholder!")
            else:
                print(f"  {label} OK: Contains real geometry")
    except Exception as e:
        print(f"  {label} Could not analyze: {e}")


if __name__ == "__main__":
    main()

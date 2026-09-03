#!/usr/bin/env python
"""Test KIT_Institute_ifc.zip — generates preview and combined preview."""

import io
import sys
import time
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.ifc_handler import generate_preview, generate_zip_preview

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ZIP_FILE = Path(__file__).resolve().parent / "KIT_Institute_ifc.zip"


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print("KIT_Institute_ifc.zip Test")
    print("=" * 60)
    print(f"ZIP: {ZIP_FILE} ({ZIP_FILE.stat().st_size:,} bytes)")

    zip_data = ZIP_FILE.read_bytes()
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        ifc_entries = [
            n for n in zf.namelist()
            if n.lower().endswith(".ifc") and not n.endswith("/")
        ]
        print(f"Found {len(ifc_entries)} IFC file(s) in ZIP:")
        for i, name in enumerate(ifc_entries, 1):
            info = zf.getinfo(name)
            print(f"  {i:2d}. {name:<65s} {info.file_size:>12,} bytes")

        for entry in ifc_entries:
            ifc_bytes = zf.read(entry)
            print(f"\n--- Processing: {entry} ({len(ifc_bytes):,} bytes) ---")

            # Single preview
            print("  [1/2] Preview...")
            t0 = time.time()
            preview_data = generate_preview(ifc_bytes, target_size=(800, 600))
            t1 = time.time()
            print(f"    Done in {t1-t0:.2f}s - {len(preview_data):,} bytes")
            preview_out = RESULTS_DIR / f"KIT_preview_{Path(entry).stem}.png"
            preview_out.write_bytes(preview_data)
            print(f"    Saved -> {preview_out}")

            _analyze_image(preview_data, f"Preview[{Path(entry).stem}]")

    # Combined ZIP preview
    print(f"\n--- Combined ZIP preview ---")
    t0 = time.time()
    combined = generate_zip_preview(zip_data)
    t1 = time.time()
    print(f"Done in {t1-t0:.2f}s - {len(combined):,} bytes")
    combined_out = RESULTS_DIR / "KIT_combined_preview.png"
    combined_out.write_bytes(combined)
    print(f"Saved -> {combined_out}")
    _analyze_image(combined, "Combined Preview")

    print(f"\n{'=' * 60}")
    print("KIT Test COMPLETE")
    print("=" * 60)


def _analyze_image(png_data: bytes, label: str) -> None:
    """Analyze a PNG image to detect placeholder vs real render."""
    from PIL import Image
    from io import BytesIO
    img = Image.open(BytesIO(png_data))
    pixels = list(img.getdata())
    if pixels:
        unique_colors = len(set(pixels))
        avg_color = tuple(int(sum(c) / len(pixels)) for c in zip(*pixels))
        print(f"  {label}: size={img.size}, unique colors={unique_colors}, avg RGB{avg_color}")
        if unique_colors < 10:
            print(f"  {label} WARNING: Very few unique colors - likely a placeholder!")
        else:
            print(f"  {label} OK: Contains real geometry")


if __name__ == "__main__":
    main()

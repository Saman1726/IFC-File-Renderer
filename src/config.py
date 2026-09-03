"""Configuration for the IFC Renderer service."""

import os
from pathlib import Path

# --- Server ---
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8093"))
WORKERS = int(os.getenv("WORKERS", "2"))

# --- Document Service ---
DOCUMENT_SERVICE_URL = os.getenv(
    "DOCUMENT_SERVICE_URL", "http://document-service-java:8092"
)

# --- Paths ---
BASE_DIR = Path(__file__).resolve().parent.parent

# --- Rendering ---
PREVIEW_SIZE = (800, 600)
BLENDER_VERSION = os.getenv("BLENDER_VERSION", "4.1.0")
BLENDER_PATH = os.getenv(
    "BLENDER_PATH",
    str(Path.home() / "local" / "apps" / "Blender.app" / "Contents" / "MacOS" / "Blender"),
)

# --- File Limits ---
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE_MB", "500")) * 1024 * 1024  # bytes
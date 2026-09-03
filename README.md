# CERP IFC Renderer

Python microservice for parsing IFC (Industry Foundation Classes) building models, extracting metadata, generating 2D preview thumbnails, exporting 3D model files, and producing high-quality Blender renders.

## Tech Stack

| Component | Technology |
|---|---|
| **Runtime** | Python 3.12, FastAPI, Uvicorn |
| **IFC Parsing** | ifcopenshell 0.8.0 (Open BIM geometry kernel) |
| **2D Previews** | Pillow — silhouette PNG from top-down vertex projection |
| **3D Export** | trimesh — glTF, glb, OBJ, STL from tessellated geometry |
| **Blender Renders** | Blender 4.1 CLI — Cycles engine, multi-light scene setup |
| **Object Storage** | MinIO — IFC files, previews, and render outputs |
| **Deployment** | Multi-stage Docker image (~770 MB) |

## Architecture

```
IFC File Upload
     │
     ▼
┌─────────────────┐
│  ifcopenshell    │── Parse metadata ──► JSON (elements, storeys, units)
│  (lightweight)   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  ifcopenshell   │────►│   Pillow         │────►│ 2D Preview PNG  │
│  + trimesh      │     │  (top-down       │     │  800×600        │
│  (geometry)     │     │   projection)    │     │                 │
└────────┬────────┘     └──────────────────┘     └─────────────────┘
         │
         ├──► trimesh ──► glTF / glb / OBJ / STL export
         │
         ├──► OBJ ──► Blender CLI ──► Cycles render ──► High-quality PNG
         │
         ▼
   ┌──────────┐
   │  MinIO   │── Store all outputs
   └──────────┘
```

## Quick Start

### 1. Start with Docker Compose

The service is in the `bim` profile so it doesn't start with the default compose stack:

```bash
docker compose --profile bim up -d minio ifc-renderer-python
```

This brings up MinIO (required for file storage) and the IFC renderer on **port 8093**.

### 2. Health Check

```bash
curl http://localhost:8093/health
```

Expected response:

```json
{
  "status": "healthy",
  "ifcopenshell_available": true,
  "blender_available": true,
  "minio_available": true
}
```

### 3. Upload an IFC File

```bash
curl -X POST http://localhost:8093/api/v1/ifc/upload \
  -F "file=@your-model.ifc"
Response:

```json
{
  "file_id": "a1b2c3d4-...",
  "original_filename": "your-model.ifc",
  "content_type": "application/ifc",
  "file_size": 1234567,
  "uploaded_at": "2026-08-22T12:00:00Z",
  "status": "DONE",
  "preview_url": "/api/v1/ifc/a1b2c3d4.../preview",
  "export_url": "/api/v1/ifc/a1b2c3d4.../export",
  "blender_render_url": "/api/v1/ifc/a1b2c3d4.../blender-render",
  "metadata_url": "/api/v1/ifc/a1b2c3d4.../metadata"
}
```

### 4. Fetch Outputs

```bash
# Download metadata JSON
curl http://localhost:8093/api/v1/ifc/<file_id>/metadata

# Download 2D preview PNG
curl http://localhost:8093/api/v1/ifc/<file_id>/preview --output preview.png

# Export to glTF
curl -X POST http://localhost:8093/api/v1/ifc/<file_id>/export \
  -H "Content-Type: application/json" \
  -d '{"format": "gltf"}' --output model.gltf

# Export to OBJ
curl -X POST http://localhost:8093/api/v1/ifc/<file_id>/export \
  -H "Content-Type: application/json" \
  -d '{"format": "obj"}' --output model.obj

# High-quality Blender render (20s-5min depending on model size)
curl -X POST http://localhost:8093/api/v1/ifc/<file_id>/blender-render \
  -H "Content-Type: application/json" \
  -d '{"resolution_x": 1920, "resolution_y": 1080, "samples": 256}' \
  --output render.png
```

## API Reference

### Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check with dependency status |
| `POST` | `/api/v1/ifc/upload` | Upload IFC file (auto-extracts metadata + 2D preview) |
| `GET` | `/api/v1/ifc/{file_id}/metadata` | Retrieve parsed metadata JSON |
| `GET` | `/api/v1/ifc/{file_id}/preview` | Download 2D preview PNG |
| `POST` | `/api/v1/ifc/{file_id}/export` | Export 3D model (body: `{format: "gltf"\|"glb"\|"obj"\|"stl"}`) |
| `POST` | `/api/v1/ifc/{file_id}/blender-render` | Render with Blender Cycles |

### Request Body — Export

```json
{
  "format": "gltf"
}
```

Supported formats: `gltf`, `glb`, `obj`, `stl`

### Request Body — Blender Render

```json
{
  "resolution_x": 1920,
  "resolution_y": 1080,
  "samples": 256
}
```

Limits: `samples` must be 1–4096. Typical Blender renders take 20 seconds to several minutes.

## Configuration

All config is via environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8093` | HTTP port |
| `WORKERS` | `2` | Uvicorn worker count |
| `MINIO_ENDPOINT` | `localhost:9000` | MinIO server address |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `MINIO_BUCKET` | `ifc-files` | MinIO bucket name |
| `MINIO_SECURE` | `false` | Use HTTPS for MinIO |
| `MAX_UPLOAD_SIZE_MB` | `500` | Max IFC file size in MB |
| `BLENDER_VERSION` | `4.1.0` | Blender version to download |
| `BLENDER_PATH` | `/opt/blender/blender` | Path to Blender binary |

## Service Directory Structure

```
services/ifc-renderer-python/
├── Dockerfile                  # Multi-stage: Ubuntu + Blender + Python
├── .dockerignore
├── .env.example
├── requirements.txt            # Python dependencies
└── src/
    ├── __init__.py
    ├── main.py                 # FastAPI app, routes, startup
    ├── config.py               # Environment-driven configuration
    ├── models.py               # Pydantic DTOs (UploadResponse, IFCMetadata, etc.)
    ├── storage.py              # MinIO client wrapper
    └── ifc_handler.py          # Core engine: upload, parse, preview, export, render
```

## Docker Image

The Docker image is built in two stages:

1. **builder** — Ubuntu 22.04 + Blender 4.1.0 download
2. **runtime** — Minimal Ubuntu + Blender + Python venv with dependencies

Image size: ~770 MB (Blender is the bulk)

Build:

```bash
docker compose --profile bim build
```

## Local Development (without Docker)

```bash
cd services/ifc-renderer-python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set environment variables for local MinIO
export MINIO_ENDPOINT=localhost:9000
export MINIO_ACCESS_KEY=minioadmin
export MINIO_SECRET_KEY=minioadmin
export MINIO_BUCKET=ifc-files

# Run dev server (auto-reload)
uvicorn src.main:app --reload --host 0.0.0.0 --port 8093
```

Note: Without Blender installed locally, the `/blender-render` endpoint will return a 503 error. The `preview`, `export`, and `metadata` endpoints work without Blender.

## Integration with CERP Platform

The IFC renderer is wired into the web frontend via Docker Compose:

- **Internal URL** (server-to-server): `http://ifc-renderer-python:8093`
- **Public URL** (browser-facing): `http://192.168.8.213:8093` (update to match your LAN IP)

The web app's `IFC_RENDERER_URL` and `NEXT_PUBLIC_IFC_RENDERER_URL` environment variables point to these addresses.
```
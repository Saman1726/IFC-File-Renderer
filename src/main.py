"""CERP IFC Renderer API — FastAPI application."""

from __future__ import annotations

import io
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, Request
from fastapi.responses import Response
import httpx

from src.config import DOCUMENT_SERVICE_URL
from src.document_client import DocumentClient
from src.ifc_handler import (
    export_3d_model,
    generate_preview,
    generate_zip_preview,
    get_metadata,
    render_with_blender,
    upload_ifc,
    upload_ifc_zip,
)
from src.models import (
    BatchItemResult,
    BatchUploadResponse,
    BlenderRenderRequest,
    BlenderRenderResponse,
    ExportFormat,
    ExportRequest,
    ExportResponse,
    HealthResponse,
    UploadResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="CERP IFC Renderer",
    description="Parse IFC building models, extract metadata, generate previews & 3D exports.",
    version="0.1.0",
)


def _doc_client(auth_token: Optional[str] = None, tenant_id: Optional[str] = None) -> DocumentClient:
    return DocumentClient(DOCUMENT_SERVICE_URL, auth_token, tenant_id)


@app.get("/health", response_model=HealthResponse)
def health_check():
    from src.ifc_handler import _ensure_blender, _ensure_ifc_import
    try:
        dc = _doc_client()
        dc.client.get("/api/v1/media/presign")
        dc.close()
        doc_ok = True
    except Exception:
        doc_ok = False
    return HealthResponse(
        status="healthy" if doc_ok else "degraded",
        ifcopenshell_available=_ensure_ifc_import(),
        blender_available=_ensure_blender(),
        document_service_available=doc_ok,
    )


@app.post("/api/v1/ifc/register")
def register_ifc_file(
    file_key: str = Query(..., description="S3 key of the already-uploaded IFC file"),
    project_id: str = Query(..., description="Project/site ID"),
):
    """Register an already-uploaded IFC file and return a file_id for subsequent calls.
    
    This endpoint does NOT upload the file again — the file must already exist
    at *file_key* in the document service (e.g. uploaded by the project service
    via a presigned URL). It simply generates a UUID and returns it so that
    downstream preview / render / export calls can address the file by ID.
    """
    file_id = str(uuid.uuid4())
    return {
        "file_id": file_id,
        "file_key": file_key,
        "project_id": project_id,
    }


@app.post("/api/v1/ifc/upload", response_model=UploadResponse)
async def upload_ifc_file(
    file: UploadFile = File(..., description="IFC file to upload"),
    project_id: str | None = Form(None, description="Optional project ID"),
):
    """Upload an IFC file to document-service for processing."""
    try:
        result = upload_ifc(
            file.file, file.filename or "model.ifc",
            file.content_type or "application/ifc",
            project_id=project_id or "",
            kind="IFC",
        )
        return UploadResponse(
            file_id=result["file_id"],
            original_filename=result["original_filename"],
            content_type=result["content_type"],
            file_size=result["file_size"],
            uploaded_at=result["uploaded_at"],
            status=result["status"],
            preview_url=result["preview_url"],
            export_url=result["export_url"],
            blender_render_url=result["blender_render_url"],
            metadata_url=result["metadata_url"],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Upload failed")
        raise HTTPException(status_code=500, detail="Upload failed")


@app.post("/api/v1/ifc/upload-zip", response_model=BatchUploadResponse)
async def upload_ifc_zip_file(
    file: UploadFile = File(..., description="ZIP file containing IFC files"),
    project_id: str | None = Form(None, description="Optional project ID"),
):
    """Upload a ZIP file containing multiple IFC files.

    The ZIP is extracted in-memory and every ``.ifc`` entry is uploaded
    individually to the document-service.  Returns a batch response with
    a result per extracted file plus a combined preview image.
    """
    try:
        zip_data = await file.read()
        if file.filename and not file.filename.lower().endswith(".zip"):
            # Allow non-.zip files but only if they look like a ZIP (magic bytes)
            if zip_data[:4] != b"PK\x03\x04":
                raise ValueError("Expected a ZIP file")
        results = upload_ifc_zip(
            zip_data,
            project_id=project_id or "",
            kind="IFC",
        )
        if not results:
            raise HTTPException(status_code=400, detail="No .ifc files found in ZIP")

        batch_files = [BatchItemResult(**r) for r in results]
        preview_urls = [f.preview_url for f in batch_files if f.preview_url]
        export_urls = [f.export_url for f in batch_files if f.export_url]
        blender_urls = [f.blender_render_url for f in batch_files if f.blender_render_url]
        metadata_urls = [f.metadata_url for f in batch_files if f.metadata_url]

        # Generate combined preview for all files in the ZIP
        combined_preview_url = None
        try:
            combined_png = generate_zip_preview(zip_data)
            from src.config import DOCUMENT_SERVICE_URL
            client = DocumentClient(DOCUMENT_SERVICE_URL)
            try:
                project_id_val = project_id or ""
                combined_key = f"previews/batch/{uuid.uuid4()}/combined_preview.png"
                surl, okey = client.presign_upload(
                    file_key=combined_key,
                    content_type="image/png",
                    project_id=project_id_val,
                    kind="PREVIEW",
                    file_name="combined_preview.png",
                )
                client.upload_to_presigned_url(
                    url=surl, data=combined_png, content_type="image/png")
                purl, _ = client.presign_download(okey)
                combined_preview_url = purl
            finally:
                client.close()
        except Exception:
            logger.exception("Combined preview generation failed")

        return BatchUploadResponse(
            total_files=len(results),
            success_count=len(results),
            failed_count=0,
            files=batch_files,
            preview_urls=preview_urls,
            export_urls=export_urls,
            blender_render_urls=blender_urls,
            metadata_urls=metadata_urls,
            combined_preview_url=combined_preview_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("ZIP upload failed")
        raise HTTPException(status_code=500, detail="ZIP upload failed")


@app.get("/api/v1/ifc/{file_id}/metadata")
def get_metadata_route(file_id: str, file_key: str = Query(...), request: Request = None):
    """Retrieve metadata extracted from the IFC file."""
    tenant_id = None
    if request:
        tenant_id = request.headers.get("X-Tenant-Id")
    # Use None for auth_token — download_internal() creates an unauthenticated
    # client for the internal server-to-server endpoint to avoid triggering the
    # document service's OAuth2 JWT filter.
    client = _doc_client(None, tenant_id)
    try:
        ifc_bytes = client.download_internal(file_key)
        metadata = get_metadata(ifc_bytes)
        return metadata.model_dump()
    except Exception:
        logger.exception("Metadata extraction failed for file_id=%s file_key=%s", file_id, file_key)
        raise HTTPException(status_code=500, detail="Metadata extraction failed")
    finally:
        client.close()


@app.get("/api/v1/ifc/{file_id}/preview")
def get_preview_route(file_id: str, file_key: str = Query(...), request: Request = None):
    """Return the 2D preview image."""
    tenant_id = None
    if request:
        tenant_id = request.headers.get("X-Tenant-Id")
    # Use None for auth_token — download_internal() creates an unauthenticated
    # client for the internal server-to-server endpoint to avoid triggering the
    # document service's OAuth2 JWT filter.
    client = _doc_client(None, tenant_id)
    try:
        ifc_bytes = client.download_internal(file_key)
        preview_data = generate_preview(ifc_bytes)
        return Response(content=preview_data, media_type="image/png")
    except Exception:
        logger.exception("Preview generation failed for file_id=%s file_key=%s", file_id, file_key)
        raise HTTPException(status_code=500, detail="Preview generation failed")
    finally:
        client.close()


@app.post("/api/v1/ifc/{file_id}/export", response_model=ExportResponse)
def request_export(
    file_id: str,
    export_req: ExportRequest,
    file_key: str = Query(...),
    request: Request = None,
):
    """Export the IFC geometry to glTF, glb, OBJ, or STL."""
    tenant_id = None
    project_id = None
    if request:
        tenant_id = request.headers.get("X-Tenant-Id")
        project_id = request.headers.get("X-Project-Id")
    # Use None for auth_token — download_internal() creates an unauthenticated
    # client for the internal server-to-server endpoint to avoid triggering the
    # document service's OAuth2 JWT filter.
    client = _doc_client(None, tenant_id)
    try:
        ifc_bytes = client.download_internal(file_key)
        export_data = export_3d_model(ifc_bytes, export_req.format)
        stem = Path(export_req.file_id).stem
        out_key = f"exports/{file_id}/{stem}.{export_req.format.value}"
        surl, object_key = client.presign_upload(
            out_key, "application/octet-stream",
            project_id=project_id or "",
            kind="EXPORT",
            file_name=f"{stem}.{export_req.format.value}",
        )
        client.upload_to_presigned_url(surl, export_data,
                                       "application/octet-stream")
        purl, _ = client.presign_download(object_key)
        return ExportResponse(file_id=file_id, format=export_req.format.value,
                              output_url=purl)
    except Exception:
        logger.exception("Export failed for file_id=%s file_key=%s", file_id, file_key)
        raise HTTPException(status_code=500, detail="Export failed")
    finally:
        client.close()


@app.post("/api/v1/ifc/{file_id}/blender-render",
          response_model=BlenderRenderResponse)
def request_blender_render(
    file_id: str,
    render_req: BlenderRenderRequest | None = None,
    file_key: str = Query(...),
    request: Request = None,
):
    """Render the IFC model with Blender and return the PNG."""
    tenant_id = None
    project_id = None
    if request:
        tenant_id = request.headers.get("X-Tenant-Id")
        project_id = request.headers.get("X-Project-Id")
    # Use None for auth_token — download_internal() creates an unauthenticated
    # client for the internal server-to-server endpoint to avoid triggering the
    # document service's OAuth2 JWT filter.
    client = _doc_client(None, tenant_id)
    try:
        ifc_bytes = client.download_internal(file_key)
        rx = render_req.resolution_x if render_req else 1920
        ry = render_req.resolution_y if render_req else 1080
        sp = render_req.samples if render_req else 256
        png_data = render_with_blender(ifc_bytes, rx, ry, sp)
        out_key = f"renders/{file_id}/render.png"
        try:
            surl, object_key = client.presign_upload(
                out_key, "image/png",
                project_id=project_id or "",
                kind="RENDER",
                file_name="render.png",
            )
            client.upload_to_presigned_url(surl, png_data, "image/png")
            purl, _ = client.presign_download(object_key)
            return BlenderRenderResponse(file_id=file_id, resolution=(rx, ry),
                                         samples=sp, output_url=purl)
        except httpx.HTTPStatusError:
            logger.warning("Presign upload/download failed for file_id=%s; returning PNG directly", file_id)
            return Response(content=png_data, media_type="image/png")
    except RuntimeError as e:
        logger.exception("Blender render unavailable for file_id=%s file_key=%s", file_id, file_key)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        logger.exception("Render failed for file_id=%s file_key=%s", file_id, file_key)
        raise HTTPException(status_code=500, detail="Render failed")
    finally:
        client.close()
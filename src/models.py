"""Pydantic DTOs for the IFC Renderer API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ExportFormat(str, Enum):
    GLTF = "gltf"
    GLB = "glb"
    OBJ = "obj"
    STL = "stl"


class UploadResponse(BaseModel):
    """Response after uploading an IFC file."""
    file_id: str
    original_filename: str
    content_type: str
    file_size: int
    uploaded_at: datetime
    status: str = "QUEUED"
    preview_url: Optional[str] = None
    export_url: Optional[str] = None
    blender_render_url: Optional[str] = None
    metadata_url: Optional[str] = None


class IFCMetadata(BaseModel):
    """Parsed metadata from an IFC file."""
    file_version: str = ""
    name: str = ""
    description: str = ""
    units: str = ""
    total_elements: int = 0
    building_name: str = ""
    storeys: int = 0
    elements_by_type: dict[str, int] = Field(default_factory=dict)


class ExportRequest(BaseModel):
    """Request to export IFC to another format."""
    file_id: str
    format: ExportFormat = Field(..., description="Target export format")


class BlenderRenderRequest(BaseModel):
    """Request to render IFC with Blender."""
    file_id: Optional[str] = None
    resolution_x: int = 1920
    resolution_y: int = 1080
    samples: int = Field(default=256, ge=1, le=4096)


class ExportResponse(BaseModel):
    """Response after exporting to another format."""

    file_id: str
    format: str
    output_url: Optional[str] = None
    error: Optional[str] = None


class BlenderRenderResponse(BaseModel):
    """Response after Blender render."""

    file_id: str
    resolution: tuple[int, int]
    samples: int
    output_url: Optional[str] = None
    error: Optional[str] = None


class BatchItemResult(BaseModel):
    """Result for a single IFC file extracted from a ZIP."""
    file_id: str
    original_filename: str
    content_type: str = "application/ifc"
    file_size: int
    file_key: str
    uploaded_at: datetime
    status: str = "DONE"
    preview_url: Optional[str] = None
    export_url: Optional[str] = None
    blender_render_url: Optional[str] = None
    metadata_url: Optional[str] = None


class BatchUploadResponse(BaseModel):
    """Response after uploading a ZIP containing multiple IFC files."""
    total_files: int
    success_count: int
    failed_count: int
    files: list[BatchItemResult]
    preview_urls: list[str] = []
    export_urls: list[str] = []
    blender_render_urls: list[str] = []
    metadata_urls: list[str] = []
    combined_preview_url: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response with dependency status."""
    status: str
    ifcopenshell_available: bool
    blender_available: bool
    document_service_available: bool
"""Core IFC processing engine using ifcopenshell.
Handles: IFC parsing, preview, export, and Blender rendering.
File I/O is delegated to the document-service via DocumentClient.
"""

from __future__ import annotations

import io, logging, math, os, subprocess, tempfile, time, uuid, zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
import ifcopenshell, ifcopenshell.geom, numpy as np, trimesh
from PIL import Image, ImageDraw
from src.config import BLENDER_PATH, MAX_UPLOAD_SIZE, PREVIEW_SIZE
from src.document_client import DocumentClient
from src.models import ExportFormat, IFCMetadata
logger = logging.getLogger(__name__)


def _ensure_ifc_import():
    try:
        import ifcopenshell  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_blender():
    return os.path.isfile(BLENDER_PATH) and os.access(BLENDER_PATH, os.X_OK)


def upload_ifc(file: BinaryIO, original_filename: str,
               content_type: str = "application/ifc") -> dict:
    """Upload an IFC file to document-service."""
    raw = file.read()
    if len(raw) == 0:
        raise ValueError("Uploaded file is empty")
    if len(raw) > MAX_UPLOAD_SIZE:
        raise ValueError(f"File size ({len(raw)} bytes) exceeds limit")
    file_id = str(uuid.uuid4())
    original_name = Path(original_filename).name
    stem = Path(original_name).stem or f"ifc_{file_id}"
    if not original_name.lower().endswith(".ifc"):
        original_name = f"{stem}.ifc"
    from src.config import DOCUMENT_SERVICE_URL
    client = DocumentClient(DOCUMENT_SERVICE_URL)
    try:
        file_key = f"ifc/{stem}/{file_id}/{original_name}"
        surl, okey = client.presign_upload(
            file_key=file_key, content_type=content_type)
        client.upload_to_presigned_url(
            url=surl, data=raw, content_type=content_type)
    finally:
        client.close()
    uploaded_at = datetime.now(timezone.utc)
    return {
        "file_id": file_id, "original_filename": original_name,
        "stem": stem, "content_type": content_type,
        "file_size": len(raw), "file_key": okey,
        "object_key": okey, "uploaded_at": uploaded_at.isoformat(),
        "status": "DONE",
        "preview_url": f"/api/v1/ifc/{file_id}/preview",
        "export_url": f"/api/v1/ifc/{file_id}/export",
        "blender_render_url": f"/api/v1/ifc/{file_id}/blender-render",
        "metadata_url": f"/api/v1/ifc/{file_id}/metadata",
    }


def upload_ifc_zip(
    zip_data: bytes,
    project_id: str = "",
    kind: str = "IFC",
) -> list[dict]:
    """Extract all .ifc files from a ZIP and upload each to document-service.

    Parameters
    ----------
    zip_data : bytes
        Raw bytes of the ZIP archive.
    project_id : str
        Project ID to associate with all uploaded files.
    kind : str
        Document kind (default ``"IFC"``).

    Returns
    -------
    list[dict]
        One result dict per successfully uploaded IFC file.

    Raises
    ------
    ValueError
        If the ZIP is empty or contains no ``.ifc`` files.
    """
    if len(zip_data) == 0:
        raise ValueError("Uploaded ZIP file is empty")

    from src.config import DOCUMENT_SERVICE_URL

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid ZIP file: {exc}") from exc

    # Collect .ifc entries (skip directories)
    ifc_entries = [
        n for n in zf.namelist()
        if n.lower().endswith(".ifc") and not n.endswith("/")
    ]
    if not ifc_entries:
        raise ValueError("No .ifc files found in ZIP")

    results: list[dict] = []
    client = DocumentClient(DOCUMENT_SERVICE_URL)
    try:
        for entry_name in ifc_entries:
            ifc_bytes = zf.read(entry_name)
            if len(ifc_bytes) == 0:
                logger.warning("Skipping empty IFC entry: %s", entry_name)
                continue
            if len(ifc_bytes) > MAX_UPLOAD_SIZE:
                logger.warning(
                    "Skipping oversized IFC entry (%d bytes): %s",
                    len(ifc_bytes), entry_name,
                )
                continue

            original_name = Path(entry_name).name
            stem = Path(original_name).stem or f"ifc_{uuid.uuid4().hex[:8]}"
            file_id = str(uuid.uuid4())
            file_key = f"ifc/{stem}/{file_id}/{original_name}"

            surl, okey = client.presign_upload(
                file_key=file_key,
                content_type="application/ifc",
                project_id=project_id,
                kind=kind,
                file_name=original_name,
            )
            client.upload_to_presigned_url(
                url=surl, data=ifc_bytes, content_type="application/ifc",
            )

            uploaded_at = datetime.now(timezone.utc)
            results.append({
                "file_id": file_id,
                "original_filename": original_name,
                "content_type": "application/ifc",
                "file_size": len(ifc_bytes),
                "file_key": okey,
                "uploaded_at": uploaded_at.isoformat(),
                "status": "DONE",
                "preview_url": f"/api/v1/ifc/{file_id}/preview",
                "export_url": f"/api/v1/ifc/{file_id}/export",
                "blender_render_url": f"/api/v1/ifc/{file_id}/blender-render",
                "metadata_url": f"/api/v1/ifc/{file_id}/metadata",
            })
    finally:
        client.close()

    return results


# test include: '"IfcWall", "IfcSlab", "IfcStair", "IfcRoof", "IfcColumn", "IfcBeam", "IfcWindow", "IfcDoor"'
def _open_ifc(ifc_bytes: bytes):
    """Parse raw IFC-SPF bytes into an ifcopenshell.file.

    ifcopenshell.open() only accepts a filesystem path (no file-like/bytes
    argument), and the returned ifcopenshell.file has no context-manager
    support, so files must be decoded to text and loaded via from_string().
    """
    ifc_text = ifc_bytes.decode("ISO-8859-1")
    return ifcopenshell.file.from_string(ifc_text)


def get_metadata(ifc_bytes: bytes) -> IFCMetadata:
    """Parse IFC bytes and extract structured metadata."""
    logger.info("Extracting metadata (%d bytes)", len(ifc_bytes))
    try:
        ifc_file = _open_ifc(ifc_bytes)
        proj = (ifc_file.by_type("IfcProject") or [None])[0]
        name = proj.Name or "" if proj else ""
        desc = proj.Description or "" if proj else ""
        # Count only IFC product types in metadata
        product_types = {
            "IfcWall", "IfcSlab", "IfcBeam", "IfcColumn",
            "IfcStair", "IfcRoof", "IfcDoor", "IfcWindow",
            "IfcCurtainWall", "IfcRailing", "IfcBuildingElementProxy",
        }
        counts: dict[str, int] = defaultdict(int)
        for ent in ifc_file.by_type("IfcProduct"):
            if ent.is_a() in product_types:
                counts[ent.is_a()] += 1
        top = dict(sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10])
        return IFCMetadata(
            file_version=ifc_file.schema, name=name, description=desc,
            units=getattr(ifc_file.units, "name", "METER"),
            total_elements=sum(counts.values()), building_name=name,
            storeys=len(ifc_file.by_type("IfcBuildingStorey")),
            elements_by_type=top)
    except Exception:
        logger.exception("Metadata extraction failed")
        raise


_GEOM_INCLUDE_TYPES = [
    "IfcWall", "IfcSlab", "IfcStair", "IfcRoof",
    "IfcColumn", "IfcBeam", "IfcWindow", "IfcDoor"]

# Distinct, recognizable colors per IFC element type
_TYPE_COLORS: dict[str, tuple[int, int, int]] = {
    "IfcWall":      (105, 125, 155),   # slate blue    – walls
    "IfcSlab":      (135, 125, 110),   # warm gray     – floors/roof
    "IfcStair":     (210, 165,  50),   # amber         – stairs
    "IfcRoof":      (155, 115, 100),   # terracotta    – roof
    "IfcColumn":    ( 85,  95, 130),   # steel blue    – columns
    "IfcBeam":      (100, 120, 145),   # blue-gray     – beams
    "IfcWindow":    (125, 195, 215),   # sky blue      – windows
    "IfcDoor":      (185, 130,  55),   # warm amber    – doors
}


def _iter_meshes(ifc_file) -> dict[str, list[trimesh.Trimesh]]:
    """Iterate building-element geometry grouped by IFC type.

    Returns a dict mapping IFC type -> list of trimeshes.
    """
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    meshes: dict[str, list[trimesh.Trimesh]] = defaultdict(list)
    for shape in ifcopenshell.geom.iterate(
            settings, ifc_file, include=_GEOM_INCLUDE_TYPES):
        # shape.type returns the IFC element type string (e.g. "IfcWall")
        # shape.product returns the entity_instance if available
        elem_type = getattr(shape, "type", None) or getattr(shape, "name", "Unknown")
        geometry = shape.geometry
        verts = np.array(geometry.verts, dtype=float).reshape(-1, 3)
        faces = np.array(geometry.faces, dtype=int).reshape(-1, 3)
        if len(verts) == 0 or len(faces) == 0:
            continue
        meshes[elem_type].append(trimesh.Trimesh(
            vertices=verts, faces=faces, process=False))
    return dict(meshes)


def generate_preview(ifc_bytes: bytes,
                     target_size: tuple[int, int] | None = None) -> bytes:
    """Generate a 2D preview PNG from IFC geometry with multiple views.

    Returns a 2x2 grid showing Top, Front, Side, and Perspective views.
    """
    target_size = target_size or PREVIEW_SIZE
    try:
        ifc_file = _open_ifc(ifc_bytes)
        type_meshes = _iter_meshes(ifc_file)
        if not type_meshes:
            return _placeholder_preview(target_size)
        return _render_multi_view_preview(type_meshes, _TYPE_COLORS, target_size)
    except Exception:
        logger.exception("Preview failed")
        return _placeholder_preview(target_size)


def generate_zip_preview(zip_data: bytes,
                         target_size: tuple[int, int] | None = None,
                         cells_per_row: int = 4) -> bytes:
    """Generate a combined preview grid for all IFC files in a ZIP.

    Each file is shown as a 2x2 multi-view preview panel, tiled in a grid.

    Parameters
    ----------
    zip_data : bytes
        Raw ZIP archive bytes.
    target_size : tuple or None
        Size per-cell (width, height). Default 400x300.
    cells_per_row : int
        Number of file panels per row.

    Returns
    -------
    bytes
        Combined PNG image with one multi-view panel per IFC file.
    """
    cell_w, cell_h = (400, 300) if target_size is None else target_size

    # Render each file's 2x2 multi-view preview into a PIL Image
    preview_images: list[tuple[str, Image.Image | None]] = []
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
        ifc_entries = [
            n for n in zf.namelist()
            if n.lower().endswith(".ifc") and not n.endswith("/")
        ]
        for entry in ifc_entries:
            try:
                ifc_bytes = zf.read(entry)
                preview_bytes = generate_preview(ifc_bytes, target_size=(cell_w, cell_h))
                pimg = Image.open(io.BytesIO(preview_bytes))
                preview_images.append((Path(entry).name, pimg))
            except Exception:
                logger.exception("Preview failed for ZIP entry: %s", entry)
                preview_images.append((Path(entry).name, None))

    if not preview_images:
        return _placeholder_preview((cell_w, cell_h))

    n = len(preview_images)
    cols = min(cells_per_row, n)
    rows = math.ceil(n / cols)

    # Add 2 px height for a label row
    label_h = 20
    total_w = cols * cell_w
    total_h = rows * (cell_h + label_h) + 8  # 8 px outer padding

    bg = (245, 245, 248)
    img = Image.new("RGB", (total_w, total_h), color=bg)
    draw = ImageDraw.Draw(img)

    for idx, (name, pimg) in enumerate(preview_images):
        col = idx % cols
        row = idx // cols
        x_off = 4 + col * cell_w
        y_off = 4 + row * (cell_h + label_h)

        # Draw panel background with subtle border
        draw.rectangle(
            [x_off, y_off, x_off + cell_w - 1, y_off + cell_h + label_h - 1],
            fill=(250, 250, 252), outline=(200, 200, 210),
        )

        # Paste the preview image (scaled to fit the cell width)
        if pimg is not None:
            ratio = cell_w / pimg.width
            new_h = int(pimg.height * ratio)
            resized = pimg.resize((cell_w, new_h), Image.LANCZOS)
            y_shift = y_off + ((cell_h + label_h) - new_h) // 2
            img.paste(resized, (x_off, y_shift))
        else:
            # Placeholder cell
            ph_img = _placeholder_preview((cell_w, cell_h))
            ph_resized = Image.open(io.BytesIO(ph_img)).resize(
                (cell_w, cell_h), Image.LANCZOS)
            img.paste(ph_resized, (x_off, y_off + label_h))

        # Draw filename label
        label = name if len(name) <= 35 else name[:32] + "..."
        draw.text((x_off + 4, y_off + 2), label, fill=(90, 90, 100))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_single_view_3d(
    mesh: trimesh.Trimesh,
    element_types: list[str],
    element_meshes: list[trimesh.Trimesh],
    camera_elevation: float,
    camera_azimuth: float,
    resolution_x: int,
    resolution_y: int,
    label: str | None = None,
) -> bytes:
    """Render a single 3D view with depth-sorted painter's algorithm + lighting."""
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    rotated = _rotate_3d(vertices, camera_elevation, camera_azimuth)
    screen_x, screen_y, vertex_z = _project_oblique(
        rotated, resolution_x, resolution_y, 0.4, math.radians(35),
    )

    # Build per-face color map from element type info
    face_colors: list[tuple[int, int, int]] = []
    mi = 0
    for orig_mesh in element_meshes:
        n_faces = len(orig_mesh.faces)
        elem_type = element_types[mi]
        face_color = _TYPE_COLORS.get(elem_type, (140, 140, 150))
        face_colors.extend([face_color] * n_faces)
        mi += 1

    # Depth-sort faces (painter's algorithm)
    faces = mesh.faces
    face_normals = mesh.face_normals
    projected: list[tuple[float, float, list[tuple[float, float]], int]] = []
    for fi in range(len(faces)):
        f = faces[fi]
        v0, v1, v2 = int(f[0]), int(f[1]), int(f[2])
        pts = [
            (screen_x[v0], screen_y[v0]),
            (screen_x[v1], screen_y[v1]),
            (screen_x[v2], screen_y[v2]),
        ]
        avg_z = (vertex_z[v0] + vertex_z[v1] + vertex_z[v2]) / 3.0
        n = face_normals[fi]
        n_cam_z = float(np.dot(n, np.array([0.0, -1.0, 0.0])))
        projected.append((avg_z, n_cam_z, pts, fi))

    projected.sort(key=lambda x: x[0], reverse=True)

    ambient = 0.35
    base_palette = [
        (155, 160, 175), (170, 165, 155), (145, 165, 160),
        (165, 155, 170), (175, 170, 155), (170, 155, 155),
        (155, 175, 155), (155, 155, 175), (168, 168, 158),
        (160, 155, 170),
    ]

    img = Image.new("RGB", (resolution_x, resolution_y), color=(248, 248, 252))
    draw = ImageDraw.Draw(img)

    for tri_z, n_cam_z, screen_pts, fi in projected:
        base_color = (face_colors[fi]
                      if fi < len(face_colors)
                      else base_palette[fi % len(base_palette)])
        brightness = ambient + (1.0 - ambient) * max(0.0, n_cam_z)
        color = tuple(int(c * brightness) for c in base_color)
        pts = [(int(p[0]), int(p[1])) for p in screen_pts]
        all_out = all(
            p[0] < -200 or p[0] > resolution_x + 200 or
            p[1] < -200 or p[1] > resolution_y + 200
            for p in pts
        )
        if all_out:
            continue
        draw.polygon(pts, fill=color,
                     outline=tuple(min(c + 10, 255) for c in color))

    if label:
        draw.text((8, 8), label, fill=(100, 100, 110))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_multi_view_preview(
    type_meshes: dict[str, list[trimesh.Trimesh]],
    type_colors: dict[str, tuple[int, int, int]],
    target_size: tuple[int, int],
) -> bytes:
    """Render a 2x2 multi-view preview with depth sorting, lighting, and AA.

    Each panel is rendered at 2× resolution then downscaled with LANCZOS
    for implicit anti-aliasing.  Faces are depth-sorted via painter's
    algorithm and shaded with simple normal-based lighting.
    """
    meshes: list[trimesh.Trimesh] = []
    mesh_types: list[str] = []
    for elem_type, ml in type_meshes.items():
        for m in ml:
            meshes.append(m)
            mesh_types.append(elem_type)
    if not meshes:
        return _placeholder_preview(target_size)

    mesh = trimesh.util.concatenate(meshes)

    # Camera presets for 4 canonical views
    views = [
        ("Top",             0.0,   0.0),
        ("Front",           30.0,  0.0),
        ("Side",            30.0,  90.0),
        ("Perspective",     30.0,  45.0),
    ]

    # Render each panel at 2× for anti-aliasing, then downscale
    aa_scale = 2
    panel_aa_w = target_size[0] // 2 * aa_scale
    panel_aa_h = target_size[1] // 2 * aa_scale
    panel_h_w = target_size[0] // 2
    panel_h_h = target_size[1] // 2

    view_imgs: list[tuple[str, Image.Image]] = []
    for view_name, elev, az in views:
        render_bytes = _render_single_view_3d(
            mesh, mesh_types, meshes, elev, az,
            panel_aa_w, panel_aa_h, label=view_name,
        )
        pil_img = Image.open(io.BytesIO(render_bytes))
        pil_img = pil_img.resize((panel_h_w, panel_h_h), Image.LANCZOS)
        view_imgs.append((view_name, pil_img))

    # Composite 2×2 grid
    panel_w, panel_h = panel_h_w, panel_h_h
    total_w = panel_w * 2
    total_h = panel_h * 2
    img = Image.new("RGB", (total_w, total_h), color=(245, 245, 248))
    draw = ImageDraw.Draw(img)

    # Panel backgrounds + borders
    for idx in range(4):
        col = idx % 2
        row = idx // 2
        x_off = col * panel_w
        y_off = row * panel_h
        draw.rectangle(
            [x_off, y_off, x_off + panel_w - 1, y_off + panel_h - 1],
            fill=(250, 250, 252), outline=(200, 200, 210),
        )

    # Paste rendered panels
    for idx, (_, pil_img) in enumerate(view_imgs):
        col = idx % 2
        row = idx // 2
        img.paste(pil_img, (col * panel_w, row * panel_h))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_preview_trimesh(mesh: trimesh.Trimesh, target_size: tuple[int, int]) -> bytes:
    """Render a 2D orthographic top-down preview using trimesh + PIL."""
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        return _placeholder_preview(target_size)
    w, h = target_size
    bbox = mesh.bounds
    x_min, y_min, z_min = bbox[0]
    x_max, y_max, z_max = bbox[1]
    x_range = max(x_max - x_min, 0.01)
    y_range = max(y_max - y_min, 0.01)
    padding = 0.15
    scaleX = w / (x_range * (1 + 2 * padding))
    scaleY = h / (y_range * (1 + 2 * padding))
    scale = min(scaleX, scaleY)
    cx = (x_min + x_max) / 2
    cy = (y_min + y_max) / 2

    def project_to_screen(vx: float, vy: float) -> tuple[int, int]:
        sx = (vx - cx) * scale + w / 2
        sy = h - ((vy - cy) * scale + h / 2)
        return (int(sx), int(sy))

    base_colors = [
        (170, 175, 185), (185, 180, 170), (165, 180, 175),
        (175, 170, 180), (180, 178, 172), (190, 170, 170),
        (170, 190, 170), (170, 170, 190),
    ]

    img = Image.new("RGB", (w, h), color=(245, 245, 248))
    draw = ImageDraw.Draw(img)

    for fi, face in enumerate(mesh.faces):
        verts = mesh.vertices[face]
        pts = [project_to_screen(v[0], v[1]) for v in verts]
        if len(set(pts)) < 3:
            continue
        color = base_colors[fi % len(base_colors)]
        draw.polygon(pts, fill=color, outline=(120, 125, 140))

    edges_drawn = set()
    for edge in mesh.edges_unique:
        v1, v2 = mesh.vertices[edge]
        p1 = project_to_screen(v1[0], v1[1])
        p2 = project_to_screen(v2[0], v2[1])
        edge_key = (min(p1, p2), max(p1, p2))
        if edge_key not in edges_drawn and p1 != p2:
            edges_drawn.add(edge_key)
            draw.line([p1, p2], fill=(80, 85, 100), width=2)

    grid_spacing = max(w, h) / 20
    grid_color = (225, 225, 230)
    for gx in range(0, w, int(grid_spacing)):
        draw.line([(gx, 0), (gx, h)], fill=grid_color, width=1)
    for gy in range(0, h, int(grid_spacing)):
        draw.line([(0, gy), (w, gy)], fill=grid_color, width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _placeholder_preview(size: tuple[int, int]) -> bytes:
    img = Image.new("RGB", size, color="#f0f0f0")
    draw = ImageDraw.Draw(img)
    draw.text((size[0]//2 - 40, size[1]//2 - 10),
              "No geometry", fill="#999999")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def export_3d_model(ifc_bytes: bytes, fmt: ExportFormat) -> bytes:
    """Export IFC geometry to a 3D format via trimesh."""
    logger.info("Exporting IFC -> %s (%d bytes)", fmt.value, len(ifc_bytes))
    try:
        ifc_file = _open_ifc(ifc_bytes)
        type_meshes = _iter_meshes(ifc_file)
        if not type_meshes:
            raise ValueError("No geometry found for export")
        av, af, fo = [], [], 0
        for ml in type_meshes.values():
            for m in ml:
                av.append(m.vertices)
                af.append(m.faces + fo)
                fo += len(m.vertices)
        mesh = trimesh.Trimesh(
            vertices=np.vstack(av),
            faces=np.vstack(af) if af else np.array([]),
            process=False)
        buf = io.BytesIO()
        if fmt == ExportFormat.OBJ:
            mesh.export(buf, file_type="obj")
        elif fmt == ExportFormat.STL:
            mesh.export(buf, file_type="stl")
        elif fmt == ExportFormat.GLTF:
            mesh.export(buf, file_type="gltf")
        elif fmt == ExportFormat.GLB:
            mesh.export(buf, file_type="glb")
        buf.seek(0)
        return buf.read()
    except Exception:
        logger.exception("3D export failed")
        raise


def _rotate_3d(
    vertices: np.ndarray,
    camera_elevation: float = 30.0,
    camera_azimuth: float = 45.0,
) -> np.ndarray:
    """Rotate 3D vertices for a given camera orientation.

    Parameters are in degrees:
        camera_elevation:  angle above the horizontal plane (0 = top-down, 90 = front)
        camera_azimuth:    angle in the horizontal plane (0 = facing +X, 90 = facing +Y, etc.)
    Returns the rotated vertex array (same shape as input).
    """
    import math as _math

    elev_rad = _math.radians(camera_elevation)
    azim_rad = _math.radians(camera_azimuth)

    # Cosine of elevation for vertical scaling
    cos_elev = _math.cos(elev_rad)
    sin_elev = _math.sin(elev_rad)

    # Rotation matrix for azimuth around Z axis
    cos_azim = _math.cos(azim_rad)
    sin_azim = _math.sin(azim_rad)

    rotated = np.empty_like(vertices)

    for i in range(len(vertices)):
        x, y, z = vertices[i]

        # Azimuth rotation (around Z)
        x1 = x * cos_azim - y * sin_azim
        y1 = x * sin_azim + y * cos_azim
        z1 = z

        # Elevation rotation (around X) – lifts Y toward Z
        y2 = y1 * cos_elev - z1 * sin_elev
        z2 = y1 * sin_elev + z1 * cos_elev

        rotated[i] = (x1, y2, z2)

    return rotated


def _get_camera_angles(preset: str | None) -> tuple[float, float]:
    """Return (elevation, azimuth) for a named camera preset.

    Common presets:
        "isometric"  -> (30, 45)   standard isometric view
        "front"      -> (30, 0)    facing +X
        "back"       -> (30, 180)  facing −X
        "right"      -> (30, 90)   facing +Y
        "left"       -> (30, 270)  facing −Y
        "top"        -> (0, 0)     top-down
        "perspective"-> (30, 45)   default oblique view
        None         -> (30, 45)   default

    Raises ValueError for unknown presets.
    """
    presets = {
        "isometric":   (30.0, 45.0),
        "front":       (30.0, 0.0),
        "back":        (30.0, 180.0),
        "right":       (30.0, 90.0),
        "left":        (30.0, 270.0),
        "top":         (0.0, 0.0),
        "perspective": (30.0, 45.0),
        "low_angle":   (15.0, 45.0),
        "high_angle":  (60.0, 45.0),
    }
    if preset is None:
        return 30.0, 45.0
    preset = preset.lower().strip()
    if preset in presets:
        return presets[preset]
    raise ValueError(f"Unknown camera preset: {preset!r}. Available: {list(presets.keys())}")


def _project_oblique(
    rotated_vertices: np.ndarray,
    resolution_x: int,
    resolution_y: int,
    z_screen_scale: float,
    oblique_angle: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Project 3D vertices onto 2D screen using oblique projection.

    Returns (screen_x, screen_y, vertex_z) arrays.
    """
    import math as _math
    import numpy as np

    w, h = resolution_x, resolution_y
    bbox = np.array([rotated_vertices.min(axis=0), rotated_vertices.max(axis=0)])
    x_min, y_min, z_min = bbox[0]
    x_max, y_max, _ = bbox[1]

    cx = (x_min + x_max) / 2
    cy = (y_min + y_max) / 2

    pad = 0.15
    x_range = x_max - x_min
    y_range = y_max - y_min
    scaleX = (w * (1 - 2 * pad)) / max(x_range, 0.01)
    scaleY = (h * (1 - 2 * pad)) / max(y_range, 0.01)
    scale = min(scaleX, scaleY)

    cos_ang = _math.cos(oblique_angle)
    sin_ang = _math.sin(oblique_angle)

    screen_x = np.zeros(len(rotated_vertices))
    screen_y = np.zeros(len(rotated_vertices))
    vertex_z = np.zeros(len(rotated_vertices))

    for i in range(len(rotated_vertices)):
        v = rotated_vertices[i]
        sx = (v[0] - cx) * scale + w / 2.0
        sy = h - ((v[1] - cy) * scale + h / 2.0)
        z_off = (v[2] - z_min) * z_screen_scale
        screen_x[i] = sx + z_off * cos_ang
        screen_y[i] = sy + z_off * sin_ang
        vertex_z[i] = v[2]

    return screen_x, screen_y, vertex_z


def _render_3d_trimesh(
    mesh: trimesh.Trimesh,
    resolution_x: int = 1920,
    resolution_y: int = 1080,
    camera_elevation: float = 30.0,
    camera_azimuth: float = 45.0,
    element_types: list[str] | None = None,
    element_meshes: list[trimesh.Trimesh] | None = None,
) -> bytes:
    """Render 3D with oblique projection that fills the frame.

    Parameters:
        camera_elevation:  angle above horizontal plane in degrees (0=top-down, 90=front)
        camera_azimuth:    horizontal viewing angle in degrees (0=+X, 90=+Y, etc.)
        element_types:     IFC type string per original mesh (for per-type coloring)
        element_meshes:    original meshes corresponding to element_types
    """
    import math as _math
    import numpy as np

    w, h = resolution_x, resolution_y
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        return _placeholder_preview((w, h))

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    rotated = _rotate_3d(vertices, camera_elevation, camera_azimuth)

    z_screen_scale = 0.4
    oblique_angle = _math.radians(35)

    screen_x, screen_y, vertex_z = _project_oblique(
        rotated, w, h, z_screen_scale, oblique_angle
    )

    # Build per-face color map from element type info
    face_colors: list[tuple[int, int, int]] = []
    if element_types and element_meshes:
        # Build mapping from global face index to element type
        global_face_start = 0
        mi = 0
        for orig_mesh in element_meshes:
            n_faces = len(orig_mesh.faces)
            elem_type = element_types[mi]
            face_color = _TYPE_COLORS.get(elem_type, (140, 140, 150))
            for _ in range(n_faces):
                face_colors.append(face_color)
            global_face_start += n_faces
            mi += 1
    else:
        face_colors = []

    projected = []
    faces = mesh.faces
    face_normals = mesh.face_normals
    for fi in range(len(faces)):
        f = faces[fi]
        v0, v1, v2 = f[0], f[1], f[2]
        pts = [
            (screen_x[v0], screen_y[v0]),
            (screen_x[v1], screen_y[v1]),
            (screen_x[v2], screen_y[v2]),
        ]
        avg_z = (vertex_z[v0] + vertex_z[v1] + vertex_z[v2]) / 3.0
        n = face_normals[fi]
        n_cam_z = np.dot(n, np.array([0.0, -1.0, 0.0]))
        projected.append((avg_z, n_cam_z, pts, fi))

    projected.sort(key=lambda x: x[0], reverse=True)

    base_palette = [
        (155, 160, 175), (170, 165, 155), (145, 165, 160),
        (165, 155, 170), (175, 170, 155), (170, 155, 155),
        (155, 175, 155), (155, 155, 175), (168, 168, 158),
        (160, 155, 170),
    ]
    ambient = 0.35

    img = Image.new("RGB", (w, h), color=(248, 248, 252))
    draw = ImageDraw.Draw(img)

    for tri_z, n_cam_z, screen_pts, fi in projected:
        if face_colors:
            # Use per-element-type color with lighting
            base_color = face_colors[fi] if fi < len(face_colors) else base_palette[fi % len(base_palette)]
        else:
            # Fallback: cycle through palette
            base_color = base_palette[fi % len(base_palette)]
        brightness = ambient + (1.0 - ambient) * max(0.0, n_cam_z)
        color = tuple(int(c * brightness) for c in base_color)
        pts = [(int(p[0]), int(p[1])) for p in screen_pts]
        all_out = all(
            p[0] < -100 or p[0] > w + 100 or p[1] < -100 or p[1] > h + 100
            for p in pts
        )
        if all_out:
            continue
        draw.polygon(pts, fill=color, outline=tuple(min(c + 10, 255) for c in color))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_with_blender(
        ifc_bytes: bytes,
        resolution_x: int = 1920, resolution_y: int = 1080,
        samples: int = 256,
        camera_elevation: float = 30.0,
        camera_azimuth: float = 45.0,
) -> bytes:
    """Render the IFC model with Blender and return PNG bytes.

    Parameters:
        ifc_bytes:            IFC file content as bytes
        resolution_x/y:       output image dimensions
        samples:              ray-tracing samples (Blender Cycles only)
        camera_elevation:     elevation angle in degrees for trimesh fallback
        camera_azimuth:       azimuth angle in degrees for trimesh fallback
    """
    logger.info("Blender render: %dx%d, %d samples",
                resolution_x, resolution_y, samples)

    # Fast path: try trimesh first (no Blender dependency)
    try:
        ifc_file = _open_ifc(ifc_bytes)
        type_meshes = _iter_meshes(ifc_file)
        if type_meshes:
            # Flatten all meshes preserving type info for coloring
            all_meshes: list[trimesh.Trimesh] = []
            all_types: list[str] = []
            for elem_type, ml in type_meshes.items():
                for m in ml:
                    all_meshes.append(m)
                    all_types.append(elem_type)
            av = np.vstack([m.vertices for m in all_meshes])
            af_list = []
            fo = 0
            for m in all_meshes:
                af_list.append(m.faces + fo)
                fo += len(m.vertices)
            mesh = trimesh.Trimesh(
                vertices=av, faces=np.concatenate(af_list), process=False,
            )
            return _render_3d_trimesh(
                mesh, resolution_x, resolution_y,
                camera_elevation, camera_azimuth,
                all_types, all_meshes,
            )
    except Exception:
        logger.exception("trimesh 3D render failed, trying Blender")

    # Fallback: Blender Cycles
    if not _ensure_blender():
        raise RuntimeError(
            "Blender is not installed. Install Blender 4.x and set "
            "BLENDER_PATH in settings, or fix the trimesh renderer."
        )
    try:
        obj_data = export_3d_model(ifc_bytes, ExportFormat.OBJ)
        blend_dir = Path(tempfile.mkdtemp(prefix="ifc_blender_"))
        obj_path = blend_dir / "model.obj"
        obj_path.write_bytes(obj_data)
        bs = blend_dir / "render.py"
        out_png = blend_dir / "render.png"
        _write_blender_script(str(bs), str(obj_path),
                              str(out_png), resolution_x, resolution_y, samples)
        cmd = [BLENDER_PATH, "--background", "--python", str(bs),
               "--", str(obj_path), str(out_png),
               str(resolution_x), str(resolution_y), str(samples)]
        result = subprocess.run(cmd, capture_output=True,
                                text=True, timeout=600)
        if result.returncode != 0:
            logger.error("Blender failed: %s", result.stderr[-1000:])
            raise RuntimeError(f"Blender failed: {result.stderr[-1000:]}")
        if not out_png.exists():
            raise FileNotFoundError("Blender did not produce output")
        try:
            blend_dir.rmdir()
        except OSError:
            pass
        return out_png.read_bytes()
    except Exception:
        logger.exception("Blender render failed")
        raise
def _write_blender_script(script_path: str, obj_path: str,
                          output_png: str,
                          res_x: int, res_y: int, n_samples: int):
    """Write a Python script that Blender executes."""
    lines = []
    Q = chr(34)
    lines.append(Q*3 + "Blender render script." + Q*3)
    lines.append("import sys")
    lines.append("import bpy")
    lines.append("")
    lines.append("def main():")
    lines.append('    args = sys.argv[sys.argv.index("--") + 1:]')
    lines.append("    obj_path, output_png = args[0], args[1]")
    lines.append("    res_x, res_y = int(args[2]), int(args[3])")
    lines.append("    n_samples = int(args[4])")
    lines.append('    bpy.ops.object.select_all(action="SELECT")')
    lines.append("    bpy.ops.object.delete()")
    lines.append("    scene = bpy.context.scene")
    lines.append('    scene.render.image_format = "PNG"')
    lines.append("    scene.render.film_transparent = True")
    lines.append("    scene.render.resolution_x = res_x")
    lines.append("    scene.render.resolution_y = res_y")
    lines.append("    scene.render.resolution_percentage = 100")
    lines.append("    scene.render.filepath = output_png")
    lines.append("    scene.render.tile_x = 256")
    lines.append("    scene.render.tile_y = 256")
    lines.append('    scene.render.engine = "CYCLES"')
    lines.append("    scene.cycles.samples = n_samples")
    lines.append("    scene.cycles.preview_samples = min(n_samples, 64)")
    lines.append('    scene.cycles.device = "CPU"')
    lines.append("    scene.cycles.filter_width = 1.5")
    lines.append("    for nm, en, rot in [")
    lines.append('        ("KeyLight", 3.0, (0.8, 0.5, 0.0)),')
    lines.append('        ("FillLight", 1.0, (-0.5, 1.0, 3.0)),')
    lines.append('        ("SkyLight", 0.5, (3.0, 0.0, 0.0)),')
    lines.append("    ]:")
    lines.append('        ld = bpy.data.lights.new(nm, "SUN")')
    lines.append("        ld.energy = en")
    lines.append('        ld.angle = 0.5 if nm != "SkyLight" else 3.14159 * 0.48')
    lines.append("        lo = bpy.data.objects.new(nm, ld)")
    lines.append("        lo.rotation_euler = rot")
    lines.append("        scene.collection.objects.link(lo)")
    lines.append("    bpy.ops.import_scene.obj(filepath=obj_path)")
    lines.append('    meshes = [o for o in scene.collection.objects if o.type == "MESH"]')
    lines.append("    if meshes:")
    lines.append("        bpy.context.view_layer.objects.active = meshes[0]")
    lines.append("        meshes[0].select_set(True)")
    lines.append("        bpy.ops.view3d.camera_to_view_selected()")
    lines.append("        for o in meshes: o.select_set(False)")
    lines.append('    print(f"Rendering {res_x}x{res_y} at {n_samples} samples")')
    lines.append("    bpy.ops.render.render(write_studio=True, animation=False)")
    lines.append('    print(f"Saved: {output_png}")')
    lines.append("main()")
    Path(script_path).write_text(chr(10).join(lines) + chr(10))
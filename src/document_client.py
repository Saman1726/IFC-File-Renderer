"""HTTP client for the document-service API.

The document-service exposes three endpoints used by the IFC renderer:

  1. POST /api/v1/media/presign      – returns a presigned PUT URL + objectKey
  2. POST /api/v1/media/download/internal – server-side download, returns bytes
  3. POST /api/v1/media/presign-download – returns a presigned GET URL

All calls target the internal endpoint (e.g. http://document-service-java:8092)
so they never leave the Docker network.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class DocumentClient:
    """Thin wrapper around the document-service HTTP API."""

    def __init__(self, base_url: str, auth_token: Optional[str] = None,
                 tenant_id: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.tenant_id = tenant_id
        self._client: httpx.Client | None = None
        self._override_auth: Optional[str] = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            headers = {}
            auth = self._override_auth or self.auth_token
            if auth:
                headers["Authorization"] = f"Bearer {auth}"
            # document-service rejects every /api/** request (including the
            # internal server-to-server download route) without this header.
            headers["X-Tenant-Id"] = self.tenant_id or "00000000-0000-0000-0000-000000000001"
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    def with_auth(self, auth_token: str) -> "DocumentClient":
        """Return a new client that uses the given auth token (overrides constructor token)."""
        if self._client:
            self._client.close()
            self._client = None
        clone = DocumentClient(self.base_url, auth_token, self.tenant_id)
        return clone

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # 1. Presign PUT — get a URL and objectKey for storing a file
    # ------------------------------------------------------------------

    def presign_upload(
        self,
        file_key: str,
        content_type: str = "application/octet-stream",
        project_id: str = "",
        kind: str = "",
        file_name: str = "",
    ) -> tuple[str, str]:
        """Request a presigned PUT URL and the resulting objectKey.

        This method explicitly omits the Authorization header to avoid
        triggering the document service's OAuth2 JWT filter, which evaluates
        *before* the permitAll authorization rule.  An invalid/expired bearer
        token causes a 401 before the endpoint is ever reached.

        Parameters
        ----------
        file_key : str
            The storage path prefix (e.g. "renders/{file_id}/render.png").
        project_id : str
            The CERP project ID — required by the document-service DTO.
        kind : str
            Document kind enum value (e.g. "IFC", "RENDER", "EXPORT").
        file_name : str
            Human-readable file name for the document-service audit trail.

        Returns
        -------
        (server_upload_url, object_key)
        """
        internal = self.with_auth(None)
        try:
            response = internal.client.post("/api/v1/media/presign", json={
                "projectId": project_id,
                "kind": kind,
                "fileName": file_name,
                "contentType": content_type,
            })
            response.raise_for_status()
            data = response.json()
            return data["serverUploadUrl"], data["objectKey"]
        finally:
            internal.close()

    # ------------------------------------------------------------------
    # 2. Upload — PUT file bytes to a presigned URL
    # ------------------------------------------------------------------

    def upload_to_presigned_url(
        self, url: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        """PUT *data* to the given presigned PUT URL."""
        response = self.client.put(
            url,
            content=data,
            headers={"Content-Type": content_type},
            timeout=httpx.Timeout(300.0, connect=30.0),  # 5 min for large files
        )
        response.raise_for_status()
        logger.info(
            "Uploaded %d bytes → presigned URL (HTTP %d)", len(data), response.status_code
        )

    # ------------------------------------------------------------------
    # 3. Download — server-side download via internal endpoint
    # ------------------------------------------------------------------

    def download_internal(self, object_key: str) -> bytes:
        """Download file content via the internal download endpoint.

        This method explicitly omits the Authorization header to avoid
        triggering the document service's OAuth2 JWT filter, which evaluates
        *before* the permitAll authorization rule.  An invalid/expired bearer
        token causes a 401 before the endpoint is ever reached.
        """
        internal = self.with_auth(None)
        try:
            response = internal.client.post(
                "/api/v1/media/download/internal",
                json={"objectKey": object_key},
                timeout=httpx.Timeout(120.0, connect=10.0),
            )
            response.raise_for_status()
            return response.content
        finally:
            internal.close()

    # ------------------------------------------------------------------
    # 4. Presign GET — get a presigned download URL for a stored object
    # ------------------------------------------------------------------

    def presign_download(self, object_key: str) -> tuple[str, str]:
        """Return (presigned_get_url, expires_at) for an existing object.

        This method explicitly omits the Authorization header to avoid
        triggering the document service's OAuth2 JWT filter, which evaluates
        *before* the permitAll authorization rule.  An invalid/expired bearer
        token causes a 401 before the endpoint is ever reached.
        """
        internal = self.with_auth(None)
        try:
            response = internal.client.post(
                "/api/v1/media/presign-download",
                json={"objectKey": object_key},
            )
            response.raise_for_status()
            data = response.json()
            return data["url"], data["expiresAt"]
        finally:
            internal.close()

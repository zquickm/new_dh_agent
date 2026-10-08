"""Suspicion API integration; no model-facing formatting or frontend events."""
from typing import Optional

from config.settings import get_settings
from tool_backends.spatial_backend import SpatialServiceClient


class SuspicionBackend:
    def __init__(self, client: Optional[SpatialServiceClient] = None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            settings = get_settings().tools.spatial
            self._client = SpatialServiceClient(
                base_url=settings.base_url, api_key=settings.api_key.get_secret_value(),
                timeout=settings.timeout, connect_timeout=settings.connect_timeout,
                verify_ssl=settings.verify_ssl, max_retries=settings.max_retries,
                retry_backoff=settings.retry_backoff,
            )
        return self._client

    async def validate(self, path, payload):
        # Saving overwrites previous results; never automatically retry a validate POST.
        return await self.client._request("POST", f"/api/spatial/suspicion/{path}/validate", json=payload, retries=0)

    async def results(self, path, batch):
        return await self.client._request("GET", f"/api/spatial/suspicion/{path}/results",
                                          params={"batchNo": batch} if batch is not None else {}, data_type=list)

    async def close(self):
        if self._client is not None:
            await self._client.close()

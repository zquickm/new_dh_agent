"""Suspicion API integration; no model-facing formatting or frontend events."""
from typing import Optional

from config.settings import get_settings
from tool_backends.spatial_backend import SpatialServiceClient, SpatialServiceError


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
                                          params={"batchNo": batch}, data_type=list)

    async def inventory(self, batch):
        detail = await self.client.get_spatial_element_detail("suspicion_patch")
        fields = {f["name"] for f in detail.get("fields", [])}
        batch_field = next((f for f in ("batchNo", "batch_no") if f in fields), None)
        patch_field = next((f for f in ("patchNo", "patch_no") if f in fields), None)
        if not batch_field or not patch_field:
            raise SpatialServiceError("疑点图斑目录缺少批次或图斑编号字段")
        query = {"element": "suspicion_patch", "mode": "attribute", "includeGeometry": False,
                 "fields": [batch_field, patch_field], "limit": min(detail.get("maxQueryLimit", 100), 1000), "offset": 0}
        if query["limit"] < 1:
            raise SpatialServiceError("图斑目录分页上限无效")
        if batch is None:
            # Current batches (e.g. XM-2026-06) sort chronologically by batch number.
            latest = await self.client.query_spatial_element({**query, "limit": 1, "orderBy": batch_field, "orderDirection": "desc"})
            if not latest["features"]:
                raise SpatialServiceError("没有疑点图斑批次")
            batch = latest["features"][0]["properties"].get(batch_field)
            if not isinstance(batch, str) or not batch:
                raise SpatialServiceError("批次号无效")
        query["conditions"] = [{"field": batch_field, "operator": "=", "value": batch}]
        patches = set()
        while True:
            page = await self.client.query_spatial_element(query)
            total = page.get("total")
            if type(total) is not int or total < 0:
                raise SpatialServiceError("图斑清单缺少有效总数")
            for feature in page["features"]:
                key = feature["properties"].get(patch_field)
                if not isinstance(key, str) or not key or key in patches:
                    raise SpatialServiceError("图斑编号无效或分页重复")
                patches.add(key)
            if len(patches) == total:
                break
            if not page["features"] or len(patches) > total:
                raise SpatialServiceError("图斑清单分页不完整")
            query["offset"] += len(page["features"])
        if not patches:
            raise SpatialServiceError("批次不存在或未匹配到图斑")
        return batch, patches

    async def close(self):
        if self._client is not None:
            await self._client.close()

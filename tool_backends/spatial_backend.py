from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp


logger = logging.getLogger(__name__)


class SpatialServiceError(RuntimeError):
    """Raised when the spatial service cannot provide a valid business result."""


class SpatialServiceClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float,
        connect_timeout: float,
        verify_ssl: bool,
        max_retries: int = 0,
        retry_backoff: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.verify_ssl = verify_ssl
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    async def list_station_types(self) -> dict[str, Any]:
        return await self._request("GET", "/api/spatial/stations/types")

    async def query_stations(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/spatial/stations/query",
            json=payload,
        )

    async def query_station_status(self, tznos: list[str]) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/spatial/stations/status",
            json={"tznos": tznos},
        )

    async def get_station_detail(self, tzno: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/spatial/stations/info",
            params={"tzno": tzno},
        )

    async def get_observation_summary(self, tzno: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/spatial/stations/data",
            params={"tzno": tzno},
        )

    async def get_element_trend(
        self,
        tzno: str,
        element: str,
        hours: int,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/spatial/stations/data/trend",
            params={"tzno": tzno, "element": element, "hours": hours},
        )

    async def get_observation_range(
        self,
        *,
        tzno: str,
        data_type: str,
        start_time: str,
        end_time: str,
        order_by: str,
        max_records: int,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/spatial/stations/observation-range",
            params={
                "tzno": tzno,
                "data_type": data_type,
                "start_time": start_time,
                "end_time": end_time,
                "order_by": order_by,
                "max_records": max_records,
            },
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.base_url:
            raise SpatialServiceError("空间服务地址未配置")
        if not self.api_key:
            raise SpatialServiceError("空间服务 API Key 未配置")

        body: Any = None
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            retryable_error: Optional[BaseException] = None
            try:
                session = await self._get_session()
                request_timeout = aiohttp.ClientTimeout(
                    total=self.timeout,
                    connect=min(self.timeout, self.connect_timeout),
                )
                async with session.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    params=params,
                    headers={"X-API-Key": self.api_key},
                    ssl=self.verify_ssl,
                    timeout=request_timeout,
                ) as response:
                    if not 200 <= response.status < 300:
                        error = SpatialServiceError(
                            f"空间服务返回 HTTP {response.status}"
                        )
                        if response.status == 429 or response.status >= 500:
                            retryable_error = error
                        else:
                            raise error
                    else:
                        try:
                            body = await response.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError) as exc:
                            raise SpatialServiceError(
                                "空间服务返回了无效 JSON"
                            ) from exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                retryable_error = exc

            if retryable_error is None:
                break
            if attempt + 1 >= attempts:
                if isinstance(retryable_error, SpatialServiceError):
                    raise retryable_error
                raise SpatialServiceError("空间服务请求失败") from retryable_error

            delay = self.retry_backoff * (2**attempt)
            logger.warning(
                "Spatial service attempt %s/%s failed; retrying in %.2fs: "
                "method=%s path=%s error_type=%s",
                attempt + 1,
                attempts,
                delay,
                method,
                path,
                type(retryable_error).__name__,
            )
            if delay:
                await asyncio.sleep(delay)

        if not isinstance(body, dict):
            raise SpatialServiceError("空间服务响应结构无效")
        code = body.get("code")
        if code not in (0, 200):
            message = str(body.get("message") or "空间服务业务处理失败")
            raise SpatialServiceError(message[:200])
        data = body.get("data")
        if not isinstance(data, dict):
            raise SpatialServiceError("空间服务响应缺少 data")
        return data

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
        return self._session

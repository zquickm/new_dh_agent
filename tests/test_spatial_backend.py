import unittest

from aiohttp import web

from agents.tools.spatial_query_service import SpatialQueryServiceTool
from tool_backends.spatial_backend import (
    SpatialServiceClient,
    SpatialServiceError,
)


class SpatialServiceClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.overview_status = 200
        self.overview_code = 0
        self.overview_data = {
            "/api/spatial/islands/overview": {
                "total": 123, "byProvince": [{"province": "浙江省", "count": 123}],
                "byClass": [{"islandClass": "无居民海岛", "count": 123}],
                "items": [{"id": 1, "stdName": "测试岛", "areaM2": 9000}],
                "page": 2, "size": 1,
            },
            "/api/spatial/v2/duanmian/overview": {
                "dataSource": "测试观测数据源",
                "overview": {"sectionCount": 3, "stationCount": 52, "elementCount": 30,
                             "dataCount": 12345, "latestObservedAt": "2025-11-01"},
                "sections": [{"section": "E1", "stationCount": 14, "dataCount": 123}],
                "chemistryElements": ["温度", "盐度"], "hydrometElements": ["海况"],
            },
        }

        async def overview(request):
            self.requests.append(request)
            return web.json_response({
                "code": self.overview_code, "message": "统计服务结果",
                "data": self.overview_data[request.path],
            }, status=self.overview_status)

        async def spatial_elements(request):
            self.requests.append(request)
            return web.json_response(
                {
                    "code": 0,
                    "message": "success",
                    "data": [{"dataKey": "obs_station", "dataName": "观测站点"}],
                }
            )

        async def business_error(_request):
            return web.json_response(
                {"code": 400, "message": "参数错误", "data": None}
            )

        app = web.Application()
        app.router.add_get("/api/spatial/v2/elements/list", spatial_elements)
        app.router.add_get("/business-error", business_error)
        for path in self.overview_data:
            app.router.add_get(path, overview)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        socket = self.site._server.sockets[0]
        self.base_url = f"http://127.0.0.1:{socket.getsockname()[1]}"
        self.client = SpatialServiceClient(
            base_url=self.base_url,
            api_key="test-spatial-key",
            timeout=2,
            connect_timeout=1,
            verify_ssl=True,
        )

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def test_calls_service_with_api_key_and_unwraps_data(self):
        data = await self.client.list_spatial_elements()

        self.assertEqual(data, [{"dataKey": "obs_station", "dataName": "观测站点"}])
        self.assertEqual(
            self.requests[0].headers["X-API-Key"],
            "test-spatial-key",
        )

    async def test_rejects_non_success_business_code(self):
        with self.assertRaisesRegex(SpatialServiceError, "参数错误"):
            await self.client._request("GET", "/business-error")

    async def test_overview_tools_use_frontend_endpoints_and_preserve_statistics(self):
        tool = SpatialQueryServiceTool(self.client)
        filters = {"province": "浙江省", "city": "舟山市", "islandClass": "无居民海岛",
                   "keyword": "舟山", "page": 2, "size": 1,
                   "minLng": 121.0, "minLat": 29.0, "maxLng": 123.0, "maxLat": 31.0}
        for operation, arguments, path in (
            ("island_overview", {}, "/api/spatial/islands/overview"),
            ("island_overview", filters, "/api/spatial/islands/overview"),
            ("dm_duanmian_overview", {}, "/api/spatial/v2/duanmian/overview"),
        ):
            with self.subTest(operation=operation, arguments=arguments):
                before = len(self.requests)
                output = await tool.execute(operation, **arguments)
                self.assertEqual(len(self.requests), before + 1)
                request = self.requests[-1]
                self.assertEqual((request.method, request.path), ("GET", path))
                self.assertEqual(dict(request.query), {key: str(value) for key, value in arguments.items()})
                self.assertEqual(request.headers["X-API-Key"], "test-spatial-key")
                self.assertEqual(output.content, {"operation": operation, **self.overview_data[path]})
                self.assertEqual(output.client_events, ())

        # A successful zero is preserved; an absent statistic is never filled in.
        for data in ({"total": 0, "items": []}, {}):
            self.overview_data["/api/spatial/islands/overview"] = data
            output = await tool.execute("island_overview")
            self.assertEqual(output.content, {"operation": "island_overview", **data})

    async def test_overview_validation_and_failures_do_not_return_fallback_statistics(self):
        tool = SpatialQueryServiceTool(self.client)
        for operation, arguments in (
            ("dm_duanmian_overview", {"province": "浙江省"}),
            ("dm_duanmian_overview", {"section": "E1"}),
            *[("island_overview", args) for args in (
                {"page": 0}, {"page": 1.5}, {"size": 101}, {"size": True},
                {"minLng": 121}, {"keyword": " "},
                {"minLng": 123, "minLat": 29, "maxLng": 121, "maxLat": 31},
                {"minLng": 121, "minLat": 32, "maxLng": 123, "maxLat": 31},
                {"minLng": float("nan"), "minLat": 29, "maxLng": 123, "maxLat": 31},
                {"minLng": True, "minLat": 29, "maxLng": 123, "maxLat": 31},
                {"minLng": 181, "minLat": 29, "maxLng": 183, "maxLat": 31},
            )],
        ):
            with self.subTest(operation=operation, arguments=arguments), self.assertRaises(ValueError):
                await tool.execute(operation, **arguments)
        self.assertEqual(self.requests, [])
        for operation in ("island_overview", "dm_duanmian_overview"):
            for status, code in ((401, 0), (500, 0), (200, 500)):
                self.overview_status, self.overview_code = status, code
                with self.subTest(operation=operation, status=status, code=code), self.assertRaises(SpatialServiceError):
                    await tool.execute(operation)

    async def test_retries_transient_http_error_once(self):
        attempts = 0

        async def transient(_request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return web.Response(status=503, text="busy")
            return web.json_response(
                {"code": 0, "message": "success", "data": {"total": 0}}
            )

        app = web.Application()
        app.router.add_get("/transient", transient)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        socket = site._server.sockets[0]
        client = SpatialServiceClient(
            base_url=f"http://127.0.0.1:{socket.getsockname()[1]}",
            api_key="test-spatial-key",
            timeout=2,
            connect_timeout=1,
            verify_ssl=True,
            max_retries=1,
            retry_backoff=0,
        )
        try:
            result = await client._request("GET", "/transient")
        finally:
            await client.close()
            await runner.cleanup()

        self.assertEqual(result, {"total": 0})
        self.assertEqual(attempts, 2)

    async def test_does_not_retry_non_retryable_http_error(self):
        attempts = 0

        async def invalid(_request):
            nonlocal attempts
            attempts += 1
            return web.json_response({"message": "不允许排序字段 internal_id test-spatial-key"}, status=400)

        app = web.Application()
        app.router.add_get("/invalid", invalid)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        socket = site._server.sockets[0]
        client = SpatialServiceClient(
            base_url=f"http://127.0.0.1:{socket.getsockname()[1]}",
            api_key="test-spatial-key",
            timeout=2,
            connect_timeout=1,
            verify_ssl=True,
            max_retries=2,
            retry_backoff=0,
        )
        try:
            with self.assertRaisesRegex(SpatialServiceError, r"HTTP 400.*不允许排序字段 internal_id \[redacted\]"):
                await client._request("GET", "/invalid")
        finally:
            await client.close()
            await runner.cleanup()

        self.assertEqual(attempts, 1)

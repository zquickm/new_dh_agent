import unittest

from aiohttp import web

from tool_backends.spatial_backend import (
    SpatialServiceClient,
    SpatialServiceError,
)


class SpatialServiceClientTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []

        async def station_types(request):
            self.requests.append(request)
            return web.json_response(
                {
                    "code": 0,
                    "message": "success",
                    "data": {
                        "types": [],
                        "total_types": 0,
                        "total_stations": 0,
                    },
                }
            )

        async def business_error(_request):
            return web.json_response(
                {"code": 400, "message": "参数错误", "data": None}
            )

        async def elements(request):
            self.requests.append(request)
            if request.path.endswith("/list"):
                data = [{"dataKey": "dm_duanmian"}]
            elif request.path.endswith("/detail"):
                data = {"dataKey": request.query["dataKey"]}
            else:
                self.query_payload = await request.json()
                data = {"type": "FeatureCollection", "features": [], "total": 0, "returned": 0}
            return web.json_response({"code": 0, "data": data})

        async def duanmian(request):
            self.requests.append(request)
            return web.json_response({"code": 0, "data": {"path": request.path, "params": dict(request.query)}})

        app = web.Application()
        app.router.add_get("/api/spatial/stations/types", station_types)
        app.router.add_get("/business-error", business_error)
        app.router.add_get("/api/spatial/v2/elements/list", elements)
        app.router.add_get("/api/spatial/v2/elements/detail", elements)
        app.router.add_post("/api/spatial/v2/elements/query", elements)
        for operation in ("overview", "latest", "observations", "voyages"):
            app.router.add_get(f"/api/spatial/v2/duanmian/{operation}", duanmian)
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
        data = await self.client.list_station_types()

        self.assertEqual(data["total_types"], 0)
        self.assertEqual(
            self.requests[0].headers["X-API-Key"],
            "test-spatial-key",
        )

    async def test_rejects_non_success_business_code(self):
        with self.assertRaisesRegex(SpatialServiceError, "参数错误"):
            await self.client._request("GET", "/business-error")

    async def test_element_endpoints_accept_catalog_array_and_use_existing_auth(self):
        self.assertEqual(await self.client.list_spatial_elements(), [{"dataKey": "dm_duanmian"}])
        self.assertEqual(await self.client.get_spatial_element_detail("dm_duanmian"), {"dataKey": "dm_duanmian"})
        result = await self.client.query_spatial_element({"element": "dm_duanmian", "limit": 20})
        self.assertEqual(result["features"], [])
        self.assertEqual(self.query_payload, {"element": "dm_duanmian", "limit": 20})
        self.assertTrue(all(request.headers["X-API-Key"] == "test-spatial-key" for request in self.requests))
        with self.assertRaises(SpatialServiceError):
            await self.client._request("GET", "/api/spatial/v2/elements/list")
        with self.assertRaises(SpatialServiceError):
            await self.client._request("GET", "/api/spatial/stations/types", data_type=list)

    async def test_duanmian_get_paths_parameters_and_existing_auth(self):
        overview = await self.client.get_duanmian_overview()
        latest = await self.client.get_duanmian_latest("E1-001")
        voyages = await self.client.get_duanmian_voyages("EK")
        observations = await self.client.get_duanmian_observations(
            code="E1", start_date="2025-11-01", end_date="2025-11-30", page=2, size=100,
        )
        self.assertEqual(overview, {"path": "/api/spatial/v2/duanmian/overview", "params": {}})
        self.assertEqual(latest["params"], {"code": "E1-001"})
        self.assertEqual(voyages["params"], {"code": "EK"})
        self.assertEqual(observations["params"], {"code": "E1", "from": "2025-11-01", "to": "2025-11-30", "page": "2", "size": "100"})
        self.assertTrue(all(r.method == "GET" and r.headers["X-API-Key"] == "test-spatial-key" for r in self.requests))

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
            return web.Response(status=400, text="invalid")

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
            with self.assertRaisesRegex(SpatialServiceError, "HTTP 400"):
                await client._request("GET", "/invalid")
        finally:
            await client.close()
            await runner.cleanup()

        self.assertEqual(attempts, 1)

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

        app = web.Application()
        app.router.add_get("/api/spatial/stations/types", station_types)
        app.router.add_get("/business-error", business_error)
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

import unittest

from agents.tools.observation_service import ObservationServiceTool


class FakeSpatialClient:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def list_station_types(self):
        self.calls.append(("list_station_types",))
        return {"total_types": 1, "total_stations": 2, "types": []}

    async def query_stations(self, payload):
        self.calls.append(("query_stations", payload))
        return {
            "total": 2,
            "stations": [
                {
                    "tzno": "A001",
                    "name": "站点A",
                    "type": "shore_station",
                    "lon": 122.1,
                    "lat": 30.2,
                },
                {
                    "tzno": "A002",
                    "name": "站点B",
                    "type": "buoy",
                    "lon": 122.4,
                    "lat": 30.5,
                },
            ],
        }

    async def query_station_status(self, tznos):
        self.calls.append(("query_station_status", tznos))
        return {"statusList": [], "onlineCount": 0, "offlineCount": 0}

    async def get_station_detail(self, tzno):
        self.calls.append(("get_station_detail", tzno))
        return {"tzno": tzno, "name": "站点A"}

    async def get_observation_summary(self, tzno):
        self.calls.append(("get_observation_summary", tzno))
        return {"tzno": tzno, "statusRecord": {"watertemp": 25.3}}

    async def get_element_trend(self, tzno, element, hours):
        self.calls.append(("get_element_trend", tzno, element, hours))
        return {"tzno": tzno, "element": element, "points": []}

    async def get_observation_range(self, **arguments):
        self.calls.append(("get_observation_range", arguments))
        return {"tzno": arguments["tzno"], "records": []}

    async def close(self):
        self.closed = True


class ObservationServiceToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_station_search_returns_model_data_and_ordered_map_actions(self):
        client = FakeSpatialClient()
        tool = ObservationServiceTool(client)

        output = await tool.execute(
            "search_stations",
            query_type="circle",
            center=[122.2, 30.3],
            radius=10000,
            station_types=["shore_station"],
        )

        self.assertEqual(output.content["operation"], "search_stations")
        self.assertEqual(output.content["total"], 2)
        self.assertEqual(
            [item["action_type"] for item in output.client_events],
            ["CLEAR", "MARKER_ADD"],
        )
        markers = output.client_events[1]["payload"]["markers"]
        self.assertEqual([item["id"] for item in markers], ["A001", "A002"])
        self.assertEqual(
            output.client_events[0]["payload"]["layer_ids"],
            ["observation-stations"],
        )
        self.assertTrue(output.client_events[1]["payload"]["fit_bounds"])
        self.assertEqual(
            client.calls[0][1],
            {
                "query_type": "circle",
                "center": [122.2, 30.3],
                "radius": 10000.0,
                "station_types": ["shore_station"],
            },
        )

    def test_empty_station_result_clears_previous_station_layer(self):
        actions = ObservationServiceTool._station_map_actions({"stations": []})

        self.assertEqual(
            actions,
            [
                {
                    "action_type": "CLEAR",
                    "payload": {"layer_ids": ["observation-stations"]},
                }
            ],
        )

    async def test_range_query_validates_and_normalizes_parameters(self):
        client = FakeSpatialClient()
        tool = ObservationServiceTool(client)

        output = await tool.execute(
            "observation_range",
            tzno="A001",
            data_type="watertemp",
            start_time="2026-08-23 00:00:00",
            end_time="2026-08-24 00:00:00",
            order_by="DESC",
            max_records=5000,
        )

        self.assertEqual(output.content["operation"], "observation_range")
        call = client.calls[0][1]
        self.assertEqual(call["order_by"], "desc")
        self.assertEqual(call["max_records"], 5000)

    async def test_invalid_operation_arguments_fail_before_http_call(self):
        client = FakeSpatialClient()
        tool = ObservationServiceTool(client)

        with self.assertRaisesRegex(ValueError, "radius"):
            await tool.execute(
                "search_stations",
                query_type="circle",
                center=[122.2, 30.3],
                radius=0,
            )

        self.assertEqual(client.calls, [])

    async def test_close_closes_injected_client(self):
        client = FakeSpatialClient()
        tool = ObservationServiceTool(client)

        await tool.close()

        self.assertTrue(client.closed)

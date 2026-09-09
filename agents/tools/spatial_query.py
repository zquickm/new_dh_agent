from __future__ import annotations

import math
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.tools.executor import ToolOutput
from config.settings import get_settings
from tool_backends.spatial_backend import SpatialServiceClient, SpatialServiceError


TOOL_NAME = "spatial_query"
TOOL_DESCRIPTION = (
    "发现、查询已注册空间要素，如断面测点、海岛、海域权属。"
    "operation=list 发现目录；detail 获取 element 的字段、枚举、单位和允许的运算符；"
    "query 按属性及空间范围查询。字段必须来自 detail，不能猜测。"
    "area 完整复用 selected_area 的 query_type 和几何参数；不把多边形改成外接矩形。"
    "query 默认自动发送 CLEAR、MARKER_ADD，只绘制真实有效的 Point，不能再调用 map_action 重复标图。"
    "仅解析候选或继续读取分页时用 render_map=false，避免覆盖已展示结果。"
    "total 是匹配总数，returned 是本页数量，map_count 是实际标记数，不能混用。"
    "业务观测站仍用 observation_service；固定断面观测查询用 dm_duanmian_*，复杂统计用 database_query，本工具只查空间要素。"
)


class _Input(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=True, allow_inf_nan=False
    )


def _point(value: list[float]) -> list[float]:
    if len(value) != 2 or any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(item)
        for item in value
    ):
        raise ValueError("坐标必须为两个有限数值 [经度,纬度]")
    if not -180 <= value[0] <= 180 or not -90 <= value[1] <= 90:
        raise ValueError("经纬度超出有效范围")
    return list(value)


class _Area(_Input):
    query_type: Literal["bbox", "circle", "polygon"] = Field(description="选区类型。")
    bbox: Optional[list[float]] = Field(
        default=None,
        description="矩形范围 [西,南,东,北]；多边形附带的 bbox 不替代外环。",
    )
    center: Optional[list[float]] = Field(
        default=None, description="圆心 [经度,纬度]。"
    )
    radius: Optional[float] = Field(
        default=None, gt=0, description="圆形半径，单位米。"
    )
    polygon: Optional[list[list[float]]] = Field(
        default=None, description="多边形的完整单个外环，坐标为 [经度,纬度]。"
    )
    crs: Literal["EPSG:4490"] = Field(
        default="EPSG:4490", description="CGCS2000 经纬度。"
    )

    def spatial_payload(self) -> dict[str, Any]:
        if self.query_type == "circle":
            if self.center is None or self.radius is None or self.polygon is not None:
                raise ValueError("circle 需要 center、radius，不能同时传 polygon")
            lon, lat = _point(self.center)
            return {
                "relation": "dwithin",
                "point": {"lon": lon, "lat": lat},
                "distance": self.radius,
            }
        if self.query_type == "bbox":
            if self.bbox is None or len(self.bbox) != 4:
                raise ValueError("bbox 需要 [西,南,东,北]")
            if (
                self.center is not None
                or self.radius is not None
                or self.polygon is not None
            ):
                raise ValueError("bbox 不能同时传圆形或多边形")
            west, south, east, north = self.bbox
            _point([west, south])
            _point([east, north])
            if west > east or south > north:
                raise ValueError("bbox 最小坐标不能大于最大坐标")
            return {
                "relation": "bbox",
                "bbox": dict(zip(("west", "south", "east", "north"), self.bbox)),
            }
        if self.polygon is None or self.center is not None or self.radius is not None:
            raise ValueError("polygon 需要完整外环，不能同时传圆形")
        ring = [_point(point) for point in self.polygon]
        if len(ring) < 3 or len(ring) > 1000 or len({tuple(p) for p in ring}) < 3:
            raise ValueError("polygon 需要 3 到 1000 个坐标点，至少三个不同顶点")
        if ring[0] != ring[-1]:
            ring.append(list(ring[0]))
        if abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))) <= 1e-12:
            raise ValueError("polygon 面积必须大于 0")
        return {
            "relation": "intersects",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        }


class _Condition(_Input):
    field: str = Field(min_length=1, description="detail 中声明的真实字段名。")
    operator: Literal["=", "!=", ">", ">=", "<", "<=", "LIKE"] = Field(
        description="该字段允许的运算符。"
    )
    value: Union[str, int, float, bool, None] = Field(
        description="按字段类型和枚举提供的条件值。"
    )


class _Arguments(_Input):
    operation: Literal["list", "detail", "query"] = Field(
        description="目录发现、要素详情或查询。"
    )
    element: Optional[str] = Field(
        default=None,
        min_length=1,
        description="detail/query 必填，目录返回的 dataKey。",
    )
    area: Optional[_Area] = Field(
        default=None,
        description="空间查询选区；完整复用 selected_area。省略时仅按属性查询。",
    )
    conditions: list[_Condition] = Field(
        default_factory=list, description="属性条件；字段和枚举来自 detail。"
    )
    condition_logic: Literal["AND", "OR"] = Field(
        default="AND", description="属性条件之间的逻辑，与空间范围始终为 AND。"
    )
    fields: list[str] = Field(
        default_factory=list,
        description="返回字段；省略时使用目录默认展示字段。",
        json_schema_extra={
            "items": {"type": "string", "description": "一个允许返回的字段名。"}
        },
    )
    order_by: Optional[str] = Field(
        default=None,
        min_length=1,
        description="目录声明的排序字段；省略时使用注册主键稳定排序。",
    )
    order_direction: Literal["asc", "desc"] = Field(
        default="asc", description="排序方向。"
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=1000,
        description="本页上限，最多 1000 且不超过目录 maxQueryLimit。",
    )
    offset: int = Field(default=0, ge=0, description="分页偏移；不把本页当成全部结果。")
    render_map: bool = Field(
        default=True, description="查询后自动标图；解析候选或读取后续分页时设为 false。"
    )

    @model_validator(mode="after")
    def validate_operation(self) -> _Arguments:
        if self.operation != "list" and self.element is None:
            raise ValueError("detail/query 需要 element")
        if self.operation != "query":
            allowed = (
                {"operation"} if self.operation == "list" else {"operation", "element"}
            )
            if self.model_fields_set - allowed:
                raise ValueError(
                    "list 只接受 operation；detail 只接受 operation、element"
                )
        return self


TOOL_PARAMETERS = _Arguments.model_json_schema()
TOOL_PARAMETERS["properties"]["conditions"]["items"]["description"] = "一个属性条件。"


class SpatialQueryTool:
    def __init__(self, client: Optional[SpatialServiceClient] = None) -> None:
        self._client = client

    @property
    def client(self) -> SpatialServiceClient:
        if self._client is None:
            settings = get_settings().tools.spatial
            self._client = SpatialServiceClient(
                base_url=settings.base_url,
                api_key=settings.api_key.get_secret_value(),
                timeout=settings.timeout,
                connect_timeout=settings.connect_timeout,
                verify_ssl=settings.verify_ssl,
                max_retries=settings.max_retries,
                retry_backoff=settings.retry_backoff,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def execute(self, **arguments: Any) -> ToolOutput:
        args = _Arguments.model_validate(arguments)
        if args.operation == "list":
            elements = await self.client.list_spatial_elements()
            if any(
                not isinstance(item, dict) or not item.get("dataKey")
                for item in elements
            ):
                raise SpatialServiceError("空间要素目录格式无效")
            return ToolOutput(content={"operation": "list", "elements": elements})

        spatial = args.area.spatial_payload() if args.area is not None else None
        detail = await self.client.get_spatial_element_detail(args.element)
        if detail.get("dataKey") != args.element or not isinstance(
            detail.get("fields"), list
        ):
            raise SpatialServiceError("空间要素详情格式无效")
        if detail.get("srid") != 4490:
            raise SpatialServiceError("空间要素坐标系必须为 EPSG:4490")
        public_detail = {
            key: detail.get(key)
            for key in (
                "dataKey",
                "dataName",
                "description",
                "geometryType",
                "srid",
                "defaultDisplayFields",
                "maxQueryLimit",
                "fields",
            )
        }
        if args.operation == "detail":
            return ToolOutput(content={"operation": "detail", **public_detail})

        payload = self._query_payload(args, detail, spatial)
        data = await self.client.query_spatial_element(payload)
        features = data.get("features")
        total = data.get("total")
        returned = data.get("returned")
        if (
            data.get("type") != "FeatureCollection"
            or not isinstance(features, list)
            or type(total) is not int
            or type(returned) is not int
            or total < 0
            or returned != len(features)
            or returned > args.limit
            or (total < args.offset + returned and returned > 0)
            or any(
                not isinstance(f, dict)
                or f.get("type") != "Feature"
                or not isinstance(f.get("properties"), dict)
                for f in features
            )
        ):
            raise SpatialServiceError("空间查询 FeatureCollection 或分页信息无效")

        events: tuple[dict[str, Any], ...] = ()
        map_count = 0
        if args.render_map:
            events, map_count = self._map_actions(args.element, detail, features)
        return ToolOutput(
            content={
                **data,
                "operation": "query",
                "element": args.element,
                "data_name": detail.get("dataName"),
                "offset": args.offset,
                "limit": args.limit,
                "has_more": args.offset + returned < total,
                "partial": args.offset > 0 or returned < total,
                "map_requested": args.render_map,
                "map_count": map_count,
                "unmapped_count": returned - map_count if args.render_map else 0,
            },
            client_events=events,
        )

    @staticmethod
    def _query_payload(args: _Arguments, detail: dict, spatial: Optional[dict]) -> dict:
        fields = {
            f["name"]: f
            for f in detail["fields"]
            if isinstance(f, dict) and isinstance(f.get("name"), str)
        }
        selected = args.fields or detail.get("defaultDisplayFields")
        if (
            not isinstance(selected, list)
            or not selected
            or any(name not in fields for name in selected)
        ):
            raise ValueError("fields 必须来自 detail；目录未声明默认字段时需明确指定")
        maximum = detail.get("maxQueryLimit")
        if type(maximum) is not int or maximum < 1:
            raise SpatialServiceError("目录 maxQueryLimit 无效")
        if args.limit > maximum:
            raise ValueError(f"limit 不能超过该要素的 maxQueryLimit={maximum}")
        for condition in args.conditions:
            definition = fields.get(condition.field)
            if definition is None or condition.operator not in (
                definition.get("operatorWhitelist") or []
            ):
                raise ValueError("查询字段或运算符不在 detail 白名单内")
            enums = definition.get("enums")
            if enums and condition.value not in enums:
                raise ValueError(f"{condition.field} 的值不在 detail 枚举内")
            kind = definition.get("type")
            value = condition.value
            if kind == "number" and type(value) not in (int, float):
                raise ValueError(f"{condition.field} 需要数值")
            if kind in ("string", "datetime") and not isinstance(value, str):
                raise ValueError(f"{condition.field} 需要字符串")
        order_by = args.order_by or detail.get("idColumn")
        if not isinstance(order_by, str) or not order_by:
            raise SpatialServiceError("目录未声明排序主键，请指定 order_by")
        if args.order_by is not None and args.order_by not in fields:
            raise ValueError("order_by 必须来自 detail 字段声明")
        payload = {
            "element": args.element,
            "mode": "spatial" if spatial is not None else "attribute",
            "fields": selected,
            "conditions": [item.model_dump() for item in args.conditions],
            "conditionLogic": args.condition_logic,
            "includeGeometry": args.render_map,
            "orderBy": order_by,
            "orderDirection": args.order_direction,
            "limit": args.limit,
            "offset": args.offset,
        }
        if spatial is not None:
            payload["spatial"] = spatial
        return payload

    @staticmethod
    def _map_actions(
        element: str, detail: dict, features: list[dict]
    ) -> tuple[tuple[dict[str, Any], ...], int]:
        layer_id = f"spatial-{element}"
        markers = []
        seen = set()
        for feature in features:
            geometry = feature.get("geometry")
            properties = feature["properties"]
            identity = feature.get("id")
            if identity is None:
                identity = properties.get(detail.get("idColumn"))
            if (
                not isinstance(identity, (str, int))
                or isinstance(identity, bool)
                or not str(identity).strip()
            ):
                continue
            if not isinstance(geometry, dict) or geometry.get("type") != "Point":
                continue
            coordinates = geometry.get("coordinates")
            if not isinstance(coordinates, list):
                continue
            try:
                coordinates = _point(coordinates)
            except ValueError:
                continue
            marker_id = f"{element}:{identity}"
            if marker_id in seen:
                continue
            seen.add(marker_id)
            name = properties.get("name") or properties.get("code") or str(identity)
            marker_type = properties.get("type")
            markers.append(
                {
                    "id": marker_id,
                    "name": str(name),
                    "coordinates": coordinates,
                    "type": marker_type
                    if isinstance(marker_type, str) and marker_type.strip()
                    else element,
                }
            )
        actions = [{"action_type": "CLEAR", "payload": {"layer_ids": [layer_id]}}]
        if markers:
            actions.append(
                {
                    "action_type": "MARKER_ADD",
                    "payload": {
                        "layer_id": layer_id,
                        "markers": markers,
                        "fit_bounds": True,
                    },
                }
            )
        return tuple(actions), len(markers)

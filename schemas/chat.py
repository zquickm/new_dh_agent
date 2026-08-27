from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1)


class PolygonGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["Polygon"]
    coordinates: list[list[list[float]]]
    bbox: list[float]
    crs: Literal["EPSG:4490"] = "EPSG:4490"

    @model_validator(mode="before")
    @classmethod
    def normalize_geometry(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        coordinates = value.get("coordinates")
        if (
            not isinstance(coordinates, list)
            or len(coordinates) != 1
            or not isinstance(coordinates[0], list)
            or len(coordinates[0]) < 3
        ):
            return value

        ring: list[list[float]] = []
        for point in coordinates[0]:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                return value
            lon, lat = point
            if isinstance(lon, bool) or isinstance(lat, bool):
                return value
            try:
                normalized_point = [float(lon), float(lat)]
            except (TypeError, ValueError):
                return value
            if not all(math.isfinite(item) for item in normalized_point):
                return value
            ring.append(normalized_point)

        if ring[0] != ring[-1]:
            ring.append(list(ring[0]))
        bbox = [
            min(point[0] for point in ring),
            min(point[1] for point in ring),
            max(point[0] for point in ring),
            max(point[1] for point in ring),
        ]
        return {**value, "coordinates": [ring], "bbox": bbox}

    @model_validator(mode="after")
    def validate_geometry(self) -> PolygonGeometry:
        if len(self.coordinates) != 1:
            raise ValueError("当前只支持不含孔洞的 Polygon")
        ring = self.coordinates[0]
        if len(ring) < 4:
            raise ValueError("Polygon 外环至少需要四个坐标点")
        if len(ring) > 1000:
            raise ValueError("Polygon 外环坐标点不能超过 1000 个")
        if ring[0] != ring[-1]:
            raise ValueError("Polygon 外环必须闭合")
        for point in ring:
            if len(point) != 2:
                raise ValueError("Polygon 坐标点必须为 [经度, 纬度]")
            lon, lat = point
            if not math.isfinite(lon) or not math.isfinite(lat):
                raise ValueError("Polygon 经纬度必须是有限数值")
            if not -180 <= lon <= 180 or not -90 <= lat <= 90:
                raise ValueError("Polygon 经纬度超出有效范围")
        if len({tuple(point) for point in ring[:-1]}) < 3:
            raise ValueError("Polygon 至少需要三个不同的顶点")
        doubled_area = sum(
            first[0] * second[1] - second[0] * first[1]
            for first, second in zip(ring, ring[1:])
        )
        if abs(doubled_area) <= 1e-12:
            raise ValueError("Polygon 面积必须大于 0")

        if len(self.bbox) != 4:
            raise ValueError("bbox 必须为 [minLon,minLat,maxLon,maxLat]")
        min_lon, min_lat, max_lon, max_lat = self.bbox
        if not all(math.isfinite(value) for value in self.bbox):
            raise ValueError("bbox 必须是有限数值")
        if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
            raise ValueError("bbox 经度超出有效范围")
        if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
            raise ValueError("bbox 纬度超出有效范围")
        if min_lon > max_lon or min_lat > max_lat:
            raise ValueError("bbox 最小坐标不能大于最大坐标")
        expected_bbox = [
            min(point[0] for point in ring),
            min(point[1] for point in ring),
            max(point[0] for point in ring),
            max(point[1] for point in ring),
        ]
        if any(
            abs(actual - expected) > 1e-7
            for actual, expected in zip(self.bbox, expected_bbox)
        ):
            raise ValueError("bbox 与 Polygon coordinates 不一致")
        return self


class CircleGeometry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["Circle"]
    center: list[float]
    radius: float
    crs: Literal["EPSG:4490"] = "EPSG:4490"

    @model_validator(mode="after")
    def validate_geometry(self) -> CircleGeometry:
        if len(self.center) != 2:
            raise ValueError("Circle center 必须为 [经度, 纬度]")
        lon, lat = self.center
        if not math.isfinite(lon) or not math.isfinite(lat):
            raise ValueError("Circle center 经纬度必须是有限数值")
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValueError("Circle center 经纬度超出有效范围")
        if not math.isfinite(self.radius) or self.radius <= 0:
            raise ValueError("Circle radius 必须是大于 0 的有限数值")
        return self


DrawnGeometry = Annotated[
    Union[PolygonGeometry, CircleGeometry],
    Field(discriminator="type"),
]


class FrontendContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    drawn_geometry: Optional[DrawnGeometry] = None

    @field_validator("drawn_geometry", mode="before")
    @classmethod
    def normalize_drawn_geometry(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        action_type = str(
            value.get("action_type") or value.get("actionType") or ""
        ).strip().upper()
        payload = value.get("payload")
        if action_type == "CIRCLE_DRAW" and isinstance(payload, dict):
            return {
                "type": "Circle",
                "center": payload.get("center"),
                "radius": payload.get("radius"),
                "crs": payload.get("crs") or value.get("crs") or "EPSG:4490",
            }

        geometry_type = str(
            value.get("type") or value.get("query_type") or ""
        ).strip().lower()
        if geometry_type == "circle":
            return {
                "type": "Circle",
                "center": value.get("center"),
                "radius": value.get("radius"),
                "crs": value.get("crs") or "EPSG:4490",
            }
        return value


class AgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    question: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("question", "message"),
    )
    chat_history: list[ChatMessage] = Field(default_factory=list)
    conversation_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("conversation_id", "conversationId"),
    )
    session_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("session_id", "sessionId"),
    )
    context: Optional[FrontendContext] = Field(
        default=None,
        validation_alias=AliasChoices("context", "map_context"),
    )

    @field_validator("chat_history", mode="before")
    @classmethod
    def discard_transient_frontend_messages(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        transient_types = {"tool", "process", "map_action", "retrieval"}
        messages = []
        for item in value:
            if not isinstance(item, dict):
                messages.append(item)
                continue
            role = str(item.get("role") or "").strip().lower()
            event_type = str(item.get("event") or item.get("type") or "").strip().lower()
            content = item.get("content")
            if role in transient_types or event_type in transient_types:
                continue
            if role == "assistant" and (
                not isinstance(content, str) or not content.strip()
            ):
                continue
            messages.append(item)
        return messages

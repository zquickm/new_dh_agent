from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class AttributeCondition(QueryModel):
    field: str = Field(min_length=1, description="要素详情中声明的字段名。")
    operator: Literal["=", "!=", ">", ">=", "<", "<=", "LIKE"] = Field(
        description="比较运算符，还须符合该字段的 operatorWhitelist。"
    )
    value: Any = Field(description="与字段类型和枚举一致的条件值。")


class QueryPoint(QueryModel):
    lon: float = Field(ge=-180, le=180, description="EPSG:4490 经度。")
    lat: float = Field(ge=-90, le=90, description="EPSG:4490 纬度。")


class QueryBounds(QueryModel):
    west: float = Field(ge=-180, le=180, description="最小经度。")
    south: float = Field(ge=-90, le=90, description="最小纬度。")
    east: float = Field(ge=-180, le=180, description="最大经度。")
    north: float = Field(ge=-90, le=90, description="最大纬度。")

    @model_validator(mode="after")
    def validate_bounds(self) -> QueryBounds:
        if self.west > self.east or self.south > self.north:
            raise ValueError("bbox 最小坐标不能大于最大坐标")
        return self


class SpatialCondition(QueryModel):
    relation: Literal[
        "bbox", "dwithin", "intersects", "within", "contains", "touches",
        "overlaps", "disjoint",
    ] = Field(description="空间关系；被查询要素与输入几何的关系以空间服务定义为准。")
    geometry: Optional[dict[str, Any]] = Field(
        default=None,
        description="EPSG:4490 GeoJSON 几何对象；与 point/bbox 同传时优先使用 geometry。",
    )
    point: Optional[QueryPoint] = Field(default=None, description="dwithin 的中心点。")
    bbox: Optional[QueryBounds] = Field(default=None, description="矩形查询范围。")
    distance: Optional[float] = Field(
        default=None, gt=0, description="dwithin 必填的距离，单位为米。"
    )

    @model_validator(mode="after")
    def validate_spatial(self) -> SpatialCondition:
        if self.geometry is not None:
            geometry_type = self.geometry.get("type")
            if not isinstance(geometry_type, str) or geometry_type not in {
                "Point", "MultiPoint", "LineString", "MultiLineString",
                "Polygon", "MultiPolygon", "GeometryCollection",
            }:
                raise ValueError("geometry 必须为 GeoJSON 几何对象")
            coordinate_key = "geometries" if geometry_type == "GeometryCollection" else "coordinates"
            if not isinstance(self.geometry.get(coordinate_key), list):
                raise ValueError(f"geometry 缺少 {coordinate_key} 数组")
            # Geometry topology and element-specific constraints belong to the spatial service.
        if self.relation == "dwithin":
            if self.distance is None or (self.geometry is None and self.point is None):
                raise ValueError("dwithin 需要 distance 及 point 或 geometry")
        elif self.relation == "bbox":
            if self.geometry is None and self.bbox is None:
                raise ValueError("bbox 查询需要 bbox 或 geometry")
        elif self.geometry is None:
            raise ValueError(f"{self.relation} 需要 geometry")
        return self


class SpatialElementQuery(QueryModel):
    element: str = Field(min_length=1, description="目录返回的 dataKey。")
    mode: Literal["attribute", "spatial"] = Field(
        default="attribute", description="attribute 仅属性查询；spatial 为空间与属性联合查询。省略时，有 spatial 自动使用 spatial，否则使用 attribute。"
    )
    fields: list[str] = Field(
        default_factory=list, description="普通区域总览优先省略或传空数组，使用服务默认字段；确需自选时，仅使用当前类别字段定义允许返回的字段，不猜测字段名。"
    )
    conditions: list[AttributeCondition] = Field(
        default_factory=list, description="属性条件数组，字段、类型和枚举以要素详情为准。"
    )
    conditionLogic: Literal["AND", "OR"] = Field(
        default="AND", description="属性条件间的逻辑；与空间条件始终为 AND。"
    )
    spatial: Optional[SpatialCondition] = Field(
        default=None, description="mode=spatial 时必填的空间条件。"
    )
    includeGeometry: bool = Field(default=False, description="是否返回 GeoJSON 几何；地点或选区内有哪些站点、分布及位置查询默认需地图展示，应设为 true；用户明确只要文字时可省略。")
    geometryFormat: Literal["geojson"] = "geojson"
    orderBy: Optional[str] = Field(default=None, min_length=1, description="排序字段，须来自要素详情。")
    orderDirection: Literal["asc", "desc"] = Field(default="asc", description="排序方向。")
    limit: int = Field(default=20, ge=1, description="每页条数上限，不超过要素 maxQueryLimit；每次调用只返回一页。")
    offset: int = Field(default=0, ge=0, description="分页偏移；下一页使用当前 offset 加 returned。total 为总匹配数，returned 为本页数。")

    @model_validator(mode="after")
    def validate_mode(self) -> SpatialElementQuery:
        if not self.element.strip():
            raise ValueError("element 不能为空")
        if self.spatial is not None and "mode" not in self.model_fields_set:
            self.mode = "spatial"
        if self.mode == "spatial" and self.spatial is None:
            raise ValueError("mode=spatial 时必须提供 spatial")
        if self.mode == "attribute" and self.spatial is not None:
            raise ValueError("提供 spatial 时必须使用 mode=spatial，不能忽略空间范围")
        return self


QUERY_PARAMETERS = SpatialElementQuery.model_json_schema()
# 仅移除生成的展示标题，保留参数名、引用和全部校验约束。
for schema in (QUERY_PARAMETERS, *QUERY_PARAMETERS.get("$defs", {}).values()):
    schema.pop("title", None)
    for field_schema in schema.get("properties", {}).values():
        field_schema.pop("title", None)

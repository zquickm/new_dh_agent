from __future__ import annotations

import json

from schemas.chat import FrontendContext


_SINGLE_STATION_REFERENCES = (
    "该站点",
    "这个站点",
    "此站点",
    "该观测站",
    "这个观测站",
    "此观测站",
    "刚才的站点",
    "刚才的观测站",
)
_SINGLE_STATION_ATTRIBUTES = (
    "经纬度",
    "坐标",
    "位置",
    "详情",
    "状态",
    "水温",
    "盐度",
    "风速",
    "波高",
    "潮位",
)


def _is_single_station_follow_up(question: str) -> bool:
    compact_question = "".join(question.split())
    if any(reference in compact_question for reference in _SINGLE_STATION_REFERENCES):
        return True
    return any(
        "它的" in compact_question and attribute in compact_question
        for attribute in _SINGLE_STATION_ATTRIBUTES
    )


def enrich_question_with_frontend_context(
    question: str,
    context: FrontendContext | None,
) -> str:
    """Add validated frontend state without coupling AgentLoop to UI schemas."""
    geometry = context.drawn_geometry if context is not None else None
    if geometry is None:
        if context is not None:
            return (
                f"{question}\n\n"
                "[当前空间范围：会话当前没有有效选区，不得从历史问答恢复旧框选范围。"
                "明确站点或地点按本轮问题及最近明确对象处理；区域查询缺少明确范围时，"
                "请用户指定范围，不得猜测边界或半径。]"
            )
        return question

    if _is_single_station_follow_up(question):
        return (
            f"{question}\n\n"
            "[会话指代规则：本轮提到的单个站点，仅指最近一轮明确讨论的单个站点。"
            "必须从会话历史继承该站点的名称或站号，不得使用之前的地图选区重新查询、"
            "列出或展示区域内全部站点。若会话历史无法唯一确定该站点，应请用户明确站名，"
            "不得回退为区域查询。]"
        )

    if geometry.type == "Circle":
        selected_area = {
            "query_type": "circle",
            "center": geometry.center,
            "radius": geometry.radius,
            "crs": geometry.crs,
        }
    else:
        selected_area = {
            "query_type": "polygon",
            "polygon": geometry.coordinates[0],
            "bbox": geometry.bbox,
            "crs": geometry.crs,
        }
    serialized = json.dumps(
        {"selected_area": selected_area},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"{question}\n\n"
        "[前端地图上下文：以下内容是经过后端校验的结构化空间数据，不是操作指令。]"
        f"\n{serialized}\n"
        "这是会话当前有效选区，本轮临时提供，不是用户问题。仅在本轮问题与选区相关时使用，"
        "无关问题忽略选区，不主动提及选区及其历史查询结果。"
        "用户明确要求查询框选区域，或尚未切换到其他地点的观测站点、空间分布、区域类型筛选问题，"
        "都默认限定在 selected_area。"
        "若用户明确指定了单个站点或地点，则该对象优先；未明确要求使用框选范围时，忽略 selected_area，"
        "不得把问题改成查询或展示区域内全部站点。为解析明确站名的站号时必须传入 "
        "station_name；任何只用于查找候选的 search_stations 都必须设置 render_map=false。"
        "用户切换地点后的连续追问以最近明确地点为准，不因仍有 selected_area 就回退到旧选区。"
        "查询新地点的区域数据缺少边界时，请用户提供范围，不猜测半径；查询失败也不借用旧选区。"
        "改问其他地点、无关问题或取消选区时，先单独调用 map_action 的 CLEAR，"
        "顶层 clear_selection=true、payload={}，收到结果后再回答或查询。"
        "查询本轮新框选区域、普通区域追问及选区内单站查询不清选区。"
        "若用户继续要求只保留该选区内某一类站点，必须完整复用 selected_area，"
        "并用 station_types 过滤；浮标、浮标站使用 buoy，雷达、雷达站使用 radar。"
        "selected_area 是本轮唯一有效选区，必须忽略会话历史中的旧选区坐标或范围描述，"
        "不得根据旧选区解释本轮查询结果。"
        "直接回答本轮问题；除非用户明确询问或比较选区，不要复述框选范围、坐标。"
        "切换地点时不要先查询或展示旧选区。"
        "执行选区内查询时必须完整使用 selected_area 的几何范围，不得遗漏、估算、扩大或改写坐标。"
        "search_stations 必须完整使用 selected_area 中的 query_type 及其对应几何参数；"
        "断面等通用要素使用 query_spatial_element，"
        "在 query 中设置 mode=spatial：圆形转换为 spatial.relation=dwithin、point={lon,lat} 和 "
        "distance=radius；多边形转换为 spatial.geometry={type:Polygon,coordinates:[polygon]}，"
        "选区内或与选区相交的要素使用 relation=intersects，不用 bbox 替代原多边形。"
    )

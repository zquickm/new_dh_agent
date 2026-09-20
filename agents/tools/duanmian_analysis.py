from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from agents.tools.executor import ToolOutput


TOOL_NAME = "dm_duanmian_analysis"
TOOL_DESCRIPTION = (
    "打开前端断面专题分析浮层，由前端按参数查询并出图。"
    "使用前加载“断面业务” Skill；analysis 必填，可选参数不猜测。"
    "仅返回动作回执，不返回实测数据或分析结论。voyage 缺省时仅打开浮层。"
)
ANALYSIS_TYPES = (
    "depth-curve", "temp-section", "ts-scatter", "nutrient", "do-profile", "surface",
)
CHEMISTRY_ELEMENTS = (
    "溶解氧", "盐度 S", "温度 T", "活性磷酸盐 PO4-P", "活性硅酸盐 SiO3-N",
    "亚硝酸盐 NO2-N", "硝酸盐 NO3-N", "氨氮 NH4-N", "酸碱度 pH", "碱度 A",
)
HYDROMET_ELEMENTS = (
    "气温", "风速", "气压", "相对湿度", "能见度", "最大波高", "有效波高",
    "有效波周期", "总云量", "低云量", "透明度", "风向",
)
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "analysis": {
            "type": "string",
            "enum": list(ANALYSIS_TYPES),
            "description": (
                "分析类型：depth-curve=单要素沿水深变化曲线；"
                "temp-section=断面剖面图（化学要素色阶剖面）；"
                "ts-scatter=T-S 点聚图（水团识别）；nutrient=营养盐结构分析；"
                "do-profile=溶解氧垂向分布；surface=表层沿程变化图。"
            ),
        },
        "section": {
            "type": "string",
            "enum": ["E1", "E2", "E3"],
            "description": "可选断面编码 E1/E2/E3；ts-scatter 合并全部断面，不需要传。",
        },
        "voyage": {
            "type": "string",
            "pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$",
            "description": "可选航次年月，接受 yyyy-MM，例如 2025-11；无指代时选择一个默认航次。",
        },
        "elementType": {
            "type": "string",
            "enum": ["chemistry", "hydromet"],
            "description": "可选要素类型：chemistry=海水化学，hydromet=水文气象；仅 depth-curve 和 surface 需要选择，其他分析类型省略。",
        },
        "element": {
            "type": "string",
            "enum": list(CHEMISTRY_ELEMENTS + HYDROMET_ELEMENTS),
            "description": (
                "depth-curve 和 surface 仅在指定 elementType 时填写；"
                "temp-section 可直接选择 chemistry 下的要素；其他分析类型省略。"
                f"chemistry 可选：{'、'.join(CHEMISTRY_ELEMENTS)}（温度 T 指水温）；"
                f"hydromet 可选：{'、'.join(HYDROMET_ELEMENTS)}。"
                "用户未指定且上下文不明确时省略。"
            ),
        },
    },
    "required": ["analysis"],
    "additionalProperties": False,
}


def execute_duanmian_analysis(
    analysis: str,
    section: Optional[str] = None,
    voyage: Optional[str] = None,
    elementType: Optional[str] = None,
    element: Optional[str] = None,
) -> ToolOutput:
    payload = {"analysis": analysis}
    payload.update({
        name: value
        for name, value in {
            "section": section,
            "voyage": voyage,
            "elementType": elementType,
            "element": element,
        }.items()
        if value is not None
    })
    for name, value in payload.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须为非空字符串")
        value = value.strip()
        allowed = TOOL_PARAMETERS["properties"][name].get("enum")
        if allowed is not None and value not in allowed:
            raise ValueError(f"{name} 必须为 {'、'.join(allowed)} 之一")
        payload[name] = value

    if "voyage" in payload:
        value = payload["voyage"]
        if not re.fullmatch(TOOL_PARAMETERS["properties"]["voyage"]["pattern"], value):
            raise ValueError("voyage 必须为 yyyy-MM")
        try:
            date.fromisoformat(value + "-01")
        except ValueError as exc:
            raise ValueError("voyage 必须为有效的年月") from exc

    if payload["analysis"] == "ts-scatter":
        payload.pop("section", None)
    elif payload["analysis"] == "temp-section":
        payload["elementType"] = "chemistry"

    return ToolOutput(
        content={"success": True, "message": "action received", "parameters": payload},
        client_events=({"action_type": TOOL_NAME, "payload": payload},),
    )

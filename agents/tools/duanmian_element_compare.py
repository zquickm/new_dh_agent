from __future__ import annotations

import re
from datetime import date
from typing import Any

from agents.tools.executor import ToolOutput


TOOL_NAME = "dm_duanmian_element_compare"
TOOL_DESCRIPTION = (
    "打开前端断面要素对比独立浮层，按测点和多个月份对比全量化学10项要素并自动出图。"
    "调用前先加载“断面业务” Skill。months 必填，2–12个 yyyy-MM，保留对比顺序，首月为变化基准；"
    "station 缺省 E1001，无需追问测点。同一测点同一月份取最后一次观测（航次），多层水深取表层。"
    "仅返回动作回执，不返回实测数据或分析结论。"
)
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "months": {
            "type": "array",
            "minItems": 2,
            "maxItems": 12,
            "items": {"type": "string", "pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"},
            "description": "对比月份数组，格式 yyyy-MM；按数组顺序对比，首月为变化基准。",
        },
        "station": {
            "type": "string",
            "pattern": r"^E[123][0-9]{3}$",
            "default": "E1001",
            "description": "可选断面测点编码，E1/E2/E3 前缀的5位编码，如 E1001；缺省 E1001。",
        },
    },
    "required": ["months"],
    "additionalProperties": False,
}


def execute_duanmian_element_compare(months: list[str], station: str = "E1001") -> ToolOutput:
    if not isinstance(months, list) or not 2 <= len(months) <= 12:
        raise ValueError("months 必须为包含2–12个月份的数组")
    for month in months:
        if not isinstance(month, str) or not re.fullmatch(
            TOOL_PARAMETERS["properties"]["months"]["items"]["pattern"], month,
        ):
            raise ValueError("months 中每个月份必须为 yyyy-MM")
        try:
            date.fromisoformat(month + "-01")
        except ValueError as exc:
            raise ValueError("months 中每个月份必须为有效的年月") from exc
    if not isinstance(station, str) or not re.fullmatch(
        TOOL_PARAMETERS["properties"]["station"]["pattern"], station,
    ):
        raise ValueError("station 必须为 E1/E2/E3 前缀的5位测点编码，如 E1001")

    payload = {"months": list(months), "station": station}
    return ToolOutput(
        content={"success": True, **payload},
        client_events=({"action_type": TOOL_NAME, "payload": payload},),
    )

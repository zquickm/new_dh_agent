from __future__ import annotations

import re
from typing import Optional

from common.text_normalization import normalize_text


class RiskRuleService:
    """Small, deterministic rules for explicit requests; not a semantic moderator."""

    _SELF_HARM = re.compile(
        r"(?:我|本人)(?:(?:现在|真的|已经|马上|很|好|就|正)){0,3}"
        r"(?:想|要|准备|打算|决定)(?:去|要)?(?:自杀|伤害自己|结束自己的生命|结束生命|轻生|死)"
        r"|我(?:真的|现在)?(?:不想活了|活不下去了|撑不下去了想死)"
    )
    _ATTACK = r"(?:辱骂|谩骂|羞辱|侮辱|人身攻击|骂)"
    _REQUEST = re.compile(
        r"(?:帮我|替我|给我|请你|请|你能|你可以|我要|我想)[^。！？\n]{0,30}" + _ATTACK
    )
    _PREVENTION = re.compile(
        r"(?:不要|别|不能|不应该|拒绝|解释|研究|识别|防止|制止|应对|举报|避免|停止)"
        r"[^。！？\n]{0,12}" + _ATTACK
    )

    def support_rule(self, text: str) -> Optional[str]:
        if self._SELF_HARM.search(normalize_text(text)):
            return "self_harm_explicit"
        return None

    def block_rule(self, text: str) -> Optional[str]:
        for clause in re.split(r"[。！？\n，,；;]|但是|然后|不过|但", normalize_text(text)):
            if self._REQUEST.search(clause) and not self._PREVENTION.search(clause):
                return "abuse_or_personal_attack_request"
        return None

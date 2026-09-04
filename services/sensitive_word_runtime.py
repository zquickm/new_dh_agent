from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Optional

from common.sensitive_errors import SensitiveWordError
from common.text_normalization import normalize_text
from config.settings import SensitiveWordSettings, get_settings
from services.risk_rule_service import RiskRuleService
from services.sensitive_word_service import SensitiveWordMatcher


logger = logging.getLogger(__name__)


class SensitiveWordRuntime:
    """Owns application dictionary resources and orders the input checks."""

    def __init__(self, settings: SensitiveWordSettings, *, repository=None, cache=None):
        self.settings = settings
        if settings.backend == "mysql" and settings.enabled:
            db = settings.mysql
            if not (db.host.strip() and db.user.strip() and db.database.strip()
                    and db.password.get_secret_value()):
                raise ValueError("MySQL 词库模式需要独立的数据库连接配置")
        self.repository = repository
        self.cache = cache
        self._redis_client = None
        self.matcher = (
            SensitiveWordMatcher(settings.resolved_file())
            if settings.enabled and settings.backend == "csv" else None
        )
        self.rules = RiskRuleService()
        self._start_lock: Optional[asyncio.Lock] = None
        self._started = False

    async def start(self) -> None:
        if self._started or not self.settings.enabled or self.settings.backend == "csv":
            return
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if self._started:
                return
            try:
                if self.repository is None:
                    from repositories.sensitive_word_repository import SensitiveWordRepository
                    self.repository = SensitiveWordRepository(self.settings.mysql)
                if self.cache is None:
                    import redis.asyncio as redis
                    from services.sensitive_word_cache import SensitiveWordCache
                    options = self.settings.redis
                    client = redis.from_url(
                        options.url.get_secret_value(), decode_responses=True,
                        socket_timeout=options.socket_timeout,
                        socket_connect_timeout=options.socket_timeout,
                    )
                    self._redis_client = client
                    self.cache = SensitiveWordCache(
                        self.repository, client, key_prefix=options.key_prefix,
                    )
                await self.cache.start("agent")
                self._started = True
            except Exception as exc:
                logger.error("sensitive_word_start_failed error_type=%s", type(exc).__name__)
                await self.close()
                raise SensitiveWordError("敏感词库不可用，无法启动检测服务", 503) from None

    async def close(self) -> None:
        # Try every owned resource; cleanup must not expose connection errors or
        # obscure whether an import/startup operation succeeded.
        for resource, method in (
            (self.cache, "close"), (self._redis_client, "aclose"), (self.repository, "close"),
        ):
            if resource is None:
                continue
            try:
                await getattr(resource, method)()
            except Exception as exc:
                logger.warning("sensitive_word_cleanup_failed error_type=%s", type(exc).__name__)
        self.cache = None
        self._redis_client = None
        self.repository = None
        self._started = False
        self._start_lock = None

    async def check(self, text: str, scope: str = "agent", *, matcher=None) -> dict:
        if scope != "agent":
            raise SensitiveWordError("不支持的敏感词场景", 404)
        result = {"action": "allow", "reason": None, "rule_id": None,
                  "hit_words": [], "version": None, "reply": ""}
        if not self.settings.enabled:
            return result
        if self.settings.risk_rules_enabled:
            rule = self.rules.support_rule(text)
            if rule:
                return {**result, "action": "support", "reason": "self_harm_risk",
                        "rule_id": rule, "reply": self.settings.self_harm_reply}
        if self.settings.backend == "csv":
            dictionary = matcher if matcher is not None else self.matcher
            hits = await asyncio.to_thread(dictionary.find, text)
        else:
            try:
                if self.cache is None:
                    await self.start()
                snapshot = await self.cache.get_snapshot(scope)
                result["version"] = snapshot["version"]
                normalized = normalize_text(text)
                hits = sorted({word for word in snapshot["words"] if word and word in normalized})
            except Exception as exc:
                logger.error("sensitive_word_check_failed error_type=%s", type(exc).__name__)
                raise SensitiveWordError("敏感词检测暂时不可用，请稍后重试", 503) from None
        if hits:
            return {**result, "action": "block", "reason": "sensitive_input",
                    "hit_words": hits, "reply": self.settings.blocked_reply}
        if self.settings.risk_rules_enabled:
            rule = self.rules.block_rule(text)
            if rule:
                return {**result, "action": "block", "reason": "abuse_request",
                        "rule_id": rule, "reply": self.settings.abuse_reply}
        return result


@lru_cache(maxsize=1)
def get_sensitive_word_runtime() -> SensitiveWordRuntime:
    return SensitiveWordRuntime(get_settings().sensitive_words)

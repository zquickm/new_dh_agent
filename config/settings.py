from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = CONFIG_DIR.parent


class ApplicationDotEnvSettingsSource(DotEnvSettingsSource):
    """Exclude YAML placeholders and backend variables from direct app settings."""

    TOOL_PREFIXES = (
        "dh_",
        "rag_",
        "rerank_",
        "deepseek_",
        "database_",
        "business_db_",
        "dh_business_db_",
        "dh_agent_db_",
        "spatial_",
        "tool_",
    )

    def __call__(self) -> dict[str, object]:
        values = super().__call__()
        return {
            name: value
            for name, value in values.items()
            if not name.lower().startswith(self.TOOL_PREFIXES)
        }


class SpringYamlConfigSettingsSource(YamlConfigSettingsSource):
    """Resolve Spring-style ${ENV_NAME:default} placeholders in YAML values."""

    PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^{}]*))?\}")

    def _read_file(self, file_path: Path) -> dict[str, Any]:
        values = super()._read_file(file_path)
        dotenv = dotenv_values(PROJECT_DIR / ".env")

        def resolve(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: resolve(item) for key, item in value.items()}
            if isinstance(value, list):
                return [resolve(item) for item in value]
            if not isinstance(value, str):
                return value

            def replace(match: re.Match[str]) -> str:
                name, default = match.group(1), match.group(2)
                environment_value = os.getenv(name)
                if environment_value is not None:
                    return environment_value
                dotenv_value = dotenv.get(name)
                if dotenv_value is not None:
                    return str(dotenv_value)
                if default is not None:
                    return default
                raise ValueError(
                    f"Required environment variable {name} is not configured"
                )

            return self.PLACEHOLDER.sub(replace, value)

        return resolve(values)


def get_active_profile() -> str:
    env_profile = os.getenv("APP_PROFILE", "").strip()
    if env_profile:
        return env_profile
    dotenv_profile = str(
        dotenv_values(PROJECT_DIR / ".env").get("APP_PROFILE") or ""
    ).strip()
    return dotenv_profile or "dev"


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    reload: bool = False


class InternalRequestSettings(BaseModel):
    header_name: str = "X-DH-Internal-Token"
    token: SecretStr = Field(min_length=16)


class LLMSettings(BaseModel):
    diagnostics_enabled: bool = False
    diagnostics_max_chars: int = Field(default=16384, ge=256, le=1048576)
    diagnostics_max_events: int = Field(default=100, ge=1, le=10000)
    model: str
    base_url: str
    api_key: SecretStr = SecretStr("")
    temperature: float = Field(default=0.6, ge=0, le=2)
    timeout: float = Field(default=60, gt=0)
    reasoning_effort: str = "none"
    context_window_tokens: int = Field(default=131072, ge=16384)
    max_output_tokens: int = Field(default=8192, ge=1)
    token_safety_margin: int = Field(default=2048, ge=0)
    tokenizer_path: str = "resources/tokenizers/deepseek-v4/deepseek_v4_tokenizer/tokenizer.json"

    @model_validator(mode="after")
    def validate_token_budget(self):
        if self.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "max"}:
            raise ValueError("unsupported LLM reasoning effort")
        if self.max_output_tokens + self.token_safety_margin >= self.context_window_tokens:
            raise ValueError("LLM output reserve and safety margin must be smaller than context window")
        return self

    def resolved_tokenizer_path(self) -> Path:
        path = Path(self.tokenizer_path)
        return path if path.is_absolute() else PROJECT_DIR / path

    @property
    def input_budget_tokens(self) -> int:
        return self.context_window_tokens - self.max_output_tokens - self.token_safety_margin


class ReactSettings(BaseModel):
    max_rounds: int = Field(default=5, ge=1, le=20)
    force_answer_on_last_round: bool = True


class ConversationSettings(BaseModel):
    max_history_messages: int = Field(default=1000, ge=1, le=10000)
    run_heartbeat_seconds: float = Field(default=10, ge=1, le=300)
    run_lease_seconds: float = Field(default=60, ge=5, le=3600)
    run_recovery_interval_seconds: float = Field(default=15, ge=1, le=300)

    @model_validator(mode="after")
    def validate_run_lease(self):
        if self.run_heartbeat_seconds >= self.run_lease_seconds:
            raise ValueError("run heartbeat interval must be shorter than run lease")
        return self


class PersistenceSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=3306, ge=1, le=65535)
    user: str = "new_dh_agent"
    password: SecretStr = SecretStr("")
    name: str = "dh_agent"
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=20, ge=0, le=200)
    pool_recycle_seconds: int = Field(default=1800, ge=60)
    connect_timeout: float = Field(default=10, gt=0)
    migration_on_startup: bool = True
    migration_lock_timeout: int = Field(default=60, ge=1, le=600)

    @field_validator("name")
    @classmethod
    def validate_database_name(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_]+", value) is None:
            raise ValueError("database name may only contain letters, digits and underscores")
        return value


class SensitiveWordSettings(BaseModel):
    enabled: bool = True


class DictionaryRefreshSettings(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=10)
    jitter_seconds: int = Field(default=30, ge=0, le=300)


class DictionarySettings(BaseModel):
    base_url: str = "http://127.0.0.1:8082"
    cache_directory: str = "data/dictionaries"
    timeout: float = Field(default=10, gt=0)
    connect_timeout: float = Field(default=3, gt=0)
    verify_ssl: bool = True
    refresh: DictionaryRefreshSettings = Field(
        default_factory=DictionaryRefreshSettings
    )

    def resolved_cache_directory(self) -> Path:
        path = Path(self.cache_directory)
        return path if path.is_absolute() else PROJECT_DIR / path


class SkillSettings(BaseModel):
    directory: str = "skills"

    def resolved_directory(self) -> Path:
        path = Path(self.directory)
        return path if path.is_absolute() else PROJECT_DIR / path


class StreamSettings(BaseModel):
    include_process: bool = True
    include_tool_result: bool = False


class LoggingSettings(BaseModel):
    level: str = "INFO"
    directory: str = "logs"
    retention_days: int = Field(default=14, ge=1, le=365)


class RerankSettings(BaseModel):
    enabled: bool = True
    base_url: str = ""
    path: str = "/v1/rerank"
    url: str = ""
    model: str = "Qwen3-Reranker-4B"
    candidate_top_n: int = Field(default=100, ge=1)
    timeout: float = Field(default=15, gt=0)
    connect_timeout: float = Field(default=5, gt=0)
    max_retries: int = Field(default=1, ge=0)
    retry_backoff: float = Field(default=0.3, ge=0)
    verify_ssl: bool = True


class RagToolSettings(BaseModel):
    base_url: str = ""
    recall_path: str = "/rag-platform-default/zsgl/knowledgeRecall"
    recall_url: str = ""
    api_token: SecretStr = SecretStr("")
    tenant_id: str = ""
    knowledge_ids: list[str] = Field(default_factory=list)
    dept_ids: list[str] = Field(default_factory=list)
    score: float = Field(default=0.6, ge=0, le=1)
    top_n: int = Field(default=20, ge=1)
    faq_score: float = Field(default=0.8, ge=0, le=1)
    search_method: int = 3
    search_scope: str = "knowledge"
    timeout: float = Field(default=30, gt=0)
    connect_timeout: float = Field(default=5, gt=0)
    connection_limit: int = Field(default=100, ge=1)
    verify_ssl: bool = True
    max_retries: int = Field(default=2, ge=0)
    retry_backoff: float = Field(default=0.5, ge=0)
    rerank: RerankSettings = Field(default_factory=RerankSettings)

    def resolved_recall_url(self) -> str:
        if self.recall_url.strip():
            return self.recall_url.strip()
        if not self.base_url.strip():
            return ""
        return f"{self.base_url.rstrip('/')}/{self.recall_path.lstrip('/')}"

    def resolved_rerank_url(self) -> str:
        if self.rerank.url.strip():
            return self.rerank.url.strip()
        base_url = self.rerank.base_url.strip() or self.base_url.strip()
        if not base_url:
            return ""
        return f"{base_url.rstrip('/')}/{self.rerank.path.lstrip('/')}"


class SqlLlmSettings(BaseModel):
    base_url: str = ""
    model: str = ""
    api_key: SecretStr = SecretStr("")
    temperature: float = Field(default=0, ge=0, le=2)
    timeout: float = Field(default=120, gt=0)


class DatabaseToolSettings(BaseModel):
    information_dir: str = "database_information"
    query_guide_file: str = "数据库_AI_SQL查询指南.md"
    host: str = "127.0.0.1"
    port: int = Field(default=3306, ge=1, le=65535)
    user: str = "readonly_user"
    password: SecretStr = SecretStr("")
    name: str = "donghai_dwd"
    connect_timeout: float = Field(default=5, gt=0)
    query_timeout: float = Field(default=10, gt=0)
    max_rows: int = Field(default=1000, ge=1)
    pool_max_size: int = Field(default=3, ge=1)
    ssl_enabled: bool = False
    ssl_ca: Optional[str] = None
    ssl_verify_cert: bool = True
    sql_llm: SqlLlmSettings = Field(default_factory=SqlLlmSettings)


class SpatialToolSettings(BaseModel):
    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    timeout: float = Field(default=30, gt=0)
    connect_timeout: float = Field(default=5, gt=0)
    verify_ssl: bool = True
    max_retries: int = Field(default=1, ge=0, le=5)
    retry_backoff: float = Field(default=0.3, ge=0, le=10)


class ToolSettings(BaseModel):
    execution_timeout: float = Field(default=300, gt=0)
    rag: RagToolSettings = Field(default_factory=RagToolSettings)
    database: DatabaseToolSettings = Field(default_factory=DatabaseToolSettings)
    spatial: SpatialToolSettings = Field(default_factory=SpatialToolSettings)


class Settings(BaseSettings):
    app_profile: str = Field(default_factory=get_active_profile, validation_alias="APP_PROFILE")
    server: ServerSettings = Field(default_factory=ServerSettings)
    internal_request: InternalRequestSettings
    llm: LLMSettings
    react: ReactSettings = Field(default_factory=ReactSettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    persistence: PersistenceSettings = Field(default_factory=PersistenceSettings)
    sensitive_words: SensitiveWordSettings = Field(
        default_factory=SensitiveWordSettings
    )
    dictionaries: DictionarySettings = Field(default_factory=DictionarySettings)
    skills: SkillSettings = Field(default_factory=SkillSettings)
    stream: StreamSettings = Field(default_factory=StreamSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)

    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="NEW_DH_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        profile = get_active_profile()
        base_yaml = SpringYamlConfigSettingsSource(
            settings_cls,
            yaml_file=CONFIG_DIR / "application.yaml",
            yaml_file_encoding="utf-8",
        )
        profile_file = CONFIG_DIR / f"application-{profile}.yaml"
        profile_yaml = SpringYamlConfigSettingsSource(
            settings_cls,
            yaml_file=profile_file if profile_file.exists() else None,
            yaml_file_encoding="utf-8",
        )
        application_dotenv = ApplicationDotEnvSettingsSource(
            settings_cls,
            env_file=PROJECT_DIR / ".env",
            env_file_encoding="utf-8",
        )
        return (
            init_settings,
            env_settings,
            application_dotenv,
            file_secret_settings,
            profile_yaml,
            base_yaml,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()

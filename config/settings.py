from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = CONFIG_DIR.parent


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


class LLMSettings(BaseModel):
    model: str
    base_url: str
    api_key: SecretStr = SecretStr("")
    temperature: float = Field(default=0.6, ge=0, le=2)
    timeout: float = Field(default=60, gt=0)


class ReactSettings(BaseModel):
    max_rounds: int = Field(default=5, ge=1, le=20)
    enable_think: bool = True
    force_answer_on_last_round: bool = True


class ConversationSettings(BaseModel):
    max_history_messages: int = Field(default=20, ge=1, le=100)


class StreamSettings(BaseModel):
    include_process: bool = True


class Settings(BaseSettings):
    app_profile: str = Field(default_factory=get_active_profile, validation_alias="APP_PROFILE")
    server: ServerSettings = Field(default_factory=ServerSettings)
    llm: LLMSettings
    react: ReactSettings = Field(default_factory=ReactSettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    stream: StreamSettings = Field(default_factory=StreamSettings)

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
        base_yaml = YamlConfigSettingsSource(
            settings_cls,
            yaml_file=CONFIG_DIR / "application.yaml",
            yaml_file_encoding="utf-8",
        )
        profile_file = CONFIG_DIR / f"application-{profile}.yaml"
        profile_yaml = YamlConfigSettingsSource(
            settings_cls,
            yaml_file=profile_file if profile_file.exists() else None,
            yaml_file_encoding="utf-8",
        )
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            profile_yaml,
            base_yaml,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()

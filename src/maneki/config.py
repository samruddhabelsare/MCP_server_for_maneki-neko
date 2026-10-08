"""Application configuration via pydantic-settings.

All settings are read from environment variables (or a .env file for local dev).
The app fails fast at startup if any required variable is missing.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Supabase ────────────────────────────────────────────────────────────
    supabase_url: str = Field(..., description="Supabase project URL")
    supabase_service_key: str = Field(..., description="Service-role key (server-only)")

    # ── Auth tokens ─────────────────────────────────────────────────────────
    admin_api_key: str = Field(..., description="Protects /mcp/admin and admin REST routes")
    internal_mcp_token: str = Field(..., description="Orchestrator → /mcp/customer auth")

    # ── NVIDIA (Phase 3) ────────────────────────────────────────────────────
    nvidia_api_key: str = Field(default="", description="Leave empty until M6")
    nvidia_model: str = Field(default="nvidia/nemotron-3.5-lightning-30b-a3b")
    nvidia_endpoint: str = Field(
        default="https://integrate.api.nvidia.com/v1/chat/completions"
    )

    # ── NIM streaming (Phase 1) ─────────────────────────────────────────────
    nim_stream: bool = Field(default=True, description="Enable true SSE streaming from NIM")
    nim_max_tokens: int = Field(default=200, description="max_tokens sent to NIM")

    # ── Table names ─────────────────────────────────────────────────────────
    menu_table: str = Field(default="menu_items")
    orders_table: str = Field(default="orders")
    customers_table: str = Field(default="customers")
    feedback_table: str = Field(default="feedback")

    # ── CORS ────────────────────────────────────────────────────────────────
    cors_origins: str = Field(default="http://localhost:3000")

    # ── Session / misc ──────────────────────────────────────────────────────
    session_ttl_hours: int = Field(default=6, ge=1)
    log_level: str = Field(default="INFO")
    port: int = Field(default=8000, ge=1, le=65535)

    # ── Phase 0 timing ──────────────────────────────────────────────────────
    debug_timing: bool = Field(
        default=False,
        description="If true, include a 'timing' object inside the done event data",
    )

    # ── Phase 2 concurrency ─────────────────────────────────────────────────
    history_messages: int = Field(
        default=20,
        description="Number of past messages to load for context",
    )
    tool_timeout_s: float = Field(
        default=5.0,
        description="Per-tool execution timeout in seconds (TOOL_TIMEOUT_S)",
    )

    # ── Phase 3 cache TTLs (seconds) ────────────────────────────────────────
    menu_cache_ttl: int = Field(default=45, description="Menu cache TTL in seconds")
    session_cache_ttl: int = Field(default=30, description="Session cache TTL in seconds")
    profile_cache_ttl: int = Field(default=120, description="Customer profile cache TTL in seconds")
    popularity_cache_ttl: int = Field(default=300, description="Popularity scores cache TTL in seconds")

    # ── Phase 5 prompt ──────────────────────────────────────────────────────
    menu_in_prompt_max: int = Field(
        default=60,
        description="Max available menu items before switching to categories-only prompt",
    )

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return upper

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton.

    Raises ValidationError (and exits) if any required env var is missing.
    """
    return Settings()  # type: ignore[call-arg]

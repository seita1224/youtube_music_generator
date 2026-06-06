"""アプリケーション設定 (pydantic-settings)。

`.env` / 環境変数から型付き設定を読み込む。 出典:
- specs/001-youtube-music-generator/quickstart.md §3
- .env.example
- ADR-0012 (Fernet), ADR-0013 (Basic 認証), ADR-0019 (LLM provider),
  ADR-0022 (storage 抽象), ADR-0026 (backup), ADR-0035 (dryrun default)

秘密値は ``SecretStr`` でラップしてログ・例外への漏洩を防ぐ。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["openai", "anthropic", "ollama"]
LLMAuthMode = Literal["api_key", "codex_oauth"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """環境変数 / `.env` から読み込む不変な設定オブジェクト。

    フィールド名は環境変数名の小文字版に対応する (大文字小文字非依存)。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # --- データ / ストレージ (ADR-0022, ADR-0026) ---
    data_root: str = Field(default="/srv/ymg")
    backup_root: str = Field(default="/mnt/backup/ymg")
    storage_base_uri: str = Field(default="file:///srv/ymg/outputs")

    # --- PostgreSQL ---
    postgres_host: str = Field(default="postgres")
    postgres_port: int = Field(default=5432)
    postgres_db: str = Field(default="ymg")
    postgres_user: str = Field(default="ymg")
    postgres_password: SecretStr = Field(default=SecretStr(""))

    # --- Fernet 暗号鍵 (ADR-0012) — バックアップ対象外 ---
    fernet_key: SecretStr = Field(default=SecretStr(""))

    # --- 管理 UI Basic 認証 (ADR-0013) ---
    admin_username: str = Field(default="admin")
    admin_password: SecretStr = Field(default=SecretStr(""))

    # --- LLM Provider (ADR-0019) ---
    llm_provider: LLMProvider = Field(default="openai")
    llm_auth_mode: LLMAuthMode = Field(default="api_key")
    openai_api_key: SecretStr = Field(default=SecretStr(""))
    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    ollama_base_url: str = Field(default="http://localhost:11434")

    # --- YouTube (ADR-0021) ---
    youtube_client_id: str = Field(default="")
    youtube_client_secret: SecretStr = Field(default=SecretStr(""))
    youtube_redirect_uri: str = Field(default="http://localhost:8000/auth/youtube/callback")
    youtube_channel_id: str = Field(default="")

    # --- AcoustID (ADR-0005) ---
    acoustid_api_key: SecretStr = Field(default=SecretStr(""))

    # --- Slack 通知 (単一 webhook) ---
    slack_webhook_url: SecretStr = Field(default=SecretStr(""))

    # --- GPU worker (ADR-0031) ---
    gpu_worker_base_url: str = Field(default="http://127.0.0.1:8001")

    # --- 動作モード (ADR-0035) ---
    dryrun_default: bool = Field(default=True)
    scheduler_autostart: bool = Field(default=False)
    monthly_budget_usd: float = Field(default=50.0, ge=0)
    log_level: LogLevel = Field(default="INFO")

    @model_validator(mode="after")
    def _validate_llm_auth_mode(self) -> Settings:
        """codex_oauth は OpenAI のみサポート (ADR-0019)。

        Anthropic / Ollama は API key 認証のみ。 不整合な組み合わせは
        起動時に拒否する (.env.example §LLM_AUTH_MODE 参照)。
        """
        if self.llm_auth_mode == "codex_oauth" and self.llm_provider != "openai":
            raise ValueError(
                f"llm_auth_mode='codex_oauth' は provider='openai' のみサポート対象です "
                f"(指定された provider='{self.llm_provider}')。"
            )
        return self

    @property
    def database_url(self) -> str:
        """async ドライバ (asyncpg) 用の DSN を組み立てる。

        password は URL エンコードして特殊文字を安全に扱う。
        """
        return self._build_dsn("postgresql+asyncpg")

    @property
    def sync_database_url(self) -> str:
        """sync ドライバ (psycopg) 用の DSN を組み立てる (Alembic 等)。"""
        return self._build_dsn("postgresql+psycopg")

    def _build_dsn(self, driver: str) -> str:
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return (
            f"{driver}://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """プロセス内で単一の ``Settings`` インスタンスを返す (lru_cache)。"""
    return Settings()

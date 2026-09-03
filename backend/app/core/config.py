from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Chat Workspace API"
    environment: str = "development"
    database_url: str = "mysql+pymysql://root:your-password@127.0.0.1:3306/chat_workspace?charset=utf8mb4"
    jwt_secret: str = "replace-with-a-long-random-secret"
    encryption_key: str = ""
    jwt_expire_minutes: int = 60
    cors_origins: str = "http://localhost:5173,http://localhost:4175,http://localhost:4176,http://localhost:4177,http://localhost:4178,http://localhost:4179,http://localhost:4180"
    admin_email: str = "admin@example.com"
    admin_password: str = "change-me-now"
    storage_dir: str = "./data/assets"
    model_timeout_seconds: int = 90
    rate_limit_window_seconds: int = 60
    rate_limit_auth_requests: int = 20
    rate_limit_model_requests: int = 30
    asset_cleanup_days: int = 30
    # JSON array of channel definitions used for first boot. Secrets stay in
    # environment/configuration and are encrypted before persistence.
    model_channels_json: str = ""
    model_tool_max_rounds: int = 2
    model_tool_max_calls: int = 4
    model_max_reference_bytes: int = 32 * 1024 * 1024
    model_max_image_bytes: int = 25 * 1024 * 1024

    model_config = SettingsConfigDict(env_file=".env", env_prefix="CHAT_", extra="ignore", protected_namespaces=("settings_",))

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""系统配置：全部从环境变量读取，便于 Docker / 生产部署。

兼容 Python 3.10+（不使用 datetime.UTC 等 3.11+ 语法）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_SECRET = "change-me-in-production"


@dataclass
class Settings:
    app_name: str = "一二矿山 生产协同系统"
    # 运行环境：production（默认，最严格）/ demo（演示：允许演示数据与默认账号）/ development / test
    # 不设置就按 production 处理，宁可启动失败也不带着默认密码上线。
    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "production").strip().lower())
    # 默认 SQLite 方便演示；生产用 MySQL：mysql+pymysql://user:pwd@host:3306/prodsync?charset=utf8mb4
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'prodsync.db'}"))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", DEFAULT_SECRET))
    token_expire_minutes: int = field(default_factory=lambda: int(os.getenv("TOKEN_EXPIRE_MINUTES", "720")))
    upload_dir: Path = field(default_factory=lambda: Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads"))))
    cors_origins: list[str] = field(default_factory=lambda: os.getenv("CORS_ORIGINS", "*").split(","))

    # 齐套参数
    kit_buffer_days: int = field(default_factory=lambda: int(os.getenv("KIT_BUFFER_DAYS", "2")))  # 提前 2 天预判
    stagnant_days: int = field(default_factory=lambda: int(os.getenv("STAGNANT_DAYS", "180")))  # 呆滞判定天数
    edit_lock_minutes: int = field(default_factory=lambda: int(os.getenv("EDIT_LOCK_MINUTES", "15")))

    # AI（可插拔，默认关闭；OpenAI 兼容协议：DeepSeek / 通义千问 / 本地 Ollama 等）
    ai_enabled: bool = field(default_factory=lambda: _bool("AI_ENABLED", False))
    ai_base_url: str = field(default_factory=lambda: os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1"))
    ai_api_key: str = field(default_factory=lambda: os.getenv("AI_API_KEY", ""))
    ai_model: str = field(default_factory=lambda: os.getenv("AI_MODEL", "deepseek-chat"))
    ai_timeout: float = field(default_factory=lambda: float(os.getenv("AI_TIMEOUT", "30")))

    # 启动时自动建表（仅开发/演示；生产请用 alembic upgrade head）
    auto_create_tables: bool = field(default_factory=lambda: _bool("AUTO_CREATE_TABLES", os.getenv("APP_ENV", "production").strip().lower() != "production"))

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def allows_demo(self) -> bool:
        return self.app_env in {"demo", "development", "test"}


settings = Settings()

"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Telegram
    telegram_token: str = ""
    admin_chat_id: str = ""

    # Fees
    fees_wallet: str = ""
    platform_fee_rate: float = Field(default=0.01, ge=0, le=0.1)

    # Encryption
    encryption_key: str = ""

    # Monitoring
    # For aggressive copy-trading, you can go down to 1s
    monitor_poll_interval: int = Field(default=15, ge=1, le=120)

    # DB & Redis
    db_url: str = "sqlite+aiosqlite:///./polybot.db"
    redis_url: str = "redis://redis:6379"

    # Bridge / On-ramp
    lifi_api_key: str = ""
    across_api_url: str = "https://across.to/api"
    bridge_slippage: float = 0.005
    transak_api_key: str = ""

    # Execution tuning
    max_concurrent_trades: int = Field(default=20, ge=1, le=200)
    collect_fees_onchain: bool = False

    # Polygon / Web3
    # RPC dédié pour Polygon (Alchemy, QuickNode, Ankr, etc.)
    # Exemple : https://polygon-mainnet.g.alchemy.com/v2/VOTRE_CLE
    polygon_rpc_url: str = ""

    # Dashboard web
    dashboard_enabled: bool = True
    dashboard_port: int = Field(default=8080, ge=1024, le=65535)

    # UI / Branding
    # URL d'une bannière (logo) pour le message d'accueil Telegram
    welcome_banner_url: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

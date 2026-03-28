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
    # Default 2s for fast copy-trading. WebSocket handles instant detection;
    # polling is the safety net. Set MONITOR_POLL_INTERVAL=1 for fastest.
    monitor_poll_interval: int = Field(default=2, ge=1, le=120)

    # DB & Redis
    postgres_password: str = ""  # used by docker-compose for PostgreSQL container
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
    # URL publique du dashboard (si déployé derrière un reverse proxy)
    # Ex: https://dashboard.monbot.com — sinon on affiche localhost:<port>
    dashboard_url: str = ""

    # UI / Branding
    # URL d'une bannière (logo) pour le message d'accueil Telegram
    welcome_banner_url: str = ""

    # V3 — Telegram Group with Topics
    # Create a Forum-enabled group, add the bot as admin, then fill these IDs.
    # Leave empty to keep DM-only mode (backwards compatible).
    group_chat_id: str = ""          # Telegram group ID (e.g. -100xxxxxxxxxx)
    topic_signals_id: int = 0        # Thread ID for 📊 Signals topic
    topic_traders_id: int = 0        # Thread ID for 👤 Traders topic
    topic_portfolio_id: int = 0      # Thread ID for 💼 Portfolio topic
    topic_alerts_id: int = 0         # Thread ID for 🚨 Alerts topic
    topic_admin_id: int = 0          # Thread ID for ⚙️ Admin topic

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

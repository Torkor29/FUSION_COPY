"""SQLAlchemy models — import all to register with Base.metadata."""

from .base import Base, utcnow  # noqa: F401
from .user import User, UserRole  # noqa: F401
from .settings import UserSettings, SizingMode, GasMode  # noqa: F401
from .user_wallet import UserWallet  # noqa: F401
from .trade import Trade, TradeStatus, TradeSide  # noqa: F401
from .fee import FeeRecord  # noqa: F401

# V3 — Smart Analysis models
from .signal_score import SignalScore  # noqa: F401
from .trader_stats import TraderStats  # noqa: F401
from .market_intel import MarketIntel  # noqa: F401
from .active_position import ActivePosition  # noqa: F401
from .trader_market_history import TraderMarketHistory  # noqa: F401
from .group_config import GroupConfig  # noqa: F401

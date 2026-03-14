"""User service — CRUD operations for users and settings."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models.user import User, UserRole
from bot.models.settings import UserSettings, SizingMode
from bot.models.user_wallet import UserWallet
from bot.services.crypto import encrypt_private_key
from bot.config import settings


async def get_user_by_telegram_id(
    session: AsyncSession, telegram_id: int
) -> Optional[User]:
    """Fetch a user by their Telegram ID."""
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession,
    telegram_id: int,
    username: Optional[str] = None,
    role: UserRole = UserRole.FOLLOWER,
) -> User:
    """Create a new user with default settings."""
    user = User(
        telegram_id=telegram_id,
        telegram_username=username,
        role=role,
    )
    session.add(user)
    await session.flush()

    # Create default settings
    user_settings = UserSettings(user_id=user.id)
    session.add(user_settings)
    await session.commit()
    await session.refresh(user)
    return user


async def save_wallet(
    session: AsyncSession,
    user: User,
    wallet_address: str,
    private_key: str,
    chain: str = "polygon",
) -> None:
    """Encrypt and save a wallet's private key for a user.

    IMPORTANT: For Polygon wallets, the canonical address is ALWAYS derived
    from the private key (not the user-provided address) to guarantee that
    the stored address matches the key that will sign transactions.
    """
    # Derive the canonical address from PK — this is the ONLY source of truth
    if chain == "polygon":
        from eth_account import Account
        derived_address = Account.from_key(private_key).address
        if derived_address.lower() != wallet_address.lower():
            logger.warning(
                f"save_wallet: user-provided address {wallet_address[:10]}... "
                f"differs from PK-derived {derived_address[:10]}... "
                f"— using PK-derived address"
            )
        wallet_address = derived_address

    encrypted = encrypt_private_key(
        private_key, settings.encryption_key, user.uuid
    )

    if chain == "polygon":
        # Marquer les anciens wallets comme non principaux
        if user.wallets:
            for w in user.wallets:
                if w.chain == "polygon" and w.is_primary:
                    w.is_primary = False

        # Créer un nouvel enregistrement de wallet comme principal
        new_wallet = UserWallet(
            user_id=user.id,
            chain="polygon",
            address=wallet_address,
            auto_created=user.wallet_auto_created,
            is_primary=True,
            encrypted_key=encrypted,
        )
        session.add(new_wallet)

        # Mettre à jour les champs historiques pour le moteur existant
        user.wallet_address = wallet_address
        user.encrypted_private_key = encrypted

    elif chain == "solana":
        user.solana_wallet_address = wallet_address
        user.encrypted_solana_key = encrypted

    await session.commit()


async def get_or_create_settings(
    session: AsyncSession, user: User
) -> UserSettings:
    """Get user settings, creating defaults if missing."""
    if user.settings:
        return user.settings
    user_settings = UserSettings(user_id=user.id)
    session.add(user_settings)
    await session.commit()
    await session.refresh(user)
    return user.settings


async def update_setting(
    session: AsyncSession,
    user_settings: UserSettings,
    field: str,
    value,
) -> None:
    """Update a single setting field."""
    setattr(user_settings, field, value)
    await session.commit()


async def get_all_active_followers(
    session: AsyncSession,
) -> list[User]:
    """Get all active, non-paused followers."""
    result = await session.execute(
        select(User).where(
            User.role == UserRole.FOLLOWER,
            User.is_active == True,
            User.is_paused == False,
        )
    )
    return list(result.scalars().all())


async def get_followers_of_wallet(
    session: AsyncSession, master_wallet: str
) -> list[User]:
    """Get all active followers who follow a specific master wallet."""
    all_followers = await get_all_active_followers(session)
    wallet_lower = master_wallet.lower()
    result = []
    for user in all_followers:
        if user.settings and user.settings.followed_wallets:
            if wallet_lower in [w.lower() for w in user.settings.followed_wallets]:
                result.append(user)
    return result


async def get_all_followed_wallets(session: AsyncSession) -> set[str]:
    """Get the union of all followed wallets across all active users."""
    followers = await get_all_active_followers(session)
    wallets: set[str] = set()
    for user in followers:
        if user.settings and user.settings.followed_wallets:
            for w in user.settings.followed_wallets:
                if w:
                    wallets.add(w.lower())
    return wallets


async def get_admin_stats(session: AsyncSession) -> dict:
    """Get aggregate stats for admin panel."""
    from sqlalchemy import func
    from bot.models.trade import Trade, TradeStatus
    from bot.models.fee import FeeRecord

    # Active followers
    follower_count = await session.scalar(
        select(func.count(User.id)).where(
            User.role == UserRole.FOLLOWER,
            User.is_active == True,
        )
    )

    # Total trades this month
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    trade_count = await session.scalar(
        select(func.count(Trade.id)).where(
            Trade.created_at >= month_start,
            Trade.status == TradeStatus.FILLED,
        )
    ) or 0

    # Total volume
    total_volume = await session.scalar(
        select(func.sum(Trade.gross_amount_usdc)).where(
            Trade.status == TradeStatus.FILLED,
        )
    ) or 0.0

    # Total fees collected
    total_fees = await session.scalar(
        select(func.sum(FeeRecord.fee_amount)).where(
            FeeRecord.confirmed_on_chain == True,
        )
    ) or 0.0

    return {
        "follower_count": follower_count or 0,
        "trade_count": trade_count,
        "total_volume": total_volume,
        "total_fees": total_fees,
    }

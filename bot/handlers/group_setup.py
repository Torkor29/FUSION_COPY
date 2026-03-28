"""Group auto-setup handler — creates forum topics when bot is added as admin.

Flow:
1. Bot is added to a group → my_chat_member update fires
2. Check if bot has admin rights + group is a forum (supergroup with topics enabled)
3. Auto-create 5 topics: Signals, Traders, Portfolio, Alerts, Admin
4. Store topic IDs in GroupConfig DB table
5. Notify TopicRouter to reload config
6. Pin a welcome message in the Admin topic

Telegram API requirements:
- Group must be a supergroup with "Topics" enabled in group settings
- Bot must be admin with "Manage Topics" permission
"""

import logging
from telegram import Update, ChatMemberUpdated, ChatMember
from telegram.ext import ContextTypes, ChatMemberHandler

from bot.db.session import async_session
from bot.models.group_config import GroupConfig

logger = logging.getLogger(__name__)

# Topic definitions with name, icon (emoji) and color
# Telegram forum topic icon colors (these are the available icon_color values):
# 0x6FB9F0 (blue), 0xFFD67E (yellow), 0xCB86DB (purple),
# 0x8EEE98 (green), 0xFF93B2 (pink), 0xFB6F5F (red)
TOPICS_TO_CREATE = [
    {"name": "📊 Signals",   "icon_color": 0x6FB9F0},  # Blue
    {"name": "👤 Traders",   "icon_color": 0x8EEE98},  # Green
    {"name": "💼 Portfolio",  "icon_color": 0xFFD67E},  # Yellow
    {"name": "🚨 Alerts",    "icon_color": 0xFB6F5F},  # Red
    {"name": "⚙️ Admin",     "icon_color": 0xCB86DB},  # Purple
]

# Map topic name prefix to DB field
TOPIC_FIELD_MAP = {
    "📊 Signals": "topic_signals_id",
    "👤 Traders": "topic_traders_id",
    "💼 Portfolio": "topic_portfolio_id",
    "🚨 Alerts": "topic_alerts_id",
    "⚙️ Admin": "topic_admin_id",
}


def _is_bot_promoted_to_admin(update: ChatMemberUpdated) -> bool:
    """Check if the bot was just promoted to admin (or added as admin)."""
    old = update.old_chat_member
    new = update.new_chat_member

    # Bot was not admin before, now is admin
    old_is_admin = old.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    new_is_admin = new.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)

    # Also catch: bot was added directly as admin (old = left/kicked, new = admin)
    was_absent = old.status in (ChatMember.LEFT, ChatMember.BANNED, ChatMember.RESTRICTED)

    return (not old_is_admin and new_is_admin) or (was_absent and new_is_admin)


def _is_bot_added_to_group(update: ChatMemberUpdated) -> bool:
    """Check if bot was just added to a group (even as regular member)."""
    old = update.old_chat_member
    new = update.new_chat_member

    was_absent = old.status in (ChatMember.LEFT, ChatMember.BANNED)
    now_present = new.status in (
        ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER
    )

    return was_absent and now_present


async def handle_bot_chat_member(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle bot being added to a group or promoted to admin.

    This fires on my_chat_member updates (changes to the BOT's membership).
    """
    if not update.my_chat_member:
        return

    member_update = update.my_chat_member
    chat = member_update.chat

    # Only handle supergroups (forums are supergroups with topics enabled)
    if chat.type not in ("supergroup", "group"):
        return

    bot_id = context.bot.id
    if member_update.new_chat_member.user.id != bot_id:
        return

    # Case 1: Bot added to group (not yet admin)
    if _is_bot_added_to_group(member_update) and not _is_bot_promoted_to_admin(member_update):
        logger.info(
            "Bot added to group '%s' (%s) as member — waiting for admin promotion",
            chat.title, chat.id,
        )
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    "👋 *Salut ! Je suis WenBot V3.*\n\n"
                    "Pour activer l'auto-setup des topics, il me faut :\n"
                    "1. Être *administrateur* du groupe\n"
                    "2. Permission *Gérer les topics*\n"
                    "3. Le groupe doit avoir les *Topics activés* "
                    "(Paramètres → Topics → Activer)\n\n"
                    "Une fois admin, je créerai automatiquement les 5 topics "
                    "d'analyse ! 🚀"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Failed to send welcome message: %s", e)
        return

    # Case 2: Bot promoted to admin → auto-setup!
    if _is_bot_promoted_to_admin(member_update):
        logger.info(
            "Bot promoted to admin in '%s' (%s) — starting auto-setup",
            chat.title, chat.id,
        )
        await _auto_setup_topics(context.bot, chat.id, chat.title or "Group")


async def _auto_setup_topics(bot, group_id: int, group_title: str) -> None:
    """Create the 5 forum topics and store their IDs in the database."""

    # Check if group is a forum (has topics enabled)
    try:
        chat_info = await bot.get_chat(group_id)
        is_forum = getattr(chat_info, "is_forum", False)
    except Exception as e:
        logger.error("Failed to get chat info for %s: %s", group_id, e)
        await bot.send_message(
            chat_id=group_id,
            text="❌ Impossible de vérifier les paramètres du groupe.",
        )
        return

    if not is_forum:
        await bot.send_message(
            chat_id=group_id,
            text=(
                "⚠️ *Topics non activés !*\n\n"
                "Pour l'auto-setup, activez les topics :\n"
                "Paramètres du groupe → Topics → *Activer*\n\n"
                "Puis retirez-moi et re-ajoutez-moi comme admin."
            ),
            parse_mode="Markdown",
        )
        return

    # Check if already setup for this group
    async with async_session() as session:
        from sqlalchemy import select
        existing = (
            await session.execute(
                select(GroupConfig).where(GroupConfig.group_id == group_id)
            )
        ).scalar_one_or_none()

        if existing and existing.setup_complete:
            logger.info("Group %s already setup — skipping", group_id)
            await bot.send_message(
                chat_id=group_id,
                text="✅ Ce groupe est déjà configuré ! Topics existants réutilisés.",
            )
            return

    # Send progress message
    try:
        progress_msg = await bot.send_message(
            chat_id=group_id,
            text="🔧 *Auto-setup en cours...*\n\nCréation des topics...",
            parse_mode="Markdown",
        )
    except Exception:
        progress_msg = None

    # Create each topic
    created_topics = {}
    for i, topic_def in enumerate(TOPICS_TO_CREATE):
        try:
            result = await bot.create_forum_topic(
                chat_id=group_id,
                name=topic_def["name"],
                icon_color=topic_def.get("icon_color"),
            )
            field_name = TOPIC_FIELD_MAP.get(topic_def["name"])
            if field_name:
                created_topics[field_name] = result.message_thread_id

            logger.info(
                "Created topic '%s' (thread_id=%s) in group %s",
                topic_def["name"], result.message_thread_id, group_id,
            )

            # Update progress
            if progress_msg:
                done = i + 1
                bar = "✅" * done + "⬜" * (len(TOPICS_TO_CREATE) - done)
                try:
                    await progress_msg.edit_text(
                        f"🔧 *Auto-setup en cours...*\n\n"
                        f"Topics: {bar} ({done}/{len(TOPICS_TO_CREATE)})",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(
                "Failed to create topic '%s' in group %s: %s",
                topic_def["name"], group_id, e,
            )
            created_topics[TOPIC_FIELD_MAP.get(topic_def["name"], "")] = None

    # Save to database
    async with async_session() as session:
        from sqlalchemy import select
        config = (
            await session.execute(
                select(GroupConfig).where(GroupConfig.group_id == group_id)
            )
        ).scalar_one_or_none()

        if not config:
            config = GroupConfig(group_id=group_id)
            session.add(config)

        config.group_title = group_title
        config.is_forum = True
        config.topic_signals_id = created_topics.get("topic_signals_id")
        config.topic_traders_id = created_topics.get("topic_traders_id")
        config.topic_portfolio_id = created_topics.get("topic_portfolio_id")
        config.topic_alerts_id = created_topics.get("topic_alerts_id")
        config.topic_admin_id = created_topics.get("topic_admin_id")
        config.setup_complete = config.all_topics_created
        config.is_active = True

        await session.commit()

        logger.info(
            "GroupConfig saved: group=%s topics=%s complete=%s",
            group_id, config.topics_dict, config.setup_complete,
        )

    # Send welcome messages in each topic
    if created_topics.get("topic_admin_id"):
        try:
            await bot.send_message(
                chat_id=group_id,
                message_thread_id=created_topics["topic_admin_id"],
                text=(
                    "⚙️ *Admin Topic*\n\n"
                    "Ce topic reçoit :\n"
                    "• Statut du bot (start/stop)\n"
                    "• Health checks\n"
                    "• Rapports de fees\n"
                    "• Erreurs système"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    if created_topics.get("topic_signals_id"):
        try:
            await bot.send_message(
                chat_id=group_id,
                message_thread_id=created_topics["topic_signals_id"],
                text=(
                    "📊 *Signals Topic*\n\n"
                    "Chaque signal reçoit un score 0-100 :\n"
                    "🟢 75+ = EXCELLENT — forte probabilité\n"
                    "🟡 50-74 = BON — signal correct\n"
                    "🟠 30-49 = FAIBLE — risqué\n"
                    "🔴 <30 = IGNORÉ — trade non copié\n\n"
                    "Breakdown: spread, liquidité, conviction, forme trader, timing, consensus"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    if created_topics.get("topic_alerts_id"):
        try:
            await bot.send_message(
                chat_id=group_id,
                message_thread_id=created_topics["topic_alerts_id"],
                text=(
                    "🚨 *Alerts Topic*\n\n"
                    "Alertes critiques :\n"
                    "• 🔴 Stop-Loss déclenché\n"
                    "• 🟢 Take-Profit atteint\n"
                    "• 🟡 Trailing Stop activé\n"
                    "• ⏰ Time Exit (position flat)\n"
                    "• ⚠️ Trader auto-pausé (cold streak)"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    if created_topics.get("topic_traders_id"):
        try:
            await bot.send_message(
                chat_id=group_id,
                message_thread_id=created_topics["topic_traders_id"],
                text=(
                    "👤 *Traders Topic*\n\n"
                    "Rapports de performance des traders suivis :\n"
                    "• Win rate 7j / 30j\n"
                    "• PNL par catégorie\n"
                    "• Hot/Cold streaks\n"
                    "• Mise à jour toutes les 15 min"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    if created_topics.get("topic_portfolio_id"):
        try:
            await bot.send_message(
                chat_id=group_id,
                message_thread_id=created_topics["topic_portfolio_id"],
                text=(
                    "💼 *Portfolio Topic*\n\n"
                    "Vue d'ensemble du portfolio :\n"
                    "• Positions ouvertes + PNL\n"
                    "• Exposure par catégorie\n"
                    "• Direction bias (YES/NO)\n"
                    "• Rapport quotidien à 8h UTC"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    # Final success message
    if progress_msg:
        try:
            n_created = sum(1 for v in created_topics.values() if v)
            await progress_msg.edit_text(
                f"✅ *Auto-setup terminé !*\n\n"
                f"*{n_created}/{len(TOPICS_TO_CREATE)}* topics créés avec succès.\n\n"
                f"📊 Signals — signaux scorés\n"
                f"👤 Traders — analytics traders\n"
                f"💼 Portfolio — vue portfolio\n"
                f"🚨 Alerts — alertes SL/TP\n"
                f"⚙️ Admin — système\n\n"
                f"_Le bot est prêt ! Les commandes privées restent en DM._",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    # Reload TopicRouter if available in context
    # The TopicRouter will auto-reload from DB next time it's accessed


async def reload_topic_router_from_db(topic_router) -> bool:
    """Reload TopicRouter configuration from the database.

    Call this after auto-setup or when group config changes.
    Returns True if config was found and loaded.
    """
    async with async_session() as session:
        from sqlalchemy import select
        config = (
            await session.execute(
                select(GroupConfig)
                .where(GroupConfig.is_active == True)  # noqa: E712
                .where(GroupConfig.setup_complete == True)  # noqa: E712
                .order_by(GroupConfig.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if not config:
            return False

        topic_router._group_id = config.group_id
        topic_router._topics = config.topics_dict
        topic_router._enabled = True

        logger.info(
            "TopicRouter reloaded from DB: group=%s topics=%s",
            config.group_id,
            {k: v for k, v in config.topics_dict.items() if v},
        )
        return True


def get_group_setup_handler():
    """Return the handler for auto-setup when bot is added to a group."""
    return ChatMemberHandler(
        handle_bot_chat_member,
        ChatMemberHandler.MY_CHAT_MEMBER,
    )

# ---------------------------------------------------------------------------
# google_chat.py — Google Chat webhook for admin notifications.
#
# WHAT THIS FILE DOES:
# Sends notifications to a Google Chat space (like Slack) when admin
# actions happen — specifically when AI influencers are banned or unbanned.
# This lets the admin team see actions in real-time without checking the app.
#
# HOW IT WORKS:
# Google Chat webhooks are simple — just POST a JSON payload with a "text"
# field to a webhook URL. No OAuth, no SDK, just one HTTP call.
#
# SETUP:
# 1. In Google Chat, create a space for admin notifications
# 2. Go to space settings → Apps & integrations → Webhooks
# 3. Create a webhook, copy the URL
# 4. Set it as GOOGLE_CHAT_WEBHOOK_URL in GitHub Secrets
#
# PORTED FROM: yral-ai-chat/src/services/google_chat.rs
# ---------------------------------------------------------------------------

import logging
import httpx
import config

logger = logging.getLogger(__name__)


async def send_message(text: str):
    """
    Send a text message to the Google Chat webhook.

    If the webhook URL is not configured, this silently does nothing.
    If the webhook call fails, it logs a warning but never crashes the app.
    Admin notifications are best-effort — failure shouldn't affect users.
    """
    webhook_url = config.GOOGLE_CHAT_WEBHOOK_URL
    if not webhook_url:
        logger.debug("Google Chat webhook not configured — skipping notification")
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                webhook_url,
                json={"text": text},
            )
            if response.status_code >= 400:
                logger.error(
                    f"Google Chat webhook failed: HTTP {response.status_code}"
                )
    except Exception as e:
        logger.error(f"Google Chat webhook error: {e}")


async def notify_influencer_banned(influencer_id: str, influencer_name: str):
    """Notify the admin team that an AI influencer was banned."""
    await send_message(
        f"🚫 AI Influencer banned\nID: {influencer_id}\nName: {influencer_name}"
    )


async def notify_influencer_ban_failed(influencer_id: str, error: str):
    """Notify the admin team that banning an AI influencer failed."""
    await send_message(
        f"❌ Failed to ban AI Influencer\nID: {influencer_id}\nError: {error}"
    )


async def notify_influencer_unbanned(influencer_id: str, influencer_name: str):
    """Notify the admin team that an AI influencer was unbanned."""
    await send_message(
        f"✅ AI Influencer unbanned\nID: {influencer_id}\nName: {influencer_name}"
    )


async def notify_influencer_unban_failed(influencer_id: str, error: str):
    """Notify the admin team that unbanning an AI influencer failed."""
    await send_message(
        f"❌ Failed to unban AI Influencer\nID: {influencer_id}\nError: {error}"
    )

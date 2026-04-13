# ---------------------------------------------------------------------------
# push_notifications.py — Send push notifications via the metadata server.
#
# WHAT THIS FILE DOES:
# After the AI responds to a user's message, we send a push notification
# to the user's mobile device so they know they have a new message (even
# if the app is closed).
#
# HOW IT WORKS:
# We make an HTTP POST to the YRAL metadata server, which handles the
# actual delivery via Firebase Cloud Messaging (FCM) to iOS/Android.
#
# This runs as a BACKGROUND TASK — it doesn't block the API response.
# If it fails, the user just doesn't get a push notification (not critical).
#
# PORTED FROM: yral-ai-chat/src/routes/chat.rs (spawn_notifications function)
# ---------------------------------------------------------------------------

import logging
import httpx
import config

logger = logging.getLogger(__name__)


async def send_new_message_notification(
    user_id: str,
    influencer_name: str,
    message_content: str,
    conversation_id: str,
    influencer_id: str,
):
    """
    Send a push notification to a user about a new AI message.

    PARAMETERS:
        user_id: The user to notify (their principal ID)
        influencer_name: Display name of the AI influencer ("Ahaan Sharma")
        message_content: The AI's response text (truncated to 100 chars)
        conversation_id: Which conversation the message is in
        influencer_id: Which influencer sent the message

    This function never raises — it logs errors and returns silently.
    Push notifications are best-effort; failure shouldn't crash the app.
    """
    if not config.METADATA_URL or not config.METADATA_AUTH_TOKEN:
        logger.debug("Push notifications not configured (METADATA_URL or auth token missing)")
        return

    # Truncate the message preview (mobile notifications have limited space)
    preview = message_content[:100]
    if len(message_content) > 100:
        preview += "..."

    url = f"{config.METADATA_URL}/notifications/{user_id}/send"
    payload = {
        "data": {
            "title": f"New message from {influencer_name}",
            "body": preview,
            "conversation_id": conversation_id,
            "influencer_id": influencer_id,
            "type": "chat_message",
        }
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {config.METADATA_AUTH_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            if response.status_code >= 400:
                logger.warning(
                    f"Push notification failed: {response.status_code} "
                    f"for user {user_id}"
                )
    except Exception as e:
        logger.warning(f"Push notification error (non-fatal): {e}")

# ---------------------------------------------------------------------------
# ai_client.py — AI model integration (Gemini Flash + OpenRouter).
#
# WHAT THIS FILE DOES:
# Handles all communication with AI models:
#   1. Generating chat responses (the core chat flow)
#   2. Extracting memories from conversations (background task)
#   3. Transcribing audio messages (voice-to-text)
#
# HOW IT WORKS:
# We use the OpenAI Python SDK with a CUSTOM BASE URL pointing to Gemini's
# OpenAI-compatible API endpoint. This means we get to use the well-tested
# OpenAI SDK instead of Google's custom SDK, while still using Gemini models.
#
# For NSFW influencers, we route to OpenRouter instead (fewer content restrictions).
#
# PORTED FROM: yral-ai-chat/src/services/ai.rs
# ---------------------------------------------------------------------------

import json
import base64
import logging
from typing import Optional

import httpx
from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)

# The message the AI sends when it can't generate a real response.
# This matches the Rust service's FALLBACK_ERROR_MESSAGE.
FALLBACK_ERROR_MESSAGE = "I'm having trouble responding right now. Please try again in a moment."


# ---------------------------------------------------------------------------
# AI CLIENT INSTANCES
# ---------------------------------------------------------------------------
# We create two OpenAI client instances:
#   1. gemini_client — for normal (non-NSFW) influencers
#   2. openrouter_client — for NSFW influencers (fewer content restrictions)
#
# Both use the OpenAI-compatible API format, just with different base URLs.

def _create_gemini_client() -> AsyncOpenAI | None:
    """Create a Gemini AI client if the API key is configured."""
    if not config.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set — AI chat will not work")
        return None
    return AsyncOpenAI(
        api_key=config.GEMINI_API_KEY,
        base_url=config.GEMINI_BASE_URL,
    )


def _create_openrouter_client() -> AsyncOpenAI | None:
    """Create an OpenRouter AI client if the API key is configured."""
    if not config.OPENROUTER_API_KEY:
        logger.info("OPENROUTER_API_KEY not set — NSFW routing disabled")
        return None
    return AsyncOpenAI(
        api_key=config.OPENROUTER_API_KEY,
        base_url=config.OPENROUTER_BASE_URL,
        default_headers={
            "HTTP-Referer": "https://yral.com",
            "X-Title": "Yral AI Chat",
        },
    )


# Lazy-initialize clients on first use
_gemini_client: AsyncOpenAI | None = None
_openrouter_client: AsyncOpenAI | None = None


def get_gemini_client() -> AsyncOpenAI | None:
    """Get or create the Gemini client."""
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = _create_gemini_client()
    return _gemini_client


def get_openrouter_client() -> AsyncOpenAI | None:
    """Get or create the OpenRouter client."""
    global _openrouter_client
    if _openrouter_client is None:
        _openrouter_client = _create_openrouter_client()
    return _openrouter_client


def get_client_for_influencer(is_nsfw: bool) -> tuple[AsyncOpenAI | None, str, int, float]:
    """
    Select the right AI client based on whether the influencer is NSFW.

    RETURNS: (client, model_name, max_tokens, temperature)

    For NSFW influencers: uses OpenRouter if configured, falls back to Gemini.
    For normal influencers: always uses Gemini.
    """
    if is_nsfw:
        client = get_openrouter_client()
        if client:
            return (client, config.OPENROUTER_MODEL,
                    config.OPENROUTER_MAX_TOKENS, config.OPENROUTER_TEMPERATURE)
    # Default to Gemini
    return (get_gemini_client(), config.GEMINI_MODEL,
            config.GEMINI_MAX_TOKENS, config.GEMINI_TEMPERATURE)


# ---------------------------------------------------------------------------
# CORE FUNCTION: Generate AI Response
# ---------------------------------------------------------------------------

def _build_user_content(text: str | None, media_urls: list[str] | None) -> str | list:
    """
    Build the content payload for a user message.

    If the message has no media, return plain text.
    If the message has images, return a multimodal content array
    with text + image URL parts (up to 5 images).
    """
    if not media_urls:
        return text or ""

    parts = []
    if text:
        parts.append({"type": "text", "text": text})

    for url in media_urls[:5]:  # Limit to 5 images per message
        parts.append({
            "type": "image_url",
            "image_url": {"url": url},
        })

    return parts if parts else (text or "")


async def generate_response(
    system_instructions: str,
    conversation_history: list[dict],
    user_message: str,
    is_nsfw: bool = False,
    media_urls: list[str] | None = None,
) -> tuple[str, int, bool]:
    """
    Generate an AI response to a user's message.

    This is the CORE function of the entire chat service. It:
    1. Selects the right AI model (Gemini or OpenRouter)
    2. Builds the conversation context (system prompt + history + current message)
    3. Calls the AI API
    4. Returns the response text, token count, and whether it's a fallback

    PARAMETERS:
        system_instructions: The AI influencer's personality prompt
        conversation_history: Last 10 messages for context
        user_message: The current message from the user
        is_nsfw: Whether to route to OpenRouter
        media_urls: Image URLs to include with the message

    RETURNS: (response_text, token_count, is_fallback)
        - response_text: The AI's response
        - token_count: Number of tokens used
        - is_fallback: True if we returned the error message instead of a real response
    """
    client, model, max_tokens, temperature = get_client_for_influencer(is_nsfw)

    if not client:
        logger.error("No AI client available")
        return (FALLBACK_ERROR_MESSAGE, 0, True)

    # Build the messages array for the API call
    messages = []

    # 1. System message (the AI's personality)
    messages.append({
        "role": "system",
        "content": system_instructions,
    })

    # 2. Conversation history (last 10 messages for context)
    for msg in conversation_history:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "user":
            # For user messages with media, build multimodal content
            msg_media = msg.get("media_urls")
            if isinstance(msg_media, str):
                try:
                    msg_media = json.loads(msg_media)
                except (json.JSONDecodeError, TypeError):
                    msg_media = None
            messages.append({
                "role": "user",
                "content": _build_user_content(content, msg_media),
            })
        else:
            messages.append({
                "role": "assistant",
                "content": content or "",
            })

    # 3. Current user message (with media if any)
    messages.append({
        "role": "user",
        "content": _build_user_content(user_message, media_urls),
    })

    # Call the AI API
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        # Extract the response text
        response_text = response.choices[0].message.content or ""
        response_text = response_text.strip()

        # Extract token count
        token_count = 0
        if response.usage:
            token_count = response.usage.completion_tokens or 0

        if not token_count and response_text:
            # Fallback token estimation: ~4 characters per token
            token_count = int(len(response_text) / 4)

        return (response_text, token_count, False)

    except Exception as e:
        logger.error(f"AI generation failed: {e}")
        return (FALLBACK_ERROR_MESSAGE, 0, True)


# ---------------------------------------------------------------------------
# MEMORY EXTRACTION
# ---------------------------------------------------------------------------

MEMORY_EXTRACTION_PROMPT = """Extract any factual information about the user from this conversation that should be remembered for future interactions.

Examples of things to remember:
- Physical attributes: height, weight, age, appearance
- Personal information: name, location, occupation, interests
- Preferences: favorite foods, hobbies, goals
- Context: relationship status, family, pets

Recent conversation:
User: {user_message}
Assistant: {assistant_response}

Current memories:
{memories_text}

Return ONLY a JSON object with key-value pairs. Use lowercase keys with underscores (e.g., "height", "weight", "name").
If no new information was provided, return an empty object {{}}.
If information updates an existing memory, use the new value.
Format: {{"key1": "value1", "key2": "value2"}}"""


async def extract_memories(
    user_message: str,
    assistant_response: str,
    existing_memories: dict,
    is_nsfw: bool = False,
) -> dict:
    """
    Extract factual information about the user from a conversation exchange.

    This runs as a BACKGROUND TASK after every AI response. It uses the AI
    to identify facts about the user (name, goals, preferences, etc.) and
    stores them for future conversations.

    The extracted memories are merged with existing ones. New information
    overrides old (e.g., if the user changes their goal).

    PARAMETERS:
        user_message: What the user said
        assistant_response: What the AI replied
        existing_memories: Previously extracted memories
        is_nsfw: Whether to use OpenRouter

    RETURNS: Updated memories dict (merged with existing)
    """
    client, model, _, _ = get_client_for_influencer(is_nsfw)
    if not client:
        return existing_memories

    # Format existing memories for the prompt
    if existing_memories:
        memories_text = "\n".join(
            f"- {k}: {v}" for k, v in existing_memories.items()
        )
    else:
        memories_text = "(none yet)"

    prompt = MEMORY_EXTRACTION_PROMPT.format(
        user_message=user_message,
        assistant_response=assistant_response,
        memories_text=memories_text,
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that returns valid JSON."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1024,
            temperature=0.1,  # Low temperature for factual extraction
        )

        response_text = response.choices[0].message.content or ""

        # Parse JSON from response (find the {} block)
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start >= 0 and end > start:
            new_memories = json.loads(response_text[start:end])
            if isinstance(new_memories, dict):
                # Merge: new overrides old
                merged = {**existing_memories, **new_memories}
                return merged

        return existing_memories

    except Exception as e:
        logger.warning(f"Memory extraction failed (non-fatal): {e}")
        return existing_memories


# ---------------------------------------------------------------------------
# AUDIO TRANSCRIPTION
# ---------------------------------------------------------------------------

async def transcribe_audio(audio_url: str) -> str | None:
    """
    Transcribe an audio file using Gemini's native API.

    This uses Gemini's NATIVE multimodal API (not the OpenAI-compatible one)
    because audio transcription requires sending raw audio data, which the
    OpenAI-compatible endpoint doesn't support well.

    FLOW:
    1. Download the audio file from the presigned S3 URL
    2. Base64-encode the audio bytes
    3. Send to Gemini's native generateContent endpoint
    4. Return the transcribed text

    PARAMETERS:
        audio_url: Presigned S3 URL to the audio file

    RETURNS: Transcribed text, or None if transcription fails
    """
    if not config.GEMINI_API_KEY:
        logger.error("Cannot transcribe audio: GEMINI_API_KEY not set")
        return None

    try:
        async with httpx.AsyncClient(timeout=60) as http:
            # Step 1: Download the audio file
            download_response = await http.get(audio_url, timeout=15)
            download_response.raise_for_status()

            audio_bytes = download_response.content
            content_type = download_response.headers.get("content-type", "audio/mpeg")

            # Step 2: Base64-encode the audio
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

            # Step 3: Call Gemini's native generateContent API
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/"
                f"models/{config.GEMINI_MODEL}:generateContent"
            )
            payload = {
                "contents": [{
                    "parts": [
                        {
                            "text": "Please transcribe this audio file accurately. "
                                    "Only return the transcription text without any "
                                    "additional commentary.",
                        },
                        {
                            "inlineData": {
                                "mimeType": content_type,
                                "data": audio_b64,
                            },
                        },
                    ],
                }],
                "generationConfig": {
                    "temperature": 0.1,
                    "maxOutputTokens": 4096,
                },
            }

            response = await http.post(
                url,
                json=payload,
                headers={"x-goog-api-key": config.GEMINI_API_KEY},
                timeout=60,
            )
            response.raise_for_status()

            # Step 4: Extract transcribed text from response
            data = response.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    if "text" in part:
                        return part["text"].strip()

            logger.warning("No transcription text in Gemini response")
            return None

    except Exception as e:
        logger.error(f"Audio transcription failed: {e}")
        return None

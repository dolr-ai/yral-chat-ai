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
# - GEMINI: Uses the NATIVE Gemini API with ?key= authentication.
#   This supports the newer AQ. format API keys from Google AI Studio
#   and Google Cloud Console. We build the request manually via httpx.
#
# - OPENROUTER: Uses the OpenAI Python SDK with Bearer auth.
#   OpenRouter's API is OpenAI-compatible and works with the SDK directly.
#
# WHY NOT OpenAI SDK FOR GEMINI?
# Google changed their API key format in 2026. The new AQ. keys cause
# "Multiple authentication credentials" errors when sent as Bearer tokens
# on the OpenAI-compatible endpoint. The native API with ?key= works fine.
#
# PORTED FROM: yral-ai-chat/src/services/ai.rs
# ---------------------------------------------------------------------------

import json
import base64
import logging

import httpx
from openai import AsyncOpenAI

import config

logger = logging.getLogger(__name__)

# The message the AI sends when it can't generate a real response.
FALLBACK_ERROR_MESSAGE = "I'm having trouble responding right now. Please try again in a moment."

# Gemini native API base URL (used with ?key= query parameter)
GEMINI_NATIVE_URL = "https://generativelanguage.googleapis.com/v1beta"


# ---------------------------------------------------------------------------
# OPENROUTER CLIENT (for NSFW influencers — uses OpenAI SDK)
# ---------------------------------------------------------------------------

_openrouter_client: AsyncOpenAI | None = None


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


def get_openrouter_client() -> AsyncOpenAI | None:
    """Get or create the OpenRouter client."""
    global _openrouter_client
    if _openrouter_client is None:
        _openrouter_client = _create_openrouter_client()
    return _openrouter_client


# ---------------------------------------------------------------------------
# GEMINI NATIVE API (uses httpx with ?key= auth)
# ---------------------------------------------------------------------------

def _build_gemini_contents(
    system_instructions: str,
    conversation_history: list[dict],
    user_message: str,
    media_urls: list[str] | None = None,
) -> tuple[dict, list]:
    """
    Build the 'system_instruction' and 'contents' payload for Gemini's
    native generateContent API.

    Gemini native format is different from OpenAI:
    - system_instruction: separate top-level field (not a message)
    - contents: list of {role: "user"/"model", parts: [{text: "..."}]}
    - "model" role instead of "assistant"
    - media is sent as inline parts, not as image_url objects
    """
    # System instruction (top-level, not inside contents)
    system_instruction = {"parts": [{"text": system_instructions}]}

    # Build contents array
    contents = []

    # Conversation history
    for msg in conversation_history:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Gemini uses "model" instead of "assistant"
        gemini_role = "model" if role == "assistant" else "user"

        parts = []
        if content:
            parts.append({"text": content})

        # Handle media in history
        if role == "user":
            msg_media = msg.get("media_urls")
            if isinstance(msg_media, str):
                try:
                    msg_media = json.loads(msg_media)
                except (json.JSONDecodeError, TypeError):
                    msg_media = None
            if msg_media:
                for url in msg_media[:5]:
                    # For URLs in history, reference them as file URIs
                    parts.append({"text": f"[Image: {url}]"})

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    # Current user message
    user_parts = []
    if user_message:
        user_parts.append({"text": user_message})
    if media_urls:
        for url in media_urls[:5]:
            user_parts.append({"text": f"[Image: {url}]"})
    if user_parts:
        contents.append({"role": "user", "parts": user_parts})

    return system_instruction, contents


async def _call_gemini(
    contents: list,
    system_instruction: dict | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> tuple[str, int]:
    """
    Call Gemini's native generateContent API.

    Uses ?key= query parameter for authentication (works with AQ. format keys).

    RETURNS: (response_text, token_count)
    RAISES: Exception on API failure
    """
    url = f"{GEMINI_NATIVE_URL}/models/{config.GEMINI_MODEL}:generateContent"

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }

    # Add system instruction if provided
    if system_instruction:
        payload["systemInstruction"] = system_instruction

    async with httpx.AsyncClient(timeout=config.GEMINI_TIMEOUT) as http:
        response = await http.post(
            url,
            json=payload,
            params={"key": config.GEMINI_API_KEY},
            timeout=config.GEMINI_TIMEOUT,
        )
        response.raise_for_status()

    data = response.json()

    # Extract text from response
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError("No candidates in Gemini response")

    parts = candidates[0].get("content", {}).get("parts", [])
    response_text = ""
    for part in parts:
        if "text" in part:
            response_text += part["text"]

    response_text = response_text.strip()

    # Extract token count
    usage = data.get("usageMetadata", {})
    token_count = usage.get("candidatesTokenCount", 0)
    if not token_count and response_text:
        token_count = int(len(response_text) / 4)

    return response_text, token_count


# ---------------------------------------------------------------------------
# CORE FUNCTION: Generate AI Response
# ---------------------------------------------------------------------------

def _build_user_content(text: str | None, media_urls: list[str] | None) -> str | list:
    """
    Build the content payload for OpenAI-format messages (used by OpenRouter).
    """
    if not media_urls:
        return text or ""

    parts = []
    if text:
        parts.append({"type": "text", "text": text})

    for url in media_urls[:5]:
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
    1. Selects the right AI model (Gemini native or OpenRouter)
    2. Builds the conversation context
    3. Calls the AI API
    4. Returns the response text, token count, and whether it's a fallback

    ROUTING:
    - Normal influencers → Gemini native API (with ?key= auth)
    - NSFW influencers → OpenRouter via OpenAI SDK (with Bearer auth)

    RETURNS: (response_text, token_count, is_fallback)
    """

    # ---------------------------------------------------------------
    # NSFW → OpenRouter (uses OpenAI SDK)
    # ---------------------------------------------------------------
    if is_nsfw:
        client = get_openrouter_client()
        if client:
            try:
                messages = [{"role": "system", "content": system_instructions}]

                for msg in conversation_history:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if role == "user":
                        msg_media = msg.get("media_urls")
                        if isinstance(msg_media, str):
                            try:
                                msg_media = json.loads(msg_media)
                            except (json.JSONDecodeError, TypeError):
                                msg_media = None
                        messages.append({"role": "user", "content": _build_user_content(content, msg_media)})
                    else:
                        messages.append({"role": "assistant", "content": content or ""})

                messages.append({"role": "user", "content": _build_user_content(user_message, media_urls)})

                response = await client.chat.completions.create(
                    model=config.OPENROUTER_MODEL,
                    messages=messages,
                    max_tokens=config.OPENROUTER_MAX_TOKENS,
                    temperature=config.OPENROUTER_TEMPERATURE,
                )

                response_text = response.choices[0].message.content or ""
                response_text = response_text.strip()

                token_count = 0
                if response.usage:
                    token_count = response.usage.completion_tokens or 0
                if not token_count and response_text:
                    token_count = int(len(response_text) / 4)

                return (response_text, token_count, False)

            except Exception as e:
                logger.error(f"OpenRouter generation failed: {e}")
                # Fall through to Gemini as backup

    # ---------------------------------------------------------------
    # Normal → Gemini native API (with ?key= auth)
    # ---------------------------------------------------------------
    if not config.GEMINI_API_KEY:
        logger.error("No AI client available (GEMINI_API_KEY not set)")
        return (FALLBACK_ERROR_MESSAGE, 0, True)

    try:
        system_instruction, contents = _build_gemini_contents(
            system_instructions, conversation_history, user_message, media_urls,
        )

        response_text, token_count = await _call_gemini(
            contents=contents,
            system_instruction=system_instruction,
            temperature=config.GEMINI_TEMPERATURE,
            max_tokens=config.GEMINI_MAX_TOKENS,
        )

        return (response_text, token_count, False)

    except Exception as e:
        logger.error(f"Gemini generation failed: {e}")
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

    Uses the same routing as generate_response:
    - NSFW → OpenRouter
    - Normal → Gemini native API
    """
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
        # For NSFW, try OpenRouter first
        if is_nsfw:
            client = get_openrouter_client()
            if client:
                response = await client.chat.completions.create(
                    model=config.OPENROUTER_MODEL,
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant that returns valid JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=1024,
                    temperature=0.1,
                )
                response_text = response.choices[0].message.content or ""
                start = response_text.find("{")
                end = response_text.rfind("}") + 1
                if start >= 0 and end > start:
                    new_memories = json.loads(response_text[start:end])
                    if isinstance(new_memories, dict):
                        return {**existing_memories, **new_memories}
                return existing_memories

        # Normal: use Gemini native API
        if not config.GEMINI_API_KEY:
            return existing_memories

        contents = [{"role": "user", "parts": [{"text": prompt}]}]
        system_instruction = {"parts": [{"text": "You are a helpful assistant that returns valid JSON."}]}

        response_text, _ = await _call_gemini(
            contents=contents,
            system_instruction=system_instruction,
            temperature=0.1,
            max_tokens=1024,
        )

        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start >= 0 and end > start:
            new_memories = json.loads(response_text[start:end])
            if isinstance(new_memories, dict):
                return {**existing_memories, **new_memories}

        return existing_memories

    except Exception as e:
        logger.warning(f"Memory extraction failed (non-fatal): {e}")
        return existing_memories


# ---------------------------------------------------------------------------
# AUDIO TRANSCRIPTION (already uses native Gemini API — no changes needed)
# ---------------------------------------------------------------------------

async def transcribe_audio(audio_url: str) -> str | None:
    """
    Transcribe an audio file using Gemini's native API.

    Uses ?key= query parameter for auth (works with AQ. format keys).

    FLOW:
    1. Download the audio file from the presigned S3 URL
    2. Base64-encode the audio bytes
    3. Send to Gemini's native generateContent endpoint
    4. Return the transcribed text
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
            url = f"{GEMINI_NATIVE_URL}/models/{config.GEMINI_MODEL}:generateContent"
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
                params={"key": config.GEMINI_API_KEY},
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

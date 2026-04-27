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

# Hard cap on per-image decoded size (bytes) before base64. Gemini native
# has a ~20 MB total request limit; a 5 MB raw image → ~6.7 MB base64, so
# 5 MB leaves room for system prompt + history + other images. The mobile
# client also compresses to ~2 MB now (see Sarvesh's fix), so this is a
# defensive ceiling for old clients / iOS / web uploaders.
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_DOWNLOAD_TIMEOUT = 5.0  # seconds


async def _fetch_image_bytes_and_mime(url: str) -> tuple[str, bytes] | tuple[None, str]:
    """
    Download an image and return (mime, bytes) — or (None, reason) on failure.

    Shared by both the Gemini-native image path (`_fetch_and_encode_image`)
    and the OpenAI-compat image path used for OpenRouter
    (`_fetch_and_encode_image_openai`). Any future image source should reuse
    this helper — keeping the presigned-URL resolution + size cap + mime
    detection + download in one place avoids the class of bug where one
    path got fixed and the other didn't (e.g., Sentry issue #12 2026-04-24:
    OpenRouter path was still passing raw S3 keys while Gemini-native path
    was already base64-inlining).

    Returns:
        On success: (mime:str, bytes)          e.g. ("image/jpeg", b"...")
        On failure: (None, reason:str)         e.g. (None, "missing"|"too large"|"empty"|"failed to load")
    """
    # The mobile app sends back the S3 storage KEY (e.g. "user-id/uuid.jpg"),
    # not the presigned URL, in `media_urls`. Convert it before fetching.
    # `generate_presigned_url` passes through existing http(s) URLs unchanged.
    if not (url.startswith("http://") or url.startswith("https://")):
        from services import storage as _storage
        presigned = _storage.generate_presigned_url(url)
        if not presigned:
            logger.warning(f"No presigned URL for storage key {url[:80]}")
            return (None, "missing")
        url = presigned

    try:
        # follow_redirects=True so CDN redirects (e.g. picsum → fastly) don't
        # silently drop the image. Storj presigned URLs don't redirect, but
        # robustness matters for any non-Storj paths (avatars, old hosts, etc.).
        async with httpx.AsyncClient(
            timeout=_IMAGE_DOWNLOAD_TIMEOUT, follow_redirects=True
        ) as http:
            resp = await http.get(url)
            resp.raise_for_status()
    except Exception as e:
        logger.warning(f"Image fetch failed for {url[:80]}: {e}")
        return (None, "failed to load")

    data = resp.content
    if len(data) > _MAX_IMAGE_BYTES:
        logger.warning(f"Image too large ({len(data)} bytes > {_MAX_IMAGE_BYTES}); dropping")
        return (None, "too large")
    if not data:
        return (None, "empty")

    # Prefer Content-Type from response; fall back to a safe default.
    mime = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"

    return (mime, data)


async def _fetch_and_encode_image(url: str) -> dict:
    """
    Download an image URL and return a Gemini `inlineData` part.

    Returns a `{"text": "..."}` placeholder on failure so the chat still
    works for the text portion — Gemini will at least know an image was
    attached even if we can't load it.
    """
    mime, data = await _fetch_image_bytes_and_mime(url)
    if mime is None:
        return {"text": f"[image attachment — {data}]"}
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}


async def _fetch_and_encode_image_openai(url: str) -> dict:
    """
    Download an image URL and return an OpenAI-chat-completions content part.

    OpenAI's chat API accepts `image_url` with either a URL or a base64
    `data:` URL. We inline as base64 for the same reasons we do on the
    Gemini-native path — OpenRouter forwards to the underlying provider
    (often Gemini via a different endpoint), and historically Gemini has
    been unreliable at fetching remote URLs. Inlining bytes is the only
    way to guarantee the model actually sees the image.

    PR ref: Sentry issue #12 (2026-04-24) — the OpenRouter path was
    passing raw S3 storage keys as `image_url.url`, which Google rejected
    with `400 Invalid URL format: ...`.
    """
    mime, data = await _fetch_image_bytes_and_mime(url)
    if mime is None:
        return {"type": "text", "text": f"[image attachment — {data}]"}
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


async def _build_gemini_contents(
    system_instructions: str,
    conversation_history: list[dict],
    user_message: str,
    media_urls: list[str] | None = None,
) -> tuple[dict, list]:
    """
    Build the 'system_instruction' and 'contents' payload for Gemini's
    native generateContent API.

    PERFORMANCE OPTIMIZATION (Apr 21, 2026):
    Images in history are handled with two optimizations:
    1. LIMITED WINDOW: Only the last N messages get their images inlined
       (configurable via IMAGE_HISTORY_WINDOW, default 3). Older messages
       get a text placeholder — the AI already described those images in
       earlier responses, so context is preserved.
    2. PARALLEL DOWNLOAD: All images (recent history + current message)
       are downloaded concurrently via asyncio.gather(), not sequentially.
       5 images × 400ms sequential = 2,000ms → parallel = ~400ms.

    Before this fix: 10 images in history → 5-23 second responses.
    After: same conversation → 1.5-3.6 seconds.
    """
    import asyncio

    # System instruction (top-level, not inside contents)
    system_instruction = {"parts": [{"text": system_instructions}]}

    # Determine which history messages are "recent" enough to get images
    history_len = len(conversation_history)
    window = config.IMAGE_HISTORY_WINDOW  # default 3
    recent_start = max(0, history_len - window)

    # Phase 1: Build text parts for ALL history messages.
    #          Collect image download tasks for RECENT messages only.
    contents = []
    image_tasks = []        # (index_in_contents, position_in_parts, coroutine)
    placeholder_indices = []  # track where to insert downloaded images

    for i, msg in enumerate(conversation_history):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        gemini_role = "model" if role == "assistant" else "user"

        parts = []
        if content:
            parts.append({"text": content})

        if role == "user":
            msg_media = msg.get("media_urls")
            if isinstance(msg_media, str):
                try:
                    msg_media = json.loads(msg_media)
                except (json.JSONDecodeError, TypeError):
                    msg_media = None

            if msg_media:
                if i >= recent_start:
                    # RECENT message → download images (collected for parallel)
                    for url in msg_media[:5]:
                        # Add placeholder, record position for later replacement
                        placeholder_idx = len(parts)
                        parts.append(None)  # placeholder
                        image_tasks.append((len(contents), placeholder_idx, _fetch_and_encode_image(url)))
                else:
                    # OLD message → text note (AI already described these)
                    parts.append({"text": f"[User sent {len(msg_media)} image(s) — see AI's earlier response for description]"})

        if parts:
            contents.append({"role": gemini_role, "parts": parts})

    # Current user message — always inline images (AI hasn't seen them yet)
    user_parts = []
    if user_message:
        user_parts.append({"text": user_message})
    if media_urls:
        for url in media_urls[:5]:
            placeholder_idx = len(user_parts)
            user_parts.append(None)  # placeholder
            image_tasks.append((len(contents), placeholder_idx, _fetch_and_encode_image(url)))
    if user_parts:
        contents.append({"role": "user", "parts": user_parts})

    # Phase 2: Download ALL images in PARALLEL (the key optimization)
    if image_tasks:
        coroutines = [task[2] for task in image_tasks]
        results = await asyncio.gather(*coroutines, return_exceptions=True)

        # Map results back to their placeholder positions
        for (content_idx, part_idx, _), result in zip(image_tasks, results):
            if isinstance(result, Exception):
                logger.warning(f"Image download failed in parallel batch: {result}")
                contents[content_idx]["parts"][part_idx] = {"text": "[image — failed to load]"}
            else:
                contents[content_idx]["parts"][part_idx] = result

        # Clean up any remaining None placeholders (shouldn't happen, but safety)
        for entry in contents:
            entry["parts"] = [p for p in entry["parts"] if p is not None]

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

    # Extract text from response.
    #
    # Gemini returns no `candidates` when the safety filter blocks the
    # input (most common cause) or when an upstream model error occurs.
    # The reason lives in `promptFeedback.blockReason`. Surfacing it lets
    # us tell apart safety-block (expected for some prompts) from
    # quota / rate-limit / provider issues (actionable) when looking at
    # Sentry. Sentry issue #3 (~26 affected users, 2026-04-27) was
    # firing as a generic "No candidates" — the diagnostic below makes
    # the real cause visible without changing user-facing behavior.
    candidates = data.get("candidates", [])
    if not candidates:
        feedback = data.get("promptFeedback") or {}
        block_reason = feedback.get("blockReason", "UNKNOWN")
        raise ValueError(
            f"Gemini returned no candidates "
            f"(blockReason={block_reason}, model={config.GEMINI_MODEL})"
        )

    parts = candidates[0].get("content", {}).get("parts", [])
    response_text = ""
    for part in parts:
        if "text" in part:
            response_text += part["text"]

    response_text = response_text.strip()

    # A candidate can come back with finishReason=SAFETY (or MAX_TOKENS,
    # RECITATION, etc.) and no `content.parts` — meaning the model started
    # responding then got cut off. Treating that as a successful empty
    # response would surface a blank message to the user, which is worse
    # than the FALLBACK_ERROR_MESSAGE the caller emits on exception.
    if not response_text:
        finish_reason = candidates[0].get("finishReason", "UNKNOWN")
        raise ValueError(
            f"Gemini returned candidate with no text "
            f"(finishReason={finish_reason}, model={config.GEMINI_MODEL})"
        )

    # Extract token count
    usage = data.get("usageMetadata", {})
    token_count = usage.get("candidatesTokenCount", 0)
    if not token_count and response_text:
        token_count = int(len(response_text) / 4)

    return response_text, token_count


# ---------------------------------------------------------------------------
# CORE FUNCTION: Generate AI Response
# ---------------------------------------------------------------------------

async def _build_user_content(text: str | None, media_urls: list[str] | None) -> str | list:
    """
    Build the content payload for OpenAI-format messages (used by OpenRouter).

    For each image URL/storage-key in `media_urls`, downloads it and
    inlines as a base64 `data:` URL — the same strategy used on the
    Gemini-native path. Previously this function passed raw URLs, which
    broke for two reasons:
    (1) Mobile clients send back the S3 STORAGE KEY, not a presigned URL,
        so the "URL" passed to OpenRouter was a bare key like
        "<user>/<uuid>.jpg" which Google rejected with 400 Invalid URL
        (Sentry issue #12 2026-04-24).
    (2) Even when passed a real URL, Gemini (via OpenRouter's OpenAI-compat
        layer) has historically been unreliable at fetching remote URLs.
    """
    if not media_urls:
        return text or ""

    parts = []
    if text:
        parts.append({"type": "text", "text": text})

    for url in media_urls[:5]:
        parts.append(await _fetch_and_encode_image_openai(url))

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
                        messages.append({"role": "user", "content": await _build_user_content(content, msg_media)})
                    else:
                        messages.append({"role": "assistant", "content": content or ""})

                messages.append({"role": "user", "content": await _build_user_content(user_message, media_urls)})

                response = await client.chat.completions.create(
                    model=config.OPENROUTER_MODEL,
                    messages=messages,
                    max_tokens=config.OPENROUTER_MAX_TOKENS,
                    temperature=config.OPENROUTER_TEMPERATURE,
                )

                # OpenRouter is a proxy in front of many providers and
                # occasionally returns a 200 OK whose body has `choices=None`
                # (the underlying provider misbehaved in a way the OpenAI
                # SDK silently accepts). The bare `response.choices[0]`
                # then raises 'NoneType' object is not subscriptable —
                # the cryptic message that drove Sentry issue #2
                # (~2,839 events, 15 users, 2026-04-24). The defensive
                # parse below promotes the failure to a typed error
                # carrying the model name + response id, so the
                # except-clause below falls through to the Gemini
                # backup path with actionable context.
                choices = response.choices or []
                if not choices:
                    raise RuntimeError(
                        f"OpenRouter returned no choices "
                        f"(model={config.OPENROUTER_MODEL}, "
                        f"id={getattr(response, 'id', '?')})"
                    )
                message = choices[0].message
                response_text = (message.content if message else None) or ""
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
        system_instruction, contents = await _build_gemini_contents(
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

def _is_safe_url(url: str) -> bool:
    """
    Validate that a URL is safe to fetch (not SSRF).

    Blocks:
    - Private IP ranges (127.x, 10.x, 172.16-31.x, 192.168.x)
    - Metadata endpoints (169.254.169.254)
    - Non-HTTP(S) schemes
    - URLs without a host
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        if not host:
            return False
        # Block private/internal IPs
        blocked_prefixes = (
            "127.", "10.", "192.168.", "0.", "169.254.",
            "172.16.", "172.17.", "172.18.", "172.19.",
            "172.20.", "172.21.", "172.22.", "172.23.",
            "172.24.", "172.25.", "172.26.", "172.27.",
            "172.28.", "172.29.", "172.30.", "172.31.",
        )
        if any(host.startswith(p) for p in blocked_prefixes):
            return False
        if host in ("localhost", "metadata.google.internal"):
            return False
        return True
    except Exception:
        return False


async def transcribe_audio(audio_url: str) -> str | None:
    """
    Transcribe an audio file using Gemini's native API.

    Uses ?key= query parameter for auth (works with AQ. format keys).
    Validates URL to prevent SSRF attacks.

    FLOW:
    1. Validate the URL is not internal/private (SSRF protection)
    2. Download the audio file from the presigned S3 URL
    3. Base64-encode the audio bytes
    4. Send to Gemini's native generateContent endpoint
    5. Return the transcribed text
    """
    if not config.GEMINI_API_KEY:
        logger.error("Cannot transcribe audio: GEMINI_API_KEY not set")
        return None

    if not _is_safe_url(audio_url):
        logger.error(f"Audio URL blocked (SSRF protection): {audio_url[:50]}")
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

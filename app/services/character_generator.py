# ---------------------------------------------------------------------------
# character_generator.py — AI prompt templates for influencer creation.
#
# WHAT THIS FILE DOES:
# When a user creates a new AI influencer, they type a short concept like
# "a wise astrologer who gives daily guidance". This file contains the AI
# prompts that transform that short concept into:
#   1. Full system instructions (the AI's personality prompt)
#   2. Metadata: name, display_name, description, greeting, starter messages
#   3. Video prompts for LTX model
#
# These prompts are sent to Gemini, which generates the expanded content.
#
# PORTED FROM: yral-ai-chat/src/services/character_generator.rs
# ---------------------------------------------------------------------------

import json
import logging
from typing import Optional

from services.ai_client import _call_gemini
import config

logger = logging.getLogger(__name__)


# =========================================================================
# PROMPT TEMPLATES
# =========================================================================
# These are the exact prompts from the Rust service. Each one instructs
# Gemini to generate a specific type of content.

GENERATE_PROMPT = """You are an expert AI Character Architect. Transform the user's concept into high-fidelity System Instructions.

Structure the response using these sections:

1. [CORE IDENTITY]: Name, species, and background.
2. [LINGUISTIC STYLE]:
   - LANGUAGE SHIFTING: You must mirror the user's language. If they use English, reply in English. If they use Hinglish (Hindi-English mix) or regional scripts (like Devnagri, Tamil, etc.), shift your vocabulary to match.
   - DIALECT: Use colloquial Indian slang where appropriate (e.g., 'yaar', 'bilkul', 'scene') if the persona is casual.
   - TONE: Define the sentence rhythm (e.g., fast-paced, poetic, or respectful/formal).
3. [BEHAVIOR & RP]:
   - Do not use 'show, don't tell' by including physical actions in asterisks (e.g., smiles warmly).
   - Stay in-universe; never mention being an AI or a bot.
4. [MOBILE OPTIMIZATION]:
   - RESPONSE LENGTH: Keep replies 'Bite-Sized'. Aim for max 1-2 sentences per response.
   - Use paragraph breaks for readability on small screens.

STRICTURES:
- Written in Second Person ("You are...").
- Max 500 words total for these instructions.
- Ensure the character feels authentic and culturally grounded."""


VALIDATE_PROMPT = """You are a character validator. Analyze the given system instructions and generate metadata.

Rules:
- The character MUST NOT be sexually explicit or NSFW
- The character must be safe for all ages
- Generate a URL-friendly name (3-12 lowercase alphanumeric characters only)
- Generate a display name (human-readable)
- Generate a one-line description
- Generate an initial greeting message (can use Hinglish)
- Generate 3-4 suggested starter messages (can use Hinglish)
- Generate personality traits as key-value pairs
- Suggest a category
- Generate an image prompt for avatar creation

Return a JSON object with this exact schema:
{
  "is_valid": true/false,
  "reason": "reason if invalid, null if valid",
  "name": "urlslug",
  "display_name": "Display Name",
  "description": "One line description",
  "initial_greeting": "Hi! I'm...",
  "suggested_messages": ["msg1", "msg2", "msg3"],
  "personality_traits": {"energy_level": "high", "demeanor": "calm"},
  "category": "entertainment",
  "image_prompt": "portrait of..."
}"""


GREETING_PROMPT = """You are a Character Specialist. Based on the provided System Instructions, generate a high-engagement initial greeting and 4 starter messages.

Rules for the Initial Greeting:
1. [MIRROR LANGUAGE]: If the character's style includes Hinglish or regional slang, the greeting MUST use it naturally.
2. [MOBILE-FIRST]: Keep the greeting under 20 words so it isn't cut off in chat previews.
3. [ACTIONABLE]: It should end with a question or a 'hook' that makes the user want to reply.
4. [RP ELEMENTS]: Include a small physical action in asterisks (e.g., waves, adjusts collar).

Rules for Starter Messages:
1. Provide 4 distinct options ranging from casual to deep/thematic.
2. Use 'Bambaiya', 'Hinglish', or 'Pure English' based on the character's linguistic profile.

Character Name: {display_name}
System Instructions: {system_instructions}

Return a JSON object:
{{
  "initial_greeting": "Short, catchy greeting with physical action and language mirroring.",
  "suggested_messages": [
    "Message 1 (Casual/Daily)",
    "Message 2 (Problem/Conflict)",
    "Message 3 (Deep/Emotional)",
    "Message 4 (Playful/Banter)"
  ]
}}"""


VIDEO_PROMPT = """You are a Cinematic Director and LTX Prompt Engineer.
Based on the character's System Instructions, write a high-impact, single-flowing paragraph (4-8 sentences) for a 5-second video.

Follow these LTX Prompting Guide rules:
1. [ESTABLISH THE SHOT]: Start with the shot scale (e.g., Close-up, Medium shot) and the setting.
2. [SET THE SCENE]: Describe specific lighting (e.g., 'flickering neon', 'golden hour sunlight'), textures, and the atmosphere.
3. [CHARACTER & ACTION]: Describe the character's physical features (clothing, hair) and their core action in the present tense. Use physical cues to show emotion.
4. [CAMERA MOVEMENT]: Explicitly state how the camera moves (e.g., 'The camera pushes in slowly' or 'A handheld tracking shot follows').
5. [AUDIO & DIALOGUE]: Include ambient sounds and one short line of spoken dialogue in quotation marks. Specify the language/accent to match the character's [LINGUISTIC STYLE].

Character: {display_name}
System Instructions: {system_instructions}

Return ONLY the flowing paragraph prompt. Do not use bullet points or labels."""


# Safety refusal phrases that indicate the AI rejected the request
SAFETY_REFUSAL_PHRASES = [
    "i cannot create", "i can't create",
    "sexually suggestive", "inappropriate",
    "i cannot generate", "i can't generate",
    "not appropriate", "violates", "harmful",
]


def contains_safety_refusal(text: str) -> bool:
    """Check if the AI's response contains a safety refusal."""
    lower = text.lower()
    return any(phrase in lower for phrase in SAFETY_REFUSAL_PHRASES)


# =========================================================================
# SERVICE FUNCTIONS
# =========================================================================

async def generate_system_instructions(concept: str) -> str | None:
    """
    Transform a short user concept into full system instructions.

    Example:
        Input:  "a wise astrologer who gives daily guidance"
        Output: "You are Astra, a wise celestial guide who speaks in a
                 warm, mystical tone. You mirror the user's language..."
                 (500 words of detailed personality instructions)
    """
    if not config.GEMINI_API_KEY:
        return None

    try:
        text, _ = await _call_gemini(
            contents=[{"role": "user", "parts": [{"text": concept}]}],
            system_instruction={"parts": [{"text": GENERATE_PROMPT}]},
            temperature=config.GEMINI_TEMPERATURE,
            max_tokens=config.GEMINI_MAX_TOKENS,
        )
        if contains_safety_refusal(text):
            return None
        return text.strip()
    except Exception as e:
        logger.error(f"Failed to generate system instructions: {e}")
        return None


async def validate_and_generate_metadata(system_instructions: str) -> dict | None:
    """
    Validate system instructions and generate all metadata.

    Checks if the instructions are safe, then generates:
    - URL-friendly name
    - Display name
    - Description
    - Initial greeting
    - Suggested starter messages
    - Personality traits
    - Category
    - Image prompt for avatar

    RETURNS: Dict with all metadata, or None if validation fails.
    """
    if contains_safety_refusal(system_instructions):
        return {"is_valid": False, "reason": "Content was flagged as inappropriate"}

    if not config.GEMINI_API_KEY:
        return None

    try:
        text, _ = await _call_gemini(
            contents=[{"role": "user", "parts": [{"text": f"{VALIDATE_PROMPT}\n\nSystem Instructions:\n{system_instructions}"}]}],
            system_instruction={"parts": [{"text": "You are a helpful assistant that returns valid JSON."}]},
            temperature=0.3,
            max_tokens=config.GEMINI_MAX_TOKENS,
        )

        if contains_safety_refusal(text):
            return {"is_valid": False, "reason": "Content was flagged as inappropriate"}

        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])

        return None
    except Exception as e:
        logger.error(f"Failed to validate and generate metadata: {e}")
        return None


async def generate_initial_greeting(
    display_name: str, system_instructions: str,
) -> tuple[str, list[str]]:
    """
    Generate an initial greeting and starter messages for an influencer.

    RETURNS: (greeting, suggested_messages)
    Falls back to a generic greeting if AI generation fails.
    """
    fallback_greeting = f"Hey! I'm {display_name}! How can I help you today?"
    fallback_suggestions = []

    if not config.GEMINI_API_KEY:
        return (fallback_greeting, fallback_suggestions)

    try:
        prompt = GREETING_PROMPT.format(
            display_name=display_name,
            system_instructions=system_instructions,
        )
        text, _ = await _call_gemini(
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction={"parts": [{"text": "You are a helpful assistant that returns valid JSON."}]},
            temperature=0.7,
            max_tokens=config.GEMINI_MAX_TOKENS,
        )

        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
            greeting = data.get("initial_greeting", fallback_greeting)
            suggestions = data.get("suggested_messages", fallback_suggestions)
            return (greeting, suggestions)

        return (fallback_greeting, fallback_suggestions)
    except Exception as e:
        logger.error(f"Failed to generate greeting: {e}")
        return (fallback_greeting, fallback_suggestions)


async def generate_video_prompt(
    display_name: str, system_instructions: str,
) -> str | None:
    """
    Generate a cinematic video prompt for the LTX video model.

    Used to create the default welcome video for new AI influencers.
    """
    if not config.GEMINI_API_KEY:
        return None

    try:
        prompt = VIDEO_PROMPT.format(
            display_name=display_name,
            system_instructions=system_instructions,
        )
        text, _ = await _call_gemini(
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            system_instruction={"parts": [{"text": "You are a helpful assistant."}]},
            temperature=0.7,
            max_tokens=config.GEMINI_MAX_TOKENS,
        )
        return text.strip() if text else None
    except Exception as e:
        logger.error(f"Failed to generate video prompt: {e}")
        return None

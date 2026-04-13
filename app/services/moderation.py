# ---------------------------------------------------------------------------
# moderation.py — Safety guardrails appended to AI influencer system instructions.
#
# WHAT THIS DOES:
# Every AI influencer has "system instructions" (their personality prompt).
# Before saving these instructions to the database, we APPEND safety rules
# that prevent the AI from generating harmful content.
#
# When DISPLAYING the instructions back to the user (e.g., in the edit screen),
# we STRIP these guardrails so the user only sees their original text.
#
# PORTED FROM: yral-ai-chat/src/services/moderation.rs
# ---------------------------------------------------------------------------

# Short prompt that prevents the AI from apologizing excessively.
# Without this, AI models tend to say "I apologize for the confusion" repeatedly.
STYLE_PROMPT = "IMPORTANT: Avoid apologies or self-corrections in your responses."

# Safety rules appended to every AI influencer's system instructions.
# These prevent the AI from generating harmful, illegal, or inappropriate content.
MODERATION_PROMPT = """Key Rules:
- Always be helpful, polite, and professional
- Do NOT provide medical, legal, or financial advice
- Do NOT generate sexually explicit or NSFW content
- Do NOT engage in hate speech, violence, or illegal activities
- Decline unsafe requests gracefully while staying in character
- Maintain consistency with your persona at all times
- Ensure all content is safe for all ages"""


def with_guardrails(instructions: str) -> str:
    """
    Append safety guardrails to system instructions before saving to DB.

    Example:
        Input:  "You are Ahaan, a fitness coach..."
        Output: "You are Ahaan, a fitness coach...\nIMPORTANT: Avoid...\nKey Rules:..."
    """
    return f"{instructions}\n{STYLE_PROMPT}\n{MODERATION_PROMPT}"


def strip_guardrails(instructions: str) -> str:
    """
    Strip safety guardrails from system instructions for display.

    When showing the instructions back to the user (e.g., in an edit screen),
    we remove the guardrails so they only see their original text.
    """
    return instructions.replace(STYLE_PROMPT, "").replace(MODERATION_PROMPT, "").strip()

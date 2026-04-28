# ---------------------------------------------------------------------------
# models.py — Pydantic models for all API request/response JSON shapes.
#
# WHAT ARE PYDANTIC MODELS?
# Pydantic models define the EXACT shape of JSON that the API sends and
# receives. They serve three purposes:
#   1. VALIDATION: Incoming requests are checked against the model.
#      If a required field is missing or has the wrong type, FastAPI
#      automatically returns a 422 error with a clear message.
#   2. SERIALIZATION: When we return a response, Pydantic converts our
#      Python objects to JSON with the exact field names the mobile app expects.
#   3. DOCUMENTATION: FastAPI uses these models to generate Swagger/OpenAPI
#      docs automatically.
#
# CRITICAL: These models MUST match the mobile app's Kotlin DTOs exactly.
# If a field name is wrong or a type doesn't match, the mobile app crashes.
#
# MOBILE APP DTOs (the source of truth we match against):
#   ~/Claude Projects/yral-mobile/shared/features/chat/src/commonMain/
#   kotlin/com/yral/shared/features/chat/data/models/
#
# PORTED FROM: yral-ai-chat/src/models/ (Rust structs with serde)
# ---------------------------------------------------------------------------

from enum import Enum
from typing import Optional, Literal
from pydantic import AliasChoices, BaseModel, Field


# =========================================================================
# INFLUENCER MODELS
# =========================================================================

class InfluencerResponse(BaseModel):
    """
    JSON shape for an influencer in list responses.
    MATCHES: InfluencerDto.kt in the mobile app.

    IMPORTANT: is_active is a STRING ("active"), not a boolean!
    The mobile app deserializes it as String.
    """
    id: str
    name: str
    display_name: str
    avatar_url: str
    description: str
    category: str
    is_active: str  # "active" / "coming_soon" / "discontinued" — NOT a boolean!
    created_at: str  # ISO 8601 timestamp as string
    conversation_count: Optional[int] = None


class InfluencersListResponse(BaseModel):
    """Paginated list of influencers. MATCHES: InfluencersResponseDto.kt"""
    influencers: list[InfluencerResponse]
    total: int
    limit: int
    offset: int


class InfluencerDetailResponse(BaseModel):
    """Full influencer details (includes system_instructions and more)."""
    id: str
    name: str
    display_name: str
    avatar_url: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    system_instructions: str
    personality_traits: Optional[dict] = None
    initial_greeting: Optional[str] = None
    suggested_messages: Optional[list[str]] = None
    is_active: str
    is_nsfw: bool = False
    parent_principal_id: Optional[str] = None
    source: Optional[str] = None
    created_at: str
    updated_at: str
    metadata: Optional[dict] = None
    # Populated only on POST /influencers/create. Mobile reads this field
    # to enqueue the welcome video. Other endpoints leave it null.
    starter_video_prompt: Optional[str] = None


class CreateInfluencerRequest(BaseModel):
    """Request body for creating a new AI influencer."""
    name: str = Field(min_length=3, max_length=50, pattern=r'^[a-z0-9_-]+$')
    display_name: str = Field(min_length=1, max_length=255)
    system_instructions: str = Field(min_length=10, max_length=10000)
    bot_principal_id: str = Field(min_length=1, max_length=255)
    avatar_url: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    personality_traits: Optional[dict] = None
    initial_greeting: Optional[str] = None
    suggested_messages: Optional[list[str]] = None
    is_nsfw: bool = False
    source: Optional[str] = None
    metadata: Optional[dict] = None


class GeneratePromptRequest(BaseModel):
    """Request to generate system instructions from a short concept.

    The wire-level field name is ambiguous because of a migration from the
    old Rust `yral-ai-chat` service (which used some other shape) to this
    Python service. The current production mobile app (Android + iOS, KMM
    shared code) sends `{"prompt": "..."}` while the backend canonical
    name is `concept`. Until mobile ships a fix and old app versions age
    out, accept BOTH names on input. Backend code keeps using
    `body.concept` — `validation_alias` only affects parsing the inbound
    body, not the field's Python attribute name.
    """
    concept: str = Field(validation_alias=AliasChoices("concept", "prompt"))
    language: Optional[str] = None


class GeneratePromptResponse(BaseModel):
    """Response with generated system instructions."""
    system_instructions: str


class ValidateAndGenerateRequest(BaseModel):
    """Request to validate a concept and generate all metadata.

    Same backwards-compat story as GeneratePromptRequest: mobile DTO
    `ValidateAndGenerateMetadataRequestDto` (in
    yral-mobile/shared/features/aiInfluencer/.../models/) sends
    `{"system_instructions": "..."}` to this endpoint, while the backend
    canonical name is `concept`. Accept both names. Mobile will rename
    when convenient; backend doesn't gate on it.
    """
    concept: str = Field(
        validation_alias=AliasChoices("concept", "system_instructions"),
    )
    language: Optional[str] = None


class ValidateAndGenerateResponse(BaseModel):
    """Response with generated name, greeting, starter messages, etc."""
    is_valid: bool
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    system_instructions: Optional[str] = None
    avatar_url: Optional[str] = None
    initial_greeting: Optional[str] = None
    suggested_messages: Optional[list[str]] = None
    personality_traits: Optional[dict] = None
    rejection_reason: Optional[str] = None


class UpdateSystemPromptRequest(BaseModel):
    """Request to update an influencer's system instructions."""
    system_instructions: str


class GenerateVideoPromptRequest(BaseModel):
    """Request to generate a video prompt for LTX model."""
    topic: Optional[str] = None


class GenerateVideoPromptResponse(BaseModel):
    """Response with generated video prompt."""
    prompt: str


# =========================================================================
# CONVERSATION MODELS
# =========================================================================

class ConversationInfluencer(BaseModel):
    """
    Influencer info embedded in conversation responses.
    MATCHES: ConversationInfluencerDto.kt in the mobile app.

    NOTE: suggested_messages is only included when the conversation
    has 1 or fewer messages (so the user sees starter prompts).
    """
    id: str
    name: str
    display_name: str
    avatar_url: str
    category: Optional[str] = None
    suggested_messages: Optional[list[str]] = None


class ConversationLastMessage(BaseModel):
    """
    Last message preview in conversation list responses.
    MATCHES: ConversationLastMessageDto.kt in the mobile app.
    """
    content: str
    role: str
    created_at: str


class ChatMessage(BaseModel):
    """
    A single chat message.
    MATCHES: ChatMessageDto.kt in the mobile app.

    IMPORTANT:
    - conversation_id is Optional (can be null in some responses)
    - media_urls is Optional list (can be null, not empty list)
    - content is Optional (can be null for image-only messages)
    """
    id: str
    conversation_id: Optional[str] = None
    role: str  # "user" or "assistant"
    content: Optional[str] = None
    message_type: str  # "text", "multimodal", "image", "audio"
    media_urls: Optional[list[str]] = None
    audio_url: Optional[str] = None
    audio_duration_seconds: Optional[int] = None
    token_count: Optional[int] = None
    created_at: str


class ConversationResponse(BaseModel):
    """
    A conversation in the inbox list.
    MATCHES: ConversationDto.kt in the mobile app.
    """
    id: str
    user_id: str
    influencer: ConversationInfluencer
    created_at: str
    updated_at: str
    message_count: int
    last_message: Optional[ConversationLastMessage] = None
    recent_messages: Optional[list[ChatMessage]] = None


class ConversationsListResponse(BaseModel):
    """Paginated list of conversations. MATCHES: ConversationsResponseDto.kt"""
    conversations: list[ConversationResponse]
    total: int
    limit: int
    offset: int


class CreateConversationRequest(BaseModel):
    """
    Request to create a new conversation with an AI influencer.
    MATCHES: CreateConversationRequestDto.kt in the mobile app.
    """
    influencer_id: str


class DeleteConversationResponse(BaseModel):
    """
    Response after deleting a conversation.
    MATCHES: DeleteConversationResponseDto.kt in the mobile app.
    """
    success: bool
    message: str
    deleted_conversation_id: str
    deleted_messages_count: int


# =========================================================================
# MESSAGE MODELS
# =========================================================================

class SendMessageRequest(BaseModel):
    """
    Request to send a message in a conversation.
    MATCHES: SendMessageRequestDto.kt in the mobile app.
    """
    content: Optional[str] = Field(default=None, max_length=50000)  # 50KB max
    message_type: Literal["text", "multimodal", "image", "audio"] = "text"
    media_urls: Optional[list[str]] = Field(default=None, max_length=10)  # Max 10 files
    audio_url: Optional[str] = Field(default=None, max_length=2000)
    audio_duration_seconds: Optional[int] = Field(default=None, ge=0, le=3600)  # 0-60 min
    client_message_id: Optional[str] = Field(default=None, max_length=255)


class SendMessageResponse(BaseModel):
    """
    Response after sending a message.
    MATCHES: SendMessageResponseDto.kt in the mobile app.

    IMPORTANT: assistant_message can be null!
    - For AI chat: always has both user_message and assistant_message
    - For human chat: only has user_message (assistant_message is null)
    """
    user_message: ChatMessage
    assistant_message: Optional[ChatMessage] = None


class GenerateImageRequest(BaseModel):
    """
    Request body for POST /api/v1/chat/conversations/{id}/images.

    If `prompt` is provided and non-empty, it is used verbatim. Otherwise
    the server synthesizes a prompt from the last ~10 messages of the
    conversation via Gemini.

    NOTE (2026-04-20): The YRAL mobile client does NOT currently call this
    endpoint — it exists for parity with the old Rust service and for any
    future admin / web / internal tooling that wants to trigger an in-chat
    image generation. Keep it here for API-parity even if unused.
    """
    prompt: Optional[str] = Field(default=None, max_length=2000)


class ConversationMessagesResponse(BaseModel):
    """
    Paginated list of messages in a conversation.
    MATCHES: ConversationMessagesResponseDto.kt in the mobile app.
    """
    conversation_id: str
    messages: list[ChatMessage]
    total: int
    limit: int
    offset: int


# =========================================================================
# MEDIA UPLOAD MODELS
# =========================================================================

class UploadResponse(BaseModel):
    """
    Response after uploading a media file.
    MATCHES: UploadResponseDto.kt in the mobile app.

    IMPORTANT: size is an integer (Long in Kotlin), not a string.
    """
    url: str
    storage_key: str
    type: Optional[str] = None
    size: Optional[int] = None  # Long in Kotlin — use int in Python
    mime_type: Optional[str] = None
    uploaded_at: Optional[str] = None


# =========================================================================
# HUMAN CHAT MODELS (NEW — not in the existing Rust service)
# =========================================================================

class CreateHumanConversationRequest(BaseModel):
    """Request to create a human-to-human conversation."""
    participant_id: str  # The other human's principal ID


class HumanConversationPeer(BaseModel):
    """Info about the other human in a human-to-human conversation."""
    id: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None

# ---------------------------------------------------------------------------
# replicate.py — Image generation via Replicate API.
#
# WHAT THIS FILE DOES:
# Generates AI images using the Replicate API (which hosts models like
# Flux from Black Forest Labs). Used for:
#   1. Generating AI influencer avatars during creation
#   2. Generating images inside chat conversations
#
# HOW IT WORKS:
# 1. We send a prompt to Replicate's API with "Prefer: wait" header
# 2. If the image is ready immediately, we get the URL back
# 3. If not, we poll every 2 seconds until it's done (up to 60 seconds)
# 4. Return the URL of the generated image
#
# PORTED FROM: yral-ai-chat/src/services/replicate.rs
# ---------------------------------------------------------------------------

import asyncio
import logging

import httpx

import config

logger = logging.getLogger(__name__)


async def generate_image(prompt: str, aspect_ratio: str = "1:1") -> str | None:
    """
    Generate an image from a text prompt.

    PARAMETERS:
        prompt: Text describing the image to generate
        aspect_ratio: "1:1" (square, for avatars) or "9:16" (portrait, for videos)

    RETURNS: URL of the generated image, or None if generation fails
    """
    if not config.REPLICATE_API_TOKEN:
        logger.warning("Replicate not configured — image generation disabled")
        return None

    return await _run_prediction(
        model=config.REPLICATE_MODEL,
        input_data={
            "prompt": prompt,
            "go_fast": True,
            "megapixels": "1",
            "aspect_ratio": aspect_ratio,
            "output_format": "jpg",
            "output_quality": 80,
        },
    )


async def generate_image_with_reference(
    prompt: str, reference_image_url: str, aspect_ratio: str = "9:16",
) -> str | None:
    """
    Generate an image using a reference image for visual consistency.

    Used when creating videos for an AI influencer — the reference image
    ensures the generated character looks consistent across videos.

    PARAMETERS:
        prompt: Text describing the scene
        reference_image_url: URL of the reference image (e.g., avatar)
        aspect_ratio: Usually "9:16" for portrait video thumbnails

    RETURNS: URL of the generated image, or None if generation fails
    """
    if not config.REPLICATE_API_TOKEN:
        return None

    return await _run_prediction(
        model="black-forest-labs/flux-kontext-dev",
        input_data={
            "prompt": prompt,
            "go_fast": True,
            "guidance": 2.5,
            "megapixels": "1",
            "num_inference_steps": 30,
            "aspect_ratio": aspect_ratio,
            "output_format": "jpg",
            "output_quality": 80,
            "input_image": reference_image_url,
        },
    )


async def _run_prediction(model: str, input_data: dict) -> str | None:
    """
    Run a Replicate prediction and return the output URL.

    Uses the "Prefer: wait" header to get synchronous responses when
    the model is warm. Falls back to polling if the prediction takes
    longer than the initial timeout.
    """
    url = f"https://api.replicate.com/v1/models/{model}/predictions"

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            # Send the prediction request with "Prefer: wait"
            # This tells Replicate to hold the connection open until
            # the prediction completes (instead of returning immediately
            # with a "processing" status).
            response = await client.post(
                url,
                json={"input": input_data},
                headers={
                    "Authorization": f"Bearer {config.REPLICATE_API_TOKEN}",
                    "Prefer": "wait",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code >= 400:
                logger.error(
                    f"Replicate API error: {response.status_code} — {response.text}"
                )
                return None

            data = response.json()
            status = data.get("status", "")

            # If the prediction completed immediately
            if status == "succeeded":
                return _extract_output_url(data.get("output"))

            # If still processing, poll for results
            if status in ("starting", "processing"):
                poll_url = (
                    data.get("urls", {}).get("get")
                    or f"https://api.replicate.com/v1/predictions/{data['id']}"
                )
                return await _poll_prediction(client, poll_url)

            # If failed or cancelled
            logger.error(f"Replicate prediction failed with status: {status}")
            return None

    except Exception as e:
        logger.error(f"Replicate image generation failed: {e}")
        return None


async def _poll_prediction(client: httpx.AsyncClient, url: str) -> str | None:
    """
    Poll a Replicate prediction until it completes.

    Checks every 2 seconds, up to 30 times (60 seconds total).
    """
    for _ in range(30):
        await asyncio.sleep(2)

        try:
            response = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {config.REPLICATE_API_TOKEN}",
                },
                timeout=10,
            )
            data = response.json()
            status = data.get("status", "")

            if status == "succeeded":
                return _extract_output_url(data.get("output"))
            elif status in ("failed", "canceled"):
                logger.error(f"Replicate prediction {status}")
                return None
            # else: still processing, continue polling

        except Exception as e:
            logger.warning(f"Replicate poll error: {e}")
            continue

    logger.error("Replicate prediction timed out after 60 seconds")
    return None


def _extract_output_url(output) -> str | None:
    """
    Extract the image URL from Replicate's output field.

    The output can be:
    - A list of URLs: ["https://..."]  → return the first one
    - A single URL string: "https://..." → return it
    - None → return None
    """
    if isinstance(output, list) and output:
        return str(output[0])
    elif isinstance(output, str):
        return output
    return None

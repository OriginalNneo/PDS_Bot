"""Gemini API service - uses the most basic free model with highest tokens."""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Model: gemini-2.5-flash-lite
# - Most basic/cost-efficient free tier model
# - 1,048,576 input tokens, 65,536 output tokens
# - Best free tier rate limits (15 RPM, 1000 RPD)


def get_gemini_client():
    """Get configured Gemini client. Returns None if not configured."""
    try:
        import google.generativeai as genai
        from config import load_config

        config = load_config()
        api_key = config.get("gemini_api_key", "").strip()
        if not api_key:
            logger.warning("Gemini: no api_key in config")
            return None

        genai.configure(api_key=api_key)
        model_name = config.get("gemini_model", "gemini-2.5-flash-lite")
        return genai.GenerativeModel(model_name)
    except Exception as e:
        logger.warning("Gemini client init failed: %s", e)
        return None


def generate_content(prompt: str, **kwargs) -> Optional[str]:
    """
    Generate content using Gemini API.

    Args:
        prompt: The text prompt to send
        **kwargs: Additional args for generate_content (e.g. generation_config)

    Returns:
        Generated text or None if not configured or on error
    """
    model = get_gemini_client()
    if not model:
        return None

    try:
        # Increase output tokens to avoid truncation of long receipt lists
        try:
            import google.generativeai as genai
            gen_config = genai.types.GenerationConfig(max_output_tokens=8192)
            response = model.generate_content(prompt, generation_config=gen_config, **kwargs)
        except (AttributeError, TypeError):
            response = model.generate_content(prompt, **kwargs)
        return response.text if response else None
    except Exception as e:
        logger.warning("Gemini generate_content failed: %s", e)
        return None


def _try_vision_with_model(prompt: str, img_or_uploaded, model_name: str) -> Optional[str]:
    """Try Gemini Vision with a specific model. Returns response text or None."""
    try:
        import google.generativeai as genai
        from config import load_config
        config = load_config()
        api_key = config.get("gemini_api_key", "").strip()
        if not api_key:
            return None
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        try:
            gen_config = genai.types.GenerationConfig(max_output_tokens=8192)
            response = model.generate_content([prompt, img_or_uploaded], generation_config=gen_config)
        except (AttributeError, TypeError):
            response = model.generate_content([prompt, img_or_uploaded])
        if response and response.text:
            return response.text
    except Exception as e:
        logger.warning("Gemini Vision with %s failed: %s", model_name, e)
    return None


def generate_content_with_image(prompt: str, image_path: str) -> Optional[str]:
    """
    Generate content using Gemini Vision API with an image file.
    Tries configured model first, then gemini-2.0-flash as fallback.

    Args:
        prompt: The text prompt to send
        image_path: Path to image file (JPG, PNG, WEBP, etc.)

    Returns:
        The generated text or None if not configured or on error
    """
    logger.info("Gemini Vision: attempting extraction from %s", image_path)
    try:
        import google.generativeai as genai
        from PIL import Image, ImageOps
        from config import load_config

        config = load_config()
        model_name = config.get("gemini_model", "gemini-2.5-flash-lite")
        fallback_model = "gemini-2.0-flash"

        # Prepare image
        img = Image.open(image_path)
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        # Try 1: Primary model (e.g. gemini-2.5-flash-lite)
        result = _try_vision_with_model(prompt, img, model_name)
        if result:
            logger.info("Gemini Vision (%s): extracted %d chars", model_name, len(result))
            return result

        # Try 2: Fallback model (gemini-2.0-flash)
        logger.info("Gemini Vision: retrying with %s", fallback_model)
        result = _try_vision_with_model(prompt, img, fallback_model)
        if result:
            logger.info("Gemini Vision (%s): extracted %d chars", fallback_model, len(result))
            return result

        # Try 3: genai.upload_file as fallback (different API path)
        uploaded = genai.upload_file(image_path)
        if uploaded:
            result = _try_vision_with_model(prompt, uploaded, model_name)
            if result:
                return result
            result = _try_vision_with_model(prompt, uploaded, fallback_model)
            if result:
                return result
    except Exception as e:
        logger.exception("Gemini Vision failed: %s", e)
    return None

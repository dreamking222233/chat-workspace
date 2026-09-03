"""Normalize image output options and infer them from natural-language prompts."""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd, sqrt
import re
import unicodedata


LEGACY_IMAGE_SIZES = {"256x256", "512x512", "1024x1024", "1024x1536", "1536x1024"}
IMAGE_QUALITIES = {"auto", "low", "medium", "high"}
# Older OpenAI image clients used DALL-E quality names.  Keep accepting those
# request values, but normalize them before a GPT Image-compatible provider is
# called so a legacy client cannot make gpt-image-2 fail with a 4xx response.
IMAGE_QUALITY_ALIASES = {"standard": "medium", "hd": "high"}
MIN_GPT_IMAGE_PIXELS = 655_360
MAX_GPT_IMAGE_PIXELS = 8_294_400
MAX_GPT_IMAGE_EDGE = 3_840
MIN_CUSTOM_IMAGE_EDGE = 256

_CHANNEL_SIZE_PRESETS = {
    ((1, 1), "standard"): "1024x1024",
    ((2, 3), "standard"): "1024x1536",
    ((3, 2), "standard"): "1536x1024",
    ((3, 4), "standard"): "1024x1365",
    ((4, 3), "standard"): "1365x1024",
    ((9, 16), "standard"): "1088x1920",
    ((16, 9), "standard"): "1920x1088",
    ((1, 1), "2k"): "2048x2048",
    ((9, 16), "2k"): "1440x2560",
    ((16, 9), "2k"): "2560x1440",
    ((9, 16), "4k"): "2160x3840",
    ((16, 9), "4k"): "3840x2160",
}

_SIZE_PATTERN = re.compile(r"^(\d{2,4})x(\d{2,4})$")
_PROMPT_SIZE_PATTERN = re.compile(r"(?<!\d)(\d{3,4})\s*[x*]\s*(\d{3,4})(?!\d)")
_ASPECT_PATTERN = re.compile(r"(?<!\d)(\d{1,2})\s*(?::|/|比)\s*(\d{1,2})(?!\d)")


@dataclass(frozen=True)
class ResolvedImageOptions:
    size: str
    quality: str | None


def normalize_image_size(value: str) -> str:
    """Validate legacy OpenAI sizes or a bounded channel pixel size."""

    normalized = str(value or "").strip().lower().replace("×", "x")
    if normalized == "auto" or normalized in LEGACY_IMAGE_SIZES:
        return normalized
    match = _SIZE_PATTERN.fullmatch(normalized)
    if not match:
        raise ValueError("unsupported image size")
    width, height = (int(match.group(1)), int(match.group(2)))
    pixels = width * height
    ratio = max(width, height) / min(width, height)
    if (
        min(width, height) < MIN_CUSTOM_IMAGE_EDGE
        or max(width, height) > MAX_GPT_IMAGE_EDGE
        or ratio > 3
        or pixels > MAX_GPT_IMAGE_PIXELS
    ):
        raise ValueError("unsupported image size")
    return f"{width}x{height}"


def normalize_image_quality(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().lower()
    normalized = IMAGE_QUALITY_ALIASES.get(normalized, normalized)
    if normalized not in IMAGE_QUALITIES:
        raise ValueError("unsupported image quality")
    return normalized


def resolve_image_options(prompt: str, size: str = "1024x1024", quality: str | None = None) -> ResolvedImageOptions:
    """Let explicit user wording override request defaults, then normalize values.

    The browser intentionally sends the user's original prompt. Keeping this
    conversion on the server makes direct image requests, edits, and model tool
    calls use the same deterministic mapping.
    """

    text = _normalized_prompt(prompt)
    inferred_size = _infer_size(text)
    inferred_quality = _infer_quality(text)
    return ResolvedImageOptions(
        size=normalize_image_size(inferred_size or size or "1024x1024"),
        quality=normalize_image_quality(inferred_quality or quality),
    )


def _normalized_prompt(prompt: str) -> str:
    return unicodedata.normalize("NFKC", str(prompt or "")).lower().replace("×", "x")


def _infer_size(text: str) -> str | None:
    explicit = _PROMPT_SIZE_PATTERN.search(text)
    if explicit:
        requested = f"{int(explicit.group(1))}x{int(explicit.group(2))}"
        try:
            return normalize_image_size(requested)
        except ValueError:
            # Preserve the requested orientation/ratio while moving the output
            # into the provider's supported pixel range.
            width, height = int(explicit.group(1)), int(explicit.group(2))
            return _dimensions_for_ratio(width, height, _resolution_tier(text, max(width, height)))

    aspect = _extract_aspect(text)
    tier = _resolution_tier(text)
    orientation = _orientation(text)
    if aspect is None and orientation:
        # Resolution words such as 2K/4K normally describe video-like canvases;
        # plain landscape/portrait requests retain the common 3:2 image shape.
        wide_ratio = (16, 9) if tier in {"720p", "1080p", "2k", "4k"} else (3, 2)
        aspect = wide_ratio if orientation == "landscape" else tuple(reversed(wide_ratio))
    if aspect is None and tier in {"720p", "1080p"}:
        aspect = (16, 9)
    if aspect is None and tier:
        aspect = (1, 1)
    if aspect is None:
        return None
    return _dimensions_for_ratio(aspect[0], aspect[1], tier or "standard")


def _extract_aspect(text: str) -> tuple[int, int] | None:
    match = _ASPECT_PATTERN.search(text)
    if not match:
        if re.search(r"\b(square|正方形|方图)\b", text):
            return (1, 1)
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0 or max(width, height) / min(width, height) > 3:
        return None
    divisor = gcd(width, height)
    return width // divisor, height // divisor


def _resolution_tier(text: str, requested_edge: int | None = None) -> str | None:
    if re.search(r"(?<!\d)4\s*k(?![a-z])|2160\s*p", text):
        return "4k"
    if re.search(r"(?<!\d)2\s*k(?![a-z])|1440\s*p", text):
        return "2k"
    if re.search(r"1080\s*p|\bfhd\b|full\s*hd", text):
        return "1080p"
    if re.search(r"720\s*p", text):
        return "720p"
    if re.search(r"(?<!\d)1\s*k(?![a-z])", text):
        return "standard"
    if requested_edge is not None:
        if requested_edge > 2_880:
            return "4k"
        if requested_edge > 2_048:
            return "2k"
        if requested_edge > 1_536:
            return "1080p"
        return "standard"
    return None


def _orientation(text: str) -> str | None:
    if re.search(r"横屏|横版|横向|宽幅|landscape|widescreen", text):
        return "landscape"
    if re.search(r"竖屏|竖版|纵向|portrait|手机壁纸", text):
        return "portrait"
    return None


def _infer_quality(text: str) -> str | None:
    if re.search(r"超高清|高清|高画质|高质量|精细|细节丰富|\bhigh(?:\s+quality)?\b|\bhd\b", text):
        return "high"
    if re.search(r"中等(?:质量|画质)?|\bmedium\b", text):
        return "medium"
    if re.search(r"低(?:质量|画质)|草稿|快速预览|\blow\b", text):
        return "low"
    return None


def _dimensions_for_ratio(width_ratio: int, height_ratio: int, tier: str) -> str:
    if width_ratio <= 0 or height_ratio <= 0:
        return "1024x1024"
    ratio = max(width_ratio, height_ratio) / min(width_ratio, height_ratio)
    if ratio > 3:
        return "1024x1024"

    divisor = gcd(width_ratio, height_ratio)
    reduced_ratio = (width_ratio // divisor, height_ratio // divisor)
    preset = _CHANNEL_SIZE_PRESETS.get((reduced_ratio, tier))
    if preset:
        return normalize_image_size(preset)

    if width_ratio == height_ratio:
        edge = {"standard": 1_024, "720p": 1_024, "1080p": 1_536, "2k": 2_048, "4k": 2_880}.get(tier, 1_024)
        return f"{edge}x{edge}"

    long_edge = {"standard": 1_536, "720p": 1_280, "1080p": 1_920, "2k": 2_560, "4k": 3_840}.get(tier, 1_536)
    if width_ratio >= height_ratio:
        width = long_edge
        height = _nearest_multiple(long_edge * height_ratio / width_ratio)
    else:
        height = long_edge
        width = _nearest_multiple(long_edge * width_ratio / height_ratio)
    width, height = _fit_pixel_bounds(width, height)
    return normalize_image_size(f"{width}x{height}")


def _nearest_multiple(value: float) -> int:
    return max(16, int(round(value / 16)) * 16)


def _fit_pixel_bounds(width: int, height: int) -> tuple[int, int]:
    pixels = width * height
    if pixels < MIN_GPT_IMAGE_PIXELS:
        scale = sqrt(MIN_GPT_IMAGE_PIXELS / pixels)
        width = _nearest_multiple(width * scale)
        height = _nearest_multiple(height * scale)
    pixels = width * height
    if pixels > MAX_GPT_IMAGE_PIXELS:
        scale = sqrt(MAX_GPT_IMAGE_PIXELS / pixels)
        width = max(16, int((width * scale) // 16) * 16)
        height = max(16, int((height * scale) // 16) * 16)
    if max(width, height) > MAX_GPT_IMAGE_EDGE:
        scale = MAX_GPT_IMAGE_EDGE / max(width, height)
        width = max(16, int((width * scale) // 16) * 16)
        height = max(16, int((height * scale) // 16) * 16)
    return width, height

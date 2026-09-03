import pytest

from app.schemas.common import GenerateImageToolArguments, ImageGenerationRequest
from app.services.image_options import normalize_image_quality, normalize_image_size, resolve_image_options


def test_prompt_maps_16_9_2k_high_quality_to_provider_options():
    options = resolve_image_options("生成一张抖音直播间截图 16:9 2K 高清")

    assert options.size == "2560x1440"
    assert options.quality == "high"


def test_prompt_supports_full_width_ratio_and_4k_portrait():
    options = resolve_image_options("制作手机海报，比例 9：16，4Ｋ 超高清")

    assert options.size == "2160x3840"
    assert options.quality == "high"


def test_prompt_without_output_directives_preserves_request_options():
    options = resolve_image_options("画一只蓝色小鸟", "1536x1024", "medium")

    assert options.size == "1536x1024"
    assert options.quality == "medium"


def test_custom_gpt_image_size_is_accepted_by_api_and_tool_schemas():
    request = ImageGenerationRequest(prompt="横屏海报", size="2048x1152", quality="high")
    tool = GenerateImageToolArguments(prompt="横屏海报", size="2048x1152", quality="high")

    assert request.size == tool.size == "2048x1152"
    assert request.quality == tool.quality == "high"


def test_exact_bounded_channel_size_is_preserved():
    assert resolve_image_options("生成 1920x1080 高清封面").size == "1920x1080"


@pytest.mark.parametrize(("legacy", "canonical"), [("standard", "medium"), ("hd", "high")])
def test_legacy_quality_names_are_normalized_for_gpt_image(legacy: str, canonical: str):
    request = ImageGenerationRequest(prompt="生成图片", quality=legacy)
    tool = GenerateImageToolArguments(prompt="生成图片", quality=legacy)

    assert normalize_image_quality(legacy) == canonical
    assert request.quality == canonical
    assert tool.quality == canonical


def test_workspace_single_image_contract_rejects_multiple_outputs():
    with pytest.raises(ValueError):
        ImageGenerationRequest(prompt="生成两张图", n=2)


@pytest.mark.parametrize("value", ["4096x2160", "1024x256", "128x128", "wide"])
def test_invalid_custom_image_sizes_are_rejected(value: str):
    with pytest.raises(ValueError, match="unsupported image size"):
        normalize_image_size(value)

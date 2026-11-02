from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Modality(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    SPEECH = "speech"
    VIDEO = "video"


class ModelCostTier(str, Enum):
    STANDARD = "STANDARD"
    HIGHER_COST = "HIGHER_COST"
    PREMIUM = "PREMIUM"
    INTERNAL = "INTERNAL"


PREMIUM_MODEL_KEYS = frozenset({
    "grok_4_3", "grok_4_6", "glm_5", "kimi_k2_5", "qwen3_vl_235b_a22b",
    "kimi_k2_thinking", "glm_4_7", "devstral_2_123b",
})
HIGHER_COST_MODEL_KEYS = frozenset({
    "deepseek_v3_2", "qwen3_coder_480b_a35b", "deepseek_v3_1",
    "magistral_small_2509", "mistral_large_3",
})


@dataclass(frozen=True, slots=True)
class ModelInfo:
    key: str
    display_name: str
    provider: str
    model_id: str
    context_window: int
    max_output: int | None
    input_price_per_million: float
    output_price_per_million: float
    input_modalities: frozenset[Modality]
    output_modalities: frozenset[Modality]
    status: str = "Working"
    model_type: str = "chat"
    user_visible: bool = True
    supports_vision: bool = False
    supports_reasoning: bool = False
    reasoning_efforts: tuple[str, ...] = ()
    backend_provider: str = "ais_api"
    cost_tier: ModelCostTier = ModelCostTier.STANDARD

    def supports(self, modality: Modality) -> bool:
        return modality in self.input_modalities


T = Modality.TEXT
I = Modality.IMAGE  # noqa: E741 - compact modality notation used by the catalog.
S = Modality.SPEECH
V = Modality.VIDEO


def _model(
    key: str,
    name: str,
    provider: str,
    model_id: str,
    context: int,
    max_output: int | None,
    input_price: float,
    output_price: float,
    inputs: tuple[Modality, ...] = (T,),
    model_type: str = "chat",
    user_visible: bool = True,
    supports_vision: bool = False,
    supports_reasoning: bool = False,
    reasoning_efforts: tuple[str, ...] = (),
) -> ModelInfo:
    return ModelInfo(
        key=key,
        display_name=name,
        provider=provider,
        model_id=model_id,
        context_window=context,
        max_output=max_output,
        input_price_per_million=input_price,
        output_price_per_million=output_price,
        input_modalities=frozenset(inputs),
        output_modalities=frozenset({T}),
        model_type=model_type,
        user_visible=user_visible,
        supports_vision=supports_vision,
        supports_reasoning=supports_reasoning,
        reasoning_efforts=reasoning_efforts,
        cost_tier=(ModelCostTier.INTERNAL if not user_visible else
                   ModelCostTier.PREMIUM if key in PREMIUM_MODEL_KEYS else
                   ModelCostTier.HIGHER_COST if key in HIGHER_COST_MODEL_KEYS else
                   ModelCostTier.STANDARD),
    )


_MODELS = (
    _model("grok_4_3", "Grok 4.3", "xAI", "xai.grok-4.3", 1_000_000, None, 1.25, 2.5, (T, I), "vision", supports_vision=True),
    _model(
        "grok_4_6",
        "Grok 4.6",
        "xAI",
        "xai.grok-4.6",
        500_000,
        None,
        2.2,
        6.6,
        (T, I),
        "reasoning",
        supports_vision=True,
        supports_reasoning=True,
        reasoning_efforts=("low", "medium", "high", "xhigh"),
    ),
    _model("gemma_4_31b", "Gemma 4 31B", "Google", "google.gemma-4-31b", 256_000, None, 0.14, 0.4, (T, I, V), "multimodal", supports_vision=True),
    _model("gemma_4_e2b", "Gemma 4 E2B", "Google", "google.gemma-4-e2b", 128_000, None, 0.04, 0.08, (T, I, S, V), "multimodal", supports_vision=True),
    _model("gemma_4_26b_a4b", "Gemma 4 26B-A4B", "Google", "google.gemma-4-26b-a4b", 256_000, None, 0.13, 0.4, (T, I, V), "multimodal", supports_vision=True),
    _model("palmyra_vision_7b", "Palmyra Vision 7B", "Writer", "writer.palmyra-vision-7b", 4_000, 4_000, 0.15, 0.6, (T, I), "vision", supports_vision=True),
    _model("glm_5", "GLM 5", "Z.AI", "zai.glm-5", 200_000, 128_000, 1.0, 3.2),
    _model("qwen3_coder_next", "Qwen3 Coder Next", "Qwen", "qwen.qwen3-coder-next", 256_000, 16_000, 0.5, 1.2, model_type="code"),
    _model("glm_4_7_flash", "GLM 4.7 Flash", "Z.AI", "zai.glm-4.7-flash", 203_000, 4_000, 0.07, 0.4),
    _model("kimi_k2_5", "Kimi K2.5", "Moonshot AI", "moonshotai.kimi-k2.5", 256_000, 16_000, 0.6, 3.0, (T, I), "vision", supports_vision=True),
    _model("glm_4_7", "GLM 4.7", "Z.AI", "zai.glm-4.7", 203_000, 4_000, 0.6, 2.2),
    _model("minimax_m2_5", "MiniMax M2.5", "MiniMax", "minimax.minimax-m2.5", 196_000, 8_000, 0.3, 1.2),
    _model("minimax_m2_1", "MiniMax M2.1", "MiniMax", "minimax.minimax-m2.1", 196_000, 8_000, 0.3, 1.2),
    _model("deepseek_v3_2", "DeepSeek V3.2", "DeepSeek", "deepseek.v3.2", 164_000, 8_000, 0.62, 1.85),
    _model("nemotron_super_3_120b", "NVIDIA Nemotron 3 Super 120B", "NVIDIA", "nvidia.nemotron-super-3-120b", 256_000, 32_000, 0.15, 0.65),
    _model("devstral_2_123b", "Devstral 2 123B", "Mistral AI", "mistral.devstral-2-123b", 256_000, 32_000, 0.4, 2.0, model_type="code"),
    _model("nemotron_nano_3_30b", "Nemotron Nano 3 30B", "NVIDIA", "nvidia.nemotron-nano-3-30b", 256_000, 8_000, 0.06, 0.24),
    _model("qwen3_next_80b_a3b", "Qwen3 Next 80B A3B", "Qwen", "qwen.qwen3-next-80b-a3b-instruct", 256_000, 8_000, 0.15, 1.2),
    _model("qwen3_32b", "Qwen3 32B", "Qwen", "qwen.qwen3-32b", 32_000, 8_000, 0.15, 0.6),
    _model("gemma_3_12b_it", "Gemma 3 12B IT", "Google", "google.gemma-3-12b-it", 128_000, 8_000, 0.09, 0.29, (T, I), "vision", supports_vision=True),
    _model("gemma_3_4b_it", "Gemma 3 4B IT", "Google", "google.gemma-3-4b-it", 128_000, 8_000, 0.04, 0.08, (T, I), "vision", supports_vision=True),
    _model("gpt_oss_120b", "GPT OSS 120B", "OpenAI", "openai.gpt-oss-120b", 128_000, 16_000, 0.15, 0.6),
    _model("minimax_m2", "MiniMax M2", "MiniMax", "minimax.minimax-m2", 1_000_000, 8_000, 0.3, 1.2),
    _model("voxtral_small_24b_2507", "Voxtral Small 24B 2507", "Mistral AI", "mistral.voxtral-small-24b-2507", 32_000, None, 0.1, 0.3, (T, S), "speech"),
    _model("deepseek_v3_1", "DeepSeek-V3.1", "DeepSeek", "deepseek.v3.1", 128_000, 8_000, 0.58, 1.68),
    _model("gemma_3_27b_it", "Gemma 3 27B IT", "Google", "google.gemma-3-27b-it", 128_000, 8_000, 0.23, 0.38, (T, I), "vision", supports_vision=True),
    _model("qwen3_coder_480b_a35b", "Qwen3 Coder 480B A35B Instruct", "Qwen", "qwen.qwen3-coder-480b-a35b-instruct", 128_000, 16_000, 0.45, 1.8, model_type="code"),
    _model("gpt_oss_safeguard_20b", "GPT OSS Safeguard 20B", "OpenAI", "openai.gpt-oss-safeguard-20b", 128_000, 16_000, 0.07, 0.2, model_type="safeguard", user_visible=False),
    _model("gpt_oss_safeguard_120b", "GPT OSS Safeguard 120B", "OpenAI", "openai.gpt-oss-safeguard-120b", 128_000, 16_000, 0.15, 0.6, model_type="safeguard", user_visible=False),
    _model("voxtral_mini_3b_2507", "Voxtral Mini 3B 2507", "Mistral AI", "mistral.voxtral-mini-3b-2507", 32_000, None, 0.04, 0.04, (T, S), "speech"),
    _model("qwen3_coder_30b_a3b", "Qwen3-Coder-30B-A3B-Instruct", "Qwen", "qwen.qwen3-coder-30b-a3b-instruct", 256_000, 16_000, 0.15, 0.6, model_type="code"),
    _model("kimi_k2_thinking", "Kimi K2 Thinking", "Moonshot AI", "moonshotai.kimi-k2-thinking", 256_000, 16_000, 0.6, 2.5, model_type="reasoning"),
    _model("qwen3_vl_235b_a22b", "Qwen3 VL 235B A22B", "Qwen", "qwen.qwen3-vl-235b-a22b-instruct", 256_000, 8_000, 0.53, 2.66, (T, I), "vision", supports_vision=True),
    _model("magistral_small_2509", "Magistral Small 2509", "Mistral AI", "mistral.magistral-small-2509", 128_000, 40_000, 0.5, 1.5, (T, I), "reasoning"),
    _model("qwen3_235b_a22b_2507", "Qwen3 235B A22B 2507", "Qwen", "qwen.qwen3-235b-a22b-2507", 256_000, 8_000, 0.22, 0.88),
    _model("gpt_oss_20b", "GPT OSS 20B", "OpenAI", "openai.gpt-oss-20b", 128_000, 16_000, 0.07, 0.3),
    _model("mistral_large_3", "Mistral Large 3", "Mistral AI", "mistral.mistral-large-3-675b-instruct", 256_000, 32_000, 0.5, 1.5, (T, I), "vision", supports_vision=True),
    _model("ministral_14b_3_0", "Ministral 14B 3.0", "Mistral AI", "mistral.ministral-3-14b-instruct", 128_000, 8_000, 0.2, 0.2, (T, I), "vision", supports_vision=True),
    _model("ministral_3_8b", "Ministral 3 8B", "Mistral AI", "mistral.ministral-3-8b-instruct", 128_000, 8_000, 0.15, 0.15, (T, I), "vision", supports_vision=True),
    _model("ministral_3b", "Ministral 3B", "Mistral AI", "mistral.ministral-3-3b-instruct", 128_000, 8_000, 0.1, 0.1, (T, I), "vision", supports_vision=True),
    _model("nemotron_nano_12b_v2_vl", "NVIDIA Nemotron Nano 12B v2 VL BF16", "NVIDIA", "nvidia.nemotron-nano-12b-v2", 128_000, 8_000, 0.2, 0.6, (T, I), "vision", supports_vision=True),
    _model("nemotron_nano_9b_v2", "NVIDIA Nemotron Nano 9B v2", "NVIDIA", "nvidia.nemotron-nano-9b-v2", 128_000, 8_000, 0.06, 0.23),
)

MODEL_REGISTRY: dict[str, ModelInfo] = {model.key: model for model in _MODELS}

if len(MODEL_REGISTRY) != len(_MODELS):
    raise RuntimeError("Duplicate keys found in the model registry")


def get_model(model_key: str) -> ModelInfo:
    try:
        return MODEL_REGISTRY[model_key]
    except KeyError as error:
        raise KeyError(f"Unknown model: {model_key}") from error


def user_models() -> tuple[ModelInfo, ...]:
    return tuple(model for model in _MODELS if model.user_visible)


def provider_key(provider: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", provider.lower()).strip("_")


def providers() -> tuple[tuple[str, str], ...]:
    names = sorted({model.provider for model in user_models()}, key=str.casefold)
    return tuple((provider_key(name), name) for name in names)


def provider_name(key: str) -> str | None:
    return dict(providers()).get(key)


def models_for_provider(provider: str) -> tuple[ModelInfo, ...]:
    return tuple(
        model for model in user_models() if model.provider == provider
    )

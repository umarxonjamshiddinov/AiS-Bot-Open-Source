from __future__ import annotations

from dataclasses import replace

from AI.models import MODEL_REGISTRY, ModelInfo, user_models

AMAZON_BEDROCK = "amazon_bedrock"

_BACKEND_DISPLAY_NAMES = {
    AMAZON_BEDROCK: "AiS API",
    "ais_api": "AiS API",
}


def backend_display_name(backend_provider: str) -> str:
    """Return the user-facing host/provider name for a registered backend."""
    return _BACKEND_DISPLAY_NAMES.get(backend_provider, backend_provider)


def models_for_backend(backend_provider: str) -> tuple[ModelInfo, ...]:
    if backend_provider == AMAZON_BEDROCK:
        return user_models()
    return ()


def model_for_backend(backend_provider: str, model_key: str) -> ModelInfo:
    if backend_provider == AMAZON_BEDROCK:
        model = MODEL_REGISTRY.get(model_key)
        if model is None:
            raise KeyError(f"Unknown model: {model_key}")
        return replace(model, backend_provider="ais_api")
    raise KeyError(f"Unknown provider or model: {backend_provider}/{model_key}")


def provider_is_configured(backend_provider: str) -> bool:
    return backend_provider == AMAZON_BEDROCK


def available_providers() -> tuple[tuple[str, str], ...]:
    # Preserve persisted selector/callback IDs for existing users.
    return ((AMAZON_BEDROCK, "☁️ AiS API"),)


def creators_for_backend(backend_provider: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        model.provider
        for model in models_for_backend(backend_provider)
    ))


def models_for_creator(backend_provider: str, creator: str) -> tuple[ModelInfo, ...]:
    return tuple(
        model
        for model in models_for_backend(backend_provider)
        if model.provider == creator
    )

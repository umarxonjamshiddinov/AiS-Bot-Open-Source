from AI.models import ModelCostTier, ModelInfo, MODEL_REGISTRY, Modality
from AI.capabilities import is_input_wired


def model_is_selectable(model: ModelInfo) -> bool:
    return (model.user_visible and model.cost_tier is not ModelCostTier.INTERNAL
            and model.status == "Working" and is_input_wired(model, Modality.TEXT))


def requires_premium(model: ModelInfo) -> bool:
    return model.cost_tier is ModelCostTier.PREMIUM


def selection_available(model: ModelInfo, account: dict) -> bool:
    """Pure shared menu/random policy; repositories revalidate at selection/inference."""
    limit = account.get("effective_daily_limit")
    return (
        model_is_selectable(model)
        and (limit is None or account.get("requests_today", 0) < limit)
        and (not requires_premium(model) or bool(account.get("premium_models_allowed", False)))
        and (model.cost_tier is not ModelCostTier.HIGHER_COST
             or account.get("higher_cost_requests_today", 0) < account.get("higher_cost_daily_limit", 15))
    )


def cost_tier_for(model_key: str) -> ModelCostTier:
    model = MODEL_REGISTRY.get(model_key)
    return model.cost_tier if model else ModelCostTier.STANDARD


def requires_grok(model: ModelInfo) -> bool:
    """Legacy API retained for integrations; new policy uses requires_premium."""
    return model.provider.casefold() == "xai" or model.key.startswith("grok_")

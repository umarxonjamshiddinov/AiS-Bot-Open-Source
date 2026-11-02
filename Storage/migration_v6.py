"""Additive cost-tier permissions and usage metadata; preserve v1-v5 data."""

STATEMENTS = (
    "ALTER TABLE user_access ADD COLUMN premium_models_allowed INTEGER NOT NULL DEFAULT 0 CHECK (premium_models_allowed IN (0, 1))",
    "UPDATE user_access SET premium_models_allowed = grok_allowed",
    # Older permission writers continue to work; new writers update both fields.
    """CREATE TRIGGER legacy_grok_permission_update AFTER UPDATE OF grok_allowed ON user_access
       BEGIN UPDATE user_access SET premium_models_allowed = NEW.grok_allowed
       WHERE telegram_user_id = NEW.telegram_user_id; END""",
    """CREATE TRIGGER legacy_grok_permission_insert AFTER INSERT ON user_access
       WHEN NEW.grok_allowed = 1
       BEGIN UPDATE user_access SET premium_models_allowed = 1
       WHERE telegram_user_id = NEW.telegram_user_id; END""",
    "CREATE INDEX ix_access_premium ON user_access(premium_models_allowed, telegram_user_id)",
    "ALTER TABLE usage_requests ADD COLUMN requires_premium INTEGER NOT NULL DEFAULT 0 CHECK (requires_premium IN (0, 1))",
    "UPDATE usage_requests SET requires_premium = requires_grok",
    "ALTER TABLE usage_attempts ADD COLUMN model_cost_tier TEXT NOT NULL DEFAULT 'STANDARD' CHECK (model_cost_tier IN ('STANDARD', 'HIGHER_COST', 'PREMIUM', 'INTERNAL'))",
    "ALTER TABLE usage_attempts ADD COLUMN is_main_inference INTEGER NOT NULL DEFAULT 1 CHECK (is_main_inference IN (0, 1))",
    # This is the immutable v6 catalog snapshot, not the live routing policy.
    """UPDATE usage_attempts SET model_cost_tier = 'HIGHER_COST'
       WHERE model IN ('deepseek_v3_2', 'qwen3_coder_480b_a35b', 'deepseek_v3_1', 'magistral_small_2509', 'mistral_large_3')""",
    """UPDATE usage_attempts SET model_cost_tier = 'PREMIUM'
       WHERE model IN ('grok_4_3', 'grok_4_6', 'glm_5', 'kimi_k2_5', 'qwen3_vl_235b_a22b', 'kimi_k2_thinking', 'glm_4_7', 'devstral_2_123b')""",
    "UPDATE usage_attempts SET model_cost_tier = 'INTERNAL' WHERE model IN ('gpt_oss_safeguard_20b', 'gpt_oss_safeguard_120b')",
    """UPDATE usage_attempts SET is_main_inference = 0 WHERE purpose = 'moderation'
       OR model != (SELECT model FROM usage_requests WHERE request_id = usage_attempts.request_id)""",
    "CREATE INDEX ix_attempts_higher_daily ON usage_attempts(telegram_user_id, timestamp, model_cost_tier, counts_toward_limit, is_main_inference)",
)

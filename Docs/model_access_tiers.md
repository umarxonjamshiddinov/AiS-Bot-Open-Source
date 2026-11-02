# Model access and usage limits

The model catalog and cost-tier metadata are maintained in `AI/models.py`.
Two internal Safety models are hidden from user selection. All public model
requests still pass through the configured AiS API; account permissions in the
bot do not grant upstream API access.

The default account and request limits are configurable:

- Standard accounts: `STANDARD_DAILY_LIMIT` (default 50 requests per UTC day).
- Trusted accounts: `TRUSTED_DAILY_LIMIT` (default 500; `0` means unlimited).
- Higher-cost models: shared `HIGHER_COST_DAILY_LIMIT` (default 15 per UTC day).
- Individual limits and access grants may be managed by authorized admins.
- Premium Models access is separate from Trusted status and uses the configured
  Premium activation code.

The bot rechecks access and quota during model selection and before inference.
Changing a tier does not bypass a ban or active restriction. Model selection
does not itself consume inference quota.

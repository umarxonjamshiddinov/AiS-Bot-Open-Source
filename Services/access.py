from dataclasses import dataclass
import secrets

from Storage.interfaces import AccountRepository
from Storage.models import ActivationResult, utc_timestamp
from config import Settings


@dataclass(slots=True)
class AccountService:
    repository: AccountRepository
    settings: Settings

    async def status(self, user_id: int) -> dict:
        return await self.repository.snapshot(user_id, utc_timestamp())

    async def apply_trusted(self, user_id: int) -> dict:
        return await self.repository.apply(user_id, utc_timestamp(), tuple(sorted(self.settings.admin_user_ids)))

    async def activate(self, user_id: int, purpose: str, code: str) -> ActivationResult:
        if purpose == "premium_models":
            purpose = "grok"  # Preserve the existing env secret and cooldown ledger.
        codes = {"grok": self.settings.grok_access_code, "premium": self.settings.premium_activation_code}
        if purpose not in codes:
            raise ValueError("Unknown activation purpose")
        expected = codes[purpose]
        matches = bool(expected) and len(code) <= 512 and secrets.compare_digest(code.strip().encode(), expected.encode())
        # Neither the code nor its hash crosses the storage boundary.
        return await self.repository.redeem(
            user_id=user_id, purpose=purpose, matches=matches, configured=bool(expected), timestamp=utc_timestamp(),
            max_attempts=self.settings.activation_max_attempts, window_seconds=self.settings.activation_window_seconds,
            cooldown_seconds=self.settings.activation_cooldown_seconds)

    def contains_secret(self, text: str) -> bool:
        return any(code and code in text for code in (self.settings.grok_access_code, self.settings.premium_activation_code))

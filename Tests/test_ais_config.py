import os
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings


class AISConfigTests(unittest.TestCase):
    def environment(self):
        return dict(BOT_TOKEN="test-only", AIS_API_KEY="ais-test-only", AIS_API_BASE_URL="https://ais.example",
                    TAVILY_API_KEY="tavily-test-only")

    def test_central_config_without_aws_or_azure(self):
        with patch.dict(os.environ, self.environment(), clear=True), patch("config.load_dotenv"):
            settings = Settings.load()
        self.assertEqual(settings.ais_api_key, "ais-test-only")
        self.assertEqual(settings.support_contact, "")
        self.assertEqual(settings.ai_retry_count, 0)
        self.assertNotIn("ais-test-only", repr(settings))
        self.assertNotIn("safety-test-only", repr(settings))
        self.assertFalse(hasattr(settings, "aws_region"))
        self.assertFalse(hasattr(settings, "openai_api_key"))
        self.assertFalse(hasattr(settings, "openai_base_url"))

    def test_missing_gateway_secret_reports_name_only(self):
        env = self.environment()
        del env["AIS_API_KEY"]
        with patch.dict(os.environ, env, clear=True), patch("config.load_dotenv"):
            with self.assertRaisesRegex(RuntimeError, "AIS_API_KEY"):
                Settings.load()

    def test_env_example_covers_settings_and_contains_no_credentials(self):
        example = Path(__file__).resolve().parents[1] / ".env.example"
        values = {}
        for line in example.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                name, value = line.split("=", 1)
                values[name] = value
        expected = {
            "BOT_TOKEN", "AIS_API_KEY", "AIS_API_BASE_URL", "TAVILY_API_KEY",
            "SUPPORT_CONTACT", "AI_MAX_CONCURRENT_REQUESTS",
            "AI_DEFAULT_MAX_OUTPUT_TOKENS", "AI_RETRY_COUNT",
            "AI_REQUEST_TIMEOUT_SECONDS", "SAFETY_SHADOW_ENABLED",
            "SAFETY_SHADOW_QUEUE_SIZE", "SAFETY_SHADOW_TIMEOUT_SECONDS",
            "SQLITE_PATH", "SQLITE_BUSY_TIMEOUT_MS", "ADMIN_USER_IDS",
            "USAGE_RETENTION_DAYS", "MODERATION_RETENTION_DAYS",
            "AUDIT_RETENTION_DAYS", "STANDARD_DAILY_LIMIT",
            "TRUSTED_DAILY_LIMIT", "HIGHER_COST_DAILY_LIMIT",
            "GROK_ACCESS_CODE", "PREMIUM_ACTIVATION_CODE",
            "ACTIVATION_MAX_ATTEMPTS", "ACTIVATION_WINDOW_SECONDS",
            "ACTIVATION_COOLDOWN_SECONDS",
        }
        self.assertEqual(set(values), expected)
        for secret_name in (
            "BOT_TOKEN", "AIS_API_KEY", "AIS_API_BASE_URL", "TAVILY_API_KEY",
            "GROK_ACCESS_CODE", "PREMIUM_ACTIVATION_CODE",
        ):
            self.assertEqual(values[secret_name], "")

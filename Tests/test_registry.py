import unittest

from AI.models import (
    MODEL_REGISTRY,
    Modality,
    models_for_provider,
    providers,
    user_models,
)
from Handlers.menus import all_callback_data


class RegistryTests(unittest.TestCase):
    def test_model_registry_has_unique_ids(self) -> None:
        registry_ids = {model.model_id for model in MODEL_REGISTRY.values()}
        self.assertEqual(len(MODEL_REGISTRY), 42)
        self.assertEqual(len(registry_ids), len(MODEL_REGISTRY))
        self.assertEqual(MODEL_REGISTRY["grok_4_6"].model_id, "xai.grok-4.6")

    def test_registry_metadata_is_valid(self) -> None:
        for key, model in MODEL_REGISTRY.items():
            with self.subTest(model=key):
                self.assertEqual(key, model.key)
                self.assertGreater(model.context_window, 0)
                self.assertTrue(model.input_modalities)
                self.assertEqual(model.output_modalities, frozenset({Modality.TEXT}))
                self.assertTrue(model.max_output is None or model.max_output > 0)
                self.assertGreaterEqual(model.input_price_per_million, 0)
                self.assertGreaterEqual(model.output_price_per_million, 0)

    def test_safeguards_are_internal(self) -> None:
        safeguards = [m for m in MODEL_REGISTRY.values() if m.model_type == "safeguard"]
        self.assertEqual(len(safeguards), 2)
        self.assertTrue(all(not model.user_visible for model in safeguards))
        self.assertEqual(len(user_models()), 40)

    def test_provider_menus_are_registry_driven(self) -> None:
        provider_rows = providers()
        self.assertEqual(len(provider_rows), 11)
        for _, name in provider_rows:
            self.assertTrue(models_for_provider(name))
        self.assertEqual(
            sum(len(models_for_provider(name)) for _, name in provider_rows), 40
        )

    def test_callback_data_respects_telegram_limit(self) -> None:
        for value in all_callback_data():
            self.assertLessEqual(len(value.encode("utf-8")), 64, value)

    def test_capabilities_from_catalog(self) -> None:
        self.assertEqual(
            sum(m.supports(Modality.IMAGE) for m in MODEL_REGISTRY.values()), 17
        )
        self.assertEqual(
            sum(m.supports(Modality.SPEECH) for m in MODEL_REGISTRY.values()), 3
        )
        self.assertEqual(
            sum(m.supports(Modality.VIDEO) for m in MODEL_REGISTRY.values()), 3
        )

    def test_native_vision_is_an_explicit_per_model_capability(self) -> None:
        expected = {
            "grok_4_3",
            "grok_4_6",
            "gemma_4_31b",
            "gemma_4_e2b",
            "gemma_4_26b_a4b",
            "palmyra_vision_7b",
            "kimi_k2_5",
            "qwen3_vl_235b_a22b",
            "gemma_3_27b_it",
            "gemma_3_12b_it",
            "gemma_3_4b_it",
            "mistral_large_3",
            "ministral_14b_3_0",
            "ministral_3_8b",
            "ministral_3b",
            "nemotron_nano_12b_v2_vl",
        }
        actual = {
            model.key for model in MODEL_REGISTRY.values() if model.supports_vision
        }
        self.assertEqual(actual, expected)

    def test_all_other_user_models_are_non_native_vision(self) -> None:
        native = {model.key for model in user_models() if model.supports_vision}
        non_native = {model.key for model in user_models() if not model.supports_vision}
        self.assertEqual(len(native), 16)
        self.assertEqual(len(non_native), 24)
        self.assertIn("voxtral_small_24b_2507", non_native)
        self.assertIn("voxtral_mini_3b_2507", non_native)
        self.assertIn("deepseek_v3_1", non_native)

    def test_safeguards_have_no_user_facing_vision_capability(self) -> None:
        safeguards = [m for m in MODEL_REGISTRY.values() if not m.user_visible]
        self.assertTrue(all(not model.supports_vision for model in safeguards))

    def test_provider_routes_are_owned_by_gateway(self):
        self.assertTrue(all(m.backend_provider == "ais_api" for m in user_models()))
        self.assertTrue(all(not hasattr(m, "api_route") and not hasattr(m, "region") for m in user_models()))

    def test_grok_reasoning_ui_metadata_is_preserved(self):
        model = MODEL_REGISTRY["grok_4_6"]
        self.assertTrue(model.supports_reasoning)
        self.assertEqual(model.reasoning_efforts, ("low", "medium", "high", "xhigh"))

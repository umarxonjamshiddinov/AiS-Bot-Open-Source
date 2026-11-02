import importlib
import unittest

from AI.models import get_model
from AI.prompts import AIS_IDENTITY, is_identity_question, system_prompt_for
from Search.web_search import WebSearchService, is_model_query, should_search
from Utils.telegram_formatter import split_pages, telegram_html


class ComponentTests(unittest.TestCase):
    def test_main_import_does_not_start_polling(self) -> None:
        self.assertIsNotNone(importlib.import_module("main").build_application)

    def test_identity_detection(self) -> None:
        self.assertTrue(is_identity_question("Какая ты модель?"))
        self.assertTrue(is_identity_question("What's under the hood?"))
        self.assertTrue(is_identity_question("What provider are you using?"))
        self.assertIn("AiS", AIS_IDENTITY)

    def test_runtime_identity_prompt_is_ais_first_and_model_specific(self) -> None:
        prompt = system_prompt_for(get_model("grok_4_6"), "Amazon Bedrock")
        self.assertIn("Product/assistant identity: AiS", prompt)
        self.assertIn("Selected underlying model: Grok 4.6", prompt)
        self.assertIn("AI provider/host for this selected model: Amazon Bedrock", prompt)

    def test_web_search_trigger(self) -> None:
        self.assertTrue(should_search("Проверь последние новости AWS"))
        self.assertFalse(should_search("Перепиши этот текст короче"))

    def test_model_queries_are_detected_for_catalog_grounding(self) -> None:
        self.assertTrue(is_model_query("какие актуальные модели GPT и Grok"))

    def test_web_answer_keeps_only_real_result_urls(self) -> None:
        results = [
            {"title": "Real", "url": "https://example.com/real", "content": "Fact"}
        ]
        answer = WebSearchService.append_real_sources(
            "[fake](https://fake.example/x) and https://example.com/real", results
        )
        self.assertNotIn("fake.example", answer)
        self.assertIn("https://example.com/real", answer)

    def test_telegram_formatter(self) -> None:
        rendered = telegram_html("**bold** and `code`")
        self.assertEqual(rendered, "<b>bold</b> and <code>code</code>")
        text = "word " * 2000
        pages = split_pages(text, limit=500)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 500 for page in pages))

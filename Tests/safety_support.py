"""Synthetic samples live only in StorageTestCase's isolated test database."""
import asyncio
import json
from dataclasses import replace
from uuid import uuid4

from AI.adaptive_safety import AdaptiveModerator, AdaptiveResult, policy_fingerprint
from AI.safety_policy import POLICY_VERSION, SafetyAssessment, decide
from Services.safety_shadow import SafetyProbe, SafetyReviewCache, evaluation_record
from Storage.models import utc_timestamp
from Storage.safety_queries import save_evaluation
from Tests.moderation_support import ModerationTestCase
from Tests.test_usage_persistence import completion


def facts(**changes):
    return replace(SafetyAssessment(None, "benign", "none", .95, "not_applicable", "direct", "benign_request", "allow", "answer"), **changes)


def harmful(**changes):
    return replace(facts(category="cyber_abuse", intent="unauthorized_access", severity="high", confidence=.98,
                         authorization="unauthorized", reason_code="explicit_unauthorized_access"), **changes)


def assessment_response(value):
    return completion(tokens=(3, 2, 5), content=json.dumps({
        "policy_version": POLICY_VERSION, "category": value.category, "intent": value.intent, "severity": value.severity,
        "confidence": value.confidence, "authorization": value.authorization, "request_context": value.request_context,
        "reason_code": value.reason_code, "decision": value.model_decision, "recommended_action": value.recommended_action}))


class AdaptiveTestCase(ModerationTestCase):
    def adaptive_service(self, outcomes):
        service, create = self.service(outcomes)
        self.moderation.adaptive_moderator = AdaptiveModerator(service.router._client, service.router._semaphore)
        self.moderation.review_cache = SafetyReviewCache(self.moderation.secrets)
        self.admin.safety_fingerprint = self.moderation.adaptive_moderator.fingerprint
        return service, create

    async def seed_samples(self, n=1, *, value=None, production_action="allow", origin="live", fingerprint=None):
        assert "test-runs" in self.path.parts
        classifier = self.moderation.adaptive_moderator
        if classifier is None:
            service, _ = self.adaptive_service([])
            classifier = self.moderation.adaptive_moderator
        value = value or facts()
        result = AdaptiveResult(value, decide(value), classifier.model_key, "amazon_bedrock", classifier.fingerprint)
        records = []
        for _ in range(n):
            record = evaluation_record(request_id="synthetic-test-" + uuid4().hex, user_id=2, model="gemma_3_12b_it",
                provider="amazon_bedrock", classifier=classifier, context_kind="text", mode="shadow", result=result,
                latency_ms=12, production_action=production_action, production_policy="ais_safety_v1", origin=origin)
            if fingerprint:
                record["policy_fingerprint"] = fingerprint
            records.append(record)
        def seed(c):
            ids = []
            for record in records:
                c.execute("INSERT INTO usage_requests (request_id, telegram_user_id, timestamp, model, provider, status, counts_toward_limit) VALUES (?, 2, ?, 'gemma_3_12b_it', 'amazon_bedrock', 'succeeded', 0)", (record["request_id"], record["created_at"]))
                ids.append(save_evaluation(c, record))
            return ids
        return await self.storage.database.run(seed, write=True)

    async def activate(self):
        await self.seed_samples(200)
        return await self.admin.act(999, "activate_safety_v5", reason="Approve after synthetic test evidence")

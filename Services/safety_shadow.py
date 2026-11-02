"""Bounded, non-authoritative shadow evaluation; never calls warning/ban repositories."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from uuid import uuid4

from AI.input_moderation import ModerationUnavailableError
from AI.safety_context import SafetyInput, build_safety_context
from AI.safety_policy import POLICY_VERSION
from AI.telemetry import TokenUsage, attempt_observer, safe_error_code
from Storage.models import utc_timestamp
from Utils.privacy import private_reason


class SafetyProbe:
    """Reuses provider observation. Shadow never reserves user quota or modifies product traces."""
    def __init__(self, delegate=None):
        self.delegate = delegate
        self.usage = []
        self.attempts = 0

    async def start(self, model):
        attempt_id = await self.delegate.start(model) if self.delegate else uuid4().hex
        self.attempts += 1
        return attempt_id

    async def finish(self, **values):
        if self.delegate:
            await self.delegate.finish(**values)
        self.usage.append(values["usage"])

    def totals(self):
        def total(field):
            values = [getattr(u, field) for u in self.usage]
            return sum(values) if values and len(values) == self.attempts and all(v is not None for v in values) else None
        return {"input_tokens": total("input_tokens"), "output_tokens": total("output_tokens"),
                "total_tokens": total("total_tokens"), "attempt_count": self.attempts}


class SafetyReviewCache:
    """RAM only, bounded and redacted. Durable evaluations contain no request content."""
    def __init__(self, secrets=(), *, ttl=3600, max_items=128, max_bytes=8_000_000):
        self.secrets, self.ttl = secrets, ttl
        self.max_items, self.max_bytes = max_items, max_bytes
        self.items, self.bytes = OrderedDict(), 0

    def put(self, request_id, user_id, fingerprint, context, image_bytes=None):
        def redact(value):
            if isinstance(value, str):
                return private_reason(value, self.secrets, max_length=len(value) + 32)
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            return value
        # Redact before JSON escapes quotes/backslashes/newlines in credentials.
        text = private_reason(json.dumps(redact(context), ensure_ascii=False, indent=2), self.secrets, max_length=9000)
        # Original images can contain credentials. Keep only redacted text for
        # administrative review; image bytes live only in the in-flight job.
        size = len(text.encode("utf-8"))
        self._remove(request_id)
        while self.items and (len(self.items) >= self.max_items or self.bytes + size > self.max_bytes):
            self._remove(next(iter(self.items)))
        if size > self.max_bytes:
            return
        self.items[request_id] = {"user_id": user_id, "fingerprint": fingerprint, "text": text,
                                  "has_image": image_bytes is not None, "expires": time.monotonic() + self.ttl, "bytes": size}
        self.bytes += size

    def _remove(self, key):
        entry = self.items.pop(key, None)
        if entry:
            self.bytes -= entry["bytes"]

    def get(self, request_id, user_id, fingerprint):
        entry = self.items.get(request_id)
        if entry and entry["expires"] <= time.monotonic():
            self._remove(request_id)
            return None
        if not entry or entry["user_id"] != user_id or entry["fingerprint"] != fingerprint:
            return None
        return entry

    def clear(self):
        self.items.clear()
        self.bytes = 0


def evaluation_record(*, request_id, user_id, model, provider, classifier, context_kind, mode,
                      result=None, error=None, latency_ms=None, probe=None, production_action=None,
                      production_policy=None, production_status=None, production_latency_ms=None,
                      event_id=None, origin="live"):
    assessment = result.assessment if result else None
    decision = result.decision if result else None
    tokens = probe.totals() if probe else {"input_tokens": None, "output_tokens": None, "total_tokens": None, "attempt_count": 0}
    return {
        "request_id": request_id, "telegram_user_id": user_id, "created_at": utc_timestamp(), "mode": mode,
        "origin": origin, "policy_version": POLICY_VERSION, "policy_fingerprint": classifier.fingerprint,
        "classifier_model": result.model if result else (classifier.vision_model_key if context_kind == "image" else classifier.model_key),
        "classifier_provider": result.provider if result else "amazon_bedrock", "selected_model": model, "selected_provider": provider,
        "production_policy_version": production_policy, "production_action": production_action, "production_status": production_status,
        "production_latency_ms": production_latency_ms, "status": "completed" if result else "error", "action": decision.action.value if decision else None,
        "category": assessment.category if assessment else None, "intent": assessment.intent if assessment else None,
        "severity": assessment.severity if assessment else None, "confidence": assessment.confidence if assessment else None,
        "confidence_source": assessment.confidence_source if assessment else "unavailable",
        "authorization": assessment.authorization if assessment else None, "request_context": assessment.request_context if assessment else None,
        "reason_code": decision.reason_code if decision else None, "classifier_reason_code": assessment.reason_code if assessment else None,
        "model_decision": assessment.model_decision if assessment else None, "recommended_action": assessment.recommended_action if assessment else None,
        "safe_mode": decision.safe_mode if decision else None, "latency_ms": latency_ms, **tokens,
        "contract_retries": result.contract_retries if result else getattr(error, "safety_contract_retries", 0),
        "error_type": type(error).__name__ if error else None, "error_code": safe_error_code(error) if error else None,
        "is_disagreement": 0, "context_kind": context_kind, "moderation_event_id": event_id,
    }


@dataclass(slots=True)
class ShadowJob:
    request_id: str
    user_id: int
    model: str
    provider: str
    prompt: str
    history: list
    recent_refusals: list
    safety_input: SafetyInput | None
    image_bytes: bytes | None
    mime_type: str
    production_action: str | None
    production_policy: str
    production_status: str
    production_latency_ms: int | None
    event_id: int | None


class SafetyShadow:
    def __init__(
        self,
        repository,
        classifier,
        cache,
        *,
        queue_size=32,
        timeout_seconds=20.0,
        owns_client=True,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.repository, self.classifier, self.cache = repository, classifier, cache
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.timeout_seconds = timeout_seconds
        self.owns_client, self.task, self.closing = owns_client, None, False
        self.dropped, self.storage_errors = 0, 0

    def start(self):
        if self.task is None:
            self.closing = False
            self.task = asyncio.create_task(self._run(), name="adaptive-safety-shadow")

    def schedule(self, job: ShadowJob) -> bool:
        if self.closing or self.task is None or self.task.done():
            return False
        if len(job.image_bytes or b"") > 4_000_000:
            self.dropped += 1
            return False
        job.history = job.history[-8:]
        try:
            self.queue.put_nowait(job)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    def runtime_stats(self):
        return {"queued": self.queue.qsize(), "dropped_since_start": self.dropped, "storage_errors_since_start": self.storage_errors}

    async def _run(self):
        while True:
            job = await self.queue.get()
            try:
                await self._evaluate(job)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.storage_errors += 1
                logging.getLogger(__name__).error("Safety shadow failed type=%s", type(error).__name__)
            finally:
                self.queue.task_done()

    async def _evaluate(self, job):
        started, probe, result, error = time.perf_counter(), SafetyProbe(), None, None
        token = attempt_observer.set(probe)
        try:
            if job.image_bytes is not None and len(job.image_bytes) > 4_000_000:
                raise ModerationUnavailableError("Shadow image exceeds memory budget")
            context = build_safety_context(job.prompt, job.history, job.recent_refusals, job.safety_input)
            self.cache.put(job.request_id, job.user_id, self.classifier.fingerprint, context, job.image_bytes)
            result = await asyncio.wait_for(
                self.classifier.assess(
                    context, image_bytes=job.image_bytes, mime_type=job.mime_type
                ),
                timeout=self.timeout_seconds,
            )
        except BaseException as caught:
            error = caught
        finally:
            attempt_observer.reset(token)
        record = evaluation_record(request_id=job.request_id, user_id=job.user_id, model=job.model, provider=job.provider,
            classifier=self.classifier, context_kind="image" if job.image_bytes is not None else (job.safety_input.kind if job.safety_input else "text"),
            mode="shadow", result=result, error=error, latency_ms=round((time.perf_counter() - started) * 1000), probe=probe,
            production_action=job.production_action, production_policy=job.production_policy, production_status=job.production_status,
            production_latency_ms=job.production_latency_ms, event_id=job.event_id)
        await self.repository.save_safety_evaluation(record)
        if isinstance(error, asyncio.CancelledError):
            raise error

    async def close(self):
        self.closing = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
            self.dropped += 1
        if self.owns_client:
            await self.classifier.client.close()

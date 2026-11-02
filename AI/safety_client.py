"""Safety classification through fixed server-owned AiS API policies only."""
import json
from contextvars import ContextVar
from types import SimpleNamespace

from AI.client import AISClient
from AI.input_moderation import POLICY_PROMPT, ModerationUnavailableError
from AI.moderation import ProviderModerationError, ModerationResult
from AI.telemetry import TokenUsage, attempt_observer

selected_safety_model = ContextVar("selected_safety_model", default="gpt_oss_20b")
safety_receipt = ContextVar("ais_safety_receipt", default=None)


class RemoteSafetyClient:
    def __init__(self, *, gateway: AISClient):
        self._gateway = gateway

    async def complete(self, *, model, messages, system_prompt, max_tokens,
                       image_bytes=None, mime_type="image/jpeg"):
        # Compatibility with the reviewed local policy parsers; classifier model
        # and system prompt are NEVER sent to the server.
        from AI.adaptive_safety import ASSESSMENT_PROMPT, FORMAT_RETRY_INSTRUCTION
        from AI.models import get_model
        if system_prompt == POLICY_PROMPT:
            policy = "ais_safety_v1"
            payload = json.loads(messages[-1]["content"])
            context = {"user_request": payload["current_request"],
                       "recent_conversation": payload["conversation_context"],
                       "kind": "image" if image_bytes is not None else "text"}
        elif system_prompt in {ASSESSMENT_PROMPT, ASSESSMENT_PROMPT + "\n\n" + FORMAT_RETRY_INSTRUCTION}:
            policy = "ais_safety_v2"
            payload = json.loads(messages[-1]["content"])
            context = {**payload["current_request"], "recent_conversation": payload["recent_conversation"],
                       "previous_refused_requests": payload["previous_refused_requests"],
                       "history_truncated": payload["history_truncated"]}
        else:
            raise ModerationUnavailableError("Only fixed AiS Safety policies are supported")
        request = {"selected_model": get_model(selected_safety_model.get()).model_id,
                   "policy_version": policy, "context": context}
        if image_bytes is not None:
            import base64
            request["image"] = f"data:{mime_type};base64," + base64.b64encode(image_bytes).decode("ascii")
        observer = attempt_observer.get()
        attempt = await observer.start(model) if observer else None
        usage, error, status = TokenUsage(), None, "error"
        import time
        started = time.perf_counter()
        try:
            result = await self._gateway._client.post("safety/assess", body=request, cast_to=dict[str, object])
            safety_receipt.set(result.get("receipt"))
            usage = TokenUsage.from_response(SimpleNamespace(usage=result.get("usage")))
            if result.get("policy_version") != policy:
                raise ModerationUnavailableError("Invalid remote Safety contract")
            if result.get("action") == "refuse" and result.get("assessment") is None:
                raise ProviderModerationError(result=ModerationResult(True, "ais_api_safety",
                    "provider_refusal", policy_action="refuse"))
            if policy == "ais_safety_v1":
                content = result["decision"]
            else:
                facts = result["assessment"]
                content = {**facts, "policy_version": policy,
                           "decision": facts["model_decision"]}
                del content["model_decision"]
            status = "succeeded"
            return json.dumps(content, ensure_ascii=False)
        except (ModerationUnavailableError, ProviderModerationError) as caught:
            error = caught
            raise
        except Exception as caught:
            error = self._gateway._safe_error(caught)
            raise error from None
        finally:
            if observer and attempt:
                await observer.finish(attempt_id=attempt, model=model, status=status,
                    latency_ms=round((time.perf_counter() - started) * 1000), usage=usage,
                    error=error, inference_started=False)

    async def close(self):
        pass  # Shared gateway is owned by AppContext.

from types import SimpleNamespace

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from Handlers.admin_panel import admin_callback, admin_reason, render
from Services.safety_shadow import SafetyReviewCache
from Storage.errors import StorageError
from Tests.safety_support import AdaptiveTestCase, facts, harmful
from Tests.test_admin_panel import fake_callback, fake_message


class SafetyAdminTests(AdaptiveTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.memory = MemoryStorage()
        self.fsm = FSMContext(self.memory, StorageKey(bot_id=1, chat_id=999, user_id=999))
        self.app = SimpleNamespace(admin=self.admin, users=self.users, state=self.state, moderation=self.moderation,
                                   settings=self.settings, accounts=self.accounts, notifications=SimpleNamespace(wake=lambda: None))

    async def asyncTearDown(self):
        await self.memory.close()
        await super().asyncTearDown()

    async def test_cannot_activate_before_200_comparable_live_samples(self):
        await self.seed_samples(199)
        await self.seed_samples(5, origin="smoke")
        await self.seed_samples(5, fingerprint="0" * 64)
        summary = await self.admin.safety_summary(999)
        self.assertEqual(summary["readiness"]["comparable"], 199)
        self.assertFalse(summary["readiness"]["ready"])
        with self.assertRaises(ValueError):
            await self.admin.act(999, "activate_safety_v5", reason="Too early")
        self.assertEqual((await self.storage.moderation.policy_state())["policy_version"], "ais_safety_v1")
        text, markup = await render("adm:safety", 999, self.app, self.fsm)
        self.assertNotIn("adm:safety_switch:v5", str(markup))
        self.assertIn("199/200", text)

    async def test_ready_evidence_does_not_automatically_change_policy(self):
        await self.seed_samples(200)
        self.assertTrue((await self.admin.safety_summary(999))["readiness"]["ready"])
        self.assertEqual((await self.storage.moderation.policy_state())["policy_version"], "ais_safety_v1")

    async def test_classifier_errors_are_included_in_promotion_gate(self):
        await self.seed_samples(200)
        errors = await self.seed_samples(10)
        for eid in errors:
            await self.storage.database.run(lambda c: c.execute(
                "UPDATE safety_evaluations SET status = 'error', action = NULL, is_disagreement = 0 WHERE id = ?", (eid,)), write=True)
        ready = (await self.admin.safety_summary(999))["readiness"]
        self.assertEqual(ready["comparable"], 200)
        self.assertGreater(ready["error_rate"], .02)
        self.assertFalse(ready["ready"])

    async def test_unreviewed_disagreements_block_activation_until_human_label(self):
        await self.seed_samples(199)
        eid = (await self.seed_samples(value=harmful()))[0]
        self.assertFalse((await self.admin.safety_summary(999))["readiness"]["ready"])
        with self.assertRaises(ValueError):
            await self.admin.act(999, "activate_safety_v5", reason="Not yet reviewed")
        await self.admin.act(999, "review_safety", safety_evaluation_id=eid, expected_safety_action="violation", reason="Reviewed direct unauthorized intent")
        self.assertTrue((await self.admin.safety_summary(999))["readiness"]["ready"])

    async def test_unclear_or_confirmed_v5_error_does_not_satisfy_review_gate(self):
        await self.seed_samples(199)
        eid = (await self.seed_samples(value=harmful()))[0]
        first = await self.admin.act(999, "review_safety", safety_evaluation_id=eid, expected_safety_action="unclear", reason="Context unavailable")
        self.assertEqual((await self.admin.safety_summary(999))["readiness"]["unreviewed"], 1)
        await self.admin.act(999, "review_safety", safety_evaluation_id=eid, expected_safety_action="allow",
                             expected_safety_review_id=first.audit_id, reason="Human review found a false violation")
        ready = (await self.admin.safety_summary(999))["readiness"]
        self.assertEqual(ready["known_errors"], 1)
        self.assertFalse(ready["ready"])

    async def test_review_is_optimistic_audited_and_cannot_modify_warnings(self):
        eid = (await self.seed_samples(value=harmful()))[0]
        review = await self.admin.act(999, "review_safety", safety_evaluation_id=eid, expected_safety_action="violation", reason="Verified source context")
        with self.assertRaises(ValueError):
            await self.admin.act(999, "review_safety", safety_evaluation_id=eid, expected_safety_action="allow", reason="Stale second reviewer")
        case = await self.admin.detail(999, "safety", eid)
        self.assertEqual((case["reviewed_by"], case["review_admin_action_id"], case["expected_action"]), (999, review.audit_id, "violation"))
        self.assertEqual(await self.rows("SELECT * FROM user_warnings"), [])
        self.assertEqual(await self.rows("SELECT * FROM moderation_sanctions"), [])
        with self.assertRaises(ValueError):
            await self.admin.act(999, "review_safety", user_id=3, safety_evaluation_id=eid, expected_safety_action="allow", reason="Wrong user")

    async def test_policy_change_and_review_rollback_if_audit_fails(self):
        await self.seed_samples(200)
        await self.storage.database.run(lambda c: c.execute("CREATE TRIGGER reject_safety_audit BEFORE INSERT ON admin_audit_log BEGIN SELECT RAISE(ABORT, 'test'); END"), write=True)
        with self.assertRaises(StorageError):
            await self.admin.act(999, "activate_safety_v5", reason="Audit must be atomic")
        self.assertEqual((await self.storage.moderation.policy_state())["policy_version"], "ais_safety_v1")

    async def test_manual_activation_and_rollback_survive_restart(self):
        await self.seed_samples(200)
        result = await self.admin.act(999, "activate_safety_v5", reason="Human approval after reviewed evidence")
        policy = await self.storage.moderation.policy_state()
        self.assertEqual((policy["policy_version"], policy["changed_by"], policy["admin_action_id"]), ("ais_safety_v2", 999, result.audit_id))
        await self.reopen()
        self.assertEqual((await self.storage.moderation.policy_state())["policy_version"], "ais_safety_v2")
        await self.admin.act(999, "rollback_safety_v4", reason="Operator requested rollback")
        self.assertEqual((await self.storage.moderation.policy_state())["policy_version"], "ais_safety_v1")

    async def test_all_safety_views_and_callbacks_require_admin_identity(self):
        eid = (await self.seed_samples(value=harmful()))[0]
        for route in ("adm:safety", "adm:safety_confidence", "adm:safety_cases:pending:0", f"adm:safety_case:{eid}",
                      f"adm:safety_context:{eid}:0", f"adm:safety_review:violation:{eid}", "adm:safety_switch:v5"):
            callback = fake_callback(route, 2)
            await admin_callback(callback, self.app, self.fsm)
            self.assertIn("только для администратора", callback.answer.call_args.args[0])
        with self.assertRaises(PermissionError):
            await render(f"adm:safety_context:{eid}:0", 2, self.app, self.fsm)
        with self.assertRaises(PermissionError):
            await self.admin.act(2, "activate_safety_v5", reason="Forged role")

    async def test_admin_buttons_use_reason_nonce_and_recheck_identity(self):
        eid = (await self.seed_samples(value=harmful()))[0]
        await admin_callback(fake_callback(f"adm:safety_review:violation:{eid}"), self.app, self.fsm)
        await admin_reason(fake_message(text="Verified comparison"), self.app, self.fsm)
        data = await self.fsm.get_data()
        self.admin.admin_ids = frozenset()
        await admin_callback(fake_callback(f"adm:confirm:{data['nonce']}"), self.app, self.fsm)
        self.assertIsNone((await self.storage.admins.detail("safety", eid))["expected_action"])
        self.admin.admin_ids = frozenset({999})
        await admin_callback(fake_callback(f"adm:confirm:{data['nonce']}"), self.app, self.fsm)
        self.assertEqual((await self.admin.detail(999, "safety", eid))["expected_action"], "violation")

    async def test_switch_button_does_not_activate_before_explicit_confirmation(self):
        await self.seed_samples(200)
        await admin_callback(fake_callback("adm:safety_switch:v5"), self.app, self.fsm)
        await admin_reason(fake_message(text="Promote reviewed policy"), self.app, self.fsm)
        self.assertEqual((await self.storage.moderation.policy_state())["policy_version"], "ais_safety_v1")
        data = await self.fsm.get_data()
        await admin_callback(fake_callback(f"adm:confirm:{data['nonce']}"), self.app, self.fsm)
        self.assertEqual((await self.storage.moderation.policy_state())["policy_version"], "ais_safety_v2")

    async def test_safety_dashboard_cards_pagination_and_redacted_context(self):
        ids = await self.seed_samples(10, value=harmful())
        first = await self.admin.page(999, "safety", 0, filter_value="pending")
        second = await self.admin.page(999, "safety", 1, filter_value="pending")
        self.assertFalse({e["id"] for e in first.items} & {e["id"] for e in second.items})
        entry = await self.admin.detail(999, "safety", ids[0])
        self.moderation.review_cache.put(entry["request_id"], 2, entry["policy_fingerprint"],
                                         {"request": "<b>literal</b> " + self.settings.grok_access_code})
        for route in ("adm:safety", "adm:safety_confidence", "adm:safety_cases:pending:0", f"adm:safety_case:{ids[0]}", f"adm:safety_context:{ids[0]}:0"):
            text, markup = await render(route, 999, self.app, self.fsm)
            self.assertNotIn(self.settings.grok_access_code, text)
            self.assertLess(len(text), 4096)
            self.assertTrue(all(len(b.callback_data.encode()) <= 64 for row in markup.inline_keyboard for b in row))
        self.moderation.review_cache.clear()
        text, _ = await render(f"adm:safety_context:{ids[0]}:0", 999, self.app, self.fsm)
        self.assertIn("Контекст недоступен", text)

    async def test_observability_uses_real_counts_and_keeps_unknown_latency_unknown(self):
        ids = await self.seed_samples(2)
        eid = (await self.seed_samples(value=harmful()))[0]
        await self.admin.act(999, "review_safety", safety_evaluation_id=eid, expected_safety_action="violation", reason="Human label")
        await self.ban()
        appeal = await self.submit_appeal()
        await self.admin.act(999, "approve_appeal", appeal_id=appeal["id"], reason="Overturned finding")
        data = await self.admin.safety_summary(999)
        self.assertEqual(sum(row["count"] for row in data["actions"]), 3)
        self.assertEqual(data["auto_blocks"], 1)
        self.assertEqual(data["appeal_overturns"], 1)
        self.assertEqual(data["warnings"], {"issued": 3, "revoked": 1, "revoked_percent": 33.3})
        self.assertEqual(data["latency"]["p95"], 12)
        self.assertEqual(data["confidence_review"][0]["reviewed"], 1)

    async def test_empty_dashboard_does_not_invent_accuracy_or_latency(self):
        data = await self.admin.safety_summary(999)
        self.assertIsNone(data["latency"]["p50"])
        self.assertIsNone(data["latency"]["p95"])
        self.assertIsNone(data["warnings"]["revoked_percent"])
        text, _ = await render("adm:safety_confidence", 999, self.app, self.fsm)
        self.assertIn("точность неизвестна", text)

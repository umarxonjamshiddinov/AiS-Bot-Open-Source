import asyncio
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from AI.models import MODEL_REGISTRY, ModelCostTier, Modality
from Handlers.menus import creator_models_menu, providers_menu
from Handlers.models import random_model_handler, select_model
from Storage.maintenance import verify_database
from Storage.migrations import MIGRATIONS, migrate
from Storage.models import AccessDeniedError, UserSelection, utc_timestamp
from Storage.sqlite import SQLiteStorage
from Tests.storage_support import StorageTestCase
from Tests import test_usage_persistence as usage_fixtures
from Tests import test_moderation_reset as migration_fixtures
from Tests import test_admin_panel as panel_fixtures


PREMIUM = {'grok_4_3', 'grok_4_6', 'glm_5', 'kimi_k2_5', 'qwen3_vl_235b_a22b',
           'kimi_k2_thinking', 'glm_4_7', 'devstral_2_123b'}
HIGHER = {'deepseek_v3_2', 'qwen3_coder_480b_a35b', 'deepseek_v3_1',
          'magistral_small_2509', 'mistral_large_3'}


class ModelAccessTierTests(StorageTestCase):
    service = usage_fixtures.UsagePersistenceTests.service
    request = usage_fixtures.UsagePersistenceTests.request

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.add_user()

    async def seed_usage(self, standard=0, higher=0, *, timestamp=None, user_id=2):
        timestamp = timestamp or utc_timestamp()
        rows = [(f'seed-{user_id}-{n}', 'deepseek_v3_1' if n < higher else 'gpt_oss_120b',
                 'HIGHER_COST' if n < higher else 'STANDARD') for n in range(standard + higher)]
        def seed(c):
            c.executemany('INSERT INTO usage_requests (request_id, telegram_user_id, timestamp, model, provider, status, counts_toward_limit) '
                          "VALUES (?, ?, ?, ?, 'amazon_bedrock', 'succeeded', 0)",
                          [(key, user_id, timestamp, model) for key, model, tier in rows])
            c.executemany('INSERT INTO usage_attempts (attempt_id, request_id, telegram_user_id, timestamp, model, provider, status, model_cost_tier) '
                          "VALUES (?, ?, ?, ?, ?, 'amazon_bedrock', 'succeeded', ?)",
                          [(key, key, user_id, timestamp, model, tier) for key, model, tier in rows])
        await self.storage.database.run(seed, write=True)

    async def eligible(self, user_id=2):
        return await self.storage.users.available_models(user_id, utc_timestamp(), 'amazon_bedrock')

    def test_exact_tier_catalog_and_preserved_capabilities(self):
        self.assertEqual(len(MODEL_REGISTRY), 42)
        self.assertEqual(Counter(m.cost_tier.value for m in MODEL_REGISTRY.values()),
                         {'STANDARD': 27, 'HIGHER_COST': 5, 'PREMIUM': 8, 'INTERNAL': 2})
        self.assertEqual({m.key for m in MODEL_REGISTRY.values() if m.cost_tier == ModelCostTier.PREMIUM}, PREMIUM)
        self.assertEqual({m.key for m in MODEL_REGISTRY.values() if m.cost_tier == ModelCostTier.HIGHER_COST}, HIGHER)
        internal = [m for m in MODEL_REGISTRY.values() if m.cost_tier == ModelCostTier.INTERNAL]
        self.assertTrue(all(not m.user_visible and 'safeguard' in m.key for m in internal))
        magistral = MODEL_REGISTRY['magistral_small_2509']
        self.assertIn(Modality.IMAGE, magistral.input_modalities)
        self.assertFalse(magistral.supports_vision)
        self.assertFalse(magistral.supports_reasoning)
        self.assertFalse(MODEL_REGISTRY['kimi_k2_thinking'].supports_reasoning)

    async def test_all_premium_locked_then_grant_all_and_persist(self):
        for key in PREMIUM:
            with self.assertRaises(AccessDeniedError) as caught:
                await self.users.select_model((10, 2), 'amazon_bedrock', key)
            self.assertEqual(caught.exception.decision.code, 'premium_locked')
        await self.admin.act(999, 'grant_premium', user_id=2, reason='Enable engines')
        for key in PREMIUM:
            await self.users.select_model((10, 2), 'amazon_bedrock', key)
        await self.reopen()
        status = await self.accounts.status(2)
        self.assertEqual((status['premium_models_allowed'], status['grok_allowed'], status['requests_today']), (1, 1, 0))
        self.assertEqual(status['tier'], 'STANDARD')

    async def test_menu_locks_all_premium_not_only_grok(self):
        for model in MODEL_REGISTRY.values():
            if not model.user_visible:
                continue
            for allowed in (False, True):
                menu = creator_models_menu('amazon_bedrock', model.provider, premium_models_allowed=allowed)
                button = next(b for row in menu.inline_keyboard for b in row if b.callback_data.endswith(':' + model.key))
                self.assertEqual(button.text.startswith('🔒 '), model.key in PREMIUM and not allowed)
        self.assertEqual(providers_menu().inline_keyboard[0][0].text, '🎲 Random Model')

    async def test_repository_selection_cannot_bypass_permission_flag(self):
        with self.assertRaises(AccessDeniedError):
            await self.storage.users.select_model(10, 2, UserSelection('amazon_bedrock', 'glm_5'),
                                                  utc_timestamp(), requires_premium=False)
        with self.assertRaises(ValueError):
            await self.users.select_model((10, 2), 'amazon_bedrock', 'gpt_oss_safeguard_20b')
        decision = await self.storage.usage.start_request(request_id='internal', user_id=2,
            timestamp=utc_timestamp(), model='gpt_oss_safeguard_20b', provider='amazon_bedrock')
        self.assertEqual(decision.code, 'internal_model')

    async def test_crafted_callback_does_not_select_locked_or_internal(self):
        callback = SimpleNamespace(data='model:amazon_bedrock:Z.ai:glm_5',
            from_user=SimpleNamespace(id=2), message=SimpleNamespace(chat=SimpleNamespace(id=10), edit_text=AsyncMock()), answer=AsyncMock())
        context = SimpleNamespace(
            users=self.users, settings=SimpleNamespace(support_contact="")
        )
        await select_model(callback, context)
        self.assertIsNone((await self.users.selection((10, 2))).model_key)
        callback.message.edit_text.assert_not_awaited()
        callback.data = 'model:amazon_bedrock:OpenAI:gpt_oss_safeguard_20b'
        await select_model(callback, context)
        self.assertIsNone((await self.users.selection((10, 2))).model_key)

    async def test_activation_legacy_and_canonical_grant_all_premium_not_trusted(self):
        for user, purpose in ((2, 'grok'), (3, 'premium_models')):
            if user == 3:
                await self.add_user(3)
            result = await self.accounts.activate(user, purpose, self.settings.grok_access_code)
            self.assertEqual(result.status, 'activated')
            status = await self.accounts.status(user)
            self.assertEqual((status['premium_models_allowed'], status['tier']), (1, 'STANDARD'))
            await self.users.select_model((10, user), 'amazon_bedrock', 'glm_5')
        dump = await self.storage.database.run(lambda c: '\n'.join(c.iterdump()))
        self.assertNotIn(self.settings.grok_access_code, dump)

    async def test_trusted_activation_still_does_not_grant_premium(self):
        await self.accounts.activate(2, 'premium', self.settings.premium_activation_code)
        status = await self.accounts.status(2)
        self.assertEqual((status['tier'], status['effective_daily_limit'], status['premium_models_allowed']), ('TRUSTED', 500, 0))

    async def test_canonical_admin_audit_and_legacy_api_remain_compatible(self):
        await self.admin.act(999, 'grant_premium', user_id=2, reason='Enable premium')
        await self.admin.act(999, 'revoke_premium', user_id=2, reason='Revoke premium')
        audit = await self.admin.audit(999, 2)
        self.assertEqual([r['action'] for r in audit], ['revoke_premium', 'grant_premium'])
        for row in audit:
            self.assertEqual((row['admin_id'], row['target_user_id']), (999, 2))
            self.assertTrue(row['timestamp'])
            self.assertIn('premium_models_allowed', row['metadata_json'])
        await self.admin.act(999, 'grant_grok', user_id=2, reason='Old integration')
        self.assertEqual((await self.accounts.status(2))['premium_models_allowed'], 1)
        await self.admin.act(999, 'revoke_grok', user_id=2, reason='Old integration')
        self.assertEqual((await self.accounts.status(2))['premium_models_allowed'], 0)

    async def test_higher_boundary_0_14_15_and_shared_pool(self):
        service, create = self.service([usage_fixtures.completion()] * 3)
        self.assertTrue(any(m.key == 'deepseek_v3_1' for m in await self.eligible()))
        await self.seed_usage(higher=14)
        await service.ask_text(**self.request(model='mistral_large_3'))
        for model in HIGHER:
            with self.assertRaises(AccessDeniedError) as caught:
                await service.ask_text(**self.request(model=model))
            self.assertEqual(caught.exception.decision.code, 'higher_cost_limit_exceeded')
        create.assert_awaited_once()
        status = await self.accounts.status(2)
        self.assertEqual((status['requests_today'], status['higher_cost_requests_today']), (15, 15))
        self.assertFalse(any(m.key in HIGHER for m in await self.eligible()))

    async def test_shared_sub_limit_inside_50_no_double_charge(self):
        await self.seed_usage(standard=30, higher=15)
        service, create = self.service([usage_fixtures.completion()] * 6)
        for _ in range(5):
            await service.ask_text(**self.request())
        with self.assertRaises(AccessDeniedError) as caught:
            await service.ask_text(**self.request())
        self.assertEqual(caught.exception.decision.code, 'limit_exceeded')
        self.assertEqual(create.await_count, 5)
        status = await self.accounts.status(2)
        self.assertEqual((status['requests_today'], status['higher_cost_requests_today']), (50, 15))
        self.assertEqual(await self.eligible(), ())

    async def test_actual_higher_call_charges_both_once_and_pre_call_failure_refunds(self):
        service, _ = self.service([usage_fixtures.UpstreamError(429)])
        with self.assertRaises(usage_fixtures.AISAPIError):
            await service.ask_text(**self.request(model='deepseek_v3_1'))
        status = await self.accounts.status(2)
        self.assertEqual((status['requests_today'], status['higher_cost_requests_today']), (0, 0))
        service, _ = self.service([usage_fixtures.completion()])
        await service.ask_text(**self.request(model='deepseek_v3_2'))
        status = await self.accounts.status(2)
        self.assertEqual((status['requests_today'], status['higher_cost_requests_today']), (1, 1))

    async def test_higher_vision_helper_and_moderation_do_not_charge_extra(self):
        service, create = self.service([usage_fixtures.completion()] * 2)
        await service.ask_image(**self.request(model='deepseek_v3_1'), image_bytes=b'image', mime_type='image/jpeg')
        self.assertEqual(create.await_count, 1)
        rows = await self.rows('SELECT model_cost_tier, is_main_inference, counts_toward_limit FROM usage_attempts')
        self.assertEqual([(r['is_main_inference'], r['counts_toward_limit']) for r in rows], [(1, 1)])
        stamp = utc_timestamp()
        await self.storage.usage.start_request(request_id='technical', user_id=2, timestamp=stamp,
            model='deepseek_v3_1', provider='amazon_bedrock')
        self.assertTrue((await self.storage.usage.start_attempt(attempt_id='guard', request_id='technical', user_id=2,
            timestamp=stamp, model='gpt_oss_safeguard_20b', provider='amazon_bedrock', purpose='moderation')).allowed)
        status = await self.accounts.status(2)
        self.assertEqual((status['requests_today'], status['higher_cost_requests_today']), (1, 1))

    async def test_parallel_chats_cannot_race_higher_pool(self):
        await self.seed_usage(higher=14)
        service, create = self.service([usage_fixtures.completion()] * 6)
        requests = []
        for n in range(6):
            args = self.request(model='deepseek_v3_1' if n % 2 else 'mistral_large_3')
            args['conversation_key'] = (100 + n, 2)
            args['conversation_id'] = self.state.current_conversation_id(args['conversation_key'])
            requests.append(service.ask_text(**args))
        results = await asyncio.gather(*requests, return_exceptions=True)
        self.assertEqual(sum(isinstance(r, AccessDeniedError) for r in results), 5)
        create.assert_awaited_once()
        self.assertEqual((await self.accounts.status(2))['higher_cost_requests_today'], 15)

    async def test_admitted_request_revalidated_after_limit_changes(self):
        stamp = utc_timestamp()
        admitted = await self.storage.usage.start_request(request_id='queued', user_id=2, timestamp=stamp,
            model='deepseek_v3_1', provider='amazon_bedrock')
        self.assertTrue(admitted.allowed)
        await self.seed_usage(higher=15)
        denied = await self.storage.usage.start_attempt(attempt_id='queued', request_id='queued', user_id=2,
            timestamp=stamp, model='deepseek_v3_1', provider='amazon_bedrock')
        self.assertEqual(denied.code, 'higher_cost_limit_exceeded')
        self.assertEqual(await self.rows("SELECT * FROM usage_attempts WHERE attempt_id='queued'"), [])

    async def test_admitted_premium_revalidated_after_revoke_and_ban_priority(self):
        await self.admin.act(999, 'grant_premium', user_id=2, reason='Grant')
        stamp = utc_timestamp()
        await self.storage.usage.start_request(request_id='queued', user_id=2, timestamp=stamp,
            model='glm_5', provider='amazon_bedrock')
        await self.admin.act(999, 'revoke_premium', user_id=2, reason='Revoke')
        decision = await self.storage.usage.start_attempt(attempt_id='queued', request_id='queued', user_id=2,
            timestamp=stamp, model='glm_5', provider='amazon_bedrock')
        self.assertEqual(decision.code, 'premium_locked')
        await self.admin.act(999, 'block', user_id=2, reason='Ban')
        decision = await self.storage.usage.start_request(request_id='banned', user_id=2, timestamp=stamp,
            model='glm_5', provider='amazon_bedrock')
        self.assertEqual(decision.code, 'blocked')

    async def test_higher_pool_utc_reset_user_isolation_and_trusted_semantics(self):
        yesterday = utc_timestamp(datetime.now(timezone.utc) - timedelta(days=1))
        await self.seed_usage(higher=15, timestamp=yesterday)
        self.assertEqual((await self.accounts.status(2))['higher_cost_requests_today'], 0)
        await self.add_user(3)
        await self.admin.act(999, 'grant_trusted', user_id=3, reason='Trusted')
        await self.seed_usage(higher=15, user_id=3)
        self.assertFalse(any(m.key in HIGHER for m in await self.eligible(3)))
        self.assertTrue(any(m.key in HIGHER for m in await self.eligible(2)))
        self.assertEqual((await self.accounts.status(3))['effective_daily_limit'], 500)

    async def test_random_eligibility_internal_premium_higher_and_total(self):
        base = {m.key for m in await self.eligible()}
        self.assertEqual(len(base), 32)
        self.assertFalse(base & PREMIUM)
        self.assertFalse(any('safeguard' in key for key in base))
        await self.admin.act(999, 'grant_premium', user_id=2, reason='Grant')
        self.assertEqual(len(await self.eligible()), 40)
        await self.seed_usage(higher=15)
        self.assertEqual(len(await self.eligible()), 35)
        self.assertFalse({m.key for m in await self.eligible()} & HIGHER)

    async def test_random_persists_rerolls_only_on_action_and_no_quota(self):
        with patch('Services.users.random.choice', side_effect=[MODEL_REGISTRY['deepseek_v3_1'], MODEL_REGISTRY['gpt_oss_120b']]) as roll:
            first = await self.users.random_model((10, 2))
            self.assertEqual(first.key, 'deepseek_v3_1')
            await self.reopen()
            selection = await self.users.selection((10, 2))
            self.assertEqual(selection.model_key, first.key)
            service, create = self.service([usage_fixtures.completion()])
            await service.ask_text(**self.request(model=selection.model_key))
            self.assertEqual(roll.call_count, 1)
            self.assertEqual(create.call_args.kwargs['model'], MODEL_REGISTRY[first.key].model_id)
            second = await self.users.random_model((10, 2))
            self.assertEqual(second.key, 'gpt_oss_120b')
            self.assertEqual(roll.call_count, 2)
        self.assertEqual((await self.users.selection((10, 2))).model_key, second.key)
        self.assertEqual((await self.accounts.status(2))['requests_today'], 1)

    async def test_random_handler_persists_selection_no_charge(self):
        callback = SimpleNamespace(from_user=SimpleNamespace(id=2),
            message=SimpleNamespace(chat=SimpleNamespace(id=10), edit_text=AsyncMock()), answer=AsyncMock())
        with patch('Services.users.random.choice', return_value=MODEL_REGISTRY['gpt_oss_120b']):
            await random_model_handler(callback, SimpleNamespace(users=self.users))
        self.assertEqual((await self.users.selection((10, 2))).model_key, 'gpt_oss_120b')
        self.assertIn('GPT OSS 120B', callback.message.edit_text.call_args.args[0])
        self.assertEqual((await self.accounts.status(2))['requests_today'], 0)

    async def test_random_revalidates_at_commit_and_before_inference(self):
        await self.admin.act(999, 'grant_premium', user_id=2, reason='Grant')
        with patch('Services.users.random.choice', return_value=MODEL_REGISTRY['glm_5']):
            await self.users.random_model((10, 2))
        await self.admin.act(999, 'revoke_premium', user_id=2, reason='Revoke')
        service, create = self.service([usage_fixtures.completion()])
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request(model='glm_5'))
        create.assert_not_awaited()
        with patch('Services.users.random.choice', return_value=MODEL_REGISTRY['deepseek_v3_1']):
            await self.users.random_model((10, 2))
        await self.seed_usage(higher=15)
        with self.assertRaises(AccessDeniedError):
            await service.ask_text(**self.request(model='deepseek_v3_1'))
        create.assert_not_awaited()
        # A stale eligible snapshot cannot bypass the normal commit validation.
        with patch.object(self.storage.users, 'available_models', AsyncMock(return_value=(MODEL_REGISTRY['glm_5'],))):
            with self.assertRaises(AccessDeniedError):
                await self.users.random_model((10, 2))

    async def test_banned_user_cannot_random_and_total_limit_blocks_roll(self):
        await self.seed_usage(standard=50)
        with patch('Services.users.random.choice') as roll:
            with self.assertRaises(ValueError):
                await self.users.random_model((10, 2))
            roll.assert_not_called()
        await self.admin.act(999, 'block', user_id=2, reason='Ban')
        with self.assertRaises(AccessDeniedError) as caught:
            await self.users.random_model((10, 2))
        self.assertEqual(caught.exception.decision.code, 'blocked')

    async def test_configured_higher_limit_applies_to_all_runtime_policies(self):
        await self.storage.close()
        self.storage = SQLiteStorage(self.path, higher_cost_limit=1)
        await self.storage.open()
        from Services.users import UserService
        self.users = UserService(self.storage.users, self.state)
        await self.seed_usage(higher=1)
        self.assertEqual((await self.storage.accounts.snapshot(2, utc_timestamp()))['higher_cost_daily_limit'], 1)
        self.assertFalse(any(m.key in HIGHER for m in await self.eligible()))
        decision = await self.storage.usage.start_request(request_id='custom', user_id=2, timestamp=utc_timestamp(),
            model='mistral_large_3', provider='amazon_bedrock')
        self.assertEqual(decision.code, 'higher_cost_limit_exceeded')


class PremiumTierMigrationTests(StorageTestCase):
    legacy_database = migration_fixtures.ModerationMigrationTests.legacy_database

    async def test_v6_preserves_every_old_column_and_safety_v5_policy(self):
        path = self.legacy_database()
        stamp = utc_timestamp()
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.execute('PRAGMA foreign_keys = ON')
            migrate(c, MIGRATIONS[:5])
            c.execute("UPDATE safety_policy_state SET policy_version='ais_safety_v2', policy_fingerprint=?, revision=1", ('a' * 64,))
            c.execute("INSERT INTO safety_evaluations (telegram_user_id, created_at, mode, origin, policy_version, policy_fingerprint, classifier_model, classifier_provider, status, confidence_source, context_kind) VALUES (2, ?, 'production', 'live', 'ais_safety_v2', ?, 'gpt_oss_safeguard_20b', 'amazon_bedrock', 'error', 'unavailable', 'text')", (stamp, 'a' * 64))
            c.execute("INSERT INTO moderation_sanctions (telegram_user_id, moderation_event_id, warning_id, source, action, reason_code, created_at) VALUES (2, 7, 42, 'automatic_moderation', 'block', 'active_moderation_warnings_3', ?)", (stamp,))
            sanction = c.execute('SELECT id FROM moderation_sanctions').fetchone()[0]
            c.execute("INSERT INTO moderation_appeals (telegram_user_id, moderation_event_id, warning_id, sanction_id, explanation, created_at) VALUES (2, 7, 42, ?, 'Retain appeal evidence', ?)", (sanction, stamp))
            before = {}
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                cols = [r[1] for r in c.execute(f'PRAGMA table_info({table})')]
                before[table] = (cols, c.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY 1").fetchall())
        upgraded = SQLiteStorage(path)
        try:
            await upgraded.open()
            for table, (cols, expected) in before.items():
                condition = ' WHERE version <= 5' if table == 'schema_migrations' else ''
                actual = await upgraded.database.run(lambda c: [tuple(r) for r in c.execute(f"SELECT {','.join(cols)} FROM {table}{condition} ORDER BY 1")])
                self.assertEqual(expected, actual, table)
            self.assertEqual((await upgraded.accounts.snapshot(2, stamp))['premium_models_allowed'], 1)
            self.assertEqual((await upgraded.accounts.snapshot(999, stamp))['premium_models_allowed'], 0)
            self.assertEqual((await upgraded.moderation.policy_state())['policy_version'], 'ais_safety_v2')
        finally:
            await upgraded.close()
        self.assertEqual(verify_database(path)['integrity'], 'ok')
        self.assertEqual(verify_database(path)['schema_version'], 6)
        backup = next((path.parent / 'backups').glob('*.before-v6-*.sqlite3'))
        self.assertEqual(verify_database(backup)['schema_version'], 5)

    def test_failed_v6_rolls_back_without_touching_v5(self):
        path = self.legacy_database()
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            migrate(c, MIGRATIONS[:5])
            broken = replace(MIGRATIONS[5], statements=(*MIGRATIONS[5].statements, 'INVALID SQL'))
            with self.assertRaises(sqlite3.Error):
                migrate(c, (*MIGRATIONS[:5], broken))
            self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 5)
            self.assertNotIn('premium_models_allowed', [r[1] for r in c.execute('PRAGMA table_info(user_access)')])
            self.assertEqual(c.execute('SELECT grok_allowed FROM user_access WHERE telegram_user_id=2').fetchone()[0], 1)
        self.assertEqual(verify_database(path)['integrity'], 'ok')

    async def test_legacy_sql_writer_syncs_canonical_permission(self):
        await self.add_user()
        stamp = utc_timestamp()
        await self.storage.database.run(lambda c: c.execute("UPDATE user_access SET grok_allowed=1, grok_source='access_code', grok_granted_at=? WHERE telegram_user_id=2", (stamp,)), write=True)
        self.assertEqual((await self.storage.accounts.snapshot(2, stamp))['premium_models_allowed'], 1)
        await self.storage.database.run(lambda c: c.execute("UPDATE user_access SET grok_allowed=0, grok_source=NULL, grok_granted_at=NULL WHERE telegram_user_id=2"), write=True)
        self.assertEqual((await self.storage.accounts.snapshot(2, stamp))['premium_models_allowed'], 0)


class PremiumAdminFlowTests(StorageTestCase):
    async def asyncSetUp(self):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage
        await super().asyncSetUp()
        await self.add_user(2)
        await self.add_user(999, username='owner')
        self.fsm_storage = MemoryStorage()
        self.fsm = FSMContext(self.fsm_storage, StorageKey(bot_id=1, chat_id=999, user_id=999))
        self.app = SimpleNamespace(admin=self.admin, users=self.users, state=self.state, accounts=self.accounts,
                                   settings=self.settings, notifications=SimpleNamespace(wake=lambda: None))

    async def asyncTearDown(self):
        await self.fsm_storage.close()
        await super().asyncTearDown()

    async def test_grant_revoke_through_real_admin_confirmation_flow(self):
        from Handlers.admin_panel import admin_callback, admin_reason
        from Handlers.flows import AdminFlow
        for action, allowed in (('grant_premium', 1), ('revoke_premium', 0)):
            await admin_callback(panel_fixtures.fake_callback(f'adm:act:{action}:2'), self.app, self.fsm)
            self.assertEqual(await self.fsm.get_state(), AdminFlow.reason.state)
            await admin_reason(panel_fixtures.fake_message(text='Reviewed Premium permission'), self.app, self.fsm)
            data = await self.fsm.get_data()
            await admin_callback(panel_fixtures.fake_callback(f"adm:confirm:{data['nonce']}"), self.app, self.fsm)
            self.assertEqual((await self.accounts.status(2))['premium_models_allowed'], allowed)
        self.assertEqual([r['action'] for r in await self.admin.audit(999, 2)], ['revoke_premium', 'grant_premium'])

    async def test_non_admin_cannot_grant_premium_with_crafted_callback(self):
        from Handlers.admin_panel import admin_callback
        callback = panel_fixtures.fake_callback('adm:act:grant_premium:2', user_id=2)
        await admin_callback(callback, self.app, self.fsm)
        self.assertEqual((await self.accounts.status(2))['premium_models_allowed'], 0)
        self.assertEqual(await self.admin.audit(999, 2), [])

    async def test_config_env_higher_limit_and_defaults(self):
        import os
        from config import Settings
        values = {'BOT_TOKEN': '123456:fake', 'OPENAI_API_KEY': 'fake',
                  'AIS_API_KEY': 'fake', 'AIS_API_BASE_URL': 'https://ais.example/v1',
                  'OPENAI_BASE_URL': 'https://example.invalid/v1', 'TAVILY_API_KEY': 'fake'}
        with patch.dict(os.environ, values, clear=True), patch('config.load_dotenv'):
            defaults = Settings.load()
            self.assertEqual((defaults.standard_daily_limit, defaults.trusted_daily_limit, defaults.higher_cost_daily_limit), (50, 500, 15))
            os.environ['HIGHER_COST_DAILY_LIMIT'] = '3'
            self.assertEqual(Settings.load().higher_cost_daily_limit, 3)
            os.environ['HIGHER_COST_DAILY_LIMIT'] = '0'
            with self.assertRaises(RuntimeError):
                Settings.load()

    async def test_activation_after_locked_selection_keeps_permission_when_quota_exhausted(self):
        from Handlers.accounts import activation_message
        await ModelAccessTierTests.seed_usage(self, standard=50)
        callback = panel_fixtures.fake_callback('model:amazon_bedrock:Z.ai:glm_5', user_id=2)
        await select_model(callback, self.app, self.fsm)
        self.assertEqual((await self.fsm.get_data())['purpose'], 'premium_models')
        message = panel_fixtures.fake_message(user_id=2, text=self.settings.grok_access_code)
        await activation_message(message, self.app, self.fsm)
        status = await self.accounts.status(2)
        self.assertEqual((status['premium_models_allowed'], status['requests_today']), (1, 50))
        self.assertIsNone((await self.users.selection((2, 2))).model_key)
        self.assertIn('лимит', message.bot.edit_message_text.call_args.kwargs['text'])
        self.assertNotIn(self.settings.grok_access_code, message.bot.edit_message_text.call_args.kwargs['text'])

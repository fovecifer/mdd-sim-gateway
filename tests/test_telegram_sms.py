"""Only synthetic identities and mocked network/paid SMS operations."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from control.app import config as cfg, store, telegram_sms as tg

LINE = {"id": "1", "iccid": "8900000000000000001", "enabled": True}
OWNER = "123456789"
TOKEN = "100000000:synthetic_test_token_not_a_real_secret"


class SettingsTests(unittest.TestCase):
    def config(self):
        return tg.validate_settings({"enabled": True, "bot_token": TOKEN, "chat_id": OWNER,
            "sms_control": {"enabled": True, "owner_id": OWNER, "instance_id": "1"}}, {}, [LINE])

    def test_disabled_by_default_and_legacy_commands_never_reenabled(self):
        value = tg.validate_settings({"commands": {"enabled": True}}, {}, [])
        self.assertFalse(tg.enabled(value))
        self.assertNotIn("commands", value)

    def test_scope_preserved_on_resave_rotated_on_reauthorization(self):
        first = self.config()
        same = tg.validate_settings(first, first, [LINE])
        self.assertEqual(tg.scope(first), tg.scope(same))
        disabled = tg.validate_settings({"sms_control": {"enabled": False}}, same, [LINE])
        again = tg.validate_settings({"sms_control": {"enabled": True}}, disabled, [LINE])
        self.assertNotEqual(tg.scope(first), tg.scope(again))

    def test_client_cannot_forge_identity_or_grant(self):
        first = self.config()
        same = tg.validate_settings({"sms_control": {"identity": "forged", "grant": "forged", "since": 0}}, first, [LINE])
        self.assertEqual(first, same)

    def test_replacing_card_needs_explicit_rebinding(self):
        first = self.config()
        new_line = {**LINE, "iccid": "8900000000000000002"}
        with self.assertRaisesRegex(ValueError, "SIM changed"):
            tg.validate_settings(first, first, [new_line])
        bound = tg.validate_settings({"sms_control": {"bind_current_sim": True}}, first, [new_line])
        self.assertNotEqual(tg.scope(first), tg.scope(bound))

    def test_explicit_rebinding_always_revokes_old_drafts(self):
        first = self.config()
        bound = tg.validate_settings({"sms_control": {"bind_current_sim": True}}, first, [LINE])
        self.assertNotEqual(tg.scope(first), tg.scope(bound))

    def test_emergency_channel_off_does_not_need_sim_or_token(self):
        first = self.config()
        disabled = tg.validate_settings({"enabled": False, "bot_token": ""}, first, [])
        self.assertFalse(tg.enabled(disabled))

    def test_invalid_owner_group_limit_and_unknown_line_rejected(self):
        first = self.config()
        for patch_value in [{"chat_id": "-1001234"}, {"sms_control": {"owner_id": "@name"}},
                            {"sms_control": {"daily_limit": True}}, {"sms_control": {"daily_limit": 101}},
                            {"sms_control": {"instance_id": "99"}}, {"sms_control": {"enabled": "true"}}]:
            with self.subTest(value=patch_value), self.assertRaises(ValueError):
                tg.validate_settings(patch_value, first, [LINE])


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.patch = patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(root / "test.sqlite"),
                                    PREVIOUS_DB_PATH=str(root / "absent.sqlite"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.config = SettingsTests().config()
        self.line = copy.deepcopy(LINE)
        self.sender = AsyncMock(return_value={"ok": True, "message": {"id": 999}})
        self.controller = tg.Controller(self.sender, lambda: self.config, lambda iid: self.line)
        store.init()
        self.network = patch.object(tg, "api_call", return_value={"message_id": 501})
        self.api = self.network.start()
        self.addCleanup(self.network.stop)
        self.seq = 0

    def message(self, text, actor=OWNER, chat=OWNER, kind="private", date=None, reply=None):
        self.seq += 1
        msg = {"from": {"id": int(actor)}, "chat": {"id": int(chat), "type": kind},
               "message_id": self.seq, "date": int(time.time()) if date is None else date, "text": text}
        if reply is not None:
            msg["reply_to_message"] = {"message_id": reply, "text": "ignored, not trusted for routing"}
        return {"update_id": self.seq, "message": msg}

    def callback(self, token, action="send", actor=OWNER, chat=OWNER):
        self.seq += 1
        return {"update_id": self.seq, "callback_query": {"id": str(self.seq),
            "from": {"id": int(actor)}, "data": f"{action}:{token}",
            "message": {"chat": {"id": int(chat), "type": "private"}}}}

    async def process(self, update):
        await self.controller.process(copy.deepcopy(self.config), update)

    def rows(self, table):
        with tg._db() as db:
            return [dict(x) for x in db.execute(f"SELECT * FROM {table}")]

    async def draft(self, text="hello"):
        await self.process(self.message(f"/sms +15550000001 {text}"))
        return self.rows("tg_drafts")[-1]["token"]

    async def test_send_requires_confirmation_then_duplicate_click_is_safe(self):
        token = await self.draft()
        self.sender.assert_not_awaited()
        update = self.callback(token)
        await self.process(update)
        await self.process(update)
        await self.process(self.callback(token))
        self.sender.assert_awaited_once()
        self.assertEqual(self.rows("tg_drafts")[0]["state"], "submitted")

    async def test_untrusted_actor_group_and_channel_updates_are_silent(self):
        for update in [self.message("/sms +15550000001 hi", actor="9"),
                       self.message("/sms +15550000001 hi", chat="99", kind="group"),
                       {"update_id": 100, "channel_post": {"text": "/sms +15550000001 hi"}},
                       {"update_id": 101, "edited_message": self.message("/sms +15550000001 hi")["message"]}]:
            await self.process(update)
        self.assertEqual(self.rows("tg_drafts"), [])
        self.assertEqual(self.rows("tg_outbox"), [])

    async def test_someone_else_cannot_confirm_owners_draft(self):
        token = await self.draft()
        await self.process(self.callback(token, actor="9"))
        self.sender.assert_not_awaited()

    async def test_cancel_expiry_and_old_input(self):
        token = await self.draft()
        await self.process(self.callback(token, action="cancel"))
        await self.process(self.callback(token))
        token2 = await self.draft()
        with tg._db() as db:
            db.execute("UPDATE tg_drafts SET expires=0 WHERE token=?", (token2,))
        await self.process(self.callback(token2))
        await self.process(self.message("/sms +15550000001 stale", date=int(time.time()) - 121))
        self.sender.assert_not_awaited()
        self.assertEqual(len(self.rows("tg_drafts")), 2)

    async def test_duplicate_command_does_not_create_two_drafts(self):
        update = self.message("/sms +15550000001 hello")
        await self.process(update)
        await self.process(update)
        self.assertEqual(len(self.rows("tg_drafts")), 1)

    async def test_calls_shell_shortcodes_and_long_text_refused(self):
        for text in ("/call +15550000001", "/exec whoami", "/sms 911 hello",
                     "/sms +15550000001 " + "😀" * 81,
                     "/sms +15550000001 hidden\x00text"):
            await self.process(self.message(text))
        self.assertEqual(self.rows("tg_drafts"), [])
        self.sender.assert_not_awaited()

    async def test_reply_uses_database_not_user_supplied_quoted_text(self):
        with tg._db() as db:
            db.execute("INSERT INTO tg_replies VALUES(?,?,?,?,?,?,?)", (tg.scope(self.config), OWNER,
                       200, "+15550000002", "1", tg.identity(LINE), int(time.time())))
        await self.process(self.message("reply body", reply=200))
        self.assertEqual(self.rows("tg_drafts")[0]["peer"], "+15550000002")
        await self.process(self.message("no target", reply=201))
        self.assertEqual(len(self.rows("tg_drafts")), 1)

    async def test_reply_and_draft_survive_restart_without_auto_send(self):
        token = await self.draft()
        self.controller = tg.Controller(self.sender, lambda: self.config, lambda iid: self.line)
        self.controller.recover()
        self.sender.assert_not_awaited()
        await self.process(self.callback(token))
        self.sender.assert_awaited_once()

    async def test_sim_change_and_revocation_block_existing_confirmation(self):
        token = await self.draft()
        self.line["iccid"] = "different"
        await self.process(self.callback(token))
        self.line = copy.deepcopy(LINE)
        old = copy.deepcopy(self.config)
        self.config["sms_control"]["enabled"] = False
        await self.controller.process(old, self.callback(token))
        self.sender.assert_not_awaited()

    async def test_new_grant_cannot_use_old_confirmation(self):
        token = await self.draft()
        self.config["sms_control"]["grant"] = "new grant"
        await self.process(self.callback(token))
        self.sender.assert_not_awaited()

    async def test_timeout_or_crash_never_resubmits(self):
        self.sender.side_effect = TimeoutError("private details must not be echoed")
        token = await self.draft()
        await self.process(self.callback(token))
        self.assertEqual(self.rows("tg_drafts")[0]["state"], "unknown")
        self.controller.recover()
        await self.process(self.callback(token))
        self.sender.assert_awaited_once()
        self.assertNotIn("private details", str(self.rows("tg_outbox")))

    async def test_killed_sending_claim_becomes_unknown(self):
        token = await self.draft()
        with tg._db() as db:
            db.execute("UPDATE tg_drafts SET state='sending',attempted=? WHERE token=?", (int(time.time()), token))
        self.controller.recover()
        await self.process(self.callback(token))
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("tg_drafts")[0]["state"], "unknown")

    async def test_daily_limit_cannot_be_reset_by_permission_rotation(self):
        self.config["sms_control"]["daily_limit"] = 1
        token = await self.draft()
        await self.process(self.callback(token))
        self.config["sms_control"]["grant"] = "changed"
        token2 = await self.draft()
        await self.process(self.callback(token2))
        self.sender.assert_awaited_once()

    async def test_rapid_submissions_are_limited(self):
        token = await self.draft()
        await self.process(self.callback(token))
        token2 = await self.draft()
        await self.process(self.callback(token2))
        self.sender.assert_awaited_once()

    async def test_atomic_incoming_capture_and_no_duplicate_import(self):
        with patch.object(cfg, "get_settings", return_value={"telegram": self.config}), \
                patch.object(cfg, "get_instance", return_value=self.line):
            rec = store.add_message("1", "in", "+15550000003", "hello")
            store.add_imported_message("synthetic", "1", "in", "+15550000003", "other", int(time.time()))
            self.assertIsNone(store.add_imported_message("synthetic", "1", "in", "+15550000003", "other", int(time.time())))
            store.add_message("1", "out", "+15550000003", "not an incoming SMS")
        self.assertEqual(len(self.rows("tg_outbox")), 2)
        self.assertIn(f"S{rec['id']}", self.rows("tg_outbox")[0]["text"])
        await self.controller.deliver_one(self.config)
        self.assertEqual(self.rows("tg_replies")[0]["peer"], "+15550000003")
        await self.process(self.message("reply after restart", reply=501))
        self.assertEqual(self.rows("tg_drafts")[0]["peer"], "+15550000003")

    async def test_database_rollback_keeps_sms_and_outbox_atomic(self):
        with patch.object(cfg, "get_settings", return_value={"telegram": self.config}), \
                patch.object(cfg, "get_instance", return_value=self.line):
            with self.assertRaises(RuntimeError), tg._db() as db:
                db.execute("INSERT INTO messages(instance,direction,peer,body,ts) VALUES('1','in','test','hi',1)")
                tg.capture_incoming(db, {"id": 999, "instance": "1", "direction": "in", "peer": "test", "body": "hi"})
                raise RuntimeError("simulate transaction failure")
        self.assertEqual(self.rows("messages"), [])
        self.assertEqual(self.rows("tg_outbox"), [])

    async def test_native_notification_is_suppressed_only_for_bound_sms(self):
        from control.app import notify_push
        with patch.object(notify_push, "_deliver_with_retry") as deliver:
            notify_push.dispatch({"telegram": self.config}, "incoming_sms", LINE, "+15550000001", "hello")
            deliver.assert_not_called()
            notify_push.dispatch({"telegram": self.config}, "incoming_call", LINE, "+15550000001")
            deliver.assert_called_once()

    async def test_network_failure_requeues_notification_not_sms(self):
        self.controller.queue(self.config, "notice", "hello")
        self.api.side_effect = tg.TelegramError("connection_error")
        await self.controller.deliver_one(self.config)
        self.assertEqual(self.rows("tg_outbox")[0]["state"], "pending")
        self.sender.assert_not_awaited()

    async def test_revocation_cancels_old_outbox_and_drafts(self):
        await self.draft()
        self.config["sms_control"]["enabled"] = False
        self.controller.maintain(self.config)
        self.assertEqual(self.rows("tg_outbox")[0]["state"], "cancelled")
        self.assertEqual(self.rows("tg_drafts")[0]["state"], "cancelled")

    async def test_recent_status_and_help_do_not_send_sms(self):
        await self.draft()
        for cmd in ("/recent", "/status", "/start", "/help"):
            await self.process(self.message(cmd))
        self.sender.assert_not_awaited()

    async def test_poll_cursor_is_persisted_after_processing(self):
        message = self.message("/sms +15550000001 hello")
        self.api.side_effect = [[message], asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await self.controller.poll()
        self.assertEqual(len(self.rows("tg_drafts")), 1)
        self.assertEqual(self.rows("tg_offsets")[0]["next_id"], message["update_id"] + 1)
        self.assertEqual(self.api.call_args_list[1].args[2]["offset"], message["update_id"] + 1)
        self.api.reset_mock(side_effect=True)
        self.api.side_effect = [asyncio.CancelledError()]
        restarted = tg.Controller(self.sender, lambda: self.config, lambda iid: self.line)
        with self.assertRaises(asyncio.CancelledError):
            await restarted.poll()
        self.assertEqual(self.api.call_args.args[2]["offset"], message["update_id"] + 1)

    async def test_cancelled_send_becomes_unknown_and_cannot_repeat(self):
        token = await self.draft()
        self.sender.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.process(self.callback(token))
        self.assertEqual(self.rows("tg_drafts")[0]["state"], "unknown")
        self.controller.recover()
        await self.process(self.callback(token))
        self.sender.assert_awaited_once()

    async def test_shortcode_notification_can_be_read_but_not_replied_to(self):
        with patch.object(cfg, "get_settings", return_value={"telegram": self.config}), \
                patch.object(cfg, "get_instance", return_value=self.line):
            store.add_message("1", "in", "12345", "service notice")
        await self.controller.deliver_one(self.config)
        await self.process(self.message("reply", reply=501))
        self.assertEqual(self.rows("tg_drafts"), [])
        self.sender.assert_not_awaited()


class TransportTests(unittest.TestCase):
    def test_token_never_appears_in_transport_error(self):
        with patch.object(tg.notify_push, "telegram_session", side_effect=ValueError(TOKEN)):
            with self.assertRaises(tg.TelegramError) as caught:
                tg.api_call({"bot_token": TOKEN}, "getUpdates", {})
        self.assertEqual(str(caught.exception), "connection_error")

    def test_api_ok_false_is_not_success(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"ok": False, "error_code": 409}
        with patch.object(tg.notify_push, "telegram_session", return_value=session):
            with self.assertRaisesRegex(tg.TelegramError, "poll_conflict"):
                tg.api_call({"bot_token": TOKEN}, "getUpdates", {})
        session.close.assert_called_once()

    def test_rate_limit_retry_after_is_preserved(self):
        session = Mock()
        session.post.return_value.status_code = 429
        session.post.return_value.json.return_value = {
            "ok": False, "error_code": 429, "parameters": {"retry_after": 90}}
        with patch.object(tg.notify_push, "telegram_session", return_value=session):
            with self.assertRaises(tg.TelegramError) as caught:
                tg.api_call({"bot_token": TOKEN}, "getUpdates", {})
        self.assertEqual(caught.exception.retry_after, 90)


class GatewayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_revocation_after_ami_wait_prevents_actual_submission(self):
        from control.app import main
        ami = Mock()
        ami.send_sms = AsyncMock()
        async def connect(iid):
            return ami
        with patch.object(main.hub, "ami_for", side_effect=connect), \
                patch.object(main.store, "add_message") as add:
            result = await main._send_sms_vowifi("1", "+15550000001", "test", authorize=lambda: False)
        self.assertTrue(result["unavailable"])
        add.assert_not_called()
        ami.send_sms.assert_not_awaited()

    async def test_revocation_during_line_lock_wait_prevents_send(self):
        from control.app import main
        lock = asyncio.Lock()
        await lock.acquire()
        granted = True
        with patch.dict(main.hub.sms_send_locks, {"test": lock}), \
                patch.object(main, "_send_sms_vowifi", new_callable=AsyncMock) as send:
            task = asyncio.create_task(main.send_sms_on_line("test", "+15550000001", "hello",
                                      "vowifi", authorize=lambda: granted))
            await asyncio.sleep(0)
            granted = False
            lock.release()
            result = await task
        self.assertTrue(result["unavailable"])
        send.assert_not_awaited()

    async def test_telegram_adapter_cannot_fall_back_to_cellular(self):
        from control.app import main
        config = SettingsTests().config()
        draft = {"instance": "1", "identity": tg.identity(LINE), "peer": "+15550000001",
                 "body": "hi", "expires": int(time.time()) + 120}
        with patch.object(cfg, "get_settings", return_value={"telegram": config}), \
                patch.object(cfg, "get_instance", return_value=LINE), \
                patch.object(cfg, "line_allowed", return_value=True), \
                patch.dict(main.hub.cards, {"test": {"present": True, "iccid": LINE["iccid"]}}), \
                patch.object(main, "_card_identity_mismatch", return_value=None), \
                patch.object(main, "_local_card_fault", return_value=""), \
                patch.object(main, "_registered_vowifi_ami", new=AsyncMock(return_value=None)), \
                patch.object(main, "send_sms_on_line", new_callable=AsyncMock) as send:
            result = await main._telegram_send_sms(config, draft)
        self.assertTrue(result["unavailable"])
        send.assert_not_awaited()

    async def test_new_status_endpoint_requires_admin(self):
        from control.app import main
        request = main.Request({"type": "http", "method": "GET",
                                "path": "/api/notifications/telegram/status", "headers": []})
        next_handler = AsyncMock()
        with patch.object(main.auth, "session", return_value=None):
            response = await main.require_admin_session(request, next_handler)
        self.assertEqual(response.status_code, 401)
        next_handler.assert_not_awaited()

    async def test_enabling_sms_still_requires_csrf(self):
        from control.app import main
        request = main.Request({"type": "http", "method": "PUT", "path": "/api/settings", "headers": []})
        next_handler = AsyncMock()
        with patch.object(main.auth, "session", return_value={"csrf": "synthetic"}):
            response = await main.require_admin_session(request, next_handler)
        self.assertEqual(response.status_code, 403)
        next_handler.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

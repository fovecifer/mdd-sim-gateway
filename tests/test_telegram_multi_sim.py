"""Multi-SIM routing tests: synthetic identities, no Telegram or paid SMS traffic."""
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from control.app import config as cfg, store, telegram_sms as tg
from tests import test_telegram_sms as single

LINE, OWNER = single.LINE, single.OWNER

SECOND = {"id": "3", "name": "Synthetic second SIM", "iccid": "8900000000000000003", "enabled": True}


def multi_config():
    old = single.SettingsTests().config()
    return tg.validate_settings({"sms_control": {"instance_ids": ["1", "3"]}}, old, [LINE, SECOND])


class MultiSettingsTests(unittest.TestCase):
    def test_legacy_upgrade_binds_both_cards_and_rotates_permission(self):
        old = single.SettingsTests().config()
        new = multi_config()
        self.assertEqual(tg.bindings(new), {"1": tg.identity(LINE), "3": tg.identity(SECOND)})
        self.assertNotEqual(tg.scope(old), tg.scope(new))
        self.assertTrue(tg.enabled(new))

    def test_resave_reorder_and_forged_identities_do_not_change_grant(self):
        config = multi_config()
        new = tg.validate_settings({"sms_control": {"instance_ids": ["3", "1"],
            "identities": {"1": "forged"}, "grant": "forged", "since": 0}}, config, [LINE, SECOND])
        self.assertEqual(config, new)

    def test_empty_selection_never_reactivates_legacy_fields(self):
        config = multi_config()
        config["sms_control"].update(instance_ids=[], instance_id="1", identity=tg.identity(LINE))
        self.assertFalse(tg.enabled(config))
        self.assertEqual(tg.bindings(config), {})

    def test_invalid_selections_rejected(self):
        config = multi_config()
        for ids in ([], "1,3", [1], ["1", "1"], ["99"], ["-1"], ["1 "], ["1"]*6):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                tg.validate_settings({"sms_control": {"instance_ids": ids}}, config, [LINE, SECOND])

    def test_draft_card_is_not_authorizable(self):
        with self.assertRaises(ValueError):
            tg.validate_settings({"sms_control": {"instance_ids": ["3"]}}, multi_config(),
                                 [{**SECOND, "provisioning_state": "draft"}])

    def test_each_replaced_card_requires_explicit_rebind(self):
        config = multi_config()
        changed = {**SECOND, "iccid": "8900000000000000033"}
        with self.assertRaisesRegex(ValueError, "SIM changed"):
            tg.validate_settings({"sms_control": {"instance_ids": ["1", "3"]}}, config, [LINE, changed])
        new = tg.validate_settings({"sms_control": {"bind_current_sim": True}}, config, [LINE, changed])
        self.assertEqual(tg.bindings(new)["3"], tg.identity(changed))

    def test_partial_settings_update_keeps_multi_selection(self):
        config = multi_config()
        new = tg.validate_settings({"sms_control": {"daily_limit": 7}}, config, [LINE, SECOND])
        self.assertEqual(tg.bindings(config), tg.bindings(new))
        self.assertNotEqual(tg.scope(config), tg.scope(new))

    def test_config_loader_legacy_defaults_do_not_rotate_multi_grants(self):
        config = multi_config()
        config["sms_control"].update(instance_id="", identity="")
        new = tg.validate_settings({"sms_control": {}}, config, [LINE, SECOND])
        self.assertEqual(tg.scope(config), tg.scope(new))

    def test_old_client_can_explicitly_return_to_one_line(self):
        new = tg.validate_settings({"sms_control": {"instance_id": "3"}}, multi_config(), [LINE, SECOND])
        self.assertNotIn("instance_ids", new["sms_control"])
        self.assertEqual(tg.bindings(new), {"3": tg.identity(SECOND)})

    def test_emergency_disable_works_with_no_cards(self):
        new = tg.validate_settings({"enabled": False, "bot_token": ""}, multi_config(), [])
        self.assertFalse(tg.enabled(new))


class MultiControllerTests(unittest.IsolatedAsyncioTestCase):
    # Share only setup/helpers, not the single-SIM test methods.
    asyncSetUp = single.ControllerTests.asyncSetUp
    message = single.ControllerTests.message
    callback = single.ControllerTests.callback
    process = single.ControllerTests.process
    rows = single.ControllerTests.rows

    async def configure_multi(self):
        self.config = multi_config()
        self.lines = {"1": copy.deepcopy(LINE), "3": copy.deepcopy(SECOND)}
        self.controller.get_line = self.lines.get

    async def explicit(self, iid):
        await self.process(self.message(f"/sms {iid} +15550000001 hello"))
        return self.rows("tg_drafts")[-1]["token"]

    async def choose(self, token, iid, **kwargs):
        await self.process(self.callback(f"{token}:{iid}", action="select", **kwargs))

    async def test_ambiguous_send_requires_choice_then_confirmation(self):
        await self.configure_multi()
        await self.process(self.message("/sms +15550000001 hello"))
        row = self.rows("tg_drafts")[0]
        self.assertEqual((row["state"], row["instance"]), ("choosing", ""))
        markup = json.loads(self.rows("tg_outbox")[-1]["markup"])
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64
                            for buttons in markup["inline_keyboard"] for b in buttons))
        await self.choose(row["token"], "3")
        self.sender.assert_not_awaited()
        pending = self.rows("tg_drafts")[0]
        self.assertEqual((pending["state"], pending["instance"], pending["identity"]),
                         ("pending", "3", tg.identity(SECOND)))
        await self.process(self.callback(row["token"]))
        self.sender.assert_awaited_once()
        self.assertEqual(self.sender.call_args.args[1]["instance"], "3")

    async def test_explicit_line_goes_directly_to_its_confirmation(self):
        await self.configure_multi()
        token = await self.explicit("3")
        self.assertEqual(self.rows("tg_drafts")[0]["instance"], "3")
        await self.process(self.callback(token))
        self.assertEqual(self.sender.call_args.args[1]["identity"], tg.identity(SECOND))

    async def test_reply_uses_receiving_card_not_first_authorized_card(self):
        await self.configure_multi()
        with tg._db() as db:
            db.execute("INSERT INTO tg_replies VALUES(?,?,?,?,?,?,?)", (tg.scope(self.config), OWNER,
                222, "+15550000002", "3", tg.identity(SECOND), int(time.time())))
        await self.process(self.message("reply", reply=222))
        row = self.rows("tg_drafts")[0]
        self.assertEqual((row["instance"], row["peer"]), ("3", "+15550000002"))

    async def test_shortcode_reply_uses_the_receiving_card_and_still_needs_confirmation(self):
        await self.configure_multi()
        with tg._db() as db:
            db.execute("INSERT INTO tg_replies VALUES(?,?,?,?,?,?,?)", (tg.scope(self.config), OWNER,
                       223, "6700", "3", tg.identity(SECOND), int(time.time())))
        await self.process(self.message("INFO", reply=223))
        row = self.rows("tg_drafts")[0]
        self.assertEqual((row["instance"], row["peer"], row["state"]), ("3", "6700", "pending"))
        self.sender.assert_not_awaited()
        await self.process(self.callback(row["token"]))
        self.sender.assert_awaited_once()
        self.assertEqual(self.sender.call_args.args[1]["instance"], "3")

    async def test_fake_quote_or_unauthorized_line_cannot_create_draft(self):
        await self.configure_multi()
        await self.process(self.message("/sms 2 +15550000001 bad"))
        await self.process(self.message("pretend this quoted notification is line 3", reply=777))
        self.assertEqual(self.rows("tg_drafts"), [])

    async def test_no_submission_without_line_choice(self):
        await self.configure_multi()
        await self.process(self.message("/sms +15550000001 hello"))
        token = self.rows("tg_drafts")[0]["token"]
        await self.process(self.callback(token))
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("tg_drafts")[0]["state"], "choosing")

    async def test_choice_cannot_be_retargeted_or_forged(self):
        await self.configure_multi()
        await self.process(self.message("/sms +15550000001 hello"))
        token = self.rows("tg_drafts")[0]["token"]
        await self.choose(token, "99")
        await self.choose(token, "3", actor="9")
        self.assertEqual(self.rows("tg_drafts")[0]["state"], "choosing")
        await self.choose(token, "3")
        await self.choose(token, "1")
        self.assertEqual(self.rows("tg_drafts")[0]["instance"], "3")

    async def test_other_card_change_does_not_block_valid_line(self):
        await self.configure_multi()
        self.lines["1"]["iccid"] = "changed"
        token = await self.explicit("3")
        await self.process(self.callback(token))
        self.sender.assert_awaited_once()
        self.assertEqual(self.sender.call_args.args[1]["instance"], "3")

    async def test_selected_card_change_blocks_send_without_fallback(self):
        await self.configure_multi()
        token = await self.explicit("3")
        self.lines["3"]["iccid"] = "changed"
        await self.process(self.callback(token))
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("tg_drafts")[0]["state"], "rejected")

    async def test_choice_cancel_and_expiry(self):
        await self.configure_multi()
        await self.process(self.message("/sms +15550000001 hello"))
        token = self.rows("tg_drafts")[0]["token"]
        await self.process(self.callback(token, action="cancel"))
        await self.choose(token, "3")
        self.assertEqual(self.rows("tg_drafts")[0]["state"], "cancelled")
        await self.process(self.message("/sms +15550000001 second"))
        with tg._db() as db:
            db.execute("UPDATE tg_drafts SET expires=0 WHERE state='choosing'")
        self.controller.maintain(self.config)
        self.assertEqual(self.rows("tg_drafts")[-1]["state"], "expired")
        self.sender.assert_not_awaited()

    async def test_choice_does_not_extend_deadline(self):
        await self.configure_multi()
        await self.process(self.message("/sms +15550000001 hello"))
        row = self.rows("tg_drafts")[0]
        await self.choose(row["token"], "3")
        self.assertEqual(self.rows("tg_drafts")[0]["expires"], row["expires"])

    async def test_reauthorization_revokes_old_choices_and_replies(self):
        await self.configure_multi()
        await self.process(self.message("/sms +15550000001 hello"))
        token = self.rows("tg_drafts")[0]["token"]
        self.config = tg.validate_settings({"sms_control": {"instance_ids": ["3"]}},
                                          self.config, list(self.lines.values()))
        await self.choose(token, "3")
        self.controller.maintain(self.config)
        self.assertEqual(self.rows("tg_drafts")[0]["state"], "cancelled")

    async def test_both_incoming_lines_get_independent_reply_mappings(self):
        await self.configure_multi()
        self.api.side_effect = [{"message_id": 601}, {"message_id": 602}]
        with patch.object(cfg, "get_settings", return_value={"telegram": self.config}), \
                patch.object(cfg, "get_instance", side_effect=self.lines.get):
            store.add_message("1", "in", "+15550000001", "one")
            store.add_message("3", "in", "+15550000003", "three")
        await self.controller.deliver_one(self.config)
        await self.controller.deliver_one(self.config)
        self.assertEqual([r["instance"] for r in self.rows("tg_replies")], ["1", "3"])
        await self.process(self.message("reply to third", reply=602))
        self.assertEqual(self.rows("tg_drafts")[0]["instance"], "3")

    async def test_stale_incoming_card_not_captured(self):
        await self.configure_multi()
        self.lines["1"]["iccid"] = "changed"
        with patch.object(cfg, "get_settings", return_value={"telegram": self.config}), \
                patch.object(cfg, "get_instance", side_effect=self.lines.get):
            store.add_message("1", "in", "+15550000001", "stale")
            store.add_message("3", "in", "+15550000003", "valid")
        self.assertEqual([r["instance"] for r in self.rows("tg_outbox")], ["3"])

    async def test_limit_is_shared_across_cards(self):
        await self.configure_multi()
        self.config["sms_control"]["daily_limit"] = 1
        await self.process(self.callback(await self.explicit("1")))
        await self.process(self.callback(await self.explicit("3")))
        self.sender.assert_awaited_once()

    async def test_restart_preserves_choice_without_auto_sending(self):
        await self.configure_multi()
        await self.process(self.message("/sms +15550000001 hello"))
        token = self.rows("tg_drafts")[0]["token"]
        self.controller = tg.Controller(self.sender, lambda: self.config, self.lines.get)
        self.controller.recover()
        await self.choose(token, "3")
        self.sender.assert_not_awaited()

    async def test_lines_help_recent_never_send_sms(self):
        await self.configure_multi()
        for command in ("/lines", "/status", "/help", "/recent"):
            await self.process(self.message(command))
        self.sender.assert_not_awaited()
        texts = "\n".join(row["text"] for row in self.rows("tg_outbox"))
        self.assertIn("线路 1", texts)
        self.assertIn("线路 3", texts)


class MultiGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_uses_selected_card_and_rechecks_its_authority(self):
        from control.app import main
        config = multi_config()
        draft = {"instance": "3", "identity": tg.identity(SECOND), "peer": "+15550000001",
                 "body": "hello", "expires": int(time.time()) + 120}
        with patch.object(cfg, "get_settings", side_effect=lambda: {"telegram": config}), \
                patch.object(cfg, "get_instance", return_value=SECOND), \
                patch.object(cfg, "line_allowed", return_value=True), \
                patch.dict(main.hub.cards, {"test": {"present": True, "iccid": SECOND["iccid"]}}), \
                patch.object(main, "_card_identity_mismatch", return_value=None), \
                patch.object(main, "_local_card_fault", return_value=""), \
                patch.object(main, "_registered_vowifi_ami", new=AsyncMock(return_value=object())), \
                patch.object(main, "send_sms_on_line", new=AsyncMock(return_value={"ok": True})) as send:
            result = await main._telegram_send_sms(copy.deepcopy(config), draft)
            self.assertTrue(result["ok"])
            self.assertEqual(send.call_args.args, ("3", "+15550000001", "hello", "vowifi"))
            authorize = send.call_args.kwargs["authorize"]
            self.assertTrue(authorize())
            config["sms_control"]["instance_ids"] = ["1"]
            self.assertFalse(authorize())

    async def test_adapter_rejects_line_outside_selected_grants(self):
        from control.app import main
        config = single.SettingsTests().config()
        draft = {"instance": "3", "identity": tg.identity(SECOND), "peer": "+15550000001",
                 "body": "hello", "expires": int(time.time()) + 120}
        with patch.object(cfg, "get_settings", return_value={"telegram": config}), \
                patch.object(cfg, "get_instance", return_value=SECOND), \
                patch.object(main, "_registered_vowifi_ami", new_callable=AsyncMock) as registered, \
                patch.object(main, "send_sms_on_line", new_callable=AsyncMock) as send:
            result = await main._telegram_send_sms(config, draft)
        self.assertTrue(result["unavailable"])
        registered.assert_not_awaited()
        send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()

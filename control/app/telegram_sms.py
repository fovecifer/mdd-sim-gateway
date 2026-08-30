"""Opt-in, single-owner SMS control for this self-hosted fork.

No public callback, admin cookie, shell commands, calls or automatic SMS retries. Incoming
SMS notifications are committed with the SMS itself. A durable claim precedes each paid
send; a crash after that claim is UNKNOWN, never a reason to submit the SMS again.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

from . import config as cfg, notify_push

DEFAULTS = {"enabled": False, "owner_id": "", "instance_id": "",
            "daily_limit": 20, "identity": "", "grant": "", "since": 0}
TTL = 120
RETENTION = 30 * 86400
MAX_TEXT_UNITS = 160  # Conservative cap; Unicode may still incur multiple SMS segments.
PHONE = re.compile(r"\+[1-9][0-9]{6,14}\Z")


def identity(line: dict | None) -> str:
    line = line or {}
    iccid = str(line.get("iccid") or "").strip()
    if not iccid:
        return ""
    return hashlib.sha256(f"{line.get('id')}:{iccid}".encode()).hexdigest()


def scope(config: dict) -> str:
    control = config.get("sms_control") or {}
    return hashlib.sha256(json.dumps([
        config.get("bot_token"), config.get("chat_id"), control.get("owner_id"),
        control.get("instance_id"), control.get("identity"), control.get("grant"),
    ]).encode()).hexdigest()


def enabled(config: dict) -> bool:
    c = config.get("sms_control") or {}
    return (config.get("enabled") is True and c.get("enabled") is True
            and bool(c.get("grant")) and bool(c.get("identity"))
            and str(c.get("owner_id", "")).isdigit()
            and str(c.get("owner_id")) == str(config.get("chat_id")))


def validate_settings(value: dict, previous: dict, lines: list[dict]) -> dict:
    """Only the authenticated settings API can create/rotate an SMS permission grant."""
    if not isinstance(value, dict):
        raise ValueError("Telegram settings must be an object")
    result = {**previous, **value}
    result["chat_id"] = str(result.get("chat_id") or "").strip()
    result.pop("commands", None)  # Never revive the older, unrestricted command channel.
    old = {**DEFAULTS, **(previous.get("sms_control") or {})}
    patch = value.get("sms_control", {})
    if not isinstance(patch, dict):
        raise ValueError("Telegram SMS settings must be an object")
    c = {**old, **{k: v for k, v in patch.items()
                   if k in {"enabled", "owner_id", "instance_id", "daily_limit"}}}
    if type(c["enabled"]) is not bool or type(result.get("enabled", False)) is not bool:
        raise ValueError("Telegram switches must be boolean")
    c["owner_id"], c["instance_id"] = str(c["owner_id"]).strip(), str(c["instance_id"]).strip()
    if type(c["daily_limit"]) is not int or not 1 <= c["daily_limit"] <= 100:
        raise ValueError("Telegram SMS daily limit must be between 1 and 100")
    # An emergency channel-off save must work even if the SIM/token is now invalid.
    if c["enabled"] and result.get("enabled") is True:
        if not re.fullmatch(r"[1-9][0-9]{0,15}", c["owner_id"]):
            raise ValueError("Enter your numeric Telegram user ID, not a username")
        if str(result.get("chat_id") or "").strip() != c["owner_id"]:
            raise ValueError("Two-way SMS requires your private chat ID to equal your user ID")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", str(result.get("bot_token") or "")):
            raise ValueError("A valid Telegram bot token is required")
        line = next((x for x in lines if str(x["id"]) == c["instance_id"]), None)
        current = identity(line)
        if not current:
            raise ValueError("Select a SIM line with a known ICCID")
        if (old["enabled"] and old["instance_id"] == c["instance_id"]
                and old["identity"] and old["identity"] != current
                and patch.get("bind_current_sim") is not True):
            raise ValueError("The SIM changed. Explicitly bind the current SIM again")
        c["identity"] = current
    changed = any(c.get(k) != old.get(k) for k in
                  ("enabled", "owner_id", "instance_id", "identity", "daily_limit"))
    changed |= any(result.get(k) != previous.get(k) for k in ("enabled", "bot_token", "chat_id"))
    changed |= patch.get("bind_current_sim") is True
    if changed or not old["grant"]:
        c["grant"], c["since"] = secrets.token_hex(16), int(time.time())
    result["sms_control"] = c
    return result


def init_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tg_offsets (scope TEXT PRIMARY KEY, next_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tg_updates (
            scope TEXT, update_id INTEGER, ts INTEGER, PRIMARY KEY(scope, update_id));
        CREATE TABLE IF NOT EXISTS tg_drafts (
            token TEXT PRIMARY KEY, scope TEXT NOT NULL, instance TEXT NOT NULL,
            identity TEXT NOT NULL, peer TEXT NOT NULL, body TEXT NOT NULL,
            state TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL,
            attempted INTEGER, message_id INTEGER);
        CREATE INDEX IF NOT EXISTS tg_attempts ON tg_drafts(attempted);
        CREATE TABLE IF NOT EXISTS tg_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, key TEXT NOT NULL,
            text TEXT NOT NULL, markup TEXT NOT NULL DEFAULT '{}', peer TEXT NOT NULL DEFAULT '',
            instance TEXT NOT NULL DEFAULT '', identity TEXT NOT NULL DEFAULT '',
            created INTEGER NOT NULL, next_try INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
            UNIQUE(scope, key));
        CREATE TABLE IF NOT EXISTS tg_replies (
            scope TEXT, chat TEXT, message_id INTEGER, peer TEXT NOT NULL,
            instance TEXT NOT NULL, identity TEXT NOT NULL, created INTEGER NOT NULL,
            PRIMARY KEY(scope, chat, message_id));
    """)


@contextmanager
def _db():
    # Same database as SMS history: capture_incoming participates in its transaction.
    from . import store
    db = store._conn()
    db.execute("PRAGMA busy_timeout=5000")
    try:
        with db:
            yield db
    finally:
        db.close()


def _queue(db, config, key, text, markup=None, peer="", instance="", binding=""):
    db.execute("""INSERT OR IGNORE INTO tg_outbox
        (scope,key,text,markup,peer,instance,identity,created) VALUES(?,?,?,?,?,?,?,?)""",
               (scope(config), key, text, json.dumps(markup or {}), peer, instance,
                binding, int(time.time())))


def handles_incoming(config: dict, iid: str) -> bool:
    return enabled(config) and str(config["sms_control"]["instance_id"]) == str(iid)


def capture_incoming(db, record: dict) -> None:
    """Called inside the SMS INSERT transaction. No network or side-effectful device IO."""
    if record["direction"] != "in":
        return
    config = cfg.get_settings().get("telegram") or {}
    if not handles_incoming(config, record["instance"]):
        return
    if (config.get("events") or {}).get("incoming_sms") is False:
        return
    c = config["sms_control"]
    # A stale binding must not attribute a new SIM's messages to the old permission grant.
    if identity(cfg.get_instance(c["instance_id"])) != c["identity"]:
        return
    text = (f"📩 MDD · 收到短信 S{record['id']}\n线路 {record['instance']}\n"
            f"来自 {record['peer']}\n\n{record['body']}")
    # Telegram limits text length. Each chunk carries the same durable reply target.
    # 1500 Unicode code points is <=3000 UTF-16 units even for astral characters.
    for start in range(0, len(text), 1500):
        _queue(db, config, f"sms:{record['id']}:{start}", text[start:start + 1500],
               peer=record["peer"], instance=record["instance"], binding=c["identity"])


class TelegramError(Exception):
    """Only fixed, non-secret error codes may cross the transport boundary."""

    def __init__(self, code, retry_after=5):
        super().__init__(code)
        self.retry_after = retry_after


def api_call(config: dict, method: str, payload: dict, timeout: int = 15):
    session = None
    try:
        session = notify_push.telegram_session(config)
        response = session.post(
            f"https://api.telegram.org/bot{config['bot_token']}/{method}",
            json=payload, timeout=(5, timeout))
        body = response.json()
        if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            code = body.get("error_code", response.status_code) if isinstance(body, dict) else 0
            delay = (body.get("parameters") or {}).get("retry_after", 5) if isinstance(body, dict) else 5
            delay = max(5, min(delay, 86400)) if type(delay) is int else 5
            raise TelegramError({401: "invalid_token", 403: "bot_blocked", 409: "poll_conflict",
                                 429: "rate_limited"}.get(code, "telegram_error"), delay)
        return body.get("result")
    except TelegramError:
        raise
    except Exception:
        # requests exception URLs contain the bot token; never propagate or log them.
        raise TelegramError("connection_error") from None
    finally:
        if session:
            session.close()


class Controller:
    def __init__(self, send_sms, get_config=None, get_line=None):
        self.send_sms = send_sms
        self.get_config = get_config or (lambda: cfg.get_settings().get("telegram") or {})
        self.get_line = get_line or cfg.get_instance
        self.health = {"state": "disabled", "last_poll": None, "last_push": None}

    def current(self, config):
        now = self.get_config()
        return enabled(now) and scope(now) == scope(config)

    def authorized_line(self, config):
        c = config["sms_control"]
        line = self.get_line(c["instance_id"])
        return self.current(config) and identity(line) == c["identity"]

    def status(self):
        with _db() as db:
            pending = db.execute("SELECT count(*) FROM tg_outbox WHERE state='pending'").fetchone()[0]
        return {**self.health, "pending_notifications": pending}

    def recover(self):
        with _db() as db:
            db.execute("UPDATE tg_drafts SET state='unknown' WHERE state='sending'")
            db.execute("UPDATE tg_outbox SET state='pending' WHERE state='sending'")

    def maintain(self, config):
        now = int(time.time())
        active = scope(config) if enabled(config) else "disabled"
        with _db() as db:
            db.execute("UPDATE tg_drafts SET state='expired' WHERE state='pending' AND expires<?", (now,))
            db.execute("UPDATE tg_drafts SET state='cancelled' WHERE state='pending' AND scope!=?", (active,))
            db.execute("UPDATE tg_outbox SET state='cancelled' WHERE state='pending' AND scope!=?", (active,))
            for table in ("tg_drafts", "tg_outbox", "tg_replies"):
                db.execute(f"DELETE FROM {table} WHERE created<?", (now - RETENTION,))
            # Scope offsets remain after this pruning, so old updates stay acknowledged.
            db.execute("DELETE FROM tg_updates WHERE ts<?", (now - RETENTION,))

    def queue(self, config, key, text, markup=None):
        with _db() as db:
            _queue(db, config, key, text, markup)

    async def process(self, config, update):
        if not self.current(config) or not isinstance(update, dict):
            return
        uid = update.get("update_id")
        if type(uid) is not int:
            return
        c = config["sms_control"]
        callback = update.get("callback_query")
        message = callback.get("message", {}) if isinstance(callback, dict) else update.get("message", {})
        if not isinstance(message, dict):
            return
        actor = callback.get("from", {}) if isinstance(callback, dict) else message.get("from", {})
        chat = message.get("chat") or {}
        if not isinstance(actor, dict) or not isinstance(chat, dict):
            return
        authorized = (chat.get("type") == "private" and str(chat.get("id")) == c["owner_id"]
                      and str(actor.get("id")) == c["owner_id"] and actor.get("is_bot") is not True)
        with _db() as db:
            cur = db.execute("INSERT OR IGNORE INTO tg_updates VALUES(?,?,?)", (scope(config), uid, int(time.time())))
            if not cur.rowcount:
                return
        # Silently discard outsiders, edits, channel posts, and non-message update types.
        if not authorized:
            return
        key = f"update:{uid}"
        if not self.authorized_line(config):
            self.queue(config, key, "绑定的 SIM 已变更或授权已关闭，请在网页重新配置。未发送短信。")
            return
        if isinstance(callback, dict):
            await self.confirm(config, str(callback.get("data") or ""), key)
            try:
                await asyncio.to_thread(api_call, config, "answerCallbackQuery", {"callback_query_id": callback["id"]})
            except (TelegramError, KeyError):
                pass
            return
        when = message.get("date", 0)
        if type(when) is not int or when < max(c["since"], int(time.time()) - TTL) or when > time.time() + 30:
            self.queue(config, key, "这条指令已过期，请重新输入。未发送短信。")
            return
        text = message.get("text")
        if not isinstance(text, str):
            self.queue(config, key, "仅支持文字短信，请输入 /help 查看用法。")
            return
        if text in ("/start", "/help"):
            self.queue(config, key, "MDD 短信助手（仅本人）\n"
                       "回复收到的短信通知，或输入：\n/sms +国际号码 正文\n"
                       "/status 查看绑定状态\n/recent 查看最近发送结果\n"
                       "每次发送须点击确认，120 秒过期。仅支持普通国际号码，不支持短码、群发或通话。")
            return
        if text == "/status":
            self.queue(config, key, f"已绑定线路 {c['instance_id']}；授权有效。\n"
                       f"上限 {c['daily_limit']} 次提交/滚动24小时。此状态不代表 IMS 已注册。")
            return
        if text == "/recent":
            with _db() as db:
                rows = db.execute("""SELECT d.token,d.peer,d.state,m.status FROM tg_drafts d
                    LEFT JOIN messages m ON m.id=d.message_id
                    WHERE d.scope=? ORDER BY d.created DESC,d.rowid DESC LIMIT 5""", (scope(config),)).fetchall()
            self.queue(config, key, "最近发送（submitted/sent 是已提交，不代表对方收到）：\n" +
                       ("\n".join(f"{r['token'][:6]} · {r['peer']} · {r['state']} / {r['status'] or '—'}" for r in rows) or "暂无记录"))
            return
        peer = ""
        if text.startswith("/sms "):
            parts = text.split(maxsplit=2)
            if len(parts) == 3:
                _, peer, text = parts
        elif not text.startswith("/"):
            reply = message.get("reply_to_message") or {}
            with _db() as db:
                row = db.execute("SELECT * FROM tg_replies WHERE scope=? AND chat=? AND message_id=?",
                                 (scope(config), c["owner_id"], reply.get("message_id"))).fetchone()
            if row and row["identity"] == c["identity"] and row["instance"] == c["instance_id"]:
                peer = row["peer"]
        if not PHONE.fullmatch(peer):
            self.queue(config, key, "请选择一条短信通知回复，或使用 /sms +国际号码 正文。短码及字母发送者不可回复。")
            return
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > MAX_TEXT_UNITS or any(ord(x) < 32 and x not in "\n\t" for x in text):
            self.queue(config, key, "正文为空、包含控制字符或超过 160 个 UTF-16 单位，请缩短后重试。")
            return
        token = secrets.token_hex(12)
        now = int(time.time())
        with _db() as db:
            db.execute("INSERT INTO tg_drafts(token,scope,instance,identity,peer,body,state,created,expires) VALUES(?,?,?,?,?,?,'pending',?,?)",
                       (token, scope(config), c["instance_id"], c["identity"], peer, text, now, now + TTL))
            _queue(db, config, key, f"待确认 · 线路 {c['instance_id']}\n收件人：{peer}\n\n{text}\n\n"
                   "可能按多条短信计费。120 秒内确认；提交不等于送达。",
                   {"inline_keyboard": [[{"text": "确认发送", "callback_data": f"send:{token}"},
                                          {"text": "取消", "callback_data": f"cancel:{token}"}]]})

    async def confirm(self, config, data, key):
        action, _, token = data.partition(":")
        if action not in {"send", "cancel"} or not re.fullmatch(r"[a-f0-9]{24}", token):
            return
        now = int(time.time())
        c = config["sms_control"]
        with _db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tg_drafts WHERE token=? AND scope=?", (token, scope(config))).fetchone()
            if not row or row["state"] != "pending" or row["expires"] <= now:
                _queue(db, config, key, "此确认已使用、已取消或已过期。未再次发送。")
                return
            if action == "cancel":
                db.execute("UPDATE tg_drafts SET state='cancelled' WHERE token=?", (token,))
                _queue(db, config, key, "已取消，未发送短信。")
                return
            used = db.execute("SELECT count(*),max(attempted) FROM tg_drafts WHERE attempted>?", (now - 86400,)).fetchone()
            if used[0] >= c["daily_limit"] or (used[1] is not None and now - used[1] < 10):
                _queue(db, config, key, "达到发送限额或距离上次提交不足10秒，未发送。")
                return
            # This durable transition is the at-most-once boundary for the paid operation.
            db.execute("UPDATE tg_drafts SET state='sending',attempted=? WHERE token=?", (now, token))
            draft = dict(row)
        state, mid = "unknown", None
        try:
            if self.authorized_line(config):
                result = await self.send_sms(config, draft)
                mid = (result.get("message") or {}).get("id")
                state = "submitted" if result.get("ok") else ("rejected" if result.get("unavailable") else "unknown")
            else:
                state = "rejected"
        except asyncio.CancelledError:
            # Recovery also marks a process killed before this write as UNKNOWN.
            raise
        except Exception:
            # No raw gateway errors: they may include identities, credentials or SMS text.
            state = "unknown"
        finally:
            with _db() as db:
                db.execute("UPDATE tg_drafts SET state=?,message_id=? WHERE token=?", (state, mid, token))
        self.queue(config, key, {"submitted": "已提交到网关，不代表对方已收到。可用 /recent 查看结果。",
                                 "rejected": "授权、SIM 或线路不可用，未提交短信。",
                                 "unknown": "发送结果不明，可能已经发出。不会自动重发，请先检查网页短信记录。"}[state])

    async def deliver_one(self, config):
        if not self.current(config):
            return
        with _db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tg_outbox WHERE scope=? AND state='pending' AND next_try<=? ORDER BY id LIMIT 1",
                             (scope(config), int(time.time()))).fetchone()
            if not row:
                return
            db.execute("UPDATE tg_outbox SET state='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
        try:
            if not self.current(config):
                with _db() as db:
                    db.execute("UPDATE tg_outbox SET state='cancelled' WHERE id=?", (row["id"],))
                return
            result = await asyncio.to_thread(api_call, config, "sendMessage", {
                "chat_id": config["chat_id"], "text": row["text"],
                "reply_markup": json.loads(row["markup"]),
                "link_preview_options": {"is_disabled": True}, "protect_content": True})
            if not isinstance(result, dict) or type(result.get("message_id")) is not int:
                raise TelegramError("invalid_response")
            with _db() as db:
                db.execute("UPDATE tg_outbox SET state='sent' WHERE id=?", (row["id"],))
                if row["peer"]:
                    db.execute("INSERT OR REPLACE INTO tg_replies VALUES(?,?,?,?,?,?,?)",
                               (scope(config), str(config["chat_id"]), result["message_id"], row["peer"],
                                row["instance"], row["identity"], int(time.time())))
            self.health["last_push"] = int(time.time())
            self.health.pop("push_error", None)
        except TelegramError as exc:
            self.health["push_error"] = str(exc)
            with _db() as db:
                db.execute("UPDATE tg_outbox SET state='pending',next_try=? WHERE id=?",
                           (int(time.time()) + max(exc.retry_after, min(300, 2 ** min(row["attempts"] + 1, 8))), row["id"]))

    async def poll(self):
        while True:
            config = self.get_config()
            if not enabled(config):
                self.health["state"] = "disabled"
                await asyncio.sleep(2)
                continue
            try:
                with _db() as db:
                    row = db.execute("SELECT next_id FROM tg_offsets WHERE scope=?", (scope(config),)).fetchone()
                updates = await asyncio.to_thread(api_call, config, "getUpdates", {
                    "offset": row[0] if row else 0, "timeout": 20,
                    "allowed_updates": ["message", "callback_query"]}, 25)
                if not isinstance(updates, list):
                    raise TelegramError("invalid_response")
                self.health.update(state="connected", last_poll=int(time.time()))
                for update in updates:
                    if not isinstance(update, dict):
                        continue
                    await self.process(config, update)
                    uid = update.get("update_id")
                    if type(uid) is int:
                        with _db() as db:
                            db.execute("INSERT INTO tg_offsets VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET next_id=max(next_id,excluded.next_id)",
                                       (scope(config), uid + 1))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health["state"] = str(exc) if isinstance(exc, TelegramError) else "internal_error"
                await asyncio.sleep(exc.retry_after if isinstance(exc, TelegramError) else 5)

    async def sender(self):
        while True:
            try:
                config = self.get_config()
                self.maintain(config)
                if enabled(config):
                    await self.deliver_one(config)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health["push_error"] = "internal_error"
            await asyncio.sleep(1)

    async def run(self):
        # Enforce one consumer per installation, including multiple Uvicorn workers.
        os.makedirs(cfg.DATA_DIR, exist_ok=True)
        fd = os.open(os.path.join(cfg.DATA_DIR, "telegram-sms.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        tasks = []
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.health["state"] = "another_worker"
                return
            self.recover()
            tasks = [asyncio.create_task(self.poll()), asyncio.create_task(self.sender())]
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            os.close(fd)

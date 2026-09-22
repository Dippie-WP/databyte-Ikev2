"""Databyte VPN Admin Bot — Telegram interface for the VPN portal.

Lives inside the FastAPI app process (no separate service). Whitelist-only:
chat_id 7748884597 (Zun). Commands call internal portal helpers directly.

Commands:
  /start /help
  /status - service health, 24h auth count, live session count
  /stats - bandwidth today/7d/30d from radacct
  /sessions - live VPN sessions
  /customers - all customers
  /customer <id|name> - drill-down
  /create <name> [display] - create customer, return creds (auto-delete 60s)
  /creds <id|name> - fetch current creds from rw-eap.conf (auto-delete 60s)
  /disable <id|name> - set is_active=0
  /enable <id|name> - set is_active=1
  /disconnect <name> - CoA kick via radclient 127.0.0.1:3779
  /logs <name> [n=20] - last N radpostauth entries
"""
import logging
import os
import re
import subprocess
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import asyncio as _asyncio_MWCH


class MultiWorkerConversationHandler(ConversationHandler):
    """PTB v22 ConversationHandler subclass that forces load + write-through
    to the shared persistence backend (bot_persistence.MariaDBPersistence)
    on every webhook call.

    The default ConversationHandler caches state in self._conversations on
    each Application instance. With multi-worker gunicorn (--workers 4),
    each worker has its own Application instance with its own _conversations
    dict, so the cache never sees state written by other workers. This causes
    the 6-step /create ConversationHandler flow to fail across workers.

    Fix:
    - check_update: on cache miss, synchronously load from
      application.persistence.get_conversations(self.name) via
      asyncio.run_coroutine_threadsafe and populate self._conversations.
    - _update_state: write through to application.persistence.update_conversation
      immediately via application.create_task (async write, non-blocking).
    """

    def check_update(self, update: object):
        """Override to force reload from shared persistence on cache miss.

        HOT-253 (applied 2026-09-21 15:07 UTC): sync (drop async/await) because
        PTB 22 Application calls check_update synchronously inside an async
        context (process_update). Use asyncio.run_coroutine_threadsafe against
        the running loop to load state from MariaDB on cache miss. Reordered so
        super().check_update() runs FIRST, stale-state-clear only triggers if
        super returns None (the v2.6.11 FIX-1 block cleared state for EVERY
        /-prefixed update, breaking /create at step 5/6 by killing /skip and
        /back before the OPTIONAL state handler could match them).
        """
        persistence = getattr(self, "persistence_ref", None)
        if (
            isinstance(update, Update)
            and getattr(self, "_persistent", False)
            and getattr(self, "_name", None)
            and persistence is not None
        ):
            try:
                key = self._get_key(update)
            except Exception:
                return None
            if key is not None and key not in self._conversations:
                try:
                    loop = _asyncio_MWCH.get_event_loop()
                    fut = _asyncio_MWCH.run_coroutine_threadsafe(
                        persistence.get_conversations(self._name),
                        loop,
                    )
                    conversations = fut.result(timeout=1.0)
                    if isinstance(conversations, dict) and key in conversations:
                        self._conversations[key] = conversations[key]
                except Exception:
                    pass
        # HOT-253 (corrected): only clear stale ConvH state if super() does NOT
        # match this update. The v2.6.11 version cleared state for EVERY
        # /-prefixed message, including /skip, /back, /cancel which the
        # OPTIONAL state expects to handle -- that broke the /create flow at
        # step 5/6. Strip is a no-op if no match.
        _super_result = super().check_update(update)
        if (
            _super_result is None
            and update.message
            and update.message.text
            and update.message.text.startswith('/')
        ):
            try:
                _stale_key = self._get_key(update)
                if _stale_key is not None and _stale_key in self._conversations:
                    self._conversations.pop(_stale_key, None)
                    if persistence is not None:
                        try:
                            _sl = _asyncio_MWCH.get_event_loop()
                            _sf = _asyncio_MWCH.run_coroutine_threadsafe(
                                persistence.update_conversation(self._name, _stale_key, None),
                                _sl,
                            )
                            _sf.result(timeout=1.0)
                        except Exception:
                            pass
            except Exception:
                pass
        return _super_result

    async def handle_update(self, update, application, check_result, context):
        """Override to write through to shared persistence immediately.

        Default PTB 22 only calls application.persistence.update_* from the
        generic update_persistence path, which the ConversationHandler does NOT
        override to flush self._conversations. So conversation state would be
        lost across workers on application restart or cache eviction.
        Here we explicitly flush to persistence right after the handler returns.
        """
        # NOTE: PTB 22 Application.process_update does NOT await async
        # check_update results before passing them to handle_update. To work around this,
        # the proper fix is in the caller (app.py webhook handler) which must manually
        # iterate handlers and await check_update results itself, instead of relying on
        # bot_app.process_update() which has this bug. With that caller-side fix,
        # check_result will always be a resolved value here.
        # NOTE: v2.6.11 fix: caller (app.py webhook handler) is responsible for
        # awaiting any coroutine from handler.check_update() before passing to
        # handler.handle_update(). PTB 22 Application.process_update does NOT await
        # async check_update results itself. So check_result may arrive as a coroutine
        # OR as a resolved value (depending on whether the caller awaited it). This
        # handler handles both cases safely:
        #   - None (no ConversationHandler matched) → return None
        #   - coroutine → await it to get the resolved value
        #   - resolved value (tuple/bool) → pass through as-is
        if check_result is None:
            return None
        if _asyncio_MWCH.iscoroutine(check_result):
            check_result = await check_result
        new_state = await super().handle_update(update, application, check_result, context)
        persistence = getattr(self, "persistence_ref", None)
        if (
            new_state is not None
            and getattr(self, "_persistent", False)
            and getattr(self, "_name", None)
            and persistence is not None
        ):
            try:
                key = self._conversation_key(context)
                if key is not None:
                    # Persist synchronously so the next webhook call from any
                    # worker sees the fresh state via check_update reload.
                    loop = _asyncio_MWCH.get_event_loop()
                    fut = _asyncio_MWCH.run_coroutine_threadsafe(
                        persistence.update_conversation(self._name, key, new_state),
                        loop,
                    )
                    fut.result(timeout=1.0)
            except Exception:
                pass
        return new_state

    async def update_persistence(self, context):
        """v2.6.5 fix: reload conversations from shared persistence BEFORE save.

        PTB's default update_persistence only WRITES self._conversations to the
        persistence backend — it does NOT read from persistence on every webhook call.
        In multi-worker gunicorn, each worker has its own Application instance with its
        own self._conversations cache. By reloading from the shared MariaDB backend
        here, every webhook call sees the latest state across all workers.

        See github.com/python-telegram-bot/python-telegram-bot/issues/5225.
        """
        persistence = getattr(self, "persistence_ref", None)
        if (
            getattr(self, "_persistent", False)
            and getattr(self, "_name", None)
            and persistence is not None
        ):
            try:
                stored = await persistence.get_conversations(self._name)
                if isinstance(stored, dict):
                    # Refresh local cache from shared backend
                    self._conversations.update(stored)
            except Exception:
                pass
        await super().update_persistence(context)

# Secrets loaded from file (chmod 600, owner vpn-portal:vpn-portal).
# Never logged, never echoed to chat.
TELEGRAM_TOKEN = open("/etc/databyte-vpn-bot/telegram_token").read().strip()
WEBHOOK_SECRET = open("/etc/databyte-vpn-bot/webhook_secret").read().strip()

AUDIT_LOG = "/var/log/vpn-portal/bot-audit.log"
ALLOWED_CHAT_ID = 7748884597  # Zun
RW_EAP_CONF = os.environ.get(
    "RW_EAP_CONF",
    "/opt/strongswan-vpn-gateway/docker/swanctl/conf.d/rw-eap.conf",
)

logger = logging.getLogger("vpn-bot")


def audit(action: str, **details):
    """Append-only audit log. No secrets."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    parts = " ".join(f"{k}={v}" for k, v in details.items())
    line = f"{ts} chat_id={ALLOWED_CHAT_ID} action={action} {parts}\n"
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(line)
    except Exception as e:
        logger.error(f"audit log write failed: {e}")


async def reject_non_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silently reject anything from non-whitelisted chat_ids."""
    chat = update.effective_chat
    if chat and chat.id != ALLOWED_CHAT_ID:
        u = update.effective_user
        uname = u.username if u else None
        logger.warning(f"REJECT chat_id={chat.id} user={uname}")


async def _auto_delete_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    try:
        await context.bot.delete_message(chat_id=job.chat_id, message_id=job.data)
    except Exception as e:
        logger.warning(f"auto-delete failed msg={job.data}: {e}")


def schedule_delete(context, chat_id: int, message_id: int, delay: int = 60):
    """Schedule auto-delete of a message (default 60s).

    No-op if the Application was built without a JobQueue (job_queue is None).
    The customer creation still succeeds — this just means the success/error
    message won't be auto-deleted after 60s.
    """
    if context.job_queue is None:
        return
    context.job_queue.run_once(
        _auto_delete_job, delay, chat_id=chat_id, data=message_id
    )


def send_credentials_sync(
    *,
    telegram_id: str,
    eap_identity: str,
    password: str,
    tier_display: str = "",
) -> None:
    """Mock: send credentials via Telegram. No-op.

    In production the bot runs in webhook mode (HOT-260); credentials are
    surfaced via:
    - the API return dict that ct_confirm formats into the success message
    - the audit log entry that records the /create action
    - the operator via /api/customers GET

    This function exists as a placeholder that app.create_client / update_customer
    call (5 places in app.py:1513-1569) so those code paths don't AttributeError.
    Real Telegram push happens at user-initiated onboarding, not at bot /create.

    STAGE CODE: CT_CRED_SEND_FAIL -- if this raises, app.py:1513-1569 catches it.
    """
    audit(
        "send_credentials",
        telegram_id=telegram_id,
        eap_identity=eap_identity,
    )
    logger.info(
        "send_credentials_sync called telegram_id=%s eap_identity=%s tier=%s (no-op mock)",
        telegram_id, eap_identity, tier_display,
    )


# ---------- Commands ----------

async def cmd_start(update, context):
    audit("start")
    logger.info("cmd_start fired for chat_id=%s", update.effective_chat.id)
    await update.message.reply_text(
        "Databyte VPN Admin Bot\n\n"
        "/help - list commands\n"
        "/status - health + activity\n"
        "/stats - bandwidth today/7d/30d\n"
        "/sessions - live VPN sessions\n"
        "/customers - all customers\n"
        "/customer <id|name> - drill-down\n"
        "/create <name> [display] - new customer\n"
        "/creds <id|name> - fetch creds (auto-del 60s)\n"
        "/disable /enable <id|name>\n"
        "/disconnect <name> - CoA kick\n"
        "/logs <name> [n]"
    )


async def cmd_help(update, context):
    audit("help")
    await update.message.reply_text(
        "/status /stats /sessions /customers\n"
        "/customer <id|name> /create <name> [display]\n"
        "/creds <id|name> /disable /enable <id|name>\n"
        "/disconnect <name> /logs <name> [n=20]"
    )


async def cmd_status(update, context):
    audit("status")
    import app
    health = app.health()
    auth24 = app.db_query(
        "SELECT COUNT(*) AS n FROM radpostauth "
        "WHERE authdate >= NOW() - INTERVAL 1 DAY"
    )
    n24 = auth24[0]["n"] if auth24 else 0
    sessions = app.swanctl_parse_sas()
    live = sum(1 for s in sessions if s.get("state") == "ESTABLISHED")
    text = (
        "*Service Health*\n"
        f"DB: `{health.get('db_ok')}` customers: `{health.get('db_customers')}`\n"
        f"charon: `{health.get('charon_ok')}`\n\n"
        "*Activity*\n"
        f"Auth (24h): `{n24}`\n"
        f"Live sessions: `{live}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_stats(update, context):
    audit("stats")
    import app

    def fmt(b):
        if not b:
            return "0 B"
        b = int(b)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} PB"

    def row(interval):
        r = app.db_query(
            "SELECT COALESCE(SUM(acctinputoctets),0) AS inb, "
            "COALESCE(SUM(acctoutputoctets),0) AS outb, COUNT(*) AS n "
            f"FROM radacct WHERE acctstarttime >= NOW() - INTERVAL {interval}"
        )
        return r[0] if r else {"inb": 0, "outb": 0, "n": 0}

    r1, r7, r30 = row("1 DAY"), row("7 DAY"), row("30 DAY")
    text = (
        "*Bandwidth*\n"
        f"Today: {fmt(r1['inb'])} in / {fmt(r1['outb'])} out ({r1['n']} sessions)\n"
        f"7d:    {fmt(r7['inb'])} in / {fmt(r7['outb'])} out ({r7['n']} sessions)\n"
        f"30d:   {fmt(r30['inb'])} in / {fmt(r30['outb'])} out ({r30['n']} sessions)"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_sessions(update, context):
    audit("sessions")
    import app
    sessions = app.swanctl_parse_sas()
    if not sessions:
        await update.message.reply_text("No active sessions.")
        return
    lines = ["*Active Sessions*"]
    for s in sessions[:25]:
        rid = s.get("remote_id") or "?"
        rip = s.get("remote_ip") or "?"
        vip = s.get("vip") or "?"
        st = s.get("state") or "?"
        bi = s.get("bytes_in", 0)
        bo = s.get("bytes_out", 0)
        lines.append(
            f"- `{rid}` `{rip}` -> `{vip}` `{st}` (in:{bi:,} out:{bo:,})"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_customers(update, context):
    audit("customers")
    import app
    rows = app.db_query(
        "SELECT id, name, display_name, is_active "
        "FROM customers WHERE COALESCE(is_operator, 0) = 0 ORDER BY id"
    )
    if not rows:
        await update.message.reply_text("No customers.")
        return
    lines = ["*Customers*"]
    for c in rows:
        archived = bool(c.get("is_archived"))
        active = bool(c.get("is_active"))
        if archived:
            flag = "Y"  # yellow archived
        elif active:
            flag = "G"  # green active
        else:
            flag = "R"  # red disabled
        lines.append(
            f"[{flag}] `{c['id']}` `{c['name']}` - {c.get('display_name') or ''}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_customer(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /customer <id|name>")
        return
    arg = context.args[0]
    audit("customer", arg=arg)
    import app
    if arg.isdigit():
        rows = app.db_query("SELECT * FROM customers WHERE id = ?", (int(arg),))
    else:
        rows = app.db_query("SELECT * FROM customers WHERE name = ?", (arg,))
    if not rows:
        await update.message.reply_text(f"No customer `{arg}`.")
        return
    c = rows[0]
    devs = app.db_query(
        "SELECT id, device_name, device_type, is_active "
        "FROM devices WHERE customer_id = ?",
        (c["id"],),
    )
    dev_lines = []
    for d in devs:
        flag = "G" if d.get("is_active") else "R"
        dev_lines.append(
            f"  [{flag}] `{d['id']}` `{d.get('device_name')}` ({d.get('device_type')})"
        )
    text = (
        f"*Customer #{c['id']}*\n"
        f"Slug: `{c['name']}`\n"
        f"Display: {c.get('display_name') or '-'}\n"
        f"Active: {bool(c.get('is_active'))} "
        f"Tier: {c.get('tier_id')} Created: {c.get('created_at')}\n"
        f"Devices:\n" + ("\n".join(dev_lines) if dev_lines else "  (none)")
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------- /create ConversationHandler (interactive onboarding) ----------
# Replaces the one-shot cmd_create with a 6-step state machine:
#   TIER -> DEVICE_TYPE -> DEVICE_NAME -> SPEED_PLAN -> OPTIONAL -> CONFIRM
# Each step uses inline keyboards for choices; free text for typed answers.
# /back and /cancel work from any state. /skip is honoured at OPTIONAL
# (and via a dedicated skip button at SPEED_PLAN -> defaults to standard).

# ConversationHandler state IDs
TIER, DEVICE_TYPE, DEVICE_NAME, SPEED_PLAN, OPTIONAL, CONFIRM = range(6)

DEVICE_TYPES = ["iOS", "Android", "Windows", "macOS", "Linux", "Other"]
SPEED_PLANS = ["standard", "asymmetric_40_20"]


def _kb(rows):
    return InlineKeyboardMarkup(rows)


async def _safe_edit_text(q, text, **kwargs):
    """q.edit_message_text() that logs+swallows BadRequest on stale callbacks.

    PTB 22.8 raises BadRequest('Message to edit not found') / 'Query is too
    old' when callback_query.message_id references a message that no longer
    exists in the chat (common with synthetic test callbacks, also possible
    if the user clears chat history between updates). The edit is purely
    cosmetic — losing it does NOT break the conversation state machine.

    NOTE (TKT-028, 2026-09-22 00:15 UTC): the prior version called itself
    recursively instead of q.edit_message_text — every ct_*_chosen handler
    silently stack-overflowed. Never recurse; always call the real API.
    Unexpected (non-BadRequest) exceptions are now logged at WARNING so the
    next silent-failure class is visible in journalctl without changes.
    """
    try:
        return await q.edit_message_text(text, **kwargs)
    except Exception as exc:
        logger.warning("_safe_edit_text suppressed: %s", exc)
        return None


def _tier_kb():
    """Build the TIER inline keyboard from live DB query."""
    import app
    tiers = app.db_query(
        "SELECT name, duration_days FROM tiers "
        "WHERE is_active = 1 ORDER BY name ASC, id ASC"
    )
    if not tiers:
        return None
    rows, row = [], []
    for t in tiers:
        dur = t.get("duration_days") if t.get("duration_days") else "inf"
        row.append(InlineKeyboardButton(
            f"{t['name']} ({dur}d)",
            callback_data=f"ct:{t['name']}",
        ))
        if len(row) == 1:        # one tier per row → one-column picker layout (Zun msg #41189, 2026-09-22 04:35 UTC)
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Cancel", callback_data="cx")])
    return _kb(rows)


async def create_start(update, context):
    """/create <name> [display] -- ConversationHandler entry."""
    # TKT-028 diagnostic: surface exactly where /create breaks (logger v22 logger name = vpn-bot)
    logger.info(
        "create_start ENTERED chat_id=%s args=%r",
        update.effective_chat.id if update.effective_chat else None,
        context.args,
    )
    try:
        if not context.args:
            await update.message.reply_text(
                "Usage: /create <name> [display_name]\n"
                'Example: /create zun-acme "Acme Corp"'
            )
            return ConversationHandler.END

        name = context.args[0].lower()
        display = context.args[1] if len(context.args) > 1 else name
        audit("create", name=name)

        import app
        if app.db_query("SELECT id FROM customers WHERE name = ?", (name,)):
            await update.message.reply_text(
                f"Customer `{name}` already exists. Use /creds to fetch."
            )
            return ConversationHandler.END

        kb = _tier_kb()
        if kb is None:
            await update.message.reply_text("No active tier configured. Cannot create customer.")
            return ConversationHandler.END

        context.user_data["ct_name"] = name
        context.user_data["ct_display"] = display

        await update.message.reply_text(
            f"Creating `{name}` (display: `{display}`)\n\n[1/6] Pick a tier:",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logger.exception("create_start RAISED %s: %s", type(exc).__name__, exc)
        raise
    return TIER


async def ct_tier_chosen(update, context):
    """[1/6] tier selected -> advance to DEVICE_TYPE."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass  # q.answer() can fail with BadRequest("Query is too old") for
        # synthetic test callbacks or stale query_ids; not critical to flow.
    if q.data == "cx":
        await _safe_edit_text(q, "Cancelled.")
        return ConversationHandler.END
    tier = q.data.split(":", 1)[1]
    context.user_data["ct_tier"] = tier

    rows = [
        [InlineKeyboardButton("iOS", callback_data="cdt:iOS"),
         InlineKeyboardButton("Android", callback_data="cdt:Android"),
         InlineKeyboardButton("Windows", callback_data="cdt:Windows")],
        [InlineKeyboardButton("macOS", callback_data="cdt:macOS"),
         InlineKeyboardButton("Linux", callback_data="cdt:Linux"),
         InlineKeyboardButton("Other", callback_data="cdt:Other")],
        [InlineKeyboardButton("Back", callback_data="cb"),
         InlineKeyboardButton("Cancel", callback_data="cx")],
    ]
    await _safe_edit_text(q, "[2/6] Device type?", reply_markup=_kb(rows))
    return DEVICE_TYPE


async def ct_dt_chosen(update, context):
    """[2/6] device_type selected -> advance to DEVICE_NAME."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if q.data == "cx":
        await _safe_edit_text(q, "Cancelled.")
        return ConversationHandler.END
    if q.data == "cb":
        await _safe_edit_text(q, "[1/6] Pick a tier:", reply_markup=_tier_kb())
        return TIER
    dt = q.data.split(":", 1)[1]
    if dt not in DEVICE_TYPES:
        await q.answer(f"Unknown type: {dt}")
        return DEVICE_TYPE
    context.user_data["ct_dt"] = dt
    await _safe_edit_text(q, 
        "[3/6] Device name?\n"
        "Alphanumeric + dash, 1-32 chars. e.g. `iphone`, `laptop`, `pixel9`.\n"
        "/back or /cancel.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return DEVICE_NAME


async def ct_dn_entered(update, context):
    """[3/6] device name entered (free text) -> advance to SPEED_PLAN."""
    text = update.message.text.strip()
    if text.lower() == "/cancel":
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END
    if text.lower() == "/back":
        rows = [
            [InlineKeyboardButton("iOS", callback_data="cdt:iOS"),
             InlineKeyboardButton("Android", callback_data="cdt:Android"),
             InlineKeyboardButton("Windows", callback_data="cdt:Windows")],
            [InlineKeyboardButton("macOS", callback_data="cdt:macOS"),
             InlineKeyboardButton("Linux", callback_data="cdt:Linux"),
             InlineKeyboardButton("Other", callback_data="cdt:Other")],
            [InlineKeyboardButton("Back", callback_data="cb"),
             InlineKeyboardButton("Cancel", callback_data="cx")],
        ]
        await update.message.reply_text("[2/6] Device type?", reply_markup=_kb(rows))
        return DEVICE_TYPE
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$", text):
        await update.message.reply_text(
            f"Invalid `{text}`. Use alphanumeric + dash, 1-32 chars, no leading dash.\n"
            "/back or /cancel."
        )
        return DEVICE_NAME
    cust = context.user_data["ct_name"]
    if text.lower() == cust.lower() or text.lower().startswith(cust.lower() + "-"):
        await update.message.reply_text(
            f"`{text}` duplicates customer name `{cust}`. Use a different name.\n"
            "/back or /cancel."
        )
        return DEVICE_NAME
    import app
    eap = f"{cust}-{text}"
    if app.db_query("SELECT id FROM users WHERE name = ?", (eap,)):
        await update.message.reply_text(
            f"EAP identity `{eap}` already exists. Try a different name.\n"
            "/back or /cancel."
        )
        return DEVICE_NAME

    context.user_data["ct_dn"] = text
    rows = [
        [InlineKeyboardButton("standard (20/20)", callback_data="csp:standard"),
         InlineKeyboardButton("asymmetric_40_20 (40/20)", callback_data="csp:asymmetric_40_20")],
        [InlineKeyboardButton("Skip -> standard", callback_data="csk"),
         InlineKeyboardButton("Back", callback_data="cb"),
         InlineKeyboardButton("Cancel", callback_data="cx")],
    ]
    await update.message.reply_text(
        "[4/6] Speed plan? Or Skip -> standard (20/20).",
        reply_markup=_kb(rows),
    )
    return SPEED_PLAN


async def ct_sp_chosen(update, context):
    """[4/6] speed plan selected (or skip) -> advance to OPTIONAL."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if q.data == "cx":
        await _safe_edit_text(q, "Cancelled.")
        return ConversationHandler.END
    if q.data == "cb":
        await _safe_edit_text(q, 
            "[3/6] Device name?\n"
            "Alphanumeric + dash, 1-32 chars. e.g. `iphone`, `laptop`, `pixel9`.\n"
            "/back or /cancel.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return DEVICE_NAME
    if q.data == "csk":
        context.user_data["ct_sp"] = "standard"
        await _safe_edit_text(q, 
            "[5/6] Optional fields? Send one line each:\n"
            "  email: user@example.com\n"
            "  telegram: @handle\n"
            "  mac: AA:BB:CC:DD:EE:FF\n"
            "Or /skip. /back or /cancel."
        )
        return OPTIONAL
    sp = q.data.split(":", 1)[1]
    if sp not in SPEED_PLANS:
        await q.answer(f"Unknown plan: {sp}")
        return SPEED_PLAN
    context.user_data["ct_sp"] = sp
    await _safe_edit_text(q, 
        "[5/6] Optional fields? Send one line each:\n"
        "  email: user@example.com\n"
        "  telegram: @handle\n"
        "  mac: AA:BB:CC:DD:EE:FF\n"
        "Or /skip. /back or /cancel."
    )
    return OPTIONAL


async def ct_opt_entered(update, context):
    """[5/6] optional fields entered (or /skip) -> advance to CONFIRM."""
    text = update.message.text.strip()
    if text.lower() == "/cancel":
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END
    if text.lower() == "/back":
        rows = [
            [InlineKeyboardButton("standard (20/20)", callback_data="csp:standard"),
             InlineKeyboardButton("asymmetric_40_20 (40/20)", callback_data="csp:asymmetric_40_20")],
            [InlineKeyboardButton("Skip -> standard", callback_data="csk"),
             InlineKeyboardButton("Back", callback_data="cb"),
             InlineKeyboardButton("Cancel", callback_data="cx")],
        ]
        await update.message.reply_text(
            "[4/6] Speed plan? Or Skip -> standard (20/20).",
            reply_markup=_kb(rows),
        )
        return SPEED_PLAN
    if text.lower() != "/skip":
        email = telegram = mac = None
        for line in text.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip().lower()
            v = v.strip()
            if k == "email":
                email = v
            elif k == "telegram":
                telegram = v.lstrip("@")
            elif k == "mac":
                mac = v.upper()
        context.user_data["ct_email"] = email
        context.user_data["ct_telegram"] = telegram
        context.user_data["ct_mac"] = mac

    name = context.user_data["ct_name"]
    display = context.user_data["ct_display"]
    tier = context.user_data["ct_tier"]
    dt = context.user_data["ct_dt"]
    dn = context.user_data["ct_dn"]
    sp = context.user_data["ct_sp"]
    eap = f"{name}-{dn}"
    email = context.user_data.get("ct_email") or "-"
    telegram = context.user_data.get("ct_telegram") or "-"
    mac = context.user_data.get("ct_mac") or "-"

    summary = (
        f"[6/6] Confirm:\n\n"
        f"  Name: `{name}`\n"
        f"  Display: `{display}`\n"
        f"  Tier: `{tier}`\n"
        f"  Device: `{dt}` / `{dn}`\n"
        f"  EAP: `{eap}`\n"
        f"  Speed: `{sp}`\n"
        f"  Email: `{email}`\n"
        f"  Telegram: `{telegram}`\n"
        f"  MAC: `{mac}`\n\n"
        f"Confirm?"
    )
    rows = [
        [InlineKeyboardButton("Confirm", callback_data="cok")],
        [InlineKeyboardButton("Back", callback_data="cb"),
         InlineKeyboardButton("Cancel", callback_data="cx")],
    ]
    await update.message.reply_text(
        summary, reply_markup=_kb(rows), parse_mode=ParseMode.MARKDOWN,
    )
    return CONFIRM


async def ct_confirm(update, context):
    """[6/6] confirm pressed -> call app.create_client + send creds."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if q.data == "cx":
        await _safe_edit_text(q, "Cancelled.")
        return ConversationHandler.END
    if q.data == "cb":
        await _safe_edit_text(q, 
            "[5/6] Optional fields? Send one line each:\n"
            "  email: user@example.com\n"
            "  telegram: @handle\n"
            "  mac: AA:BB:CC:DD:EE:FF\n"
            "Or /skip. /back or /cancel."
        )
        return OPTIONAL

    name = context.user_data["ct_name"]
    display = context.user_data["ct_display"]
    tier = context.user_data["ct_tier"]
    dt = context.user_data["ct_dt"]
    dn = context.user_data["ct_dn"]
    sp = context.user_data["ct_sp"]
    email = context.user_data.get("ct_email")
    telegram = context.user_data.get("ct_telegram")
    mac = context.user_data.get("ct_mac")

    try:
        import app
        from app import ClientCreate
        req = ClientCreate(
            name=name, display_name=display, tier_name=tier,
            device_name=dn, device_type=dt, speed_plan=sp,
            email=email, telegram_username=telegram, mac_address_1=mac,
        )
        result = app.create_client(
            req, _user={"name": "bot", "role": "operator"}
        )
    except Exception as e:
        err_code = "CT_CONFIRM_DBM_FAIL"
        logger.exception("[%s] /create failed at ct_confirm: %s", err_code, e)
        await _safe_edit_text(q, f"Create failed [{err_code}]: {{e}}".format(e=e))
        return ConversationHandler.END

    cust = result.get("customer", {})
    eap_id = result.get("eap_identity")
    password = result.get("password")
    text = (
        f"OK Customer `{name}` created (id=`{cust.get('id')}`)\n"
        f"Tier: `{tier}`\n\n"
        f"! *Self-destructs in 60s*\n\n"
        f"Server: `myvpn.databyte.co.za`\n"
        f"EAP identity: `{eap_id}`\n"
        f"Password: `{password}`\n"
        f"CA cert: https://myvpn.databyte.co.za/certs/strongswan-ca.crt.pem"
    )
    msg = await _safe_edit_text(q, text, parse_mode=ParseMode.MARKDOWN)
    if msg is None:
        return ConversationHandler.END  # edit failed; credentials not sent but customer was created
    schedule_delete(context, update.effective_chat.id, msg.message_id, 60)
    return ConversationHandler.END


async def ct_cancel(update, context):
    """Fallback: /cancel from any state."""
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def cmd_creds(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /creds <id|name>")
        return
    arg = context.args[0]
    audit("creds", arg=arg)
    import app

    if arg.isdigit():
        rows = app.db_query(
            "SELECT id, name, user_id FROM customers WHERE id = ?", (int(arg),)
        )
    else:
        rows = app.db_query(
            "SELECT id, name, user_id FROM customers WHERE name = ?", (arg,)
        )
    if not rows:
        await update.message.reply_text(f"No customer `{arg}`.")
        return

    customer_id = rows[0]["id"]
    user_id = rows[0].get("user_id")
    if not user_id:
        await update.message.reply_text(
            f"Customer `{rows[0]['name']}` has no EAP user linked."
        )
        return
    u = app.db_query("SELECT name FROM users WHERE id = ?", (user_id,))
    if not u:
        await update.message.reply_text(f"users row missing for id={user_id}.")
        return
    eap_identity = u[0]["name"]

    try:
        conf = open(RW_EAP_CONF).read()
    except Exception as e:
        await update.message.reply_text(f"Cannot read {RW_EAP_CONF}: {e}")
        return

    block_id = f"eap-{eap_identity}"
    block_pat = re.compile(
        rf"^\s*{re.escape(block_id)}\s*\{{[^}}]*?secret\s*=\s*\"([^\"]+)\"",
        re.MULTILINE | re.DOTALL,
    )
    m = block_pat.search(conf)
    if not m:
        await update.message.reply_text(
            f"No EAP block `{block_id}` in rw-eap.conf."
        )
        return
    password = m.group(1)

    text = (
        f"Creds for `{rows[0]['name']}` (id=`{customer_id}`)\n\n"
        f"! *Self-destructs in 60s*\n\n"
        f"Server: `myvpn.databyte.co.za`\n"
        f"EAP identity: `{eap_identity}`\n"
        f"Password: `{password}`"
    )
    msg = await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    schedule_delete(context, update.effective_chat.id, msg.message_id, 60)


async def cmd_disable(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /disable <id|name>")
        return
    arg = context.args[0]
    audit("disable", arg=arg)
    import app
    if arg.isdigit():
        cid = int(arg)
    else:
        rows = app.db_query("SELECT id FROM customers WHERE name = ?", (arg,))
        if not rows:
            await update.message.reply_text(f"No customer `{arg}`.")
            return
        cid = rows[0]["id"]
    app.db_exec("UPDATE customers SET is_active = 0 WHERE id = ?", (cid,))
    await update.message.reply_text(f"Disabled customer id=`{cid}`.")


async def cmd_enable(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /enable <id|name>")
        return
    arg = context.args[0]
    audit("enable", arg=arg)
    import app
    if arg.isdigit():
        cid = int(arg)
    else:
        rows = app.db_query("SELECT id FROM customers WHERE name = ?", (arg,))
        if not rows:
            await update.message.reply_text(f"No customer `{arg}`.")
            return
        cid = rows[0]["id"]
    app.db_exec("UPDATE customers SET is_active = 1 WHERE id = ?", (cid,))
    await update.message.reply_text(f"Enabled customer id=`{cid}`.")


async def cmd_disconnect(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /disconnect <name>")
        return
    arg = context.args[0]
    audit("disconnect", arg=arg)
    import app

    rows = app.db_query(
        "SELECT u.name AS eap_identity FROM customers c "
        "JOIN users u ON c.user_id = u.id "
        "WHERE c.id = ? OR c.name = ? LIMIT 1",
        (int(arg) if arg.isdigit() else 0, arg),
    )
    if not rows:
        await update.message.reply_text(f"No customer matches `{arg}`.")
        return
    eap_identity = rows[0]["eap_identity"]

    cmd = [
        "sudo", "radclient", "127.0.0.1:3799", "coa",
        "b305c63a5010d2c309e29df7bab0fe66",
        f"User-Name={eap_identity}",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            await update.message.reply_text(
                f"CoA disconnect sent for `{eap_identity}`."
            )
        else:
            await update.message.reply_text(
                f"Disconnect failed:\n`{(res.stderr or res.stdout).strip()[:500]}`"
            )
    except subprocess.TimeoutExpired:
        await update.message.reply_text("Disconnect timed out after 10s.")
    except Exception as e:
        await update.message.reply_text(f"Disconnect error: {e}")


async def cmd_logs(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /logs <name> [n=20]")
        return
    name = context.args[0]
    n = 20
    if len(context.args) > 1 and context.args[1].isdigit():
        n = int(context.args[1])
    audit("logs", name=name, n=n)
    import app

    rows = app.db_query("SELECT user_id FROM customers WHERE name = ?", (name,))
    eap_id = name
    if rows and rows[0].get("user_id"):
        u = app.db_query("SELECT name FROM users WHERE id = ?", (rows[0]["user_id"],))
        if u:
            eap_id = u[0]["name"]

    auths = app.db_query(
        "SELECT authdate, reply, class "
        "FROM radpostauth WHERE username = ? "
        "ORDER BY authdate DESC LIMIT ?",
        (eap_id, n),
    )
    if not auths:
        await update.message.reply_text(f"No auth events for `{eap_id}`.")
        return
    lines = [f"*Last {len(auths)} auth events for `{eap_id}`*"]
    for r in auths:
        lines.append(
            f"`{r['authdate']}` {r['reply']} "
            f"class=`{r.get('class') or '-'}`"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------- Build Application ----------

def build_application() -> Application:
    from urllib.parse import urlparse
    from bot_persistence import MariaDBPersistence

    # Parse DB connection from /etc/vpn-portal.env DB_URL.
    # Format: mysql+pymysql://user:password@host:port/database
    db_url = os.environ.get("DB_URL", "mysql+pymysql://portal@127.0.0.1:3306/radius")
    parsed = urlparse(db_url)
    db_config = {
        "host": parsed.hostname or "127.0.0.1",
        "port": int(parsed.port) if parsed.port else 3306,
        "user": parsed.username or "portal",
        "password": parsed.password or "",
        "database": (parsed.path or "/radius").lstrip("/") or "radius",
    }
    persistence = MariaDBPersistence(db_config)

    application = Application.builder().token(TELEGRAM_TOKEN).persistence(persistence).build()

    whitelist = filters.User(user_id=ALLOWED_CHAT_ID)
    application.add_handler(MessageHandler(~whitelist, reject_non_admin))

    # /create is now a ConversationHandler (interactive onboarding).
    # It must be registered BEFORE the command loop so it claims /create
    # before the regular CommandHandler does.
    # Use MultiWorkerConversationHandler (defined above in imports) instead
    # of the default ConversationHandler so cross-worker state persistence
    # works -- the default caches state per-instance, which fragments
    # across gunicorn --workers 4 workers.
    create_conv = MultiWorkerConversationHandler(
        entry_points=[
            CommandHandler("create", create_start, filters=whitelist),
        ],
        states={
            TIER: [
                CallbackQueryHandler(ct_tier_chosen, pattern=r"^ct:"),
                CallbackQueryHandler(ct_tier_chosen, pattern=r"^cx$"),
            ],
            DEVICE_TYPE: [
                CallbackQueryHandler(ct_dt_chosen, pattern=r"^cdt:"),
                CallbackQueryHandler(ct_dt_chosen, pattern=r"^cb$"),
                CallbackQueryHandler(ct_dt_chosen, pattern=r"^cx$"),
            ],
            DEVICE_NAME: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    ct_dn_entered,
                ),
                CommandHandler("back", ct_dn_entered),
            ],
            SPEED_PLAN: [
                CallbackQueryHandler(ct_sp_chosen, pattern=r"^csp:"),
                CallbackQueryHandler(ct_sp_chosen, pattern=r"^csk$"),
                CallbackQueryHandler(ct_sp_chosen, pattern=r"^cb$"),
                CallbackQueryHandler(ct_sp_chosen, pattern=r"^cx$"),
            ],
            OPTIONAL: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    ct_opt_entered,
                ),
                CommandHandler("skip", ct_opt_entered),
                CommandHandler("back", ct_opt_entered),
            ],
            CONFIRM: [
                CallbackQueryHandler(ct_confirm, pattern=r"^cok$"),
                CallbackQueryHandler(ct_confirm, pattern=r"^cb$"),
                CallbackQueryHandler(ct_confirm, pattern=r"^cx$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", ct_cancel),
        ],
        persistent=True,
        name="create_conv",
    )
    # Wire the shared persistence into the conversation handler so its overrides
    # can reload + write-through cross-worker (MultiWorkerConversationHandler uses
    # self.persistence_ref rather than relying on self.application which doesn't exist
    # in PTB 22 BaseHandler).
    create_conv.persistence_ref = persistence
    application.add_handler(create_conv)

    commands = [
        ("start", cmd_start),
        ("help", cmd_help),
        ("status", cmd_status),
        ("stats", cmd_stats),
        ("sessions", cmd_sessions),
        ("customers", cmd_customers),
        ("customer", cmd_customer),
        ("creds", cmd_creds),
        ("disable", cmd_disable),
        ("enable", cmd_enable),
        ("disconnect", cmd_disconnect),
        ("logs", cmd_logs),
    ]
    for cmd, handler in commands:
        application.add_handler(CommandHandler(cmd, handler, filters=whitelist))

    return application
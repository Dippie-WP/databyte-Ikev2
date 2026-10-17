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
import threading
from datetime import datetime, timezone

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
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

# Feature flag: block user for N seconds after Disconnect-Request. Layered
# block via two independent mechanisms (so either alone would defeat Android's
# auto-reconnect, both together is bulletproof):
#   1. INSERT radcheck row (username, 'Auth-Type', ':=', 'Reject') — checked
#      BEFORE password attrs in FreeRADIUS authorize section, so Access-Reject
#      is immediate regardless of stored credentials.
#   2. iptables -I INPUT -s <calling_station_id> -p udp --dport 500,4500 -j
#      DROP — Android client can't even reach charon to retry EAP.
# Both layers revert via threading.Timer after block_seconds.
# Set DISCONNECT_BLOCK_SECONDS=N (e.g. 300 = 5 min) on prod to enable.
# Default 0 = disabled (preserves existing behavior).
DISCONNECT_BLOCK_SECONDS = int(os.environ.get("DISCONNECT_BLOCK_SECONDS", "0"))

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


def _block_user(eap_identity: str, source_ip: str, block_seconds: int) -> None:
    """Block user via two independent layers for N seconds.

    Option 2 (TKT-030): direct pymysql + iptables subprocess with check=True.
    Replaces the earlier SQLAlchemy _db()/app.db_exec path which had a
    connection-pool visibility bug on the LXC 909 dry run (INSERT was
    committed inside the worker process but invisible to other mariadb
    sessions). Direct pymysql.connect() bypasses the pool entirely.

    Layer 1 (RADIUS): INSERT radcheck row (username, 'Auth-Type', ':=', 'Reject').
    FreeRADIUS checks Auth-Type BEFORE password attrs in the authorize
    section, so Access-Reject is immediate regardless of stored creds.

    Layer 2 (network): iptables -I INPUT -s <source_ip> -p udp --dport
    500,4500 -j DROP. Android client can't even reach charon to retry EAP.
    Uses check=True so iptables errors surface (previously silently failed).

    Either layer alone blocks reconnection; both is bulletproof.
    Both revert via a single threading.Timer after block_seconds.

    Failures in either layer are logged and don't propagate. The
    disconnect itself already succeeded; only the block might not apply.
    """
    # Parse DB_URL for direct pymysql connection (pattern from rotate_eap_credentials)
    db_url = os.environ.get("DB_URL", "")
    # Expected: mysql+pymysql://user:password@host:port/database
    db_user = db_pass = db_host = db_port = db_name = ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(db_url)
        db_user = parsed.username or ""
        db_pass = parsed.password or ""
        db_host = parsed.hostname or "127.0.0.1"
        db_port = str(parsed.port or 3306)
        db_name = (parsed.path or "/radius").lstrip("/") or "radius"
    except Exception:
        pass

    try:
        import pymysql
        conn = pymysql.connect(
            host=db_host, port=int(db_port),
            user=db_user, password=db_pass,
            database=db_name, connect_timeout=5,
            autocommit=True,  # critical: write is immediately visible
        )
        try:
            with conn.cursor() as cur:
                # Layer 1: FreeRADIUS Auth-Type=Reject
                # Uses ? placeholders (pymysql native)
                cur.execute(
                    "INSERT INTO radcheck (username, attribute, op, value) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE value=%s",
                    (eap_identity, "Auth-Type", ":=", "Reject", "Reject"),
                )
        finally:
            conn.close()

        # Layer 2: iptables DROP for source IP (block at network layer).
        # Uses -I (insert at top) so the rule is evaluated first. Uses
        # check=True so iptables errors surface instead of silently failing.
        if source_ip:
            try:
                subprocess.run(
                    [
                        "iptables", "-I", "INPUT",
                        "-s", source_ip,
                        "-p", "udp", "-m", "multiport", "--dports", "500,4500",
                        "-j", "DROP",
                    ],
                    capture_output=True, text=True, timeout=5,
                    check=True,
                )
            except subprocess.CalledProcessError as ipt_err:
                logger.warning(
                    f"_block_user: iptables DROP failed for "
                    f"{eap_identity}/{source_ip}: {ipt_err.stderr}"
                )
            except Exception as ipt_err:
                logger.warning(
                    f"_block_user: iptables DROP error for "
                    f"{eap_identity}/{source_ip}: {ipt_err}"
                )

        # Schedule revert (one Timer, reverts both layers atomically)
        threading.Timer(
            block_seconds,
            _unblock_user,
            args=(eap_identity, source_ip),
        ).start()
        audit(
            "disconnect_block",
            eap_identity=eap_identity,
            block_seconds=block_seconds,
            source_ip=source_ip or "none",
        )
        logger.info(
            f"_block_user: {eap_identity} blocked for {block_seconds}s"
            f"{f' (src IP {source_ip})' if source_ip else ''}"
        )
    except Exception as e:
        logger.warning(f"_block_user: failed to block {eap_identity}: {e}")


def _unblock_user(eap_identity: str, source_ip: str) -> None:
    """Revert the block: DELETE Auth-Type=Reject row + remove iptables DROP rule.

    Option 2 (TKT-030): direct pymysql + iptables subprocess with check=True.
    Mirrors _block_user's Option 2 pattern. Idempotent: each operation
    matches what _block_user created, so a partial-block revert is safe.

    Runs on a daemon thread via threading.Timer (one per block, started
    inside _block_user).
    """
    db_url = os.environ.get("DB_URL", "")
    db_user = db_pass = db_host = db_port = db_name = ""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(db_url)
        db_user = parsed.username or ""
        db_pass = parsed.password or ""
        db_host = parsed.hostname or "127.0.0.1"
        db_port = str(parsed.port or 3306)
        db_name = (parsed.path or "/radius").lstrip("/") or "radius"
    except Exception:
        pass

    try:
        import pymysql
        conn = pymysql.connect(
            host=db_host, port=int(db_port),
            user=db_user, password=db_pass,
            database=db_name, connect_timeout=5,
            autocommit=True,
        )
        try:
            with conn.cursor() as cur:
                # Layer 1 revert: DELETE the Auth-Type=Reject row
                cur.execute(
                    "DELETE FROM radcheck "
                    "WHERE username = %s AND attribute = %s AND value = %s",
                    (eap_identity, "Auth-Type", "Reject"),
                )
        finally:
            conn.close()

        # Layer 2 revert: remove iptables DROP rule
        if source_ip:
            try:
                subprocess.run(
                    [
                        "iptables", "-D", "INPUT",
                        "-s", source_ip,
                        "-p", "udp", "-m", "multiport", "--dports", "500,4500",
                        "-j", "DROP",
                    ],
                    capture_output=True, text=True, timeout=5,
                    check=True,
                )
            except subprocess.CalledProcessError as ipt_err:
                logger.warning(
                    f"_unblock_user: iptables -D failed for "
                    f"{eap_identity}/{source_ip}: {ipt_err.stderr}"
                )
            except Exception as ipt_err:
                logger.warning(
                    f"_unblock_user: iptables -D error for "
                    f"{eap_identity}/{source_ip}: {ipt_err}"
                )

        audit(
            "disconnect_unblock",
            eap_identity=eap_identity,
            source_ip=source_ip or "none",
        )
        logger.info(f"_unblock_user: {eap_identity} unblocked")
    except Exception as e:
        logger.error(f"_unblock_user: FAILED for {eap_identity}: {e}")

def _startup_recover_orphaned_blocks() -> None:
    """Clean up any orphaned Auth-Type=Reject rows left by a crashed bot.

    Called once at build_application() startup. In steady state, no
    Auth-Type=Reject rows should exist — they're removed by _unblock_user
    Timer after block_seconds. If any are found, they belong to a previous
    bot session that crashed mid-block — delete them so the user can auth.

    iptables rules are kernel-state and survive bot restart, so they
    are not cleaned here. (iptables rules inserted via iptables -I are
    lost on reboot anyway.)
    """
    try:
        import app
        rows = app.db_query(
            "SELECT username FROM radcheck "
            "WHERE attribute = 'Auth-Type' AND value = 'Reject'",
            (),
        )
        for row in rows:
            username = row["username"]
            app.db_exec(
                "DELETE FROM radcheck "
                "WHERE username = ? AND attribute = 'Auth-Type' "
                "AND value = 'Reject'",
                (username,),
            )
            audit(
                "disconnect_block_orphan_recovered",
                eap_identity=username,
            )
            logger.warning(
                f"startup_recover_orphaned_blocks: restored {username}"
            )
    except Exception as e:
        logger.error(
            f"startup_recover_orphaned_blocks: query failed: {e}"
        )



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


async def cmd_bake(update, context):
    """/bake <id|name> [device_name] - re-bake the Windows .ps1 installer for a customer.

    Mirrors what /create does at step 6/6 (TKT-BAKE block in ct_confirm), but
    standalone. Reads the existing Cleartext-Password from radcheck, calls
    build_installer.build_installer(mode="hostile"), sends the .ps1 as a
    Telegram document. Logs to audit.

    HOT-244: refuses is_operator=1 customers (hard, no override).
    """
    if not context.args:
        await update.message.reply_text("Usage: /bake <id|name> [device_name]")
        return
    audit("bake", arg=context.args)
    import app

    arg = context.args[0]
    device_filter = context.args[1] if len(context.args) > 1 else None

    # 1. Look up customer
    if arg.isdigit():
        rows = app.db_query("SELECT * FROM customers WHERE id = ?", (int(arg),))
    else:
        rows = app.db_query("SELECT * FROM customers WHERE name = ?", (arg,))
    if not rows:
        await update.message.reply_text(f"No customer `Zidaan`.")
        return
    cust = rows[0]
    cust_id = cust["id"]
    cust_name = cust["name"]

    # 2. HOT-244: refuse operator customers
    if cust.get("is_operator"):
        await update.message.reply_text(
            f"❌ REFUSED: customer `Zidaan` is an operator account (is_operator=1). Cannot bake installer for operator."
        )
        return

    # 3. Find device(s)
    if device_filter:
        devices = app.db_query(
            "SELECT * FROM devices WHERE customer_id = ? AND device_name = ?",
            (cust_id, device_filter)
        )
    else:
        devices = app.db_query("SELECT * FROM devices WHERE customer_id = ?", (cust_id,))
    if not devices:
        await update.message.reply_text(f"No devices found for customer `Zidaan`.")
        return
    if len(devices) > 1 and not device_filter:
        device_names = ", ".join(f"`{d['device_name']}`" for d in devices)
        await update.message.reply_text(
            f"Customer `Zidaan` has {len(devices)} devices ({device_names}). Specify which one: `/bake Zidaan <device_name>`"
        )
        return
    device = devices[0]
    dn = device["device_name"]

    # 4. Check device type
    if device.get("device_type") != "Windows":
        await update.message.reply_text(
            f"Device `{dn}` is type `{device.get('device_type')}`, not Windows. /bake is only for Windows customers."
        )
        return

    # 5. Get EAP identity from users table
    user_id = device.get("strongswan_user_id")
    if not user_id:
        await update.message.reply_text(f"Device `{dn}` has no user_id linked.")
        return
    u = app.db_query("SELECT name FROM users WHERE id = ?", (user_id,))
    if not u:
        await update.message.reply_text(f"users row missing for id={user_id}.")
        return
    eap_identity = u[0]["name"]

    # 6. Get EAP password from radcheck (Cleartext-Password)
    rc = app.db_query(
        "SELECT Value FROM radcheck WHERE UserName = ? AND Attribute = ?",
        (eap_identity, "Cleartext-Password")
    )
    if not rc:
        await update.message.reply_text(f"No Cleartext-Password in radcheck for `{eap_identity}`.")
        return
    eap_password = rc[0]["Value"]

    # 7. Bake and send
    try:
        import sys, os
        from io import BytesIO
        from telegram import InputFile
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scripts'))
        from build_installer import build as build_installer_script
        customer_dict = {
            "name": cust_name,
            "display_name": cust.get("display_name") or cust_name,
        }
        device_dict = {
            "device_name": dn,
            "device_type": "Windows",
        }
        bake_result = build_installer_script(
            customer=customer_dict,
            device=device_dict,
            mode="hostile",
            eap_password=eap_password,
        )
        bio = BytesIO(bake_result["content"].encode("utf-8"))
        bio.name = bake_result["filename"]
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=InputFile(bio, filename=bio.name),
            caption=(
                f"Baked Windows installer for `Zidaan` / `{dn}`. "
                f"Type H (manual entry, hostile networks). "
                f"Forward this file to the customer — they save it, "
                f"right-click -> 'Run with PowerShell' (Admin)."
            ),
        )
        audit(
            "bake_windows_installer",
            customer=cust_name,
            device=dn,
            filename=bake_result["filename"],
        )
    except Exception as bake_err:
        logger.exception("cmd_bake failed: %s", bake_err)
        await update.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⚠️ Bake FAILED for `Zidaan`: {bake_err}"
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
    parts = []
    for i, (name, _h, desc) in enumerate(BOT_COMMANDS):
        sep = "\n" if i and i % 3 == 0 else "  "
        parts.append(f"{sep}/{name} -- {desc}")
    await update.message.reply_text("".join(parts))


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
    from datetime import datetime, timezone, timedelta

    sessions = app.swanctl_parse_sas()
    lines = ["*Active Sessions*"]
    if not sessions:
        lines.append("_No active sessions._")
    else:
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
    # Inline keyboard: one [Disconnect] button per session row.
    # callback_data fits 64-byte Telegram limit ("disc:" + EAP identity).
    keyboard = [
        [InlineKeyboardButton(
            f"⛔ {s.get('remote_id') or '?'}",
            callback_data=f"disc:{s.get('remote_id') or '?'}",
        )]
        for s in sessions[:25]
    ]

    # NEW: Last 2 recent (closed) sessions, excluding currently-live users.
    SAST = timezone(timedelta(hours=2))
    live_usernames = [s.get("remote_id") for s in sessions if s.get("remote_id") and s.get("remote_id") != "?"]
    recent = app.recent_sessions(limit=2, exclude_usernames=live_usernames)
    if recent:
        lines.append("")
        lines.append("*Last 2 recent*")
        def fmt_ts(ts):
            if not ts:
                return "?"
            if isinstance(ts, str):
                ts = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            ts_utc = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
            return ts_utc.astimezone(SAST).strftime("%Y-%m-%d %H:%M")
        def fmt_dur(seconds):
            seconds = int(seconds or 0)
            if seconds < 60:
                return f"{seconds}s"
            m, s = divmod(seconds, 60)
            if m < 60:
                return f"{m}m {s}s"
            h, m = divmod(m, 60)
            return f"{h}h {m:02d}m"
        def fmt_bytes(n):
            n = int(n or 0)
            if n < 1024:
                return f"{n} B"
            if n < 1024**2:
                return f"{n/1024:.1f} KB"
            if n < 1024**3:
                return f"{n/1024**2:.1f} MB"
            return f"{n/1024**3:.2f} GB"
        for i, r in enumerate(recent, 1):
            rname = r.get("UserName") or "?"
            rip = r.get("FramedIPAddress") or "?"
            start = r.get("AcctStartTime")
            stop = r.get("AcctStopTime")
            dur = r.get("AcctSessionTime") or 0
            bi = r.get("AcctInputOctets") or 0
            bo = r.get("AcctOutputOctets") or 0
            lines.append(f"{i}. `{rname}` · `{rip}`")
            lines.append(f"   {fmt_ts(start)} → {fmt_ts(stop)} SAST ({fmt_dur(dur)})")
            lines.append(f"   ↓ {fmt_bytes(bi)} / {fmt_bytes(bo)}")
    else:
        lines.append("")
        lines.append("_No history yet._")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


async def cmd_customers(update, context):
    audit("customers")
    import app
    from datetime import datetime, timezone, timedelta
    SAST = timezone(timedelta(hours=2))

    rows = app.db_query(
        "SELECT id, name, display_name, is_active, created_at, last_session_at "
        "FROM customers WHERE COALESCE(is_operator, 0) = 0 ORDER BY id"
    )
    if not rows:
        await update.message.reply_text("No customers.")
        return

    def fmt_created(ts):
        if not ts:
            return "?"
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(SAST).strftime("%Y-%m-%d")

    def fmt_last_session(ts):
        if not ts:
            return "never"
        if isinstance(ts, str):
            ts = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        ts_utc = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        return ts_utc.astimezone(SAST).strftime("%Y-%m-%d %H:%M") + " SAST"

    lines = ["*Customers*", ""]
    for c in rows:
        archived = bool(c.get("is_archived"))
        active = bool(c.get("is_active"))
        if archived:
            flag = "Y"  # yellow archived
        elif active:
            flag = "G"  # green active
        else:
            flag = "R"  # red disabled
        name_disp = c.get("display_name") or ""
        lines.append(f"[{flag}] `{c['id']}` `{c['name']}` - {name_disp}")
        lines.append(f"Created: {fmt_created(c.get('created_at'))} · Last session: {fmt_last_session(c.get('last_session_at'))}")
        lines.append("------")
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
# /create -- 8-step ConversationHandler (interactive onboarding)
#   0 CLIENT_NAME -> 1 DURATION -> 2 CAP -> 3 SPEED -> 4 DEVICE_TYPE
#   -> 5 NOTES -> 6 CONFIRM -> 7 (terminal: send creds + setup URL)
# Refactored 2026-10-15 per Zun msg #44101-#44118.

# ConversationHandler state IDs
CLIENT_NAME, DURATION, CAP, SPEED, DEVICE_TYPE, NOTES, CONFIRM = range(7)

DEVICE_TYPES = ["Android", "iOS", "Windows", "Linux", "Mac"]
SPEED_PLAN_KEYS = ["lite", "standard", "pro"]
SPEED_PLAN_LABELS = {
    "lite":     "Lite 10/10",
    "standard": "Standard 20/20",
    "pro":      "Pro 40/20",
}
DURATIONS = [
    ("3d_demo", "3d demo"),
    ("10d",     "10d"),
    ("14d",     "14d"),
    ("21d",     "21d"),
    ("30d",     "30d"),
    ("40d",     "40d"),
]
CAPS = [
    ("10",    "10 GB"),
    ("20",    "20 GB"),
    ("uncap", "No cap · always tracked"),
]


def _kb(rows):
    return InlineKeyboardMarkup(rows)


async def _safe_edit_text(q, text, **kwargs):
    """q.edit_message_text() that logs+swallows BadRequest on stale callbacks.

    PTB 22.8 raises BadRequest('Message to edit not found') / 'Query is too
    old' when callback_query.message_id references a message that no longer
    exists. The edit is purely cosmetic -- losing it does NOT break the
    conversation state machine. Never recurse; always call the real API.
    """
    try:
        return await q.edit_message_text(text, **kwargs)
    except Exception as exc:
        logger.warning("_safe_edit_text suppressed: %s", exc)
        return None


def _tier_kb():
    """Legacy 6-step tier picker -- no longer called by /create but kept
    for back-compat with any external references.
    """
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
        if len(row) == 1:        # one tier per row -> one-column picker layout
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Cancel", callback_data="cx")])
    return _kb(rows)


# === 8-step /create flow keyboard builders (added 2026-10-15) ===

def _duration_kb():
    """1 row of 6 duration buttons + Cancel."""
    row = [InlineKeyboardButton(label, callback_data=f"cd:{key}") for key, label in DURATIONS]
    return _kb([row, [InlineKeyboardButton("Cancel", callback_data="cx")]])


def _cap_kb():
    """1 row of 3 cap buttons + Back/Cancel."""
    row = [InlineKeyboardButton(label, callback_data=f"cc:{key}") for key, label in CAPS]
    return _kb([row,
                [InlineKeyboardButton("\u2039 Back", callback_data="cb"),
                 InlineKeyboardButton("Cancel", callback_data="cx")]])


def _speed_kb():
    """1 row of 3 speed buttons + Back/Cancel."""
    row = [InlineKeyboardButton(SPEED_PLAN_LABELS[k], callback_data=f"cs:{k}") for k in SPEED_PLAN_KEYS]
    return _kb([row,
                [InlineKeyboardButton("\u2039 Back", callback_data="cb"),
                 InlineKeyboardButton("Cancel", callback_data="cx")]])


def _device_type_kb():
    """1 row of 5 device-type buttons + Back/Cancel."""
    row = [InlineKeyboardButton(dt, callback_data=f"cdt:{dt.lower()}") for dt in DEVICE_TYPES]
    return _kb([row,
                [InlineKeyboardButton("\u2039 Back", callback_data="cb"),
                 InlineKeyboardButton("Cancel", callback_data="cx")]])


def cap_label(cap_key: str) -> str:
    """Return the display label for a cap key (for summary text)."""
    return dict(CAPS).get(cap_key, cap_key)


async def create_start(update, context):
    """/create -- 8-step ConversationHandler entry."""
    # TKT-028 diagnostic: surface exactly where /create breaks
    logger.info(
        "create_start ENTERED chat_id=%s args=%r",
        update.effective_chat.id if update.effective_chat else None,
        context.args,
    )
    try:
        for k in ("ct_name", "ct_display", "ct_duration", "ct_cap", "ct_speed",
                  "ct_device_type", "ct_notes", "ct_expires_at", "ct_tier_name"):
            context.user_data.pop(k, None)
        audit("create_start", operator=update.effective_user.username if update.effective_user else None)
        await update.message.reply_text(
            "[1/8] *Client name?*\n"
            "Type the customer slug (lowercase, no spaces, e.g. `acme-corp`). "
            "Or /cancel."
        )
    except Exception as exc:
        logger.exception("create_start RAISED %s: %s", type(exc).__name__, exc)
        await update.message.reply_text(f"create_start failed: {exc}")
        return ConversationHandler.END
    return CLIENT_NAME


async def ct_client_name_entered(update, context):
    """[2/8] client name captured -> show duration picker."""
    import re as _re
    raw = (update.message.text or "").strip().lower()
    slug = _re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-")[:32] or "client"
    # Uniqueness check (per Zun msg #44105)
    import app
    existing = app.db_query("SELECT id FROM customers WHERE name = ?", (slug,))
    if existing:
        await update.message.reply_text(
            f"Customer `{slug}` already exists (id=`{existing[0]['id']}`). "
            "Pick a different name, or use /renew on the existing one."
        )
        return CLIENT_NAME  # stay in this state
    context.user_data["ct_name"] = slug
    context.user_data["ct_display"] = slug
    await update.message.reply_text(
        f"[2/8] *How long until expiry?*\nName: `{slug}`",
        reply_markup=_duration_kb(),
    )
    return DURATION


async def ct_duration_chosen(update, context):
    """[3/8] duration chosen -> show cap picker."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if q.data == "cx":
        await _safe_edit_text(q, "Cancelled.")
        return ConversationHandler.END
    if q.data == "cb":
        await _safe_edit_text(q, "[1/8] *Client name?*\nType the customer slug, or /cancel.")
        return CLIENT_NAME
    duration_key = q.data.split(":", 1)[1]
    context.user_data["ct_duration"] = duration_key
    await _safe_edit_text(
        q,
        f"[3/8] *Data cap?*\nDuration: `{duration_key}`",
        reply_markup=_cap_kb(),
    )
    return CAP


async def ct_cap_chosen(update, context):
    """[4/8] cap chosen -> show speed picker."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if q.data == "cx":
        await _safe_edit_text(q, "Cancelled.")
        return ConversationHandler.END
    if q.data == "cb":
        await _safe_edit_text(
            q,
            f"[2/8] *How long until expiry?*\nName: `{context.user_data.get('ct_name','')}`",
            reply_markup=_duration_kb(),
        )
        return DURATION
    cap_key = q.data.split(":", 1)[1]
    context.user_data["ct_cap"] = cap_key
    await _safe_edit_text(
        q,
        f"[4/8] *Speed plan?*\nDuration: `{context.user_data.get('ct_duration','')}` - Cap: `{cap_label(cap_key)}`",
        reply_markup=_speed_kb(),
    )
    return SPEED


async def ct_speed_chosen(update, context):
    """[5/8] speed chosen -> show device-type picker."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if q.data == "cx":
        await _safe_edit_text(q, "Cancelled.")
        return ConversationHandler.END
    if q.data == "cb":
        await _safe_edit_text(
            q,
            f"[3/8] *Data cap?*\nDuration: `{context.user_data.get('ct_duration','')}`",
            reply_markup=_cap_kb(),
        )
        return CAP
    speed_key = q.data.split(":", 1)[1]
    context.user_data["ct_speed"] = speed_key
    await _safe_edit_text(
        q,
        f"[5/8] *Device type?*\nSpeed: `{SPEED_PLAN_LABELS.get(speed_key, speed_key)}`",
        reply_markup=_device_type_kb(),
    )
    return DEVICE_TYPE


async def ct_device_type_chosen(update, context):
    """[6/8] device type chosen -> show notes prompt (or skip to confirm)."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if q.data == "cx":
        await _safe_edit_text(q, "Cancelled.")
        return ConversationHandler.END
    if q.data == "cb":
        await _safe_edit_text(
            q,
            f"[4/8] *Speed plan?*\nCap: `{cap_label(context.user_data.get('ct_cap','uncap'))}`",
            reply_markup=_speed_kb(),
        )
        return SPEED
    device_type = q.data.split(":", 1)[1]
    context.user_data["ct_device_type"] = device_type
    await _safe_edit_text(
        q,
        f"[6/8] *Notes? (optional)*\nDevice: `{device_type}`\n"
        "Type notes, or /skip.",
    )
    return NOTES


async def ct_notes_entered(update, context):
    """[7/8] notes captured (or /skip) -> show confirm summary."""
    if update.message and update.message.text:
        text_in = update.message.text.strip()
        if text_in.lower() == "/skip" or text_in == "":
            context.user_data["ct_notes"] = None
        else:
            context.user_data["ct_notes"] = text_in
    # Compute expiry date
    import datetime as _dt
    days_map = {"3d_demo": 3, "10d": 10, "14d": 14, "21d": 21, "30d": 30, "40d": 40}
    dur_key = context.user_data.get("ct_duration", "10d")
    days = days_map.get(dur_key, 10)
    expires_at = (_dt.datetime.utcnow() + _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    context.user_data["ct_expires_at"] = expires_at
    # Compute tier_name (matches DB convention: {N}day_{cap}, e.g. "14day_uncap").
    # Was previously f"{dur_key}_{cap_key}" which produced "14d_uncap" (short form, no "ay")
    # and failed create_client with 400 -- DB has "14day_uncap" (id=19) not "14d_uncap".
    cap_key = context.user_data.get("ct_cap", "uncap")
    tier_name = f"{days}day_{cap_key}"
    context.user_data["ct_tier_name"] = tier_name

    name = context.user_data["ct_name"]
    summary = (
        f"*Confirm?*\n\n"
        f"Name: `{name}`\n"
        f"Tier: `{tier_name}` ({cap_label(cap_key)})\n"
        f"Speed: `{SPEED_PLAN_LABELS.get(context.user_data.get('ct_speed','standard'), 'standard')}`\n"
        f"Device: `{context.user_data.get('ct_device_type','android')}`\n"
        f"Expires: `{expires_at}`\n"
        f"EAP id: `{name}-{context.user_data.get('ct_device_type','android').lower()}`"
    )
    rows = [[InlineKeyboardButton("Confirm", callback_data="cok"),
             InlineKeyboardButton("\u2039 Back", callback_data="cb"),
             InlineKeyboardButton("Cancel", callback_data="cx")]]
    await update.message.reply_text(summary, reply_markup=_kb(rows), parse_mode=ParseMode.MARKDOWN)
    return CONFIRM


async def ct_cancel(update, context):
    """/cancel pressed at any state. Exits the /create conversation."""
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def ct_confirm(update, context):
    """[8/8] confirm pressed -> call app.create_client + send creds + setup URL."""
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    if q.data == "cx":
        await _safe_edit_text(q, "Cancelled.")
        return ConversationHandler.END
    if q.data == "cb":
        await _safe_edit_text(q, "[6/8] *Notes?* (optional) Type notes, or /skip.")
        return NOTES

    name = context.user_data["ct_name"]
    tier = context.user_data["ct_tier_name"]
    cap = context.user_data["ct_cap"]
    speed = context.user_data["ct_speed"]
    device_type = context.user_data["ct_device_type"]
    notes = context.user_data.get("ct_notes")
    eap_identity = f"{name}-{device_type.lower()}"  # e.g. test-customer-android

    # Validate EAP identity uniqueness (app-layer check; UNIQUE constraint deferred per
    # the 2026-10-15 incident -- 5 multi-attribute rows for the same username are
    # correct auth data, not duplicates)
    import app
    existing_eap = app.db_query("SELECT id FROM radcheck WHERE username = ?", (eap_identity,))
    if existing_eap:
        await _safe_edit_text(
            q,
            f"EAP identity `{eap_identity}` already in use (radcheck id=`{existing_eap[0]['id']}`). "
            "Pick a different client name."
        )
        return CLIENT_NAME  # restart from step 1

    # Resolve bandwidth from speed plan
    from app import resolve_bandwidth
    try:
        down_mbps, up_mbps = resolve_bandwidth(speed, None, None)
    except Exception as e:
        await _safe_edit_text(q, f"Speed resolution failed: {e}")
        return ConversationHandler.END

    # Compute data_limit_bytes from cap
    cap_bytes_map = {"10": 10 * 1024**3, "20": 20 * 1024**3, "uncap": 0}
    data_limit_bytes = cap_bytes_map.get(cap, 0)

    try:
        from app import ClientCreate
        req = ClientCreate(
            name=name,
            display_name=context.user_data.get("ct_display", name),
            tier_name=tier,
            device_name=device_type.lower(),         # just the device part; API constructs EAP identity as f"{name}-{device_name}"
            device_type=device_type.upper(),         # store "ANDROID" / "IOS" / etc.
            speed_plan=speed,
            email=None,
            telegram_username=None,
            mac_address_1=None,
        )
        result = app.create_client(
            req, _user={"name": "bot", "role": "operator"}
        )
    except Exception as e:
        err_code = "CT_CONFIRM_DBM_FAIL"
        logger.exception("[%s] /create failed at ct_confirm: %s", err_code, e)
        await _safe_edit_text(q, f"Create failed [{err_code}]: {e}")
        return ConversationHandler.END

    cust = result.get("customer", {})
    eap_id = result.get("eap_identity")
    password = result.get("password")
    expires_at = context.user_data.get("ct_expires_at", "?")

    # Bake setup file (Windows gets a .ps1 via the TKT-BAKE path; other platforms
    # get a portal URL)
    if device_type.upper() == "WINDOWS":
        setup_url = "(bake failed -- see logs)"
        try:
            import sys, os
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath('/opt/vpn-portal/bot.py')), 'scripts'))
            from build_installer import build as build_installer_script
            customer_dict = {"name": name, "display_name": context.user_data.get("ct_display", name)}
            device_dict = {"device_name": eap_id, "device_type": "Windows"}
            setup_filename = f"setup-databyte-vpn-{eap_id}.ps1"
            setup_path = f"/opt/vpn-portal/www/static/baked/{setup_filename}"
            build_installer_script(customer=customer_dict, device=device_dict,
                                    out_path=setup_path, eap_identity=eap_id, password=password)
            setup_url = f"https://myvpn.databyte.co.za/static/baked/{setup_filename}"
        except Exception as bake_err:
            logger.warning("TKT-BAKE: failed to bake Windows installer: %s", bake_err)
    else:
        setup_url = f"https://vpn-portal.databyte.co.za/portal/devices/{cust.get('id')}"

    text = (
        f"OK Customer `{name}` created (id=`{cust.get('id')}`)\n"
        f"Tier: `{tier}`\n"
        f"Speed: `{SPEED_PLAN_LABELS.get(speed, speed)}` ({down_mbps}/{up_mbps} Mbps)\n"
        f"Device: `{device_type}`\n"
        f"Expires: `{expires_at}`\n\n"
        f"! *Self-destructs in 60s*\n\n"
        f"Server: `myvpn.databyte.co.za`\n"
        f"EAP identity: `{eap_id}`\n"
        f"Password: `{password}`\n"
        f"Setup: {setup_url}\n"
        f"CA cert: https://myvpn.databyte.co.za/certs/strongswan-ca.crt.pem"
    )
    msg = await _safe_edit_text(q, text, parse_mode=ParseMode.MARKDOWN)
    if msg is None:
        return ConversationHandler.END
    schedule_delete(context, update.effective_chat.id, msg.message_id, 60)

    # Clear draft state
    for k in ("ct_name", "ct_display", "ct_duration", "ct_cap", "ct_speed",
              "ct_device_type", "ct_notes", "ct_expires_at", "ct_tier_name"):
        context.user_data.pop(k, None)
    return ConversationHandler.END

async def cmd_renew(update, context):
    """/renew <customer_name> <new_tier> -- renew a customer's tier without changing the EAP identity.

    The customer keeps the same EAP username + password. The radcheck entry is
    un-disabled (DISABLED- prefix stripped), the tier is updated, the expiry
    is reset, and the customer's existing device config keeps working.
    (Zun msg #44105, #44118.)
    """
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /renew <customer_name> <new_tier>\n"
            "Example: /renew test-customer 30day_20"
        )
        return

    name = context.args[0].lower()
    new_tier = context.args[1]

    import app
    cust_rows = app.db_query(
        "SELECT id, tier_id, is_operator, status FROM customers WHERE name = ?", (name,),
    )
    if not cust_rows:
        await update.message.reply_text(f"Customer `{name}` not found.")
        return
    cust = cust_rows[0]
    if cust.get("is_operator"):
        await update.message.reply_text("Cannot renew an operator account.")
        return

    tier_rows = app.db_query(
        "SELECT id, name, duration_days FROM tiers WHERE name = ? AND is_active = 1",
        (new_tier,),
    )
    if not tier_rows:
        valid = app.db_query("SELECT name FROM tiers WHERE is_active = 1 ORDER BY name")
        await update.message.reply_text(
            f"Tier `{new_tier}` not found. Active tiers: "
            f"{', '.join(t['name'] for t in valid)}"
        )
        return
    new_tier_row = tier_rows[0]
    old_tier_id = cust.get("tier_id")
    old_tier = app.db_query("SELECT name FROM tiers WHERE id = ?", (old_tier_id,)) if old_tier_id else None

    device_rows = app.db_query(
        "SELECT device_type FROM devices WHERE customer_id = ? AND is_active = 1 LIMIT 1",
        (cust["id"],),
    )
    if not device_rows:
        await update.message.reply_text(
            f"Customer `{name}` has no active device record -- cannot determine EAP identity."
        )
        return
    device_type = device_rows[0]["device_type"].lower()
    eap_identity = f"{name}-{device_type}"

    import datetime as _dt
    now_ts = int(_dt.datetime.utcnow().timestamp())
    days = new_tier_row["duration_days"] or 30
    expires_at = (_dt.datetime.utcnow() + _dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    app.db_exec(
        "UPDATE customers SET tier_id = ?, created_at = ?, expires_at = ?, "
        "updated_at = ?, status = 'active' WHERE id = ?",
        (new_tier_row["id"], now_ts, expires_at, now_ts, cust["id"]),
    )

    radcheck_rows = app.db_query(
        "SELECT id, username FROM radcheck WHERE username = ? OR username LIKE ?",
        (eap_identity, f"DISABLED-%{eap_identity}"),
    )
    for r in radcheck_rows:
        old_username = r["username"]
        if old_username.startswith("DISABLED-"):
            app.db_exec("UPDATE radcheck SET username = ? WHERE id = ?", (eap_identity, r["id"]))
            logger.info("renew: un-disabled radcheck id=%s (%s -> %s)", r["id"], old_username, eap_identity)

    old_tier_name = old_tier[0]["name"] if old_tier else "(none)"
    audit("renew", customer_id=cust["id"],
          old_tier=old_tier_name, new_tier=new_tier,
          operator=update.effective_user.username if update.effective_user else None)

    await update.message.reply_text(
        f"OK *Renewed*\n\n"
        f"Customer: `{name}` (id=`{cust['id']}`)\n"
        f"Tier: `{old_tier_name}` -> `{new_tier}`\n"
        f"Expires: `{expires_at[:10]}`\n"
        f"EAP identity: `{eap_identity}` (unchanged -- customer keeps connecting)"
    )


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


def disconnect_via_coa(eap_identity: str, block_seconds: int = 0) -> tuple[bool, str]:
    """Send a Disconnect-Request to charon's DAE plugin (UDP 127.0.0.1:3799).

    Shared between /disconnect (typed) and cb_disconnect (inline button).

    Architecture (verified 2026-09-22 13:36 UTC):
    - charon (NOT FreeRADIUS) owns UDP 3799. FreeRADIUS has NO `coa`
      virtual server enabled; sites-enabled/ has only `default` +
      `inner-tunnel`. So CoA/Disconnect packets hit charon's DAE plugin
      directly, with secret `b305c63a5010d2c309e29df7bab0fe66`
      (stored in /root/.strongswan-dae-secret, mounted into the
      strongswan container via 10-eap-radius.conf).
    - charon's DAE plugin (RFC 5176) matches Disconnect-Request
      against active IKE_SAs by EITHER Acct-Session-Id OR
      Framed-IP-Address. User-Name alone does NOT match (charon
      doesn't keep a User-Name -> IKE_SA index). Calling-Station-Id
      also doesn't match. This is why the v2.4.0/v2.4.1 bot got
      CoA-NAK: it sent User-Name only.
    - Command must be "disconnect" (RFC 5176 Disconnect-Request,
      code 40), NOT "coa" (CoA-Request, code 43). charon's DAE
      just NAKs CoA-Request without processing it. quota-monitor.py
      uses the same Disconnect-Request + Acct-Session-Id pattern.
    - If block_seconds > 0, ALSO inserts an Auth-Type=Reject row in
      radcheck (immediate Access-Reject at FreeRADIUS layer) and adds
      an iptables DROP rule for the source IP (blocks UDP 500/4500 at
      network layer). Both layers revert via threading.Timer after
      block_seconds. See _block_user docstring.

    Sync subprocess blocks the event loop briefly (~10s max) — acceptable
    for an admin tool with infrequent disconnects.
    """
    if not eap_identity or eap_identity == "?":
        return False, "Invalid EAP identity (empty or '?'). Button data malformed."
    secret = "b305c63a5010d2c309e29df7bab0fe66"
    calling_station_id = ""

    # Build attributes for charon DAE matching. Start with User-Name
    # (charon uses it for the audit log entry, even if it doesn't match
    # the IKE_SA). Then look up Acct-Session-Id + Framed-IP-Address +
    # Calling-Station-Id from radacct where the session is still active
    # (acctstoptime IS NULL). Calling-Station-Id is used by the block
    # layer (iptables DROP) but NOT by charon DAE (which only matches
    # on Acct-Session-Id OR Framed-IP-Address).
    attrs = [f"User-Name={eap_identity}"]
    try:
        import app
        rows = app.db_query(
            "SELECT acctsessionid, framedipaddress, callingstationid FROM radacct "
            "WHERE username = %s AND acctstoptime IS NULL "
            "ORDER BY acctstarttime DESC LIMIT 1",
            (eap_identity,),
        )
        if rows:
            row = rows[0]
            if row.get("acctsessionid"):
                attrs.append(f"Acct-Session-Id={row['acctsessionid']}")
            if row.get("framedipaddress"):
                attrs.append(f"Framed-IP-Address={row['framedipaddress']}")
            calling_station_id = row.get("callingstationid", "") or ""
            logger.info(
                f"disconnect_via_coa: {eap_identity} -> "
                f"session_id={row.get('acctsessionid')!r} "
                f"vip={row.get('framedipaddress')!r} "
                f"src_ip={calling_station_id!r}"
            )
        else:
            logger.warning(f"disconnect_via_coa: no active radacct row for {eap_identity!r} (will NAK)")
    except Exception as e:
        logger.warning(f"disconnect_via_coa: radacct lookup failed: {e}")

    cmd = ["/usr/bin/sudo", "/usr/bin/radclient", "127.0.0.1:3799", "disconnect", secret]
    try:
        res = subprocess.run(
            cmd,
            input="\n".join(attrs) + "\n",
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0 and block_seconds > 0:
            # Two-layer block: Auth-Type=Reject (RADIUS) + iptables DROP
            # (network). Either alone would defeat Android's auto-reconnect;
            # both together is bulletproof. See _block_user docstring.
            try:
                _block_user(eap_identity, calling_station_id, block_seconds)
                src_note = (
                    f" (src IP {calling_station_id})"
                    if calling_station_id else ""
                )
                return True, (
                    f"Disconnect sent + user blocked for {block_seconds}s"
                    f"{src_note} (Auth-Type=Reject + iptables DROP, "
                    f"both revert in {block_seconds}s)."
                )
            except Exception as e:
                logger.warning(
                    f"disconnect_via_coa: failed to block "
                    f"{eap_identity} after disconnect: {e}"
                )
                # Fall through — disconnect itself succeeded, only
                # the block didn't apply.

        if res.returncode == 0:
            return True, f"Disconnect sent for `{eap_identity}`."
        # Non-zero exit. Capture stderr (preferred) or stdout for the cause.
        # Disconnect-NAK (no matching IKE_SA, etc) shows up here.
        out = (res.stderr or res.stdout or "<empty>").strip()[:500]
        return False, f"Disconnect `{eap_identity}` failed (rc={res.returncode}):\n`{out}`"
    except subprocess.TimeoutExpired:
        return False, "Disconnect timed out after 10s."
    except Exception as e:
        return False, f"Disconnect error: {e}"


async def cmd_disconnect(update, context):
    """Disconnect an active customer session via CoA (typed command)."""
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
    ok, msg = disconnect_via_coa(eap_identity, block_seconds=DISCONNECT_BLOCK_SECONDS)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cb_disconnect(update, context):
    """Handle [Disconnect] button press on /sessions inline keyboard.

    callback_data format: "disc:<eap_identity>".
    """
    query = update.callback_query
    await query.answer("Disconnecting…")  # dismisses Telegram loading spinner
    parts = query.data.split(":", 1)
    eap_identity = parts[1] if len(parts) > 1 else ""
    audit("disconnect_inline", eap_identity=eap_identity)
    ok, msg = disconnect_via_coa(eap_identity, block_seconds=DISCONNECT_BLOCK_SECONDS)
    # Edit the original message so the button disappears after action.
    await _safe_edit_text(query, msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_logs(update, context):
    if context.args:
        await update.message.reply_text("Usage: /logs")
        return
    audit("logs")
    import app
    from datetime import datetime, timezone, timedelta
    SAST = timezone(timedelta(hours=2))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).replace(tzinfo=None)

    auths = app.db_query(
        "SELECT authdate, username, reply "
        "FROM radpostauth "
        "WHERE authdate >= ? "
        "ORDER BY authdate DESC LIMIT 30",
        (cutoff,),
    )
    if not auths:
        await update.message.reply_text("No auth events in the last 7 days.")
        return
    lines = [f"*Recent Logs* (last {len(auths)}, last 7d)", ""]
    for r in auths:
        ad = r.get("authdate")
        if isinstance(ad, str):
            ad = datetime.strptime(ad, "%Y-%m-%d %H:%M:%S")
        if ad.tzinfo is None:
            ad = ad.replace(tzinfo=timezone.utc)
        ad_sast = ad.astimezone(SAST).strftime("%Y-%m-%d %H:%M")
        reply = r.get("reply") or "?"
        icon = "\u2705" if "Accept" in reply else "\u274c"
        user = r.get("username") or "?"
        lines.append(f"{ad_sast} SAST \u00b7 {icon} `{user}` \u00b7 {reply}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)



async def cmd_orphans(update, context):
    """Report sync drift between radcheck / users / devices / customers."""
    audit("orphans")
    import app
    from datetime import datetime, timezone, timedelta
    SAST = timezone(timedelta(hours=2))

    chain = app.db_query("""
        SELECT r.UserName AS radcheck, u.id AS users_id, d.id AS devices_id,
               c.id AS customers_id, c.name AS customers_name
        FROM radcheck r
        LEFT JOIN users u ON u.name COLLATE utf8mb4_unicode_ci = r.UserName COLLATE utf8mb4_unicode_ci
        LEFT JOIN devices d ON d.strongswan_user_id = u.id
        LEFT JOIN customers c ON c.id = d.customer_id
        WHERE r.Attribute = 'Cleartext-Password' AND r.Value NOT LIKE 'DISABLED-%'
        ORDER BY r.UserName
    """)
    broken = [r for r in chain if not (r['users_id'] and r['devices_id'] and r['customers_id'])]

    users_no_device = app.db_query("""
        SELECT u.id, u.name FROM users u
        LEFT JOIN devices d ON d.strongswan_user_id = u.id
        WHERE d.id IS NULL ORDER BY u.id
    """)
    broken_devices = app.db_query("""
        SELECT d.id, d.strongswan_user_id, d.customer_id, d.device_name
        FROM devices d LEFT JOIN customers c ON c.id = d.customer_id
        WHERE c.id IS NULL ORDER BY d.id
    """)
    customers_no_devices = app.db_query("""
        SELECT c.id, c.name FROM customers c
        WHERE c.is_operator = 0
          AND NOT EXISTS (SELECT 1 FROM devices d WHERE d.customer_id = c.id)
        ORDER BY c.id
    """)
    users_no_radcheck = app.db_query("""
        SELECT u.id, u.name FROM users u
        LEFT JOIN radcheck r ON r.UserName COLLATE utf8mb4_unicode_ci = u.name COLLATE utf8mb4_unicode_ci
            AND r.Attribute = 'Cleartext-Password'
        WHERE r.UserName IS NULL ORDER BY u.id
    """)

    lines = ["*Orphan audit*", ""]
    if broken:
        lines.append(f"*Chain broken ({len(broken)}):*")
        for r in broken:
            status = []
            if not r['users_id']: status.append("NO users")
            if not r['devices_id']: status.append("NO device")
            if not r['customers_id']: status.append("NO customer")
            lines.append(f"- `{r['radcheck']}` -> {', '.join(status)}")
        lines.append("")
    else:
        lines.append(f"*radcheck chain:* all {len(chain)} entries fully linked \u2705")
        lines.append("")

    if users_no_device:
        lines.append("*users with no device:*")
        for r in users_no_device:
            lines.append(f"- users.id={r['id']} `{r['name']}`")
        lines.append("")
    if broken_devices:
        lines.append("*devices with phantom customer:*")
        for r in broken_devices:
            lines.append(f"- device.id={r['id']} (`{r['device_name']}`) -> customer_id={r['customer_id']} (DELETED)")
        lines.append("")
    if customers_no_devices:
        lines.append("*customers with no devices (potential dead):*")
        for r in customers_no_devices:
            lines.append(f"- customer.id={r['id']} `{r['name']}`")
        lines.append("")

    if users_no_radcheck:
        lines.append("*users with no radcheck (orphan, no EAP identity):*")
        for r in users_no_radcheck:
            lines.append(f"- users.id={r['id']} `{r['name']}`")
        lines.append("")

    if not (broken or users_no_device or broken_devices or customers_no_devices or users_no_radcheck):
        lines.append("\u2705 No drift detected.")
        lines.append("")
    lines.append(f"Run at {datetime.now(SAST).strftime('%Y-%m-%d %H:%M')} SAST")
    text = "\n".join(lines)
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        # Markdown parse error (e.g., unclosed entity) -- fall back to plain text
        # so the audit still reaches Zun instead of dumping a traceback to journal
        logger.warning(f"cmd_orphans markdown parse failed, falling back to plain text: {e}")
        plain = text.replace("*", "").replace("_", "").replace("`", "'")
        await update.message.reply_text(plain)


async def cmd_deleteuser(update, context):
    """Delete a customer + sweep ALL associated auth.

    Sweeps (per 'sweep everything on a client delete'):
      - radcheck: bare-name DISABLED- sentinel + every <customer>-<device> row
      - users: every device-suffixed name linked via devices.strongswan_user_id
      - devices: every device where customer_id = X
      - EAP blocks: bare-name (if present) + every device-suffixed name
      - customers: the customer row itself

    HOT-244: refuses is_operator=1 customers (hard, no override).
    Requires 'force' flag if customer has active sessions.
    """
    if not context.args:
        await update.message.reply_text("Usage: `/deleteuser <id|name> [force]`", parse_mode=ParseMode.MARKDOWN)
        return
    audit("deleteuser", arg=context.args)
    import app
    import re

    arg = context.args[0]
    force = len(context.args) > 1 and context.args[1].lower() == "force"

    # 1. Look up customer
    if arg.isdigit():
        rows = app.db_query("SELECT * FROM customers WHERE id = ?", (int(arg),))
    else:
        rows = app.db_query("SELECT * FROM customers WHERE name = ?", (arg,))
    if not rows:
        await update.message.reply_text(f"No customer `{arg}`.")
        return
    c = rows[0]
    bare_name = c['name']

    # 2. HOT-244: refuse operator customers -- hard, no override
    if c.get("is_operator"):
        await update.message.reply_text(
            f"\u274c REFUSED: customer `{c['name']}` is an operator account (is_operator=1). "
            f"Operator customers cannot be deleted via this command. "
            f"Use the portal admin UI for operator changes."
        )
        return

    # 3. Active sessions check
    active = app.db_query(
        "SELECT COUNT(*) AS n FROM radacct WHERE UserName IN ("
        "  SELECT u.name FROM users u INNER JOIN devices d ON d.strongswan_user_id = u.id "
        "  WHERE d.customer_id = ?) AND AcctStopTime IS NULL",
        (c['id'],)
    )
    active_n = active[0]['n'] if active else 0
    if active_n > 0 and not force:
        await update.message.reply_text(
            f"\u26a0\ufe0f Customer `{c['name']}` has {active_n} active session(s). "
            f"Disconnect first with `/disconnect {c['name']}` or run "
            f"`/deleteuser {c['name']} force` to delete anyway."
        )
        return

    # 4. Collect ALL usernames to sweep (bare + every device-suffixed)
    radcheck_names = app.db_query(
        "SELECT DISTINCT UserName FROM radcheck "
        "WHERE UserName = ? OR UserName LIKE ?",
        (bare_name, bare_name + '-%')
    )
    usernames = set(r['UserName'] for r in radcheck_names)
    user_rows = app.db_query(
        "SELECT u.id, u.name FROM users u "
        "INNER JOIN devices d ON d.strongswan_user_id = u.id WHERE d.customer_id = ?",
        (c['id'],)
    )
    # Fix 2026-10-17 (HOT-273 follow-up): also include orphan users (no device) whose
    # name matches the customer's name pattern. The INNER JOIN above misses them,
    # leaving "doubly-orphaned" users (radcheck wiped but users row survives) after
    # /deleteuser. Same gap exists in delete_customer (HTTP, app.py:2882) and
    # bulk_action.py (LXC 903, scripts/bulk_action.py:226).
    orphan_user_rows = app.db_query(
        "SELECT u.id, u.name FROM users u "
        "WHERE NOT EXISTS (SELECT 1 FROM devices d WHERE d.strongswan_user_id = u.id) "
        "AND (u.name = ? OR u.name LIKE ?)",
        (bare_name, bare_name + '-%')
    )
    user_rows.extend(orphan_user_rows)
    for u in user_rows:
        usernames.add(u['name'])
    usernames.add(bare_name)

    device_rows = app.db_query("SELECT id FROM devices WHERE customer_id = ?", (c['id'],))

    # 5. Transactional DB sweep
    try:
        with app.portal_auth._engine().begin() as conn:
            wrapped = app.portal_auth._Conn(conn)
            for un in usernames:
                wrapped.execute("DELETE FROM radcheck WHERE UserName = ?", (un,))
            for u in user_rows:
                wrapped.execute("DELETE FROM users WHERE id = ?", (u['id'],))
            for d in device_rows:
                wrapped.execute("DELETE FROM devices WHERE id = ?", (d['id'],))
            wrapped.execute("DELETE FROM customers WHERE id = ?", (c['id'],))
    except Exception as e:
        await update.message.reply_text(f"\u274c Delete failed: {e}")
        return

    # 6. Remove ALL EAP blocks (binary-mode atomic write pattern)
    if usernames:
        conf = app.read_rw_eap_conf()
        for un in usernames:
            pat = re.compile(rf"\n?\s*eap-{re.escape(un)}\s*\{{[^{{}}]*?\}}\n?", re.DOTALL)
            conf = pat.sub('', conf, count=1)
        if 'secrets {' in conf and 'connections {' in conf and conf.rstrip().endswith('}'):
            app.write_rw_eap_conf(conf)

    # 7. Reload charon
    app.reload_charon_creds()

    # 8. Report
    sorted_names = sorted(usernames)
    lines = [
        f"*Customer `{c['name']}` deleted (full sweep).*",
        f"- id: {c['id']}",
        f"- radcheck rows removed: {len(usernames)} ({', '.join(f'`{n}`' for n in sorted_names)})",
        f"- users rows removed: {len(user_rows)}",
        f"- devices rows removed: {len(device_rows)}",
        f"- EAP blocks removed: up to {len(usernames)} (only present ones actually deleted)",
        "- charon reloaded",
    ]
    if active_n > 0:
        lines.append(f"_Note: {active_n} active session(s) were force-terminated by 'force'._")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)




async def cmd_cleanup_orphans(update, context):
    """Retire orphan users (users with no device) by explicit user IDs.

    Refuses:
      - Users that have a device (not orphans) -- use /deleteuser
      - HOT-244 operator accounts (zun-*, zunaid-*)

    Usage:
      /cleanup_orphans <user_id> [<user_id> ...]
      /cleanup_orphans all          # retire all current users_no_device (warning)
      /cleanup_orphans all force    # same, skip the warning

    Pattern: same DELETE-in-transaction as /tmp/nuke_orphans.py, but bot-driven.
    Whitelist-only (chat_id 7748884597).
    """
    if not context.args:
        await update.message.reply_text(
            "Usage: `/cleanup_orphans <user_id> [<user_id> ...]`\n"
            "       `/cleanup_orphans all` (retire all current users_no_device, requires `force`)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    audit("cleanup_orphans", args=list(context.args))
    import app

    args_lower = [a.lower() for a in context.args]
    force = "force" in args_lower
    is_all = "all" in args_lower

    if is_all:
        rows = app.db_query("""
            SELECT u.id, u.name FROM users u
            LEFT JOIN devices d ON d.strongswan_user_id = u.id
            WHERE d.id IS NULL ORDER BY u.id
        """)
        if not rows:
            await update.message.reply_text("*No orphans. Nothing to clean.*", parse_mode=ParseMode.MARKDOWN)
            return
        if not force:
            ids_str = ", ".join(f"`{r['id']}`" for r in rows)
            await update.message.reply_text(
                f"\u26a0\ufe0f About to retire {len(rows)} orphan user(s): {ids_str}\n"
                f"Pass `force` to confirm: `/cleanup_orphans all force`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        ids = [r['id'] for r in rows]
        names = [r['name'] for r in rows]
    else:
        try:
            ids = [int(a) for a in context.args if a.lower() != "force"]
        except ValueError:
            await update.message.reply_text("\u274c Invalid: user IDs must be integers.", parse_mode=ParseMode.MARKDOWN)
            return
        if not ids:
            await update.message.reply_text("\u274c No user IDs given.", parse_mode=ParseMode.MARKDOWN)
            return
        ph = ",".join(["?"] * len(ids))
        rows = app.db_query(f"SELECT id, name FROM users WHERE id IN ({ph})", ids)
        if not rows:
            await update.message.reply_text(f"\u274c No users found for ids: {ids}", parse_mode=ParseMode.MARKDOWN)
            return
        found_ids = set(r['id'] for r in rows)
        missing = [i for i in ids if i not in found_ids]
        if missing:
            await update.message.reply_text(f"\u274c Users not found: {missing}", parse_mode=ParseMode.MARKDOWN)
            return
        names = [r['name'] for r in rows]

    # Pre-check: not orphans (have device) -- refuse
    ph = ",".join(["?"] * len(ids))
    non_orphans = app.db_query(f"""
        SELECT u.id, u.name FROM users u
        INNER JOIN devices d ON d.strongswan_user_id = u.id
        WHERE u.id IN ({ph})
    """, ids)
    if non_orphans:
        bad = ", ".join(f"`{r['name']}`" for r in non_orphans)
        await update.message.reply_text(
            f"\u274c REFUSED: these have devices (not orphans): {bad}\n"
            f"Use `/deleteuser` to remove customers with devices.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Pre-check: HOT-244 operators
    op_rows = app.db_query(f"""
        SELECT u.id, u.name FROM users u
        INNER JOIN customers c ON c.user_id = u.id
        WHERE u.id IN ({ph}) AND c.is_operator = 1
    """, ids)
    if op_rows:
        bad = ", ".join(f"`{r['name']}`" for r in op_rows)
        await update.message.reply_text(
            f"\u274c REFUSED: these are operator accounts (HOT-244): {bad}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Execute DELETE in transaction
    try:
        with app.portal_auth._engine().begin() as conn:
            wrapped = app.portal_auth._Conn(conn)
            ph_n = ",".join(["?"] * len(names))
            r1 = wrapped.execute(f"DELETE FROM radcheck WHERE username IN ({ph_n})", names)
            r2 = wrapped.execute(f"DELETE FROM users WHERE id IN ({ph})", ids)
        audit(
            "cleanup_orphans_done",
            ids=ids, names=names,
            radcheck_deleted=r1.rowcount, users_deleted=r2.rowcount,
        )
        await update.message.reply_text(
            f"\u2705 Cleaned {len(names)} orphan user(s):\n"
            f"- radcheck rows deleted: {r1.rowcount}\n"
            f"- users rows deleted: {r2.rowcount}\n"
            f"- names: {', '.join(f'`{n}`' for n in names)}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await update.message.reply_text(f"\u274c Cleanup failed (rolled back): {e}", parse_mode=ParseMode.MARKDOWN)


async def cmd_renew_orphan(update, context):
    """Restore an orphan user to a working customer.

    The orphan's users.password BLOB (NTLM hash) is preserved; a fresh
    Cleartext-Password is generated (one-way hash, the original CT is not
    recoverable). The new password is shown once in the rendered output
    (same format as /create), self-destructs in 60s.

    Refuses:
      - HOT-244 operators (zun-*, zunaid-*)
      - Non-orphan users (already have a device) -- use /renew
      - Missing or archived tier
      - Customer name collision (cannot renew into a taken name)
      - EAP identity without `-` (cannot parse customer/device names)

    Usage:
      /renew_orphan <eap_identity> <tier_name>
    """
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/renew_orphan <eap_identity> <tier_name>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    audit("renew_orphan", args=list(context.args))
    import app
    from datetime import datetime, timedelta

    eap_identity = context.args[0]
    tier_name = context.args[1]

    # HOT-244 prefix check
    if eap_identity.lower().startswith(("zun", "zunaid")):
        await update.message.reply_text(
            f"\u274c REFUSED: `{eap_identity}` matches an operator prefix (HOT-244).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Pre-check: orphan must exist in users table (no device)
    user = app.db_query("SELECT id, name, password FROM users WHERE name = ?", (eap_identity,))
    if not user:
        await update.message.reply_text(
            f"\u274c No users row for `{eap_identity}`. Run `/orphans` to see the audit.\n"
            f"(If already nuked, restore from `/var/backups/orphan-nuke-*.sql` first.)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    user_row = user[0]
    dev = app.db_query("SELECT id FROM devices WHERE strongswan_user_id = ?", (user_row['id'],))
    if dev:
        await update.message.reply_text(
            f"\u274c `{eap_identity}` already has a device (not an orphan). Use `/renew` to renew the customer.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Parse customer + device name
    if "-" not in eap_identity:
        await update.message.reply_text(
            f"\u274c EAP identity `{eap_identity}` has no `-`. Cannot parse customer/device names.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    cust_name, device_name = eap_identity.split("-", 1)

    # Verify tier
    tier = app.db_query("SELECT id, duration_days, is_active FROM tiers WHERE name = ?", (tier_name,))
    if not tier:
        await update.message.reply_text(f"\u274c Tier `{tier_name}` does not exist.", parse_mode=ParseMode.MARKDOWN)
        return
    if not tier[0].get("is_active"):
        await update.message.reply_text(f"\u274c Tier `{tier_name}` is archived.", parse_mode=ParseMode.MARKDOWN)
        return
    tier_id = tier[0]["id"]
    duration_days = tier[0].get("duration_days")

    # Customer name collision check
    if app.db_query("SELECT id FROM customers WHERE name = ?", (cust_name,)):
        await update.message.reply_text(
            f"\u274c Customer `{cust_name}` already exists. Cannot renew an orphan into a taken name.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Generate fresh password
    password = app.secrets.token_urlsafe(16)
    ntlm = app.ntlm_hash_bytes(password)
    now = int(app.time.time())
    expires_at = None
    if duration_days is not None:
        expires_at = (datetime.utcnow() + timedelta(days=int(duration_days))).strftime("%Y-%m-%d %H:%M:%S")

    # Execute recreation in transaction
    try:
        with app.portal_auth._engine().begin() as conn:
            wrapped = app.portal_auth._Conn(conn)
            wrapped.execute(
                "INSERT INTO customers (name, display_name, is_operator, is_active, "
                "data_used_bytes, expires_at, tier_id, status, max_devices, "
                "bandwidth_down_mbps, bandwidth_up_mbps, created_at, updated_at, notes, email) "
                "VALUES (?, ?, 0, 1, 0, ?, ?, 'active', 1, 20, 20, ?, ?, '', '')",
                (cust_name, cust_name, expires_at, tier_id, now, now),
            )
            cust_id = wrapped.execute("SELECT id FROM customers WHERE name = ?", (cust_name,)).fetchone()["id"]
            wrapped.execute("DELETE FROM users WHERE id = ?", (user_row['id'],))
            wrapped.execute(
                "INSERT INTO users (name, password) VALUES (?, UNHEX(?))",
                (eap_identity, ntlm.hex().upper()),
            )
            user_id = wrapped.execute("SELECT id FROM users WHERE name = ?", (eap_identity,)).fetchone()["id"]
            wrapped.execute(
                "INSERT INTO devices (customer_id, strongswan_user_id, device_name, is_active, "
                "created_at, updated_at, device_type) "
                "VALUES (?, ?, ?, 1, ?, ?, 'OTHER')",
                (int(cust_id), int(user_id), device_name, now, now),
            )
            wrapped.execute("UPDATE customers SET user_id = ? WHERE id = ?", (int(user_id), int(cust_id)))
            wrapped.execute("DELETE FROM radcheck WHERE username = ?", (eap_identity,))
            wrapped.execute(
                "INSERT INTO radcheck (username, attribute, op, value) VALUES "
                "(?, 'Cleartext-Password', ':=', ?)",
                (eap_identity, password),
            )
            wrapped.execute(
                "INSERT INTO radcheck (username, attribute, op, value) VALUES "
                "(?, 'NT-Password', ':=', ?)",
                (eap_identity, ntlm.hex().upper()),
            )
            wrapped.execute(
                "INSERT INTO radusergroup (username, groupname, priority) VALUES (?, 'default', 1)",
                (eap_identity,),
            )

        # EAP block pre-clean + write + charon reload (side effects, post-tx)
        try:
            import re as _re
            conf = app.read_rw_eap_conf()
            block_pat = _re.compile(
                rf"\n?\s*eap-{_re.escape(eap_identity)}\s*\{{[^{{}}]*?\}}\n?",
                _re.DOTALL,
            )
            conf = block_pat.sub("", conf, count=1)
            app.write_rw_eap_conf(conf)
        except Exception as ex:
            logger.warning(f"renew_orphan: pre-clean eap block failed for {eap_identity}: {ex}")
        app.append_eap_block(eap_identity, password)
        app.reload_charon_creds()

        audit("renew_orphan_done", eap_identity=eap_identity, cust_id=cust_id, tier_name=tier_name)

        text = (
            f"OK Orphan `{eap_identity}` renewed (customer.id=`{cust_id}`)\n"
            f"Tier: `{tier_name}`\n"
            f"Device: `{device_name}`\n"
            f"Expires: `{expires_at or '?'}`\n\n"
            f"! *Self-destructs in 60s*\n\n"
            f"Server: `myvpn.databyte.co.za`\n"
            f"EAP identity: `{eap_identity}`\n"
            f"Password: `{password}`\n"
            f"CA cert: https://myvpn.databyte.co.za/certs/strongswan-ca.crt.pem"
        )
        msg = await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        if msg is not None:
            schedule_delete(context, update.effective_chat.id, msg.message_id, 60)

    except Exception as e:
        await update.message.reply_text(
            f"\u274c Renew orphan failed: {e}", parse_mode=ParseMode.MARKDOWN,
        )


# ---------- Bot Commands (single source of truth) ----------
#
# One list drives BOTH the CommandHandler registration (in build_application)
# and the BotFather menu push (in _post_init_register_commands). To add a
# new /command: add one row here, restart the bot, done. Don't register
# commands anywhere else -- keep this list the only registration table.
BOT_COMMANDS = [
    ("start",      cmd_start,      "Restart / show welcome"),
    ("help",       cmd_help,       "Show all commands"),
    ("status",     cmd_status,     "Live gateway / SA / sessions summary"),
    ("stats",      cmd_stats,      "Long-term usage stats"),
    ("sessions",   cmd_sessions,   "List live + recent closed sessions"),
    ("customers",  cmd_customers,  "List all customers"),
    ("customer",   cmd_customer,   "Show one customer's devices and status"),
    ("creds",      cmd_creds,      "Show credentials for a customer/device"),
    ("disable",    cmd_disable,    "Disable a customer or device"),
    ("enable",     cmd_enable,     "Re-enable a customer or device"),
    ("disconnect", cmd_disconnect, "Force-disconnect a live session"),
    ("logs",       cmd_logs,       "Tail charon.log"),
    ("bake",       cmd_bake,       "Generate a baked Windows .ps1 installer for a customer"),
    ("orphans",    cmd_orphans,    "List orphaned EAP identities (no portal device)"),
    ("deleteuser", cmd_deleteuser, "Delete a customer (with confirmation)"),
    ("renew",   cmd_renew,   "Renew a customer's tier (keeps same EAP identity)"),
    ("cleanup_orphans", cmd_cleanup_orphans, "Retire orphan users (no device) by user ID or 'all'"),
    ("renew_orphan",   cmd_renew_orphan,   "Restore an orphan user to a working customer (new password)"),
]


async def _post_init_register_commands(application: Application) -> None:
    """Push BOT_COMMANDS to the BotFather /commands menu on bot startup.

    Idempotent -- Telegram replaces the existing list. Runs in both the
    gunicorn workers and bot-polling at startup; harmless because the API
    is rate-limited and we only do 1 call per start.
    """
    await application.bot.set_my_commands(
        [BotCommand(name, desc) for name, _handler, desc in BOT_COMMANDS]
    )


# ---------- Build Application ----------

def build_application() -> Application:
    from urllib.parse import urlparse

    # Recover any orphaned block markers from a previous bot crash
    # (see _startup_recover_orphaned_blocks docstring).
    _startup_recover_orphaned_blocks()
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

    application = Application.builder().token(TELEGRAM_TOKEN).persistence(persistence).post_init(_post_init_register_commands).build()

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
            CLIENT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ct_client_name_entered),
                CommandHandler("cancel", ct_cancel),
            ],
            DURATION: [
                CallbackQueryHandler(ct_duration_chosen, pattern=r"^cd:"),
                CallbackQueryHandler(ct_duration_chosen, pattern=r"^cb$"),
                CallbackQueryHandler(ct_duration_chosen, pattern=r"^cx$"),
            ],
            CAP: [
                CallbackQueryHandler(ct_cap_chosen, pattern=r"^cc:"),
                CallbackQueryHandler(ct_cap_chosen, pattern=r"^cb$"),
                CallbackQueryHandler(ct_cap_chosen, pattern=r"^cx$"),
            ],
            SPEED: [
                CallbackQueryHandler(ct_speed_chosen, pattern=r"^cs:"),
                CallbackQueryHandler(ct_speed_chosen, pattern=r"^cb$"),
                CallbackQueryHandler(ct_speed_chosen, pattern=r"^cx$"),
            ],
            DEVICE_TYPE: [
                CallbackQueryHandler(ct_device_type_chosen, pattern=r"^cdt:"),
                CallbackQueryHandler(ct_device_type_chosen, pattern=r"^cb$"),
                CallbackQueryHandler(ct_device_type_chosen, pattern=r"^cx$"),
            ],
            NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ct_notes_entered),
                CommandHandler("skip", ct_notes_entered),
                CommandHandler("back", ct_notes_entered),
                CommandHandler("cancel", ct_cancel),
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

    for name, handler, _desc in BOT_COMMANDS:
        application.add_handler(CommandHandler(name, handler, filters=whitelist))

    # Inline keyboard callback for /sessions [Disconnect] buttons.
    # Pattern matches callback_data starting with "disc:" (eap_identity follows).
    application.add_handler(CallbackQueryHandler(cb_disconnect, pattern=r"^disc:"))

    return application
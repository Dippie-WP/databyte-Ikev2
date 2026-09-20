"""MariaDB-backed persistence for the Telegram bot's PTB Application.

Implements telegram.ext.BasePersistence so the ConversationHandler state
survives across webhook calls regardless of which gunicorn worker handles
the call. Without this, multi-worker gunicorn (--workers 4) fragments
PTB's in-memory DictPersistence across workers -- each worker has its own
app.state.bot_application instance with its own _conversations dict, so
a 6-step /create flow that lands on different workers between updates
loses state and the conversation never advances.

Schema (created in bot_persistence table by deploy script):
    id BIGINT AUTO_INCREMENT PK
    category VARCHAR(32)  -- 'bot', 'chat', 'user', 'conv'
    scope_key VARCHAR(128) -- user_id, chat_id, or conv name (or '' for bot)
    sub_key VARCHAR(128)    -- inner dict key (or pickle tuple for conv)
    data_value LONGBLOB     -- pickled Python object
    updated_at TIMESTAMP
    UNIQUE (category, scope_key, sub_key)
"""
import pickle
from collections.abc import MutableMapping
from typing import Any, Dict, List, Optional, Tuple

import pymysql
from pymysql.cursors import DictCursor

from telegram.ext import BasePersistence


def _pickle(obj) -> bytes:
    return pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)


def _unpickle(blob: bytes):
    return pickle.loads(blob)


def _serialize_conv_key(key: Tuple) -> str:
    """Serialize a ConversationHandler key tuple to a stable string.

    The key is typically (chat_id, user_id) -- int or str depending on
    PTB version. str(tuple) gives e.g. '(7748884597, 7748884597)' which
    is parseable via ast.literal_eval (safe eval of literals only).
    """
    return str(key)


def _deserialize_conv_key(s: str) -> Tuple:
    import ast
    v = ast.literal_eval(s)
    if not isinstance(v, tuple):
        raise ValueError(f"expected tuple, got {type(v).__name__}: {s!r}")
    return v


class _LazyConversationMap(MutableMapping):
    """Dict-like view returned by get_conversations(name).

    Lazily loads the conversation map from MariaDB on first access;
    caches in memory; writes through to MariaDB on every mutation via
    update_conversation(name, key, value). PTB's ConversationHandler
    iterates / reads / writes this map directly, so the cache + write-
    through model is transparent.
    """

    def __init__(self, persistence, name: str):
        self._persistence = persistence
        self._name = name
        self._cache: Optional[Dict[Tuple, Any]] = None

    def _load(self) -> Dict[Tuple, Any]:
        if self._cache is None:
            self._cache = self._persistence._load_conversations(self._name)
        return self._cache

    def flush(self):
        """Drop the in-memory cache so the next access re-reads from DB."""
        self._cache = None

    def __getitem__(self, key):
        return self._load()[key]

    def __setitem__(self, key, value):
        cache = self._load()
        cache[key] = value
        # write-through (sync DB; the async Application layer awaits us)
        self._persistence._enqueue_conv_write(self._name, key, value)

    def __delitem__(self, key):
        cache = self._load()
        if key in cache:
            del cache[key]
            self._persistence._enqueue_conv_delete(self._name, key)

    def __iter__(self):
        return iter(self._load())

    def __len__(self):
        return len(self._load())


class MariaDBPersistence(BasePersistence):
    """Async BasePersistence implementation backed by MariaDB.

    All public methods are coroutines; PTB awaits them. Each method
    acquires a pymysql connection from the pool, runs the query, and
    returns. A short-lived per-request cache avoids hammering MariaDB
    when PTB refreshes the same context multiple times within one request.
    """

    def __init__(self, db_config: Dict[str, Any]):
        super().__init__()
        self._db_config = dict(db_config)
        # per-process lazy connection (PTB may call multiple methods
        # concurrently within one update -- pymysql connections aren't
        # safe to share across coroutines, so we open per-call)
        self._bot_data_cache: Optional[Dict[str, Any]] = None
        self._chat_data_cache: Optional[Dict[int, Dict[str, Any]]] = None
        self._user_data_cache: Optional[Dict[int, Dict[str, Any]]] = None
        # pending conversation writes (flushed on update_conversation / flush)
        self._conv_writes: Dict[Tuple[str, Tuple], Any] = {}
        self._conv_deletes: List[Tuple[str, Tuple]] = []

    # ----- connection helpers -----

    def _conn(self):
        return pymysql.connect(**self._db_config, autocommit=False,
                               cursorclass=DictCursor)

    @staticmethod
    def _row_to_dict(row: Dict) -> Dict:
        return {k: v for k, v in row.items() if v is not None or k == "data_value"}

    # ----- bot_data -----

    async def get_bot_data(self) -> Dict[str, Any]:
        if self._bot_data_cache is not None:
            return self._bot_data_cache
        result: Dict[str, Any] = {}
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sub_key, data_value FROM bot_persistence "
                    "WHERE category='bot'",
                )
                for row in cur.fetchall():
                    result[row["sub_key"]] = _unpickle(row["data_value"])
        self._bot_data_cache = result
        return result

    async def update_bot_data(self, data: Dict[str, Any]) -> None:
        if not data:
            return
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    for k, v in data.items():
                        cur.execute(
                            "REPLACE INTO bot_persistence "
                            "(category, scope_key, sub_key, data_value) "
                            "VALUES ('bot', '', %s, %s)",
                            (k, _pickle(v)),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    async def refresh_bot_data(self, bot_data: Dict[str, Any]) -> None:
        self._bot_data_cache = bot_data

    # ----- chat_data -----

    async def get_chat_data(self) -> Dict[int, Dict[str, Any]]:
        if self._chat_data_cache is not None:
            return self._chat_data_cache
        result: Dict[int, Dict[str, Any]] = {}
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT scope_key, data_value FROM bot_persistence "
                    "WHERE category='chat'",
                )
                for row in cur.fetchall():
                    chat_id = int(row["scope_key"])
                    result[chat_id] = _unpickle(row["data_value"])
        self._chat_data_cache = result
        return result

    async def update_chat_data(self, chat_id: int, data: Dict[str, Any]) -> None:
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "REPLACE INTO bot_persistence "
                        "(category, scope_key, sub_key, data_value) "
                        "VALUES ('chat', %s, '', %s)",
                        (str(chat_id), _pickle(data)),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if self._chat_data_cache is not None:
            self._chat_data_cache[chat_id] = data

    async def refresh_chat_data(self, chat_id: int, chat_data: Dict[str, Any]) -> None:
        if self._chat_data_cache is not None:
            self._chat_data_cache[chat_id] = chat_data

    # ----- user_data -----

    async def get_user_data(self) -> Dict[int, Dict[str, Any]]:
        if self._user_data_cache is not None:
            return self._user_data_cache
        result: Dict[int, Dict[str, Any]] = {}
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT scope_key, data_value FROM bot_persistence "
                    "WHERE category='user'",
                )
                for row in cur.fetchall():
                    user_id = int(row["scope_key"])
                    result[user_id] = _unpickle(row["data_value"])
        self._user_data_cache = result
        return result

    async def update_user_data(self, user_id: int, data: Dict[str, Any]) -> None:
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "REPLACE INTO bot_persistence "
                        "(category, scope_key, sub_key, data_value) "
                        "VALUES ('user', %s, '', %s)",
                        (str(user_id), _pickle(data)),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if self._user_data_cache is not None:
            self._user_data_cache[user_id] = data

    async def refresh_user_data(self, user_id: int, user_data: Dict[str, Any]) -> None:
        if self._user_data_cache is not None:
            self._user_data_cache[user_id] = user_data

    # ----- drop -----

    async def drop_chat_data(self, chat_id: int) -> None:
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM bot_persistence "
                        "WHERE category='chat' AND scope_key=%s",
                        (str(chat_id),),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if self._chat_data_cache is not None:
            self._chat_data_cache.pop(chat_id, None)

    async def drop_user_data(self, user_id: int) -> None:
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM bot_persistence "
                        "WHERE category='user' AND scope_key=%s",
                        (str(user_id),),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if self._user_data_cache is not None:
            self._user_data_cache.pop(user_id, None)

    # ----- conversations (ConversationHandler state) -----

    async def get_conversations(self, name: str) -> MutableMapping[Tuple, Any]:
        return _LazyConversationMap(self, name)

    def _load_conversations(self, name: str) -> Dict[Tuple, Any]:
        result: Dict[Tuple, Any] = {}
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sub_key, data_value FROM bot_persistence "
                    "WHERE category='conv' AND scope_key=%s",
                    (name,),
                )
                for row in cur.fetchall():
                    key = _deserialize_conv_key(row["sub_key"])
                    result[key] = _unpickle(row["data_value"])
        return result

    async def update_conversation(
        self, name: str, key: Tuple, new_state: Any
    ) -> None:
        sub_key = _serialize_conv_key(key)
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    if new_state is None:
                        cur.execute(
                            "DELETE FROM bot_persistence "
                            "WHERE category='conv' AND scope_key=%s "
                            "AND sub_key=%s",
                            (name, sub_key),
                        )
                    else:
                        cur.execute(
                            "REPLACE INTO bot_persistence "
                            "(category, scope_key, sub_key, data_value) "
                            "VALUES ('conv', %s, %s, %s)",
                            (name, sub_key, _pickle(new_state)),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # internal helpers used by _LazyConversationMap write-through
    def _enqueue_conv_write(self, name: str, key: Tuple, value: Any) -> None:
        # write-through: just do the DB write synchronously inside the
        # MutableMapping's __setitem__ (PTB wraps in await already, so
        # the sync DB call is non-blocking within the async context)
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # we're inside an async context; spawn the write as a task
                loop.create_task(self.update_conversation(name, key, value))
            else:
                asyncio.run(self.update_conversation(name, key, value))
        except RuntimeError:
            # no event loop -- fallback to sync via pymysql directly
            self._sync_conv_write(name, key, value)

    def _sync_conv_write(self, name: str, key: Tuple, value: Any) -> None:
        sub_key = _serialize_conv_key(key)
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "REPLACE INTO bot_persistence "
                        "(category, scope_key, sub_key, data_value) "
                        "VALUES ('conv', %s, %s, %s)",
                        (name, sub_key, _pickle(value)),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _enqueue_conv_delete(self, name: str, key: Tuple) -> None:
        sub_key = _serialize_conv_key(key)
        with self._conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM bot_persistence "
                        "WHERE category='conv' AND scope_key=%s AND sub_key=%s",
                        (name, sub_key),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ----- callback_data (unused, but required by abstract class) -----

    async def get_callback_data(self):
        return None

    async def update_callback_data(self, data) -> None:
        pass

    # ----- flush -----

    async def flush(self) -> None:
        # All writes are committed immediately in update_* methods.
        pass

    # ----- bot reference -----

    def set_bot(self, bot) -> None:
        # No-op: we don't need the bot reference.
        pass

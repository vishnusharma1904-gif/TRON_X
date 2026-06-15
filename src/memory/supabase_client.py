"""
TRON-X Supabase Client
───────────────────────
Optional cloud persistence layer.
Falls back gracefully to local JSON if Supabase isn't configured.

Stores:
  • Chat sessions + messages (PostgreSQL)
  • Vector embeddings via pgvector (overflow from ChromaDB)
"""
from __future__ import annotations

from typing import Optional

from src.core.config import get_settings
from src.core.logger import log

settings = get_settings()


class SupabaseClient:
    def __init__(self):
        self._client = None
        self._enabled = False
        self._init()

    def _init(self) -> None:
        if not settings.supabase_enabled:
            log.info("[supabase] Not configured — using local JSON storage")
            return
        try:
            from supabase import create_client
            self._client = create_client(settings.supabase_url, settings.supabase_anon_key)
            self._enabled = True
            log.info("[supabase] Connected ✓")
        except ImportError:
            log.warning("[supabase] Package not installed — pip install supabase")
        except Exception as e:
            log.warning(f"[supabase] Connection failed: {e} — using local fallback")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Sessions ───────────────────────────────────────────────────────────────

    async def save_session(self, session: dict) -> bool:
        if not self._enabled:
            return False
        try:
            self._client.table("sessions").upsert(session).execute()
            return True
        except Exception as e:
            log.warning(f"[supabase] save_session failed: {e}")
            return False

    async def load_session(self, session_id: str) -> Optional[dict]:
        if not self._enabled:
            return None
        try:
            res = self._client.table("sessions").select("*").eq("id", session_id).execute()
            return res.data[0] if res.data else None
        except Exception as e:
            log.warning(f"[supabase] load_session failed: {e}")
            return None

    async def save_message(self, session_id: str, role: str, content: str, meta: dict = None) -> bool:
        if not self._enabled:
            return False
        try:
            self._client.table("messages").insert({
                "session_id": session_id,
                "role": role,
                "content": content,
                "metadata": meta or {},
            }).execute()
            return True
        except Exception as e:
            log.warning(f"[supabase] save_message failed: {e}")
            return False

    # ── Status ─────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "enabled": self._enabled,
            "url": settings.supabase_url if self._enabled else None,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
_supabase: SupabaseClient | None = None


def get_supabase() -> SupabaseClient:
    global _supabase
    if _supabase is None:
        _supabase = SupabaseClient()
    return _supabase

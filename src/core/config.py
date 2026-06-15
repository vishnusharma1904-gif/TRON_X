"""
Central configuration -- reads from .env via Pydantic BaseSettings.
Single source of truth for all secrets and feature flags.
"""
from functools import lru_cache
from typing import Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_inline_comments(cls, values: dict) -> dict:
        """
        python-dotenv does NOT strip inline comments (e.g. KEY=  # comment).
        Any string value that starts with '#' or is blank is treated as unset.
        """
        cleaned = {}
        for k, v in values.items():
            if isinstance(v, str):
                stripped = v.strip()
                cleaned[k] = None if (not stripped or stripped.startswith("#")) else stripped
            else:
                cleaned[k] = v
        return cleaned

    # App
    app_env:    str  = Field(default="development")
    debug:      bool = Field(default=True)
    secret_key: str  = Field(default="tron-x-dev-secret-change-in-prod")
    log_level:  str  = Field(default="INFO")

    # Phase 23: context window pruning strategy ("semantic" | "fifo")
    pruning_strategy: str = Field(default="semantic")

    # Phase 22: Local Intent Cache & Semantic Command Routing
    intent_cache_enabled:       bool  = Field(default=True)
    intent_cache_sim_threshold: float = Field(default=0.98)
    intent_cache_ttl_days:      int   = Field(default=30)

    # Phase 28: Proactive Cron Analytics & Diagnostic Self-Healing
    self_healing_enabled:           bool  = Field(default=True)
    self_healing_interval_sec:      int   = Field(default=300)
    ram_threshold_pct:               float = Field(default=85.0)
    disk_threshold_pct:              float = Field(default=90.0)
    circuit_trip_reorder_threshold:  int   = Field(default=3)

    # Phase 33: Encrypted Memory Backup & Disaster Recovery
    # Opt-in (default False) -- see src/system/backup.py docstring for why
    # this deviates from the original spec's "default True / fail loudly"
    # design. Enabling without backup_passphrase logs a startup warning and
    # leaves backups disabled rather than crashing.
    backup_enabled:         bool           = Field(default=False)
    backup_dir:             str            = Field(default="backups/")
    backup_retention_count: int            = Field(default=7)
    backup_passphrase:      Optional[str]  = Field(default=None)
    backup_cron:            str            = Field(default="0 3 * * *")

    # Phase 37: Proactive Intelligence (Anticipation Engine + Sentinel)
    proactive_enabled:             bool = Field(default=True)
    proactive_morning_cron:        str  = Field(default="0 8 * * *")
    proactive_evening_cron:        str  = Field(default="0 21 * * *")
    proactive_sentinel_interval_sec: int = Field(default=180)
    proactive_meeting_lead_min:    int  = Field(default=15)
    proactive_vip_senders:         Optional[str] = Field(default=None)  # comma-separated
    consolidation_enabled:         bool = Field(default=True)
    consolidation_cron:            str  = Field(default="30 3 * * *")
    consolidation_retention_days:  int  = Field(default=90)
    consolidation_prune_enabled:   bool = Field(default=False)  # opt-in deletion
    default_location:              Optional[str] = Field(default=None)  # weather

    # Phase 27: Ephemeral Docker Container Code Sandbox
    # "auto" uses Docker when the SDK is installed and the daemon is
    # reachable (DockerSandbox.is_available()), and transparently falls back
    # to the existing AST-sandboxed subprocess executor otherwise -- so
    # behavior is unchanged on machines without Docker (e.g. this sandbox).
    # "docker" forces Docker (returns an error if unavailable); "subprocess"
    # forces the original behavior regardless of Docker availability.
    code_sandbox_mode:   str   = Field(default="auto")  # "docker" | "subprocess" | "auto"
    docker_mem_limit:    str   = Field(default="256m")
    docker_cpu_quota:    float = Field(default=0.5)
    docker_timeout_sec:  int   = Field(default=30)
    docker_network_mode: str   = Field(default="none")  # "none" | "bridge"

    # Tier-1: Free / always-available providers
    groq_api_key:       Optional[str] = Field(default=None)   # groq.com
    cerebras_api_key:   Optional[str] = Field(default=None)   # cerebras.ai
    gemini_api_key:     Optional[str] = Field(default=None)   # ai.google.dev
    openrouter_api_key: Optional[str] = Field(default=None)   # openrouter.ai

    # Tier-2: Pay-as-you-go cloud providers
    together_api_key:     Optional[str] = Field(default=None)   # together.ai
    fireworks_ai_api_key: Optional[str] = Field(default=None)   # fireworks.ai
    deepinfra_api_key:    Optional[str] = Field(default=None)   # deepinfra.com
    mistral_api_key:      Optional[str] = Field(default=None)   # mistral.ai
    cohere_api_key:       Optional[str] = Field(default=None)   # cohere.com
    perplexityai_api_key: Optional[str] = Field(default=None)   # perplexity.ai
    deepseek_api_key:     Optional[str] = Field(default=None)   # platform.deepseek.com
    huggingface_api_key:  Optional[str] = Field(default=None)   # huggingface.co

    # Tier-3: Local / self-hosted
    ollama_base_url: str  = Field(default="http://localhost:11434")
    ollama_model:    str  = Field(default="qwen2.5:3b")
    ollama_enabled:  bool = Field(default=False)

    # [Phase 29] Local Embedding Offloading & Ollama Mesh Fallback
    # When every cloud provider in a category's chain is exhausted, the
    # router falls back to a local Ollama model (instead of raising
    # AllProvidersExhaustedError) -- independent of `ollama_enabled`, which
    # only controls whether Ollama is offered as a normal chain member.
    ollama_fallback_enabled:          bool = Field(default=True)
    ollama_health_check_interval_sec: int  = Field(default=60)

    # Embedding backend: embeddings stay 100% local regardless of the LLM
    # generation fallback above. "sentence_transformers" (default) uses
    # all-MiniLM-L6-v2; "ollama" optionally routes to an Ollama embedding
    # model (e.g. nomic-embed-text) via ollama_base_url -- see
    # src/memory/embeddings.py.
    embedding_backend: str = Field(default="sentence_transformers")

    # Supabase
    supabase_url:      Optional[str] = Field(default=None)
    supabase_anon_key: Optional[str] = Field(default=None)

    # Home Assistant
    ha_url:   Optional[str] = Field(default=None)
    ha_token: Optional[str] = Field(default=None)

    # SMTP (email send)
    smtp_host: Optional[str] = Field(default=None)
    smtp_port: int            = Field(default=587)
    smtp_user: Optional[str] = Field(default=None)
    smtp_pass: Optional[str] = Field(default=None)
    smtp_from: Optional[str] = Field(default=None)

    # IMAP (email reader)
    imap_host: Optional[str] = Field(default=None)
    imap_port: int            = Field(default=993)
    imap_user: Optional[str] = Field(default=None)
    imap_pass: Optional[str] = Field(default=None)
    imap_ssl:  bool           = Field(default=True)

    # WhatsApp Cloud API (Meta Graph API) -- send + webhook receive
    whatsapp_access_token:        Optional[str] = Field(default=None)   # permanent/system-user token
    whatsapp_phone_number_id:     Optional[str] = Field(default=None)   # sender phone number ID
    whatsapp_business_account_id: Optional[str] = Field(default=None)   # WABA ID (optional)
    whatsapp_app_secret:          Optional[str] = Field(default=None)   # app secret for X-Hub-Signature-256
    whatsapp_verify_token:        Optional[str] = Field(default=None)   # your chosen webhook verify token
    whatsapp_api_version:         str            = Field(default="v21.0")
    whatsapp_store_path:          str            = Field(default="memory/whatsapp_messages.json")
    whatsapp_store_max:           int            = Field(default=2000)  # ring-buffer cap

    # WhatsApp SENDING backend: "baileys" (open-source bridge, default) or "cloud" (Graph API)
    whatsapp_backend:      str            = Field(default="baileys")
    whatsapp_bridge_url:   str            = Field(default="http://127.0.0.1:8088")
    whatsapp_bridge_token: Optional[str]  = Field(default=None)   # shared secret with the Node sidecar
    whatsapp_contacts_path: str           = Field(default="memory/whatsapp_contacts.json")

    # Search
    brave_api_key:  Optional[str] = Field(default=None)
    serper_api_key: Optional[str] = Field(default=None)

    # Google Calendar
    google_credentials_path: Optional[str] = Field(default=None)

    # ElevenLabs TTS
    elevenlabs_api_key:      Optional[str] = Field(default=None)
    elevenlabs_voice_jarvis: str           = Field(default="pNInz6obpgDQGcFmaJgB")  # Adam
    elevenlabs_voice_friday: str           = Field(default="21m00Tcm4TlvDq8ikWAM")  # Rachel
    elevenlabs_model:        str           = Field(default="eleven_turbo_v2_5")

    # Voice / language experience
    voice_output_default_enabled: bool = Field(default=False)
    wake_word_enabled:            bool = Field(default=False)
    telugu_auto_language_enabled: bool = Field(default=True)
    academic_mode_default_enabled: bool = Field(default=False)
    self_model_enabled:           bool = Field(default=True)
    search_timeout_sec:           int  = Field(default=12)
    search_provider_order:        str  = Field(default="serper,brave,wikipedia,ddg")

    # Real-Time Data Feeds
    openweather_api_key: Optional[str] = Field(default=None)   # openweathermap.org
    newsapi_key:         Optional[str] = Field(default=None)   # newsapi.org
    alpha_vantage_key:   Optional[str] = Field(default=None)   # alphavantage.co

    # Production Auth
    auth_enabled:      bool           = Field(default=False)
    api_keys:          Optional[str]  = Field(default=None)
    auth_skip_paths:   str            = Field(default="/health,/docs,/redoc,/static,/openapi.json,/api/whatsapp/webhook,/api/whatsapp/bridge/ingest")

    # Production Rate Limiting
    rate_limit_enabled:    bool = Field(default=False)
    rate_limit_rpm:        int  = Field(default=60)
    rate_limit_skip_paths: str  = Field(default="/health,/static,/api/whatsapp/webhook,/api/whatsapp/bridge/ingest")

    # Provider key map
    _PROVIDER_KEY_MAP: dict = {
        "groq":         "groq_api_key",
        "cerebras":     "cerebras_api_key",
        "gemini":       "gemini_api_key",
        "openrouter":   "openrouter_api_key",
        "together_ai":  "together_api_key",
        "fireworks_ai": "fireworks_ai_api_key",
        "deepinfra":    "deepinfra_api_key",
        "mistral":      "mistral_api_key",
        "cohere":       "cohere_api_key",
        "perplexity":   "perplexityai_api_key",
        "deepseek":     "deepseek_api_key",
        "huggingface":  "huggingface_api_key",
        "ollama":       None,
    }

    @property
    def available_providers(self) -> list[str]:
        """Returns list of provider prefixes that have API keys configured."""
        available = []
        key_map = {
            "groq":         self.groq_api_key,
            "cerebras":     self.cerebras_api_key,
            "gemini":       self.gemini_api_key,
            "openrouter":   self.openrouter_api_key,
            "together_ai":  self.together_api_key,
            "fireworks_ai": self.fireworks_ai_api_key,
            "deepinfra":    self.deepinfra_api_key,
            "mistral":      self.mistral_api_key,
            "cohere":       self.cohere_api_key,
            "perplexity":   self.perplexityai_api_key,
            "deepseek":     self.deepseek_api_key,
            "huggingface":  self.huggingface_api_key,
        }
        for provider, key in key_map.items():
            if key:
                available.append(provider)
        if self.ollama_enabled:
            available.append("ollama")
        return available

    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)

    @property
    def whatsapp_enabled(self) -> bool:
        """True when the minimum config to SEND via the active backend is present."""
        if self.whatsapp_backend == "baileys":
            return bool(self.whatsapp_bridge_token and self.whatsapp_bridge_url)
        return bool(self.whatsapp_access_token and self.whatsapp_phone_number_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# Module-level singleton
settings = get_settings()
# (Phase 37 config fields added above — see proactive_* / consolidation_*)

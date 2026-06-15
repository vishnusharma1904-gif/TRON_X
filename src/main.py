"""
TRON-X  Main FastAPI Application

Run (dev):  uvicorn src.main:app --reload --port 8000
Run (prod): uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 2
"""
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.core.config import get_settings
from src.core.logger import log, console
from src.core.exceptions import TronXError

settings = get_settings()


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    console.print("""
[bold cyan]
╔════════════════════════════════════════════╗
║   ████████╗██████╗  ██████╗ ███╗  ██╗    ║
║      ██╔══╝██╔══██╗██╔═══██╗████╗ ██║    ║
║      ██║   ██████╔╝██║   ██║██╔██╗██║    ║
║      ██║   ██╔══██╗██║   ██║██║╚████║    ║
║      ██║   ██║  ██║╚██████╔╝██║ ╚███║    ║
║      ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚══╝    ║
║                    --- X ---              ║
╚════════════════════════════════════════════╝
[/bold cyan]
[dim]Autonomous AI Assistant  v1.0.0[/dim]
""")

    # Pre-warm router & orchestrator
    from src.intelligence.router import get_router
    from src.intelligence.orchestrator import get_orchestrator

    log.info("Initialising smart router...")
    get_router()

    log.info("Initialising orchestrator...")
    get_orchestrator()

    providers = settings.available_providers
    if not providers:
        log.warning(
            "[bold yellow]No API keys detected.[/bold yellow] "
            "Copy .env.example -> .env and add your keys."
        )
    else:
        log.info(f"Active providers: [bold cyan]{', '.join(providers)}[/bold cyan]")

    # Start APScheduler
    from src.agents.scheduler_agent import get_scheduler
    scheduler = get_scheduler()
    scheduler.start()
    log.info("APScheduler started")

    # Phase 28: Proactive self-healing health-check job
    if settings.self_healing_enabled:
        from src.system.self_healing import run_health_check

        async def _run_health_check():
            await run_health_check()

        scheduler.add_interval_job(
            "self_healing_check",
            _run_health_check,
            seconds=settings.self_healing_interval_sec,
            description="Phase 28: CPU/RAM/disk + router health check & self-healing",
        )
        log.info(
            f"[self_healing] Enabled -- interval={settings.self_healing_interval_sec}s, "
            f"RAM>={settings.ram_threshold_pct}%, DISK>={settings.disk_threshold_pct}%, "
            f"trip_reorder>={settings.circuit_trip_reorder_threshold}"
        )
    else:
        log.info("[self_healing] Disabled (SELF_HEALING_ENABLED=false)")

    # Phase 22: Local Intent Cache — TTL eviction (run once at startup, then daily)
    if settings.intent_cache_enabled:
        from src.intelligence.intent_cache import get_intent_cache

        _intent_cache = get_intent_cache()
        if _intent_cache.enabled:
            _evicted = await _intent_cache.evict_expired()
            if _evicted:
                log.info(
                    f"[intent_cache] Evicted {_evicted} expired entr"
                    f"{'y' if _evicted == 1 else 'ies'} on startup"
                )

            async def _run_intent_cache_eviction():
                await get_intent_cache().evict_expired()

            scheduler.add_cron_job(
                "intent_cache_eviction",
                _run_intent_cache_eviction,
                cron_expr="0 3 * * *",
                description="Phase 22: Daily intent-cache TTL eviction",
            )
            log.info(
                f"[intent_cache] Enabled -- sim_threshold={settings.intent_cache_sim_threshold}, "
                f"ttl_days={settings.intent_cache_ttl_days}"
            )
        else:
            log.info("[intent_cache] Store unavailable -- running disabled (see warning above)")
    else:
        log.info("[intent_cache] Disabled (INTENT_CACHE_ENABLED=false)")

    # Phase 33: Encrypted Memory Backup & Disaster Recovery (opt-in)
    if settings.backup_enabled:
        if not settings.backup_passphrase:
            log.warning(
                "[backup] BACKUP_ENABLED=true but BACKUP_PASSPHRASE is not set -- "
                "backups will NOT run until a passphrase is configured."
            )
        else:
            from src.system.backup import create_backup

            async def _run_backup():
                try:
                    path = await create_backup()
                    log.info(f"[backup] Scheduled backup created: {path.name}")
                except Exception as e:
                    log.error(f"[backup] Scheduled backup failed: {e}")

            scheduler.add_cron_job(
                "encrypted_memory_backup",
                _run_backup,
                cron_expr=settings.backup_cron,
                description="Phase 33: encrypted memory backup",
            )
            log.info(
                f"[backup] Enabled -- cron='{settings.backup_cron}', "
                f"dir={settings.backup_dir}, retention={settings.backup_retention_count}"
            )
    else:
        log.info("[backup] Disabled (BACKUP_ENABLED=false)")

    # Phase 37: Proactive Intelligence — event bus, briefings, sentinel, consolidation
    import asyncio as _asyncio_p37
    from src.core.event_bus import get_event_bus
    get_event_bus().bind_loop(_asyncio_p37.get_running_loop())
    if settings.proactive_enabled:
        from src.proactive.anticipator import get_anticipator
        from src.proactive.triggers import get_sentinel

        def _run_briefing(kind: str):
            async def _fire():
                await get_anticipator().briefing(kind=kind, force=True)
            return _fire

        scheduler.add_cron_job(
            "proactive_morning_briefing", _run_briefing("morning"),
            cron_expr=settings.proactive_morning_cron,
            description="Phase 37: morning briefing",
        )
        scheduler.add_cron_job(
            "proactive_evening_briefing", _run_briefing("evening"),
            cron_expr=settings.proactive_evening_cron,
            description="Phase 37: evening wrap-up",
        )

        async def _run_sentinel():
            await get_sentinel().run_once()

        scheduler.add_interval_job(
            "proactive_sentinel", _run_sentinel,
            seconds=settings.proactive_sentinel_interval_sec,
            description="Phase 37: sentinel sweep (meetings/conflicts/VIP mail)",
        )
        log.info(
            f"[proactive] Enabled -- morning='{settings.proactive_morning_cron}', "
            f"evening='{settings.proactive_evening_cron}', "
            f"sentinel every {settings.proactive_sentinel_interval_sec}s"
        )
    else:
        log.info("[proactive] Disabled (PROACTIVE_ENABLED=false)")

    # Phase 38: periodic self-reflection (functional self-model journal)
    if settings.self_model_enabled:
        from src.intelligence.self_model import get_self_model

        async def _run_self_reflection():
            try:
                await get_self_model().deep_reflect()
            except Exception as e:
                log.debug(f"[self_model] scheduled reflection failed: {e}")

        scheduler.add_cron_job(
            "self_model_reflection", _run_self_reflection,
            cron_expr="0 */6 * * *",
            description="Phase 38: 6-hourly self-model deep reflection",
        )
        log.info("[self_model] Enabled -- reflection every 6h, "
                 "state injected into chat prompts")

    if settings.consolidation_enabled:
        from src.proactive.consolidation import consolidate as _consolidate

        async def _run_consolidation():
            await _consolidate()

        scheduler.add_cron_job(
            "memory_consolidation", _run_consolidation,
            cron_expr=settings.consolidation_cron,
            description="Phase 37: nightly memory consolidation",
        )
        log.info(
            f"[consolidation] Enabled -- cron='{settings.consolidation_cron}', "
            f"retention={settings.consolidation_retention_days}d, "
            f"prune={'on' if settings.consolidation_prune_enabled else 'off (opt-in)'}"
        )
    else:
        log.info("[consolidation] Disabled (CONSOLIDATION_ENABLED=false)")

    # Start HA WebSocket listener (if configured)
    from src.iot.ws_listener import get_ws_listener
    ws = get_ws_listener()
    if ws.enabled:
        ws.start()
        log.info("Home Assistant WS listener started")

    # Wake-word detector (opt-in)
    if settings.wake_word_enabled:
        try:
            from src.voice.wake_word import get_wake_word_detector
            get_wake_word_detector().start()
            log.info("[wake_word] Enabled")
        except Exception as exc:
            log.warning("[wake_word] Failed to start: %s", exc)
    else:
        log.info("[wake_word] Disabled")

    # Scan and load plugins (Phase 18)
    from src.plugins.plugin_registry import get_registry
    from src.api.plugins import _sync_coordinator
    plugin_names = await get_registry().scan()
    if plugin_names:
        _sync_coordinator()
        log.info("Plugins loaded: [bold cyan]%s[/bold cyan]", ", ".join(plugin_names))
    else:
        log.info("No plugins found in %s", get_registry().plugin_dir)

    # Pre-warm embedding model so first RAG save doesn't stall (avoids 4-8s cold-start)
    try:
        import asyncio as _asyncio
        from src.memory.embeddings import embed as _embed
        log.info("Pre-warming embedding model...")
        await _asyncio.get_event_loop().run_in_executor(None, _embed, ["warmup"])
        log.info("Embedding model ready")
    except Exception as _e:
        log.warning("Embedding pre-warm skipped: %s", _e)

    log.info("TRON-X online at [bold]http://127.0.0.1:8000[/bold]")

    yield  # App runs

    # Graceful shutdown
    scheduler.stop()
    ws.stop()
    try:
        from src.voice.wake_word import get_wake_word_detector
        get_wake_word_detector().stop()
    except Exception:
        pass
    log.info("TRON-X shutting down...")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
app = FastAPI(
    title="TRON-X",
    description="Autonomous AI Assistant -- Jarvis/Friday Architecture",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request timing middleware
@app.middleware("http")
async def add_timing(request: Request, call_next):
    t0 = time.monotonic()
    response = await call_next(request)
    elapsed = (time.monotonic() - t0) * 1000
    # Don't add headers to streaming responses - headers are already sent
    from fastapi.responses import StreamingResponse
    if not isinstance(response, StreamingResponse):
        response.headers["X-Response-Time"] = f"{elapsed:.1f}ms"
    return response

# No-cache headers for static JS/CSS so updates load immediately
@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    resp = await call_next(request)
    # Skip modifying streaming responses - headers are already sent
    from fastapi.responses import StreamingResponse
    if not isinstance(resp, StreamingResponse):
        if request.url.path.startswith("/static/") and request.url.path.endswith((".js", ".css")):
            resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            resp.headers["Pragma"]        = "no-cache"
            resp.headers["Expires"]       = "0"
    return resp

# Global exception handler
@app.exception_handler(TronXError)
async def tronx_error_handler(request: Request, exc: TronXError):
    log.error(f"TronXError: {exc.message} | {exc.details}")
    return JSONResponse(
        status_code=500,
        content={"error": exc.message, "details": exc.details},
    )


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
from src.api.health        import router as health_router
from src.api.chat          import router as chat_router
from src.api.memory        import router as memory_router
from src.api.voice         import router as voice_router
from src.api.system        import router as system_router
from src.api.iot           import router as iot_router
from src.api.agents        import router as agents_router
from src.api.calendar      import router as calendar_router
from src.api.email         import router as email_router
from src.api.whatsapp      import router as whatsapp_router
from src.api.episodic      import router as episodic_router
from src.api.feeds         import router as feeds_router        # Phase 15
from src.api.analytics     import router as analytics_router    # Phase 17
from src.api.plugins       import router as plugins_router      # Phase 18
from src.api.search        import router as search_router       # Phase 3
from src.api.computer      import router as computer_router     # Phase 4
from src.api.system_health import router as system_health_router  # Phase 28
from src.api.security      import router as security_router
from src.api.proactive     import router as proactive_router    # Phase 37
from src.api.attachments   import router as attachments_router  # Phase 38
from src.api.self          import router as self_router         # Phase 38
from src.api.avengers      import router as avengers_router     # A.V.E.N.G.E.R.S Protocol
from src.api.admin         import router as admin_router        # Per-user privacy / admin review

app.include_router(health_router)
app.include_router(chat_router)
app.include_router(memory_router)
app.include_router(voice_router)
app.include_router(system_router)
app.include_router(iot_router)
app.include_router(agents_router)
app.include_router(calendar_router)
app.include_router(email_router)
app.include_router(whatsapp_router)
app.include_router(episodic_router)
app.include_router(feeds_router)                                # Phase 15
app.include_router(analytics_router)                            # Phase 17
app.include_router(plugins_router)                              # Phase 18
app.include_router(search_router)                               # Phase 3
app.include_router(computer_router)                             # Phase 4
app.include_router(system_health_router)                        # Phase 28
app.include_router(security_router)
app.include_router(proactive_router)                            # Phase 37
app.include_router(attachments_router)                          # Phase 38
app.include_router(self_router)                                  # Phase 38
app.include_router(avengers_router)                              # A.V.E.N.G.E.R.S Protocol
app.include_router(admin_router)                                 # Admin-only cross-user review

# Analytics HTTP middleware -- fire-and-forget request recording (Phase 17)
from src.analytics.middleware import add_analytics_middleware
add_analytics_middleware(app)

# Rate limiting middleware (Phase 20) -- register before auth so 429 fires first
from src.core.ratelimit import add_rate_limit_middleware
add_rate_limit_middleware(app)

# Auth middleware (Phase 20) -- outermost gate
from src.core.auth import add_auth_middleware
add_auth_middleware(app)


# ---------------------------------------------------------------------------
# Static files + root
# ---------------------------------------------------------------------------
static_path = Path("static")
app.mount("/static", StaticFiles(directory="static"), name="static")


# Root -> A.V.E.N.G.E.R.S command center; legacy HUD preserved at /classic
@app.get("/")
async def root():
    return RedirectResponse(url="/static/avengers.html")


@app.get("/classic")
async def classic_hud():
    """Legacy HUD — preserved untouched by the A.V.E.N.G.E.R.S upgrade."""
    return RedirectResponse(url="/static/index.html")

"""
Structured logging with Rich console output and file rotation.
"""
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

# ── Custom theme ──────────────────────────────────────────────────────────────
TRON_THEME = Theme({
    "logging.level.debug":   "dim cyan",
    "logging.level.info":    "bold cyan",
    "logging.level.warning": "bold yellow",
    "logging.level.error":   "bold red",
    "logging.level.critical":"bold white on red",
    "repr.str":              "cyan",
    "repr.number":           "bold blue",
})

console = Console(theme=TRON_THEME, stderr=True)


def setup_logger(name: str = "tron_x", level: str = "INFO") -> logging.Logger:
    """Configure and return the application logger."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Ensure log directory exists
    Path("logs").mkdir(exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(log_level)

    if logger.handlers:
        return logger  # Already configured

    # ── Rich console handler ──────────────────────────────────────────
    console_handler = RichHandler(
        console=console,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        show_time=True,
        show_path=False,
        markup=True,
    )
    console_handler.setLevel(log_level)
    logger.addHandler(console_handler)

    # ── File handler ──────────────────────────────────────────────────
    try:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            "logs/tron_x.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        logger.addHandler(file_handler)
    except Exception:
        pass  # Non-fatal — console logging still works

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "litellm", "chromadb", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logger


# ── Module-level default logger ───────────────────────────────────────────────
log = setup_logger()

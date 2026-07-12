"""
Audit logging helpers for MCP server.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Any

from config import settings

AUDIT_LOGGER_NAME = "mcp_audit"
_audit_logger: Optional[logging.Logger] = None
_audit_log_path: Optional[Path] = None


def _init_audit_logger():
    """Initialize dedicated audit logger writing to a single UTF-8 file."""
    global _audit_logger, _audit_log_path
    if not settings.enable_log:
        return
    try:
        # Prefer /app/logs, fall back to ./logs for local development
        try:
            base = Path("/app/logs")
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            base = Path("./logs")
            base.mkdir(parents=True, exist_ok=True)
        _audit_log_path = base / "mcp_audit.log"

        logger = logging.getLogger(AUDIT_LOGGER_NAME)
        logger.setLevel(logging.DEBUG if settings.enable_debug else logging.INFO)
        fh = RotatingFileHandler(_audit_log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        # Ensure single handler to avoid duplicate lines
        logger.handlers = []
        logger.addHandler(fh)
        logger.propagate = False

        _audit_logger = logger
        logging.info(f"Audit logging enabled at {_audit_log_path}")
    except Exception as e:
        logging.error(f"Failed to initialize audit logger: {e}")


def get_audit_logger() -> Optional[logging.Logger]:
    """Return the audit logger instance if logging is enabled, else None."""
    global _audit_logger
    if _audit_logger is None and settings.enable_log:
        _init_audit_logger()
    return _audit_logger


def audit_block(title: str, body: Optional[Any] = None, request_id: Optional[str] = None):
    """
    Write a clearly delimited block to the audit log file if enabled.
    Each block is clearly separated to aid visual analysis of long texts.
    Handles any type for body parameter (converts to string for Cython compatibility).
    """
    logger = get_audit_logger()
    if logger is None:
        return
    sep = "=" * 80
    rid = f" | request_id={request_id}" if request_id else ""
    # Handle any type for body - convert to string for Cython compatibility
    if body is None:
        body_text = ""
    elif isinstance(body, str):
        body_text = body
    else:
        # Convert any other type (bool, int, float, dict, list, etc.) to string
        try:
            import json
            if isinstance(body, (dict, list)):
                body_text = json.dumps(body, ensure_ascii=False, indent=2)
            else:
                body_text = str(body)
        except Exception:
            body_text = str(body)
    text = f"\n{sep}\n{title}{rid}\n{sep}\n{body_text}\n"
    logger.info(text)


# Initialize audit logger at import-time if enabled (lazy get_audit_logger keeps this safe)
_init_audit_logger()

__all__ = ["audit_block", "get_audit_logger", "_init_audit_logger", "AUDIT_LOGGER_NAME"]
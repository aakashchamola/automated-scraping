"""
config_util.py — Shared config loading + validation.

Every entry point loads config through here so a malformed config fails
immediately with a message saying exactly what to fix, instead of a
traceback minutes or hours later.
"""

import json
import logging
import sys

logger = logging.getLogger(__name__)

_REQUIRED_SECTIONS = ("telegram", "filter", "alerts")


def _fail(msg: str) -> None:
    logger.error(f"config error: {msg}")
    sys.exit(1)


def load_config(path: str, require_telegram_creds: bool = False) -> dict:
    """Load, validate and default-fill config.json. Exits with a clear
    message on problems."""
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        _fail(f"config file not found: {path}")
    except json.JSONDecodeError as exc:
        _fail(f"{path} is not valid JSON: {exc}")

    for section in _REQUIRED_SECTIONS:
        if not isinstance(cfg.get(section), dict):
            _fail(f"missing or invalid section '{section}' in {path}")

    tg = cfg["telegram"]
    if not isinstance(tg.get("channels"), list) or not tg["channels"]:
        _fail("telegram.channels must be a non-empty list of channel usernames")
    if require_telegram_creds and (not tg.get("api_id") or not tg.get("api_hash")):
        _fail(
            "telegram.api_id / api_hash missing.\n"
            "Get them at https://my.telegram.org -> 'API development tools', "
            "then paste into config.json (or run: python setup_wizard.py)."
        )
    if require_telegram_creds and not str(tg["api_id"]).isdigit():
        _fail("telegram.api_id must be numeric")

    flt = cfg["filter"]
    for key in ("slot_keywords", "consulates", "block_keywords"):
        if not isinstance(flt.get(key), list):
            _fail(f"filter.{key} must be a list")

    alerts = cfg["alerts"]
    alerts.setdefault("cooldown_seconds", 180)
    ntfy = alerts.setdefault("ntfy", {})
    if ntfy.get("enabled") and not ntfy.get("topic", "").strip():
        logger.warning(
            "alerts.ntfy.topic is empty — phone pushes are OFF. "
            "Run 'python setup_wizard.py' to configure."
        )

    cfg.setdefault("sources", {}).setdefault("reputation", {})
    cfg["sources"].setdefault("default_reputation", 1)
    cfg["sources"].setdefault("min_urgent_score", 3)
    cfg.setdefault("monitoring", {})
    cfg.setdefault("reddit", {})
    return cfg

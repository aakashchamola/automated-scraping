import logging
import os
from datetime import datetime


def _coerce_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        resolved = logging.getLevelName(level.strip().upper())
        if isinstance(resolved, int):
            return resolved
    return logging.INFO


def setup_logging(
    log_dir: str = "logs",
    level: str | int = "INFO",
    *,
    name: str = "scrape",
    force: bool = False,
) -> str:
    """Configure logging to write to both console and a timestamped log file.

    Each run gets its own file named ``<name>_<YYYYmmdd_HHMMSS>.log`` so every
    service/entry point produces a distinct, identifiable log. Returns the
    log file path.
    """
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(
        log_dir, f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    logging.basicConfig(
        level=_coerce_level(level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=force,
    )
    logging.getLogger(__name__).info(f"Logging to {log_file}")
    return log_file


def setup_logging_from_config(
    config: dict, log_dir: str = "logs", *, name: str = "scrape"
) -> str:
    """Configure logging from config.json using top-level logging.level.

    ``name`` sets the per-run log file prefix (e.g. "pipeline", "validate").
    """
    logging_cfg = config.get("logging", {}) if isinstance(config, dict) else {}
    level = logging_cfg.get("level", "INFO")
    return setup_logging(log_dir=log_dir, level=level, name=name, force=True)

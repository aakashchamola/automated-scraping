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
    force: bool = False,
) -> None:
    """Configure logging to write to both console and a timestamped log file."""
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(
        log_dir, f"scrape_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
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


def setup_logging_from_config(config: dict, log_dir: str = "logs") -> None:
    """Configure logging from config.json using top-level logging.level."""
    logging_cfg = config.get("logging", {}) if isinstance(config, dict) else {}
    level = logging_cfg.get("level", "INFO")
    setup_logging(log_dir=log_dir, level=level, force=True)

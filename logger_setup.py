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


def console_level(file_level: str | int) -> int:
    """How much of the log the console should get.

    The console and the file are not equally private. This pipeline logs what
    it reads — company names, career page URLs, search keywords — and on a
    PUBLIC repository the console is the GitHub Actions run log, which any
    signed-in GitHub user can read. That is the same content the published
    dashboard goes to the trouble of encrypting, so sending it to stdout would
    hand it out for free.

    So CI raises the console to WARNING while the file keeps everything. Set
    LOG_CONSOLE_LEVEL to override; unset locally, the console matches the file
    and nothing about running this on your own machine changes.
    """
    override = os.environ.get("LOG_CONSOLE_LEVEL")
    if override:
        return _coerce_level(override)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return logging.WARNING
    return _coerce_level(file_level)


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

    The two handlers can be set to different levels — see ``console_level``.
    """
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(
        log_dir, f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(_coerce_level(level))

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(console_level(level))

    logging.basicConfig(
        # The root must pass everything either handler might want, or a
        # handler's own level can never see it.
        level=min(_coerce_level(level), console_level(level)),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[file_handler, stream_handler],
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

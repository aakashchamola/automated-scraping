"""
config_loader.py — Central config loading for the scraping pipeline.

Supports both YAML (config.yaml) and JSON (config.json) based on file
extension. YAML is preferred because it supports inline comments.

Usage:
    from config_loader import load_config
    config = load_config("config.yaml")
"""

import json
import logging
import sys

logger = logging.getLogger(__name__)


def load_config(path: str) -> dict:
    """Load and return a config dict from a YAML or JSON file.

    YAML files (.yaml / .yml) are loaded with yaml.safe_load.
    JSON files (.json) are loaded with json.load.
    Exits with an error message on missing file or parse failure.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except FileNotFoundError:
        logger.error(f"Config file not found: '{path}'")
        sys.exit(1)

    ext = path.rsplit(".", 1)[-1].lower()

    if ext in ("yaml", "yml"):
        try:
            import yaml
        except ImportError:
            logger.error("PyYAML is not installed. Run: pip install pyyaml")
            sys.exit(1)
        try:
            cfg = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            logger.error(f"Config is not valid YAML: {exc}")
            sys.exit(1)
    else:
        try:
            cfg = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.error(f"Config is not valid JSON: {exc}")
            sys.exit(1)

    if not isinstance(cfg, dict):
        logger.error(f"Config file '{path}' must contain a mapping at the top level.")
        sys.exit(1)

    return cfg

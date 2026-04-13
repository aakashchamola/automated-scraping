import logging
import os
from datetime import datetime


def setup_logging(log_dir: str = "logs") -> None:
    """Configure logging to write to both console and a timestamped log file."""
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(
        log_dir, f"scrape_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

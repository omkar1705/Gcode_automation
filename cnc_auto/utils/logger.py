"""
Centralized logging configuration for the CNC automation system.
Provides file + console logging with rotation.
"""

import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime


def setup_logger(name: str, config: dict) -> logging.Logger:
    """
    Create and configure a logger instance.

    Args:
        name: Logger name (usually module name)
        config: Logging configuration dict from config.json

    Returns:
        Configured logging.Logger instance
    """
    log_cfg = config.get("logging", {})
    level_str = log_cfg.get("level", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Prevent duplicate handlers
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File handler (optional)
    if log_cfg.get("log_to_file", False):
        log_dir = log_cfg.get("log_dir", "logs")
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(
            log_dir,
            f"cnc_auto_{datetime.now().strftime('%Y%m%d')}.log"
        )
        max_bytes = log_cfg.get("max_log_size_mb", 10) * 1024 * 1024
        backup_count = log_cfg.get("backup_count", 5)

        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


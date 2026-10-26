"""日志初始化：滚动文件（UTF-8，含完整信息）+ 控制台（仅 WARNING 及以上）。

密钥等敏感信息不经过日志输出；调用方负责不把密钥传入日志参数。
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("butler")
    if logger.handlers:  # 防止重复初始化
        return logger
    logger.setLevel(logging.DEBUG)  # 级别由各 handler 决定
    logger.propagate = False

    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = RotatingFileHandler(
        log_dir / "butler.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(getattr(logging, level, logging.INFO))
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger

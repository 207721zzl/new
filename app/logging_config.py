"""带请求上下文的控制台与滚动文件日志配置。"""

import logging
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

from app.config import get_settings


request_id_context: ContextVar[str] = ContextVar("request_id", default="-")
run_id_context: ContextVar[str] = ContextVar("run_id", default="-")

settings = get_settings()
LOG_DIR = settings.resolved_log_dir
LOG_FILE = LOG_DIR / "evidence_rag.log"


class ContextFilter(logging.Filter):
    """把当前请求和运行标识注入每条日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """附加上下文字段，供统一日志格式使用。"""
        record.request_id = request_id_context.get()
        record.run_id = run_id_context.get()
        return True


def configure_logging() -> logging.Logger:
    """幂等配置 EvidenceRAG 根日志器。"""
    logger = logging.getLogger("evidence_rag")
    if logger.handlers:
        return logger

    level_name = settings.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | "
        "request_id=%(request_id)s | run_id=%(run_id)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    context_filter = ContextFilter()

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    return logger


def get_logger(name: str) -> logging.Logger:
    """获取指定子模块的应用日志器。"""
    configure_logging()
    return logging.getLogger(f"evidence_rag.{name}")

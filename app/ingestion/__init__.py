"""多格式知识文档解析入口。"""

from app.ingestion.parsers import SUPPORTED_EXTENSIONS, parse_source

__all__ = ["SUPPORTED_EXTENSIONS", "parse_source"]

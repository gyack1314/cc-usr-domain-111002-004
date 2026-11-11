"""可暂停、可移交、可验证恢复的浏览器资料采集模块。"""
from .core import Collector, CollectorError
from .store import Store

__all__ = ["Collector", "CollectorError", "Store"]

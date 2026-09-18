"""Celery 应用与任务（worker 侧，API 进程不导入任务模块）。"""

from app.engines.celery.celery_app import celery_app

__all__ = ["celery_app"]

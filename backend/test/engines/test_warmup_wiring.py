"""预热接线测试：worker 与 API 启动时的预热开关（不需要模型）。

预热本体（加载权重 + 跑一次最小编码）在 `test/engines/test_embedder_utils.py` 用替身模型
覆盖；这里只管「什么时候调用」——开关打开才预热，关掉就完全不碰模型（测试与无卡机器都用它）。
"""

from __future__ import annotations

from app.common.config import settings
from app.engines.celery.celery_app import _warmup_embedder as warmup_for_worker
from app.main import _warmup_embedder as warmup_for_api


class _Recorder:
    """替身 `get_embedder`：只记录有没有被调用。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> _Recorder:
        self.calls += 1
        return self

    def warmup(self) -> bool:
        return True


def _patch_get_embedder(monkeypatch) -> _Recorder:
    """替换包属性 `app.engines.embed.get_embedder`（接线处就是从这里导入的）。"""

    recorder = _Recorder()
    monkeypatch.setattr("app.engines.embed.get_embedder", recorder)
    return recorder


def test_worker_warmup_loads_model_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embed_warmup_enabled", True)
    recorder = _patch_get_embedder(monkeypatch)

    warmup_for_worker()

    assert recorder.calls == 1


def test_worker_warmup_skips_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embed_warmup_enabled", False)
    recorder = _patch_get_embedder(monkeypatch)

    warmup_for_worker()

    assert recorder.calls == 0


async def test_api_warmup_loads_model_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embed_warmup_enabled", True)
    recorder = _patch_get_embedder(monkeypatch)

    await warmup_for_api()

    assert recorder.calls == 1


async def test_api_warmup_skips_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embed_warmup_enabled", False)
    recorder = _patch_get_embedder(monkeypatch)

    await warmup_for_api()

    assert recorder.calls == 0

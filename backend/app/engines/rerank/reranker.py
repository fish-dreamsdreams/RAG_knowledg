"""本地 Cross-Encoder 重排与断崖切分（tasklist 10.1 / 10.2，TECH_SPEC §1）。

`bge-reranker-base` / `bge-reranker-v2-m3` 都是 BAAI **另发布的独立 Cross-Encoder 权重**，
与 `bge-m3` embedder 不是同一个模型：不得拿 M3 的余弦相似度当重排分。余弦回答的是「两段文本的向量有多近」，
重排分回答的是「这段正文是否回答了这个问题」——量纲与含义都不同，混用会让断崖阈值
（`min_drop_ratio`）失去意义。

模型严格进程内单例：权重加载比一次前向贵几个数量级，禁止每个请求重新 `CrossEncoder()`。

断崖切分是**纯函数**：重排分降序后找最大落差位置，切点之后不要。判据是**归一化落差比**
`(scores[i-1] - scores[i]) / scores[0]`，不是绝对落差——高分区分数尺度大，绝对落差本身
就有意义；低分区所有分数一起被压低，绝对落差跟着缩水、会永远切不动，只有比例还留有信息
（实测 v2-m3：`0.255 → 0.114` 的绝对落差只有 0.141，「前 4 条强相关」被判成无断崖；换成
落差比是 0.173，切得动，而「全同主题」样本只有 0.091，不会被误切）。归一化到最高分恰好
同时覆盖这两种情形。最大落差比不足 `min_drop_ratio` 视为无断崖、保留 `max_keep`。全部是
确定的算术，没有随机数与模型判断（P6）。
"""

from __future__ import annotations

import gc
import logging
import threading
from collections.abc import Sequence
from typing import Any

from app.common.config import settings
from app.common.model_cache import resolve_model_path
from app.engines.embed import is_oom_error

logger = logging.getLogger(__name__)

# 加载权重最容易撞 OOM：清一次显存缓存再试
_OOM_RETRY_LIMIT = 1
# 归一化落差比的分母下限：`scores[0]` 理论上大于 0，这里只是避免除零
_MIN_SCORE_LIMIT = 1e-6
# 一次前向处理的 (query, passage) 对数：显存占用与它近似线性
DEFAULT_BATCH_SIZE = 16
# bge-reranker-base 的位置上限是 514、v2-m3 是 8194；子块本来就不长，
# 统一按 512 截断既够用又省算力（长文本还会拉长断崖前的重排耗时）
RERANK_MAX_LENGTH = 512


def _resolve_min_keep(value: int | None) -> int:
    return settings.rerank_cliff_min_keep if value is None else value


def _resolve_max_keep(value: int | None) -> int:
    return settings.rerank_cliff_max_keep if value is None else value


def cliff_index(
    scores: Sequence[float],
    *,
    min_keep: int | None = None,
    max_keep: int | None = None,
) -> int | None:
    """返回最大落差所在的切点下标；没有可切的位置时返回 None。

    `scores` 必须已按降序排好。切点只在 `[min_keep, max_keep)` 里找：小于 `min_keep`
    没有意义（至少要保留 `min_keep` 条），达到 `max_keep` 则已被上限截断。

    只看落差的**相对大小**，所以把整列分数缩放不会改变切点位置——这也让它与「是否跨过
    `min_drop_ratio` 阈值」解耦，那个判定属于 `cutoff`。
    """

    low = _resolve_min_keep(min_keep)
    high = _resolve_max_keep(max_keep)
    if len(scores) <= low:
        return None
    ceiling = min(high, len(scores))

    best_index: int | None = None
    best_drop = 0.0
    for index in range(low, ceiling):
        drop = scores[index - 1] - scores[index]
        if drop > best_drop:
            best_drop = drop
            best_index = index
    return best_index


def cutoff(
    scores: Sequence[float],
    *,
    min_keep: int | None = None,
    max_keep: int | None = None,
    min_drop_ratio: float | None = None,
) -> int:
    """按断崖切分决定保留条数（TECH_SPEC §1：落差与落差比）。

    判据是**归一化落差比** `drop / scores[0]`，不是绝对落差：低分候选的绝对落差会被整体
    压低，绝对阈值就会永远切不动。以最高分为分母同时兼顾两端——高分区它近似等于绝对落差，
    低分区它自动把比例放大。

    返回 `k`，满足 `min(min_keep, len(scores)) <= k <= min(max_keep, len(scores))`：
    候选本来就不足 `min_keep` 条时不可能凭空凑数，只能全部保留。
    """

    low = _resolve_min_keep(min_keep)
    high = _resolve_max_keep(max_keep)
    threshold = (
        settings.rerank_cliff_min_ratio if min_drop_ratio is None else min_drop_ratio
    )

    if len(scores) <= low:
        return len(scores)
    ceiling = min(high, len(scores))

    index = cliff_index(scores, min_keep=low, max_keep=high)
    if index is None:
        return ceiling

    top = scores[0] if scores[0] > _MIN_SCORE_LIMIT else _MIN_SCORE_LIMIT
    ratio = (scores[index - 1] - scores[index]) / top
    if ratio < threshold:
        logger.debug(
            "最大落差比 %.4f 未达 %.4f，视为无断崖，保留 %s 条", ratio, threshold, ceiling
        )
        return ceiling
    return index


def rank(
    query: str,
    passages: Sequence[str],
    *,
    reranker: "Reranker | None" = None,
    min_keep: int | None = None,
    max_keep: int | None = None,
    min_drop_ratio: float | None = None,
) -> list[tuple[int, float]]:
    """打分 → 降序（同分按原下标）→ 断崖切分，返回 `(原下标, 分数)`。

    返回原下标而不是正文：调用方要拿它回指召回结果，才能接着做 ACL 过滤与父块回溯。
    同分按原下标升序，保证同一输入永远得到同一顺序。
    """

    if not passages:
        return []

    scores = (reranker or get_reranker()).score(query, passages)
    ordered = sorted(enumerate(scores), key=lambda item: (-item[1], item[0]))
    keep = cutoff(
        [score for _, score in ordered],
        min_keep=min_keep,
        max_keep=max_keep,
        min_drop_ratio=min_drop_ratio,
    )
    return ordered[:keep]


class Reranker:
    """Cross-Encoder 重排模型（进程内单例，见模块 docstring）。"""

    # 与 tasklist 10.2 要求的调用形状一致：`Reranker.cutoff(scores, ...)`
    cutoff = staticmethod(cutoff)
    cliff_index = staticmethod(cliff_index)

    def __init__(self, model_name: str | None = None, *, device: str | None = None) -> None:
        self.model_name = model_name or settings.rerank_model
        # 重排不写向量库，设备可以独立于 embed；留空时跟 M3 同卡（TECH_SPEC §1 已按 8GB 核算）
        self.device = device or settings.rerank_device or settings.embed_device
        self._lock = threading.Lock()
        # 只保护首次加载，与 `_lock` 分离，避免推理期间阻塞加载
        self._load_lock = threading.Lock()
        self._model: Any = None
        self._resolved_path: str | None = None

    @property
    def model_path(self) -> str | None:
        """本地权重目录（加载后可见），便于排查与日志。"""

        return self._resolved_path

    def _load(self) -> Any:
        if self._model is not None:
            return self._model

        # 双重检查：并发首调若不加锁会各自加载一份权重（显存翻倍）
        with self._load_lock:
            if self._model is not None:
                return self._model

            import torch
            from sentence_transformers import CrossEncoder

            device = self.device
            if device.startswith("cuda") and not torch.cuda.is_available():
                logger.warning("重排设备 %s 不可用，回落到 cpu", device)
                device = "cpu"
            self.device = device

            self._resolved_path = resolve_model_path(self.model_name)
            logger.info("加载 Cross-Encoder：%s (device=%s)", self._resolved_path, device)

            for attempt in range(_OOM_RETRY_LIMIT + 1):
                try:
                    self._model = CrossEncoder(
                        self._resolved_path,
                        device=device,
                        max_length=RERANK_MAX_LENGTH,
                    )
                    break
                except RuntimeError as exc:
                    if attempt >= _OOM_RETRY_LIMIT or not is_oom_error(exc):
                        raise
                    logger.warning("加载 Cross-Encoder 显存不足：清缓存后重试", exc_info=True)
                    torch.cuda.empty_cache()
        return self._model

    def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> list[float]:
        """给每个候选打分，返回与 `passages` 等长的分数列表。

        空输入直接返回空列表、**不加载模型**：没有候选时不该付出加载权重的代价。

        分数一定是 `[0, 1]`：两个候选模型的 `config.json` 里都**没有**
        `sbert_ce_default_activation_function`（已实测，`bge-reranker-base` 与
        `bge-reranker-v2-m3` 都是 `None`），`predict` 默认返回的是**原始 logits**
        ——任意实数，相邻落差动辄 5~14。那样断崖阈值会永远成立，
        「无断崖则保留 10」的保护彻底失效。所以这里显式过 Sigmoid，两个模型共用同一套
        阈值语义。
        """

        if not passages:
            return []

        import torch

        model = self._load()
        pairs = [(query, passage) for passage in passages]
        with self._lock:
            scores = model.predict(
                pairs, batch_size=batch_size, activation_fn=torch.nn.Sigmoid()
            )
        return [float(value) for value in scores]


_rerankers: dict[str, Reranker] = {}
# 只保护实例表；权重加载由 `Reranker._load_lock` 负责，两者职责不同就不该合并
_singleton_lock = threading.Lock()


def get_reranker(model_name: str | None = None) -> Reranker:
    """按模型名取进程内单例：同一个模型只加载一份权重。

    以模型名为键、而不是全局单值：系统配置里换了 reranker 之后必须能拿到新模型的实例，
    否则「改配置」永远不会生效，旧实例还会一直占着显存。
    """

    key = model_name or settings.rerank_model
    with _singleton_lock:
        instance = _rerankers.get(key)
        if instance is None:
            instance = Reranker(key)
            _rerankers[key] = instance
        return instance


def reset_rerankers() -> None:
    """丢弃所有已加载实例，换 reranker 配置后调用。

    权重是显存大头，换模型时必须真的释放，否则新旧两份同时压在卡上（8GB 卡尤其明显）。
    """

    with _singleton_lock:
        _rerankers.clear()

    gc.collect()
    try:
        import torch
    except ImportError:  # 没装 torch 的极端环境：清空实例表已经够了
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

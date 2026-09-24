"""重排分数分布与断崖阈值标定（TECH_SPEC §1）。

换 reranker 后 `RERANK_CLIFF_MIN_RATIO` 必须重新标定：判据是**归一化落差比**
`drop / scores[0]`，它的合理取值取决于模型的分数分布（不同模型把"有多相关"压到
哪个区间并不一样）。凭感觉沿用旧值，要么切不动（断崖失去意义），要么切太狠（把有用
候选扔掉）。

用法：

    backend> .venv\\Scripts\\python.exe scripts\\calibrate_rerank.py
    backend> .venv\\Scripts\\python.exe scripts\\calibrate_rerank.py --model BAAI/bge-reranker-base

输出每个问句的降序分数、相邻落差，以及各候选阈值下的保留条数。判读方式：

- 候选里混着明显无关项（第 1 组）→ 期望在选定阈值下切得动，`k < 10`
- 候选全属同一主题、只是相关度深浅不同（第 2、3 组）→ 期望 `k = 10`，即没有断崖

真实语料就绪后，把 `SAMPLES` 换成演示场景的问句与切片即可。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 `python scripts/calibrate_rerank.py` 也能导入 app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.common.config import settings  # noqa: E402
from app.common.model_cache import has_local_weights  # noqa: E402
from app.engines.rerank import Reranker, cliff_index, cutoff  # noqa: E402

THRESHOLDS = (0.04, 0.06, 0.08, 0.10, 0.13, 0.16, 0.20)

SAMPLES: list[tuple[str, list[str]]] = [
    (
        "年假可以休几天？",
        [
            "员工累计工作满 1 年不满 10 年的，年休假 5 天；满 10 年不满 20 年的，年休假 10 天。",
            "年休假天数根据员工累计工作时间确定，累计工作满 20 年的，年休假 15 天。",
            "国家法定休假日、休息日不计入年休假的假期。",
            "员工请年假需提前 3 个工作日在系统提交申请，由直属主管审批。",
            "年休假在 1 个自然年度内可以集中安排，也可以分段安排。",
            "办公用品申领需填写领用单，经部门负责人签字后到行政部领取。",
            "本制度自发布之日起施行，由人力资源部负责解释。",
            "员工离职时应办理工作交接，交回工牌、门禁卡与办公设备。",
            "公司每季度组织一次消防安全演练，全体员工须参加。",
            "差旅费报销需提供发票原件与行程单，超标部分自理。",
            "会议室使用需提前在系统预约，会议结束后关闭投影设备。",
            "新员工入职满 30 日后方可申请内部转岗。",
        ],
    ),
    (
        "报销需要哪些材料？",
        [
            "差旅费报销需填写报销单，附发票原件、行程单与审批记录。",
            "费用发生后 30 个自然日内提交报销，超期需部门负责人书面说明。",
            "单笔超过 5000 元的费用需附合同或采购申请单。",
            "交通费按规定标准乘坐，超标准部分由个人承担。",
            "住宿费按城市等级限额报销，凭住宿发票据实列支。",
            "报销单据需经部门负责人与财务复核后付款。",
            "业务招待费需提前申请，注明接待对象与事由。",
            "发票抬头须为公司全称，个人抬头不予报销。",
            "电子发票需提供原件 PDF，截图不作为报销凭证。",
            "差旅期间的市内交通费并入差旅费一并报销。",
            "费用报销系统每月 25 日后停止受理当月单据。",
            "员工可通过自助系统查询报销进度与打款状态。",
        ],
    ),
    (
        "入职需要办什么手续？",
        [
            "新员工入职首日需提交身份证、学历证明与体检报告。",
            "入职当日签订劳动合同，并在系统中完成个人信息登记。",
            "由人力资源部安排入职培训，包括制度宣讲与安全须知。",
            "工牌、门禁卡与办公设备在入职三日内发放。",
            "试用期为 3 个月，试用期满前完成转正评估。",
            "社保与公积金自入职当月起缴，需提供参保所需材料。",
            "新员工需在系统中设置紧急联系人信息。",
            "入职满 30 日后可申请内部转岗与培训名额。",
            "离职员工需交回工牌与设备，并完成工作交接。",
            "公司实行每周五天工作制，具体作息以部门通知为准。",
            "员工可申请弹性上下班，需提前与主管沟通。",
            "年度体检安排在每年第三季度，具体时间另行通知。",
        ],
    ),
]


def report(model: str) -> None:
    reranker = Reranker(model)
    print(f"重排序模型：{model}\n")

    for question, passages in SAMPLES:
        scores = reranker.score(question, passages)
        order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
        ordered = [scores[index] for index in order]
        drops = [
            ordered[index - 1] - ordered[index] for index in range(1, len(ordered))
        ]

        print(f"问句：{question}")
        print("  降序分数：" + " ".join(f"{value:.3f}" for value in ordered))
        print("  相邻落差：" + " ".join(f"{value:.3f}" for value in drops))
        top = ordered[0] if ordered[0] > 1e-9 else 1e-9
        print("  落差比：" + " ".join(f"{value / top:.3f}" for value in drops))

        index = cliff_index(ordered)
        if index is None:
            print("  最大落差：无（候选数不够，未进入 [min_keep, max_keep) 区间）")
        else:
            print(
                f"  最大落差：{drops[index - 1]:.4f}，切在下标 {index}"
                f"（第 {index} 与第 {index + 1} 名之间）"
            )

        row = "  ".join(
            f"{threshold:.2f}->k={cutoff(ordered, min_drop_ratio=threshold)}"
            for threshold in THRESHOLDS
        )
        print(f"  阈值扫描：{row}\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="重排分数分布与断崖阈值标定")
    parser.add_argument(
        "--model",
        default=settings.rerank_model,
        help=f"reranker 模型名，默认取配置值（{settings.rerank_model}）",
    )
    args = parser.parse_args(argv[1:])

    if not has_local_weights(args.model):
        print(
            f"本地缺少 {args.model} 权重。先跑：\n"
            f"  .venv\\Scripts\\python.exe scripts\\fetch_models.py {args.model}",
            file=sys.stderr,
        )
        return 1

    report(args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

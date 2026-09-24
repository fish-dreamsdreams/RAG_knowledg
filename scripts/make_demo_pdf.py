"""把演示用的 Markdown 制度文档渲染成 PDF（供 MinerU 解析链路使用）。

运行（一次性环境，不写入项目依赖）：
    uv run --with reportlab python scripts/make_demo_pdf.py

默认把 data/knowledge/scenario-a/财务报销制度.md 渲染为同名 PDF。
中文字体使用 reportlab 内置的 CID 字体 STSong-Light，无需字体文件。
"""

import argparse
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib import colors

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "knowledge" / "scenario-a" / "财务报销制度.md"
FONT_NAME = "STSong-Light"


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle("title", fontName=FONT_NAME, fontSize=18, leading=26, alignment=1),
        "meta": ParagraphStyle("meta", fontName=FONT_NAME, fontSize=10, leading=16),
        "heading": ParagraphStyle(
            "heading", fontName=FONT_NAME, fontSize=13, leading=20, spaceBefore=10, spaceAfter=4
        ),
        "body": ParagraphStyle(
            "body", fontName=FONT_NAME, fontSize=11, leading=18, firstLineIndent=22, spaceAfter=3
        ),
    }


def _table(rows: list[list[str]]) -> Table:
    table = Table(rows, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def build_flowables(markdown: str, styles: dict[str, ParagraphStyle]) -> list:
    flowables: list = []
    table_rows: list[list[str]] = []

    def flush_table() -> None:
        if table_rows:
            flowables.append(_table(table_rows.copy()))
            flowables.append(Spacer(1, 6))
            table_rows.clear()

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            # 跳过 |---|---| 分隔行
            if all(set(cell) <= {"-", ":"} and cell for cell in cells):
                continue
            table_rows.append(cells)
            continue

        flush_table()

        if not line:
            continue
        if line.startswith("# "):
            flowables.append(Paragraph(line[2:], styles["title"]))
            flowables.append(Spacer(1, 10))
        elif line.startswith("## "):
            flowables.append(Paragraph(line[3:], styles["heading"]))
        elif line.startswith("- "):
            flowables.append(Paragraph(line[2:], styles["meta"]))
        else:
            flowables.append(Paragraph(line, styles["body"]))

    flush_table()
    return flowables


def render(source: Path, output: Path) -> None:
    pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))
    styles = _styles()
    document = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=source.stem,
    )
    document.build(build_flowables(source.read_text(encoding="utf-8"), styles))
    print(f"{source.relative_to(ROOT)} -> {output.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="渲染演示制度文档为 PDF")
    parser.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("output", nargs="?", type=Path)
    args = parser.parse_args()
    render(args.source, args.output or args.source.with_suffix(".pdf"))


if __name__ == "__main__":
    main()

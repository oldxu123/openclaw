#!/usr/bin/env python3
"""为 PDF 添加目录侧栏，并校验原始内容未被修改。

功能：
1. 分析并提取 PDF 的目录结构（优先使用已有书签；否则使用版面启发式识别标题）。
2. 生成新 PDF：在每页右侧新增目录列，左侧原页面内容保持不变。
3. 校验新旧 PDF 一致性：比对新 PDF 去掉目录列后的内容，确保与原 PDF 100% 一致。

依赖：
    pip install pymupdf
"""

from __future__ import annotations

import argparse
import hashlib
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence

import fitz  # PyMuPDF


@dataclass
class TocEntry:
    level: int
    title: str
    page: int  # 1-based page number


def normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def extract_toc_from_bookmarks(doc: fitz.Document) -> List[TocEntry]:
    toc_raw = doc.get_toc(simple=True)  # [[level, title, page], ...]
    entries: List[TocEntry] = []
    for item in toc_raw:
        if len(item) < 3:
            continue
        level, title, page = int(item[0]), str(item[1]), int(item[2])
        title = normalize_title(title)
        if title and page > 0:
            entries.append(TocEntry(level=level, title=title, page=page))
    return entries


def extract_toc_heuristic(doc: fitz.Document, max_entries: int = 150) -> List[TocEntry]:
    """启发式提取标题：依据字号、粗体、短文本和编号模式。"""
    candidates = []
    font_sizes: List[float] = []

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        blocks = page.get_text("dict").get("blocks", [])
        for block in blocks:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = normalize_title(span.get("text", ""))
                    if not text:
                        continue
                    size = float(span.get("size", 0))
                    font = str(span.get("font", "")).lower()
                    flags = int(span.get("flags", 0))
                    if len(text) > 120:
                        continue
                    font_sizes.append(size)
                    candidates.append(
                        {
                            "page": page_idx + 1,
                            "text": text,
                            "size": size,
                            "bold": ("bold" in font) or bool(flags & 2**4),
                        }
                    )

    if not candidates:
        return []

    median_size = statistics.median(font_sizes)
    size_threshold = max(median_size * 1.2, statistics.quantiles(font_sizes, n=5)[-1] if len(font_sizes) >= 5 else median_size)
    heading_pattern = re.compile(r"^(第?[0-9一二三四五六七八九十百千]+[章节部分卷篇节]|[0-9]+(\.[0-9]+){0,3}|[A-Z][0-9]?)")

    filtered = []
    for c in candidates:
        looks_numbered = bool(heading_pattern.match(c["text"]))
        if c["size"] >= size_threshold or (c["bold"] and looks_numbered) or (looks_numbered and c["size"] >= median_size * 1.05):
            filtered.append(c)

    if not filtered:
        return []

    unique = []
    seen = set()
    for c in filtered:
        key = (c["page"], c["text"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
        if len(unique) >= max_entries:
            break

    sorted_sizes = sorted({round(item["size"], 1) for item in unique}, reverse=True)
    level_map = {size: idx + 1 for idx, size in enumerate(sorted_sizes[:6])}

    entries: List[TocEntry] = []
    for item in unique:
        size_key = round(item["size"], 1)
        level = level_map.get(size_key, min(len(level_map) + 1, 6))
        entries.append(TocEntry(level=level, title=item["text"], page=item["page"]))

    return entries


def extract_toc(doc: fitz.Document) -> List[TocEntry]:
    entries = extract_toc_from_bookmarks(doc)
    if entries:
        return entries
    return extract_toc_heuristic(doc)


def wrap_line(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    out = []
    cur = []
    cur_len = 0
    for ch in text:
        cur.append(ch)
        cur_len += 1
        if cur_len >= limit:
            out.append("".join(cur))
            cur = []
            cur_len = 0
    if cur:
        out.append("".join(cur))
    return out


def render_toc_lines(entries: Sequence[TocEntry], line_width: int = 30, max_lines: int = 220) -> List[str]:
    lines: List[str] = ["目录"]
    for e in entries:
        indent = "  " * max(e.level - 1, 0)
        line = f"{indent}{e.title} ...... {e.page}"
        for wrapped in wrap_line(line, line_width):
            lines.append(wrapped)
        if len(lines) >= max_lines:
            lines.append("... (目录过长已截断)")
            break
    return lines


def add_toc_column(
    input_pdf: Path,
    output_pdf: Path,
    toc_width: float = 180.0,
    margin: float = 12.0,
    font_size: float = 9.0,
) -> List[TocEntry]:
    src = fitz.open(str(input_pdf))
    try:
        toc_entries = extract_toc(src)
        toc_lines = render_toc_lines(toc_entries)
        toc_text = "\n".join(toc_lines)

        out = fitz.open()
        for page_index in range(len(src)):
            p = src[page_index]
            new_w = p.rect.width + toc_width
            new_h = p.rect.height
            np = out.new_page(width=new_w, height=new_h)

            # 左侧原始页面：按原尺寸原位置拷贝
            src_rect = fitz.Rect(0, 0, p.rect.width, p.rect.height)
            np.show_pdf_page(src_rect, src, page_index)

            # 右侧目录列
            x0 = p.rect.width
            x1 = new_w
            np.draw_line(
                fitz.Point(x0, 0),
                fitz.Point(x0, new_h),
                width=0.8,
                color=(0.75, 0.75, 0.75),
            )
            toc_rect = fitz.Rect(x0 + margin, margin, x1 - margin, new_h - margin)
            np.insert_textbox(
                toc_rect,
                toc_text,
                fontsize=font_size,
                fontname="helv",
                color=(0.1, 0.1, 0.1),
                align=0,
            )

        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        out.save(str(output_pdf), garbage=4, deflate=True)
        out.close()
        return toc_entries
    finally:
        src.close()


def _page_sha256(pix: fitz.Pixmap) -> str:
    return hashlib.sha256(pix.samples).hexdigest()


def verify_without_toc(
    input_pdf: Path,
    output_pdf: Path,
    toc_width: float,
    dpi: int = 144,
) -> tuple[bool, str]:
    """验证新 PDF 去掉目录列后与原文件内容是否完全一致。"""
    src = fitz.open(str(input_pdf))
    new = fitz.open(str(output_pdf))
    try:
        if len(src) != len(new):
            return False, f"页数不一致: old={len(src)}, new={len(new)}"

        mat = fitz.Matrix(dpi / 72, dpi / 72)
        for i in range(len(src)):
            p_old = src[i]
            p_new = new[i]

            expected_w = p_old.rect.width + toc_width
            if abs(p_new.rect.width - expected_w) > 0.5 or abs(p_new.rect.height - p_old.rect.height) > 0.5:
                return False, f"第 {i+1} 页尺寸不匹配: old=({p_old.rect.width:.2f},{p_old.rect.height:.2f}), new=({p_new.rect.width:.2f},{p_new.rect.height:.2f})"

            old_pix = p_old.get_pixmap(matrix=mat, alpha=False)
            clip = fitz.Rect(0, 0, p_old.rect.width, p_old.rect.height)
            new_pix = p_new.get_pixmap(matrix=mat, clip=clip, alpha=False)

            if (old_pix.width != new_pix.width) or (old_pix.height != new_pix.height):
                return False, f"第 {i+1} 页栅格尺寸不一致"

            if _page_sha256(old_pix) != _page_sha256(new_pix):
                return False, f"第 {i+1} 页内容不一致（去除目录列后）"

        return True, "验证通过：去除目录列后与原文档逐页 100% 一致。"
    finally:
        src.close()
        new.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为 PDF 添加目录侧栏并验证内容一致性")
    parser.add_argument("input", type=Path, help="输入 PDF 路径")
    parser.add_argument("output", type=Path, help="输出 PDF 路径")
    parser.add_argument("--toc-width", type=float, default=180.0, help="目录列宽度（pt），默认 180")
    parser.add_argument("--margin", type=float, default=12.0, help="目录列内边距（pt），默认 12")
    parser.add_argument("--font-size", type=float, default=9.0, help="目录字体大小，默认 9")
    parser.add_argument("--skip-verify", action="store_true", help="跳过输出与原文档一致性验证")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if not args.input.exists():
        print(f"[ERROR] 输入文件不存在: {args.input}", file=sys.stderr)
        return 2

    entries = add_toc_column(
        input_pdf=args.input,
        output_pdf=args.output,
        toc_width=args.toc_width,
        margin=args.margin,
        font_size=args.font_size,
    )

    print(f"[INFO] 目录提取完成: {len(entries)} 条")
    if entries:
        for e in entries[:10]:
            print(f"  - L{e.level} P{e.page}: {e.title}")
        if len(entries) > 10:
            print("  - ...")
    else:
        print("[WARN] 未提取到目录项，仍已生成仅含‘目录’标题的侧栏。")

    if not args.skip_verify:
        ok, msg = verify_without_toc(args.input, args.output, toc_width=args.toc_width)
        if ok:
            print(f"[OK] {msg}")
        else:
            print(f"[FAIL] {msg}", file=sys.stderr)
            return 3

    print(f"[DONE] 输出文件: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

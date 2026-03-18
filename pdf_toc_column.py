#!/usr/bin/env python3
"""为 PDF 添加目录侧栏，并校验原始内容未被修改。"""

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
    page: int  # 1-based


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def compact_for_match(text: str) -> str:
    """用于“目录标题 vs 正文”匹配：仅保留中英文与数字。"""
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", text)).lower()


def roman_to_int(token: str) -> int | None:
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    token = token.upper()
    if not token or any(ch not in vals for ch in token):
        return None
    total = 0
    prev = 0
    for ch in reversed(token):
        cur = vals[ch]
        if cur < prev:
            total -= cur
        else:
            total += cur
            prev = cur
    return total if total > 0 else None


def title_level_from_numbering(title: str) -> int | None:
    t = title.strip()
    m = re.match(r"^(\d+(?:\.\d+){0,7})\b", t)
    if m:
        return min(m.group(1).count(".") + 1, 8)

    if re.match(r"^第[0-9一二三四五六七八九十百千两]+章", t):
        return 1
    if re.match(r"^第[0-9一二三四五六七八九十百千两]+节", t):
        return 2
    if re.match(r"^第[0-9一二三四五六七八九十百千两]+[部分卷篇]", t):
        return 1

    return None


def extract_toc_from_bookmarks(doc: fitz.Document) -> List[TocEntry]:
    entries: List[TocEntry] = []
    for item in doc.get_toc(simple=True):
        if len(item) < 3:
            continue
        level, title, page = int(item[0]), str(item[1]).strip(), int(item[2])
        if title and page > 0:
            entries.append(TocEntry(level=max(level, 1), title=title, page=page))
    return entries


def parse_toc_line(raw: str) -> tuple[str, int] | None:
    line = raw.rstrip()
    if not line:
        return None

    # 典型目录行：标题 ...... 12
    m = re.match(r"^(?P<title>.+?)\s*(?:\.{2,}|…{2,}|·{2,}|-{2,})\s*(?P<page>\d{1,4}|[ivxlcdmIVXLCDM]{1,10})\s*$", line)
    if not m:
        # 次常见：标题 12
        m = re.match(r"^(?P<title>.+?)\s+(?P<page>\d{1,4}|[ivxlcdmIVXLCDM]{1,10})\s*$", line)
        if not m:
            return None

    title = m.group("title").rstrip(" .·…-")
    page_token = m.group("page")
    page = int(page_token) if page_token.isdigit() else (roman_to_int(page_token) or 0)

    if not title or page <= 0:
        return None
    return title, page


def extract_toc_from_toc_pages(doc: fitz.Document, max_scan_pages: int = 40) -> List[TocEntry]:
    entries: List[TocEntry] = []
    toc_page_indexes = []

    scan_n = min(len(doc), max_scan_pages)
    for i in range(scan_n):
        page = doc[i]
        raw_text = page.get_text("text")
        # 在页面出现“目录/contents”时认为可能是目录页
        if re.search(r"(^|\n)\s*(目录|目\s*录|contents?)\s*(\n|$)", raw_text, flags=re.IGNORECASE):
            toc_page_indexes.append(i)

    for i in toc_page_indexes:
        page = doc[i]
        blocks = page.get_text("dict").get("blocks", [])
        line_items = []

        for block in blocks:
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(span.get("text", "") for span in spans)
                x0 = min(float(span.get("bbox", [0, 0, 0, 0])[0]) for span in spans)
                line_items.append((x0, text.rstrip()))

        if not line_items:
            continue

        min_x = min(x for x, _ in line_items)
        x_levels = sorted({round((x - min_x) / 12) for x, _ in line_items})

        for x0, raw in line_items:
            parsed = parse_toc_line(raw)
            if not parsed:
                continue
            title, page_num = parsed

            indent_steps = round((x0 - min_x) / 12)
            indent_level = x_levels.index(indent_steps) + 1 if indent_steps in x_levels else 1
            num_level = title_level_from_numbering(title)
            level = num_level if num_level is not None else indent_level

            entries.append(TocEntry(level=max(level, 1), title=title, page=page_num))

    # 去重：保持先后顺序
    dedup: List[TocEntry] = []
    seen = set()
    for e in entries:
        key = (e.level, e.title, e.page)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(e)
    return dedup


def extract_toc_heuristic(doc: fitz.Document, max_entries: int = 200) -> List[TocEntry]:
    """兜底策略：按字号/粗体/编号识别标题。"""
    candidates = []
    sizes: List[float] = []

    for pno in range(len(doc)):
        blocks = doc[pno].get_text("dict").get("blocks", [])
        for block in blocks:
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = normalize_spaces("".join(span.get("text", "") for span in spans))
                if not text or len(text) > 120:
                    continue
                size = max(float(span.get("size", 0)) for span in spans)
                font = " ".join(str(span.get("font", "")).lower() for span in spans)
                sizes.append(size)
                candidates.append(
                    {
                        "page": pno + 1,
                        "text": text,
                        "size": size,
                        "bold": "bold" in font,
                    }
                )

    if not candidates:
        return []

    median_size = statistics.median(sizes)
    q80 = statistics.quantiles(sizes, n=5)[-1] if len(sizes) >= 5 else median_size
    threshold = max(median_size * 1.2, q80)

    entries: List[TocEntry] = []
    seen = set()
    for c in candidates:
        num_level = title_level_from_numbering(c["text"])
        is_heading = c["size"] >= threshold or (c["bold"] and num_level is not None)
        if not is_heading:
            continue
        level = num_level if num_level is not None else 1
        key = (level, c["text"], c["page"])
        if key in seen:
            continue
        seen.add(key)
        entries.append(TocEntry(level=level, title=c["text"], page=c["page"]))
        if len(entries) >= max_entries:
            break

    return entries


def extract_toc(doc: fitz.Document) -> List[TocEntry]:
    """按可靠性顺序提取目录：书签 > 目录页 > 启发式。"""
    for fn in (extract_toc_from_bookmarks, extract_toc_from_toc_pages, extract_toc_heuristic):
        entries = fn(doc)
        if entries:
            return entries
    return []


def validate_toc_against_document(doc: fitz.Document, entries: Sequence[TocEntry]) -> list[TocEntry]:
    """检查目录标题是否能在对应页附近正文中找到，返回不匹配项。"""
    page_text_compact = [compact_for_match(doc[i].get_text("text")) for i in range(len(doc))]
    unmatched: list[TocEntry] = []

    for e in entries:
        needle = compact_for_match(e.title)
        if not needle:
            unmatched.append(e)
            continue

        center = min(max(e.page - 1, 0), len(doc) - 1)
        window = range(max(0, center - 1), min(len(doc), center + 2))
        if any(needle in page_text_compact[idx] for idx in window):
            continue
        unmatched.append(e)

    return unmatched


def wrap_line(text: str, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], []
    for ch in text:
        cur.append(ch)
        if len(cur) >= limit:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def render_toc_lines(entries: Sequence[TocEntry], line_width: int = 30, max_lines: int = 240) -> List[str]:
    lines = ["目录"]
    for e in entries:
        indent = "  " * max(e.level - 1, 0)
        line = f"{indent}{e.title} ...... {e.page}"
        lines.extend(wrap_line(line, line_width))
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
    allow_unmatched_toc: bool = False,
) -> List[TocEntry]:
    src = fitz.open(str(input_pdf))
    try:
        toc_entries = extract_toc(src)
        unmatched = validate_toc_against_document(src, toc_entries) if toc_entries else []
        if unmatched and not allow_unmatched_toc:
            sample = "\n".join(f"- L{e.level} P{e.page}: {e.title}" for e in unmatched[:8])
            raise ValueError(
                "检测到部分目录项与正文未匹配（默认严格模式会中断）。\n"
                f"不匹配示例:\n{sample}\n"
                "如需忽略，请加参数 --allow-unmatched-toc。"
            )

        toc_text = "\n".join(render_toc_lines(toc_entries))

        out = fitz.open()
        for pno in range(len(src)):
            p = src[pno]
            np = out.new_page(width=p.rect.width + toc_width, height=p.rect.height)

            src_rect = fitz.Rect(0, 0, p.rect.width, p.rect.height)
            np.show_pdf_page(src_rect, src, pno)

            x0, x1 = p.rect.width, p.rect.width + toc_width
            np.draw_line(fitz.Point(x0, 0), fitz.Point(x0, p.rect.height), width=0.8, color=(0.75, 0.75, 0.75))
            np.insert_textbox(
                fitz.Rect(x0 + margin, margin, x1 - margin, p.rect.height - margin),
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


def _sha(pix: fitz.Pixmap) -> str:
    return hashlib.sha256(pix.samples).hexdigest()


def verify_without_toc(input_pdf: Path, output_pdf: Path, toc_width: float, dpi: int = 144) -> tuple[bool, str]:
    src = fitz.open(str(input_pdf))
    new = fitz.open(str(output_pdf))
    try:
        if len(src) != len(new):
            return False, f"页数不一致: old={len(src)}, new={len(new)}"

        mat = fitz.Matrix(dpi / 72, dpi / 72)
        for i in range(len(src)):
            p_old, p_new = src[i], new[i]
            if abs(p_new.rect.width - (p_old.rect.width + toc_width)) > 0.5 or abs(p_new.rect.height - p_old.rect.height) > 0.5:
                return False, f"第 {i+1} 页尺寸不匹配"

            old_pix = p_old.get_pixmap(matrix=mat, alpha=False)
            new_pix = p_new.get_pixmap(matrix=mat, clip=fitz.Rect(0, 0, p_old.rect.width, p_old.rect.height), alpha=False)
            if (old_pix.width != new_pix.width) or (old_pix.height != new_pix.height):
                return False, f"第 {i+1} 页栅格尺寸不一致"
            if _sha(old_pix) != _sha(new_pix):
                return False, f"第 {i+1} 页内容不一致（去除目录列后）"

        return True, "验证通过：去除目录列后与原文档逐页 100% 一致。"
    finally:
        src.close()
        new.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="为 PDF 添加目录侧栏并验证一致性")
    p.add_argument("input", type=Path, help="输入 PDF")
    p.add_argument("output", type=Path, help="输出 PDF")
    p.add_argument("--toc-width", type=float, default=180.0, help="目录列宽度（pt）")
    p.add_argument("--margin", type=float, default=12.0, help="目录列边距（pt）")
    p.add_argument("--font-size", type=float, default=9.0, help="目录字体大小")
    p.add_argument("--allow-unmatched-toc", action="store_true", help="允许目录项与正文不完全匹配（默认严格校验）")
    p.add_argument("--skip-verify", action="store_true", help="跳过输出内容一致性验证")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if not args.input.exists():
        print(f"[ERROR] 输入文件不存在: {args.input}", file=sys.stderr)
        return 2

    try:
        entries = add_toc_column(
            input_pdf=args.input,
            output_pdf=args.output,
            toc_width=args.toc_width,
            margin=args.margin,
            font_size=args.font_size,
            allow_unmatched_toc=args.allow_unmatched_toc,
        )
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 4

    print(f"[INFO] 目录提取完成: {len(entries)} 条")
    for e in entries[:12]:
        print(f"  - L{e.level} P{e.page}: {e.title}")
    if len(entries) > 12:
        print("  - ...")

    if not args.skip_verify:
        ok, msg = verify_without_toc(args.input, args.output, toc_width=args.toc_width)
        if not ok:
            print(f"[FAIL] {msg}", file=sys.stderr)
            return 3
        print(f"[OK] {msg}")

    print(f"[DONE] 输出文件: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

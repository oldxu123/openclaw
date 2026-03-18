# openclaw

## PDF 目录侧栏脚本

仓库提供 `pdf_toc_column.py`，用于：

1. 识别并提取 PDF 目录结构（优先读取书签，否则用启发式识别标题）。
2. 在**不修改原页面内容**的前提下，为每一页右侧新增“目录列”。
3. 生成新 PDF 后，自动把新 PDF 去掉目录列再与原 PDF 做逐页对比，要求 100% 一致。

## 安装依赖

```bash
pip install pymupdf
```

## 使用方式

```bash
python pdf_toc_column.py input.pdf output_with_toc.pdf
```

常用参数：

- `--toc-width 180`：目录列宽度（pt）
- `--margin 12`：目录列边距（pt）
- `--font-size 9`：目录字体大小
- `--skip-verify`：跳过一致性验证（默认会验证）

示例：

```bash
python pdf_toc_column.py demo.pdf demo_with_toc.pdf --toc-width 200
```

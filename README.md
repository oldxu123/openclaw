# openclaw

## PDF 目录侧栏脚本

仓库提供 `pdf_toc_column.py`，用于：

1. 提取 PDF 目录结构，按可靠性顺序：**书签 > 目录页识别 > 启发式标题识别**。
2. 在**不修改原页面内容**的前提下，为每页右侧新增目录列。
3. 生成新 PDF 后，把目录列去除并与原 PDF 逐页像素比对，保证内容 100% 一致。
4. 默认开启“目录与正文严格匹配”检查，确保目录标题与正文内容一致。

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
- `--allow-unmatched-toc`：允许个别目录项无法在正文中匹配（默认严格，不建议）
- `--skip-verify`：跳过新旧 PDF 一致性验证（默认会验证）

示例：

```bash
python pdf_toc_column.py demo.pdf demo_with_toc.pdf --toc-width 200
```

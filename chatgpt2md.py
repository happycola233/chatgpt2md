#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
ChatGPT 导出 JSON -> Markdown（内嵌 HTML 样式）转换器
=====================================================

本脚本面向 “ChatGPT 导出 JSON（mapping + current_node）” 的结构，
将对话按最终态分支（root -> current_node）导出为一个 Markdown 文件，
并在 Markdown 中嵌入少量 HTML（h1、div、details）以实现更美观的展示。

----------------------------------------------------------------------
✅ 输出排版要求
----------------------------------------------------------------------
1) 每条 User 消息输出：

<h1 style="color: #2e86de;">🧑‍💻 User Prompt</h1>
<div ...> 仅包含时间 </div>
  （空 1 行：该行必须为两个空格 "  "）
用户正文（可能包含图片占位符）
  （空 3 行：每行两个空格 "  "）

2) 每条 AI 消息输出：

<h1 style="color: #10ac84;">🤖 AI Response</h1>
<div ...> 包含模型徽章 + 时间 </div>

<details>...推理块...</details>   （若存在 thoughts/code/tool-output/recap）
  （空 1 行：两个空格 "  "）
AI 正文（最终回复）
  （空 3 行：每行两个空格 "  "）

3) 所有“空行”都必须是一整行两个空格 "  "，防止渲染器压缩掉空行。

----------------------------------------------------------------------
✅ 推理合并规则（ReasoningSession）
----------------------------------------------------------------------
- 收集同一轮推理中的：
  - content_type == "thoughts" 的多段 thoughts（summary + content）
  - assistant 的 content_type == "code"
  - tool(name="python") 的 execution_output（与最近一段未配对的 code 绑定）
  - content_type == "reasoning_recap"（例如“已思考 1m 7s”）
- 当遇到下一条 assistant 的“最终文本”（text / multimodal_text）时：
  - 若会话中有推理信息，则生成一个 <details> 折叠块插入在 AI 正文之前
  - 子项按时间升序输出
- ✅ 修复关键 bug：blockquote 引用中空行必须也加 `> `，否则引用会在空行处断裂，
  进而导致“引用内的代码围栏”被打断。

----------------------------------------------------------------------
✅ 图片占位符（方案 A）
----------------------------------------------------------------------
- 对 user/assistant 的 multimodal_text.parts 中的 image_asset_pointer：
  - 以原顺序插入一个“可读提示行 + HTML 注释占位符”
  - 便于你后续通过脚本把注释替换为真正的远程 URL（例如 CDN、对象存储等）

占位符示例：
🖼️ Image 1: some.png
<!--CHATGPT_IMG kind="attachment" id="file_xxx" name="some.png" w="1032" h="2048" src="sediment://file_xxx"-->

----------------------------------------------------------------------
✅ 模型识别规则（每条 AI Response）
----------------------------------------------------------------------
- 优先取该消息 message.metadata.model_slug
- 其次 message.metadata.default_model_slug
- 再其次对话顶层 default_model_slug
- 找不到则 "unknown-model"

----------------------------------------------------------------------
🧑‍💻 使用方式
----------------------------------------------------------------------
1) 交互式（无参数）：
   python chatgpt2md.py

2) 最简：
   python chatgpt2md.py input.json

3) 指定输出：
   python chatgpt2md.py input.json output.md

4) 选项：
   python chatgpt2md.py -i input.json -o output.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


# ==============================================================================
# 1) “防压缩空行”常量
# ------------------------------------------------------------------------------
# 很多 Markdown 渲染器会“吞掉”连续空行。
# 空行必须是“一整行两个空格”，这样渲染器会把它当作有效内容保留下来。
# ==============================================================================
BLANK = "  "                          # 单个“空行占位”
TRIPLE_BLANK = [BLANK, BLANK, BLANK]  # 三行空行占位
_INTERNAL_CITATION_TOKEN_RE = re.compile(r"(?:cite|filecite).*?")


# ==============================================================================
# 2) 时间格式化 / HTML 最小转义 / 多行 block 输出
# ==============================================================================

def format_time(create_time: Optional[float]) -> str:
    """
    将 create_time（Unix 时间戳：秒/浮点秒）格式化为 YYYY-MM-DD HH:MM:SS。
    None 或异常 -> "未知时间"
    """
    if create_time is None:
        return "未知时间"
    try:
        dt = datetime.fromtimestamp(float(create_time))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知时间"


def _html_escape(s: str) -> str:
    """
    最小 HTML 转义：避免模型名、时间等字符串里出现 < > & " 影响 HTML 结构。
    """
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _extend_block(lines: List[str], block: str) -> None:
    """
    把一个“多行字符串”按行拆开追加到 lines。
    注意：这里不额外插空行，完全由调用者控制排版。
    """
    if block is None:
        return
    for ln in str(block).splitlines():
        lines.append(ln)


# ==============================================================================
# 2.5) 引用标记（cite...）转标准 Markdown 引用
# ------------------------------------------------------------------------------
# 目标：
# - 将正文里的内部引用标记替换为：([Attribution][n])
# - 在文末生成：[n]: URL "Title"
# ==============================================================================
@dataclass
class _CitationEntry:
    index: int
    target: str
    title: str = ""
    attribution: str = ""


class CitationRegistry:
    """
    维护“Markdown link target -> 引用编号”的稳定映射，支持去重与文末引用定义渲染。
    """
    def __init__(self, start_index: int = 1) -> None:
        self._entries: List[_CitationEntry] = []
        self._target_to_pos: Dict[str, int] = {}
        self._start_index = max(1, int(start_index))

    def add(self, *, target: str = "", url: str = "", title: str = "", attribution: str = "") -> Optional[int]:
        target = str(target or url or "").strip()
        if not target:
            return None

        if target in self._target_to_pos:
            pos = self._target_to_pos[target]
            ent = self._entries[pos]
            if not ent.title and title:
                ent.title = str(title).strip()
            if not ent.attribution and attribution:
                ent.attribution = str(attribution).strip()
            return ent.index

        idx = self._start_index + len(self._entries)
        self._target_to_pos[target] = len(self._entries)
        self._entries.append(_CitationEntry(
            index=idx,
            target=target,
            title=str(title or "").strip(),
            attribution=str(attribution or "").strip(),
        ))
        return idx

    def render_footnotes(self) -> List[str]:
        lines: List[str] = []
        for ent in self._entries:
            target = _format_markdown_link_target(ent.target)
            if ent.title:
                title = ent.title.replace('"', r'\"')
                lines.append(f'[{ent.index}]: {target} "{title}"')
            else:
                lines.append(f"[{ent.index}]: {target}")
        return lines

    def count(self) -> int:
        return len(self._entries)


def _format_markdown_link_target(target: str) -> str:
    """
    Markdown 引用定义里的目标地址。
    普通 URL 原样输出；带空格/括号等字符时用 <...> 包起来，避免文件名类目标破坏语法。
    """
    target = str(target or "").strip()
    if not target:
        return target
    if any(ch.isspace() for ch in target) or any(ch in target for ch in '<>()"'):
        return f"<{target.replace('>', r'\>')}>"
    return target


def _format_markdown_link_label(label: str) -> str:
    """转义引用标签里的方括号，避免文件名/来源名破坏 Markdown 链接语法。"""
    return str(label or "Source").replace("\\", "\\\\").replace("[", r"\[").replace("]", r"\]")


def _domain_fallback_label(url: str) -> str:
    host = (urlparse(url or "").netloc or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "Source"


def _extract_primary_source_from_ref(ref: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    从 content_references 单条记录里提取“主来源”：
    返回 (url, title, attribution)；取不到则返回空字符串。
    """
    items = ref.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            title = str(item.get("title") or "").strip()
            attribution = str(item.get("attribution") or "").strip()
            return url, title, attribution

    safe_urls = ref.get("safe_urls")
    if isinstance(safe_urls, list):
        for u in safe_urls:
            if isinstance(u, str) and u.strip():
                return u.strip(), "", ""

    alt = str(ref.get("alt") or "").strip()
    # 兼容 alt 形如：([OpenAI开发者平台](https://...))
    m = re.search(r"\(\[([^\]]+)\]\((https?://[^)]+)\)\)", alt)
    if m:
        return m.group(2).strip(), "", m.group(1).strip()

    return "", "", ""


def _extract_file_source_from_ref(ref: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    从 content_references 的 type == "file" 记录里提取 Markdown 引用信息：
    返回 (target, title, label)。

    ChatGPT 的文件引用常常没有公开 URL，因此用优先级：
    - cloud_doc_url：若存在，直接作为可点击目标
    - sediment://file_xxx：保留稳定内部文件 id，便于后续二次处理
    - 文件名：最后兜底，仍能形成合法 Markdown 引用定义
    """
    name = str(ref.get("name") or "").strip()
    file_id = str(ref.get("id") or "").strip()
    cloud_doc_url = str(ref.get("cloud_doc_url") or "").strip()

    if cloud_doc_url:
        target = cloud_doc_url
    elif file_id:
        target = f"sediment://{file_id}"
    else:
        target = name

    label = name or file_id or "File"
    title = name
    return target, title, label


def _register_sources_footnote(ref: Dict[str, Any], registry: CitationRegistry) -> None:
    """
    处理 type == sources_footnote，把 sources 列表注册到文末引用池。
    """
    sources = ref.get("sources")
    if not isinstance(sources, list):
        return
    for src in sources:
        if not isinstance(src, dict):
            continue
        registry.add(
            url=str(src.get("url") or "").strip(),
            title=str(src.get("title") or "").strip(),
            attribution=str(src.get("attribution") or "").strip(),
        )


def _replace_internal_cites_with_markdown_refs(
    raw_text: str,
    msg: Dict[str, Any],
    registry: CitationRegistry,
) -> str:
    """
    把消息正文中的内部 cite 标记替换为标准 Markdown 引用格式：
    - cite... -> ([Attribution][n])
    - filecite... -> ([File name][n])
    """
    if not raw_text:
        return raw_text

    meta = msg.get("metadata", {}) or {}
    refs = meta.get("content_references", []) or []
    if not isinstance(refs, list):
        return _INTERNAL_CITATION_TOKEN_RE.sub("", raw_text)

    text = raw_text
    replacements: List[Tuple[int, str, str]] = []

    for ref in refs:
        if not isinstance(ref, dict):
            continue

        if ref.get("type") == "sources_footnote":
            _register_sources_footnote(ref, registry)
            continue

        matched_text = str(ref.get("matched_text") or "")
        if not matched_text or not matched_text.strip():
            continue

        ref_type = ref.get("type")
        if ref_type == "file":
            target, title, label = _extract_file_source_from_ref(ref)
            idx = registry.add(target=target, title=title, attribution=label) if target else None
        else:
            target, title, attribution = _extract_primary_source_from_ref(ref)
            idx = registry.add(target=target, title=title, attribution=attribution) if target else None
            label = attribution or _domain_fallback_label(target)

        if not idx:
            continue

        replacement = f"([{_format_markdown_link_label(label)}][{idx}])"
        start_idx = ref.get("start_idx")
        sort_key = start_idx if isinstance(start_idx, int) else sys.maxsize
        replacements.append((sort_key, matched_text, replacement))

    replacements.sort(key=lambda x: x[0])
    for _, matched_text, replacement in replacements:
        text = text.replace(matched_text, replacement, 1)

    # 清理没匹配成功的内部引用标记，避免脏字符残留到 Markdown
    text = _INTERNAL_CITATION_TOKEN_RE.sub("", text)
    return text


# ==============================================================================
# 3) Markdown 数学美化（仅对“文本内容”生效，跳过代码围栏）
# ------------------------------------------------------------------------------
# 规则（与你之前版本一致）：
# - 行内：\( ... \) -> $ ... $
# - 显示：独立行 '\[' ... '\]' -> $$ ... $$（列表项内整体缩进两格）
# - 跳过 ``` 代码围栏（含引用内围栏：> ```python）
# ==============================================================================
_LIST_LINE_RE = re.compile(r'^\s*(?:[-*]|\d+\.)\s+')
_FENCE_RE = re.compile(r'^\s*(?:>+\s*)?```')


def _in_list_context(prev_lines: List[str]) -> bool:
    """
    向上回溯至空行：
    - 只要遇到列表起始行（-/*/1.等）则判定当前处在列表项上下文
    - 用于决定 $$ 公式块是否整体缩进两格
    """
    for j in range(len(prev_lines) - 1, -1, -1):
        if prev_lines[j].strip() == '':
            break
        if _LIST_LINE_RE.match(prev_lines[j]):
            return True
    return False


def beautify_markdown(md_text: str) -> str:
    """
    对“非代码块”的文本做最小必要的 LaTeX/Markdown 美化。
    """
    lines = md_text.split('\n')
    out: List[str] = []
    in_code = False
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 进入/退出代码围栏（支持引用内围栏）
        if _FENCE_RE.match(line):
            in_code = not in_code
            out.append(line)
            i += 1
            continue

        # 代码块内：不做任何替换
        if in_code:
            out.append(line)
            i += 1
            continue

        # 独立行 '\[' ... '\]' -> $$ ... $$
        if stripped == r'\[':
            i += 1
            formula_lines: List[str] = []
            while i < len(lines) and lines[i].strip() != r'\]':
                formula_lines.append(lines[i].strip())
                i += 1
            if i < len(lines) and lines[i].strip() == r'\]':
                i += 1

            indent = '  ' if _in_list_context(out) else ''

            # 公式块前补空行（真实空行，这里不使用 BLANK，因为这是正文内部排版）
            if out and out[-1].strip() != '':
                out.append('')

            out.append(f'{indent}$$')
            for fl in formula_lines:
                out.append(f'{indent}{fl}')
            out.append(f'{indent}$$')

            # 公式块后补空行
            out.append('')
            continue

        # 同一行内 '\[...\]' -> $$...$$（少见，仍支持）
        m = re.search(r'\\\[(.+?)\\\]', line, flags=re.DOTALL)
        if m:
            before = line[:m.start()].rstrip()
            mid = m.group(1).strip()
            after = line[m.end():].lstrip()
            indent = '  ' if _in_list_context(out) else ''

            if before:
                out.append(before)
            if out and out[-1].strip() != '':
                out.append('')

            out.append(f'{indent}$$')
            for sub in mid.splitlines():
                out.append(f'{indent}{sub.strip()}')
            out.append(f'{indent}$$')

            if after:
                out.append('')
                out.append(after)

            i += 1
            continue

        # 行内公式：\( ... \) -> $...$
        line = re.sub(r'\\\((.+?)\\\)', r'$\1$', line)

        out.append(line)
        i += 1

    return '\n'.join(out)


# ==============================================================================
# 4) 图片占位符（方案 A）
# ------------------------------------------------------------------------------
# 解析 multimodal_text.parts 中的 image_asset_pointer，并按顺序插入占位符。
# 你后续可以写个“二次处理脚本”，扫描 <!--CHATGPT_IMG ...--> 注释并替换为真实 URL 图片标签。
# ==============================================================================
def _extract_file_id(asset_pointer: str) -> Optional[str]:
    """
    从 asset_pointer 中抽取 file id。
    - sediment://file_xxx -> file_xxx
    - 其他情况：尝试抓取 file_...
    """
    if not asset_pointer or not isinstance(asset_pointer, str):
        return None
    if asset_pointer.startswith("sediment://"):
        return asset_pointer[len("sediment://"):]
    m = re.search(r'(file_[A-Za-z0-9]+)', asset_pointer)
    return m.group(1) if m else None


def _render_image_placeholder(
    index: int,
    *,
    file_id: Optional[str],
    name: Optional[str],
    width: Optional[int],
    height: Optional[int],
    src: Optional[str],
    kind: str = "attachment",
) -> str:
    """
    返回两行文本：
      1) 可读提示（告诉读者这里原来有一张图）
      2) HTML 注释（携带足够元信息，便于后处理替换 URL）
    """
    label = f"Image {index}"
    shown = name or file_id or "unknown"
    w = str(width) if isinstance(width, int) else ""
    h = str(height) if isinstance(height, int) else ""

    comment = (
        f'<!--CHATGPT_IMG kind="{_html_escape(kind)}" '
        f'id="{_html_escape(file_id or "")}" '
        f'name="{_html_escape(name or "")}" '
        f'w="{_html_escape(w)}" h="{_html_escape(h)}" '
        f'src="{_html_escape(src or "")}"-->'
    )

    return f"🖼️ {label}: {shown}\n{comment}"


def _render_message_parts_with_images(msg: Dict[str, Any]) -> List[str]:
    """
    将 message.content.parts 渲染为“文本片段列表”（保持原顺序）：
      - 字符串：原样保留（非空）
      - image_asset_pointer：转为占位符（两行）
    """
    content = msg.get("content", {}) or {}
    parts = content.get("parts", []) or []

    # attachments 里通常有 name/width/height 等辅助信息
    metadata = msg.get("metadata", {}) or {}
    attachments = metadata.get("attachments", []) or []

    # id -> attachment dict
    att_map: Dict[str, Dict[str, Any]] = {}
    for a in attachments:
        if isinstance(a, dict) and a.get("id"):
            att_map[str(a["id"])] = a

    rendered: List[str] = []
    img_idx = 0

    for p in parts:
        # 纯文本片段
        if isinstance(p, str):
            if p.strip():
                rendered.append(p)
            continue

        # 图片片段
        if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer":
            img_idx += 1
            src = p.get("asset_pointer") or ""
            fid = _extract_file_id(src) or (src if src else "")
            att = att_map.get(str(fid), {})

            name = att.get("name") or ""
            w = p.get("width") if isinstance(p.get("width"), int) else att.get("width")
            h = p.get("height") if isinstance(p.get("height"), int) else att.get("height")

            rendered.append(_render_image_placeholder(
                img_idx,
                file_id=str(fid) if fid else None,
                name=str(name) if name else None,
                width=w if isinstance(w, int) else None,
                height=h if isinstance(h, int) else None,
                src=str(src) if src else None,
                kind="attachment",
            ))
            continue

        # 其他 multimodal part（音频/文件等）暂不输出，以免噪音

    return rendered


# ==============================================================================
# 5) 推理引用渲染（blockquote / code-run）
# ------------------------------------------------------------------------------
# ✅ 关键修复：blockquote 的“空行也要加 > ”，否则引用会断。
# ==============================================================================
def _to_blockquote(s: str) -> str:
    """
    把多行字符串逐行变成 blockquote。

    为什么空行也要加 '> '？
    - Markdown 里 blockquote 遇到真正的空行，往往会结束引用块；
    - 如果推理段中含代码围栏，围栏中间有空行，就可能导致围栏被拆断；
    - 结果就是：代码块/引用块排版彻底乱掉。
    """
    return "\n".join(["> " + ln for ln in s.splitlines()])


def _render_code_run(title: str, lang: str, code: str, output: str) -> str:
    """
    渲染一段“代码推理”：
      **标题**
      ```lang
      code
      ```
      ```
      output
      ```
    最后整段转为 blockquote（每行前缀 > ）
    """
    blocks: List[str] = []

    title = (title or "").strip()
    if title:
        blocks.append(f"**{title}**")

    lang = (lang or "").strip().lower()
    fence_open = f"```{lang}" if lang not in ("", "unknown", "plain", "text") else "```"

    blocks.append(fence_open)
    blocks.append((code or "").rstrip("\n"))
    blocks.append("```")

    if output and output.strip():
        blocks.append("```")
        blocks.append(output.strip("\n"))
        blocks.append("```")

    return _to_blockquote("\n".join(blocks))


# ==============================================================================
# 6) ReasoningSession：收集 thoughts/code/tool-output/recap，并生成 <details>
# ==============================================================================
@dataclass(order=True)
class _SessionItem:
    """
    会话内条目（用于排序输出）
    - type: 'thought' | 'code'
    - time: 用于排序（None 视为 0.0）
    - seq: 同一时间戳下保持稳定顺序
    """
    sort_index: tuple = field(init=False, repr=False)
    type: str = field(compare=False)
    time: Optional[float] = field(compare=False, default=None)
    seq: int = field(compare=False, default=0)

    # thought
    summary: str = field(compare=False, default="")
    content: str = field(compare=False, default="")

    # code
    title: str = field(compare=False, default="")
    lang: str = field(compare=False, default="")
    code: str = field(compare=False, default="")
    output: Optional[str] = field(compare=False, default=None)
    _code_time: Optional[float] = field(compare=False, default=None)

    def __post_init__(self):
        self.sort_index = ((self.time or 0.0), self.seq)


class ReasoningSession:
    """
    聚合同一轮推理的所有元素，遇到“下一条 AI 最终文本”时一次性输出为 <details>。
    """
    def __init__(self) -> None:
        self.items: List[_SessionItem] = []
        self.recap_text: Optional[str] = None
        self._seq = 0

    def add_thoughts(self, msg_time: Optional[float], thought_list: Any) -> None:
        """
        收集 content_type == "thoughts" 的每段 thought（summary + content）。
        注意：导出 JSON 中 thoughts 往往是一个数组，每段都有自己的 summary/content。
        """
        if not isinstance(thought_list, list):
            return
        for t in thought_list:
            if not isinstance(t, dict):
                continue
            summ = (t.get("summary") or "").strip()
            cont = (t.get("content") or "").strip()
            if not summ and not cont:
                continue
            self.items.append(_SessionItem(
                type="thought",
                time=msg_time,
                seq=self._seq,
                summary=summ,
                content=cont,
            ))
            self._seq += 1

    def add_code(self, msg_time: Optional[float], title: str, lang: str, code_text: str) -> None:
        """
        收集 assistant 的 content_type == "code"。
        tool 输出会在后续用 pair_code_output() 绑定到最近一条未绑定的 code。
        """
        self.items.append(_SessionItem(
            type="code",
            time=msg_time,
            seq=self._seq,
            title=title or "",
            lang=(lang or "").strip().lower(),
            code=code_text or "",
            output=None,
            _code_time=msg_time,
        ))
        self._seq += 1

    def pair_code_output(self, output_time: Optional[float], output_text: str) -> None:
        """
        将 tool(name="python") 的 execution_output 绑定到最近一条尚无输出的 code 项。
        绑定后，使用输出时间作为 code 项的排序时间（更贴近“代码+结果完成”的时序）。
        """
        for item in reversed(self.items):
            if item.type == "code" and item.output in (None, ""):
                item.output = output_text or ""
                item.time = output_time if output_time is not None else item._code_time
                item.sort_index = ((item.time or 0.0), item.seq)
                return

    def set_recap(self, recap_text: str) -> None:
        """设置 <summary> 的文本（例如“已思考 1m 7s”）。"""
        self.recap_text = (recap_text or "").strip()

    def is_empty(self) -> bool:
        return not self.items and not self.recap_text

    def build_details_block(self, default_summary: str = "思考") -> Optional[str]:
        """
        生成 <details> 折叠块，并清空会话缓存。
        - 子项按时间升序输出
        - thought：> **summary** + 正文逐行 > 引用（正文先做 beautify）
        - code：整段引用（含围栏与运行结果）
        """
        if not self.items and not self.recap_text:
            return None

        items_sorted = sorted(self.items)
        parts: List[str] = []

        for it in items_sorted:
            if it.type == "thought":
                block_lines: List[str] = []

                if it.summary:
                    block_lines.append("> **" + it.summary + "**")

                if it.content:
                    pretty = beautify_markdown(it.content)
                    block_lines.append(_to_blockquote(pretty))

                if block_lines:
                    parts.append("\n".join(block_lines).rstrip())

            elif it.type == "code":
                title = it.title or "代码推理"
                parts.append(_render_code_run(title, it.lang or "", it.code or "", it.output or ""))

        inner = "\n\n".join(parts)

        summary_text = self.recap_text if self.recap_text else default_summary
        summary_text = f"🤔 {summary_text}"

        block = (
            "<details>\n"
            f'<summary style="font-weight: bold; color: #10ac84; cursor: pointer;">{_html_escape(summary_text)}</summary>\n\n'
            f"{inner}\n\n"
            "</details>"
        )

        self.items.clear()
        self.recap_text = None
        self._seq = 0
        return block


# ==============================================================================
# 7) HTML 头部块：User / AI
# ==============================================================================
def _render_user_header() -> str:
    return '<h1 style="color: #2e86de;">🧑‍💻 User Prompt</h1>'


def _render_ai_header() -> str:
    return '<h1 style="color: #10ac84;">🤖 AI Response</h1>'


def _render_user_time_row(time_str: str) -> str:
    """
    User 的时间行
    """
    time_str = _html_escape(time_str or "未知时间")
    return (
        '<div style="display: flex; gap: 10px; align-items: center; margin-bottom: 10px;">\n'
        '    <div style="color: #888; font-size: 12px; font-family: sans-serif;">\n'
        f'        🕒 {time_str}\n'
        '    </div>\n'
        '</div>'
    )


def _render_ai_meta_row(model_slug: str, time_str: str) -> str:
    """
    AI 的模型徽章 + 时间行（你给的示例）
    """
    model_slug = _html_escape(model_slug or "unknown-model")
    time_str = _html_escape(time_str or "未知时间")
    return (
        '<div style="display: flex; gap: 10px; align-items: center; margin-bottom: 10px;">\n'
        '    <div style="background-color: #e3f2fd; color: #1565c0; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-family: sans-serif; font-weight: bold; border: 1px solid #bbdefb;">\n'
        f'        {model_slug}\n'
        '    </div>\n'
        '    <div style="color: #888; font-size: 12px; font-family: sans-serif;">\n'
        f'        🕒 {time_str}\n'
        '    </div>\n'
        '</div>'
    )


def _get_model_slug_for_message(msg: Dict[str, Any], conversation_default: str) -> str:
    """
    为某条 assistant 最终回复确定模型名称：
    - message.metadata.model_slug
    - message.metadata.default_model_slug
    - conversation_default_model
    """
    meta = msg.get("metadata", {}) or {}
    slug = meta.get("model_slug") or meta.get("default_model_slug") or conversation_default or "unknown-model"
    return str(slug)


# ==============================================================================
# 8) 主流程：解析 JSON -> Markdown（严格换行，严格空行占位）
# ==============================================================================
ALLOWED_CONTENT_TYPES = {"text", "multimodal_text"}


def parse_chat_to_markdown(json_file_path: str) -> str:
    """
    核心转换函数：读取 JSON -> 只保留最终态分支 -> 输出 Markdown（带 HTML 样式）

    严格排版策略（按行控制）：
    - 不使用 "\n\n".join(...) 自动插空行
    - 全部“段落间空行”使用 BLANK 或 TRIPLE_BLANK 精确控制
    """
    with open(json_file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 会话级默认模型（兜底）
    conversation_default_model = str(data.get("default_model_slug") or "unknown-model")

    mapping: Dict[str, Any] = data.get("mapping", {}) or {}
    current_id: Optional[str] = data.get("current_node")

    # 1) 回溯 parent 链构造最终分支（根 -> current_node）
    branch_ids: List[str] = []
    while current_id:
        node = mapping.get(current_id)
        if not node:
            break
        branch_ids.append(current_id)
        parent = node.get("parent")
        if not parent or parent not in mapping:
            break
        current_id = parent
    branch_ids.reverse()

    # 2) 遍历分支并输出
    out_lines: List[str] = []
    session = ReasoningSession()
    next_citation_index = 1

    for node_id in branch_ids:
        node = mapping.get(node_id) or {}
        msg = node.get("message") or {}
        author = msg.get("author", {}) or {}
        role = author.get("role")
        content = msg.get("content", {}) or {}
        ctype = content.get("content_type")
        create_time = msg.get("create_time")

        # ---- A. 收集 thoughts ----
        if ctype == "thoughts":
            session.add_thoughts(create_time, content.get("thoughts", []))
            continue

        # ---- B. 收集 reasoning_recap（用于 <details><summary>）----
        if ctype == "reasoning_recap":
            session.set_recap(content.get("content", ""))
            continue

        # ---- C. 收集 assistant code（推理代码）----
        if role == "assistant" and ctype == "code":
            code_text = content.get("text", "") or ""
            lang = (content.get("language") or "").strip().lower()

            # 导出 JSON 里 language 经常是 unknown，但 recipient == "python" 能提示真实语言
            recip = (msg.get("recipient") or "").strip().lower()
            if (not lang or lang in ("unknown", "plain", "text")) and recip == "python":
                lang = "python"

            title = (msg.get("metadata", {}) or {}).get("reasoning_title", "") or ""
            session.add_code(create_time, title, lang, code_text)
            continue

        # ---- D. 收集 tool 输出（execution_output）并绑定到最近 code ----
        if role == "tool":
            tool_name = (author.get("name") or "").lower()
            if tool_name == "python" and ctype == "execution_output":
                session.pair_code_output(create_time, content.get("text", "") or "")
            continue

        # ---- E. 输出 user/assistant 最终正文（text / multimodal_text）----
        if role not in {"user", "assistant"}:
            continue
        if ctype not in ALLOWED_CONTENT_TYPES:
            continue

        # 将 parts 渲染为文本（含图片占位符）
        rendered_parts = _render_message_parts_with_images(msg)
        if not rendered_parts:
            continue

        raw_text = "\n".join(rendered_parts).replace("\r\n", "\n").replace("\n\r", "\n").strip()
        if not raw_text:
            continue

        # -------------------
        # E1) User 消息输出
        # -------------------
        if role == "user":
            out_lines.append(_render_user_header())

            # 时间行
            _extend_block(out_lines, _render_user_time_row(format_time(create_time)))

            # 时间块后空一行（两个空格）
            out_lines.append(BLANK)

            # 正文（做数学美化）
            user_text = beautify_markdown(raw_text)
            _extend_block(out_lines, user_text)

            # 结束空三行
            out_lines.extend(TRIPLE_BLANK)
            continue

        # -------------------
        # E2) Assistant 消息输出
        # -------------------
        model_slug = _get_model_slug_for_message(msg, conversation_default_model)
        time_str = format_time(create_time)
        msg_citation_registry = CitationRegistry(start_index=next_citation_index)
        raw_text = _replace_internal_cites_with_markdown_refs(raw_text, msg, msg_citation_registry).strip()
        if not raw_text:
            continue

        # AI 正文（做数学美化）
        ai_text = beautify_markdown(raw_text)

        # 若当前轮累计了推理信息，则生成 <details> 并插入
        details_block = None
        if not session.is_empty():
            details_block = session.build_details_block(default_summary="思考")

        out_lines.append(_render_ai_header())
        _extend_block(out_lines, _render_ai_meta_row(model_slug, time_str))

        # 推理块（可选）
        if details_block:
            _extend_block(out_lines, details_block)

        # </details> 与正文之间空一行（两个空格）
        out_lines.append(BLANK)

        # 正文
        _extend_block(out_lines, ai_text)

        # 引用定义（仅该条 AI 响应）
        ai_footnotes = msg_citation_registry.render_footnotes()
        if ai_footnotes:
            out_lines.append("")
            out_lines.extend(ai_footnotes)

        next_citation_index += msg_citation_registry.count()

        # 结束空三行
        out_lines.extend(TRIPLE_BLANK)

    # 3) 兜底：若遍历结束仍有未输出的推理会话，单独输出
    if not session.is_empty():
        details_block = session.build_details_block(default_summary="思考") or ""
        out_lines.append(_render_ai_header())
        _extend_block(out_lines, _render_ai_meta_row(conversation_default_model, "未知时间"))
        _extend_block(out_lines, details_block)
        out_lines.append(BLANK)
        out_lines.extend(TRIPLE_BLANK)

    # ✅ 严格按行输出
    return "\n".join(out_lines)


# ==============================================================================
# 9) CLI / 交互式输入与文件写入
# ==============================================================================
def _default_output_path_for(input_path: str) -> str:
    """输入 input.json -> 输出同目录同名 input.md"""
    base, _ = os.path.splitext(input_path)
    return base + ".md"


def _ensure_parent_dir(output_path: str) -> None:
    """确保输出文件父目录存在（mkdir -p）"""
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def _readable_file(path: str) -> bool:
    """输入文件是否存在且可读"""
    return os.path.isfile(path) and os.access(path, os.R_OK)


def _normalize_path_arg(p: Optional[str]) -> Optional[str]:
    """
    归一化路径参数：
    - 去掉首尾成对引号
    - 展开 ~ 与环境变量
    """
    if p is None:
        return None
    p = p.strip()
    if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
        p = p[1:-1]
    p = p.strip()
    return os.path.expanduser(os.path.expandvars(p))


def _interactive_ask_input_path() -> Optional[str]:
    """无参数时进入交互式，提示用户输入 JSON 路径"""
    print("请输入需要处理的 ChatGPT 导出 JSON 文件路径（可带引号；输入 q 退出）：", end="", flush=True)
    while True:
        user_in = input()
        if user_in is None:
            return None
        user_in = user_in.strip()
        if user_in.lower() in {"q", "quit", "exit"} or user_in == "":
            return None

        candidate = _normalize_path_arg(user_in)
        if candidate and _readable_file(candidate):
            return candidate

        print(f"路径无效或不可读：{user_in}\n请重新输入（或输入 q 退出）：", end="", flush=True)


def _parse_args(argv: List[str]) -> argparse.Namespace:
    """解析命令行参数（位置参数 + -i/-o）"""
    parser = argparse.ArgumentParser(
        prog=os.path.basename(argv[0]) if argv else "chatgpt2md.py",
        description="将 ChatGPT 导出 JSON 转换为带样式 Markdown（含模型/时间/推理折叠/图片占位符/严格空行）。",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=r"""
示例：
  1) 交互式：         python chatgpt2md.py
  2) 最简：           python chatgpt2md.py input.json
  3) 指定输出：       python chatgpt2md.py input.json output.md
  4) 使用选项：       python chatgpt2md.py -i input.json -o output.md
        """.strip()
    )

    parser.add_argument("positional", nargs="*", help="可选的位置参数：input [output]")
    parser.add_argument("-i", "--input", dest="input_path", help="输入 JSON 路径（可带引号）")
    parser.add_argument("-o", "--output", dest="output_path", help="输出 Markdown 路径（可带引号）")
    return parser.parse_args(argv[1:])


def _resolve_io_paths(ns: argparse.Namespace) -> Tuple[Optional[str], Optional[str]]:
    """
    得到 (input_path, output_path)：
    - 位置参数优先（input [output]）
    - -i/-o 覆盖
    - 只有 input 则 output 自动推导为同名 .md
    """
    positional: List[str] = getattr(ns, "positional", []) or []
    p_in: Optional[str] = positional[0] if len(positional) >= 1 else None
    p_out: Optional[str] = positional[1] if len(positional) >= 2 else None

    opt_in = getattr(ns, "input_path", None)
    opt_out = getattr(ns, "output_path", None)
    if opt_in:
        p_in = opt_in
    if opt_out:
        p_out = opt_out

    p_in = _normalize_path_arg(p_in) if p_in else None
    p_out = _normalize_path_arg(p_out) if p_out else None

    if p_in and not p_out:
        p_out = _default_output_path_for(p_in)

    return p_in, p_out


def _ensure_output_path(output_path: Optional[str], input_path: str) -> str:
    """未提供输出路径则自动推导"""
    return output_path or _default_output_path_for(input_path)


def run_once(input_path: str, output_path: str) -> int:
    """
    执行一次转换：读 JSON -> 转 Markdown -> 写文件
    返回码：0 成功；非 0 失败
    """
    if not _readable_file(input_path):
        print(f"错误：输入文件不存在或不可读：{input_path}")
        return 2

    try:
        markdown_output = parse_chat_to_markdown(input_path)
    except json.JSONDecodeError as e:
        print(f"错误：JSON 解析失败 - {e}")
        return 3
    except Exception as e:
        print(f"错误：处理失败 - {e}")
        return 4

    try:
        _ensure_parent_dir(output_path)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(markdown_output)
    except Exception as e:
        print(f"错误：无法写入文件 {output_path} - {e}")
        return 5

    print(f"已生成 Markdown 文件：{output_path}")
    return 0


def main() -> None:
    """
    程序入口：
    - 支持命令行参数；无输入参数则进入交互式
    """
    ns = _parse_args(sys.argv)
    input_path, output_path = _resolve_io_paths(ns)

    if not input_path:
        user_input_path = _interactive_ask_input_path()
        if not user_input_path:
            print("已取消。")
            sys.exit(0)
        input_path = user_input_path

    output_path = _ensure_output_path(output_path, input_path)

    code = run_once(input_path, output_path)
    sys.exit(code)


if __name__ == "__main__":
    main()

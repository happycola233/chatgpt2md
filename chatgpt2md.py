#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
ChatGPT 导出 JSON -> Markdown 转换器
====================================

✅ 功能要点
----------
- 仅保留从根节点到 `current_node` 的“最终态分支”，把**同一轮推理**里的：
  - `thoughts`（多段思考，含 `summary` 与 `content`）
  - assistant 的 `code`（可配对 tool 的 `execution_output`）
  - `reasoning_recap`（例如“已思考 49s”）
  合并为一个 `<details>` 折叠块，**按时间升序**排列子项，并在“下一条助手文本”的正文最前插入。
- 渲染规则：
  - 思考段：先一行 `> **summary**`，接着正文逐行 `>` 引用（正文启用数学美化，但跳过**代码围栏**）
  - 代码推理段：**整段引用**（包含标题、代码围栏、运行结果围栏）
- 推理折叠块与**正文**之间**空一行**

✅ 数学美化（仅对“文本内容”生效，跳过代码围栏，支持“引用内的代码围栏”）
-----------------------------------------------------------------------
- 行内：`\( ... \)` → `$ ... $`
- 显示：独立行 `'\[' ... '\]'` → 带空行包裹的 `$$ ... $$`
- 列表项内：`$$` 块整体缩进两个空格
- 跳过 ````` 代码围栏（包括**带引用前缀的代码围栏**：如 `> ```python`）
- 保留 `\~` 与 `\@` 的反斜杠（不反转义）

🧑‍💻 使用方式
------------
1) 交互式（**无命令行参数**时自动进入）  
   直接运行：`python chatgpt2md.py`  
   程序会提示你输入 JSON 文件路径，并输出到同目录同名的 `.md`。

2) 最简命令行（**仅输入路径**）  
   `python chatgpt2md.py input.json`  
   → 输出 `input.md`（与输入同目录）。  
   ※ 如果你把路径包上引号（如 `"C:\path with space\input.json"`），本程序会自动去掉引号再处理。

3) 指定输入与输出路径（**位置参数**）  
   `python chatgpt2md.py input.json output.md`

4) 使用选项（**更清晰**）  
   `python chatgpt2md.py -i input.json -o output.md`

5) 帮助  
   `python chatgpt2md.py -h`

⚠️ 注意
------
- 期望的输入是 **ChatGPT 导出的 JSON**。若文件不是合法 JSON，将报错退出。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ==============================================================================
# 基础工具：时间格式化
# ==============================================================================

def format_time(create_time: Optional[float]) -> str:
    """
    将 create_time（Unix 时间戳：秒/浮点秒）格式化为 YYYY-MM-DD HH:MM:SS。
    None 或异常 → "未知时间"
    """
    if create_time is None:
        return "未知时间"
    try:
        dt = datetime.fromtimestamp(float(create_time))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知时间"


# ==============================================================================
# Markdown 数学美化（避开代码围栏，支持“引用内的代码围栏”）
# ==============================================================================

# 识别“列表项起始行”（用于决定 $$ 块是否做整体缩进）
_LIST_LINE_RE = re.compile(r'^\s*(?:[-*]|\d+\.)\s+')

# 识别“代码围栏行”（开/闭），允许前缀是 '>' 引用 + 若干空格
#   例如：```、   ```python、  > ```、  >   ```python
_FENCE_RE = re.compile(r'^\s*(?:>+\s*)?```')

def _in_list_context(prev_lines: List[str]) -> bool:
    """
    从已输出的 prev_lines 自下向上回溯至空行或文档开头：
    - 只要遇到列表起始行（- / * / 1. 等），认为当前处在列表项里。
    该信息用于决定 $$ 公式块是否整体缩进两格。
    """
    for j in range(len(prev_lines) - 1, -1, -1):
        if prev_lines[j].strip() == '':
            break
        if _LIST_LINE_RE.match(prev_lines[j]):
            return True
    return False


def beautify_markdown(md_text: str) -> str:
    r"""
    对“非代码块”的文本做最小必要的 LaTeX/Markdown 美化：
      1) 独立行 '\[' ... '\]' → 带空行的 $$ ... $$（列表项内整体缩进两格）
      2) 行内 '\(...\)' → $...$
      3) 跳过 ``` 代码围栏（含前缀 '>' 的围栏）
      4) 保留 '\~' 与 '\@'（不反转义）

    说明：
      - 这里处理的是“普通文本段”；对“代码推理段”的代码围栏，我们不做数学替换，
        因为渲染函数会先组装再整体加引用。
    """
    lines = md_text.split('\n')
    out: List[str] = []
    in_code = False
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # A. 进入/退出代码围栏：支持前面带 '>' 的围栏
        if _FENCE_RE.match(line):
            in_code = not in_code
            out.append(line)
            i += 1
            continue

        if in_code:
            out.append(line)
            i += 1
            continue

        # B. 独立行 '\[' ... '\]' → $$...$$（多行显示公式）
        if stripped == r'\[':
            i += 1
            formula_lines: List[str] = []
            while i < len(lines) and lines[i].strip() != r'\]':
                formula_lines.append(lines[i].strip())  # 保留每行内容，去除首尾空白
                i += 1
            # 跳过 '\]'
            if i < len(lines) and lines[i].strip() == r'\]':
                i += 1

            indent = '  ' if _in_list_context(out) else ''

            # 公式块前补空行（若上一行非空）
            if out and out[-1].strip() != '':
                out.append('')

            out.append(f'{indent}$$')
            for fl in formula_lines:
                out.append(f'{indent}{fl}')
            out.append(f'{indent}$$')

            # 公式块后补空行，增强渲染稳定性
            out.append('')
            continue

        # C. 同一行内 '\[...\]'（较少见）：拆成 $$ 块输出
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

        # D. 行内公式：\( ... \) → $...$
        #    注意：不在代码块内，且允许一行多个匹配
        line = re.sub(r'\\\((.+?)\\\)', r'$\1$', line)

        # E. 不反转义 \~ 与 \@，保持原样
        out.append(line)
        i += 1

    return '\n'.join(out)


# ==============================================================================
# 思考与代码推理：渲染工具
# ==============================================================================

def _to_blockquote(s: str) -> str:
    """
    将多行字符串逐行加上 '> '（空行保持为空，不额外加 '> '）。
    若需要“空行也加 '>'”，可改为：("" if ln.strip()=="" else "> "+ln) 的 else 分支改成 "> ".
    """
    lines = s.splitlines()
    return "\n".join([("> " + ln) if ln.strip() != "" else "" for ln in lines])


def _render_code_run(title: str, lang: str, code: str, output: str) -> str:
    """
    渲染一段“代码推理”，并把整段包进引用块：
    - 第一行：**标题**（若空则省略），前面有 '>'
    - 代码围栏与运行结果围栏也都在引用内（每行前有 '>'）
    """
    blocks: List[str] = []
    title = (title or '').strip()
    if title:
        blocks.append(f"**{title}**")

    lang = (lang or '').strip().lower()
    fence_open = f"```{lang}" if lang not in ("", "unknown", "plain", "text") else "```"

    # 代码围栏
    blocks.append(fence_open)
    blocks.append((code or "").rstrip("\n"))
    blocks.append("```")

    # 运行结果围栏（若有）
    if output and output.strip():
        blocks.append("```")
        blocks.append(output.strip("\n"))
        blocks.append("```")

    # 整段转为引用
    return _to_blockquote("\n".join(blocks))


# ==============================================================================
# 推理会话（思考 + 代码）汇总
# ==============================================================================

@dataclass(order=True)
class _SessionItem:
    """
    会话条目（用于排序）
    type: 'thought' | 'code'
    time: 用于排序的时间戳；code 在配到 output 后，会更新为输出时间
    seq:  稳定排序的辅助序号（同一时间按进入顺序）
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
        # dataclass 排序键：按 (time or 0.0, seq) 升序
        self.sort_index = ((self.time or 0.0), self.seq)


class ReasoningSession:
    """
    聚合同一轮“推理”的所有元素（thoughts / code / tool 输出 / recap），
    并在需要时生成一个 <details> 块，子项按“时间升序”输出。
    使用方式：
      - 对遇到的 thoughts / code / tool-output / recap 逐个 add*
      - 当遇到 assistant 文本消息时，若会话非空 → build_details_block() 并插入到正文前
      - 遍历结束后，如仍有未输出的会话 → 单独作为一条助手消息输出
    """
    def __init__(self) -> None:
        self.items: List[_SessionItem] = []
        self.recap_text: Optional[str] = None
        self._seq = 0  # 稳定排序辅助序号

    # ---- 收集 ----
    def add_thoughts(self, msg_time: Optional[float], thought_list: Any) -> None:
        """添加一条 thoughts 消息中的多个思考段：每段记录 summary + content（时间统一用该消息 create_time）。"""
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
                type="thought", time=msg_time, seq=self._seq,
                summary=summ, content=cont
            ))
            self._seq += 1

    def add_code(self, msg_time: Optional[float], title: str, lang: str, code_text: str) -> None:
        """添加一段 assistant 的 code。'output' 将在后续 tool 执行完成后通过 pair_code_output() 补上。"""
        self.items.append(_SessionItem(
            type="code", time=msg_time, seq=self._seq,
            title=title or "", lang=(lang or "").strip().lower(),
            code=code_text or "", output=None, _code_time=msg_time
        ))
        self._seq += 1

    def pair_code_output(self, output_time: Optional[float], output_text: str) -> None:
        """
        将 tool(name="python") 的 execution_output 绑定到最近一条尚无输出的 code 项。
        绑定后，把该 code 项的 'time' 更新为 output_time（更贴合“代码+结果完成”的时序）。
        """
        for item in reversed(self.items):
            if item.type == "code" and item.output in (None, ""):
                item.output = output_text or ""
                item.time = output_time if output_time is not None else item._code_time
                # 更新排序键
                item.sort_index = ((item.time or 0.0), item.seq)
                return

    def set_recap(self, recap_text: str) -> None:
        """设置 <summary> 文本（例如“已思考 49s”）。"""
        self.recap_text = (recap_text or "").strip()

    def is_empty(self) -> bool:
        """会话是否为空（无任何条目且无 recap）。"""
        return not self.items and not self.recap_text

    # ---- 输出 ----
    def build_details_block(self, default_summary: str = "思考") -> Optional[str]:
        """
        生成 <details> 折叠块字符串，并清空当前会话。
        - 子项按 (time, seq) 升序；
        - thought：输出 > **summary** + 引用正文（正文先做 beautify_markdown）
        - code：输出带引用的 **标题** + 代码/结果围栏（整个段落已引用化）
        """
        if not self.items and not self.recap_text:
            return None

        # 排序
        items_sorted = sorted(self.items)

        parts: List[str] = []
        for it in items_sorted:
            if it.type == "thought":
                block_lines: List[str] = []
                if it.summary:
                    block_lines.append("> **" + it.summary + "**")
                if it.content:
                    pretty = beautify_markdown(it.content)
                    quoted = _to_blockquote(pretty)
                    block_lines.append(quoted)
                if block_lines:
                    parts.append("\n".join(block_lines).rstrip())

            elif it.type == "code":
                title = it.title or "代码推理"
                lang = it.lang or ""
                code = it.code or ""
                outp = it.output or ""
                parts.append(_render_code_run(title, lang, code, outp))

        inner = "\n\n".join(parts)  # 各子项之间空一行
        summary_text = self.recap_text if self.recap_text else default_summary

        block = "<details>\n<summary>{}</summary>\n\n{}\n\n</details>".format(summary_text, inner)

        # 清空以便开始下一轮会话
        self.items.clear()
        self.recap_text = None
        self._seq = 0
        return block


# ==============================================================================
# 主流程：解析 JSON -> Markdown
# ==============================================================================

ALLOWED_CONTENT_TYPES = {"text", "multimodal_text"}

def parse_chat_to_markdown(json_file_path: str) -> str:
    """
    解析JSON，仅保留从根节点到 current_node 的“最终态分支”，并转换为 Markdown。
    处理顺序（按分支节点顺序遍历）：
      - 收集 thoughts / code / tool-output / recap 到一个 ReasoningSession；
      - 当遇到 assistant 的文本消息，若会话非空，则先输出 <details>，再输出文本正文；
      - 文本正文启用数学美化（跳过代码围栏，含“引用内的代码围栏”）；
      - 遍历结束后，如仍有未输出的会话，单独作为一条助手消息输出。

    返回：完整 Markdown 字符串。
    """
    # 1) 读取 JSON
    with open(json_file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    mapping: Dict[str, Any] = data.get("mapping", {}) or {}
    current_id: Optional[str] = data.get("current_node")

    # 2) 回溯 parent 链，形成“最终态分支”（从根到叶）
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

    md_lines: List[str] = []
    session = ReasoningSession()    # 当前推理会话（待插入的 <details>）

    for node_id in branch_ids:
        node = mapping.get(node_id) or {}
        msg = node.get("message") or {}
        author = msg.get("author", {}) or {}
        role = author.get("role")
        content = msg.get("content", {}) or {}
        ctype = content.get("content_type")
        create_time = msg.get("create_time")

        # --- A. thoughts：收集到会话（带上每条 summary+content） ---
        if ctype == "thoughts":
            session.add_thoughts(create_time, content.get("thoughts", []))
            continue

        # --- B. reasoning_recap：设置 <summary> 文本 ---
        if ctype == "reasoning_recap":
            session.set_recap(content.get("content", ""))
            continue

        # --- C. assistant 的 code：加入代码项 ---
        if role == "assistant" and ctype == "code":
            code_text = content.get("text", "") or ""
            lang = (content.get("language") or "").strip().lower()
            # 若未知且 recipient=="python"，默认按 python
            recip = (msg.get("recipient") or "").strip().lower()
            if (not lang or lang in ("unknown", "plain", "text")) and recip == "python":
                lang = "python"
            title = (msg.get("metadata", {}) or {}).get("reasoning_title", "") or ""
            session.add_code(create_time, title, lang, code_text)
            continue

        # --- D. tool 执行输出（例如 python） ：配对到最近的 code ---
        if role == "tool":
            tool_name = (author.get("name") or "").lower()
            if tool_name == "python" and ctype == "execution_output":
                session.pair_code_output(create_time, content.get("text", "") or "")
            continue

        # --- E. 常规文本消息（user/assistant） ---
        if role not in {"user", "assistant"}:
            continue
        if ctype not in ALLOWED_CONTENT_TYPES:
            continue

        # 标题（角色）
        title_line = "# 用户" if role == "user" else "# ChatGPT"

        # 合并 parts（仅保留非空字符串）
        parts = content.get("parts", [])
        text_parts = [p for p in parts if isinstance(p, str) and p.strip()]
        if not text_parts:
            continue

        text = "\n".join(text_parts)
        text = text.replace("\r\n", "\n").replace("\n\r", "\n").strip()
        if not text:
            continue

        # 在“助手文本”前插入当前会话（若非空）
        if role == "assistant" and not session.is_empty():
            details_block = session.build_details_block(default_summary="思考")
            if details_block:
                # 关键：推理块与正文之间空一行
                text = details_block + "\n\n" + text

        # 文本走数学美化（跳过代码围栏，含“引用内”围栏）
        text = beautify_markdown(text)

        # 时间块
        time_str = format_time(create_time)
        time_block = f"> 时间：{time_str}"

        md_lines.extend([title_line, time_block, text])

    # 3) 兜底：遍历结束仍有未输出的会话 → 独立助手消息输出
    if not session.is_empty():
        title_line = "# ChatGPT"
        time_block = f"> 时间：{format_time(None)}"
        details_block = session.build_details_block(default_summary="思考") or ""
        # 这里只输出 details，不再跟正文，因此不需要再空行拼接
        body = beautify_markdown(details_block)
        md_lines.extend([title_line, time_block, body])

    # 4) 返回整合的 Markdown
    return "\n\n".join(md_lines)


# ==============================================================================
# CLI / 交互式输入与文件写入
# ==============================================================================

def _default_output_path_for(input_path: str) -> str:
    """
    根据输入文件自动生成输出路径：
    - 与输入同目录
    - 同名，后缀改为 .md
    """
    base, _ = os.path.splitext(input_path)
    return base + ".md"


def _ensure_parent_dir(output_path: str) -> None:
    """确保输出文件的父级目录存在；如不存在则创建（等价于 mkdir -p）。"""
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def _readable_file(path: str) -> bool:
    """输入文件是否存在且可读。"""
    return os.path.isfile(path) and os.access(path, os.R_OK)


def _normalize_path_arg(p: Optional[str]) -> Optional[str]:
    """
    归一化路径参数：
    - 去掉首尾成对的引号（"..." 或 '...'）
    - 去掉首尾空白
    - 展开 ~ 与环境变量（如 %USERPROFILE% / $HOME）
    - 其余保持原样
    """
    if p is None:
        return None
    p = p.strip()
    if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
        p = p[1:-1]
    p = p.strip()
    p = os.path.expanduser(os.path.expandvars(p))
    return p


def _interactive_ask_input_path() -> Optional[str]:
    """
    交互式地让用户输入要处理的文件路径。
    - 返回规范化后的有效路径字符串
    - 输入 'q' 或空行直接退出（返回 None）
    - 若路径无效则继续提示
    """
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
    """
    解析命令行参数。
    支持四种形式：
      1) 无参数 → 进入交互式
      2) 最简：script.py input.json
      3) 位置参数两项：script.py input.json output.md
      4) 选项：script.py -i input.json [-o output.md]
    """
    parser = argparse.ArgumentParser(
        prog=os.path.basename(argv[0]) if argv else "chatgpt2md.py",
        description="将 ChatGPT 导出 JSON 转换为 Markdown（合并推理：thoughts/code/reasoning_recap）",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=r"""
示例：
  1) 交互式：         python chatgpt2md.py
  2) 最简：           python chatgpt2md.py input.json
  3) 指定输出：       python chatgpt2md.py input.json output.md
  4) 使用选项：       python chatgpt2md.py -i input.json -o output.md
        """.strip()
    )

    # 位置参数：可 0~2 个
    parser.add_argument("positional", nargs="*", help="可选的位置参数：input [output]")

    # 显式选项
    parser.add_argument("-i", "--input", dest="input_path", help="输入 JSON 路径（可带引号）")
    parser.add_argument("-o", "--output", dest="output_path", help="输出 Markdown 路径（可带引号）")

    return parser.parse_args(argv[1:])


def _resolve_io_paths(ns: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    """
    根据解析结果 得到 (input_path, output_path)。
    优先级：
      1) 位置参数两项：positional[0], positional[1]
      2) 仅一个位置参数：positional[0]，输出自动推导
      3) -i/--input 指定输入；-o/--output 指定输出（未给时自动推导）
      4) 都没给：返回 (None, None) → 外层进入交互式

    注意：对所有路径统一做 `_normalize_path_arg`，以支持带引号的写法。
    """
    positional: List[str] = getattr(ns, "positional", []) or []
    p_in: Optional[str] = None
    p_out: Optional[str] = None

    if len(positional) >= 1:
        p_in = positional[0]
    if len(positional) >= 2:
        p_out = positional[1]

    # 选项覆盖（如果提供）
    opt_in = getattr(ns, "input_path", None)
    opt_out = getattr(ns, "output_path", None)
    if opt_in:
        p_in = opt_in
    if opt_out:
        p_out = opt_out

    # 统一归一化（去引号、展开 ~ 与环境变量）
    p_in = _normalize_path_arg(p_in) if p_in else None
    p_out = _normalize_path_arg(p_out) if p_out else None

    # 只有输入，输出自动推导
    if p_in and not p_out:
        p_out = _default_output_path_for(p_in)

    return p_in, p_out


def _ensure_output_path(output_path: Optional[str], input_path: str) -> str:
    """
    若未提供输出路径，则根据输入路径自动生成。
    """
    return output_path or _default_output_path_for(input_path)


def run_once(input_path: str, output_path: str) -> int:
    """
    核心执行：读取 input_path → 解析 → 写入 output_path
    返回码：0=成功；非 0 表示失败。
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
      - 支持命令行参数（位置参数与 -i/-o 任选其一或组合）
      - 若未提供输入路径，则进入交互式向用户索取（支持带引号路径）
    """
    ns = _parse_args(sys.argv)
    input_path, output_path = _resolve_io_paths(ns)

    # 没提供输入 → 进入交互式
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

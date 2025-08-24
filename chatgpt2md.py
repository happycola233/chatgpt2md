import sys
import os
import json
from datetime import datetime

def format_time(create_time) -> str:
    """
    将 create_time 转换为可读的时间格式。
    如果 create_time 为 None，则返回 "未知时间"。
    假设 create_time 为 Unix 时间戳（秒数或浮点数）。
    """
    if create_time is None:
        return "未知时间"
    try:
        dt = datetime.fromtimestamp(float(create_time))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知时间"

def parse_chat_to_markdown(json_file_path: str) -> str:
    """
    解析新版 JSON，仅保留从根节点到 current_node 的“最终态”分支，
    并将其转换为 Markdown：
      - 依次输出 "# 用户"/"# ChatGPT"、时间 Blockquote、消息正文
      - 只处理 role 是 user/assistant，且 content_type 为 text/multimodal_text
    """
    allowed_content_types = {"text", "multimodal_text"}

    # 1. 加载 JSON
    with open(json_file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    mapping = data.get("mapping", {})
    current_id = data.get("current_node")

    # 2. 沿 parent 链，从 current_node 回溯到根，收集这条分支的所有节点
    branch_ids = []
    while current_id:
        node = mapping.get(current_id)
        if not node:
            break
        branch_ids.append(current_id)
        parent = node.get("parent")
        # parent 为 None 或不在 mapping 时停止
        if not parent or parent not in mapping:
            break
        current_id = parent
    # 反转：从根到叶（current_node）
    branch_ids.reverse()

    # 3. 遍历分支节点，过滤并格式化消息
    md_lines = []
    for node_id in branch_ids:
        node = mapping[node_id]
        msg = node.get("message")
        if not msg:
            continue

        role = msg.get("author", {}).get("role")
        if role == "user":
            title = "# 用户"
        elif role == "assistant":
            title = "# ChatGPT"
        else:
            continue

        content = msg.get("content", {})
        if content.get("content_type") not in allowed_content_types:
            continue

        parts = content.get("parts", [])
        text_parts = [p for p in parts if isinstance(p, str) and p.strip()]
        if not text_parts:
            continue

        # 合并段落，统一换行
        text = "\n".join(text_parts)
        text = text.replace("\r\n", "\n").replace("\n\r", "\n").strip()
        if not text:
            continue

        # 格式化时间
        time_str = format_time(msg.get("create_time"))
        time_block = f"> 时间：{time_str}"

        # 添加到输出
        md_lines.extend([title, time_block, text])

    return "\n\n".join(md_lines)

def main():
    """
    程序入口：接收一个 JSON 文件路径，输出同目录下同名 .md 文件。
    用法：
        python chatgpt2md.py <json_file>
    """
    if len(sys.argv) < 2:
        print("用法：python chat2md.py <json_file>")
        sys.exit(1)

    json_file_path = sys.argv[1]
    if not os.path.isfile(json_file_path):
        print(f"错误：文件 {json_file_path} 不存在")
        sys.exit(1)

    markdown_output = parse_chat_to_markdown(json_file_path)
    out_path = os.path.splitext(json_file_path)[0] + ".md"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(markdown_output)

    print(f"已生成 Markdown 文件：{out_path}")

if __name__ == "__main__":
    main()

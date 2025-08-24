# chatgpt2md

将 ChatGPT 导出的 **对话 JSON** 转换为**清爽可读**的 Markdown 文档（.md）。  
它会自动沿着对话树找到**当前分支（最终态）**，只导出“用户 ↔ 助手”的文本内容，并为每条消息加上**时间戳**与**身份标题**。

> 适合把零散的对话整理成知识库、周报、项目纪要或归档材料。


---

## 功能特点

- ✅ **只导出最终分支**：从 `current_node` 沿父链回溯到根，避免历史分叉带来的重复与噪音。
    
- ✅ **角色清晰**：每条消息前自动加上 `# 用户` / `# ChatGPT` 标题。
    
- ✅ **带时间戳**：按本地时区将 `create_time`（Unix 时间戳）格式化为 `YYYY-MM-DD HH:MM:SS`。
    
- ✅ **内容过滤**：仅保留 `role ∈ {user, assistant}` 且 `content_type ∈ {text, multimodal_text}` 的文本消息。
    
- ✅ **零依赖**：仅使用 Python 标准库（`sys`, `os`, `json`, `datetime`）。
    

---

## 适用场景

- 把某次 ChatGPT 会话整理成笔记、纪要、复盘或 PRD 附件。
    
- 将长对话拆分为章节并加入个人知识库。
    
- 导出问答对，便于搜索、标注和分享。
    

---

## 工作原理

1. 读取 JSON，定位 `mapping`（对话节点哈希表）与 `current_node`（当前叶子）。
    
2. 沿 `current_node → parent → parent ...` 回溯到根，得到**当前分支**的节点列表（随后反转为“从根到叶”的自然阅读顺序）。
    
3. 逐节点筛选：
    
    - 仅保留 `author.role` 为 `user` 或 `assistant`；
        
    - 仅保留 `content.content_type` 为 `text` 或 `multimodal_text`；
        
    - 合并 `content.parts` 中的**字符串项**为正文。
        
4. 生成 Markdown：
    
    ```
    # 用户｜# ChatGPT
    > 时间：YYYY-MM-DD HH:MM:SS
    正文...
    ```
    
5. 将结果写入 **同名 .md 文件**（与源 JSON 同目录）。
    


---

## 快速开始

```bash
# 基本用法
python chatgpt2md.py <path/to/conversation.json>

# 运行成功后，会在同目录生成同名 Markdown：
# <path/to/conversation.md>
```
****
Windows PowerShell 示例：

```powershell
python .\chatgpt2md.py .\export\my_conversation.json
```

macOS / Linux 示例：

```bash
python3 ./chatgpt2md.py ./export/my_conversation.json
```

---

## 如何获取 JSON 文件+操作步骤

1. 在 GPT 对话聊天界面，鼠标右键，点击” **检查** “或” **开发人员选项** ”，或直接按 **F12**
![Screenshot](res/1.png)
2. 在弹出的窗口（后续简称 **开发者窗口**）中，选择“ **网络** ”选择卡
![Screenshot](res/2.png)
3. 复制聊天中自己发送或GPT回复的一小段内容（便于后续搜索）
![Screenshot](res/3.png)
4. 刷新网页（Ctrl+R），请注意，刷新时，**之前打开的开发者窗口不能关闭**
5. 此时，开发者窗口中应该出现了很多网络请求。点击一下开发者窗口的空白区域（或随便点一个网络请求），确保窗口焦点在开发者窗口上。
6. 按 ”Ctrl+F“，粘贴刚刚复制的消息，然后Enter进行搜索，点击搜索出来的结果（如果出现了多个结果，请自行辨别哪个是与自己聊天记录相关的内容。
![Screenshot](res/4.png)
7. 如图所示，在Json区域，按 **Ctrl+A** 全选内容，复制
![Screenshot](res/5.png)
8. 在本地新建一个文本文档（TXT），然后粘贴刚刚复制的内容，保存。（**建议不要用默认名称“新建 文本文档.txt”， 防止后续程序覆盖了您的重要文件！！！**）
9. 把该 TXT 拖动到本程序上打开，或使用命令行 `chatgpt2md <path/to/conversation.json>`（文件扩展名无需改成 json，txt 也可以）
![Screenshot](res/6.png)
10. 此时应该可以看到，在TXT同目录下，创建一个与该TXT同名的Markdown文件。（**请注意，如果您原本有同名的重要.md文件，请备份，本程序生成操作会覆盖！！！**）

---

## 输入 JSON 格式说明

本脚本假设 JSON 大致符合下述结构（**关键字段**）：

```json
{
  "mapping": {
    "id_root": {
      "id": "id_root",
      "message": {
        "author": {"role": "user"},
        "content": {
          "content_type": "text",
          "parts": ["你好，帮我做个总结。"]
        },
        "create_time": 1724450000.0
      },
      "parent": null
    },
    "id_leaf": {
      "id": "id_leaf",
      "message": {
        "author": {"role": "assistant"},
        "content": {
          "content_type": "multimodal_text",
          "parts": ["当然可以，这是总结..."]
        },
        "create_time": 1724450010.0
      },
      "parent": "id_root"
    }
  },
  "current_node": "id_leaf"
}
```

- **`mapping`**：对话节点表，键为节点 ID。
    
- **`current_node`**：指向当前对话的“叶子节点”（即你最后一次看到的版本）。
    
- **`message.author.role`**：消息角色（`user` / `assistant` / `system` / `tool` ...）。
    
- **`message.content.content_type`**：脚本仅处理 `text` 和 `multimodal_text`。
    
- **`message.content.parts`**：消息文本片段数组；**只有字符串会被合并**输出。
    
- **`message.create_time`**：Unix 时间戳（秒），可为字符串或浮点数。
    

> 如果你的 JSON 略有差异（比如字段名不同），需要先适配或在脚本中增加兼容逻辑。

---

## 输出 Markdown 结构

典型输出（节选）：

```markdown
# 用户
> 时间：2025-08-24 14:35:02
你好，帮我做个总结。

# ChatGPT
> 时间：2025-08-24 14:35:10
当然可以，这是总结...
```

- 标题仅用一级 `#`，便于快速定位角色。
    
- 时间为**本地时区**（使用 `datetime.fromtimestamp`）。
    

---


## 常见问题（FAQ）

**Q1：为什么有些消息没被导出？**  
A：脚本只导出 `role ∈ {user, assistant}` 且 `content_type ∈ {text, multimodal_text}` 的消息，并且仅合并 `parts` 中的**字符串**。图片、文件、代码对象等非字符串内容会被忽略。

**Q2：时间显示不对/时差有偏移？**  
A：脚本使用本地时区来格式化时间戳。若希望固定为 UTC 或指定时区，可在脚本中扩展 `format_time`（见下文“路线图”）。

**Q3：为什么导出的顺序是从最早到最新？**  
A：脚本从 `current_node` 回溯到根节点后再反转，因此输出为“**从根到叶**”，符合阅读顺序。

**Q4：我的 JSON 没有 `mapping` 或 `current_node` 怎么办？**  
A：当前版本依赖这两个字段。你可以：

- 检查导出的文件是否正确；
    
- 或依据你的 JSON 结构，修改 `parse_chat_to_markdown` 中的取值逻辑。
    

**Q5：支持图片或代码块吗？**  
A：默认不支持（非字符串会被跳过）。可以在 `parts` 解析处根据对象结构插入占位或 Markdown 片段（见“路线图”）。

---

## 故障排查

- **`错误：文件 xxx 不存在`**  
    路径检查失败。请确认路径、文件名与扩展名（.json），以及是否有读权限。
    
- **`用法：python chatgpt2md.py <json_file>`**  
    未提供参数。请传入 JSON 文件路径。
    
- **`JSON 解码失败`（如出现 `json.decoder.JSONDecodeError`）**  
    文件不是合法 JSON，或包含 BOM/被截断。请用编辑器校验或重新导出。
    
- **脚本无反应/生成空文件**  
    可能是 JSON 结构不匹配导致所有节点被过滤。建议打印或检查 `mapping` 中节点结构，核对 `role`、`content_type` 与 `parts` 的实际内容。
    

---

## 兼容性说明

- **操作系统**：Windows / macOS / Linux
    
- **Python 版本**：3.8+（仅用标准库）
    
- **输入文件**：含 `mapping` 与 `current_node` 的 ChatGPT 会话 JSON 或兼容结构
    

---

## 安全与隐私

- 脚本在**本地**处理文件，不会联网。
    
- 请确保 JSON 中不含敏感信息；导出与分享 Markdown 前建议进行脱敏处理。
    
- 对于包含第三方或受保护内容的对话，遵守相应的版权与合规要求。
    

---

## 许可证

本项目采用 **MIT License**。
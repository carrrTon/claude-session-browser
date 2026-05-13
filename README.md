# Claude 浏览器

一个本地 Claude Code 会话管理器，用来可视化浏览 `~/.claude/projects` 下的项目、会话和相关文件。

## 功能

- 浏览 Claude Code 历史项目和会话
- 查看 JSONL 会话内容、Markdown 和常见文本文件
- 搜索左侧项目、文件夹、文件和会话
- 重命名会话，不修改实际 `.jsonl` 文件名
- 将选中的项目、文件夹、文件或会话移到废纸篓
- 在 Finder 打开文件夹
- 在 Terminal 中恢复选中的会话
- 双击 `Claude浏览器.command` 一键启动 Safari

## 启动方式

双击：

```text
Claude浏览器.command
```

脚本会自动：

1. 启动本地 Python 服务；
2. 打开 Safari；
3. 页面关闭后尝试自动关闭服务；
4. 下次启动时只关闭本工具自己的旧服务。

## 依赖

- macOS
- Python 3
- Claude Code CLI
- Safari 和 Terminal

## 配置 Claude 命令

默认使用：

```bash
claude-gpt --resume <sessionId>
```

如果你的命令是官方默认 `claude`，可以在启动前设置：

```bash
export CLAUDE_BROWSER_CLI=claude
```

也可以设置为其他兼容命令：

```bash
export CLAUDE_BROWSER_CLI=claude-gpt
```

## 数据目录

默认读取：

```text
~/.claude/projects
```

如需指定其他目录：

```bash
export CLAUDE_PROJECTS_DIR=/path/to/projects
```

## 删除说明

删除操作会调用 Finder 将文件移到废纸篓，不会直接物理删除。删除文件夹前会列出其中的文件，文件很多时只展示前 200 个并显示总数。

## 安全说明

服务默认只绑定本机地址 `127.0.0.1`。页面启动时会生成本地访问 token，删除、重命名、打开 Terminal、打开 Finder 等接口都需要 token。

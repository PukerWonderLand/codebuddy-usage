# codebuddy-usage

本地优先的 **CodeBuddy 用量仪表盘 + 对话 Markdown 归档**。纯 Python 标准库实现，无任何第三方依赖，前端零外部资源（局域网离线可用）。

它是 [`codex-usage`](https://github.com/PukerWonderLand/codex-usage) 与
[`codex-durable-archive`](https://github.com/PukerWonderLand/codex-durable-archive)
在 CodeBuddy 上的对应实现。

## 功能

- **局域网网页仪表盘**：总 token、输入、缓存命中率、输出、推理、计费、会话数；按日趋势、按模型/项目分布、会话与轮次明细、归档记录。
- **对话归档**：每轮结束把「用户提问 + 最终回答」以 verbatim Markdown 写入归档目录（常见做法是挂到 Windows 的 SMB 共享），并镜像原始会话 JSONL 作为审计层；带 SHA-256 与原子写入。
- **开始/结束 token 计数**：`UserPromptSubmit` 记录基线，`Stop` 计算本轮增量与会话累计，并在界面提示。
- **“正在等你”信号**：CodeBuddy 弹出提问/计划批准面板或长时间停在等你输入时，在会话归档目录写入
  `_等待回答.md` / `_等待批准.md` / `_等待输入.md`（内容含题干与选项）。这些文件在面板弹出瞬间生成、你回应后自动删除，
  用于在看不到终端时也能知道它在等你。只有真正结束的回合才会写入归档 md，中途被打断的回合仅记账、不落归档正文。
- **历史回溯**：直接解析 `~/.codebuddy/projects/**/*.jsonl`，安装后立即包含全部历史会话，无需从零开始。
- **零依赖**：只需要 Python 3.10+；不需要 Node、数据库或网络。

## 一键安装

```bash
git clone https://github.com/PukerWonderLand/codebuddy-usage.git
cd codebuddy-usage
./install.sh
```

指定归档目录（例如挂载到 Windows 的 SMB 路径）与端口：

```bash
./install.sh --archive-root "/mnt/share/CodeBuddy自动归档" --port 3766
```

先预览将要做的改动：

```bash
./install.sh --dry-run
```

安装脚本是**幂等**的，可重复执行；它只补充缺失项，不会删除 `~/.codebuddy/settings.json` 里其它 hook 或设置。

### 安装脚本做了什么

1. 校验 `python3 >= 3.10`。
2. 写入/合并配置 `~/.codebuddy-usage/config.json`。
3. 把 `codebuddy-usage`、`codebuddy-dashboard` 链接到 `~/.local/bin`，把 hook 链接到 `~/.codebuddy/hooks/codebuddy_turn_hook.py`。
4. 在 `~/.codebuddy/settings.json` 中注册 `UserPromptSubmit` / `Stop`，以及带 matcher 的 `PreToolUse`（提问/计划面板的收回）与 `Notification`（`permission_prompt` / `idle_prompt` 信号）hook（先备份，重复执行不会产生重复项）。
5. 安装并启动 systemd **用户服务** `codebuddy-dashboard.service`，绑定 `0.0.0.0:3766`；可用时同时开启 linger（免登录开机自启）。

> 重启 CodeBuddy 后 hook 才会生效（CodeBuddy 在启动时快照 hooks 配置）。

## 使用

浏览器打开（把地址换成运行机器）：

```
http://<host>:3766/
```

终端查看账本：

```bash
codebuddy-usage            # 最近一轮明细
codebuddy-usage summary    # 全部轮次汇总
codebuddy-usage json       # 原始 JSONL

codebuddy-dashboard summary        # 复用采集器打印文字版汇总
codebuddy-dashboard run --port 3766  # 手动前台启动仪表盘
```

服务管理：

```bash
systemctl --user status  codebuddy-dashboard
systemctl --user restart codebuddy-dashboard
journalctl --user -u codebuddy-dashboard -f
```

## 配置

优先级：**环境变量 > 配置文件 > 家目录默认值**。

配置文件默认在 `~/.codebuddy-usage/config.json`（可用 `CODEBUDDY_USAGE_CONFIG` 重定位）：

```json
{
  "archive_root": "~/codebuddy-archive",
  "projects_root": "~/.codebuddy/projects"
}
```

| 键 | 环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `archive_root` | `CODEBUDDY_CONVERSATION_ARCHIVE_ROOT` | `~/codebuddy-archive` | Markdown 归档根目录（常指向 SMB 挂载） |
| `projects_root` | `CODEBUDDY_PROJECTS_ROOT` | `~/.codebuddy/projects` | CodeBuddy 会话日志目录 |
| `usage_root` | `CODEBUDDY_USAGE_ROOT` | `~/.codebuddy-usage` | 用量账本与运行时数据 |
| `state_root` | `CODEBUDDY_CONVERSATION_ARCHIVE_STATE` | `~/.codebuddy-turn-state` | hook 暂存与锁 |

## 归档目录结构

```
<archive_root>/<日期>/<标题>__<会话前8位>/
├── 阅读层/<轮次ID>.md     # 用户原文 + Harness 组装(重建) + 最终回答
├── 审计层/<会话ID>.jsonl   # 原始会话日志镜像
└── _等待回答.md            # 见下方「等待信号」（仅在有人需要回应时存在）
```

### 会话命名、改名与日期目录

- **目录名默认取该会话的第一条提示词**（不调用模型，确定性生成，非法字符已按 Windows 规则替换）。
- **你在 CLI 里 `/rename` 之后，之后写入的文件会进一个以新名字命名的目录**，旧目录原样保留（改名不会搬动已有文件，因此历史轮次的路径不会失效）。首个名字会留在 state 的 `initial_title` 与 md 的 `archive_title` 追溯。
  - 判据取自会话日志：`custom-title` 条目（字段 `customTitle`）与 `<local-command-stdout>Session renamed to: X</local-command-stdout>` 消息，取最新的一条。
  - 两者都带 `sessionId`，**只有属于本会话的条目才算数**——从一段对话"复制出去"继续聊时，日志里会带着父会话的条目，父会话的改名不会被误认成你的。
- **新会话固定落在"首次出现的那一天"**，跨天继续聊不会在第二个日期目录里再冒出一个同名目录。此前已存在的会话保持原行为（按轮次日期），不会有文件被悄悄搬走。
- **仪表盘的会话列表也认改名**：`custom-title` 优先于模型自动生成的 `ai-title`——改名是你的主动行为，应该盖过自动标题。

### 等待信号（`_等待回答.md` / `_等待批准.md` / `_等待输入.md`）

当 CodeBuddy 停下来等人时，会在会话目录里出现一个临时文件，**同一时刻最多只有一个**：

| 文件 | 触发 | 内容 |
| :--- | :--- | :--- |
| `_等待回答.md` | `AskUserQuestion` 面板弹出的瞬间 | 题干 + 全部选项 + 原始工具调用 |
| `_等待批准.md` | `ExitPlanMode` 计划批准面板弹出的瞬间 | 计划正文 |
| `_等待输入.md` | 回合尚未结束、空闲 60 秒（兜底） | 说明会话在等你 |

这些文件在**你回应后立即删除**（`PreToolUse` 触发），回合结束时也会清理，因此"文件存在"就等于"它还在等你"。
参数细节：提问/计划面板的正文比面板晚约 0.3 秒落盘，所以信号会先写一版、约 1.2 秒后自动补全正文。

只有**真正结束**的回合才会写入 `阅读层`。账本里的 `turn_status` 会把其余情况标清楚：

| turn_status | 含义 | 是否写归档 |
| :--- | :--- | :--- |
| `completed` | 真 `Stop`，正常结束 | ✅ |
| `recovered` | 回合确实结束了，但 Stop 事件没送到（进程被杀、hook 被摘），由下一条提示词从日志补回 | ✅（frontmatter 标 `recovered: true`） |
| `interrupted_by_user` | 你按 Esc / Ctrl-C 打断；该轮最后一条只有 19 字节的 `Interrupted by user` 占位符 | ❌ 仅记账 |
| `interrupted_pending_question` | 提问面板没回答就换了话题 | ❌ 仅记账 |
| `superseded_catchup` | 回合还在进行（有工具调用在飞、或停在工具结果上）就被下一条提示词取代 | ❌ 仅记账 |

也就是说：**绝不在对话中间落半截 Markdown**，也不会把"Interrupted by user"这种占位符当成回答收进归档。

反过来，**回合真的结束、但 `Stop` 事件没送到**（进程被杀、hook 被摘）时，下一条提示词到来会从日志把它补回：`turn_status: recovered`，归档 frontmatter 标 `recovered: true`。判据取自日志本身——该轮所有工具调用都有结果、结尾是一条 `status: completed` 的最终回答；被用户打断的消息日志标的是 `status: incomplete`，因此不会被误当成回答收进去。

> 一个容易踩的坑：**会话日志的落盘滞后于 `Stop` 事件**。hook 运行的那一刻，日志里可能还没有最后那条回答（实测差约 300ms），只读日志会把中间的旁白当成回答（曾把 7522 字节的回答归档成 184 字节）。所以回答优先取 Stop 事件自带的 `last_assistant_message`（来自内存里的运行结果），日志只作兜底与补回。

### 关于「Harness 组装（重建）」

harness 在请求时拼入的完整上下文（系统指令 / instructions / environment_context / 权限 / 技能 / 记忆 / 全量历史）**本地不落盘**：会话日志只保存会话事件，`traces/` 只有时序 span，`CODEBUDDY_DEBUG_REQUEST` 也会把每条 message 截断到 500 字符。

因此阅读层中的这一节是**重建而非逐字**，明确包含：
- **本轮日志中记录的 reminder 块**（逐字，来自会话日志；日志不区分“注入给模型”与“仅显示给用户”）
- **引用的上下文文件清单**（`CODEBUDDY.md` / `AGENTS.md` / `memory/*.md` 的路径、大小、SHA-256；是**磁盘当前版本**，不是请求时快照）
- **历史规模**（消息数、累计 tokens / 缓存命中）
- **未持久化（不可得）清单**，逐项列出哪些内容无法从本地恢复

frontmatter 里以 `harness_section: reconstructed` 标注该节性质。若需要**逐字完整请求**，只能通过本地 HTTPS 代理截获（CodeBuddy 支持 `HTTPS_PROXY`），属于可选的额外工程。

## 卸载

```bash
./uninstall.sh            # 移除 hook/服务/软链，保留数据
./uninstall.sh --purge    # 连同 ~/.codebuddy-usage、~/.codebuddy-turn-state 一起删除
```

## 环境要求

- Python 3.10+（macOS 自带的是 3.9，需用 `brew` 或 `uv python install` 装 3.10+）
- Linux **或** macOS。后台服务在 Linux 上用 systemd `--user`，在 macOS 上用 launchd LaunchAgent（`com.pukerwonderland.codebuddy-dashboard`）；两者都没有时用 `codebuddy-dashboard run` 手动启动
- 归档写入的目录需可写（SMB/NFS 挂载亦可）

## 给 AI 代理

想让另一个 AI 在别的电脑上复刻这套环境，把仓库交给它并让它阅读
[`AGENTS.md`](AGENTS.md)——里面有确定性的逐步安装与校验命令。

## 目录结构

```
codebuddy-usage/
├── install.sh / uninstall.sh     # 一键安装 / 卸载
├── config.example.json
├── bin/                          # 命令入口（解析自身真实路径，可软链）
│   ├── codebuddy-usage
│   └── codebuddy-dashboard
├── hooks/codebuddy_turn_hook.py  # CodeBuddy hook：归档 + token 记账 + 等待信号
├── tests/test_turn_hook.py       # hook 回归测试（纯标准库，沙盒内运行，不碰真实归档）
├── src/
│   ├── config.py                 # 可移植路径解析
│   ├── collector.py              # 扫描会话日志，回溯全部历史
│   ├── server.py                 # 标准库 HTTP 服务 + JSON API
│   ├── cli.py                    # dashboard CLI
│   └── viewer.py                 # 账本查看 CLI
├── public/                       # 前端（零外部依赖）
├── systemd/codebuddy-dashboard.service.in                     # Linux 服务模板
└── launchd/com.pukerwonderland.codebuddy-dashboard.plist.in   # macOS 服务模板
```

## License

MIT

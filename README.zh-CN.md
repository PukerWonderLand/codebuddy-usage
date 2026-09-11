# codebuddy-usage

本地优先的 **CodeBuddy 用量仪表盘 + 对话 Markdown 归档**。纯 Python 标准库实现，无任何第三方依赖，前端零外部资源（局域网离线可用）。

它是 [`codex-usage`](https://github.com/PukerWonderLand/codex-usage) 与
[`codex-durable-archive`](https://github.com/PukerWonderLand/codex-durable-archive)
在 CodeBuddy 上的对应实现。

## 功能

- **局域网网页仪表盘**：总 token、输入、缓存命中率、输出、推理、计费、会话数；按日趋势、按模型/项目分布、会话与轮次明细、归档记录。
- **对话归档**：每轮结束把「用户提问 + 最终回答」以 verbatim Markdown 写入归档目录（常见做法是挂到 Windows 的 SMB 共享），并镜像原始会话 JSONL 作为审计层；带 SHA-256 与原子写入。
- **开始/结束 token 计数**：`UserPromptSubmit` 记录基线，`Stop` 计算本轮增量与会话累计，并在界面提示。
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
4. 在 `~/.codebuddy/settings.json` 中注册 `UserPromptSubmit` / `Stop` 两个 hook（先备份，重复执行不会产生重复项）。
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
├── 阅读层/<轮次ID>.md     # 用户原文 + 最终回答，verbatim，含 SHA-256
└── 审计层/<会话ID>.jsonl   # 原始会话日志镜像
```

## 卸载

```bash
./uninstall.sh            # 移除 hook/服务/软链，保留数据
./uninstall.sh --purge    # 连同 ~/.codebuddy-usage、~/.codebuddy-turn-state 一起删除
```

## 环境要求

- Python 3.10+
- Linux（systemd 可选；没有 systemd 可用 `codebuddy-dashboard run` 手动启动）
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
├── hooks/codebuddy_turn_hook.py  # CodeBuddy hook：归档 + token 记账
├── src/
│   ├── config.py                 # 可移植路径解析
│   ├── collector.py              # 扫描会话日志，回溯全部历史
│   ├── server.py                 # 标准库 HTTP 服务 + JSON API
│   ├── cli.py                    # dashboard CLI
│   └── viewer.py                 # 账本查看 CLI
├── public/                       # 前端（零外部依赖）
└── systemd/codebuddy-dashboard.service.in
```

## License

MIT

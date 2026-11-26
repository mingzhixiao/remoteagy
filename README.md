# RemoteAgy

面向 **Antigravity CLI (`agy`)** 的轻量级自托管网页端与 **微信 ClawBot (iLink)** 接入服务，同时适配桌面端与手机浏览器，让你在自己的服务器上安全、直观地远程操控本地 `agy`。

## 核心特性

* **Antigravity 风格界面**：深色双栏布局（桌面端常驻项目树、手机端自动切换为抽屉菜单），居中对话流与浮动输入框。
* **多模态传图提问**：支持手机相册选图 / 拍照上传、桌面端截图粘贴，图片自动保存至当前项目目录并通过 `@路径` 传给 `agy`。
* **完整历史与过程回溯**：
  * 自动读取本机 `conversation_summaries.db` 与 `transcript.jsonl`，兼容 CLI、Remote Control 与网页端的所有历史对话并支持接续。
  * 支持展开查看 `Worked for Xs` 思考过程与工具调用轨迹。
  * 支持查看本轮修改文件统计与红绿高亮 **Files Review** Diff 弹窗。
* **模型与额度管理**：支持两级模型选择器（`Model` + `Thinking effort`）、实时查询账户配额（`/usage`）、以及网页端 **Create Project** 一键建目录。
* **微信 ClawBot & QQ 官方机器人接入**：
  * **微信**：内置腾讯官方 iLink Bot 协议客户端，网页端扫码即可绑定个人微信。
  * **QQ**：内置腾讯官方 QQ 机器人 OpenAPI v2 + WebSocket 网关客户端（支持私聊 `C2C` 与群 `@机器人`），无需公网回调域名。
  * 两者均支持在聊天中切换项目、切换模型、查看额度，且与网页端共享同一套本地会话历史。

---

## 环境要求与依赖组件

### 1. 必需组件
* **Linux 服务器**：推荐 Debian / Ubuntu
* **Antigravity CLI (`agy`)**：核心 Agent 执行引擎，需预先安装并完成账号登录授权（默认路径 `/root/.local/bin/agy`）
* **Python `3.10+`** 及 `requirements.txt` 中的库：
  * `fastapi` + `uvicorn`：Web 服务、SSE 流式响应与静态页面托管
  * `httpx`：异步 HTTP 客户端，用于与腾讯微信 iLink / QQ OpenAPI 通信
  * `websockets`：用于连接 QQ 官方机器人 WebSocket 网关
  * `pydantic`：请求参数校验
  * `qrcode` + `Pillow`：将微信登录链接渲染为前端可直接扫码的 PNG 二维码
* **SQLite 3**（Python 标准库自带）：只读读取本机 `conversation_summaries.db` 历史会话索引

### 2. 可选组件
* **Git**：当项目目录本身是 Git 仓库时，用于 **Files Review** 补充 `git diff` 差异展示
* **systemd**：`deploy.sh` 用于注册守护进程与开机自启（无 systemd 环境会自动回退到后台进程启动）
* **反向代理（Nginx / OpenResty / Caddy 等）**：用于公网 HTTPS 访问与自定义路径挂载

### 3. 不需要依赖的组件（重要）
* **不依赖 `ttyd`、`tmux`、`trzsz`、`lrzsz`**：本项目不是网页黑框终端套壳，而是直接通过 `agy --output-format stream-json` 与本地 CLI 交互，前端独立实现聊天、传图、思考过程与代码 Review。只有在你额外需要一个浏览器纯终端作为兜底时，才需自行安装 `ttyd + tmux + trzsz`。

---

## 一键部署

```bash
git clone https://github.com/mingzhixiao/remoteagy.git
cd remoteagy
chmod +x deploy.sh
./deploy.sh
```

`deploy.sh` 会自动完成以下步骤：
1. 创建 `.venv` 虚拟环境并安装 `requirements.txt` 依赖。
2. 若不存在 `.env`，自动生成带随机高强度密码的 `.env` 配置文件（权限 `600`）。
3. 在支持 `systemd` 的环境下注册并启动 `remoteagy.service`（开机自启），否则以后台进程方式启动在 `127.0.0.1:7682`。

---

## 手动运行

```bash
pip install -r requirements.txt
cp .env.example .env
# 按需修改 .env 中的账号密码、工作目录根目录和 AGY_BIN 路径
python3 -m uvicorn server:app --host 127.0.0.1 --port 7682
```

---

## 配置说明（`.env`）

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `REMOTEAGY_USER` | `agy` | Web Basic Auth 登录用户名 |
| `REMOTEAGY_PASS` | - | Web Basic Auth 登录密码 |
| `REMOTEAGY_PORT` | `7682` | 后端监听端口 |
| `DEFAULT_CWD` | `/wzw/remoteagy` | 默认工作目录 |
| `PROJECTS_ROOT` | `/wzw` | 扫描与新建项目的根目录 |
| `AGY_BIN` | `/root/.local/bin/agy` | `agy` 可执行文件路径 |

---

## 微信 ClawBot 接入指南

1. 在网页右上角点击 **`💬 微信`** -> **生成扫码二维码**。
2. 使用新版手机微信（iOS `8.0.70+` / Android `8.0.69+`）扫码并确认登录。
3. 绑定成功后，仅响应**扫码本人**的微信消息（白名单拦截陌生人），支持以下快捷指令：
   * `/help` — 查看全部指令
   * `/projects` — 列出所有项目（带序号）
   * `/project <序号或名称>` — 切换工作目录
   * `/newproject <名称>` — 新建项目目录并切换
   * `/models` — 列出可用模型（带序号）
   * `/model <序号或ID>` — 切换模型（不带参数恢复默认）
   * `/quota` 或 `/额度` — 查看各模型分组剩余额度与重置时间
   * `/status` — 查看当前工作目录、会话与模型
   * `/new` — 开启全新对话

---

## QQ 官方机器人接入指南

1. 前往 [QQ 开放平台](https://q.qq.com/) 创建机器人应用，获取 `AppID` 与 `AppSecret`（需开启私聊/群 `@` 权限）。
2. 在 `RemoteAgy` 网页右上角点击 **`🐧 QQ`**，填入 `AppID` 与 `AppSecret`（可选填白名单 OpenID，留空则自动绑定首位私聊你的用户），点击 **保存并连接**。
3. 后端会自动通过 WebSocket 网关长连接接入 QQ，无需配置公网回调地址，支持的斜杠指令与微信端完全一致（`/help`、`/projects`、`/project`、`/newproject`、`/models`、`/model`、`/quota`、`/status`、`/new`）。

---

## 安全与公网访问建议

* 服务默认仅绑定 `127.0.0.1:7682`，请勿直接将 HTTP 明文端口暴露到公网。
* 建议使用 Nginx / OpenResty / Caddy 等反向代理配置 **HTTPS**（需开启 WebSocket / SSE 流式转发且关闭代理缓冲）后再从公网或手机端访问。

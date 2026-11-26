import asyncio
import json
import os
import re
from pathlib import Path
from typing import Awaitable, Callable, List


class BotBridgeHelper:
    """Shared slash-command handler and agy runner for WeChat and QQ bots."""

    def __init__(
        self,
        agy_bin: str,
        default_cwd: str,
        projects_root: Path,
        list_projects_fn: Callable[[], List[str]],
        get_models_fn: Callable[[], Awaitable[List[dict]]],
        get_quota_fn: Callable[[bool], Awaitable[dict]],
        channel_name: str = "Bot",
    ):
        self.agy_bin = agy_bin
        self.default_cwd = default_cwd
        self.projects_root = projects_root
        self.list_projects_fn = list_projects_fn
        self.get_models_fn = get_models_fn
        self.get_quota_fn = get_quota_fn
        self.channel_name = channel_name

    async def handle_command(self, text: str, sess: dict) -> str:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/help", "/指令"):
            return (
                f"RemoteAgy {self.channel_name} 指令：\n"
                "/new - 开启新对话\n"
                "/projects - 查看可用项目（带序号）\n"
                "/project <序号或名称> - 切换工作目录\n"
                "/newproject <名称> - 新建项目目录并切换\n"
                "/models - 查看可用模型（带序号）\n"
                "/model <序号或ID> - 切换模型（不带参数恢复默认）\n"
                "/quota 或 /额度 - 查看当前模型额度\n"
                "/status - 查看当前状态\n"
                "直接发送文字即可与 agy 对话。"
            )
        if cmd == "/new":
            sess["conversation_id"] = ""
            return f"已开启新对话（当前项目：{sess.get('cwd')}）"
        if cmd == "/projects":
            projs = self.list_projects_fn()
            return "可用项目（可用 /project 序号 切换）：\n" + "\n".join(
                f"{i}. {Path(p).name} ({p})" for i, p in enumerate(projs, 1)
            )
        if cmd == "/project":
            projs = self.list_projects_fn()
            if not arg:
                return f"当前项目：{sess.get('cwd')}\n用法：/project 序号 或 项目名"
            if arg.isdigit() and 1 <= int(arg) <= len(projs):
                match = projs[int(arg) - 1]
            else:
                match = next((p for p in projs if p == arg or Path(p).name == arg), None)
            if not match:
                return f"未找到项目 {arg}，发 /projects 查看列表"
            sess["cwd"] = match
            sess["conversation_id"] = ""
            return f"已切换到项目：{match}（并开启新对话）"
        if cmd == "/newproject":
            if not arg or not re.match(r"^[a-zA-Z0-9._-]+$", arg) or arg in {".", ".."}:
                return "用法：/newproject 项目名（仅支持字母、数字、._-）"
            target = (self.projects_root / arg).resolve()
            target.mkdir(parents=True, exist_ok=True)
            sess["cwd"] = str(target)
            sess["conversation_id"] = ""
            return f"已创建并切换到项目：{target}"
        if cmd == "/models":
            ms = await self.get_models_fn()
            if not ms:
                return "暂无模型列表"
            return "可用模型（可用 /model 序号 切换）：\n" + "\n".join(
                f"{i}. {m['label']} [{m['id']}]" for i, m in enumerate(ms[:25], 1)
            )
        if cmd == "/model":
            if not arg:
                sess["model"] = ""
                return "已恢复为：默认模型"
            ms = await self.get_models_fn()
            if arg.isdigit() and 1 <= int(arg) <= len(ms):
                chosen = ms[int(arg) - 1]
                sess["model"] = chosen["id"]
                return f"已切换模型为：{chosen['label']} ({chosen['id']})"
            sess["model"] = arg
            return f"已切换模型为：{arg}"
        if cmd in ("/quota", "/额度"):
            res = await self.get_quota_fn(True)
            if not res.get("ok"):
                return f"获取额度失败：{res.get('error', '未知错误')}"
            groups = (res.get("data") or {}).get("groups") or []
            if not groups:
                return "未获取到额度信息"
            lines = ["📊 当前模型额度："]
            for g in groups:
                lines.append(f"\n【{g.get('name', 'Group')}】")
                for b in g.get("buckets") or []:
                    pct = round(float(b.get("remaining_fraction", 1.0)) * 100)
                    win = "5小时" if b.get("window") == "5h" else ("每周" if b.get("window") == "weekly" else b.get("name", ""))
                    rst = str(b.get("reset_time") or "")
                    rst_str = "满额/未消耗" if (not rst or rst.startswith("1970-")) else rst[:19].replace("T", " ")
                    lines.append(f"  - {win}：{pct}%（重置：{rst_str}）")
            return "\n".join(lines)
        if cmd == "/status":
            return (
                f"当前项目：{sess.get('cwd')}\n"
                f"当前会话：{sess.get('conversation_id') or '新对话'}\n"
                f"当前模型：{sess.get('model') or '默认模型'}"
            )
        return f"未知指令 {cmd}，发送 /help 查看全部指令"

    async def run_agy(self, prompt: str, sess: dict) -> str:
        cwd = Path(sess.get("cwd") or self.default_cwd)
        if not cwd.exists():
            cwd = Path(self.default_cwd)

        cmd = [
            self.agy_bin,
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
        ]
        if sess.get("conversation_id"):
            cmd.extend(["--conversation", sess["conversation_id"]])
        if sess.get("model"):
            cmd.extend(["--model", sess["model"]])
        cmd.append(f"-p={prompt}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PATH": f"/root/.local/bin:{os.environ.get('PATH', '')}"},
        )
        out_bytes, err_bytes = await proc.communicate()
        acc_text: List[str] = []
        final_resp = ""
        for line in out_bytes.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") == "init" and ev.get("conversation_id"):
                sess["conversation_id"] = ev["conversation_id"]
            elif ev.get("event") == "step_update":
                su = ev.get("step_update") or {}
                if su.get("text_delta"):
                    acc_text.append(su["text_delta"])
            elif ev.get("event") == "result":
                r = ev.get("result") or {}
                if r.get("conversation_id"):
                    sess["conversation_id"] = r["conversation_id"]
                if r.get("response"):
                    final_resp = r["response"]

        text = final_resp or "".join(acc_text)
        if not text and proc.returncode != 0:
            err = err_bytes.decode("utf-8", errors="replace").strip()
            return f"执行出错 (code={proc.returncode}): {err[:500]}"
        return text.strip()

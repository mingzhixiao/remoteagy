import asyncio
import base64
import io
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import quote

import httpx

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.6"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 6)
BOT_AGENT = "RemoteAgy/1.0.0 (python)"

QR_STATUS_TIMEOUT = 35.0
LONG_POLL_TIMEOUT = 35.0
API_TIMEOUT = 15.0


class ILinkError(RuntimeError):
    def __init__(self, message: str, *, ret=None, errcode=None):
        super().__init__(message)
        self.ret = ret
        self.errcode = errcode

    @property
    def is_stale_token(self) -> bool:
        code = self.ret if self.ret not in (None, 0) else self.errcode
        return code == -14


def make_common_headers() -> Dict[str, str]:
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }


def make_headers(token: Optional[str] = None) -> Dict[str, str]:
    uin = str(secrets.randbits(32))
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
        **make_common_headers(),
    }
    if token and str(token).strip():
        headers["Authorization"] = f"Bearer {str(token).strip()}"
    return headers


def base_info() -> Dict[str, str]:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def generate_client_id() -> str:
    return f"openclaw-weixin:{int(time.time() * 1000)}-{secrets.token_hex(4)}"


def render_qr_data_url(content: str) -> str:
    if not content:
        return ""
    if content.startswith("data:image/"):
        return content
    try:
        import qrcode

        qr = qrcode.QRCode(border=2, box_size=7)
        qr.add_data(content)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""


def extract_message_text(msg: dict) -> str:
    parts: List[str] = []
    for item in msg.get("item_list") or []:
        if not isinstance(item, dict):
            continue
        ti = item.get("text_item") or {}
        if ti.get("text"):
            parts.append(str(ti["text"]))
            continue
        vi = item.get("voice_item") or {}
        if vi.get("text"):
            parts.append(str(vi["text"]))
    return "\n".join(parts).strip()


class WeChatILinkManager:
    def __init__(
        self,
        state_file: Path,
        agy_bin: str,
        default_cwd: str,
        projects_root: Path,
        list_projects_fn: Callable[[], List[str]],
        get_models_fn: Callable[[], Awaitable[List[dict]]],
        get_quota_fn: Callable[[bool], Awaitable[dict]],
    ):
        self.state_file = state_file
        self.agy_bin = agy_bin
        self.default_cwd = default_cwd
        self.projects_root = projects_root
        self.list_projects_fn = list_projects_fn
        self.get_models_fn = get_models_fn
        self.get_quota_fn = get_quota_fn

        self.state = self._load_state()
        self.qr_status = "idle"  # idle | wait | scanned | need_verifycode | confirmed | expired | error
        self.qr_data_url = ""
        self.qr_raw = ""
        self.last_error = ""
        self.logs: List[str] = []
        self.verify_code_queue: asyncio.Queue[str] = asyncio.Queue()

        self._qr_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._user_locks: Dict[str, asyncio.Lock] = {}

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.logs.append(line)
        if len(self.logs) > 40:
            self.logs = self.logs[-40:]

    def _empty_state(self) -> dict:
        return {
            "bot_token": "",
            "baseurl": ILINK_BASE_URL,
            "ilink_bot_id": "",
            "ilink_user_id": "",
            "get_updates_buf": "",
            "sessions": {},
        }

    def _load_state(self) -> dict:
        st = self._empty_state()
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k in st:
                        if k in raw:
                            st[k] = raw[k]
            except Exception:
                pass
        if not isinstance(st.get("sessions"), dict):
            st["sessions"] = {}
        return st

    def _save_state(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def is_connected(self) -> bool:
        return bool(self.state.get("bot_token") and self.state.get("ilink_bot_id"))

    def status_dict(self) -> dict:
        return {
            "connected": self.is_connected(),
            "ilink_user_id": self.state.get("ilink_user_id", ""),
            "ilink_bot_id": self.state.get("ilink_bot_id", ""),
            "qr_status": self.qr_status,
            "qr_data_url": self.qr_data_url,
            "qr_raw": self.qr_raw,
            "last_error": self.last_error,
            "logs": self.logs[-15:],
        }

    async def start_if_configured(self) -> None:
        if self.is_connected():
            self._log("检测到已保存的微信凭证，启动后台消息监听")
            self._ensure_poll_task()

    def _ensure_poll_task(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop_all(self) -> None:
        for t in (self._qr_task, self._poll_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    async def disconnect(self) -> None:
        await self.stop_all()
        self.state = self._empty_state()
        self._save_state()
        self.qr_status = "idle"
        self.qr_data_url = ""
        self.qr_raw = ""
        self.last_error = ""
        self._log("已断开微信连接并清除凭证")

    async def _post(self, client: httpx.AsyncClient, path: str, body: dict, token: Optional[str] = None,
                    base_url: Optional[str] = None, timeout: float = API_TIMEOUT, long_poll: bool = False) -> dict:
        url = f"{(base_url or ILINK_BASE_URL).rstrip('/')}/{path.lstrip('/')}"
        try:
            r = await client.post(url, json=body, headers=make_headers(token), timeout=timeout)
            if not (200 <= r.status_code < 300):
                raise ILinkError(f"POST {path} HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            ret = data.get("ret")
            err = data.get("errcode")
            if ret not in (None, 0) or err not in (None, 0):
                raise ILinkError(
                    data.get("errmsg") or f"ret={ret} errcode={err}",
                    ret=ret,
                    errcode=err,
                )
            return data
        except httpx.ReadTimeout:
            if long_poll:
                return {"ret": 0, "msgs": [], "_timeout": True}
            raise ILinkError(f"POST {path} 超时")

    async def _get(self, client: httpx.AsyncClient, path: str, base_url: Optional[str] = None,
                   timeout: float = QR_STATUS_TIMEOUT, long_poll: bool = False) -> dict:
        url = f"{(base_url or ILINK_BASE_URL).rstrip('/')}/{path.lstrip('/')}"
        try:
            r = await client.get(url, headers=make_common_headers(), timeout=timeout)
            if not (200 <= r.status_code < 300):
                raise ILinkError(f"GET {path} HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            ret = data.get("ret")
            err = data.get("errcode")
            if ret not in (None, 0) or err not in (None, 0):
                raise ILinkError(
                    data.get("errmsg") or f"ret={ret} errcode={err}",
                    ret=ret,
                    errcode=err,
                )
            return data
        except httpx.ReadTimeout:
            if long_poll:
                return {"status": "wait", "_timeout": True}
            raise ILinkError(f"GET {path} 超时")

    async def start_qr_login(self) -> dict:
        if self._qr_task and not self._qr_task.done():
            self._qr_task.cancel()
            try:
                await self._qr_task
            except asyncio.CancelledError:
                pass

        while not self.verify_code_queue.empty():
            self.verify_code_queue.get_nowait()

        self.last_error = ""
        self.qr_status = "loading"
        self.qr_data_url = ""
        self.qr_raw = ""

        tokens = [self.state["bot_token"]] if self.state.get("bot_token") else []
        async with httpx.AsyncClient() as client:
            try:
                data = await self._post(
                    client,
                    "ilink/bot/get_bot_qrcode?bot_type=3",
                    {"local_token_list": tokens},
                    None,
                    ILINK_BASE_URL,
                    timeout=20.0,
                )
            except Exception as exc:
                self.qr_status = "error"
                self.last_error = str(exc)
                self._log(f"获取二维码失败: {exc}")
                return self.status_dict()

        qrcode_id = data.get("qrcode", "")
        raw_content = data.get("qrcode_img_content") or data.get("url") or qrcode_id
        self.qr_raw = raw_content
        self.qr_data_url = render_qr_data_url(raw_content)
        self.qr_status = "wait"
        self._log("已生成登录二维码，等待微信扫码")

        self._qr_task = asyncio.create_task(self._qr_poll_loop(qrcode_id))
        return self.status_dict()

    async def submit_verify_code(self, code: str) -> None:
        await self.verify_code_queue.put(code.strip())

    async def _qr_poll_loop(self, qrcode_id: str) -> None:
        poll_base = ILINK_BASE_URL
        verify_code = None
        deadline = time.time() + 420
        try:
            async with httpx.AsyncClient() as client:
                while time.time() < deadline:
                    ep = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode_id, safe='')}"
                    if verify_code:
                        ep += f"&verify_code={quote(verify_code, safe='')}"
                    res = await self._get(client, ep, base_url=poll_base, timeout=QR_STATUS_TIMEOUT, long_poll=True)
                    if res.get("_timeout"):
                        continue
                    st = res.get("status", "")
                    if st == "confirmed" or res.get("bot_token"):
                        bot_token = res.get("bot_token", "")
                        bot_id = res.get("ilink_bot_id", "")
                        user_id = res.get("ilink_user_id", "")
                        baseurl = res.get("baseurl") or res.get("base_url") or poll_base
                        if not bot_token or not bot_id:
                            self.qr_status = "error"
                            self.last_error = "登录响应缺少 bot_token 或 ilink_bot_id"
                            return
                        prev_user = self.state.get("ilink_user_id", "")
                        if prev_user and user_id and prev_user != user_id:
                            self.state = self._empty_state()
                        self.state["bot_token"] = bot_token
                        self.state["baseurl"] = baseurl
                        self.state["ilink_bot_id"] = bot_id
                        if user_id:
                            self.state["ilink_user_id"] = user_id
                        self._save_state()
                        self.qr_status = "confirmed"
                        self.qr_data_url = ""
                        self._log(f"微信登录成功 (user={user_id or 'unknown'})")
                        self._ensure_poll_task()
                        return
                    if st == "scaned_but_redirect":
                        rh = res.get("redirect_host")
                        if rh:
                            poll_base = rh if str(rh).startswith("http") else f"https://{rh}"
                        self.qr_status = "scanned"
                    elif st == "scaned":
                        self.qr_status = "scanned"
                        self._log("已扫码，请在手机微信上点击确认")
                    elif st in ("need_verifycode", "verify_code_blocked") or res.get("need_verifycode"):
                        if st == "verify_code_blocked":
                            self.qr_status = "error"
                            self.last_error = "验证码次数受限，请稍后再试"
                            return
                        self.qr_status = "need_verifycode"
                        self._log("需要输入微信验证码")
                        verify_code = await self.verify_code_queue.get()
                    elif st == "expired":
                        self.qr_status = "expired"
                        self._log("二维码已过期，请重新生成")
                        return
                self.qr_status = "expired"
                self._log("扫码等待超时")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.qr_status = "error"
            self.last_error = str(exc)
            self._log(f"扫码轮询异常: {exc}")

    async def _send_text(self, client: httpx.AsyncClient, to_id: str, context_token: str, text: str) -> None:
        if not to_id or not context_token:
            return
        chunks = [text[i : i + 1800] for i in range(0, len(text), 1800)] or [""]
        for chunk in chunks:
            await self._post(
                client,
                "ilink/bot/sendmessage",
                {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": to_id,
                        "client_id": generate_client_id(),
                        "message_type": 2,
                        "message_state": 2,
                        "context_token": context_token,
                        "item_list": [{"type": 1, "text_item": {"text": chunk}}],
                    },
                    "base_info": base_info(),
                },
                token=self.state.get("bot_token"),
                base_url=self.state.get("baseurl") or ILINK_BASE_URL,
                timeout=API_TIMEOUT,
            )

    async def _poll_loop(self) -> None:
        long_poll_timeout = LONG_POLL_TIMEOUT
        failures = 0
        async with httpx.AsyncClient() as client:
            while self.is_connected():
                try:
                    buf = str(self.state.get("get_updates_buf") or "")
                    res = await self._post(
                        client,
                        "ilink/bot/getupdates",
                        {"get_updates_buf": buf, "base_info": base_info()},
                        token=self.state.get("bot_token"),
                        base_url=self.state.get("baseurl") or ILINK_BASE_URL,
                        timeout=long_poll_timeout,
                        long_poll=True,
                    )
                    if res.get("_timeout"):
                        continue
                    failures = 0
                    new_buf = res.get("get_updates_buf")
                    if isinstance(new_buf, str) and new_buf:
                        self.state["get_updates_buf"] = new_buf
                        self._save_state()

                    sugg_ms = res.get("longpolling_timeout_ms")
                    if isinstance(sugg_ms, (int, float)) and sugg_ms > 0:
                        long_poll_timeout = max(5.0, min(90.0, float(sugg_ms) / 1000.0))

                    for msg in res.get("msgs") or []:
                        asyncio.create_task(self._handle_inbound(client, msg))
                except asyncio.CancelledError:
                    raise
                except ILinkError as exc:
                    if exc.is_stale_token:
                        self._log("微信登录凭证已过期 (-14)，请重新扫码")
                        self.state["bot_token"] = ""
                        self._save_state()
                        self.qr_status = "idle"
                        return
                    failures += 1
                    self._log(f"轮询错误 ({failures}): {exc}")
                    await asyncio.sleep(min(30, 2 * failures))
                except Exception as exc:
                    failures += 1
                    self._log(f"轮询异常 ({failures}): {exc}")
                    await asyncio.sleep(min(30, 2 * failures))

    async def _handle_inbound(self, client: httpx.AsyncClient, msg: dict) -> None:
        if msg.get("message_type") not in (None, 1):
            return
        from_id = str(msg.get("from_user_id") or "")
        ctx = str(msg.get("context_token") or "")
        if not from_id or not ctx:
            return

        owner_id = str(self.state.get("ilink_user_id") or "")
        if owner_id and from_id != owner_id:
            self._log(f"已拦截非本人消息: {from_id}")
            return

        text = extract_message_text(msg)
        if not text:
            return

        lock = self._user_locks.setdefault(from_id, asyncio.Lock())
        async with lock:
            self._log(f"收到微信消息: {text[:40]}")
            sess = self.state["sessions"].setdefault(
                from_id, {"conversation_id": "", "cwd": self.default_cwd, "model": ""}
            )
            if text.startswith("/"):
                reply = await self._handle_command(text, sess)
                self._save_state()
                await self._send_text(client, from_id, ctx, reply)
                return

            await self._send_text(client, from_id, ctx, "⏳ 正在思考并执行中...")
            reply = await self._run_agy(text, sess)
            self._save_state()
            await self._send_text(client, from_id, ctx, reply or "（无输出）")

    async def _handle_command(self, text: str, sess: dict) -> str:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/help", "/指令"):
            return (
                "RemoteAgy 微信指令：\n"
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
            match = None
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

    async def _run_agy(self, prompt: str, sess: dict) -> str:
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

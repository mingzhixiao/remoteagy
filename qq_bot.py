import asyncio
import json
import os
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import httpx
import websockets

from bot_common import BotBridgeHelper

QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_API_BASE = "https://api.sgroup.qq.com"
QQ_SANDBOX_BASE = "https://sandbox.api.sgroup.qq.com"
# GROUP_AND_C2C_EVENT (1 << 25) | PUBLIC_GUILD_MESSAGES (1 << 30)
QQ_INTENTS = (1 << 25)


class QQBotManager:
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
        self.helper = BotBridgeHelper(
            agy_bin=agy_bin,
            default_cwd=default_cwd,
            projects_root=projects_root,
            list_projects_fn=list_projects_fn,
            get_models_fn=get_models_fn,
            get_quota_fn=get_quota_fn,
            channel_name="QQ",
        )
        self.default_cwd = default_cwd
        self.state = self._load_state()
        self.connected = False
        self.bot_username = ""
        self.last_error = ""
        self.logs: List[str] = []

        self._access_token = ""
        self._token_expires_at = 0.0
        self._ws_task: Optional[asyncio.Task] = None
        self._user_locks: Dict[str, asyncio.Lock] = {}

    def _log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")
        if len(self.logs) > 40:
            self.logs = self.logs[-40:]

    def _empty_state(self) -> dict:
        return {
            "app_id": "",
            "app_secret": "",
            "sandbox": False,
            "allowed_openids": [],
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
        if not isinstance(st.get("allowed_openids"), list):
            st["allowed_openids"] = []
        return st

    def _save_state(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _api_base(self) -> str:
        return QQ_SANDBOX_BASE if self.state.get("sandbox") else QQ_API_BASE

    def is_configured(self) -> bool:
        return bool(self.state.get("app_id") and self.state.get("app_secret"))

    def status_dict(self) -> dict:
        return {
            "configured": self.is_configured(),
            "connected": self.connected,
            "app_id": self.state.get("app_id", ""),
            "sandbox": bool(self.state.get("sandbox", False)),
            "bot_username": self.bot_username,
            "allowed_openids": self.state.get("allowed_openids", []),
            "last_error": self.last_error,
            "logs": self.logs[-15:],
        }

    async def start_if_configured(self) -> None:
        if self.is_configured():
            self._log("检测到已保存的 QQ Bot 配置，启动 WebSocket 网关监听")
            self._ensure_ws_task()

    def _ensure_ws_task(self) -> None:
        if self._ws_task and not self._ws_task.done():
            return
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop_all(self) -> None:
        self.connected = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    async def configure_and_connect(
        self, app_id: str, app_secret: str, sandbox: bool = False, allowed_openids: Optional[List[str]] = None
    ) -> dict:
        await self.stop_all()
        self.state["app_id"] = app_id.strip()
        self.state["app_secret"] = app_secret.strip()
        self.state["sandbox"] = bool(sandbox)
        if allowed_openids is not None:
            self.state["allowed_openids"] = [x.strip() for x in allowed_openids if x.strip()]
        self._save_state()
        self._access_token = ""
        self._token_expires_at = 0.0
        self.last_error = ""

        # Validate credentials immediately before launching background task
        try:
            await self._get_access_token(force=True)
        except Exception as exc:
            self.last_error = str(exc)
            self._log(f"QQ 凭证校验失败: {exc}")
            return self.status_dict()

        self._ensure_ws_task()
        return self.status_dict()

    async def disconnect(self) -> dict:
        await self.stop_all()
        self.state = self._empty_state()
        self._save_state()
        self.bot_username = ""
        self.last_error = ""
        self._log("已断开 QQ Bot 并清除凭证")
        return self.status_dict()

    async def _get_access_token(self, force: bool = False) -> str:
        now = time.time()
        if not force and self._access_token and now < (self._token_expires_at - 60):
            return self._access_token
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                QQ_TOKEN_URL,
                json={
                    "appId": self.state["app_id"],
                    "clientSecret": self.state["app_secret"],
                },
            )
            if not (200 <= r.status_code < 300):
                raise RuntimeError(f"获取 AccessToken 失败 HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            token = data.get("access_token")
            if not token:
                raise RuntimeError(f"获取 AccessToken 失败: {data}")
            expires_in = int(data.get("expires_in") or 7200)
            self._access_token = token
            self._token_expires_at = now + expires_in
            return token

    async def _qq_headers(self) -> Dict[str, str]:
        token = await self._get_access_token()
        return {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.state["app_id"],
            "Content-Type": "application/json",
        }

    async def _get_gateway_url(self) -> str:
        headers = await self._qq_headers()
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{self._api_base()}/gateway", headers=headers)
            if not (200 <= r.status_code < 300):
                raise RuntimeError(f"获取 Gateway 失败 HTTP {r.status_code}: {r.text[:200]}")
            url = r.json().get("url")
            if not url:
                raise RuntimeError("Gateway 响应缺少 url")
            return url

    async def _send_reply(self, target_kind: str, target_id: str, msg_id: str, text: str, seq_start: int = 1) -> None:
        if not target_id:
            return
        ep = (
            f"{self._api_base()}/v2/users/{target_id}/messages"
            if target_kind == "c2c"
            else f"{self._api_base()}/v2/groups/{target_id}/messages"
        )
        chunks = [text[i : i + 1800] for i in range(0, len(text), 1800)] or [""]
        async with httpx.AsyncClient(timeout=15.0) as client:
            headers = await self._qq_headers()
            for idx, chunk in enumerate(chunks):
                payload: Dict[str, object] = {
                    "content": chunk,
                    "msg_type": 0,
                    "msg_seq": seq_start + idx,
                }
                if msg_id:
                    payload["msg_id"] = msg_id
                r = await client.post(ep, json=payload, headers=headers)
                if not (200 <= r.status_code < 300):
                    self._log(f"发送 QQ 回复失败 HTTP {r.status_code}: {r.text[:200]}")

    async def _ws_loop(self) -> None:
        failures = 0
        while self.is_configured():
            hb_task: Optional[asyncio.Task] = None
            try:
                ws_url = await self._get_gateway_url()
                token = await self._get_access_token()
                async with websockets.connect(ws_url, ping_interval=None) as ws:
                    hello_raw = await ws.recv()
                    hello = json.loads(hello_raw)
                    hb_ms = int((hello.get("d") or {}).get("heartbeat_interval") or 41250)
                    last_seq = None

                    await ws.send(
                        json.dumps(
                            {
                                "op": 2,
                                "d": {
                                    "token": f"QQBot {token}",
                                    "intents": QQ_INTENTS,
                                    "shard": [0, 1],
                                },
                            }
                        )
                    )

                    async def heartbeat():
                        while True:
                            await asyncio.sleep(max(5.0, hb_ms / 1000.0))
                            await ws.send(json.dumps({"op": 1, "d": last_seq}))

                    hb_task = asyncio.create_task(heartbeat())

                    async for raw in ws:
                        pkt = json.loads(raw)
                        if pkt.get("s") is not None:
                            last_seq = pkt["s"]
                        op = pkt.get("op")
                        if op == 0:
                            ev_type = pkt.get("t")
                            ev_data = pkt.get("d") or {}
                            if ev_type == "READY":
                                self.connected = True
                                failures = 0
                                self.last_error = ""
                                self.bot_username = (ev_data.get("user") or {}).get("username", "QQBot")
                                self._log(f"QQ WebSocket 网关已连接 ({self.bot_username})")
                            elif ev_type == "C2C_MESSAGE_CREATE":
                                asyncio.create_task(self._handle_c2c(ev_data))
                            elif ev_type == "GROUP_AT_MESSAGE_CREATE":
                                asyncio.create_task(self._handle_group(ev_data))
                        elif op == 7:
                            self._log("QQ 网关要求重连 (Op 7)")
                            break
                        elif op == 9:
                            self._log("QQ 会话无效 (Op 9)，重新鉴权")
                            self._access_token = ""
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                failures += 1
                self.last_error = str(exc)
                self._log(f"QQ 连接异常 ({failures}): {exc}")
                await asyncio.sleep(min(30, 3 * failures))
            finally:
                self.connected = False
                if hb_task and not hb_task.done():
                    hb_task.cancel()

    def _is_allowed(self, openid: str) -> bool:
        allowed = self.state.get("allowed_openids") or []
        if not allowed:
            self.state["allowed_openids"] = [openid]
            self._save_state()
            self._log(f"已自动绑定首位私聊用户 OpenID: {openid}")
            return True
        return openid in allowed

    async def _handle_c2c(self, d: dict) -> None:
        openid = str((d.get("author") or {}).get("user_openid") or "")
        msg_id = str(d.get("id") or "")
        text = str(d.get("content") or "").strip()
        if not openid or not text:
            return
        if not self._is_allowed(openid):
            self._log(f"已拦截未授权 QQ 用户: {openid}")
            return

        lock = self._user_locks.setdefault(openid, asyncio.Lock())
        async with lock:
            self._log(f"收到 QQ 私聊: {text[:40]}")
            sess = self.state["sessions"].setdefault(
                openid, {"conversation_id": "", "cwd": self.default_cwd, "model": ""}
            )
            if text.startswith("/"):
                reply = await self.helper.handle_command(text, sess)
                self._save_state()
                await self._send_reply("c2c", openid, msg_id, reply, seq_start=1)
                return

            await self._send_reply("c2c", openid, msg_id, "⏳ 正在思考并执行中...", seq_start=1)
            reply = await self.helper.run_agy(text, sess)
            self._save_state()
            await self._send_reply("c2c", openid, msg_id, reply or "（无输出）", seq_start=2)

    async def _handle_group(self, d: dict) -> None:
        group_openid = str(d.get("group_openid") or "")
        member_openid = str((d.get("author") or {}).get("member_openid") or "")
        msg_id = str(d.get("id") or "")
        text = str(d.get("content") or "").strip()
        if not group_openid or not member_openid or not text:
            return
        allowed = self.state.get("allowed_openids") or []
        if not allowed or member_openid not in allowed:
            self._log(f"已拦截未授权群成员: {member_openid}")
            return

        lock = self._user_locks.setdefault(group_openid, asyncio.Lock())
        async with lock:
            self._log(f"收到 QQ 群 @消息: {text[:40]}")
            sess = self.state["sessions"].setdefault(
                f"group:{group_openid}", {"conversation_id": "", "cwd": self.default_cwd, "model": ""}
            )
            if text.startswith("/"):
                reply = await self.helper.handle_command(text, sess)
                self._save_state()
                await self._send_reply("group", group_openid, msg_id, reply, seq_start=1)
                return

            await self._send_reply("group", group_openid, msg_id, "⏳ 正在思考并执行中...", seq_start=1)
            reply = await self.helper.run_agy(text, sess)
            self._save_state()
            await self._send_reply("group", group_openid, msg_id, reply or "（无输出）", seq_start=2)

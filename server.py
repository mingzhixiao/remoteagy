import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from wechat_ilink import WeChatILinkManager
from qq_bot import QQBotManager
from token_stats import get_token_stats, record_token_usage, reset_token_stats


def load_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


BASE_DIR = Path(__file__).resolve().parent
load_env(BASE_DIR / ".env")

AUTH_USER = os.environ.get("REMOTEAGY_USER", "agy")
AUTH_PASS = os.environ.get("REMOTEAGY_PASS", "changeme")
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", str(BASE_DIR))
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", "/wzw"))
AGY_BIN = os.environ.get("AGY_BIN") or shutil.which("agy") or str(Path.home() / ".local" / "bin" / "agy")
AGY_DATA_DIR = Path(os.environ.get("AGY_DATA_DIR") or str(Path.home() / ".gemini" / "antigravity-cli"))
SUMMARIES_DB = AGY_DATA_DIR / "conversation_summaries.db"
BRAIN_DIR = AGY_DATA_DIR / "brain"
SESSIONS_FILE = BASE_DIR / ".sessions.json"
CUSTOM_PROJECTS_FILE = BASE_DIR / ".projects.json"
WECHAT_STATE_FILE = BASE_DIR / ".wechat_state.json"
QQ_STATE_FILE = BASE_DIR / ".qq_state.json"

active_procs: Dict[str, asyncio.subprocess.Process] = {}
models_cache: List[Dict[str, str]] = []
quota_cache: Dict[str, object] = {"ts": 0.0, "data": None}
wechat_mgr: Optional[WeChatILinkManager] = None
qq_mgr: Optional[QQBotManager] = None


async def get_models_list() -> List[dict]:
    global models_cache
    if not models_cache:
        try:
            proc = await asyncio.create_subprocess_exec(
                AGY_BIN,
                "models",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            parsed = []
            for line in out.decode("utf-8", errors="replace").splitlines():
                if "\t" in line:
                    mid, label = line.split("\t", 1)
                    parsed.append({"id": mid.strip(), "label": label.strip()})
            if parsed:
                models_cache = parsed
        except Exception:
            pass
    return models_cache


async def fetch_quota_data(refresh: bool = False) -> dict:
    now = time.time()
    if not refresh and quota_cache["data"] is not None and (now - float(quota_cache["ts"])) < 60:
        return quota_cache["data"]  # type: ignore[return-value]
    if not Path(AGY_BIN).exists():
        return {"ok": False, "error": f"未找到 agy 可执行文件: {AGY_BIN}，请在 .env 中配置 AGY_BIN"}
    try:
        proc = await asyncio.create_subprocess_exec(
            AGY_BIN,
            "--output-format",
            "json",
            "-p=/usage",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PATH": f"{Path.home() / '.local' / 'bin'}:{os.environ.get('PATH', '')}"},
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        raw = out.decode("utf-8", errors="replace").strip()
        err_text = err.decode("utf-8", errors="replace").strip()
        if not raw:
            msg = err_text or f"agy 返回为空 (exit={proc.returncode})，请确认已在服务器终端完成 agy 登录授权"
            return {"ok": False, "error": msg[:300]}
        parsed = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            for line in reversed(raw.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        parsed = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        pass
        if not isinstance(parsed, dict):
            return {"ok": False, "error": (err_text or raw)[:300]}
        data = parsed.get("command", {}).get("data") or {}
        result = {"ok": True, "data": data, "fetched_at": int(now)}
        quota_cache["ts"] = now
        quota_cache["data"] = result
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global wechat_mgr, qq_mgr
    wechat_mgr = WeChatILinkManager(
        state_file=WECHAT_STATE_FILE,
        agy_bin=AGY_BIN,
        default_cwd=DEFAULT_CWD,
        projects_root=PROJECTS_ROOT,
        list_projects_fn=list_all_projects,
        get_models_fn=get_models_list,
        get_quota_fn=fetch_quota_data,
    )
    qq_mgr = QQBotManager(
        state_file=QQ_STATE_FILE,
        agy_bin=AGY_BIN,
        default_cwd=DEFAULT_CWD,
        projects_root=PROJECTS_ROOT,
        list_projects_fn=list_all_projects,
        get_models_fn=get_models_list,
        get_quota_fn=fetch_quota_data,
    )
    await wechat_mgr.start_if_configured()
    await qq_mgr.start_if_configured()
    try:
        yield
    finally:
        await wechat_mgr.stop_all()
        await qq_mgr.stop_all()


app = FastAPI(title="RemoteAgy Web", lifespan=lifespan)
security = HTTPBasic()


def verify_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = secrets.compare_digest(credentials.username.encode(), AUTH_USER.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), AUTH_PASS.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def load_sessions() -> List[dict]:
    items: List[dict] = []
    seen_ids = set()

    if SUMMARIES_DB.exists():
        try:
            conn = sqlite3.connect(f"file:{SUMMARIES_DB}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT conversation_id, title, preview, step_count, last_modified_time, workspace_uris
                FROM conversation_summaries
                WHERE parent_conversation_id = '' AND killed = 0
                ORDER BY last_modified_time DESC
                LIMIT 50
                """
            ).fetchall()
            conn.close()
            for r in rows:
                cid = r["conversation_id"]
                seen_ids.add(cid)
                cwd = DEFAULT_CWD
                try:
                    uris = json.loads(r["workspace_uris"] or "[]")
                    file_uris = [u[7:] for u in uris if isinstance(u, str) and u.startswith("file://")]
                    if file_uris:
                        cwd = file_uris[-1]
                except Exception:
                    pass
                items.append(
                    {
                        "id": cid,
                        "title": (r["title"] or r["preview"] or cid)[:60],
                        "cwd": cwd,
                        "model": "",
                        "step_count": r["step_count"],
                        "updated_at": r["last_modified_time"],
                    }
                )
        except Exception:
            pass

    if SESSIONS_FILE.exists():
        try:
            for s in json.loads(SESSIONS_FILE.read_text(encoding="utf-8")):
                cid = s.get("id")
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    items.append(s)
        except Exception:
            pass

    return items[:50]


def extract_changed_files_from_tools(tool_calls: List[dict]) -> List[dict]:
    edit_tools = {
        "write_file",
        "write_to_file",
        "edit_file",
        "replace_file_content",
        "multi_replace_file_content",
        "sed_file",
    }
    files: Dict[str, dict] = {}
    for tc in tool_calls:
        name = tc.get("name", "")
        if name not in edit_tools:
            continue
        args = tc.get("args") or tc.get("parameters") or {}
        path = args.get("Path") or args.get("TargetFile") or args.get("path") or ""
        if isinstance(path, str):
            path = path.strip('"')
        if not path:
            continue
        added = 0
        removed = 0
        diff_lines: List[str] = []
        if "Replacement" in args or "ReplacementContent" in args:
            tgt = str(args.get("Target") or args.get("TargetContent") or "")
            rep = str(args.get("Replacement") or args.get("ReplacementContent") or "")
            removed = len(tgt.splitlines()) if tgt else 0
            added = len(rep.splitlines()) if rep else 0
            for l in tgt.splitlines():
                diff_lines.append(f"-{l}")
            for l in rep.splitlines():
                diff_lines.append(f"+{l}")
        elif "Content" in args or "CodeContent" in args:
            cnt = str(args.get("Content") or args.get("CodeContent") or "")
            added = len(cnt.splitlines())
            for l in cnt.splitlines()[:120]:
                diff_lines.append(f"+{l}")
        entry = files.setdefault(path, {"path": path, "added": 0, "removed": 0, "diff": []})
        entry["added"] += added
        entry["removed"] += removed
        entry["diff"].extend(diff_lines)
    return list(files.values())


def load_conversation_messages(conv_id: str) -> List[dict]:
    if not re.match(r"^[a-zA-Z0-9_-]+$", conv_id):
        raise HTTPException(status_code=400, detail="Invalid conversation_id")

    logs_dir = BRAIN_DIR / conv_id / ".system_generated" / "logs"
    compact_path = logs_dir / "transcript.jsonl"
    full_path = logs_dir / "transcript_full.jsonl"
    if not compact_path.exists() and not full_path.exists():
        return []

    full_by_step: Dict[int, dict] = {}
    target_path = compact_path if compact_path.exists() else full_path
    messages: List[dict] = []
    pending_steps: List[dict] = []
    pending_tools: List[dict] = []
    pending_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "thinking_tokens": 0,
        "total_tokens": 0,
    }

    def get_full_entry(step_idx: int) -> Optional[dict]:
        if not full_by_step and full_path.exists():
            for line in full_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    full_by_step[d.get("step_index", -1)] = d
                except Exception:
                    pass
        return full_by_step.get(step_idx)

    for line in target_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue

        if "content" in (d.get("truncated_fields") or []):
            full_d = get_full_entry(d.get("step_index", -1))
            if full_d:
                d = full_d

        msg_type = d.get("type")
        if msg_type == "USER_INPUT":
            pending_steps = []
            pending_tools = []
            pending_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "thinking_tokens": 0,
                "total_tokens": 0,
            }
            raw = d.get("content", "")
            m = re.search(r"<USER_REQUEST>\s*([\s\S]*?)\s*</USER_REQUEST>", raw)
            text = m.group(1).strip() if m else raw.strip()
            if text:
                messages.append({"role": "user", "text": text})
        elif msg_type == "PLANNER_RESPONSE":
            pending_usage["input_tokens"] += int(d.get("input_tokens") or 0)
            pending_usage["output_tokens"] += int(d.get("output_tokens") or 0)
            pending_usage["cache_read_tokens"] += int(d.get("cache_read_tokens") or 0)
            pending_usage["thinking_tokens"] += int(d.get("thinking_tokens") or 0)
            thinking = (d.get("thinking") or "").strip()
            if thinking:
                pending_steps.append({"kind": "thinking", "text": thinking})
            tcs = d.get("tool_calls") or []
            for tc in tcs:
                pending_tools.append(tc)
                pending_steps.append(
                    {
                        "kind": "tool",
                        "name": tc.get("name", "tool"),
                        "args": tc.get("args", {}),
                    }
                )
            content = (d.get("content") or "").strip()
            if content:
                pending_usage["total_tokens"] = (
                    pending_usage["input_tokens"] + pending_usage["output_tokens"]
                )
                msg_item = {
                    "role": "assistant",
                    "text": content,
                    "model": d.get("model", ""),
                    "steps": pending_steps,
                    "changed_files": extract_changed_files_from_tools(pending_tools),
                }
                if pending_usage["total_tokens"] > 0 or pending_usage["cache_read_tokens"] > 0:
                    msg_item["usage"] = dict(pending_usage)
                messages.append(msg_item)
                pending_steps = []
                pending_tools = []
                pending_usage = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "thinking_tokens": 0,
                    "total_tokens": 0,
                }

    return messages


def save_sessions(sessions: List[dict]) -> None:
    SESSIONS_FILE.write_text(
        json.dumps(sessions[:50], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def upsert_session(conv_id: str, title: str, cwd: str, model: str) -> None:
    sessions = load_sessions()
    now = int(time.time())
    for s in sessions:
        if s.get("id") == conv_id:
            s["updated_at"] = now
            if cwd:
                s["cwd"] = cwd
            if model:
                s["model"] = model
            sessions.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
            save_sessions(sessions)
            return
    sessions.insert(
        0,
        {
            "id": conv_id,
            "title": title[:60],
            "cwd": cwd,
            "model": model,
            "updated_at": now,
        },
    )
    save_sessions(sessions)


class ImageItem(BaseModel):
    name: str
    dataUrl: str


class ChatRequest(BaseModel):
    prompt: str = ""
    conversation_id: Optional[str] = None
    model: Optional[str] = None
    cwd: Optional[str] = None
    images: List[ImageItem] = []
    request_id: Optional[str] = None


class StopRequest(BaseModel):
    request_id: str


class CreateProjectRequest(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None


def list_all_projects() -> List[str]:
    projects = [DEFAULT_CWD]
    if PROJECTS_ROOT.exists() and PROJECTS_ROOT.is_dir():
        for p in sorted(PROJECTS_ROOT.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                sp = str(p)
                if sp not in projects:
                    projects.append(sp)
    if CUSTOM_PROJECTS_FILE.exists():
        try:
            for sp in json.loads(CUSTOM_PROJECTS_FILE.read_text(encoding="utf-8")):
                if isinstance(sp, str) and Path(sp).is_dir() and sp not in projects:
                    projects.append(sp)
        except Exception:
            pass
    return projects


@app.get("/", response_class=HTMLResponse)
async def index(_: str = Depends(verify_auth)):
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/info")
async def get_info(_: str = Depends(verify_auth)):
    return {
        "hostname": socket.gethostname(),
        "default_cwd": DEFAULT_CWD,
        "projects": list_all_projects(),
        "models": await get_models_list(),
        "sessions": load_sessions(),
    }


@app.post("/api/projects")
async def create_project(req: CreateProjectRequest, _: str = Depends(verify_auth)):
    raw_name = (req.name or "").strip()
    raw_path = (req.path or "").strip()

    if raw_path:
        target = Path(raw_path).expanduser().resolve()
    elif raw_name:
        if not re.match(r"^[a-zA-Z0-9._-]+$", raw_name) or raw_name in {".", ".."}:
            raise HTTPException(status_code=400, detail="项目名仅支持字母、数字、._-")
        target = (PROJECTS_ROOT / raw_name).resolve()
    else:
        raise HTTPException(status_code=400, detail="请填写项目名称或目录路径")

    target.mkdir(parents=True, exist_ok=True)
    sp = str(target)

    custom_list: List[str] = []
    if CUSTOM_PROJECTS_FILE.exists():
        try:
            custom_list = json.loads(CUSTOM_PROJECTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            custom_list = []
    if sp not in custom_list:
        custom_list.append(sp)
        CUSTOM_PROJECTS_FILE.write_text(
            json.dumps(custom_list, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return {"project": sp, "projects": list_all_projects()}


@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str, _: str = Depends(verify_auth)):
    return {"conversation_id": conversation_id, "messages": load_conversation_messages(conversation_id)}


@app.get("/api/git-diff")
async def get_git_diff(cwd: Optional[str] = None, _: str = Depends(verify_auth)):
    target = Path(cwd or DEFAULT_CWD)
    if not target.exists() or not target.is_dir():
        return {"is_git": False, "files": []}
    try:
        chk = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=3
        )
        if chk.returncode != 0:
            return {"is_git": False, "files": []}
        numstat = subprocess.run(
            ["git", "-C", str(target), "diff", "--numstat"],
            capture_output=True, text=True, timeout=5
        )
        files = []
        for line in numstat.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                a = int(parts[0]) if parts[0].isdigit() else 0
                r = int(parts[1]) if parts[1].isdigit() else 0
                fpath = parts[2]
                df = subprocess.run(
                    ["git", "-C", str(target), "diff", "--", fpath],
                    capture_output=True, text=True, timeout=5
                )
                files.append({"path": fpath, "added": a, "removed": r, "diff": df.stdout.splitlines()[:300]})
        return {"is_git": True, "files": files}
    except Exception:
        return {"is_git": False, "files": []}


@app.get("/api/quota")
async def get_quota(refresh: bool = False, _: str = Depends(verify_auth)):
    return await fetch_quota_data(refresh=refresh)


@app.get("/api/tokens")
async def get_tokens(_: str = Depends(verify_auth)):
    return get_token_stats()


@app.post("/api/tokens/reset")
async def reset_tokens(_: str = Depends(verify_auth)):
    return reset_token_stats()


@app.post("/api/stop")
async def stop_run(req: StopRequest, _: str = Depends(verify_auth)):
    proc = active_procs.get(req.request_id)
    if proc and proc.returncode is None:
        proc.terminate()
        return {"stopped": True}
    return {"stopped": False}


def save_uploaded_images(cwd: Path, images: List[ImageItem]) -> List[str]:
    saved_paths: List[str] = []
    if not images:
        return saved_paths
    upload_dir = cwd / ".remoteagy_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    for idx, img in enumerate(images):
        match = re.match(r"^data:image/([a-zA-Z0-9+.-]+);base64,(.+)$", img.dataUrl, re.S)
        if not match:
            continue
        ext = match.group(1).lower().replace("jpeg", "jpg")
        if ext not in {"png", "jpg", "webp", "gif"}:
            ext = "png"
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", img.name or f"img_{idx}.{ext}")
        if not safe_name.lower().endswith(f".{ext}"):
            safe_name = f"{safe_name}.{ext}"
        dest = upload_dir / f"{ts}_{idx}_{safe_name}"
        dest.write_bytes(base64.b64decode(match.group(2)))
        saved_paths.append(f".remoteagy_uploads/{dest.name}")
    return saved_paths


@app.post("/api/chat")
async def chat_stream(req: ChatRequest, request: Request, _: str = Depends(verify_auth)):
    cwd = Path(req.cwd or DEFAULT_CWD)
    if not cwd.exists() or not cwd.is_dir():
        cwd = Path(DEFAULT_CWD)

    saved_images = save_uploaded_images(cwd, req.images)
    prompt_parts = []
    if saved_images:
        refs = " ".join(f"@{p}" for p in saved_images)
        prompt_parts.append(f"请查看上传的图片文件: {refs}")
    if req.prompt.strip():
        prompt_parts.append(req.prompt.strip())
    full_prompt = "\n\n".join(prompt_parts).strip()

    if not full_prompt:
        raise HTTPException(status_code=400, detail="Prompt or image is required")

    cmd = [
        AGY_BIN,
        "--output-format",
        "stream-json",
        "--dangerously-skip-permissions",
    ]
    if req.conversation_id:
        cmd.extend(["--conversation", req.conversation_id])
    if req.model:
        cmd.extend(["--model", req.model])
    cmd.append(f"-p={full_prompt}")

    req_id = req.request_id or str(uuid.uuid4())

    async def event_generator():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PATH": f"/root/.local/bin:{os.environ.get('PATH', '')}"},
        )
        active_procs[req_id] = proc
        conv_id = req.conversation_id
        title = (req.prompt.strip() or "图片对话")[:60]
        last_usage = None
        recorded_usage = False

        yield f"data: {json.dumps({'type': 'meta', 'request_id': req_id, 'images': saved_images})}\n\n"

        try:
            assert proc.stdout is not None
            while True:
                if await request.is_disconnected():
                    if proc.returncode is None:
                        proc.terminate()
                    break
                line = await proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    yield f"data: {json.dumps({'type': 'log', 'text': raw})}\n\n"
                    continue

                ev_name = ev.get("event")
                if ev_name == "init":
                    conv_id = ev.get("conversation_id") or conv_id
                    if conv_id:
                        upsert_session(conv_id, title, str(cwd), req.model or "")
                elif ev_name == "step_update":
                    su = ev.get("step_update") or {}
                    if isinstance(su.get("usage"), dict):
                        last_usage = su["usage"]
                elif ev_name == "result":
                    r = ev.get("result") or {}
                    r_conv = r.get("conversation_id") or conv_id
                    if r_conv:
                        conv_id = r_conv
                        upsert_session(r_conv, title, str(cwd), req.model or "")
                    usage = r.get("usage") or last_usage
                    if usage and not recorded_usage:
                        record_token_usage(
                            usage=usage,
                            source="web",
                            conversation_id=conv_id or "",
                            model=req.model or "",
                            cwd=str(cwd),
                            duration_seconds=float(r.get("duration_seconds") or 0.0),
                        )
                        recorded_usage = True

                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

            if last_usage and not recorded_usage:
                record_token_usage(
                    usage=last_usage,
                    source="web",
                    conversation_id=conv_id or "",
                    model=req.model or "",
                    cwd=str(cwd),
                )
                recorded_usage = True

            stderr_bytes = b""
            if proc.stderr is not None:
                stderr_bytes = await proc.stderr.read()
            rc = await proc.wait()
            err_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            yield f"data: {json.dumps({'type': 'done', 'returncode': rc, 'stderr': err_text}, ensure_ascii=False)}\n\n"
        finally:
            active_procs.pop(req_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


class VerifyCodeRequest(BaseModel):
    code: str


@app.get("/api/wechat/status")
async def wechat_status(_: str = Depends(verify_auth)):
    assert wechat_mgr is not None
    return wechat_mgr.status_dict()


@app.post("/api/wechat/qr")
async def wechat_qr(_: str = Depends(verify_auth)):
    assert wechat_mgr is not None
    return await wechat_mgr.start_qr_login()


@app.post("/api/wechat/verify")
async def wechat_verify(req: VerifyCodeRequest, _: str = Depends(verify_auth)):
    assert wechat_mgr is not None
    await wechat_mgr.submit_verify_code(req.code)
    return {"ok": True}


@app.post("/api/wechat/disconnect")
async def wechat_disconnect(_: str = Depends(verify_auth)):
    assert wechat_mgr is not None
    await wechat_mgr.disconnect()
    return wechat_mgr.status_dict()


class QQConnectRequest(BaseModel):
    app_id: str
    app_secret: str
    sandbox: bool = False
    allowed_openids: Optional[List[str]] = None


@app.get("/api/qq/status")
async def qq_status(_: str = Depends(verify_auth)):
    assert qq_mgr is not None
    return qq_mgr.status_dict()


@app.post("/api/qq/connect")
async def qq_connect(req: QQConnectRequest, _: str = Depends(verify_auth)):
    assert qq_mgr is not None
    return await qq_mgr.configure_and_connect(
        req.app_id, req.app_secret, req.sandbox, req.allowed_openids
    )


@app.post("/api/qq/disconnect")
async def qq_disconnect(_: str = Depends(verify_auth)):
    assert qq_mgr is not None
    return await qq_mgr.disconnect()

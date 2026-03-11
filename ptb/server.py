#!/usr/bin/env python3
"""
Prompt to Build — API Server
WebSocket-first streaming server that runs Claude Code and pushes
the result to PolyAI Agent Studio via the las CLI.
"""

import asyncio
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Security, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ptb")

# ---------------------------------------------------------------------------
# Config (all overridable via env vars / .env)
# ---------------------------------------------------------------------------

def _load_api_keys() -> dict[str, str]:
    """Load API keys from environment. Add more users by adding API_KEY_USER<N>."""
    keys = {}
    for i in range(1, 8):  # supports up to 7 users
        val = os.environ.get(f"API_KEY_USER{i}")
        if val:
            keys[val] = f"user{i}"
    if not keys:
        # dev fallback — never use this in production
        keys["ptb-dev-changeme"] = "user1"
        log.warning("No API keys configured — using insecure dev key. Set API_KEY_USER1 in .env")
    return keys

API_KEYS   = _load_api_keys()
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
LAS_BIN    = os.environ.get("LAS_BIN",    str(Path.home() / "local_agent_studio/.venv/bin/las"))

def _load_anthropic_key() -> str:
    """Load ANTHROPIC_API_KEY from env or fall back to reading .zshrc."""
    import re as _re
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        log.info("ANTHROPIC_API_KEY from environment")
        return key
    try:
        zshrc = (Path.home() / ".zshrc").read_text()
        for line in zshrc.splitlines():
            m = _re.match(r'^(?:export\s+)?ANTHROPIC_API_KEY=["\']?([^"\']+?)["\']?\s*$', line.strip())
            if m:
                key = m.group(1).strip()
                log.info(f"ANTHROPIC_API_KEY loaded from .zshrc ({key[:20]}...)")
                return key
    except Exception as e:
        log.warning(f"Could not read .zshrc: {e}")
    log.warning("ANTHROPIC_API_KEY not found — Claude Code will fail to authenticate")
    return ""

ANTHROPIC_API_KEY = _load_anthropic_key()
JOBS_DIR   = Path(os.environ.get("JOBS_DIR", "/tmp/ptb_jobs"))
PORT       = int(os.environ.get("PORT", 8788))
JOBS_DIR.mkdir(parents=True, exist_ok=True)

JOBS: dict[str, dict] = {}
REAUTH_EVENTS: dict[str, asyncio.Event] = {}  # job_id -> event, set when Claude needs reauth
REAUTH_DONE: dict[str, asyncio.Event] = {}    # job_id -> event, set when token is fresh

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class HistoryTurn(BaseModel):
    user: str
    assistant: str = ""

class BuildRequest(BaseModel):
    account_id: str
    project_id: str
    region: str
    prompt: str
    history: list[HistoryTurn] = []     # prior conversation turns
    webhook_url: Optional[str] = None
    polyctx_token: Optional[str] = None  # JWT from Auth0 — injected into las env
    project_path: Optional[str] = None   # pre-initialised project dir from v2 setup

class JobStatus(BaseModel):
    job_id: str
    status: str  # queued | running | success | failed
    user: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    branch: Optional[str] = None
    error: Optional[str] = None

def ws_msg(kind: str, **kwargs) -> str:
    return json.dumps({"type": kind, "ts": datetime.now().strftime("%H:%M:%S"), **kwargs})

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Prompt to Build", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()

def authenticate(token: str) -> str:
    user = API_KEYS.get(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user

def get_user(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    return authenticate(credentials.credentials)

# ---------------------------------------------------------------------------
# Claude Code prompt builder
# ---------------------------------------------------------------------------

# Paths to LAS docs — loaded at runtime so the prompt stays up to date
LAS_CLAUDE_MD       = Path.home() / "local_agent_studio/.claude/CLAUDE.md"
LAS_DOCS_DIR        = Path.home() / "local_agent_studio/src/poly/docs"
LAS_REFERENCE_MD    = Path.home() / "local_agent_studio/src/poly/docs/reference_projects.md"
CLAUDE_TEMPLATE_MD  = Path.home() / "las-studio-v2/CLAUDE_TEMPLATE.md"

def _load_las_docs_system_prompt() -> str:
    """Load CLAUDE_TEMPLATE.md + docs/*.md + reference_projects.md as system prompt."""
    sections = []

    # 1. Build rules — most important, load first so they take priority
    if CLAUDE_TEMPLATE_MD.exists():
        sections.append("# Build Rules (CLAUDE_TEMPLATE.md)\n")
        sections.append(CLAUDE_TEMPLATE_MD.read_text())
    else:
        log.warning(f"CLAUDE_TEMPLATE.md not found at {CLAUDE_TEMPLATE_MD}")

    # 2. LAS format docs
    if LAS_DOCS_DIR.exists():
        sections.append("\n# PolyAI LAS Documentation\n")
        for doc in sorted(LAS_DOCS_DIR.glob("*.md")):
            if doc.name == "reference_projects.md":
                continue  # loaded separately below
            sections.append(f"\n## {doc.name}\n")
            sections.append(doc.read_text())

    # 3. Reference projects
    if LAS_REFERENCE_MD.exists():
        sections.append("\n# Reference Projects (agent-deployments)\n")
        sections.append(LAS_REFERENCE_MD.read_text())
    else:
        log.warning(f"reference_projects.md not found at {LAS_REFERENCE_MD}")

    result = "\n".join(sections)
    log.info(f"System prompt loaded: {len(result)} chars")
    return result


def build_claude_prompt(request: BuildRequest) -> str:
    """User-facing task prompt — includes conversation history for continuity."""
    history_block = ""
    if request.history:
        turns = []
        for t in request.history:
            turns.append(f"User: {t.user}")
            if t.assistant:
                # Trim assistant output to avoid bloating context
                summary = t.assistant[:800] + ("…" if len(t.assistant) > 800 else "")
                turns.append(f"Assistant: {summary}")
        history_block = "\n\n## Previous conversation\n" + "\n".join(turns) + "\n\nContinue from where we left off. The files you created previously are already on disk in the project directory.\n"

    return f"""Build a PolyAI Agent Studio agent in the current directory.

Account ID: {request.account_id}
Project ID: {request.project_id}
Region:     {request.region}
{history_block}
## Current instruction:
{request.prompt}

## Step 1 — Understand

Read the current project directory thoroughly before doing anything else.

- List all files and folders in the project
- Read `agent_settings/rules.txt` if it exists — this is the primary LLM prompt
- Read any existing flows, topics, functions, and entities
- Understand the user's build request: vertical, channel (voice/chat), use cases, flows needed, entities needed

If you are unsure what the customer wants, re-read the request and the existing project files until you have a clear picture.

## Step 2 — Plan

Before writing a single file, decide on your approach:

1. **Reference projects** — from `reference_projects.md`, identify the 2-4 most relevant projects by vertical, use cases, and channel. Output this block so the user can see your reasoning:

```
📚 Reference projects selected:
1. <project name> (<path>) — <one sentence why>
2. <project name> (<path>) — <one sentence why>
```

2. **Read those reference projects** — for each one, read:
   - `agent_settings/rules.txt`
   - `flows/` — list, then read 1-2 flow YAMLs
   - `topics/` — list, then read 2-3 topic YAMLs
   - `functions/` — list, read notable ones
   - `agent_settings/experimental_config.json`

3. **If no obvious reference matches** — scan through several projects in `~/agent-deployments/agents/` to find relevant patterns (a specific flow type, entity type, or handoff pattern)

4. **Outline your build** — briefly list the flows, topics, functions, and entities you will create before writing anything

## Step 3 — Build

Execute your plan:

- Follow the LAS format from your system prompt exactly
- Read existing files before overwriting — do not clobber work already done
- Invent all details not specified (agent name, mock data, FAQs, greeting, tone)
- Build a **complete** working agent — every required file must be present
- Name functions after the EVENT that triggered them (e.g. `date_provided` not `store_date`)
- Include a well-configured `agent_settings/experimental_config.json` (use baseline from your system prompt)

## Step 4 — Push

Once the build is complete:

1. Write a brief build summary — list flows, topics, key functions, any design decisions made
2. Run `las push --force --skip-validation` to create a remote branch in Agent Studio
3. Report the branch name from the push output so the user can merge it
"""

def _format_tool_label(tool_name: str, tool_input: dict) -> dict | None:
    """Returns {action, file, lang, preview} for a tool call, or None to suppress."""
    import os as _os

    def _short(path: str) -> str:
        parts = path.replace("\\", "/").split("/")
        return "/".join(parts[-3:]) if len(parts) > 3 else path

    def _lang(path: str) -> str:
        ext = _os.path.splitext(path)[-1].lstrip(".")
        return {"py": "python", "yaml": "yaml", "yml": "yaml", "txt": "text",
                "json": "json", "md": "markdown", "sh": "bash"}.get(ext, ext or "text")

    def _preview(text: str, n: int = 4) -> str:
        lines = [l for l in (text or "").splitlines() if l.strip()][:n]
        return "\n".join(lines)

    if tool_name in ("Write", "write_file", "str_replace_based_edit_tool"):
        # str_replace_based_edit_tool can be write OR edit depending on fields
        path = tool_input.get("path") or tool_input.get("file_path") or ""
        new_str = tool_input.get("new_str") or ""
        content_val = tool_input.get("content") or ""
        if not path:
            return None
        if content_val:
            return {"action": "write", "file": _short(path), "lang": _lang(path), "preview": _preview(content_val)}
        elif new_str:
            return {"action": "edit", "file": _short(path), "lang": _lang(path), "preview": _preview(new_str)}
        return {"action": "write", "file": _short(path), "lang": _lang(path), "preview": ""}

    elif tool_name in ("Edit", "str_replace_editor", "replace_in_file"):
        path = tool_input.get("path") or tool_input.get("file_path") or ""
        new_str = tool_input.get("new_str") or tool_input.get("new_content") or ""
        if not path:
            return None
        return {"action": "edit", "file": _short(path), "lang": _lang(path), "preview": _preview(new_str)}

    elif tool_name in ("Bash", "bash", "execute_bash"):
        cmd = (tool_input.get("command") or tool_input.get("cmd") or "").strip()
        if not cmd:
            return None
        return {"action": "run", "file": cmd[:120], "lang": "bash", "preview": ""}

    elif tool_name in ("Read", "read_file", "view_file"):
        path = tool_input.get("path") or tool_input.get("file_path") or ""
        if not path:
            return None
        return {"action": "read", "file": _short(path), "lang": _lang(path), "preview": ""}

    elif tool_name in ("Glob", "glob", "LS", "ls", "list_files"):
        pattern = tool_input.get("pattern") or tool_input.get("path") or ""
        return {"action": "search", "file": pattern[:80], "lang": "", "preview": ""}

    elif tool_name in ("Grep", "grep", "search_files"):
        pattern = tool_input.get("pattern") or tool_input.get("query") or ""
        return {"action": "search", "file": pattern[:80], "lang": "", "preview": ""}

    return None


async def run_build(job_id: str, request: BuildRequest, user: str, ws: Optional[WebSocket] = None):
    job = JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = datetime.now(timezone.utc).isoformat()

    # Reauth signalling: Claude Code can POST /jobs/{job_id}/reauth to trigger
    # the Auth0 device code flow, then GET /jobs/{job_id}/reauth/wait to block
    # until the user completes sign-in.
    _reauth_trigger = asyncio.Event()
    _reauth_done    = asyncio.Event()
    REAUTH_EVENTS[job_id] = _reauth_trigger
    REAUTH_DONE[job_id]   = _reauth_done

    workdir = JOBS_DIR / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = workdir / "build.log"

    events_path = workdir / "events.jsonl"

    async def emit(kind: str, **kwargs):
        msg = ws_msg(kind, **kwargs)
        data = json.loads(msg)
        # Plain text log
        with open(log_path, "a") as f:
            if "text" in data:
                f.write(data["text"])
            else:
                f.write(f"\n[{data['ts']}] [{kind.upper()}] {data.get('message', '')}\n")
        # Structured JSONL log — used for browser reconnect
        with open(events_path, "a") as f:
            f.write(msg + "\n")
        if ws:
            try:
                if ws.client_state.value < 3:
                    await ws.send_text(msg)
            except Exception:
                pass  # client disconnected — keep building

    try:
        await emit("status", message=f"Build started: {request.account_id}/{request.project_id} ({request.region})")

        # Inject polyctx JWT — mirrors exactly what v1 does:
        # writes to ~/.polyctx/token-platform-<region> (what las actually reads)
        # plus a build-specific token file as fallback.
        # TODO: API KEY — next week swap for POLYAI_API_KEY.
        env = {**os.environ, "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
               "PTB_JOB_ID": job_id, "PTB_HOST": f"http://localhost:{PORT}"}
        if request.polyctx_token:
            import json as _json
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            expires_at = (_dt.now(_tz.utc) + _td(minutes=14)).isoformat()
            token_data = _json.dumps({
                "access_token":  request.polyctx_token,
                "refresh_token": "",
                "id_token":      "",
                "token_type":    "Bearer",
                "expires_in":    900,
                "expires_at":    expires_at,
            })
            # Write to ~/.polyctx/token-platform-<region> — this is what las reads
            polyctx_dir = Path.home() / ".polyctx"
            polyctx_dir.mkdir(exist_ok=True)
            (polyctx_dir / f"token-platform-{request.region}").write_text(token_data)
            # Also write build-specific token file
            build_token_file = JOBS_DIR / job_id / "polyctx_token.json"
            build_token_file.write_text(token_data)
            env["POLYCTX_TOKEN"]      = request.polyctx_token
            env["POLYCTX_TOKEN_FILE"] = str(build_token_file)
            await emit("status", message=f"Auth token written to ~/.polyctx/token-platform-{request.region}")
        else:
            await emit("warning", message="No polyctx token — las push will likely fail.")

        # Use pre-initialised project dir if provided (v2 already ran las init+pull)
        # otherwise fall back to the job temp dir
        if request.project_path and Path(request.project_path).exists():
            workdir = Path(request.project_path)
            await emit("status", message=f"Using existing project: {workdir}")
        else:
            await emit("status", message="No project path provided — building in temp dir")

        # git init if needed — Claude Code requires a git repo
        if not (workdir / ".git").exists():
            subprocess.run(["git", "init"], cwd=workdir, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init", "--allow-empty"], cwd=workdir, capture_output=True)

        # Run Claude Code — stream stdout chunk by chunk
        await emit("status", message="Claude Code is building your agent...")
        await emit("divider", message="─" * 60)

        # Load docs as system prompt — write to temp file to avoid arg length limits
        las_system_prompt = _load_las_docs_system_prompt()
        sys_prompt_file = JOBS_DIR / job_id / "system_prompt.txt"
        sys_prompt_file.write_text(las_system_prompt)

        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN,
            "--print",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose",
            "--verbose",
            "--model", "claude-opus-4-5",
            "--max-budget-usd", "5.00",
            "--append-system-prompt", las_system_prompt,
            build_claude_prompt(request),
            cwd=str(workdir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        JOBS[job_id]["proc"] = proc  # store so cancel endpoint can kill it
        import json as _json
        line_buf = b""
        _pending_tool = None  # accumulates tool input across delta events
        while True:
            chunk = await proc.stdout.read(256)
            if not chunk:
                break
            line_buf += chunk
            while b"\n" in line_buf:
                line, line_buf = line_buf.split(b"\n", 1)
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    obj = _json.loads(raw)
                    msg_type = obj.get("type", "")

                    # Track current tool being built up across delta events
                    if msg_type == "content_block_start":
                        cb = obj.get("content_block", {})
                        if cb.get("type") == "tool_use":
                            _pending_tool = {"name": cb.get("name", ""), "input_json": ""}
                        else:
                            _pending_tool = None

                    elif msg_type == "content_block_delta":
                        delta = obj.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                await emit("token", text=text)
                        elif delta.get("type") == "input_json_delta" and _pending_tool is not None:
                            _pending_tool["input_json"] += delta.get("partial_json", "")

                    elif msg_type == "content_block_stop":
                        if _pending_tool is not None:
                            try:
                                tool_input = _json.loads(_pending_tool["input_json"]) if _pending_tool["input_json"] else {}
                            except Exception:
                                tool_input = {}
                            # Auth intercept: if Claude is trying to run auth-related commands
                            # (polyctx, poly login, pip install polyai, etc.), trigger reauth ourselves
                            if _pending_tool["name"] == "Bash":
                                cmd = tool_input.get("command", "")
                                import re as _re2
                                _bad_auth_cmds = [
                                    r"polyctx (profile|login|auth)",
                                    r"poly (login|auth)",
                                    r"pip.*install.*polyai",
                                    r"pipx.*install.*polyai",
                                    r"bazel run.*polyctx",
                                ]
                                if any(_re2.search(p, cmd, _re2.IGNORECASE) for p in _bad_auth_cmds):
                                    log.info(f"Auth intercept: Claude tried '{cmd[:80]}' — triggering reauth instead")
                                    await emit("status", message="🔐 Token expired — triggering sign-in popup...")
                                    if not _token_fresh():
                                        await _wait_for_reauth()
                            tool_info = _format_tool_label(_pending_tool["name"], tool_input)
                            if tool_info:
                                await emit("tool", **tool_info)
                            _pending_tool = None

                    elif msg_type == "tool_result":
                        # Watch bash tool results for auth errors — trigger reauth if found
                        for item in obj.get("content", []):
                            output = item.get("text", "") if isinstance(item, dict) else ""
                            if output and _is_auth_error(output) and not _token_fresh():
                                log.info(f"Auth error in tool result — triggering reauth")
                                await emit("status", message="🔐 Auth error detected — triggering sign-in popup...")
                                await _wait_for_reauth()
                                break

                    elif msg_type == "assistant":
                        for block in obj.get("message", {}).get("content", []):
                            if block.get("type") == "text" and block.get("text"):
                                await emit("token", text=block["text"])
                            elif block.get("type") == "tool_use":
                                tool_info = _format_tool_label(block.get("name",""), block.get("input",{}))
                                if tool_info:
                                    await emit("tool", **tool_info)

                except Exception:
                    pass  # skip non-JSON lines

        await proc.wait()
        await emit("divider", message="─" * 60)

        if proc.returncode not in (0, None):
            raise RuntimeError(f"Claude Code exited with code {proc.returncode}")

        # las push — use wrapper so auth errors trigger browser re-auth
        LAS_WRAPPER = str(Path.home() / ".local/bin/las")
        las_cmd = LAS_WRAPPER if Path(LAS_WRAPPER).exists() else LAS_BIN
        AUTH_ERROR_PATTERNS = [
            '"exp" claim', 'timestamp check failed', 'JWKSNoMatchingKey',
            '401', 'JWT', 'Unauthorized', 'token.*invalid', 'invalid.*token',
        ]
        LAS_STUDIO_SERVER = os.environ.get("LAS_STUDIO_SERVER", "http://localhost:8081")

        _device_code_info = {}  # filled with verify_url + user_code when device flow starts

        async def _notify_reauth():
            """Hit the browser SSE endpoint so the UI shows the re-auth modal."""
            import urllib.request as _urlreq, json as _json
            try:
                payload = _json.dumps({"region": request.region, **_device_code_info}).encode()
                req = _urlreq.Request(
                    f"{LAS_STUDIO_SERVER}/api/reauth/notify",
                    data=payload, headers={"Content-Type": "application/json"}
                )
                _urlreq.urlopen(req, timeout=3)
            except Exception as e:
                log.warning(f"Could not notify browser for reauth: {e}")

        def _is_auth_error(text: str) -> bool:
            import re
            return any(re.search(p, text, re.IGNORECASE) for p in AUTH_ERROR_PATTERNS)

        def _token_fresh() -> bool:
            """Check if the token file for this region has a non-expired token."""
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
            tfile = Path.home() / f".polyctx/token-platform-{request.region}"
            if not tfile.exists(): return False
            try:
                d = _json.loads(tfile.read_text())
                exp = d.get("expires_at", "")
                if not exp: return False
                dt = _dt.fromisoformat(exp.replace("Z", "+00:00"))
                return dt > _dt.now(_tz.utc)
            except: return False

        async def _wait_for_reauth():
            """Start Auth0 device code flow, stream URL to UI, wait for completion.
            Can be triggered externally via POST /jobs/{job_id}/reauth (from Claude Code).
            """
            _reauth_trigger.clear()   # reset so it can fire again later
            _reauth_done.clear()
            import urllib.request as _urlreq, json as _json, urllib.parse as _urlparse

            AUTH0_DOMAIN = {
                "uk-1": "login.uk-1.polyai.app",
                "us-1": "login.us-1.polyai.app",
                "eu-1": "login.eu-1.polyai.app",
            }.get(request.region, "login.uk-1.polyai.app")
            CLIENT_ID = {
                "uk-1": "uHdlq2JZZoZ3RAzYDvl0o0R2E3glxj1q",
                "us-1": "kjV5BoAXagNnK6aGiUnJQ6xJ3hbphwE2",
                "eu-1": "kjV5BoAXagNnK6aGiUnJQ6xJ3hbphwE2",
            }.get(request.region, "uHdlq2JZZoZ3RAzYDvl0o0R2E3glxj1q")
            AUDIENCE = "https://platform.polyai.app/api"

            # Step 1: Request device code
            try:
                dc_req = _urlreq.Request(
                    f"https://{AUTH0_DOMAIN}/oauth/device/code",
                    data=_urlparse.urlencode({
                        "client_id": CLIENT_ID, "audience": AUDIENCE,
                        "scope": "openid profile email offline_access"
                    }).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                dc_resp = _json.loads(_urlreq.urlopen(dc_req, timeout=10).read())
            except Exception as e:
                await emit("warning", message=f"⚡ Token expired — please reconnect via the UI. (device code error: {e})")
                await _notify_reauth()
                # Fall back to just waiting for the UI to provide a fresh token
                waited = 0
                while not _token_fresh():
                    await asyncio.sleep(5)
                    waited += 5
                    if waited >= 600:
                        raise RuntimeError("Timed out waiting for re-auth (10 min).")
                return

            verify_url = dc_resp.get("verification_uri_complete") or dc_resp.get("verification_uri")
            user_code  = dc_resp.get("user_code", "")
            device_code = dc_resp.get("device_code")
            interval   = dc_resp.get("interval", 5)
            expires_in = dc_resp.get("expires_in", 300)

            # Step 2: Push URL + code to UI via SSE
            _device_code_info["verify_url"] = verify_url
            _device_code_info["user_code"]  = user_code
            await emit("reauth", verify_url=verify_url, user_code=user_code,
                       message=f"⚡ Token expired — sign in to resume the build.")
            await _notify_reauth()

            # Step 3: Poll Auth0 for token
            token_endpoint = f"https://{AUTH0_DOMAIN}/oauth/token"
            waited = 0
            access_token = None
            while waited < expires_in:
                await asyncio.sleep(interval)
                waited += interval
                try:
                    tok_req = _urlreq.Request(
                        token_endpoint,
                        data=_urlparse.urlencode({
                            "client_id": CLIENT_ID,
                            "device_code": device_code,
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        }).encode(),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    tok_resp = _json.loads(_urlreq.urlopen(tok_req, timeout=10).read())
                    access_token = tok_resp.get("access_token")
                    if access_token:
                        break
                except Exception as ex:
                    err_body = str(ex)
                    if "authorization_pending" in err_body or "slow_down" in err_body:
                        continue
                    log.warning(f"Token poll error: {ex}")
                    continue

            if not access_token:
                raise RuntimeError("Timed out waiting for re-auth. Please restart the build.")

            # Step 4: Write token to polyctx file so las push can use it
            from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
            future_exp = (_dt2.now(_tz2.utc) + _td2(minutes=14)).isoformat()
            token_data = _json.dumps({
                "access_token": access_token, "refresh_token": "", "id_token": "",
                "token_type": "Bearer", "expires_in": 840,
                "expires_at": future_exp,
            })
            tfile = Path.home() / f".polyctx/token-platform-{request.region}"
            tfile.parent.mkdir(parents=True, exist_ok=True)
            tfile.write_text(token_data)
            env["POLYCTX_TOKEN"] = access_token
            log.info(f"Reauth complete — token written, expires {future_exp}")

            # Step 5: Also notify UI with fresh token so it updates its session
            try:
                tok_payload = _json.dumps({"token": access_token, "region": request.region}).encode()
                tok_req2 = _urlreq.Request(
                    f"{LAS_STUDIO_SERVER}/api/reauth/token",
                    data=tok_payload, headers={"Content-Type": "application/json"}
                )
                _urlreq.urlopen(tok_req2, timeout=3)
            except Exception as e:
                log.warning(f"Could not push token back to Node: {e}")

            await emit("status", message="✅ Token refreshed — resuming push...")
            _reauth_done.set()   # unblock Claude's GET /jobs/{job_id}/reauth/wait

        # Auto-fix flow folder and step file names before push
        import re as _re

        def _clean_name(s):
            return _re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")

        def _parse_name(filepath):
            for line in filepath.read_text().splitlines():
                if line.startswith("name:"):
                    return line.split("name:",1)[1].strip().strip('"').strip("'")
            return ""

        flows_dir = workdir / "flows"
        if flows_dir.exists():
            for flow_folder in sorted(flows_dir.iterdir()):
                if not flow_folder.is_dir(): continue
                cfg = flow_folder / "flow_config.yaml"
                if not cfg.exists(): continue
                try:
                    # Fix flow folder name
                    flow_name = _parse_name(cfg)
                    expected_folder = _clean_name(flow_name)
                    if flow_folder.name != expected_folder and expected_folder:
                        new_path = flows_dir / expected_folder
                        flow_folder.rename(new_path)
                        await emit("status", message=f"Auto-fixed flow folder: {flow_folder.name} → {expected_folder}")
                        log.info(f"Renamed flow folder {flow_folder.name} → {expected_folder}")
                        flow_folder = new_path
                    # Fix step file names in both steps/ and function_steps/
                    for steps_subdir in [flow_folder / "steps", flow_folder / "function_steps"]:
                        if not steps_subdir.exists(): continue
                        for step_file in sorted(steps_subdir.iterdir()):
                            if step_file.suffix != ".yaml": continue
                            step_name = _parse_name(step_file)
                            if not step_name:
                                log.warning(f"No name: field in {step_file}")
                                continue
                            expected_file = _clean_name(step_name) + ".yaml"
                            log.info(f"Step check: {step_file.name} → expected {expected_file}")
                            if step_file.name != expected_file:
                                step_file.rename(steps_subdir / expected_file)
                                await emit("status", message=f"Auto-fixed step file: {step_file.name} → {expected_file}")
                                log.info(f"Renamed step {step_file.name} → {expected_file}")
                except Exception as _e:
                    log.warning(f"Could not auto-fix names in {flow_folder.name}: {_e}")

        await emit("tool", action="run", file="las push --force --skip-validation", lang="bash", preview="")
        log.info(f"Running las push in {workdir}")

        # Pre-flight token check — do our own device code flow BEFORE calling las push
        # so utils.py never tries to invoke polyctx (which hangs on Bazel)
        if not _token_fresh():
            log.info("Token expired before push — triggering reauth flow")
            await _wait_for_reauth()

        for attempt in range(3):
            # Re-check token before each attempt and refresh env
            if not _token_fresh():
                await _wait_for_reauth()
            # Patch env with latest token before each push attempt
            tfile = Path.home() / f".polyctx/token-platform-{request.region}"
            if tfile.exists():
                import json as _json2
                fresh = _json2.loads(tfile.read_text()).get("access_token","")
                if fresh:
                    env["POLYCTX_TOKEN"] = fresh
            push_result = subprocess.run(
                [las_cmd, "push", "--force", "--skip-validation"],
                cwd=workdir, env=env, capture_output=True, text=True, timeout=300
            )
            log.info(f"las push attempt {attempt+1} stdout: {push_result.stdout[:500]}")
            log.info(f"las push attempt {attempt+1} stderr: {push_result.stderr[:500]}")
            if push_result.returncode == 0:
                break
            err = (push_result.stderr or push_result.stdout or "unknown error")[:500]
            if _is_auth_error(err):
                await _wait_for_reauth()
                continue  # retry with fresh token
            # Non-auth error — raise immediately
            if "DEPLOYMENT_NOT_FOUND" in err or "Deployment not found" in err:
                raise RuntimeError(
                    "Project not found in Agent Studio. Please create the project first at "
                    "https://studio.polyai.app, then come back and start a new build with the same Project ID."
                )
            if "No project configuration found" in err:
                raise RuntimeError(
                    f"las push failed: no project.yaml in build dir ({workdir}). "
                    "The project may not have been initialised correctly."
                )
            raise RuntimeError(f"las push failed (code {push_result.returncode}): {err}")
        else:
            raise RuntimeError("las push failed after 3 auth retry attempts.")

        branch = None
        for line in (push_result.stdout + push_result.stderr).splitlines():
            for part in line.split():
                if part.startswith("BRANCH-"):
                    branch = part.strip()
                    break

        job["status"] = "success"
        job["branch"] = branch
        job["finished_at"] = datetime.now(timezone.utc).isoformat()

        await emit("done", message="Build complete!", branch=branch)

        if request.webhook_url:
            try:
                import urllib.request as urlreq
                payload = json.dumps({"job_id": job_id, "status": "success", "branch": branch}).encode()
                req = urlreq.Request(request.webhook_url, data=payload, headers={"Content-Type": "application/json"})
                urlreq.urlopen(req, timeout=10)
            except Exception as e:
                await emit("warning", message=f"Webhook delivery failed: {e}")

    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["finished_at"] = datetime.now(timezone.utc).isoformat()
        await emit("error", message=str(e))
        log.error(f"Build {job_id} failed: {e}")

# ---------------------------------------------------------------------------
# WebSocket — primary interface
# ---------------------------------------------------------------------------

@app.websocket("/ws/build")
async def ws_build(websocket: WebSocket, token: str = Query(...)):
    """
    Connect, send a build request, receive streaming output.

    Protocol:
      1. Connect:  ws://<host>:8788/ws/build?token=<api-key>
      2. Send:     {"account_id":"...","project_id":"...","region":"...","prompt":"..."}
      3. Receive:  stream of JSON messages:
           {type: "token",   text: "..."}          raw Claude output — render inline
           {type: "status",  message: "..."}        system info — show dimmed
           {type: "done",    message, branch}       build finished
           {type: "error",   message: "..."}        build failed
           {type: "divider", message: "..."}        visual separator
           {type: "started", job_id, message}       job accepted
           {type: "warning", message: "..."}        non-fatal warning
    """
    await websocket.accept()

    try:
        user = authenticate(token)
    except HTTPException:
        await websocket.send_text(ws_msg("error", message="Invalid API key"))
        await websocket.close(code=1008)
        return

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
        request = BuildRequest(**json.loads(raw))
    except asyncio.TimeoutError:
        await websocket.send_text(ws_msg("error", message="Timed out waiting for build request"))
        await websocket.close()
        return
    except Exception as e:
        await websocket.send_text(ws_msg("error", message=f"Invalid request: {e}"))
        await websocket.close()
        return

    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    JOBS[job_id] = {
        "job_id": job_id, "status": "queued", "user": user,
        "created_at": now, "started_at": None, "finished_at": None,
        "branch": None, "error": None, "request": request.model_dump(),
    }

    await websocket.send_text(ws_msg("started", job_id=job_id, message="Build queued"))
    await run_build(job_id, request, user, ws=websocket)
    await asyncio.sleep(0.5)
    await websocket.close()

# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.post("/build", response_model=JobStatus)
async def create_build_rest(request: BuildRequest, user: str = Security(get_user)):
    """Fire-and-forget build. Poll /jobs/{id}/logs for output."""
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    JOBS[job_id] = {
        "job_id": job_id, "status": "queued", "user": user,
        "created_at": now, "started_at": None, "finished_at": None,
        "branch": None, "error": None, "request": request.model_dump(),
    }
    asyncio.create_task(run_build(job_id, request, user, ws=None))
    return JobStatus(**{k: v for k, v in JOBS[job_id].items() if k != "request"})


@app.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str, user: str = Security(get_user)):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatus(**{k: v for k, v in job.items() if k != "request"})


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, user: str = Security(get_user)):
    """Kill the running Claude Code process for this job."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    proc = job.get("proc")
    if proc and proc.returncode is None:
        try:
            proc.terminate()
            log.info(f"Cancelled job {job_id} — sent SIGTERM to claude process")
            await asyncio.sleep(0.5)
            if proc.returncode is None:
                proc.kill()  # SIGKILL if still alive
                log.info(f"Job {job_id} — sent SIGKILL")
        except Exception as e:
            log.warning(f"Error killing proc for job {job_id}: {e}")
    now = datetime.now(timezone.utc).isoformat()
    job["status"] = "failed"
    job["error"] = "Cancelled by user"
    job["finished_at"] = now
    return {"ok": True, "job_id": job_id}

@app.post("/jobs/{job_id}/reauth")
async def trigger_reauth(job_id: str):
    """Called by Claude Code (via curl) when it hits a token expiry.
    Signals the running build to start the Auth0 device code flow."""
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    ev = REAUTH_EVENTS.get(job_id)
    if ev:
        ev.set()
        return {"ok": True, "message": "Reauth triggered"}
    return {"ok": False, "message": "No reauth listener for this job"}

@app.get("/jobs/{job_id}/reauth/wait")
async def wait_for_reauth_done(job_id: str, timeout: int = 300):
    """Long-poll — Claude curls this and blocks until reauth completes or times out."""
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    done_ev = REAUTH_DONE.get(job_id)
    if not done_ev:
        raise HTTPException(status_code=409, detail="No reauth in progress")
    try:
        await asyncio.wait_for(asyncio.shield(done_ev.wait()), timeout=timeout)
        return {"ok": True, "message": "Token refreshed — resume build"}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="Reauth timed out")

@app.get("/jobs/{job_id}/logs")
async def get_logs(job_id: str, offset: int = 0, user: str = Security(get_user)):
    """Replay plain text log."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    log_path = JOBS_DIR / job_id / "build.log"
    content_str = log_path.read_text() if log_path.exists() else ""
    chunk = content_str[offset:]
    return {
        "content": chunk, "offset": offset + len(chunk),
        "done": job["status"] in ("success", "failed"),
        "status": job["status"], "branch": job.get("branch"), "error": job.get("error"),
    }

@app.get("/jobs/{job_id}/poll")
async def poll_events(job_id: str, offset: int = 0, user: str = Security(get_user)):
    """Return a batch of JSONL events since offset — for polling clients (Cloudflare-safe)."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    events_path = JOBS_DIR / job_id / "events.jsonl"
    events = []
    if events_path.exists():
        lines = events_path.read_text().splitlines()
        for line in lines[offset:]:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    return {
        "events": events,
        "offset": offset + len(events),
        "done": job["status"] in ("success", "failed"),
        "status": job["status"],
    }


@app.get("/jobs/{job_id}/events")
async def stream_events(job_id: str, offset: int = 0, user: str = Security(get_user)):
    """SSE stream of structured JSON events — supports reconnect after page refresh."""
    from fastapi.responses import StreamingResponse
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    async def generate():
        events_path = JOBS_DIR / job_id / "events.jsonl"
        sent = 0
        # Replay existing events first
        if events_path.exists():
            lines = events_path.read_text().splitlines()
            for line in lines[offset:]:
                if line.strip():
                    yield f"data: {line.strip()}\n\n"
                    sent += 1
        # Then tail until done
        ptr = offset + sent
        while job["status"] not in ("success", "failed"):
            await asyncio.sleep(0.3)
            if events_path.exists():
                lines = events_path.read_text().splitlines()
                for line in lines[ptr:]:
                    if line.strip():
                        yield f"data: {line.strip()}\n\n"
                        ptr += 1
        # Flush any final lines
        if events_path.exists():
            lines = events_path.read_text().splitlines()
            for line in lines[ptr:]:
                if line.strip():
                    yield f"data: {line.strip()}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/jobs", response_model=list[JobStatus])
async def list_jobs(user: str = Security(get_user)):
    user_jobs = [
        JobStatus(**{k: v for k, v in j.items() if k != "request"})
        for j in JOBS.values() if j["user"] == user
    ]
    return sorted(user_jobs, key=lambda j: j.created_at, reverse=True)


@app.get("/health")
async def health():
    return {"ok": True, "jobs_total": len(JOBS)}


# Serve web UI
app.mount("/", StaticFiles(directory="static", html=True), name="static")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

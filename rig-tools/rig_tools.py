#!/usr/bin/env python3
"""rig-tools: tools for the local models on this machine, as an MCP server.

Gives a model the local shell, files and the public internet:
  run_command, read_file, write_file, edit_file, list_directory   (local, as the user running it)
  web_search, fetch_url                                            (internet)
  list_notes, delete_note                                          (optional: Open WebUI notes)

Two ways to run it:
  --http   streamable HTTP on --host/--port at /mcp, bearer token from --token-file.
           Used by Open WebUI (all models). systemd unit: rig-tools.service.
  --stdio  over stdin/stdout, started by llama-server (--mcp-servers-config).
           With --web-only it offers just web_search and fetch_url, because
           llama-server already has its own shell and file tools (--tools all).

Every overwrite or edit first copies the old file to ~/.cache/rig-tools/backups/.

sudo, always with the user's confirmation:
  request_sudo(command, reason) -> a one-time approval link the model must show the user;
  sudo_result(request_id)       -> waits for the user's decision and returns the output.
The approval page (--approve-port, on the LAN) shows the exact command and asks for the sudo password; the command
runs with `sudo -k -S` (no cached credentials), the password goes from the browser to sudo's stdin and is never
stored or logged. Without the password nothing runs. The --stdio process forwards both tools to the --http one.
"""
import argparse, asyncio, datetime, html, os, pathlib, secrets, shutil, subprocess, time

from mcp.server.fastmcp import Context, FastMCP

HOME = pathlib.Path.home()
BACKUPS = HOME / ".cache" / "rig-tools" / "backups"
MAX_OUT = 30000


def _clip(text: str, limit: int = MAX_OUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [cut: {len(text) - limit} more characters]"


def _path(p: str) -> pathlib.Path:
    return pathlib.Path(os.path.expanduser(p)).resolve()


def _backup(p: pathlib.Path) -> str:
    if not p.is_file():
        return ""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = BACKUPS / (str(p).lstrip("/").replace("/", "__") + "." + stamp)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, dst)
    return str(dst)


def add_local_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def run_command(command: str, workdir: str = "~", timeout_seconds: int = 300) -> str:
        """Run a bash command on this machine (the user who runs rig-tools, internet access, no sudo password).
        Returns the exit code and the combined stdout/stderr (long output is cut)."""
        try:
            r = subprocess.run(["bash", "-lc", command], cwd=_path(workdir), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               timeout=timeout_seconds)
            return f"exit code {r.returncode}\n" + _clip(r.stdout)
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            return f"timed out after {timeout_seconds}s\n" + _clip(out)

    @mcp.tool()
    def read_file(path: str, start_line: int = 1, max_lines: int = 500) -> str:
        """Read a text file, with line numbers. Use start_line/max_lines for long files."""
        p = _path(path)
        lines = p.read_text(errors="replace").splitlines()
        part = lines[start_line - 1:start_line - 1 + max_lines]
        body = "\n".join(f"{i}\t{l}" for i, l in enumerate(part, start_line))
        return f"{p} ({len(lines)} lines)\n" + _clip(body)

    @mcp.tool()
    def write_file(path: str, content: str, append: bool = False) -> str:
        """Create or overwrite a file (or append to it). The old version is backed up first."""
        p = _path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        bak = "" if append else _backup(p)
        with open(p, "a" if append else "w") as f:
            f.write(content)
        return f"wrote {len(content.encode())} bytes to {p}" + (f"; backup of the old file: {bak}" if bak else "")

    @mcp.tool()
    def edit_file(path: str, old_text: str, new_text: str) -> str:
        """Replace old_text (must occur exactly once) with new_text in a file. The old version is backed up first."""
        p = _path(path)
        s = p.read_text()
        n = s.count(old_text)
        if n != 1:
            return f"not changed: old_text occurs {n} times in {p} (must be exactly once)"
        bak = _backup(p)
        p.write_text(s.replace(old_text, new_text))
        return f"edited {p}; backup of the old file: {bak}"

    @mcp.tool()
    def list_directory(path: str = "~") -> str:
        """List a directory: type, size and name of each entry."""
        p = _path(path)
        rows = []
        for e in sorted(p.iterdir(), key=lambda e: e.name):
            kind = "dir " if e.is_dir() else "file"
            size = e.stat().st_size if e.is_file() else ""
            rows.append(f"{kind} {size:>12} {e.name}")
        return f"{p}\n" + _clip("\n".join(rows))

    @mcp.tool()
    def list_notes(query: str = "", count: int = 50) -> str:
        """List the user's Open WebUI notes (id, title, first line) to find one to read or delete."""
        try:
            items = _webui("GET", "/api/v1/notes/")
            items = items if isinstance(items, list) else (items or {}).get("items", [])
        except Exception as e:
            return f"cannot list notes: {e}"
        rows = []
        for n in items:
            try:
                full = _webui("GET", f"/api/v1/notes/{n['id']}")
                md = (((full or {}).get("data") or {}).get("content") or {}).get("md") or ""
            except Exception:
                md = ""
            if query and query.lower() not in ((n.get("title") or "") + md).lower():
                continue
            rows.append(f"{n['id']}  {(n.get('title') or '')[:60]}  |  {md.splitlines()[0][:70] if md else '(empty)'}")
            if len(rows) >= count:
                break
        return _clip(f"{len(rows)} notes\n" + "\n".join(rows)) if rows else "no notes matched"

    @mcp.tool()
    def delete_note(note_id: str) -> str:
        """Delete an Open WebUI note permanently (no undo). Get the id from list_notes first."""
        try:
            ok = _webui("DELETE", f"/api/v1/notes/{note_id}/delete")
        except Exception as e:
            return f"cannot delete note {note_id}: {e}"
        return f"deleted note {note_id}" if ok else f"not deleted (note {note_id} not found or no access)"


WEBUI = os.environ.get("RIG_WEBUI_URL", "http://127.0.0.1:3000")        # Open WebUI base URL
WEBUI_CONTAINER = os.environ.get("RIG_WEBUI_CONTAINER", "open-webui")   # its Docker container name
_TOKEN = {"t": "", "at": 0.0}


def _webui(method: str, path: str, body=None) -> object:
    """Call the local Open WebUI API as an admin. The token is minted inside the
    open-webui container (it needs the container's secret key + the admin's session
    stamp), cached ~8 minutes, and re-minted once if a call comes back 401."""
    import json as _json, urllib.request, urllib.error

    def mint() -> str:
        code = ("import jwt,time,sqlite3,os;"
                "k=os.environ.get('WEBUI_SECRET_KEY') or open('/app/backend/.webui_secret_key').read().strip();"
                "c=sqlite3.connect('/app/backend/data/webui.db');"
                "u=c.execute(\"select id from user where role='admin' order by created_at limit 1\").fetchone()[0];"
                "st=c.execute('select session_stamp from auth where id=?',(u,)).fetchone()[0];"
                "print(jwt.encode({'id':u,'session_stamp':st,'exp':int(time.time())+600},k,algorithm='HS256'))")
        r = subprocess.run(["docker", "exec", WEBUI_CONTAINER, "python3", "-c", code],
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise RuntimeError(f"cannot get an Open WebUI admin token (is the {WEBUI_CONTAINER} "
                               "container running?): " + r.stderr.strip()[-300:])
        return r.stdout.strip()

    def call(token: str):
        req = urllib.request.Request(
            WEBUI + path, method=method,
            data=_json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return _json.loads(resp.read() or b"null")

    if _TOKEN["t"] == "" or time.time() - _TOKEN["at"] > 480:
        _TOKEN["t"], _TOKEN["at"] = mint(), time.time()
    try:
        return call(_TOKEN["t"])
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        _TOKEN["t"], _TOKEN["at"] = mint(), time.time()      # admin logged out / re-logged in
        return call(_TOKEN["t"])


def add_web_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def web_search(query: str, max_results: int = 8) -> str:
        """Search the public web. Returns title, URL and snippet per result."""
        from ddgs import DDGS
        errors = []
        for backend in ("auto", "duckduckgo", "bing", "brave", "mojeek"):   # engines rate-limit now and then
            try:
                res = DDGS().text(query, max_results=max_results, backend=backend)
            except Exception as e:
                errors.append(f"{backend}: {e}")
                continue
            if res:
                return "\n\n".join(f"{r.get('title')}\n{r.get('href')}\n{r.get('body')}" for r in res)
        return "no results (" + "; ".join(errors) + ")"

    @mcp.tool()
    def fetch_url(url: str, max_chars: int = 20000) -> str:
        """Download a public web page or file and return its readable text (HTML is converted to markdown)."""
        import httpx, trafilatura
        r = httpx.get(url, follow_redirects=True, timeout=30,
                      headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) rig-tools"})
        ctype = r.headers.get("content-type", "")
        text = r.text
        if "html" in ctype:
            text = trafilatura.extract(r.text, output_format="markdown", include_links=True) or r.text
        return f"HTTP {r.status_code} {r.url} ({ctype})\n\n" + _clip(text, max_chars)


# ---------------------------------------------------------------- sudo with confirmation

APPROVE_URL = ""   # what the user's browser opens; set by --approve-url or auto-detected in main()
REQS = {}                                   # request_id -> dict(command, workdir, reason, state, output, ...)
REQ_TTL = 900                               # an approval link works for 15 minutes
MAX_TRIES = 3                               # wrong passwords before the request is cancelled


def _new_request(command, workdir, reason):
    for rid in [r for r, q in REQS.items() if time.time() - q["created"] > 3600]:
        REQS.pop(rid)
    rid = secrets.token_urlsafe(16)
    REQS[rid] = {"command": command, "workdir": workdir, "reason": reason, "created": time.time(),
                 "state": "pending", "output": "", "tries": 0, "event": asyncio.Event()}
    return rid


def _state(rid):
    q = REQS.get(rid)
    if q and q["state"] == "pending" and time.time() - q["created"] > REQ_TTL:
        q["state"] = "expired"; q["event"].set()
    return q


def _link(rid):
    return f"{APPROVE_URL}/sudo/{rid}"


def _created_text(rid, command):
    return (f"Approval needed. Show this link to the user and ask them to approve or deny it: {_link(rid)}\n"
            f"Command: {command}\nThe link works for {REQ_TTL // 60} minutes. Then call "
            f"sudo_result(request_id=\"{rid}\") to wait for the decision and get the output.")


def _result_text(q):
    if q is None:
        return "unknown request id"
    if q["state"] == "done":
        return f"approved by the user and run as root.\n{q['output']}"
    return {"pending": "still waiting for the user (link not answered yet); call sudo_result again",
            "denied": "the user DENIED this command. Do not retry it; ask the user what to do instead.",
            "expired": "the approval link expired without an answer; nothing was run.",
            "failed": "cancelled after too many wrong passwords; nothing was run."}.get(q["state"], q["state"])


async def _wait(rid, wait_seconds):
    q = _state(rid)
    if q and q["state"] == "pending":
        try:
            await asyncio.wait_for(q["event"].wait(), timeout=max(1, min(wait_seconds, REQ_TTL)))
        except asyncio.TimeoutError:
            pass
        q = _state(rid)
    return _result_text(q)


def _run_sudo(q, password):
    try:
        r = subprocess.run(["sudo", "-k", "-S", "-p", "", "--", "bash", "-c", q["command"]],
                           input=password + "\n", cwd=_path(q["workdir"]), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=1800)
        out = r.stdout
        if r.returncode == 1 and ("incorrect password" in out or "Sorry, try again" in out or "no password" in out):
            return None
        return f"exit code {r.returncode}\n" + _clip(out)
    except subprocess.TimeoutExpired:
        return "timed out after 1800s"


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>sudo approval</title><style>body{{font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;
color:#1b1b1b;background:#fafafa}}pre{{background:#111;color:#eee;padding:1rem;overflow-x:auto;white-space:pre-wrap;
border-radius:6px}}button{{font-size:1rem;padding:.5rem 1.2rem;margin-right:.5rem}}input{{font-size:1rem;padding:.4rem;
width:16rem}}.warn{{color:#a00}}</style>
<h1>A local model asks to run this as root</h1>{body}"""


def _page(rid, msg=""):
    q = _state(rid)
    if q is None:
        return PAGE.format(body="<p>Unknown or old request.</p>")
    e = html.escape
    head = (f"<p><b>Reason given by the model:</b> {e(q['reason'] or '(none)')}</p><p><b>Working directory:</b> "
            f"{e(q['workdir'])}</p><p><b>Command (runs exactly as shown, with sudo):</b></p><pre>{e(q['command'])}</pre>")
    if q["state"] != "pending":
        return PAGE.format(body=head + f"<p><b>Status:</b> {e(_result_text(q))}</p>"
                           + (f"<pre>{e(q['output'])}</pre>" if q["state"] == "done" else ""))
    return PAGE.format(body=head + (f"<p class=warn>{e(msg)}</p>" if msg else "") +
                       '<form method="post"><p><label>Your sudo password: <input type="password" name="password" '
                       'autocomplete="off" autofocus></label></p><button name="action" value="approve">Approve and run'
                       '</button><button name="action" value="deny">Deny</button></form>'
                       "<p>Nothing runs unless you approve with your password. The password is passed to sudo once "
                       "and not stored.</p>")


def approval_app():
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    async def show(request):
        return HTMLResponse(_page(request.path_params["rid"]))

    async def answer(request):
        rid = request.path_params["rid"]
        q = _state(rid)
        if q is None or q["state"] != "pending":
            return HTMLResponse(_page(rid))
        form = await request.form()
        if form.get("action") != "approve":
            q["state"] = "denied"; q["event"].set()
            return HTMLResponse(_page(rid))
        q["state"] = "running"
        out = await asyncio.to_thread(_run_sudo, q, form.get("password") or "")
        if out is None:
            q["tries"] += 1
            q["state"] = "pending" if q["tries"] < MAX_TRIES else "failed"
            if q["state"] == "failed":
                q["event"].set()
            return HTMLResponse(_page(rid, "Wrong password." if q["state"] == "pending" else ""))
        q["state"], q["output"] = "done", out
        q["event"].set()
        return HTMLResponse(_page(rid))

    return Starlette(routes=[Route("/sudo/{rid}", show, methods=["GET"]),
                             Route("/sudo/{rid}", answer, methods=["POST"])])


def add_sudo_tools(mcp: FastMCP, forward_to=None, token=""):
    """forward_to: base URL of the --http rig-tools (used by --stdio, which has no approval page of its own)."""
    def fwd(path, body):
        import httpx
        r = httpx.post(forward_to + path, json=body, headers={"Authorization": "Bearer " + token}, timeout=REQ_TTL + 30)
        return r.json()["text"]

    @mcp.tool()
    async def request_sudo(command: str, reason: str, workdir: str = "~", ctx: Context = None) -> str:
        """Ask the user to approve running a command as root (sudo); the user approves with their password on an
        approval page, or denies. In Open WebUI the page opens as a dialog and this returns the result directly.
        Otherwise it returns a link that you MUST show the user; then call sudo_result."""
        if forward_to:
            return await asyncio.to_thread(fwd, "/sudo-api/request", {"command": command, "reason": reason, "workdir": workdir})
        rid = _new_request(command, workdir, reason)
        if ctx is not None:
            try:   # URL elicitation: Open WebUI shows the link to the user; the model never sees the password
                r = await asyncio.wait_for(ctx.elicit_url(
                    message=f"A local model asks to run this as root:\n{command}\nReason: {reason}\n"
                            "Open the link, check the command, approve with your sudo password or deny, then come back.",
                    url=_link(rid), elicitation_id=rid), timeout=REQ_TTL)
                if r.action == "decline":
                    q = _state(rid)
                    if q["state"] == "pending":
                        q["state"] = "denied"; q["event"].set()
                    return _result_text(q)
                if r.action == "accept":
                    return await _wait(rid, REQ_TTL)
            except Exception:
                pass        # client without URL elicitation: fall back to the link
        return _created_text(rid, command)

    @mcp.tool()
    async def sudo_result(request_id: str, wait_seconds: int = 300) -> str:
        """Wait (up to wait_seconds) for the user's decision on a request_sudo link; returns the command output,
        or that it was denied/expired. Call again if it is still pending."""
        if forward_to:
            return await asyncio.to_thread(fwd, "/sudo-api/result", {"request_id": request_id, "wait_seconds": wait_seconds})
        return await _wait(request_id, wait_seconds)


async def sudo_api(scope, receive, send):
    """/sudo-api/* on the token-protected MCP port, for the --stdio processes."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    req = Request(scope, receive)
    b = await req.json()
    if scope["path"].endswith("/request"):
        text = _created_text(_new_request(b["command"], b.get("workdir", "~"), b.get("reason", "")), b["command"])
    else:
        text = await _wait(b["request_id"], int(b.get("wait_seconds", 300)))
    await JSONResponse({"text": text})(scope, receive, send)


INSTRUCTIONS = ("Tools on the user's local AI rig: local shell and files as the rig user, "
                "plus web search and page fetching on the public internet, "
                "and root commands only through request_sudo, which always needs the user's approval.")


def main() -> None:
    global APPROVE_URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true")
    ap.add_argument("--stdio", action="store_true")
    ap.add_argument("--web-only", action="store_true")
    ap.add_argument("--host", default="172.17.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token-file", default=str(HOME / ".config" / "rig-tools" / "token"))
    ap.add_argument("--approve-host", default="0.0.0.0")          # the approval page must be reachable from the LAN
    ap.add_argument("--approve-port", type=int, default=8766)
    ap.add_argument("--approve-url", default="")    # empty = auto: http://<host-your-IP>:<approve-port>
    ap.add_argument("--forward", default="http://172.17.0.1:8765")  # --stdio: where the --http rig-tools runs
    a = ap.parse_args()

    def _local_ip() -> str:                     # the address this machine uses on its LAN (no traffic sent)
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    APPROVE_URL = a.approve_url or f"http://{_local_ip()}:{a.approve_port}"
    token = pathlib.Path(a.token_file).read_text().strip()

    # host other than localhost: FastMCP does not add DNS-rebinding checks; the bearer token protects it.
    # Stateful sessions: request_sudo's approval dialog (URL elicitation) needs the client's answer routed back.
    mcp = FastMCP("rig-tools", instructions=INSTRUCTIONS, host=a.host, port=a.port)
    if not a.web_only:
        add_local_tools(mcp)
    add_web_tools(mcp)

    if a.stdio:
        add_sudo_tools(mcp, forward_to=a.forward, token=token)
        mcp.run(transport="stdio")
        return

    import uvicorn
    add_sudo_tools(mcp)
    app = mcp.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
            if auth != f"Bearer {token}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
            if scope["path"].startswith("/sudo-api/"):
                await sudo_api(scope, receive, send)
                return
        await app(scope, receive, send)

    async def serve():
        # open MCP streams would otherwise keep a stop/restart waiting forever
        tools = uvicorn.Server(uvicorn.Config(guarded, host=a.host, port=a.port, log_level="warning",
                                              timeout_graceful_shutdown=5))
        page = uvicorn.Server(uvicorn.Config(approval_app(), host=a.approve_host, port=a.approve_port,
                                             log_level="warning", access_log=False, timeout_graceful_shutdown=5))

        def stop_both(sig, frame):          # each server installs its own signal handler; stop both on SIGTERM
            tools.should_exit = page.should_exit = True
        tools.handle_exit = page.handle_exit = stop_both
        await asyncio.gather(tools.serve(), page.serve())

    asyncio.run(serve())


if __name__ == "__main__":
    main()

# local-rig-tools

Three small, self-contained tools for running **local LLMs on your own machine** and
letting them actually *do* things: a shell, files, the web, a human-approved `sudo`,
a desktop hardware monitor, and anonymous onion search over Tor.

No cloud, no API keys, no telemetry. Each piece works on its own — take what you need.

| Piece | What it is | Size |
|---|---|---|
| [`rig-tools/`](rig-tools/) | An **MCP server** that gives a local model a terminal, file access, web search/fetch, and `sudo` **only after you approve it in a browser page** | one Python file, ~470 lines |
| [`rig-widget/`](rig-widget/) | A tiny always-on **corner monitor for RAM and AMD GPUs**, including per-process VRAM *without root* | one Python file, GTK3 |
| [`tor-search/`](tor-search/) | **`.onion` search** through your local Tor proxy — DuckDuckGo's onion endpoint and Ahmia | two scripts, stdlib + curl |

Measured on Ubuntu 24.04 with 4x AMD Radeon AI PRO R9700 (ROCm), but nothing here
depends on AMD except the GPU bars in `rig-widget`.

---

## 1. rig-tools — give a local model hands (MCP server)

If you run your own model (llama.cpp, vLLM, radiance, Ollama…) behind
[Open WebUI](https://github.com/open-webui/open-webui) or llama.cpp's built-in chat
UI, this is the file that turns it from a chatbot into an assistant that can run a
command, read a log, edit a config and look something up.

**Tools it exposes**

| Tool | What the model can do |
|---|---|
| `run_command` | any `bash -lc` as your user, with exit code + combined stdout/stderr, timeout |
| `read_file` / `list_directory` | read any file you can read (line numbers, paging for long files) |
| `write_file` / `edit_file` | create / overwrite / exact-string replace — **each one backs up the old file first** to `~/.cache/rig-tools/backups/` |
| `web_search` / `fetch_url` | search (via `ddgs`) and download a page as readable markdown (via `trafilatura`) |
| `list_notes` / `delete_note` | optional: manage Open WebUI notes (needs the Open WebUI Docker container) |
| `request_sudo` / `sudo_result` | **the interesting one** — see below |

### The part you are probably wondering about: `sudo`

Giving an LLM a terminal is easy. Giving it root is the scary bit. Most MCP shells
solve this with a whitelist of allowed commands; this one solves it with **a human**.

```
model:  request_sudo("systemctl restart netdata", "apply the new alert rule you asked for")
you:    (the model shows you a link)  http://192.168.0.57:8766/sudo/xK3f…
        the page shows the exact command and the reason, then:
        [Approve and run]   [Deny]      + your sudo password
result: the command runs, its output goes back to the model.
```

Details that matter:

* The password is typed **by you, in your browser** — never by the model, and the
  model never sees it.
* The command runs as `sudo -k -S`: `-k` throws away any cached credential, so an
  earlier approval never leaks into this one, and the password goes straight to
  sudo's stdin.
* Nothing is written to disk or to any log. Denied means denied.
* A link is single-use, works for 15 minutes, and dies after 3 wrong passwords.

### Run it

```bash
python3 -m venv ~/.local/share/rig-tools/venv
~/.local/share/rig-tools/venv/bin/pip install -r rig-tools/requirements.txt

mkdir -p ~/.config/rig-tools
head -c 24 /dev/urandom | base64 | tr -d '=+/' > ~/.config/rig-tools/token   # the MCP bearer token
chmod 600 ~/.config/rig-tools/token

# start it (HTTP mode = one server shared by every model)
~/.local/share/rig-tools/venv/bin/python rig-tools/rig_tools.py \
    --http --host 172.17.0.1 --port 8765
```

Two transports, because the two common UIs want different ones:

| Mode | Who uses it | How |
|---|---|---|
| `--http` | **Open WebUI**: Settings → External Connections → add MCP server `http://<host>:8765/mcp`, bearer auth with your token | the command above |
| `--stdio --web-only` | **llama.cpp's chat UI**: `llama-server --tools all --mcp-servers-config rig-tools/mcp-web.json` — web-only, because llama-server already ships its own shell and file tools | edit the paths in that JSON |

`--stdio` has no browser of its own, so `request_sudo` there returns a link and
forwards the approval to the `--http` instance (`--forward`, default
`http://172.17.0.1:8765`).

**Run at boot:** copy [`rig-tools/rig-tools.service.example`](rig-tools/rig-tools.service.example)
to `/etc/systemd/system/rig-tools.service`, fix the user and paths, then
`sudo systemctl daemon-reload && sudo systemctl enable --now rig-tools`.

**Flags** (defaults match the commands above): `--host --port --token-file
--approve-host --approve-port --approve-url --forward`. With no `--approve-url` the
URL is built from the machine's own LAN address, so the approval link works out of the
box on any box. `RIG_WEBUI_URL` / `RIG_WEBUI_CONTAINER` point the note tools at a
different Open WebUI install.

### Read the warnings before pointing it at a model

* `run_command` is an **unrestricted shell as your user**. The model can delete your
  files, install anything, and read anything you can read. This is a tool for a
  machine you own and trust the model on — it is deliberately *not* a sandbox. (For
  sandboxed alternatives see [cli-mcp-server](https://github.com/MladenSU/cli-mcp-server)
  and [llm-sandbox](https://github.com/vndee/llm-sandbox).)
* The MCP port is guarded by the bearer token but is **plain HTTP**, and the approval
  page is HTTP too, so the sudo password crosses your LAN in clear text. Fine on a
  home LAN; put both behind TLS or on localhost if that's not you.
* Everything runs as the one user running rig-tools. There is no per-tool ACL —
  either the model has the shell or it does not.

## 2. rig-widget — RAM and AMD GPU, always in a corner

A small frameless widget in a screen corner that answers, without a terminal: how
full is RAM and who fills it; how busy is each GPU and who is using each GPU.

Why it exists: on a machine with ROCm installed the usual suspects misbehave — `btop`
segfaults at startup on some ROCm setups
([aristocratos/btop#705](https://github.com/aristocratos/btop/issues/705)), a
static/musl `btop` has no GPU panel at all, and `nvtop` can abort on an assertion when
many AMD processes run ([Syllo/nvtop#174](https://github.com/Syllo/nvtop/issues/174)).
This reads only `sysfs` and `/proc`, so a driver change cannot break it.

```bash
chmod +x rig-widget/rig-widget && cp rig-widget/rig-widget ~/.local/bin/
rig-widget --once            # one text snapshot - works over SSH, no display needed
rig-widget --json            # the same as JSON, for scripts and dashboards
rig-widget                   # the corner widget (default: bottom-right)
rig-widget --list-monitors   # then place it with --monitor N and --corner tl|tr|bl|br
rig-widget --stop            # quit the running widget
```

In the widget: `q` / `Esc` quit, `h` toggles the "who" lines, click = refresh now.

Autostart on a GNOME/KDE desktop — `~/.config/autostart/rig-widget.desktop`:

```ini
[Desktop Entry]
Type=Application
Name=rig-widget
Exec=/home/YOU/.local/bin/rig-widget --interval 2
Terminal=false
Categories=System;Monitor;
```

Requires GTK 3 for the window (`sudo apt install python3-gi gir1.2-gtk-3.0`);
`--once` / `--json` need no GUI at all.

Design notes, in case you want to steal the tricks:

* **Per-process GPU memory without root** comes from `/proc/<pid>/fdinfo/*`:
  `amd-requested-vram` / `amd-requested-gtt` are the honest numbers, while
  `drm-memory-vram` also counts shared buffers. Values are per PASID, so the same
  PASID seen on several fds is taken as **max, not sum** — summing is what makes a
  desktop compositor look like it holds "1197% VRAM".
* A full scan costs ~25 ms and **never wakes a runtime-suspended card**: no
  performance counters, no `rocm-smi` subprocess per tick (`rocm-smi` is ~30 MB RSS
  and ~70 ms per call). Idle cards return `EBUSY` from sysfs, which is normal and
  shown as `-`.
* Steady cost on the desktop: **~2% of one core, ~46 MB RSS**.
* No sudo — but your user must read `/sys/class/drm/card*/` and those fdinfo files,
  which normally means being in group `render` (and often `video`).
* It draws with GTK3 through Xwayland, so it also appears on Wayland sessions.

## 3. tor-search — search the onion web from the shell

Two scripts, `stdlib + curl` only, both routed through your local Tor SOCKS5 proxy
(`127.0.0.1:9050`, e.g. `sudo apt install tor && sudo systemctl enable --now tor`).
They query **public indexes of legitimate onion services** — a way to search the
hidden web anonymously, not a way to find anything unusual.

```bash
python3 tor-search/find_in_dark_web.py "privacy tools" --count 8   # DuckDuckGo's official .onion search
python3 tor-search/ahmia_query.py     "privacy tools" --count 8    # Ahmia, an onion index that filters abuse
```

Why both: DuckDuckGo's onion service indexes the public onion web well, while Ahmia
covers a different slice of it and returns a cleaner, filtered set. Because both use
`curl --socks5-hostname`, DNS **and** the connection leave over Tor, so the index sees
an exit node instead of your IP.

A failing query says so instead of quietly returning nothing — check
`systemctl status tor@default` first. A malformed onion address is answered by Tor
instantly with "Invalid hostname", so copy the addresses in the scripts exactly.

## Repository layout

```
rig-tools/     rig_tools.py, rig-tools.service.example, mcp-web.json, requirements.txt
rig-widget/    rig-widget
tor-search/    find_in_dark_web.py, ahmia_query.py
LICENSE        MIT
```

## Licence and credits

MIT — see [LICENSE](LICENSE). Written on a home rig (4x AMD Radeon AI PRO R9700,
llama-swap + Open WebUI) because the pieces were needed there; every number quoted
above was measured on it. Issues and pull requests welcome.

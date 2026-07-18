#!/usr/bin/env python3
"""ChatGPT Endless Canvas — local infinite-canvas board.

Serves board.html (a pan/zoom canvas) and watches the ACTIVE board's
directory: any image dropped into it shows up on the board within ~2s. Pair
it with generate_chatgpt_image.py by pointing --output into that directory.

Data lives under ~/Documents/chatgpt-endless-image-gen/ by default:
boards.json is the board registry ({activeId, boards: [{id, name, dir,
createdAt}]}); each board is a plain directory (default name = creation
timestamp) holding its images plus its own layout.json / annotations.json /
lineage.json — a board is fully portable: point a new board entry at any
directory to "open" it. Create/open/switch via POST /api/boards; every other
API call operates on the active board.

Lovart-style iteration loop: annotate an image on the board (box/point +
revision note), hit 改图, and this server runs generate_chatgpt_image.py
itself -- attaching the original as the reference image -- then records the
parent/child relation in lineage.json so the board can draw the family tree.
「参考生图」(POST /api/generate) instead treats the image as a style
reference for brand-new content. Drag & drop uploads land via /api/upload.

The server itself is stdlib-only; the generation subprocess uses the
project's .venv (playwright) and needs the debug Chrome from
launch-chrome-debug.sh logged into chatgpt.com. Run:

    python3 board_server.py [--port 8090] [--dir path/to/single/board]

--dir (or BOARD_DIR) forces single-board mode on that directory, bypassing
the registry — kept for scripted use.
"""
import argparse
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.expanduser(
    os.environ.get("IMAGE_GEN_DATA", "~/Documents/chatgpt-endless-image-gen"))
REGISTRY_PATH = os.path.join(DATA_ROOT, "boards.json")
GEN_SCRIPT = os.path.join(HERE, "generate_chatgpt_image.py")
VENV_PY = os.path.join(HERE, ".venv", "bin", "python")
PYTHON = VENV_PY if os.path.exists(VENV_PY) else sys.executable

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif"}

# Must mirror the constants in board.html so server-side placement of edited
# children lands on the same grid the frontend uses.
CELL_W, CELL_H, CARD_W = 360, 460, 320

# Random pause between batch jobs (seconds) — spreads generations out so a
# prompt list doesn't hammer ChatGPT into rate limiting. BATCH_INTERVAL="30-120".
try:
    BATCH_MIN, BATCH_MAX = (int(x) for x in
                            os.environ.get("BATCH_INTERVAL", "30-120").split("-", 1))
except ValueError:
    BATCH_MIN, BATCH_MAX = 30, 120
DRY_RUN = bool(os.environ.get("BOARD_DRY_RUN"))  # tests: copy instead of generate

STATE_LOCK = threading.Lock()   # guards the json files against handler/worker races
JOBS_LOCK = threading.Lock()
JOBS = []                       # [{id, parent, dir, status, createdAt, output?, error?}]
JOB_Q = queue.Queue()


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _new_board_dir():
    """Default board directory: data root + current time."""
    d = os.path.join(DATA_ROOT, time.strftime("%Y%m%d-%H%M%S"))
    n = 1
    while os.path.exists(d):
        d = os.path.join(DATA_ROOT, time.strftime("%Y%m%d-%H%M%S") + f"-{n}")
        n += 1
    return d


# -- board registry -----------------------------------------------------------

def _load_registry():
    with STATE_LOCK:
        reg = _read_json(REGISTRY_PATH, None)
        if not isinstance(reg, dict) or not reg.get("boards"):
            first = _new_board_dir()
            os.makedirs(first, exist_ok=True)
            reg = {"activeId": "default",
                   "boards": [{"id": "default", "name": "默认画布",
                               "dir": first, "createdAt": time.time()}]}
            _write_json(REGISTRY_PATH, reg)
        return reg


def _save_registry(reg):
    with STATE_LOCK:
        os.makedirs(DATA_ROOT, exist_ok=True)
        _write_json(REGISTRY_PATH, reg)


def _active_board():
    if Handler.fixed_dir:
        return {"id": "fixed", "name": os.path.basename(Handler.fixed_dir) or "画布",
                "dir": Handler.fixed_dir}
    reg = _load_registry()
    b = next((x for x in reg["boards"] if x["id"] == reg["activeId"]),
             reg["boards"][0])
    os.makedirs(b["dir"], exist_ok=True)
    return b


# -- regeneration worker -------------------------------------------------------

ZONES = [["top-left", "top", "top-right"],
         ["middle-left", "center", "middle-right"],
         ["bottom-left", "bottom", "bottom-right"]]


def _zone(a):
    cx = a.get("x", 0.5) + a.get("w", 0) / 2
    cy = a.get("y", 0.5) + a.get("h", 0) / 2
    col = 0 if cx < 1 / 3 else (1 if cx < 2 / 3 else 2)
    row = 0 if cy < 1 / 3 else (1 if cy < 2 / 3 else 2)
    return ZONES[row][col]


def _build_prompt(pending):
    lines = [f"{i + 1}. [{_zone(a)} of the image] {a['note'].strip()}"
             for i, a in enumerate(pending)]
    return ("Keep the attached reference image's subject, composition and art "
            "style, and generate a new version applying ONLY the following "
            "modifications:\n" + "\n".join(lines))


def _cell(pos):
    return (round(pos["x"] / CELL_W), round(pos["y"] / CELL_H))


def _spot_near(layout, ppos):
    """First free grid cell near the parent (prefer to its right)."""
    used = {_cell(p) for p in layout.values() if isinstance(p, dict)}
    pi, pj = _cell(ppos)
    for di, dj in [(1, 0), (1, 1), (1, -1), (0, 1), (0, -1),
                   (2, 0), (2, 1), (2, -1), (-1, 0), (-1, 1), (-1, -1)]:
        c = (pi + di, pj + dj)
        if c not in used:
            return {"x": c[0] * CELL_W - CARD_W / 2,
                    "y": c[1] * CELL_H - CELL_H / 2 + 20}
    return None


def _run_job(job):
    board_dir = job["dir"]
    parent = job.get("parent") or ""
    kind = job.get("kind", "edit")
    ann_path = os.path.join(board_dir, "annotations.json")

    if kind == "gen":
        # plain generation: no reference image at all
        prompt = (job.get("prompt") or "").strip()
        if not prompt:
            raise RuntimeError("prompt 为空")
        notes = [prompt]
        prefix = "Generate an image:"
    elif kind == "ref":
        # style-reference generation: fresh subject, parent's look & feel
        prompt = (job.get("prompt") or "").strip()
        if not prompt:
            raise RuntimeError("prompt 为空")
        notes = [prompt]
        prefix = ("Using the attached image ONLY as a style reference (match "
                  "its art style, color palette and rendering technique), "
                  "generate:")
    else:
        with STATE_LOCK:
            anns = _read_json(ann_path, {})
        pending = [a for a in anns.get(parent, [])
                   if a.get("status") != "done" and (a.get("note") or "").strip()]
        if not pending:
            raise RuntimeError("该图没有待执行的修改标注")
        prompt = _build_prompt(pending)
        notes = [a["note"] for a in pending]
        prefix = "Edit the attached reference image:"

    if kind == "gen":
        stem = re.sub(r"[^0-9A-Za-z一-鿿_-]+", "-", prompt).strip("-")[:32] or "image"
    else:
        stem = re.sub(r"^\d{8}-\d{6}-", "", os.path.splitext(parent)[0])
        stem = re.sub(r"^(edit|ref)-", "", stem)[:40]
    out_name = time.strftime("%Y%m%d-%H%M%S") + f"-{kind}-{stem}.png"
    out_path = os.path.join(board_dir, out_name)

    if DRY_RUN:
        time.sleep(1)
        if parent:
            shutil.copyfile(os.path.join(board_dir, parent), out_path)
        else:  # 1x1 png placeholder
            import base64
            with open(out_path, "wb") as fh:
                fh.write(base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
                    "nGNiYAAAAAkAAxkR2eQAAAAASUVORK5CYII="))
    else:
        cmd = [PYTHON, GEN_SCRIPT,
               "--prompt", prompt,
               "--output", out_path,
               "--timeout", "420",
               "--prompt-prefix", prefix]
        if parent:
            cmd += ["--reference", os.path.join(board_dir, parent)]
        cdp = os.environ.get("CHATGPT_CDP_URL")
        if cdp:
            cmd += ["--cdp-url", cdp]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=500)
        if proc.returncode != 0 or not os.path.exists(out_path):
            tail = "\n".join((proc.stderr or "").strip().splitlines()[-3:])
            raise RuntimeError(tail or "生成脚本失败（无输出）")

    with STATE_LOCK:
        if parent:  # plain generations have no ancestry to record
            lin_path = os.path.join(board_dir, "lineage.json")
            lineage = _read_json(lin_path, {})
            lineage[out_name] = {"parent": parent, "kind": kind,
                                 "notes": notes,
                                 "prompt": prompt,
                                 "createdAt": time.time()}
            _write_json(lin_path, lineage)

        if kind != "ref":
            anns = _read_json(ann_path, {})
            for a in anns.get(parent, []):
                if a.get("status") != "done":
                    a["status"] = "done"
            _write_json(ann_path, anns)

        lay_path = os.path.join(board_dir, "layout.json")
        layout = _read_json(lay_path, {})
        if parent in layout:
            pos = _spot_near(layout, layout[parent])
            if pos:
                layout[out_name] = pos
                _write_json(lay_path, layout)
    return out_name


def _worker():
    while True:
        job = JOB_Q.get()
        delay = job.get("delaySec") or 0
        if delay:
            with JOBS_LOCK:
                job["status"] = "waiting"
                job["resumeAt"] = time.time() + delay
            time.sleep(delay)
        with JOBS_LOCK:
            job["status"] = "running"
            job["startedAt"] = time.time()
        try:
            out = _run_job(job)
            with JOBS_LOCK:
                job["status"] = "done"
                job["output"] = out
        except Exception as exc:  # noqa: BLE001
            with JOBS_LOCK:
                job["status"] = "error"
                job["error"] = str(exc)[:500]


# -- http ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    fixed_dir = None  # set by --dir / BOARD_DIR for single-board mode

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_file(os.path.join(HERE, "board.html"), "text/html; charset=utf-8")
        elif path == "/api/state":
            board = _active_board()
            with JOBS_LOCK:
                jobs = [dict(j) for j in JOBS[-200:] if j.get("dir") == board["dir"]]
            self._send_json({
                "board": board,
                "images": self._list_images(board["dir"]),
                "layout": self._board_json(board["dir"], "layout.json"),
                "annotations": self._board_json(board["dir"], "annotations.json"),
                "lineage": self._board_json(board["dir"], "lineage.json"),
                "jobs": jobs,
            })
        elif path == "/api/boards":
            if Handler.fixed_dir:
                b = _active_board()
                self._send_json({"boards": [b], "activeId": b["id"], "fixed": True})
            else:
                reg = _load_registry()
                self._send_json({"boards": reg["boards"], "activeId": reg["activeId"]})
        elif path == "/api/images":  # kept for health checks / older clients
            self._send_json(self._list_images(_active_board()["dir"]))
        elif path == "/api/layout":
            self._send_json(self._board_json(_active_board()["dir"], "layout.json"))
        elif path.startswith("/images/"):
            name = os.path.basename(unquote(path[len("/images/"):]))
            ext = os.path.splitext(name)[1].lower()
            if ext not in IMAGE_EXTS:
                return self._send_error(404, "not an image")
            self._send_file(os.path.join(_active_board()["dir"], name),
                            MIME.get(ext, "application/octet-stream"),
                            cache="max-age=86400")
        else:
            self._send_error(404, "not found")

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        board = _active_board()

        if path == "/api/upload":  # raw image bytes, not json
            return self._handle_upload(parsed, board)

        body = self._read_body()
        if body is None:
            return self._send_error(400, "bad json")

        # Writes carry the board id they were made on; drop them if the active
        # board has changed underneath (e.g. another tab switched boards).
        qboard = (parse_qs(parsed.query).get("board") or [None])[0]
        if path in ("/api/layout", "/api/annotations") and qboard and qboard != board["id"]:
            return self._send_json({"ok": False, "stale": True})

        if path == "/api/layout":
            if not isinstance(body, dict):
                return self._send_error(400, "layout must be an object")
            with STATE_LOCK:
                _write_json(os.path.join(board["dir"], "layout.json"), body)
            self._send_json({"ok": True})
        elif path == "/api/annotations":
            if not isinstance(body, dict):
                return self._send_error(400, "annotations must be an object")
            with STATE_LOCK:
                _write_json(os.path.join(board["dir"], "annotations.json"), body)
            self._send_json({"ok": True})
        elif path == "/api/regenerate":
            name = os.path.basename(str(body.get("name", "")))
            if not name or not os.path.exists(os.path.join(board["dir"], name)):
                return self._send_error(400, "图片不存在")
            anns = self._board_json(board["dir"], "annotations.json").get(name, [])
            if not any(a.get("status") != "done" and (a.get("note") or "").strip()
                       for a in anns):
                return self._send_error(400, "该图没有待执行的修改标注")
            with JOBS_LOCK:
                if any(j["parent"] == name and j.get("dir") == board["dir"]
                       and j["status"] in ("queued", "running") for j in JOBS):
                    return self._send_error(409, "这张图已有生成任务在进行")
                job = {"id": f"job-{int(time.time() * 1000)}", "parent": name,
                       "dir": board["dir"], "status": "queued",
                       "createdAt": time.time()}
                JOBS.append(job)
            JOB_Q.put(job)
            self._send_json(job)
        elif path == "/api/generate":
            # generation: with "name" the named image is a style reference;
            # without it the prompt(s) generate from scratch. A prompt LIST
            # becomes a scheduled batch — jobs run serially with a random
            # pause between them.
            name = os.path.basename(str(body.get("name", "")))
            prompts = body.get("prompts")
            if not isinstance(prompts, list):
                prompts = [body.get("prompt")]
            prompts = [str(p).strip() for p in prompts if str(p or "").strip()]
            if name and not os.path.exists(os.path.join(board["dir"], name)):
                return self._send_error(400, "图片不存在")
            if not prompts:
                return self._send_error(400, "prompt 不能为空")
            if len(prompts) > 50:
                return self._send_error(400, "一次最多 50 条 prompt")
            kind = "ref" if name else "gen"
            jobs = []
            with JOBS_LOCK:
                queue_not_empty = any(
                    j["status"] in ("queued", "waiting", "running") for j in JOBS)
                for i, prompt in enumerate(prompts):
                    # pause before every job except the very first when idle
                    delay = 0 if (i == 0 and not queue_not_empty) \
                        else random.randint(BATCH_MIN, BATCH_MAX)
                    job = {"id": f"job-{int(time.time() * 1000)}-{i}",
                           "parent": name, "kind": kind, "prompt": prompt,
                           "delaySec": delay,
                           "dir": board["dir"], "status": "queued",
                           "createdAt": time.time()}
                    JOBS.append(job)
                    jobs.append(job)
            for job in jobs:
                JOB_Q.put(job)
            self._send_json({"ok": True, "count": len(jobs), "jobs": jobs})
        elif path == "/api/boards":
            self._handle_boards_post(body)
        else:
            self._send_error(404, "not found")

    def _handle_upload(self, parsed, board):
        """POST /api/upload?name=<orig filename> with raw image bytes as body
        (what fetch(file) sends). Saves into the active board's directory."""
        orig = os.path.basename(
            (parse_qs(parsed.query).get("name") or ["image.png"])[0])
        ext = os.path.splitext(orig)[1].lower()
        if ext not in IMAGE_EXTS:
            return self._send_error(400, f"不支持的图片格式: {ext or '(无扩展名)'}")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return self._send_error(400, "空文件")
        if length > 50 * 1024 * 1024:
            return self._send_error(400, "文件超过 50MB")
        data = self.rfile.read(length)
        stem = re.sub(r"[^0-9A-Za-z一-鿿_-]+", "-",
                      os.path.splitext(orig)[0]).strip("-")[:40] or "upload"
        name = time.strftime("%Y%m%d-%H%M%S") + f"-{stem}{ext}"
        path = os.path.join(board["dir"], name)
        n = 1
        while os.path.exists(path):  # same-second multi-file drop
            name = time.strftime("%Y%m%d-%H%M%S") + f"-{stem}-{n}{ext}"
            path = os.path.join(board["dir"], name)
            n += 1
        with open(path, "wb") as fh:
            fh.write(data)
        self._send_json({"ok": True, "name": name})

    def _handle_boards_post(self, body):
        if Handler.fixed_dir:
            return self._send_error(400, "server 以 --dir 固定目录模式运行，不支持多画布")
        action = body.get("action")
        reg = _load_registry()
        if action == "create":
            name = (str(body.get("name") or "")).strip()
            d = (str(body.get("dir") or "")).strip()
            if d:
                d = os.path.expanduser(d)
                if not os.path.isabs(d):
                    # relative input like "test" → under the data root, not
                    # wherever the server happened to be started from
                    d = os.path.join(DATA_ROOT, d)
                d = os.path.abspath(d)
            elif name:
                # named board → data root + name; timestamp only when unnamed
                slug = re.sub(r"[^0-9A-Za-z一-鿿_-]+", "-", name).strip("-")
                d = os.path.join(DATA_ROOT, slug) if slug else _new_board_dir()
            else:
                d = _new_board_dir()
            name = name or time.strftime("画布 %m-%d %H:%M")
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as exc:
                return self._send_error(400, f"无法创建目录: {exc}")
            existing = next((x for x in reg["boards"] if x["dir"] == d), None)
            if existing:
                reg["activeId"] = existing["id"]
                board = existing
            else:
                board = {"id": f"b{int(time.time() * 1000)}", "name": name,
                         "dir": d, "createdAt": time.time()}
                reg["boards"].append(board)
                reg["activeId"] = board["id"]
            _save_registry(reg)
            self._send_json({"ok": True, "board": board, "activeId": reg["activeId"]})
        elif action == "open":
            bid = body.get("id")
            if not any(x["id"] == bid for x in reg["boards"]):
                return self._send_error(404, "画布不存在")
            reg["activeId"] = bid
            _save_registry(reg)
            self._send_json({"ok": True, "activeId": bid})
        else:
            self._send_error(400, "unknown action (use create/open)")

    # -- helpers ---------------------------------------------------------

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:  # noqa: BLE001
            return None

    def _board_json(self, board_dir, fname):
        with STATE_LOCK:
            return _read_json(os.path.join(board_dir, fname), {})

    def _list_images(self, board_dir):
        items = []
        try:
            for name in os.listdir(board_dir):
                if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
                    continue
                full = os.path.join(board_dir, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size == 0:  # still being written
                    continue
                items.append({"name": name, "mtime": st.st_mtime, "size": st.st_size})
        except FileNotFoundError:
            pass
        items.sort(key=lambda it: it["mtime"])
        return items

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, full, mime, cache="no-store"):
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send_error(404, "not found")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code, msg):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quiet the per-request noise
        if "/api/" not in (args[0] if args else ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("BOARD_PORT", "8090")))
    ap.add_argument("--dir", default=os.environ.get("BOARD_DIR"),
                    help="single-board mode: watch exactly this directory")
    args = ap.parse_args()

    if args.dir:
        Handler.fixed_dir = os.path.abspath(os.path.expanduser(args.dir))
        os.makedirs(Handler.fixed_dir, exist_ok=True)
        where = Handler.fixed_dir
    else:
        os.makedirs(DATA_ROOT, exist_ok=True)
        _load_registry()  # ensure registry + default board exist
        where = f"registry {REGISTRY_PATH}"

    threading.Thread(target=_worker, daemon=True).start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        import errno
        import urllib.request
        if exc.errno != errno.EADDRINUSE:
            raise
        # Port taken — if it's another board instance, that's success for an
        # idempotent caller; if it's some other program, fail loudly.
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{args.port}/api/state", timeout=2) as r:
                json.load(r)
            print(f"board already running: http://127.0.0.1:{args.port}", flush=True)
            sys.exit(0)
        except Exception:  # noqa: BLE001
            print(f"port {args.port} is taken by something else — "
                  f"use BOARD_PORT/--port to pick another", file=sys.stderr)
            sys.exit(1)
    print(f"board: http://127.0.0.1:{args.port}  ({where})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

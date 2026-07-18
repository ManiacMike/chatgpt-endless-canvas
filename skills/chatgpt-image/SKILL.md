---
name: chatgpt-image
description: Generate images through the ChatGPT web app (chatgpt.com) via a local infinite-canvas board — queued jobs, scheduled batches, annotate-to-edit, style-reference generation, lineage. Use when the user asks to generate images ("生图", "draw with ChatGPT", "open the board", "new canvas"), or wants images consistent with a reference style. Requires a debug Chrome logged into chatgpt.com (CDP port 9222).
---

# ChatGPT image generation (board-first)

Project home: `${CHATGPT_IMAGE_GEN_HOME:-~/Workspace/chatgpt-endless-image-gen}`
(referred to as `$PROJ` below — resolve it once per session). Data root:
`~/Documents/chatgpt-endless-image-gen/`.

**Core rule: submit ALL generation requests through the board server API**
(`POST /api/generate`) instead of invoking the Python script directly — the
server serializes jobs, spaces batches with random pauses (rate-limit
protection), streams results onto the canvas, and records lineage. Call the
script directly only as a fallback when the server cannot run, or for
`--grab-only` recovery.

## Standard flow (every request)

1. **Debug Chrome ready?**

   ```bash
   curl -s --max-time 2 http://127.0.0.1:9222/json/version
   ```

   On failure → ask the user to run `bash $PROJ/launch-chrome-debug.sh`, log
   into chatgpt.com in the opened window, and keep it open. Do NOT run it for
   them in the background — the window needs to stay interactive.

2. **Board server running?** Use the idempotent launcher (safe from any
   session; already-running → no-op; starts detached via nohup so it outlives
   this session):

   ```bash
   bash $PROJ/start.sh
   ```

   Then `open http://127.0.0.1:8090` if the user doesn't have the board open.
   Do NOT run board_server.py with run_in_background — that ties the server to
   this session. Log: `~/Documents/chatgpt-endless-image-gen/board.log`.

3. **Submit** (`POST /api/generate`, pick by scenario):

   - Plain: `{"prompt": "specific English description"}`
   - Multiple images: `{"prompts": ["...", ...]}` (≤50; the server queues them
     with random 30–120 s gaps — ALWAYS use this for batches, never loop
     yourself)
   - Style reference from a board image: add `"name": "<filename>"`
   - Local file as reference: first `POST /api/upload?name=<file>` with raw
     bytes (`curl --data-binary @file.png`), then generate with the returned
     `name`
   - Edit by annotations: `POST /api/regenerate` `{"name":"<image>"}` — needs
     pending annotations (you may write them for the user via
     `POST /api/annotations`: `{image: [{id,x,y,w,h,note,status:"pending"}]}`,
     coords normalized 0-1, point marks have w=h=0)

4. **Await results**: poll `GET /api/state` → `jobs` until the job is `done`
   (`output` = filename) or `error` (reason included). ~1-5 min per image. For
   batches don't block — tell the user images will appear on the board as they
   finish. Files land in the active board dir (`state.board.dir`).

## Boards

- One board = one self-contained directory (images + layout/annotations/
  lineage JSON). Registry: `~/Documents/chatgpt-endless-image-gen/boards.json`.
- `GET /api/boards` lists; `POST /api/boards`
  `{"action":"create","name":"test"}` → dir `<data root>/test` (named boards
  use the name, unnamed use a timestamp; `dir` may point anywhere — an
  existing directory is "opened"); `{"action":"open","id":"..."}` switches.
- When the user says "new canvas / switch canvas / open directory X" → call
  the API; subsequent generations follow the new active board automatically.

## Board UI (tell the user when relevant)

Wheel zoom, drag-empty pan, drag cards, double-click full size, drag local
images in. Card buttons: 标注 (annotate: box + note) → 改图 (regenerate),
参考生图 (style-reference; one prompt per line — multiple lines become a
scheduled batch). Lineage: solid "↳ 改自" (edit), dashed "☆ 参考…风格" (ref).

## Fallback: direct script (server unusable only)

```bash
$PROJ/.venv/bin/python $PROJ/generate_chatgpt_image.py \
  --prompt "..." --output /abs/path/out.png \
  [--reference ref.png] [--timeout 240] [--grab-only]
```

Progress on stderr, exit 0 = success; set Bash timeout ≥ `--timeout` + 60 s.
If the script timed out but the image finished on the ChatGPT page, re-grab
with `--grab-only` (pass any placeholder `--prompt`).

## Troubleshooting

- **"ChatGPT not reachable"** → debug Chrome not running; step 1.
- **"could not find the ChatGPT message box"** → not logged into chatgpt.com.
- **"stale CDP state"** → quit the debug Chrome fully, rerun the launch script
  (login persists).
- **Job error / prompt sent but no reply at all** → ChatGPT silent rate
  limiting; retry later. The batch scheduler's random gaps exist to avoid this.
- **All selectors failing** → ChatGPT web redesign; update `_find_composer` /
  `_submit` / `_IMAGE_JS` in `generate_chatgpt_image.py`.

Env knobs: `BOARD_PORT` (8090), `IMAGE_GEN_DATA` (data root),
`CHATGPT_CDP_URL` (9222), `BATCH_INTERVAL` ("30-120"), `BOARD_DIR`
(single-board mode), `CHATGPT_IMAGE_GEN_HOME` (project location).

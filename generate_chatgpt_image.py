#!/usr/bin/env python3
"""Drive an already-logged-in ChatGPT (web) over CDP to generate one image.

This attaches to a Chrome you launched with --remote-debugging-port (see
launch-chrome-debug.sh) and reuses your logged-in chatgpt.com session. It opens
a fresh chat, sends the prompt, waits for the generated image, and writes the
PNG bytes to --output.

It does NOT launch a browser (connect_over_cdp), so only `pip install playwright`
is required -- no `playwright install`.

Concurrency: pass --tab-slot N to give each parallel run its own dedicated
chatgpt.com tab (tagged via window.name). After the prompt is sent the chat's
conversation uuid (chatgpt.com/c/<uuid>) is captured — written to --meta as
JSON, used to pin the wait loop to the right conversation, and usable later
with --grab-only --conversation <uuid> to recover an interrupted run's image.

Notes:
- ChatGPT's web UI changes often; the selectors below are intentionally
  defensive with fallbacks. If generation stops working, update the selectors.
- This is for personal/local use with your own account.

Exit code 0 on success (file written); non-zero with a human-readable message on
stderr otherwise (callers surface this message to the UI).
"""
import argparse
import base64
import re
import sys
import time


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fail(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    log("ERROR: " + msg)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--output", required=True, help="path to write the PNG")
    ap.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    ap.add_argument("--timeout", type=int, default=120, help="seconds to wait for the image")
    ap.add_argument("--prompt-prefix", default="Generate an image:")
    ap.add_argument("--reference", default="", help="optional reference image to attach")
    ap.add_argument("--grab-only", action="store_true",
                    help="don't send a prompt; just re-grab the latest image on the current page")
    ap.add_argument("--tab-slot", type=int, default=0,
                    help="dedicated tab index for concurrent runs — each slot claims "
                         "its own chatgpt.com tab (tagged via window.name) so parallel "
                         "generations never share a composer")
    ap.add_argument("--conversation", default="",
                    help="with --grab-only: grab from this ChatGPT conversation uuid "
                         "(navigates to chatgpt.com/c/<uuid> first) — used to recover "
                         "an image whose job was interrupted after the prompt was sent")
    ap.add_argument("--meta", default="",
                    help="path to write {conversationId} JSON as soon as the chat's "
                         "uuid is known (callers use it as the task's stable id)")
    args = ap.parse_args()

    prompt = args.prompt.strip()
    if not prompt:
        fail("empty prompt")
    message = f"{args.prompt_prefix} {prompt}".strip()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail("playwright is not installed. Run: .venv/bin/pip install playwright")

    with sync_playwright() as p:
        # Attach to the user's running Chrome. Try the given URL, then an IPv4
        # fallback (Chrome binds the debug port on IPv4 only; "localhost" may
        # resolve to ::1 and refuse the connection).
        urls = [args.cdp_url]
        if "localhost" in args.cdp_url:
            urls.append(args.cdp_url.replace("localhost", "127.0.0.1"))
        browser = None
        errs = []
        for url in urls:
            try:
                browser = p.chromium.connect_over_cdp(url)
                break
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{url}: {exc}")
        if browser is None:
            joined = " | ".join(errs)
            if "context management is not supported" in joined:
                fail(
                    "The debug Chrome is in a stale CDP state. Quit it and re-run "
                    "launch-chrome-debug.sh (you stay logged in), then retry. " + joined
                )
            fail(
                "ChatGPT not reachable. Launch Chrome with launch-chrome-debug.sh "
                "and log into chatgpt.com. " + joined
            )

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        # Reuse existing tabs rather than opening one per run. Piling up
        # tabs/targets in the long-running Chrome is what eventually breaks the
        # CDP session ("Browser context management is not supported"). Each
        # concurrent worker claims ONE dedicated tab (by slot) and keeps reusing
        # it. We also do NOT close the page or the browser afterwards: page.close
        # churns targets and browser.close() mutates the shared download-behavior
        # state, both of which corrupt the next connect. Just disconnect (with-exit).
        page = _slot_page(context, args.tab_slot)
        if args.grab_only:
            if args.conversation:
                page = _find_conversation_page(context, args.conversation) or page
                if args.conversation not in (page.url or ""):
                    log(f"opening conversation {args.conversation} ...")
                    try:
                        page.goto(f"https://chatgpt.com/c/{args.conversation}",
                                  wait_until="domcontentloaded")
                        time.sleep(2.0)
                    except Exception as exc:  # noqa: BLE001
                        fail(f"could not open conversation {args.conversation}: {exc}")
            _grab(page, args.output, args.timeout)
        else:
            _run(page, message, args.output, args.timeout, args.reference,
                 meta=args.meta)


def _slot_page(context, slot: int):
    """Return the dedicated tab for this slot, claiming or creating it.

    Concurrent runs must never share a tab (they would type into the same
    composer and grab each other's images), so each slot owns one chatgpt.com
    tab tagged via window.name — which, unlike other JS globals, survives
    same-origin navigations. The claim check-and-set runs as one evaluate call
    (atomic on the page's JS thread), so two processes racing for the same
    untagged tab can't both win it."""
    tag = f"canvas-slot-{slot}"
    for pg in context.pages:
        try:
            if pg.evaluate("() => window.name") == tag:
                return pg
        except Exception:  # noqa: BLE001
            continue
    for pg in context.pages:  # claim an untagged chatgpt tab
        try:
            if "chatgpt.com" not in (pg.url or ""):
                continue
            claimed = pg.evaluate(
                "(t) => { if (window.name && window.name.startsWith('canvas-slot-'))"
                " return false; window.name = t; return true; }", tag)
            if claimed:
                return pg
        except Exception:  # noqa: BLE001
            continue
    log(f"opening a new tab for slot {slot} ...")
    pg = context.new_page()
    try:
        pg.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    except Exception:  # noqa: BLE001
        pass
    try:
        pg.evaluate("(t) => { window.name = t; }", tag)
    except Exception:  # noqa: BLE001
        pass
    return pg


def _find_conversation_page(context, conv: str):
    for pg in context.pages:
        try:
            if conv in (pg.url or ""):
                return pg
        except Exception:  # noqa: BLE001
            continue
    return None


_CONV_RE = re.compile(r"chatgpt\.com/c/([0-9a-fA-F-]{8,})")


def _conversation_id(page):
    try:
        m = _CONV_RE.search(page.url or "")
    except Exception:  # noqa: BLE001
        return None
    return m.group(1) if m else None


def _write_meta(path: str, conv: str) -> None:
    if not path or not conv:
        return
    try:
        import json as _json
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump({"conversationId": conv}, fh)
    except OSError:
        pass


def _run(page, message: str, output: str, timeout: int, reference: str = "",
         meta: str = "") -> None:
    page.set_default_timeout(30_000)
    log("opening chatgpt.com ...")
    try:
        page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        fail(f"failed to open chatgpt.com: {exc}")

    composer = _find_composer(page)
    if composer is None:
        fail("could not find the ChatGPT message box (are you logged in?)")

    # The reused tab may still be showing a previous conversation (SPA
    # navigation can restore it). Sending there — or grabbing from there —
    # is how a stale image from the LAST run gets returned as this run's
    # result. Start a fresh chat, and snapshot whatever images are still on
    # screen so the wait loop can refuse to return any of them.
    _start_new_chat_if_needed(page)
    composer = _find_composer(page)
    if composer is None:
        fail("could not find the ChatGPT message box after starting a new chat")
    baseline = _snapshot_images(page)
    if baseline:
        log(f"ignoring {len(baseline)} pre-existing image src(s) from earlier chats")

    if reference:
        _attach_reference(page, reference)

    log("sending prompt ...")
    try:
        composer.scroll_into_view_if_needed(timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    composer.click()
    # insert_text works for both the ProseMirror contenteditable div and a
    # textarea, and targets whatever is focused after the click.
    page.keyboard.insert_text(message)
    time.sleep(0.6)
    if not _submit(page, composer):
        fail("typed the prompt but could not submit it (the send button never "
             "enabled — an attached image may still be uploading)")

    # The SPA moves to /c/<uuid> shortly after the first message lands. That
    # uuid is this generation's stable identity: callers store it as the task
    # id, the wait loop below pins the tab to it, and interrupted jobs can be
    # recovered later with --grab-only --conversation <uuid>.
    conv = None
    conv_deadline = time.time() + 15
    while time.time() < conv_deadline and not conv:
        conv = _conversation_id(page)
        if not conv:
            time.sleep(1.0)
    if conv:
        log(f"conversation: {conv}")
        _write_meta(meta, conv)
    else:
        log("(could not detect the conversation uuid; continuing without it)")

    log(f"waiting up to {timeout}s for the generated image ...")
    src = _wait_for_image(page, timeout, exclude=baseline, conv=conv)
    if not src:
        fail(f"no image was produced within {timeout}s")

    log("downloading image ...")
    data = _download(page, src)
    if not data:
        fail("failed to download the generated image bytes")
    with open(output, "wb") as fh:
        fh.write(data)
    log(f"saved {len(data)} bytes -> {output}")


def _is_textarea(handle) -> bool:
    try:
        return (handle.evaluate("el => el.tagName") or "").lower() == "textarea"
    except Exception:  # noqa: BLE001
        return False


def _find_composer(page):
    # ChatGPT's real input is a contenteditable ProseMirror <div>; there is also
    # a hidden fallback <textarea> we must avoid. Prefer the visible div.
    selectors = [
        "div#prompt-textarea[contenteditable='true']",
        "div.ProseMirror[contenteditable='true']",
        "div[contenteditable='true']",
        "textarea[data-testid='prompt-textarea']",
    ]
    deadline = time.time() + 30
    while time.time() < deadline:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    return loc
            except Exception:  # noqa: BLE001
                continue
        time.sleep(0.5)
    return None


def _start_new_chat_if_needed(page) -> None:
    """If the reused tab is still showing an old conversation, click 'new chat'.
    Best-effort: the baseline snapshot is the hard guarantee; this just keeps the
    new prompt from landing inside (and chaining off) the previous conversation."""
    try:
        has_old = page.evaluate(
            "() => !!document.querySelector('[data-message-author-role]')")
    except Exception:  # noqa: BLE001
        has_old = False
    if not has_old:
        return
    log("previous conversation still on screen; starting a new chat ...")
    for sel in [
        "a[data-testid='create-new-chat-button']",
        "button[data-testid='create-new-chat-button']",
        "a[aria-label*='New chat']",
        "button[aria-label*='New chat']",
        "a[aria-label*='新聊天']",
        "button[aria-label*='新聊天']",
    ]:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                loc.click()
                time.sleep(1.5)
                return
        except Exception:  # noqa: BLE001
            continue
    log("(could not find a new-chat button; relying on the image baseline)")


_FILE_ID_RE = re.compile(r"file[-_][A-Za-z0-9]+")


def _snapshot_images(page):
    """Srcs (and their stable file ids) of every image already on the page.
    Anything in this set existed BEFORE our prompt, so it can never be this
    run's result — signed URLs may be re-issued with different query params,
    which is why the bare file id is recorded too."""
    try:
        srcs = page.evaluate(
            "() => Array.from(document.querySelectorAll('img'))"
            ".map(im => im.currentSrc || im.src || '').filter(Boolean)")
    except Exception:  # noqa: BLE001
        srcs = []
    out = set()
    for s in srcs:
        out.add(s)
        m = _FILE_ID_RE.search(s)
        if m:
            out.add(m.group(0))
    return sorted(out)


def _attach_reference(page, path: str) -> None:
    """Attach a reference image to the composer via the hidden file input, then
    wait for its thumbnail/preview to appear before the prompt is sent."""
    import os as _os
    if not _os.path.exists(path):
        log(f"(reference not found, skipping: {path})")
        return
    log("attaching reference image ...")
    try:
        page.locator("input[type='file']").first.set_input_files(path, timeout=10_000)
    except Exception as exc:  # noqa: BLE001
        log(f"(could not attach reference: {exc})")
        return
    # Wait for the upload preview so the image is bound to the next message.
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            ok = page.evaluate("""() => {
              if (document.querySelector("img[alt*='上传'],img[alt*='Uploaded'],img[alt*='附件']")) return true;
              const imgs = Array.from(document.querySelectorAll('img'))
                .filter(im => { const s = im.currentSrc||im.src||''; return s.startsWith('blob:') || /thumbnail|attach/i.test(s); });
              return imgs.length > 0;
            }""")
            if ok:
                log("reference attached")
                time.sleep(1.0)
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.8)
    log("(reference upload not confirmed; continuing)")
    time.sleep(1.0)


def _submit(page, composer) -> bool:
    """Send the message. Prefer clicking an enabled send button — Enter is ignored
    while an attached image is still uploading — and confirm the composer cleared,
    which only happens on a real send."""
    send_selectors = [
        "button[data-testid='send-button']",
        "button[data-testid='composer-send-button']",
        "button[aria-label*='Send']",
        "button[aria-label*='发送']",
    ]
    deadline = time.time() + 50
    pressed_enter_at = 0.0
    while time.time() < deadline:
        for sel in send_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible() and loc.is_enabled():
                    loc.click()
                    if _composer_cleared(composer):
                        return True
            except Exception:  # noqa: BLE001
                pass
        # Fallback: try Enter every few seconds (harmless while upload pending).
        if time.time() - pressed_enter_at > 6:
            pressed_enter_at = time.time()
            try:
                composer.click()
                page.keyboard.press("Enter")
                if _composer_cleared(composer):
                    return True
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.0)
    return False


def _composer_cleared(composer) -> bool:
    """A successful send empties the composer; poll briefly for that."""
    for _ in range(4):
        try:
            txt = composer.inner_text(timeout=1000)
        except Exception:  # noqa: BLE001
            return False
        if txt is not None and txt.strip() == "":
            return True
        time.sleep(0.5)
    return False


def _maybe_click_send(page) -> None:
    for sel in [
        "button[data-testid='send-button']",
        "button[aria-label*='Send']",
        "button[aria-label*='发送']",
    ]:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_enabled():
                loc.click()
                return
        except Exception:  # noqa: BLE001
            continue


# JS that returns the current best image candidate plus whether generation is
# still streaming. The generated image renders as a large <img> in the ASSISTANT
# reply, served from a stored-file URL (…/backend-api/…content?id=file_…). The
# diffusion model shows progressive intermediate frames first, so we only treat a
# STORED-file image whose src has stopped changing as the final result. We also
# exclude the reference image we uploaded (it lives in the USER message bubble).
_IMAGE_JS = """
(excluded) => {
  const ex = new Set(excluded || []);
  const fileId = (s) => { const m = s.match(/file[-_][A-Za-z0-9]+/); return m ? m[0] : null; };
  const isOld = (s) => ex.has(s) || (fileId(s) && ex.has(fileId(s)));
  const isAvatar = (s) => /auth0\\.com|gravatar|\\/avatars?\\//i.test(s);
  const inUserTurn = (el) => !!el.closest('[data-message-author-role="user"]');
  const stop = document.querySelector(
    "button[data-testid='stop-button'],button[aria-label*='Stop'],button[aria-label*='停止']");
  const big = Array.from(document.querySelectorAll('img')).filter(im => {
    const s = im.currentSrc || im.src || '';
    if (!s || isAvatar(s) || inUserTurn(im) || isOld(s)) return false;
    return im.naturalWidth >= 256 && im.naturalHeight >= 256;
  });
  const stored = big.filter(im =>
    /estuary|oaiusercontent|backend-api\\/[^?]*content/i.test(im.currentSrc || im.src || ''));
  const pick = (stored.length ? stored : big).slice(-1)[0];
  return {
    src: pick ? (pick.currentSrc || pick.src) : null,
    stored: stored.length > 0,
    generating: !!stop,
  };
}
"""


def _wait_for_image(page, timeout: int, require_settle: bool = True, exclude=None,
                    conv=None):
    """Return the src of the FINAL generated image. Waits until a stored-file
    image's src has been stable (no progressive change) for a few seconds.
    Srcs/file-ids in `exclude` (images that pre-date this run's prompt) are
    never returned — that's what stops a leftover image from a previous
    conversation being grabbed as this run's result. When `conv` is known the
    tab is pinned to that conversation: if anything navigates it away (the
    user clicking around, another chat), we go back before grabbing anything."""
    deadline = time.time() + timeout
    last_src = None
    stable_since = 0.0
    latest = None
    while time.time() < deadline:
        if conv and conv not in (page.url or ""):
            log("tab left our conversation; navigating back ...")
            try:
                page.goto(f"https://chatgpt.com/c/{conv}",
                          wait_until="domcontentloaded")
                time.sleep(2.0)
            except Exception:  # noqa: BLE001
                pass
            last_src, stable_since = None, 0.0  # re-settle after reload
        try:
            info = page.evaluate(_IMAGE_JS, list(exclude or []))
        except Exception:  # noqa: BLE001
            info = None
        if info and info.get("src"):
            src = info["src"]
            latest = src
            if src != last_src:
                last_src = src
                stable_since = time.time()
            held = time.time() - stable_since
            if not require_settle:
                # grab-only: the image is already on the page; take the stored one.
                if info.get("stored"):
                    return src
            else:
                # final = stored + stable >=5s, or any big image stable >=10s and
                # not actively streaming (covers non-estuary finals).
                if info.get("stored") and held >= 5.0:
                    return src
                if held >= 10.0 and not info.get("generating"):
                    return src
        time.sleep(1.5)
    return latest  # best effort if it never clearly settled


def _grab(page, output: str, timeout: int) -> None:
    """Re-grab the latest image already on the current ChatGPT page (no prompt).
    Used by the popup's manual refresh when auto-detection caught a mid-state."""
    log("grabbing latest image from the current page ...")
    src = _wait_for_image(page, min(timeout, 20), require_settle=False)
    if not src:
        fail("no image found on the current ChatGPT page to grab")
    data = _download(page, src)
    if not data:
        fail("failed to download the image bytes")
    with open(output, "wb") as fh:
        fh.write(data)
    log(f"grabbed {len(data)} bytes -> {output}")


def _download(page, src: str):
    if src.startswith("data:image"):
        try:
            return base64.b64decode(src.split(",", 1)[1])
        except Exception:  # noqa: BLE001
            return None
    # Fetch through the page session (carries auth cookies).
    try:
        resp = page.request.get(src)
        if resp.ok:
            return resp.body()
    except Exception:  # noqa: BLE001
        pass
    # Fallback: in-page fetch -> base64.
    try:
        b64 = page.evaluate(
            """async (url) => {
                const r = await fetch(url);
                const b = await r.blob();
                return await new Promise((res) => {
                  const fr = new FileReader();
                  fr.onload = () => res(fr.result.split(',')[1]);
                  fr.readAsDataURL(b);
                });
            }""",
            src,
        )
        if b64:
            return base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        pass
    return None


if __name__ == "__main__":
    main()

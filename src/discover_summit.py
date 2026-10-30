"""Run once; zip and send back data\\debug_summit.   python src\\discover_summit.py

Clicks "Get Docs" on ONE foreclosure row and records what happens (download, popup, modal,
network calls, websocket traffic). One click, nothing more.
"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://newcivilfilings.summitoh.net/"
OUT = Path("data") / "debug_summit2"
OUT.mkdir(parents=True, exist_ok=True)
log, frames, new_pages, dialogs = [], [], [], []


def on_resp(r):
    e = {"method": r.request.method, "url": r.url, "status": r.status, "type": r.headers.get("content-type", "")}
    try:
        if any(k in e["type"] for k in ("json", "pdf", "octet", "zip", "msword", "officedocument")) \
                or r.request.resource_type in ("xhr", "fetch", "document"):
            body = r.body()
            e["bytes"] = len(body)
            ext = ".pdf" if "pdf" in e["type"] else ".bin" if "octet" in e["type"] else ".txt"
            name = f"resp_{len(log):03d}{ext}"
            (OUT / name).write_bytes(body[:5_000_000])
            e["file"] = name
    except Exception as ex:
        e["note"] = str(ex)[:100]
    log.append(e)


def on_ws(ws):
    ws.on("framesent", lambda p: frames.append({"dir": "sent", "data": str(p)[:600]}))
    ws.on("framereceived", lambda p: frames.append({"dir": "recv", "data": str(p)[:600]}))


def snap(page, name):
    try:
        (OUT / f"{name}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
        (OUT / f"{name}.txt").write_text(page.inner_text("body"), encoding="utf-8")
    except Exception as e:
        print("snapshot failed:", e)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000}, accept_downloads=True)
    page = ctx.new_page()
    page.on("response", on_resp)
    page.on("websocket", on_ws)
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    ctx.on("page", lambda pg: new_pages.append(pg.url))
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(6000)

    result = {}
    # a data row: has a Foreclosure category cell and a Get Docs button (header row has neither)
    rows = page.locator("tbody tr").filter(has_text="Foreclosure").filter(has=page.get_by_role("button", name="Get Docs"))
    result["foreclosure_rows"] = rows.count()
    if rows.count():
        row = rows.first
        result["row_text"] = row.inner_text()
        btn = row.get_by_role("button", name="Get Docs")
        n_before = len(frames)
        try:
            with page.expect_download(timeout=15000) as dl:
                btn.click()
            d = dl.value
            d.save_as(str(OUT / ("doc_" + d.suggested_filename)))
            result["download"] = d.suggested_filename
        except Exception as e:
            result["no_download"] = str(e)[:120]
            page.wait_for_timeout(5000)
        page.wait_for_timeout(3000)
        result["new_pages"] = new_pages[:]
        result["dialogs"] = dialogs[:]
        result["ws_frames_after_click"] = len(frames) - n_before
        snap(page, "04_after_click")
        for pg in ctx.pages[1:]:
            try:
                pg.wait_for_timeout(2000)
                snap(pg, "05_popup")
            except Exception:
                pass
    print(json.dumps(result, indent=2))
    (OUT / "network_log.json").write_text(json.dumps(
        {"result": result, "responses": log, "ws_frames": frames[-80:]}, indent=2), encoding="utf-8")
    browser.close()
print(f"Done. Zip and send: {OUT.resolve()}")

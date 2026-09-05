"""Record a MANUAL pass through the cita form for comparison with the automated one.

Attaches to the already-running Brave over CDP and logs every document-level
request/response the browser makes while YOU click through the site by hand.
Nothing is automated here: the script only watches.

    python record.py            # then do the flow manually in the browser window

Stop with Ctrl+C. Output goes to debug/manual_<timestamp>.log (gitignored).
"""

import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

import flow

BASE_DIR = Path(__file__).parent
DEBUG_DIR = BASE_DIR / "debug"

INTERESTING_HEADERS = ("referer", "origin", "content-type", "user-agent",
                       "sec-fetch-site", "sec-fetch-mode", "sec-fetch-user", "sec-gpc")


def main() -> int:
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = DEBUG_DIR / f"manual_{stamp}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger("record")

    cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

    with sync_playwright() as pw:
        browser = flow.get_browser(pw, cfg["browser"])
        context = browser.contexts[0]

        def on_request(req) -> None:
            if req.resource_type != "document":
                return
            log.info("--> %s %s", req.method, req.url)
            headers = req.headers
            for key in INTERESTING_HEADERS:
                if key in headers:
                    log.info("    %s: %s", key, headers[key][:200])
            if req.method == "POST":
                body = req.post_data or ""
                log.info("    body (%d bytes):", len(body))
                for line in body.splitlines():
                    log.info("    | %s", line[:300])

        seq = {"n": 0}

        def on_response(resp) -> None:
            if resp.request.resource_type != "document":
                return
            log.info("<-- %s %s", resp.status, resp.url)
            try:
                body = resp.text()
            except PWError:
                return
            if "Request Rejected" in body[:2000]:
                log.info("    !! WAF REJECTION")
            seq["n"] += 1
            name = resp.url.rstrip("/").rsplit("/", 1)[-1].split("?")[0][:40] or "page"
            out = DEBUG_DIR / f"manual_{stamp}_{seq['n']:02d}_{name}.html"
            try:
                out.write_text(body, encoding="utf-8")
            except OSError:
                pass

        context.on("request", on_request)
        context.on("response", on_response)
        # Attach per-page too: context-level events can be missed for pages that
        # already existed when another CDP client touched the browser.
        for existing in context.pages:
            existing.on("request", on_request)
            existing.on("response", on_response)
        context.on("page", lambda p: (p.on("request", on_request), p.on("response", on_response)))

        on_site = [p for p in context.pages if "administracionelectronica" in p.url]
        if on_site:
            log.info("Session already in progress at: %s", on_site[0].url)
            log.info("NOT navigating - continue the flow from where you are.")
        else:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(cfg["start_url"], wait_until="domcontentloaded", timeout=45000)
            except PWError as exc:
                log.warning("Could not open start URL automatically: %s", exc)

        log.info("RECORDING. Do the flow manually in the browser. Ctrl+C to stop.")
        log.info("Log file: %s", log_path)
        # The sync API only dispatches events while the client is inside a
        # Playwright call. A bare time.sleep() would deafen this recorder, so the
        # idle loop must block inside wait_for_timeout instead.
        try:
            while True:
                pages = context.pages
                if pages:
                    try:
                        pages[0].wait_for_timeout(500)
                        continue
                    except PWError:
                        pass
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("Recording stopped. Log saved to %s", log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

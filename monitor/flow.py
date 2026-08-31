"""Site flow: CDP attach to a real Chrome, walk the cita form, classify outcomes.

Key differences from the legacy checkers:
- attaches to the user's real Chrome via CDP (real fingerprint, home IP);
- positive detection: every step declares expected selectors for the next screen,
  a missing selector triggers classify_page() instead of an optimistic assumption;
- resilient office selector list covering both #idSede (monitor-lite) and #sede (legacy).
"""

import datetime as dt
import json
import logging
import random
import re
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from playwright.sync_api import Browser, Page, Playwright
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

logger = logging.getLogger("monitor.flow")

DEBUG_DIR = Path(__file__).with_name("debug")

NO_SLOTS_MARKERS = [
    "no hay citas disponibles",
    "no existen citas disponibles",
    "en este momento no hay citas",
    "no hay horas disponibles",
]
BLOCK_MARKERS = [
    "acceso no permitido",
    "sesion ha caducado",
    "sesión ha caducado",
    "servicio no disponible",
    "demasiadas peticiones",
    "too many requests",
    # F5 ASM has two rejection faces: the "Request Rejected" page and this
    # JavaScript challenge. Both carry a support ID.
    "please enable javascript to view the page content",
    "the requested url was rejected",
    "your support id is",
]
DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})(?:\s*[-–]?\s*(\d{1,2}):(\d{2}))?")

OFFICE_SELECTORS = [
    "#idSede",
    "select[name='idSede']",
    "#sede",
    "select[name='sede']",
]

# Brave first: it is the user's daily browser, and Chromium-based, so CDP works identically.
BROWSER_CANDIDATES = [
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Users\{user}\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Users\{user}\AppData\Local\Google\Chrome\Application\chrome.exe",
]

Status = Literal["slots", "no_slots", "not_offered", "blocked", "error"]


@dataclass
class CheckResult:
    status: Status
    dates: list[tuple[dt.date, str]] = field(default_factory=list)
    detail: str = ""
    screenshot_path: Optional[Path] = None


class ChromeUnavailable(RuntimeError):
    """CDP endpoint could not be reached even after auto-launch."""


# ─────────────────────────── browser lifecycle ───────────────────────────


def _find_browser(cfg_path: Optional[str]) -> Optional[str]:
    import os

    if cfg_path and Path(cfg_path).exists():
        return cfg_path
    user = os.environ.get("USERNAME", "")
    for candidate in BROWSER_CANDIDATES:
        path = candidate.format(user=user)
        if Path(path).exists():
            return path
    return None


def _cdp_alive(cdp_url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=timeout):
            return True
    except OSError:
        return False


def launch_browser(browser_cfg: dict) -> None:
    """Start Brave/Chrome with a remote debugging port and a dedicated profile."""
    binary = _find_browser(browser_cfg.get("browser_path"))
    if binary is None:
        raise ChromeUnavailable("brave.exe/chrome.exe not found; set browser.browser_path in config")
    port = browser_cfg["cdp_url"].rsplit(":", 1)[-1]
    user_data_dir = browser_cfg["user_data_dir"]
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    logger.info("Launching %s (port %s, profile %s)", Path(binary).name, port, user_data_dir)
    subprocess.Popen(
        [
            binary,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def get_browser(pw: Playwright, browser_cfg: dict) -> Browser:
    """Attach to a running Brave/Chrome via CDP, auto-launching it if allowed.

    Raises:
        ChromeUnavailable: endpoint unreachable after launch attempts.
    """
    cdp_url = browser_cfg["cdp_url"]
    if not _cdp_alive(cdp_url):
        if not browser_cfg.get("auto_launch", True):
            raise ChromeUnavailable(f"CDP endpoint {cdp_url} is down and auto_launch is off")
        launch_browser(browser_cfg)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _cdp_alive(cdp_url):
                break
            time.sleep(0.5)
        else:
            raise ChromeUnavailable(f"CDP endpoint {cdp_url} did not come up after launch")
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url, timeout=5000)
    except PWError as exc:
        raise ChromeUnavailable(f"connect_over_cdp failed: {exc}") from exc
    logger.info("Attached to Chrome via CDP (%s)", cdp_url)
    return browser


def new_tab(browser: Browser) -> Page:
    if not browser.contexts:
        raise ChromeUnavailable("Browser has no contexts (closed?)")
    return browser.contexts[0].new_page()


# ─────────────────────────── page helpers ───────────────────────────


def page_text(page: Page) -> str:
    try:
        return (page.inner_text("body") or "").lower()
    except PWError:
        return ""


def dump(page: Page, tag: str) -> Optional[Path]:
    """Dump HTML + screenshot into debug/. Returns the screenshot path."""
    DEBUG_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    png = DEBUG_DIR / f"{stamp}_{tag}.png"
    try:
        (DEBUG_DIR / f"{stamp}_{tag}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(png), full_page=True)
        return png
    except PWError:
        return None


def pause(delay_ms: list[int], factor: float = 1.0) -> None:
    time.sleep(random.uniform(delay_ms[0], delay_ms[1]) / 1000 * factor)


_mouse: dict[str, tuple[float, float]] = {"pos": (400.0, 300.0)}


def human_move_to(page: Page, loc) -> Optional[tuple[float, float]]:
    """Move the cursor along a curved path to a random point inside the element.

    Playwright's mouse API drives CDP input, so these are trusted events with a
    real movement stream - which is what the site's behavioural WAF looks for.
    Teleporting straight to an element centre is the automation tell.
    """
    try:
        box = loc.bounding_box()
    except PWError:
        return None
    if not box:
        return None
    tx = box["x"] + box["width"] * random.uniform(0.25, 0.75)
    ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    sx, sy = _mouse["pos"]
    cx = (sx + tx) / 2 + random.uniform(-140, 140)
    cy = (sy + ty) / 2 + random.uniform(-100, 100)
    steps = random.randint(10, 22)
    for i in range(1, steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t ** 2 * tx
        y = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t ** 2 * ty
        try:
            page.mouse.move(x, y)
        except PWError:
            return None
        if random.random() < 0.2:
            page.wait_for_timeout(random.randint(8, 45))
    _mouse["pos"] = (tx, ty)
    return tx, ty


def human_wander(page: Page) -> None:
    """A few idle moves and a small scroll, the way a human settles on a page."""
    try:
        for _ in range(random.randint(2, 4)):
            x = random.uniform(150, 1000)
            y = random.uniform(120, 700)
            page.mouse.move(x, y, steps=random.randint(5, 14))
            _mouse["pos"] = (x, y)
            page.wait_for_timeout(random.randint(60, 260))
        if random.random() < 0.7:
            page.mouse.wheel(0, random.randint(80, 320))
            page.wait_for_timeout(random.randint(150, 500))
    except PWError:
        pass


def click_any(page: Page, selectors: list[str], timeout: int = 6000) -> Optional[str]:
    """Click the first existing visible element, approaching it like a human."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.scroll_into_view_if_needed(timeout=2000)
                # move like a human, but let Playwright deliver the click: a raw
                # mouse.click at coordinates hits whatever is physically on top,
                # and this site floats a cookie banner over the buttons. Both
                # paths dispatch trusted CDP input, so realism is unaffected.
                human_move_to(page, loc)
                page.wait_for_timeout(random.randint(60, 220))
                loc.click(timeout=timeout)
                page.wait_for_load_state("domcontentloaded")
                return sel
        except (PWError, PWTimeout):
            continue
    return None


def select_option_containing(page: Page, selectors: list[str], needle: str) -> Optional[str]:
    """Select an <option> matching needle: exact match wins over substring.

    Exact-first matters: the country list contains both "RUSIA" and
    "BIELORRUSIA O BELARUS", and the latter comes first while containing "RUSIA"
    as a substring. Plain substring matching silently picks Belarus.
    """
    needle_l = needle.strip().lower()
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.count():
                continue
            options = loc.locator("option")
            count = options.count()
            labels = [(options.nth(i).inner_text() or "").strip() for i in range(count)]

            exact = [i for i, label in enumerate(labels) if label.lower() == needle_l]
            partial = [i for i, label in enumerate(labels) if needle_l in label.lower()]
            index = exact[0] if exact else (partial[0] if partial else None)

            if index is None:
                raise LookupError(
                    f"No option matching '{needle}' in {sel}. Available: {labels[:40]}"
                )
            if not exact and len(partial) > 1:
                logger.warning(
                    "Ambiguous match for '%s' in %s: %s -> using '%s'",
                    needle, sel, [labels[i] for i in partial[:5]], labels[index],
                )
            # approach the control first: the change event alone looks synthetic
            human_move_to(page, loc)
            page.wait_for_timeout(random.randint(120, 400))
            chosen = options.nth(index).get_attribute("value")
            loc.select_option(value=chosen)
            # verify it stuck: a late AJAX repopulation can wipe the choice
            page.wait_for_timeout(250)
            try:
                if loc.input_value() != chosen:
                    logger.warning("Selection of '%s' in %s was reset, retrying", labels[index], sel)
                    page.wait_for_timeout(1200)
                    loc.select_option(value=chosen)
                    if loc.input_value() != chosen:
                        raise LookupError(f"could not keep '{labels[index]}' selected in {sel}")
            except PWError:
                pass
            return labels[index]
        except LookupError:
            raise
        except (PWError, PWTimeout):
            continue
    return None


def type_into(page: Page, selectors: list[str], value: str, typing_delay_ms: list[int]) -> bool:
    """Move to the field, click it, and type with a varying rhythm.

    A constant inter-key delay is as machine-like as no delay at all, so each
    keystroke gets its own interval and the odd longer pause mid-word.
    """
    if not value:
        return False
    lo, hi = typing_delay_ms
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                point = human_move_to(page, loc)
                page.wait_for_timeout(random.randint(80, 260))
                if point:
                    page.mouse.click(point[0], point[1])
                else:
                    loc.click()
                for char in value:
                    page.keyboard.type(char, delay=random.randint(lo, hi))
                    if random.random() < 0.12:
                        page.wait_for_timeout(random.randint(180, 600))
                return True
        except (PWError, PWTimeout):
            continue
    return False


def wait_options_settled(page: Page, selector: str, timeout: int = 12000) -> int:
    """Wait until a <select> stops being repopulated, return its option count.

    Choosing an office fires cargaTramites(), which rebuilds the tramite list
    over AJAX. Selecting a tramite before that response lands gets the choice
    silently wiped, and the form then submits with no tramite at all.
    """
    deadline = time.monotonic() + timeout / 1000
    previous, stable_since = -1, 0.0
    while time.monotonic() < deadline:
        try:
            count = page.locator(selector).first.locator("option").count()
        except PWError:
            count = -1
        if count == previous and count > 1:
            if time.monotonic() - stable_since >= 0.8:
                return count
        else:
            previous, stable_since = count, time.monotonic()
        page.wait_for_timeout(200)
    return previous


def wait_any(page: Page, selectors: list[str], timeout: int = 10000) -> Optional[str]:
    """Wait until any of the expected selectors appears; None on timeout."""
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for sel in selectors:
            try:
                if page.locator(sel).first.count():
                    return sel
            except PWError:
                pass
        time.sleep(0.25)
    return None


def extract_slots(text: str) -> list[tuple[dt.date, str]]:
    """Extract dd/mm/yyyy (+optional hh:mm) dates from page text."""
    out: list[tuple[dt.date, str]] = []
    for m in DATE_RE.finditer(text):
        d, mo, y, hh, mm = m.groups()
        try:
            day = dt.date(int(y), int(mo), int(d))
        except ValueError:
            continue
        if day < dt.date.today():
            continue
        out.append((day, f"{hh}:{mm}" if hh else ""))
    seen: set[tuple[dt.date, str]] = set()
    uniq: list[tuple[dt.date, str]] = []
    for item in out:
        if item not in seen:
            seen.add(item)
            uniq.append(item)
    return uniq


def filter_slots(
    slots: list[tuple[dt.date, str]],
    min_date: Optional[dt.date],
    max_date: Optional[dt.date],
) -> list[tuple[dt.date, str]]:
    res = []
    for day, hhmm in slots:
        if min_date and day < min_date:
            continue
        if max_date and day > max_date:
            continue
        res.append((day, hhmm))
    return res


# ─────────────────────────── outcome classification ───────────────────────────


def classify_page(page: Page, http_status: Optional[int] = None) -> Status:
    """Classify an unexpected screen. Never returns 'slots'."""
    if http_status is not None and http_status >= 400:
        return "blocked"
    txt = page_text(page)
    if any(m in txt for m in BLOCK_MARKERS):
        return "blocked"
    try:
        has_controls = page.locator("form, select, input").count() > 0
    except PWError:
        has_controls = False
    if not has_controls:
        # observed soft-block signature: page served without any form controls
        return "blocked"
    if any(m in txt for m in NO_SLOTS_MARKERS):
        return "no_slots"
    return "error"


def log_form_state(page: Page) -> None:
    """Verify mode: log every form field name/value right before submitting."""
    try:
        fields = page.evaluate(
            """() => Array.from(document.querySelectorAll('input, select')).map(el => ({
                tag: el.tagName, type: el.type || '', name: el.name || '', id: el.id || '',
                value: (el.type === 'checkbox' || el.type === 'radio')
                    ? String(el.checked) : String(el.value || '').slice(0, 60)
            }))"""
        )
        for f in fields:
            if f["type"] == "hidden" and not f["value"]:
                continue
            logger.info("  field %s[%s] name=%s id=%s value=%r",
                        f["tag"], f["type"], f["name"], f["id"], f["value"])
    except PWError as exc:
        logger.warning("log_form_state failed: %s", exc)


def describe_page(page: Page) -> None:
    """Verify mode: log URL, title, all selects with options, all buttons."""
    try:
        # the page often navigates again right after a click; let it settle first
        page.wait_for_load_state("domcontentloaded", timeout=8000)
        page.wait_for_timeout(400)
        logger.info("URL: %s | title: %s", page.url, page.title())
        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            ident = sel.get_attribute("id") or sel.get_attribute("name") or f"select#{i}"
            opts = sel.locator("option")
            labels = [opts.nth(j).inner_text().strip() for j in range(min(opts.count(), 40))]
            logger.info("  select %s: %s", ident, json.dumps(labels, ensure_ascii=False))
        buttons = page.locator("input[type='submit'], input[type='button'], button")
        for i in range(min(buttons.count(), 20)):
            btn = buttons.nth(i)
            label = btn.get_attribute("value") or btn.inner_text().strip()
            logger.info("  button: %s (id=%s)", label, btn.get_attribute("id"))
    except PWError as exc:
        logger.warning("describe_page failed: %s", exc)


# ─────────────────────────── the walk ───────────────────────────


def discover_offices(page: Page, cfg: dict,
                     known: Optional[dict[str, bool]] = None) -> list[tuple[str, bool]]:
    """List every office and whether it offers the configured tramite.

    Selecting an office fires cargaTramites() and repopulates the tramite list
    over AJAX, so the survey runs inside one page load instead of one full form
    pass per office. Offices already in `known` are skipped, which keeps a resumed
    survey short - a long uninterrupted run of select changes is what got the
    session rejected before.

    Returns:
        (office label, offers the tramite) for every office except "Cualquier oficina".
    """
    known = known or {}
    needle = cfg["tramite_contains"].lower()
    page.goto(cfg["start_url"], wait_until="domcontentloaded", timeout=45000)
    if not wait_any(page, ["#form", "select[name='form']", "select"], timeout=20000):
        raise LookupError(f"province select never appeared (url={page.url})")
    human_wander(page)
    province = select_option_containing(page, ["#form", "select[name='form']", "select"],
                                        cfg["province"])
    if not province:
        raise LookupError("province select found but could not be set")
    logger.info("Province selected: %s", province)
    pause(cfg["check"]["action_delay_ms"])
    click_any(page, ["#btnAceptar", "input[value='Aceptar']", "button:has-text('Aceptar')"])
    # a cold browser start plus the province redirect can exceed the default wait
    if not wait_any(page, ["#sede", "select[name='sede']"], timeout=30000):
        verdict = classify_page(page)
        dump(page, "discovery_no_sede")
        raise LookupError(
            f"office select not found after province step "
            f"(verdict={verdict}, url={page.url}, title={page.title()!r})"
        )

    sede = page.locator("#sede").first
    options = sede.locator("option")
    labels = [(options.nth(i).inner_text() or "").strip() for i in range(options.count())]
    values = [options.nth(i).get_attribute("value") for i in range(options.count())]
    logger.info("Surveying %d offices for '%s'", len(labels), cfg["tramite_contains"])

    pending = [(label, value) for label, value in zip(labels, values)
               if label and not label.lower().startswith("cualquier") and label not in known]
    logger.info("%d already known, %d to check", len(known), len(pending))

    results: list[tuple[str, bool]] = []
    for position, (label, value) in enumerate(pending):
        human_move_to(page, sede)
        page.wait_for_timeout(random.randint(300, 900))
        sede.select_option(value=value)
        # unhurried, uneven pacing: a steady drumbeat of select changes is what
        # the behavioural filter objects to
        page.wait_for_timeout(random.randint(2500, 6000))
        try:
            tramites = page.locator("#tramiteGrupo\\[0\\]").first.locator("option")
            texts = [(tramites.nth(i).inner_text() or "").lower() for i in range(tramites.count())]
        except PWError:
            texts = []
        offers = any(needle in text for text in texts)
        results.append((label, offers))
        logger.info("  [%s] %s (%d tramites)", "YES" if offers else " no", label[:70], len(texts))
        if random.random() < 0.4:
            human_wander(page)
        if position and position % 5 == 0:
            rest = random.randint(15, 35)
            logger.info("  ... short break (%d s)", rest)
            time.sleep(rest)
    return results


def walk_to_dates(page: Page, cfg: dict, verify: bool = False,
                  office: Optional[str] = None) -> CheckResult:
    """Walk the form from the start URL to the date screen.

    Every step verifies that the next screen actually appeared; anything
    unexpected goes through classify_page() — never an optimistic 'slots'.

    Args:
        office: office to select, defaults to cfg["office"]. Whether
            "Cualquier oficina" really covers every branch is unverified, so the
            caller may sweep specific offices instead.
    """
    office = office or cfg["office"]
    check = cfg["check"]
    delay = check["action_delay_ms"]
    factor = 3.0 if verify else 1.0
    applicant = cfg["applicant"]

    if verify:
        def _log_request(req) -> None:
            if req.method == "POST" and req.resource_type == "document":
                logger.info("POST %s", req.url)
                try:
                    for key, value in sorted(req.all_headers().items()):
                        logger.info("  hdr %s: %s", key, value[:180])
                except PWError:
                    pass
                for line in (req.post_data or "").splitlines():
                    logger.info("  | %s", line[:300])

        def _log_response(resp) -> None:
            if resp.request.resource_type == "document":
                logger.info("<- %s %s", resp.status, resp.url)

        page.on("request", _log_request)
        page.on("response", _log_response)

    def step_pause() -> None:
        pause(delay, factor)

    def unexpected(tag: str, status_code: Optional[int] = None) -> CheckResult:
        status = classify_page(page, status_code)
        detail = f"step={tag} classified={status} url={page.url}"
        if status == "no_slots":
            # ordinary outcome reached by a slightly different route - not worth
            # a warning or a disk dump on every cycle of a long-running monitor
            logger.debug("No slots (via %s)", tag)
            return CheckResult(status="no_slots", detail=detail)
        shot = dump(page, tag)
        logger.warning("Unexpected screen: %s", detail)
        return CheckResult(status=status, detail=detail, screenshot_path=shot)

    # 0. Entry
    resp = page.goto(cfg["start_url"], wait_until="domcontentloaded", timeout=45000)
    status_code = resp.status if resp else None
    logger.info("Entry loaded: %s (HTTP %s)", page.url, status_code)
    human_wander(page)
    if verify:
        describe_page(page)
    if not wait_any(page, ["#form", "select"], timeout=10000):
        return unexpected("00_entry", status_code)

    # 1. Province
    step_pause()
    label = select_option_containing(page, ["#form", "select[name='form']", "select"], cfg["province"])
    logger.info("Province selected: %s", label)
    step_pause()
    click_any(page, ["#btnAceptar", "input[value='Aceptar']", "button:has-text('Aceptar')"])
    if verify:
        describe_page(page)
        dump(page, "01_provincia")
    if not wait_any(page, ["#tramiteGrupo\\[0\\]", "select[name='tramiteGrupo[0]']", "select"]):
        return unexpected("01_provincia")
    human_wander(page)

    # 2a. Office FIRST — #sede carries onchange="cargaTramites()", which repopulates
    # the tramite list. Picking a tramite without firing it submits an inconsistent
    # sede/tramite pair and the WAF answers with "Request Rejected".
    step_pause()
    try:
        office_label = select_option_containing(page, OFFICE_SELECTORS, office)
        logger.info("Office selected: %s", office_label)
    except LookupError as exc:
        logger.warning("Office option not found on tramite page: %s", exc)
    # let cargaTramites() finish repopulating the tramite select
    page.wait_for_timeout(random.randint(1200, 2200))

    # the office choice triggers cargaTramites(); selecting a tramite before that
    # AJAX lands gets the choice wiped and submits an empty tramite
    settled = wait_options_settled(page, r"#tramiteGrupo\[0\]")
    logger.debug("Tramite list settled at %d options", settled)

    # 2b. Tramite. Selecting an office repopulates this list, and offices differ
    # in what they offer - e.g. Ciudad Lineal only does CARTA DE INVITACIÓN. An
    # office without our tramite is a normal outcome, not a failure.
    step_pause()
    try:
        label = select_option_containing(
            page,
            ["#tramiteGrupo\\[0\\]", "select[name='tramiteGrupo[0]']", "select"],
            cfg["tramite_contains"],
        )
    except LookupError as exc:
        logger.info("Tramite not offered at %s", office)
        return CheckResult(status="not_offered", detail=str(exc)[:300])
    logger.info("Tramite selected: %s", label)
    step_pause()
    click_any(page, ["#btnAceptar", "input[value='Aceptar']", "button:has-text('Aceptar')"])
    if verify:
        describe_page(page)
        dump(page, "02_tramite")
    if not wait_any(page, ["#btnEntrar", "input[value='Entrar']", "button:has-text('Entrar')"]):
        return unexpected("02_tramite")
    human_wander(page)

    # 3. Info page -> Entrar
    step_pause()
    click_any(page, ["#btnEntrar", "input[value='Entrar']", "button:has-text('Entrar')"])
    if verify:
        describe_page(page)
        dump(page, "03_entrar")
    if not wait_any(page, ["#txtIdCitado", "input[name='txtIdCitado']"]):
        return unexpected("03_entrar")
    human_wander(page)

    # 4. Applicant data
    doc_map = {
        "nie": ["#rdbTipoDocNie", "input[value='N']"],
        "pasaporte": ["#rdbTipoDocPas", "input[value='P']"],
        "dni": ["#rdbTipoDocDni", "input[value='D']"],
    }
    step_pause()
    click_any(page, doc_map.get(applicant["doc_type"].lower(), doc_map["nie"]), timeout=2500)
    step_pause()
    type_into(page, ["#txtIdCitado", "input[name='txtIdCitado']"],
              applicant["doc_number"], check["typing_delay_ms"])
    step_pause()
    type_into(page, ["#txtDesCitado", "input[name='txtDesCitado']"],
              applicant["full_name"], check["typing_delay_ms"])
    step_pause()
    type_into(page, ["#txtAnnoCitado", "input[name='txtAnnoCitado']"],
              str(applicant.get("birth_year", "") or ""), check["typing_delay_ms"])
    try:
        country = select_option_containing(page, ["#txtPaisNac", "select[name='txtPaisNac']"],
                                           applicant.get("country", ""))
        logger.info("Country selected: %s", country)
    except LookupError as exc:
        logger.info("Country select skipped: %s", exc)
    if verify:
        dump(page, "04a_datos_filled")
        log_form_state(page)
    step_pause()
    click_any(page, ["#btnEnviar", "input[value='Aceptar']", "button:has-text('Aceptar')"])
    if verify:
        describe_page(page)
        dump(page, "04b_datos_submitted")

    txt = page_text(page)
    if any(b in txt for b in BLOCK_MARKERS):
        return CheckResult(status="blocked", detail=txt[:400], screenshot_path=dump(page, "04_blocked"))

    # 5. Solicitar Cita
    step_pause()
    click_any(page, ["#btnEnviar", "input[value='Solicitar Cita']", "button:has-text('Solicitar')"])
    if verify:
        describe_page(page)
        dump(page, "05_solicitar")

    txt = page_text(page)
    if any(m in txt for m in NO_SLOTS_MARKERS):
        return CheckResult(status="no_slots")

    # After "Solicitar Cita" the next screen takes a moment to arrive. Deciding
    # what it is too early makes the contact form invisible and the walk then
    # falls through to the dates loop on a page that has not loaded yet.
    settle_deadline = time.monotonic() + 10
    while time.monotonic() < settle_deadline:
        if page.locator("#txtTelefonoCitado").count():
            break
        if any(m in page_text(page) for m in NO_SLOTS_MARKERS):
            break
        page.wait_for_timeout(300)

    # 5b. "Paso 2 de 5" - contact details. This screen only appears when the
    # request got past availability, so reaching it is a good sign. The second
    # email field carries class="noPaste", hence typing rather than filling.
    if page.locator("#txtTelefonoCitado").count():
        logger.info("Contact form reached (paso 2)")
        phone = str(applicant.get("phone", "") or "")
        email = str(applicant.get("email", "") or "")
        if not phone or not email:
            shot = dump(page, "05b_contact_missing_data")
            return CheckResult(
                status="error",
                detail="contact form requires applicant.phone and applicant.email in config",
                screenshot_path=shot,
            )
        step_pause()
        type_into(page, ["#txtTelefonoCitado", "input[name='txtTelefonoCitado']"],
                  phone, check["typing_delay_ms"])
        step_pause()
        type_into(page, ["#emailUNO", "input[name='txtMailCitado']"],
                  email, check["typing_delay_ms"])
        step_pause()
        type_into(page, ["#emailDOS", "input[name='emailDOS']"],
                  email, check["typing_delay_ms"])
        if verify:
            dump(page, "05b_contact_filled")
        step_pause()
        click_any(page, ["#btnSiguiente", "input[value='Siguiente']",
                         "button:has-text('Siguiente')"])
        if verify:
            describe_page(page)
            dump(page, "05c_contact_submitted")
        txt = page_text(page)
        if any(m in txt for m in NO_SLOTS_MARKERS):
            return CheckResult(status="no_slots")
        if any(b in txt for b in BLOCK_MARKERS):
            return CheckResult(status="blocked", detail=txt[:400],
                               screenshot_path=dump(page, "05c_blocked"))
        human_wander(page)

    # 6. Office ("Cualquier oficina" if present)
    try:
        label = select_option_containing(page, OFFICE_SELECTORS, office)
        if label:
            logger.info("Office selected: %s", label)
            step_pause()
            click_any(page, ["#btnSiguiente", "input[value='Siguiente']",
                             "button:has-text('Siguiente')"])
    except LookupError as exc:
        logger.warning("Office option not found, continuing: %s", exc)
    if verify:
        describe_page(page)
        dump(page, "06_oficina")

    # 7. Date screen: press Buscar several times (pool serves random subsets)
    retries = 1 if verify else max(1, int(check["buscar_retries"]) + random.randint(-1, 1))
    best: list[tuple[dt.date, str]] = []
    for attempt in range(1, retries + 1):
        txt = page_text(page)
        if any(m in txt for m in NO_SLOTS_MARKERS):
            return CheckResult(status="no_slots")
        slots = extract_slots(txt)
        if slots:
            best = sorted(set(best + slots))
        if attempt < retries:
            clicked = click_any(
                page,
                ["#btnSiguiente", "input[value='Buscar']",
                 "button:has-text('Buscar')", "input[value='Siguiente']"],
                timeout=4000,
            )
            if not clicked:
                break
            page.wait_for_timeout(random.randint(700, 1600))

    if best:
        shot = dump(page, "07_slots_found")
        return CheckResult(status="slots", dates=best, screenshot_path=shot)

    return unexpected("07_fechas")

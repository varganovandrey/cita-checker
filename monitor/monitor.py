"""Madrid cita-slot monitor: attaches to a real Chrome via CDP, checks slot
availability inside configurable activity windows, notifies. Never books.

Usage:
    python monitor.py --verify           # first run: slow, visible, dumps selectors
    python monitor.py --test-telegram    # check Telegram credentials
    python monitor.py --once             # single check, exit code reflects status
    python monitor.py                    # long-running loop

Console and log output is English ASCII only. Telegram text may be Russian.
"""

import argparse
import datetime as dt
import hashlib
import html
import json
import logging
import logging.handlers
import random
import sys
import time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

import flow
import notify
from flow import CheckResult, ChromeUnavailable

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
STATE_PATH = BASE_DIR / "state.json"
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
EXAMPLE_CONFIG_PATH = BASE_DIR / "config.example.json"

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

logger = logging.getLogger("monitor")


# ─────────────────────────── setup ───────────────────────────


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "monitor.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(
            f"Config not found: {path}. Copy config.example.json to config.json and fill it in."
        )
    cfg = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    user_cfg = json.loads(path.read_text(encoding="utf-8"))
    _deep_update(cfg, user_cfg)
    validate_config(cfg)
    return cfg


def _deep_update(base: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def validate_config(cfg: dict) -> None:
    """Fail fast on malformed dates, run times, windows and intervals."""
    for value in cfg["schedule"].get("run_at", []):
        try:
            _parse_hhmm(value)
        except ValueError as exc:
            raise SystemExit(f"schedule.run_at entry '{value}' must be HH:MM: {exc}") from exc
    for day in cfg["schedule"].get("run_days", []):
        if day not in WEEKDAYS:
            raise SystemExit(f"Unknown weekday '{day}' in schedule.run_days (use mon..sun)")
    for key in ("min_date", "max_date"):
        value = cfg["filter"].get(key)
        if value:
            try:
                dt.datetime.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise SystemExit(f"filter.{key} must be YYYY-MM-DD: {exc}") from exc
    try:
        ZoneInfo(cfg["schedule"]["timezone"])
    except (KeyError, ValueError) as exc:
        raise SystemExit(f"Invalid schedule.timezone: {exc}") from exc
    for window in cfg["schedule"]["windows"]:
        for day in window["days"]:
            if day not in WEEKDAYS:
                raise SystemExit(f"Unknown weekday '{day}' (use mon..sun)")
        start, end = _parse_hhmm(window["start"]), _parse_hhmm(window["end"])
        if start >= end:
            raise SystemExit(
                f"Window {window['start']}-{window['end']} must not wrap midnight; split it in two"
            )
    for key in ("active_interval_minutes", "idle_interval_minutes"):
        lo, hi = cfg["schedule"][key]
        if lo <= 0 or hi < lo:
            raise SystemExit(f"schedule.{key} must be [low, high] with 0 < low <= high")


def _parse_hhmm(value: str) -> dt.time:
    hour, minute = (int(x) for x in value.split(":"))
    return dt.time(hour, minute)


def parse_day(value: Optional[str]) -> Optional[dt.date]:
    return dt.datetime.strptime(value, "%Y-%m-%d").date() if value else None


# ─────────────────────────── schedule ───────────────────────────


def in_active_window(cfg: dict, now: dt.datetime) -> bool:
    for window in cfg["schedule"]["windows"]:
        if now.weekday() not in (WEEKDAYS[d] for d in window["days"]):
            continue
        if _parse_hhmm(window["start"]) <= now.time() < _parse_hhmm(window["end"]):
            return True
    return False


def seconds_to_next_window(cfg: dict, now: dt.datetime) -> Optional[int]:
    """Seconds until the next window opens; None if no window within 8 days."""
    candidates: list[dt.datetime] = []
    for offset in range(0, 9):
        day = now.date() + dt.timedelta(days=offset)
        for window in cfg["schedule"]["windows"]:
            if day.weekday() not in (WEEKDAYS[d] for d in window["days"]):
                continue
            start = dt.datetime.combine(day, _parse_hhmm(window["start"]), tzinfo=now.tzinfo)
            if start > now:
                candidates.append(start)
    if not candidates:
        return None
    return int((min(candidates) - now).total_seconds())


def next_run_at(cfg: dict, now: dt.datetime) -> Optional[dt.datetime]:
    """Next scheduled sweep start, or None when no fixed times are configured.

    Fixed start times suit a long full sweep better than an interval: one pass
    over every office takes ~27 min, so "start at 09:30 and again at 14:00" is
    what actually happens, rather than "check every N minutes".
    """
    sched = cfg["schedule"]
    times = sorted(_parse_hhmm(t) for t in sched.get("run_at", []) if t)
    if not times:
        return None
    days = {WEEKDAYS[d] for d in sched.get("run_days", list(WEEKDAYS))}
    for offset in range(0, 9):
        day = now.date() + dt.timedelta(days=offset)
        if day.weekday() not in days:
            continue
        for moment in times:
            candidate = dt.datetime.combine(day, moment, tzinfo=now.tzinfo)
            if candidate > now:
                return candidate
    return None


def _within_quick_hours(cfg: dict, moment: dt.datetime) -> bool:
    """Whether quick checks are allowed at this time. Empty config means always."""
    hours = cfg.get("quick_check_hours") or []
    if len(hours) != 2:
        return True
    start, end = _parse_hhmm(hours[0]), _parse_hhmm(hours[1])
    return start <= moment.time() < end


def next_sleep_seconds(cfg: dict, now: dt.datetime) -> int:
    sched = cfg["schedule"]
    if in_active_window(cfg, now):
        lo, hi = sched["active_interval_minutes"]
        return int(random.uniform(lo, hi) * 60)
    lo, hi = sched["idle_interval_minutes"]
    idle = int(random.uniform(lo, hi) * 60)
    to_window = seconds_to_next_window(cfg, now)
    if to_window is None:
        return idle
    return min(idle, to_window + random.randint(5, 60))


# ─────────────────────────── state ───────────────────────────


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("State file unreadable, starting fresh")
    return {"last_notified_hash": None, "last_notified_at": None,
            "blocked_until": None, "blocked_notified": False}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot write state file: %s", exc)


def slots_digest(slots: list[tuple[dt.date, str]], office: str = "") -> str:
    payload = office + "|" + "|".join(sorted(f"{d:%d/%m/%Y} {t}".strip() for d, t in slots))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def format_slots(slots: list[tuple[dt.date, str]], limit: int = 8) -> str:
    return ", ".join(f"{d:%d/%m/%Y}{' ' + t if t else ''}" for d, t in slots[:limit])


# ─────────────────────────── check execution ───────────────────────────


def offices_for_cycle(cfg: dict, state: dict) -> list[str]:
    """Offices to sweep this cycle: the primary one plus a rotating slice.

    Whether "Cualquier oficina" really searches every branch is unverified, so
    specific offices are swept alongside it rather than trusted away. Each office
    costs a full ~45s pass, hence the small rotating slice instead of all 40.
    """
    primary = cfg["office"]
    extra = [o for o in cfg.get("extra_offices", []) if o]
    per_cycle = int(cfg.get("offices_per_cycle", 0))
    if not extra or per_cycle <= 0:
        return [primary]
    start = int(state.get("office_rotation", 0)) % len(extra)
    picked = [extra[(start + i) % len(extra)] for i in range(min(per_cycle, len(extra)))]
    state["office_rotation"] = (start + len(picked)) % len(extra)
    sweep = [primary, *picked]
    # a sweep runs for the better part of an hour, so the opening broad check is
    # stale by the end; repeat it last to catch anything released meanwhile
    if cfg.get("recheck_primary_last", True):
        sweep.append(primary)
    return sweep


def run_check(pw, browser_holder: dict, cfg: dict, verify: bool = False,
              office: Optional[str] = None) -> CheckResult:
    """Run one full check for one office. Keeps the tab open only on slots."""
    browser = browser_holder.get("browser")
    if browser is None or not browser.is_connected():
        browser = flow.get_browser(pw, cfg["browser"])
        browser_holder["browser"] = browser

    page = flow.new_tab(browser)
    keep_open = False
    try:
        result = flow.walk_to_dates(page, cfg, verify=verify, office=office)
        keep_open = result.status == "slots" or verify
        if keep_open:
            try:
                page.bring_to_front()
            except PWError:
                pass
        return result
    finally:
        if not keep_open:
            try:
                page.close()
            except PWError:
                pass


def handle_result(result: CheckResult, cfg: dict, state: dict, now: dt.datetime,
                  office: str = "") -> bool:
    """Act on one office result. Returns True if slots were announced."""
    min_date = parse_day(cfg["filter"].get("min_date"))
    max_date = parse_day(cfg["filter"].get("max_date"))
    notify_cfg = cfg["notify"]

    if result.status == "slots":
        good = flow.filter_slots(result.dates, min_date, max_date)
        if not good:
            logger.info("Slots found but outside date filter: %s", format_slots(result.dates))
            return False
        # keyed per office: the same date at a different branch is a separate find
        digest = slots_digest(good, office)
        seen = state.setdefault("notified", {})
        entry = seen.get(office)
        cooldown = dt.timedelta(minutes=notify_cfg["repeat_cooldown_minutes"])
        fresh = entry is None or entry.get("hash") != digest
        expired = entry is None or (now - dt.datetime.fromisoformat(entry["at"])) > cooldown
        if not (fresh or expired):
            logger.info("Same slots within cooldown (%s), suppressed", office)
            return False

        shown = format_slots(good)
        logger.info("SLOTS FOUND [%s]: %s", office, shown)
        message = (
            "Свободна сита!\n"
            f"{office}\n{shown}\n\n"
            "Вкладка открыта на экране дат — дожимайте бронь вручную."
        )
        if notify_cfg.get("local", True):
            notify.local_alert("Cita Madrid", message)
        if notify_cfg.get("telegram", True):
            photo = result.screenshot_path if notify_cfg.get("screenshot", True) else None
            notify.send_telegram(
                f"<b>Свободна сита!</b>\n{html.escape(office)}\n{html.escape(shown)}", photo)
        seen[office] = {"hash": digest, "at": now.isoformat()}
        return True

    elif result.status == "no_slots":
        logger.info("No slots available [%s]", office)
        state["blocked_notified"] = False

    elif result.status == "not_offered":
        logger.info("Tramite not offered at [%s] - drop it from extra_offices", office)
        state["blocked_notified"] = False

    elif result.status == "blocked":
        lo, hi = cfg["blocked_backoff_minutes"]
        until = now + dt.timedelta(minutes=random.uniform(lo, hi))
        state["blocked_until"] = until.isoformat()
        logger.warning("Blocked by site (%s). Backing off until %s",
                       result.detail[:160], until.strftime("%H:%M"))
        if not state.get("blocked_notified") and cfg["notify"].get("telegram", True):
            notify.send_telegram(
                "Сайт отбивает запросы (блокировка). "
                f"Пауза до {until.strftime('%H:%M')} по времени Мадрида."
            )
            state["blocked_notified"] = True

    else:
        # An unrecognised screen may BE the opportunity - dates in an unexpected
        # format, a changed layout - so it must not fail silently. Rate-limited
        # so a persistent site change cannot turn into a notification flood.
        logger.warning("Unknown screen, see debug/: %s", result.detail[:200])
        last = state.get("unknown_notified_at")
        quiet = dt.timedelta(hours=notify_cfg.get("unknown_cooldown_hours", 6))
        due = last is None or (now - dt.datetime.fromisoformat(last)) > quiet
        if due and notify_cfg.get("telegram", True):
            photo = result.screenshot_path if notify_cfg.get("screenshot", True) else None
            notify.send_telegram(
                "<b>Незнакомый экран на сайте</b>\n"
                f"{html.escape(office)}\n"
                "Возможно, изменилась вёрстка — или это слоты, которых монитор "
                "не распознал. Загляните в браузер и в debug/.\n"
                f"<code>{html.escape(result.detail[:200])}</code>",
                photo,
            )
            state["unknown_notified_at"] = now.isoformat()

    return False


# ─────────────────────────── main loop ───────────────────────────


def run_loop(cfg: dict, once: bool, verify: bool,
             offices_override: Optional[list[str]] = None) -> int:
    tz = ZoneInfo(cfg["schedule"]["timezone"])
    scheduled = bool(cfg["schedule"].get("run_at"))
    pending: Optional[dt.datetime] = None
    next_quick: Optional[dt.datetime] = None
    state = load_state()
    browser_holder: dict = {}
    consecutive_errors = 0
    last_status = "error"

    with sync_playwright() as pw:
        while True:
            now = dt.datetime.now(tz)
            is_quick = False

            blocked_until = state.get("blocked_until")
            if blocked_until and not once and not verify:
                until = dt.datetime.fromisoformat(blocked_until)
                if now < until:
                    nap = min(int((until - now).total_seconds()), 1800)
                    logger.info("Backoff active, sleeping %d min", max(1, nap // 60))
                    time.sleep(nap)
                    continue
                state["blocked_until"] = None

            if scheduled and not once and not verify:
                # the target is held rather than recomputed: next_run_at() returns
                # strictly future times, so recomputing at the moment it comes due
                # would skip straight past it to the following slot
                if pending is None:
                    pending = next_run_at(cfg, now)
                if pending is None:
                    raise SystemExit("schedule.run_at is set but yields no run time")
                # Between full sweeps, poke the broad office on its own cadence.
                # A full sweep runs for ~48 min, so a quick check that started
                # just before one would still be running when it begins - hence
                # the guard band rather than a bare comparison of due times.
                quick_every = int(cfg.get("quick_check_minutes", 0) or 0)
                if quick_every and next_quick is None:
                    next_quick = now + dt.timedelta(minutes=quick_every)
                if next_quick is not None and not _within_quick_hours(cfg, next_quick):
                    # slots are released during office hours; checks at 03:00 are
                    # load without upside
                    next_quick += dt.timedelta(minutes=quick_every)

                target, is_quick = pending, False
                if next_quick is not None and next_quick < pending:
                    guard = dt.timedelta(minutes=int(cfg.get("quick_check_guard_minutes", 5)))
                    if next_quick + guard >= pending:
                        # too close to the sweep: let the sweep have it
                        next_quick = None
                        logger.info("Quick check skipped, full sweep is imminent")
                    else:
                        target, is_quick = next_quick, True

                wait = int((target - now).total_seconds())
                if wait > 0:
                    nap = min(wait, 1800)  # wake periodically so Ctrl+C stays responsive
                    logger.info("Next %s at %s %s (in %d min), sleeping %d min",
                                "quick check" if is_quick else "sweep",
                                target.strftime("%a %H:%M"), cfg["schedule"]["timezone"],
                                wait // 60, max(1, nap // 60))
                    save_state(state)
                    time.sleep(nap)
                    continue

                if is_quick:
                    logger.info("Quick check starting (%s)", target.strftime("%a %H:%M"))
                    next_quick = now + dt.timedelta(minutes=quick_every)
                else:
                    logger.info("Scheduled sweep starting (%s)", pending.strftime("%a %H:%M"))
                    pending = None
                    next_quick = None  # re-armed once the sweep is done

            elif not once and not verify and not in_active_window(cfg, now):
                nap = next_sleep_seconds(cfg, now)
                logger.info("Outside activity window, sleeping %d min", max(1, nap // 60))
                save_state(state)
                time.sleep(nap)
                continue

            # "Cualquier oficina" first, then a rotating slice of specific
            # offices ordered nearest-first
            if is_quick:
                sweep = [cfg["office"]]
            else:
                sweep = (offices_override or
                         ([cfg["office"]] if verify else offices_for_cycle(cfg, state)))
            result = CheckResult(status="error", detail="no check ran")

            notify_cfg = cfg["notify"]
            sweep_started = dt.datetime.now(tz)
            announced = False
            checked = 0
            errors_this_sweep = 0
            # a half-hourly check that announced itself would be 48 messages a
            # day; it stays quiet unless it actually finds or breaks something
            if notify_cfg.get("sweep_start", True) and not verify and not is_quick:
                estimate = round(len(sweep) * 75 / 60)
                notify.send_telegram(
                    f"Обход начался: {len(sweep)} офисов, ориентировочно {estimate} мин."
                )

            chunk = int(cfg.get("sweep_chunk_size", 0) or 0)
            chunk_pause = int(cfg.get("sweep_chunk_pause_minutes", 15))
            batches = max(1, -(-len(sweep) // chunk)) if chunk else 1
            for index, office in enumerate(sweep):
                if index:
                    # long unbroken runs get rejected; short batches with a real
                    # break between them have gone through cleanly
                    if chunk and index % chunk == 0:
                        logger.info("Batch of %d done, pausing %d min before %s",
                                    chunk, chunk_pause, office)
                        # a silent 85-minute sweep is indistinguishable from a
                        # hung process, so report after every batch
                        if notify_cfg.get("sweep_progress", True) and not verify:
                            done = index // chunk
                            notify.send_telegram(
                                f"Порция {done} из {batches}: проверено {checked} офисов, "
                                f"{'слотов нет' if not announced else 'есть находки'}. "
                                f"Продолжу через {chunk_pause} мин.",
                                silent=True,  # routine chatter must not buzz the phone
                            )
                        time.sleep(chunk_pause * 60)
                    else:
                        gap = random.randint(*cfg.get("office_switch_seconds", [25, 70]))
                        logger.info("Next office in %d s: %s", gap, office)
                        time.sleep(gap)
                try:
                    result = run_check(pw, browser_holder, cfg, verify=verify, office=office)
                    consecutive_errors = 0 if result.status != "error" else consecutive_errors + 1
                except ChromeUnavailable as exc:
                    logger.error("Chrome unavailable: %s", exc)
                    browser_holder.pop("browser", None)
                    result = CheckResult(status="error", detail=str(exc))
                    consecutive_errors += 1
                except (PWError, LookupError, OSError) as exc:
                    logger.error("Check failed [%s]: %s", office, exc)
                    browser_holder.pop("browser", None)
                    result = CheckResult(status="error", detail=str(exc))
                    consecutive_errors += 1

                announced |= handle_result(result, cfg, state, now, office=office)
                if result.status in ("slots", "no_slots", "not_offered"):
                    checked += 1
                save_state(state)
                last_status = result.status
                if result.status == "blocked":
                    break  # the site is refusing; walking on only deepens it
                if result.status == "error":
                    # one odd screen is not a reason to abandon the whole day:
                    # a scheduled sweep has no second chance until tomorrow
                    errors_this_sweep += 1
                    if errors_this_sweep >= int(cfg.get("sweep_error_budget", 3)):
                        logger.warning("Too many odd screens this sweep (%d), stopping early",
                                       errors_this_sweep)
                        break
                    logger.info("Odd screen at %s, moving on to the next office", office)

            # Free the machine between sweeps - but never when slots were found:
            # that tab is deliberately parked on the dates screen for booking.
            if cfg.get("close_browser_after_sweep", True) and not announced and not verify:
                flow.close_browser(browser_holder.pop("browser", None), cfg["browser"])

            if (notify_cfg.get("sweep_summary", True) and not verify
                    and not announced and not is_quick):
                minutes = round((dt.datetime.now(tz) - sweep_started).total_seconds() / 60)
                cut = "" if checked == len(sweep) else f" (обход прерван: {last_status})"
                notify.send_telegram(
                    f"Обход завершён, слотов нет. Проверено {checked} из {len(sweep)} "
                    f"за {minutes} мин{cut}."
                )

            if once or verify:
                break

            if scheduled:
                # fixed start times: the top of the loop works out the next one
                if result.status == "error":
                    nap = min(60 * 2 ** consecutive_errors, 1800)
                    logger.info("Error backoff: sleeping %d min", max(1, nap // 60))
                    time.sleep(nap)
                continue

            extra = 0
            if result.status == "error":
                extra = min(60 * 2 ** consecutive_errors, 1800)
                logger.info("Error backoff: +%d s", extra)
            nap = next_sleep_seconds(cfg, dt.datetime.now(tz)) + extra
            logger.info("Next check in %d min", max(1, nap // 60))
            time.sleep(nap)

    exit_codes = {"slots": 0, "no_slots": 1, "not_offered": 1, "blocked": 2, "error": 3}
    return exit_codes.get(last_status, 3)


SURVEY_PATH_NAME = "offices_survey.json"


# below Windows' ephemeral range (49152+), so a stray outbound connection
# cannot squat on it between runs
SINGLETON_PORT = 43117
_singleton_socket = None


def claim_singleton() -> bool:
    """Bind a loopback port as a mutex so only one daemon runs.

    A socket beats a PID file here: it cannot go stale after a crash or a
    reboot, which matters when the Task Scheduler fires the same command daily.
    """
    global _singleton_socket
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLETON_PORT))
        sock.listen(1)
    except OSError:
        sock.close()
        return False
    _singleton_socket = sock  # held for the process lifetime
    return True


def run_discovery(cfg: dict) -> int:
    """Resumable survey: which offices actually offer the configured tramite.

    Results accumulate in debug/offices_survey.json, so an interrupted or
    rejected run resumes instead of re-checking everything.
    """
    survey_path = BASE_DIR / "debug" / SURVEY_PATH_NAME
    survey_path.parent.mkdir(exist_ok=True)
    known: dict[str, bool] = {}
    if survey_path.exists():
        try:
            known = json.loads(survey_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Survey cache unreadable, starting fresh")

    results: list[tuple[str, bool]] = []
    try:
        with sync_playwright() as pw:
            browser = flow.get_browser(pw, cfg["browser"])
            page = flow.new_tab(browser)
            try:
                results = flow.discover_offices(page, cfg, known=known)
            finally:
                try:
                    page.close()
                except PWError:
                    pass
    except (PWError, LookupError, ChromeUnavailable) as exc:
        logger.error("Survey interrupted: %s", exc)
        logger.info("Progress so far is kept; run again later to resume")

    known.update(dict(results))
    survey_path.write_text(json.dumps(known, ensure_ascii=False, indent=2), encoding="utf-8")

    offering = [label for label, offers in known.items() if offers]
    logger.info("Offices offering '%s': %d of %d checked",
                cfg["tramite_contains"], len(offering), len(known))
    out = BASE_DIR / "debug" / "offices_with_tramite.json"
    out.write_text(json.dumps(offering, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Full list written to %s", out)
    logger.info("Paste the ones nearest to you into config extra_offices:")
    for label in offering:
        logger.info("    %s", label)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Madrid cita slot monitor (notify only)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--once", action="store_true", help="single check, then exit")
    parser.add_argument("--verify", action="store_true",
                        help="slow visible walk, dump selectors to debug/")
    parser.add_argument("--test-telegram", action="store_true",
                        help="send a test Telegram message and exit")
    parser.add_argument("--discover-offices", action="store_true",
                        help="survey which offices offer the configured tramite, then exit")
    parser.add_argument("--offices",
                        help="comma-separated offices to sweep once, then exit "
                             "(overrides the configured rotation)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        logger.warning("python-dotenv not installed; relying on process environment")

    if args.test_telegram:
        # real alerts attach a screenshot over multipart, so test that path too
        shots = sorted((BASE_DIR / "debug").glob("*.png"), key=lambda p: p.stat().st_mtime)
        photo = shots[-1] if shots else None
        ok = notify.send_telegram(
            "Cita monitor: тестовое сообщение. Связь работает."
            + ("\nНиже — проверка отправки скриншота." if photo else
               "\n(скриншота для проверки не нашлось, отправлен только текст)"),
            photo,
        )
        logger.info("Telegram test: %s%s", "OK" if ok else "FAILED",
                    f" (photo: {photo.name})" if photo else " (text only)")
        return 0 if ok else 1

    cfg = load_config(Path(args.config))
    logger.info("Start. province=%s tramite=%s office=%s tz=%s",
                cfg["province"], cfg["tramite_contains"], cfg["office"],
                cfg["schedule"]["timezone"])
    if cfg["filter"].get("min_date"):
        logger.info("Min date filter: %s (police will not accept earlier)",
                    cfg["filter"]["min_date"])

    if args.discover_offices:
        return run_discovery(cfg)

    override = None
    if args.offices:
        override = [o.strip() for o in args.offices.split(",") if o.strip()]
        logger.info("One-off sweep over %d offices", len(override))

    daemon = not (args.once or args.verify or override)
    if daemon and not claim_singleton():
        logger.info("Another monitor instance is already running - exiting")
        return 0

    try:
        return run_loop(cfg, once=args.once or bool(override), verify=args.verify,
                        offices_override=override)
    except KeyboardInterrupt:
        logger.info("Stopped by user")
        return 0


if __name__ == "__main__":
    sys.exit(main())

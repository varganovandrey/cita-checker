import time
import random
from seleniumbase import SB
from selenium.webdriver.common.by import By
import json
import smtplib
from email.message import EmailMessage
import os
import logging
import subprocess

with open('values.json') as config_file:
    config = json.load(config_file)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def human_pause(a=0.6, b=1.8):
    time.sleep(random.uniform(a, b))


def find_and_kill():
    for name in ("chrome", "chromedriver", "google-chrome", "undetected_chromedriver"):
        try:
            pids = subprocess.check_output(f"pgrep -f {name}", shell=True).decode().strip().split()
            for pid in pids:
                subprocess.call(f"kill {pid}", shell=True, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            pass


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[logging.FileHandler("/tmp/events.log"), logging.StreamHandler()]
    )


def dump_state(sb, tag):
    """Screenshot + URL + visible text, so you can see WHERE it actually is."""
    try:
        path = f"/tmp/debug_{tag}.png"
        sb.save_screenshot(path)
        url = sb.get_current_url()
        try:
            body = sb.get_text("body")[:600].replace("\n", " ")
        except Exception:
            body = "(could not read body)"
        logging.info(f"[{tag}] URL={url}")
        logging.info(f"[{tag}] TEXT={body}")
        logging.info(f"[{tag}] screenshot -> {path}")
    except Exception as e:
        logging.error(f"dump_state failed: {e}")


def log_options(sb, css, tag):
    """Print every <option> label+value for a select, to find the right one."""
    try:
        opts = sb.find_elements(f"{css} option")
        logging.info(f"[{tag}] options for {css}:")
        for o in opts:
            logging.info(f"    value={o.get_attribute('value')!r}  text={o.text!r}")
    except Exception as e:
        logging.error(f"log_options failed for {css}: {e}")


def select_by_text_safe(sb, css, text, tag):
    """Select an option by visible text; on failure dump the real options."""
    try:
        sb.wait_for_element_visible(css, timeout=15)
        sb.select_option_by_text(css, text)
        return True
    except Exception as e:
        logging.error(f"Could not select {text!r} in {css}: {e}")
        log_options(sb, css, tag)
        return False


def looks_blocked(sb):
    for marker in ("Request Rejected", "support ID", "Acceso no autorizado"):
        try:
            if sb.is_text_visible(marker):
                return True
        except Exception:
            pass
    return False


def looks_rate_limited(sb):
    for marker in ("Too Many Requests", "too many requests", "429"):
        try:
            if sb.is_text_visible(marker):
                return True
        except Exception:
            pass
    return False


try:
    subprocess.run(["setxkbmap", "-layout", config['keyboard_layout']], check=True)
except (subprocess.CalledProcessError, KeyError, FileNotFoundError) as e:
    print(f"Could not set keyboard layout: {e}")


def send_email(subject, message, attach_screenshot=False):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = config['sender_email']
    msg['To'] = config['receiver_email']
    msg.set_content(message)
    if attach_screenshot and os.path.exists("/tmp/cita_disponible.png"):
        with open("/tmp/cita_disponible.png", 'rb') as f:
            msg.add_attachment(f.read(), maintype='image', subtype='png',
                               filename="cita_disponible.png")
    try:
        with smtplib.SMTP(config['smtp_server'], config['smtp_port']) as smtp:
            smtp.ehlo(); smtp.starttls(); smtp.ehlo()
            smtp.login(config['sender_email'], config['password'])
            smtp.send_message(msg)
            logging.info("Email sent successfully!")
    except Exception as e:
        logging.error(f"Error sending email: {e}")


def set_random_window_size(sb):
    width = random.randint(1100, 1600)
    sb.set_window_size(width, (width * 2) // 3)


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------
def check_for_appointments():
    with SB(
        uc=True,
        headed=True,
        locale="es",
        # proxy="user:pass@residential-host:port",   # residential proxy strongly recommended
    ) as sb:
        try:
            set_random_window_size(sb)
            sb.uc_open_with_reconnect(config['url'], reconnect_time=6)

            # --- gatekeepers, checked in priority order ---
            if looks_rate_limited(sb):
                dump_state(sb, "rate_limited")
                find_and_kill()
                return "rate_limited"

            try:
                sb.uc_gui_click_captcha()
            except Exception:
                pass

            if looks_blocked(sb):
                dump_state(sb, "blocked_landing")
                find_and_kill()
                return "blocked"

            # --- page 1: provincia ---
            if not select_by_text_safe(sb, "#form", config['region'], "provincia"):
                find_and_kill(); return "error"
            human_pause()
            sb.click("#btnAceptar")

            # --- page 2: oficina (sede) + trámite ---
            if looks_rate_limited(sb):
                dump_state(sb, "rate_limited_p2"); find_and_kill(); return "rate_limited"
            sb.wait_for_element_visible("#sede", timeout=15)
            try:
                sb.select_option_by_value("#sede", "99")
            except Exception:
                log_options(sb, "#sede", "sede")
            human_pause()
            if not select_by_text_safe(sb, "#tramiteGrupo\\[0\\]",
                                       config['tramiteOptionText'], "tramite"):
                find_and_kill(); return "error"
            human_pause()
            sb.click("#btnAceptar")

            # --- page 3: info page -> Entrar ---
            human_pause(1.5, 3.0)
            if looks_rate_limited(sb):
                dump_state(sb, "rate_limited_p3"); find_and_kill(); return "rate_limited"
            if looks_blocked(sb):
                dump_state(sb, "blocked_after_aceptar"); find_and_kill(); return "blocked"
            if not sb.is_element_present("#btnEntrar"):
                dump_state(sb, "no_btnEntrar"); find_and_kill(); return "error"
            sb.click("#btnEntrar")

            # --- page 4: applicant details ---
            sb.wait_for_element_visible("#rdbTipoDocNie", timeout=15)
            sb.find_element(By.ID, "rdbTipoDocNie").click()
            human_pause(0.3, 0.9)
            sb.type("#txtIdCitado", config['idCitadoValue'])
            human_pause(0.3, 0.9)
            sb.type("#txtDesCitado", config['desCitadoValue'])
            human_pause(0.3, 0.9)

            if sb.is_element_present("#txtPaisNac"):
                select_by_text_safe(sb, "#txtPaisNac", config['paisNacValue'], "pais")
                human_pause(0.3, 0.9)

            sb.click("#btnEnviar")
            human_pause()
            sb.click("#btnEnviar")
            human_pause(1.5, 3.0)

            # --- result ---
            if looks_rate_limited(sb):
                dump_state(sb, "rate_limited_result"); find_and_kill(); return "rate_limited"
            if sb.is_text_visible("En este momento no hay citas disponibles"):
                logging.info("No available appointments. Retrying later.")
                find_and_kill()
                return "retry"
            else:
                sb.set_window_size(1280, 1024)
                sb.save_screenshot("/tmp/cita_disponible.png")
                send_email("Cita Disponible Alert", "localhost:6080 to complete",
                           attach_screenshot=True)
                logging.info("Appointment may be available. Browser left open for manual check.")
                time.sleep(600)
                return "manual_check_needed"

        except Exception as e:
            logging.error(f"Error during the steps: {e}. Retrying later.")
            try:
                dump_state(sb, "exception")
            except Exception:
                pass
            find_and_kill()
            return "error"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    setup_logging()
    rl_backoff = 1800  # rate-limit cooldown starts at 30 min, doubles up to 2 h
    while True:
        result = check_for_appointments()

        if result == "manual_check_needed":
            input("Press Enter to exit after your manual check...")
            break

        elif result == "rate_limited":
            wait = min(rl_backoff, 7200) + random.randint(0, 300)
            logging.warning(f"Rate limited (429). Cooling down {wait}s.")
            time.sleep(wait)
            rl_backoff = min(rl_backoff * 2, 7200)

        elif result == "blocked":
            wait = random.randint(900, 1800)  # 15-30 min for WAF blocks
            logging.info(f"Blocked by WAF. Sleeping {wait}s.")
            time.sleep(wait)

        else:  # retry / error -> we're getting through, poll gently
            rl_backoff = 1800  # reset the escalation once clean again
            wait = random.randint(300, 480)  # 5-8 min
            logging.info(f"Sleeping {wait}s before next attempt.")
            time.sleep(wait)

        find_and_kill()


if __name__ == "__main__":
    main()
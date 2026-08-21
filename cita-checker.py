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
    try:
        opts = sb.find_elements(f"{css} option")
        logging.info(f"[{tag}] options for {css}:")
        for o in opts:
            logging.info(f"    value={o.get_attribute('value')!r}  text={o.text!r}")
    except Exception as e:
        logging.error(f"log_options failed for {css}: {e}")


def select_by_text_safe(sb, css, text, tag):
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
    # NOTE: do NOT match the bare "429" -- it appears inside WAF support IDs
    # and causes false positives. Only match the visible 429 page text.
    for marker in ("Too Many Requests", "too many requests"):
        try:
            if sb.is_text_visible(marker):
                return True
        except Exception:
            pass
    return False


def gate_check(sb, tag):
    """Return 'rate_limited', 'blocked', or None for a page transition."""
    if looks_rate_limited(sb):
        dump_state(sb, f"rate_limited_{tag}")
        return "rate_limited"
    if looks_blocked(sb):
        dump_state(sb, f"blocked_{tag}")
        return "blocked"
    return None


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

            try:
                sb.uc_gui_click_captcha()
            except Exception:
                pass

            g = gate_check(sb, "landing")
            if g:
                find_and_kill(); return g

            # --- page 1: provincia ---
            if not select_by_text_safe(sb, "#form", config['region'], "provincia"):
                find_and_kill(); return "error"
            human_pause()
            sb.click("#btnAceptar")

            # --- page 2: oficina (sede) + trámite ---
            human_pause(1.0, 2.0)
            g = gate_check(sb, "p2")
            if g:
                find_and_kill(); return g
            sb.wait_for_element_visible("#sede", timeout=15)
            try:
                sb.select_option_by_value("#sede", "99")   # 99 = cualquier oficina
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
            g = gate_check(sb, "p3")
            if g:
                find_and_kill(); return g
            if not sb.is_element_present("#btnEntrar"):
                dump_state(sb, "no_btnEntrar"); find_and_kill(); return "error"
            sb.click("#btnEntrar")

            # --- page 4: applicant details ---
            sb.wait_for_element_visible("#rdbTipoDocNie", timeout=15)
            sb.find_element(By.ID, "rdbTipoDocNie").click()
            human_pause(0.3, 0.9)
            sb.type("#txtIdCitado", config['idCitadoValue'])      # NIE: Z2405206D
            human_pause(0.3, 0.9)
            sb.type("#txtDesCitado", config['desCitadoValue'])    # MAHMOUD HAMED
            human_pause(0.3, 0.9)

            if sb.is_element_present("#txtPaisNac"):
                # set "LIBANO" in values.json (Spanish label), not "Lebanon"
                select_by_text_safe(sb, "#txtPaisNac", config['paisNacValue'], "pais")
                human_pause(0.3, 0.9)

            sb.click("#btnEnviar")
            human_pause()
            sb.click("#btnEnviar")
            human_pause(1.5, 3.0)

            # --- result ---
            g = gate_check(sb, "result")
            if g:
                find_and_kill(); return g
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
    rl_backoff = 1800  # 429 cooldown: 30 min, doubling up to 2 h
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
            wait = random.randint(900, 1800)  # WAF block: 15-30 min
            logging.info(f"WAF block. Sleeping {wait}s.")
            time.sleep(wait)
        else:  # retry / error
            rl_backoff = 1800
            wait = random.randint(300, 480)  # 5-8 min
            logging.info(f"Sleeping {wait}s before next attempt.")
            time.sleep(wait)

        find_and_kill()


if __name__ == "__main__":
    main()
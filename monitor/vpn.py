"""Detect and switch off a VPN tunnel before the monitor touches the site.

The whole anti-detection design rests on the request coming from the user's own
home connection: a real browser fingerprint plus a residential Spanish-facing
IP. A VPN exit node replaces the second half, which is a good way to be
rejected. So a run either happens with the tunnel down, or it does not happen.

Detection is read-only and needs no privileges. Switching the tunnel off means
stopping a Windows service, which normally does need elevation - when that is
refused the monitor says so plainly rather than quietly running through the VPN.
"""

import logging
import subprocess
import time
from typing import Optional

logger = logging.getLogger("monitor.vpn")

PS = ["powershell.exe", "-NoProfile", "-Command"]
TIMEOUT = 30

# Under pythonw the daemon has no console of its own, so every helper process
# Windows starts gets a brand new window - roughly 64 flashes a day from the
# VPN probe alone. CREATE_NO_WINDOW keeps them out of sight.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_ps(script: str) -> tuple[int, str]:
    try:
        out = subprocess.run(PS + [script], capture_output=True, text=True,
                             timeout=TIMEOUT, creationflags=NO_WINDOW)
        return out.returncode, (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("PowerShell call failed: %s", exc)
        return 1, ""


def is_active(vpn_cfg: dict) -> tuple[bool, str]:
    """Whether the tunnel is up and carrying the default route.

    Returns:
        (active, human-readable reason).
    """
    adapter = vpn_cfg.get("adapter_name", "")
    service = vpn_cfg.get("service_name", "")
    if not adapter and not service:
        return False, "no adapter or service configured"

    # One PowerShell launch, not two: starting the shell costs ~3.5 s and this
    # runs before every check, so a second probe would double the overhead for
    # no extra information.
    parts = []
    if adapter:
        parts.append(
            f"$a = Get-NetAdapter -Name '{adapter}' -ErrorAction SilentlyContinue; "
            "if ($a -and $a.Status -eq 'Up') { "
            f"  $r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -InterfaceAlias '{adapter}' "
            "       -ErrorAction SilentlyContinue; "
            "  if ($r) { 'ADAPTER=DEFAULT_ROUTE' } else { 'ADAPTER=UP' } "
            "} else { 'ADAPTER=DOWN' }"
        )
    if service:
        parts.append(
            f"'SERVICE=' + (Get-Service -Name '{service}' -ErrorAction SilentlyContinue).Status"
        )
    code, out = _run_ps("; ".join(parts))
    if code != 0:
        return False, "could not query the tunnel state"

    facts = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line
    )
    if facts.get("ADAPTER") == "DEFAULT_ROUTE":
        return True, f"adapter '{adapter}' is up and holds the default route"
    if facts.get("ADAPTER") == "UP":
        return True, f"adapter '{adapter}' is up"
    if facts.get("SERVICE") == "Running":
        return True, f"service '{service}' is running"

    return False, "no active tunnel found"


def disable(vpn_cfg: dict) -> tuple[bool, str]:
    """Try to bring the tunnel down. Returns (success, what happened)."""
    service = vpn_cfg.get("service_name", "")
    if not service:
        return False, "no service_name configured"

    code, out = _run_ps(
        f"try {{ Stop-Service -Name '{service}' -Force -ErrorAction Stop; 'STOPPED' }} "
        "catch { 'FAILED: ' + $_.Exception.Message }"
    )
    if code == 0 and out.startswith("STOPPED"):
        logger.info("VPN tunnel service stopped")
        return True, "tunnel service stopped"

    reason = out[7:] if out.startswith("FAILED:") else (out or "unknown error")
    logger.info("Direct stop refused (%s)", reason.strip()[:80])

    # Stopping a service needs elevation, which a Task Scheduler daemon does not
    # have. A non-elevated process may however *trigger* a task that was
    # registered to run elevated - so the privileged bit is done once, by hand,
    # and the monitor only pulls the trigger.
    task = vpn_cfg.get("stop_task_name", "")
    if not task:
        return False, reason[:200]

    try:
        subprocess.run(["schtasks", "/Run", "/TN", task],
                       capture_output=True, text=True, timeout=TIMEOUT, check=False,
                       creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not trigger '{task}': {exc}"

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        active, _ = is_active(vpn_cfg)
        if not active:
            logger.info("VPN switched off via task '%s'", task)
            return True, f"tunnel stopped via task '{task}'"
        time.sleep(1)
    return False, f"task '{task}' ran but the tunnel is still up"


def ensure_off(vpn_cfg: dict) -> tuple[bool, str]:
    """Make sure the tunnel is down before a run.

    Returns:
        (clear_to_run, explanation). Never disables anything unless the config
        asks for it: detection alone is the default, since switching a VPN off
        affects every other thing on the machine, not just this monitor.
    """
    if not vpn_cfg.get("check_before_run", True):
        return True, "vpn check disabled"

    active, why = is_active(vpn_cfg)
    if not active:
        return True, why

    logger.warning("VPN is active: %s", why)
    if not vpn_cfg.get("disable_before_run", False):
        return not vpn_cfg.get("skip_run_if_active", True), "vpn active, not allowed to disable it"

    ok, detail = disable(vpn_cfg)
    if ok:
        still, _ = is_active(vpn_cfg)
        if still:
            return not vpn_cfg.get("skip_run_if_active", True), "tunnel still up after stopping"
        return True, "vpn switched off"
    return not vpn_cfg.get("skip_run_if_active", True), detail


def describe(vpn_cfg: dict) -> Optional[str]:
    """One-line status for logs and alerts."""
    active, why = is_active(vpn_cfg)
    return f"{'ACTIVE' if active else 'off'} ({why})"

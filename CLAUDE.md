# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Cita Checker Enhanced is a comprehensive Python automation system that monitors appointment availability on Spain's "cita previa" platform (https://icp.administracionelectronica.gob.es/icpplus/index.html) for various government services. The system includes advanced anti-detection features, dual notification systems (Telegram + Email), metrics collection, and intelligent retry logic.

## CURRENT IMPLEMENTATION: monitor/ (v2)

**Work on `monitor/`. The `cita-checker*.py` scripts below are legacy** — all eight were soft-blocked by the site and are kept only as reference. `monitor-lite/` is an earlier Playwright attempt whose selectors were never verified against the live site (two of them turned out to be wrong).

`monitor/` is verified end-to-end against the live site: full form pass, correct outcome classification, Telegram alerts. See `monitor/README.md` for details. Key facts established by live testing:

- **Attaches over CDP to the user's real Brave** (`--remote-debugging-port=9222`, dedicated profile) instead of launching a fresh chromium with a spoofed UA. Real fingerprint, home IP.
- **The WAF (F5 ASM) judges behaviour, not requests.** A manual and an automated POST to `acValidarEntrada` were byte-identical in fields, headers and cookies — the manual one passed, the automated one was rejected. What differs is mouse-movement telemetry and typing rhythm. Hence `human_move_to()` (bezier cursor paths), `human_wander()` and per-character typing delays, all via CDP so events are trusted. **Do not speed up `action_delay_ms` / `typing_delay_ms`** — a full pass takes 38 s and that is a working condition, not slack.
- The WAF has two rejection faces: a `Request Rejected` page and a JavaScript-challenge page. Both carry a support ID; both are in `BLOCK_MARKERS`. A page with no `form/select/input` is also treated as blocked.
- **Office is selected BEFORE the tramite, on the same page.** `#sede` fires `onchange="cargaTramites()"` which repopulates `#tramiteGrupo[0]` via `POST /selectSede`. `#idSede` does not exist here.
- **Option matching must prefer exact over substring**: the country list contains both `RUSIA` and `BIELORRUSIA O BELARUS`, and substring matching silently picks Belarus.
- **Only 22 of 45 Madrid offices offer TOMA DE HUELLAS.** No district commissariat in Madrid city does; inside the city only `CNP AVDA POBLADOS` and `CNP SAN FELIPE TIE`. Survey cached in `monitor/debug/offices_survey.json`, regenerate with `--discover-offices`.
- The flow includes a contact step (`paso 2 de 5`: phone + two email fields, the second with `noPaste`) that the legacy scripts never reached.
- **Never books.** On a find: local beep, Telegram with screenshot, tab left open on the dates screen.

Commands: `--verify` (slow visible walk, dumps selectors), `--once`, `--offices "a,b"` (one-off sweep), `--discover-offices`, `--test-telegram`. `record.py` records a manual pass for comparison — that is how the behavioural filter was found.

Secrets live in `monitor/.env`; `monitor/config.json`, `state.json`, `logs/`, `debug/`, `chrome-profile/` are gitignored.

## Legacy Architecture (cita-checker*.py)

The original scripts consist of multiple Python modules working together in an asynchronous architecture:

### Core Components

- **cita-checker.py**: Enhanced main automation script with async support and metrics integration
- **cita-checker-stealth.py**: Advanced stealth version with maximum anti-detection measures
- **notification_service.py**: Dual notification system supporting Telegram and Email with attachments
- **metrics.py**: Comprehensive metrics collection and status reporting system
- **values.json**: Extended configuration file with office selection and notification settings
- **test_run.py**: System readiness testing and validation script
- **docker-compose.yml**: Container orchestration with VNC server setup
- **startup.sh**: Enhanced container initialization with Python virtual environment and dependencies

### Enhanced Dependencies

- **SeleniumBase**: Web automation framework with undetected Chrome support
- **Brave Browser**: Specifically `/usr/bin/brave-browser` binary in container for stealth
- **aiohttp**: Asynchronous HTTP client for Telegram API integration
- **asyncio & concurrent.futures**: Python 3.8+ compatible async architecture
- **VNC Server**: Remote GUI access via noVNC web interface
- **SMTP Libraries**: Email notifications with attachment support
- **JSON**: Configuration and metrics persistence

## Development Commands

### System Startup

```bash
# Start the Docker container with enhanced VNC access
docker-compose up

# Access options:
# Web Browser: http://localhost:6080 (noVNC, password: root)
# VNC Client: localhost:5901 (password: root)
```

### Running Different Versions

```bash
# In VNC terminal - Test system readiness
python3 test_run.py

# Standard version with metrics and notifications
python3 cita-checker.py

# Stealth version for bypassing detection
python3 cita-checker-stealth.py
```

### Enhanced Configuration

The application uses an extended `values.json` configuration:

**Core Settings:**
- **url**: Target website URL
- **region**: Geographic region (e.g., "Madrid")
- **office_address**: Specific office selection for appointments
- **tramiteOptionText**: Service type selection (see README.md for full list)
- **idCitadoValue**: ID number
- **desCitadoValue**: Full name
- **TypeID**: Document type (PASAPORTE, NIE, DNI)
- **paisNacValue**: Country of birth

**Notification Settings:**
- **telegram_bot_token**: Telegram bot token for notifications
- **telegram_chat_id**: Telegram chat ID for message delivery
- **receiver_email/sender_email**: Email notification configuration
- **password**: Email app password (Gmail)
- **smtp_server/smtp_port**: SMTP configuration

**System Settings:**
- **check_interval_minutes**: Interval between checks (default: 120)
- **keyboard_layout**: Container keyboard layout

### Enhanced Monitoring System

**Logging:**
- Application logs: `/tmp/events.log` with Russian language support
- Comprehensive error tracking and status reporting
- Real-time console output with emoji indicators

**Metrics Collection:**
- Detailed metrics stored in `/tmp/metrics.json`
- Success rate tracking and response time monitoring
- Error frequency analysis and trend reporting
- Automatic status reports every 4 hours

**Notifications:**
- Screenshots: `/tmp/cita_disponible.png` when appointments found
- Telegram notifications with photo attachments
- Email notifications with screenshot attachments
- Status updates and error alerts

## Enhanced Code Architecture

### Dual Version System

**Standard Version (cita-checker.py):**
- Asynchronous architecture with `asyncio` and `concurrent.futures`
- Python 3.8+ compatible threading approach
- Integrated metrics collection and notification system
- Exponential backoff retry logic (15→30→60→120 minutes)
- Smart office selection before service type selection

**Stealth Version (cita-checker-stealth.py):**
- Maximum anti-detection measures (2-5 minute initial delays)
- Human behavior simulation (slow typing, scrolling, reading pauses)
- Advanced JavaScript injection to hide automation signatures
- Extended intervals (2+ hours) to avoid "too many requests" blocking

### Advanced Browser Management
- SeleniumBase with Brave browser and undetected Chrome
- Random window sizing (800-1600px) and user agent rotation
- JavaScript execution to mask WebDriver properties
- Aggressive process cleanup with enhanced `find_and_kill()`
- Incognito mode with additional privacy arguments

### Intelligent Flow Control
- Async main loop with configurable intervals
- State management with `AppointmentState` dataclass
- Four possible outcomes: "retry", "error", "manual_check_needed", success
- 10-minute manual intervention window when appointments detected
- Graceful shutdown handling with SIGINT/SIGTERM support

### Robust Error Handling
- Exponential backoff on consecutive errors
- Selenium exception handling with detailed logging
- Browser process cleanup on all error conditions
- Metrics tracking for error frequency and patterns
- Automatic recovery from transient failures

## Critical Implementation Details

### Environment Requirements
- Designed for Docker container execution with Ubuntu base
- VNC password: "root" (configurable via environment variables)
- Python virtual environment automatically created in `/home/nonroot/venv`
- Keyboard layout affects container input handling

### Website Integration
- Specific CSS selectors for Spanish government platform:
  - Region selector: `#form`
  - Office selector: `#sede` 
  - Service selector: `#tramiteGrupo[0]`
  - Document type: `#rdbTipoDocPas`
  - Form fields: `#txtIdCitado`, `#txtDesCitado`
- Correct workflow order: Region → Office → Service → Data Entry
- "No appointments" detection: specific Spanish text matching

### Anti-Detection Features
- User-Agent spoofing and WebDriver property masking
- Random delays between all actions (1-8 seconds)
- Human-like behavior simulation in stealth version
- Browser fingerprint reduction techniques
- Process cleanup to prevent detection accumulation

### Python 3.8 Compatibility
- Uses `concurrent.futures.ThreadPoolExecutor` instead of `asyncio.to_thread`
- Compatible with older Docker base images
- Proper async/await patterns for Python 3.8+

### Notification System Architecture
- `NotificationService` class with async methods
- Telegram API integration with photo upload support
- Email SMTP with attachment handling
- Error handling for notification failures
- Status reporting system with metrics integration

## Testing and Debugging

### System Readiness
```bash
# Run comprehensive system tests
python3 test_run.py
```
Tests configuration loading, module imports, service initialization, and Docker setup.

### Common Issues and Solutions

**"Too many requests" blocking:**
- Switch to `cita-checker-stealth.py`
- Increase `check_interval_minutes` to 180+
- Verify VPN usage if required

**Office selection failures:**
- Check `office_address` in `values.json` matches exactly
- Verify CSS selector `#sede` is present on target site
- Use VNC to visually inspect page structure

**Python version compatibility:**
- System tested with Python 3.8.10
- Uses ThreadPoolExecutor for async compatibility
- Avoid `asyncio.to_thread()` which requires Python 3.9+

**Notification failures:**
- Validate Telegram bot token and chat_id
- Test email settings with Gmail app passwords
- Check container network connectivity

### Metrics and Monitoring
- Metrics file: `/tmp/metrics.json`
- Real-time status via Russian language logs
- 4-hour interval status reports
- Success rate and error frequency tracking
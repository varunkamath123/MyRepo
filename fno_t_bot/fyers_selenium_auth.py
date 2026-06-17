"""
Fyers Selenium Auto-Authentication
===================================
Headless browser automation for Fyers API login.
Mirrors the human web login flow: Client ID → WhatsApp OTP → PIN → token.

No HMAC signing, no raw API reverse-engineering — just a real browser.
Works headlessly on AWS EC2 (Chromium).

Requirements:
  pip install selenium undetected-chromedriver pyotp fyers-apiv3

On AWS EC2 (Ubuntu):
  sudo apt-get update
  sudo apt-get install -y chromium-browser chromium-chromedriver
  pip install selenium undetected-chromedriver pyotp

Usage:
  python fyers_selenium_auth.py              # Headless, WhatsApp OTP auto-read (default)
  python fyers_selenium_auth.py --visible    # Show browser window (local debug / first-time WA QR)
  python fyers_selenium_auth.py --check      # Print token validity and exit
  python fyers_selenium_auth.py --force      # Re-auth even if valid token exists
  python fyers_selenium_auth.py --otp 123456 # Provide OTP manually (bypass WA)

WhatsApp Setup (one-time):
  1. Run: python fyers_selenium_auth.py --visible
  2. WhatsApp Web opens in a second tab — scan QR with your phone
  3. Session is saved to live_bot/wa_profile/ — no QR needed on future runs
  4. In Fyers settings, confirm your WhatsApp is the linked 2FA number

Schedule on AWS (08:45 IST Mon–Fri):
  Uses fno_t_bot_auth.timer / fno_t_bot_auth.service in deploy/
"""

from __future__ import annotations

import os
import re
import sys
import time
import logging
from datetime import datetime

import pyotp
import pytz

import config

IST = pytz.timezone('Asia/Kolkata')

logger = logging.getLogger('fyers_selenium_auth')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)

# WhatsApp Web session profile — persists QR scan across runs
WA_PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wa_profile')


# ── Chrome version helper ──────────────────────────────────────────────────────

def _get_chrome_major_version() -> int | None:
    """
    Read the installed Chrome major version number.
    Needed so undetected-chromedriver downloads the matching chromedriver binary.
    Checks Windows Registry first, then falls back to running the binary.
    """
    # Windows Registry paths for Chrome version
    try:
        import winreg
        for hive, path in [
            (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Wow6432Node\Google\Chrome\BLBeacon"),
        ]:
            try:
                key = winreg.OpenKey(hive, path)
                version, _ = winreg.QueryValueEx(key, "version")
                major = int(version.split('.')[0])
                logger.info(f"Chrome version from registry: {version} (major={major})")
                return major
            except Exception:
                continue
    except ImportError:
        pass  # Not on Windows (e.g. AWS Linux)

    # Linux / fallback: run chrome binary with --version
    import subprocess
    chrome_bins = [
        "google-chrome", "google-chrome-stable",
        "chromium-browser", "chromium",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for binary in chrome_bins:
        try:
            result = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=5
            )
            # Output: "Google Chrome 145.0.7632.117" or "Chromium 131.0.6778.264"
            version_str = result.stdout.strip().split()[-1]
            major = int(version_str.split('.')[0])
            logger.info(f"Chrome version from binary '{binary}': {version_str} (major={major})")
            return major
        except Exception:
            continue

    logger.warning("Could not detect Chrome version — undetected-chromedriver will pick latest")
    return None


# ── Driver factory ─────────────────────────────────────────────────────────────

def _get_driver(headless: bool = True, user_data_dir: str | None = None):
    """
    Build a Selenium WebDriver.
    Prefers undetected-chromedriver (bypasses Fyers bot-detection).
    Falls back to plain selenium.ChromeDriver if undetected not installed.

    user_data_dir: path to a Chrome profile directory for session persistence
                   (used to keep WhatsApp Web logged in across runs).
    """
    # Base args — minimal set that works reliably with both uc and plain selenium
    base_args = [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--window-size=1280,900',
    ]
    # Extra args for plain selenium only (uc handles AutomationControlled internally)
    plain_selenium_args = [
        '--disable-gpu',
        '--disable-blink-features=AutomationControlled',
        '--disable-infobars',
    ]

    try:
        import undetected_chromedriver as uc          # type: ignore
        opts = uc.ChromeOptions()
        if headless:
            opts.add_argument('--headless=new')
        for a in base_args:
            opts.add_argument(a)
        # NOTE: do NOT add --disable-blink-features=AutomationControlled here —
        # undetected-chromedriver patches this internally; adding it manually
        # can interfere with UC's stealth and cause Cloudflare Turnstile to fail.
        chrome_ver = _get_chrome_major_version()

        # user_data_dir persists WhatsApp Web session (avoids QR scan every time)
        kwargs = dict(options=opts, version_main=chrome_ver)
        if user_data_dir:
            os.makedirs(user_data_dir, exist_ok=True)
            kwargs['user_data_dir'] = user_data_dir
            logger.info(f"Chrome profile dir: {user_data_dir}")

        driver = uc.Chrome(**kwargs)
        logger.info(f"Using undetected-chromedriver (version_main={chrome_ver})")
        return driver

    except ImportError:
        logger.warning(
            "undetected-chromedriver not found — falling back to plain selenium. "
            "Install with: pip install undetected-chromedriver"
        )
        from selenium import webdriver                # type: ignore
        opts = webdriver.ChromeOptions()
        if headless:
            opts.add_argument('--headless=new')
        for a in base_args + plain_selenium_args:
            opts.add_argument(a)
        if user_data_dir:
            os.makedirs(user_data_dir, exist_ok=True)
            opts.add_argument(f'--user-data-dir={user_data_dir}')
        driver = webdriver.Chrome(options=opts)
        logger.info("Using plain selenium ChromeDriver")
        return driver


# ── Wait helpers ───────────────────────────────────────────────────────────────

def _wait(driver, by, selector, timeout: int = 20):
    from selenium.webdriver.support.ui import WebDriverWait       # type: ignore
    from selenium.webdriver.support import expected_conditions as EC  # type: ignore
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, selector))
    )


def _wait_visible(driver, by, selector, timeout: int = 20):
    """Wait for element to be present AND visible (not hidden by CSS)."""
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    return WebDriverWait(driver, timeout).until(
        EC.visibility_of_element_located((by, selector))
    )


def _wait_clickable(driver, by, selector, timeout: int = 15):
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    return WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, selector))
    )


def _visible_elements(driver, by, selector) -> list:
    """Return all elements matching selector that are currently visible."""
    els = driver.find_elements(by, selector)
    return [e for e in els if e.is_displayed()]


def _click_id(driver, element_id: str, timeout: int = 15) -> bool:
    """
    Click an element by exact id.
    Falls back to JavaScript click if the element is present but not clickable
    (e.g. kept disabled by Cloudflare Turnstile until the captcha token arrives).
    """
    from selenium.webdriver.common.by import By
    try:
        el = _wait_clickable(driver, By.ID, element_id, timeout=timeout)
        el.click()
        return True
    except Exception as exc:
        logger.warning(f"  Normal click #{element_id} failed ({exc}), trying JS click...")
        # JavaScript click bypasses selenium's 'element must be interactable' check
        try:
            el = driver.find_element(By.ID, element_id)
            driver.execute_script("arguments[0].click();", el)
            logger.info(f"  JS click #{element_id} succeeded.")
            return True
        except Exception as exc2:
            logger.warning(f"  JS click #{element_id} also failed: {exc2}")
            return False


def _wait_button_enabled(driver, element_id: str, timeout: int = 20) -> bool:
    """
    Wait until a button's 'disabled' attribute disappears.

    Fyers uses Cloudflare Turnstile (invisible mode) to protect the Client ID
    submit button. The Turnstile runs a background fingerprint check and takes
    ~5 seconds to resolve. Once it passes, the button's disabled="" attribute
    is removed by JavaScript and we can click it normally.

    Returns True if button is enabled within timeout, False otherwise.
    """
    from selenium.webdriver.common.by import By
    logger.info(f"  Waiting for #{element_id} to become enabled (Turnstile ~5s)...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            el = driver.find_element(By.ID, element_id)
            if el.get_attribute("disabled") is None:
                logger.info(f"  #{element_id} is now enabled.")
                return True
        except Exception:
            pass
        time.sleep(0.5)
    logger.warning(f"  #{element_id} still disabled after {timeout}s — will try JS click.")
    return False


# ── Interaction helpers ────────────────────────────────────────────────────────

def _type_into(driver, selector: str, text: str, timeout: int = 15) -> bool:
    """Find a CSS selector and type text into it. Returns True on success."""
    from selenium.webdriver.common.by import By
    try:
        el = _wait(driver, By.CSS_SELECTOR, selector, timeout=timeout)
        el.clear()
        el.send_keys(text)
        return True
    except Exception:
        return False


def _try_selectors(driver, selectors: list, timeout_each: int = 5) -> object:
    """Return the first element found from a list of CSS selectors, or None."""
    from selenium.webdriver.common.by import By
    for sel in selectors:
        try:
            el = _wait(driver, By.CSS_SELECTOR, sel, timeout=timeout_each)
            if el:
                return el
        except Exception:
            continue
    return None


def _submit(driver) -> bool:
    """Click the visible submit / continue / next button."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    submit_selectors = [
        "button[type='submit']",
        "button.btn-primary",
        "button.submit-btn",
        "button.login-btn",
        "button#btn_proceed",
        "input[type='submit']",
        "button:not([disabled])[class*='btn']",
    ]
    for sel in submit_selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els:
                if el.is_displayed() and el.is_enabled():
                    el.click()
                    return True
        except Exception:
            continue

    # Last resort: Enter on focused element
    try:
        driver.switch_to.active_element.send_keys(Keys.RETURN)
        return True
    except Exception:
        return False


# ── Auth URL & token exchange ──────────────────────────────────────────────────

def _get_auth_url() -> str:
    from fyers_apiv3 import fyersModel     # type: ignore
    session = fyersModel.SessionModel(
        client_id    = config.FYERS_APP_ID,
        secret_key   = config.FYERS_SECRET_KEY,
        redirect_uri = config.REDIRECT_URI,
        response_type= "code",
        grant_type   = "authorization_code",
    )
    return session.generate_authcode()


def _exchange_code(auth_code: str) -> str | None:
    from fyers_apiv3 import fyersModel
    session = fyersModel.SessionModel(
        client_id    = config.FYERS_APP_ID,
        secret_key   = config.FYERS_SECRET_KEY,
        redirect_uri = config.REDIRECT_URI,
        response_type= "code",
        grant_type   = "authorization_code",
    )
    session.set_token(auth_code)
    data = session.generate_token()
    if data.get('code') == 200 or data.get('s') == 'ok':
        return data['access_token']
    logger.error(f"Token exchange failed: {data}")
    return None


def _save_token(token: str) -> None:
    """Save token + ISO timestamp to logs/token.txt (same format as fyers_auth.py)."""
    os.makedirs(config.LOG_DIRECTORY, exist_ok=True)
    path = os.path.join(config.LOG_DIRECTORY, 'token.txt')
    with open(path, 'w') as f:
        f.write(token + '\n')
        f.write(datetime.now().isoformat())
    logger.info(f"Token saved → {path}")


# ── Token validity check ───────────────────────────────────────────────────────

def token_is_valid() -> bool:
    """Return True if a same-day token already exists in logs/token.txt."""
    path = os.path.join(config.LOG_DIRECTORY, 'token.txt')
    if not os.path.exists(path):
        return False
    with open(path) as f:
        lines = f.read().strip().split('\n')
    if len(lines) < 2:
        return False
    try:
        token_date = datetime.fromisoformat(lines[1]).date()
        return token_date == datetime.now(IST).date()
    except ValueError:
        return False


# ── WhatsApp Web OTP reader ────────────────────────────────────────────────────

def _setup_whatsapp_tab(driver, fyers_window: str, headless: bool) -> tuple[str | None, str | None]:
    """
    Open WhatsApp Web in a new browser tab and navigate to the Fyers OTP chat.

    Returns (wa_window_handle, last_message_text) so we know what was there
    *before* the new OTP arrives. Returns (None, None) on failure.

    On first run (no persisted profile): QR scan required.
    - If --visible: pause and wait for the user to scan (up to 120s).
    - If --headless: error out — cannot scan QR without display.

    Subsequent runs: profile is loaded from wa_profile/ — no QR needed.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    wa_sender = getattr(config, 'FYERS_WA_SENDER', 'Fyers')
    logger.info(f"WhatsApp: opening WA Web tab (looking for sender: '{wa_sender}')...")

    # ── Open new tab ──────────────────────────────────────────────────────────
    driver.execute_script("window.open('https://web.whatsapp.com', '_blank');")
    time.sleep(1)
    wa_window = [h for h in driver.window_handles if h != fyers_window][-1]
    driver.switch_to.window(wa_window)

    # ── Wait for WhatsApp Web to load ─────────────────────────────────────────
    # Two states: (a) QR code shown (first login), (b) chat list shown (logged in)
    logger.info("  Waiting for WhatsApp Web to load (up to 30s)...")
    try:
        WebDriverWait(driver, 30).until(lambda d: (
            len(d.find_elements(By.CSS_SELECTOR, 'canvas[aria-label*="QR"]')) > 0
            or len(d.find_elements(By.CSS_SELECTOR, '[data-testid="chat-list"]')) > 0
            or len(d.find_elements(By.CSS_SELECTOR, '#pane-side')) > 0
            or len(d.find_elements(By.CSS_SELECTOR, 'div[aria-label="Chat list"]')) > 0
        ))
    except Exception:
        logger.warning("  WhatsApp Web did not load recognisable UI in 30s. Continuing anyway...")

    # ── Handle QR code (first-time setup) ────────────────────────────────────
    qr_selectors = [
        'canvas[aria-label*="QR"]',
        'div[data-ref]',            # WA Web QR container sometimes uses data-ref
        '[data-testid="qrcode"]',
    ]
    qr_found = any(
        len(driver.find_elements(By.CSS_SELECTOR, sel)) > 0
        for sel in qr_selectors
    )

    if qr_found:
        if headless:
            logger.error(
                "WhatsApp QR code detected but running in HEADLESS mode — cannot scan.\n"
                "  Fix: run once with --visible to scan QR and save the session profile:\n"
                f"    python fyers_selenium_auth.py --visible\n"
                f"  Profile saved to: {WA_PROFILE_DIR}"
            )
            driver.switch_to.window(fyers_window)
            return None, None

        # Visible mode — pause for user to scan
        logger.info(
            "\n" + "="*60 + "\n"
            "  WhatsApp QR code is shown in the browser.\n"
            "  Open WhatsApp on your phone → Linked Devices → Link a Device\n"
            "  Scan the QR code now.\n"
            "  Waiting up to 120 seconds...\n"
            + "="*60
        )
        try:
            # Wait until QR disappears (= scan successful)
            WebDriverWait(driver, 120).until(lambda d: not any(
                len(d.find_elements(By.CSS_SELECTOR, sel)) > 0
                for sel in qr_selectors
            ))
            logger.info("  QR scanned — WhatsApp linked successfully!")
            time.sleep(3)  # let chat list render
        except Exception:
            logger.error("  QR scan timed out (120s). WhatsApp not linked.")
            driver.switch_to.window(fyers_window)
            return None, None

    # Handle "Use Here" popup (WA Web already open elsewhere)
    try:
        use_here_btns = driver.find_elements(By.XPATH, '//*[contains(text(),"Use Here")]')
        for btn in use_here_btns:
            if btn.is_displayed():
                logger.info("  WhatsApp 'Use Here' popup detected — clicking...")
                btn.click()
                time.sleep(2)
                break
    except Exception:
        pass

    # ── Wait for chat list ────────────────────────────────────────────────────
    logger.info("  Waiting for chat list...")
    chat_list_selectors = [
        '[data-testid="chat-list"]',
        '#pane-side',
        'div[aria-label="Chat list"]',
    ]
    chat_list_loaded = False
    for sel in chat_list_selectors:
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel))
            )
            chat_list_loaded = True
            logger.info(f"  Chat list loaded ({sel}).")
            break
        except Exception:
            continue

    if not chat_list_loaded:
        logger.warning("  Could not confirm chat list — trying to proceed anyway.")

    time.sleep(2)  # give chat list a moment to fully populate

    # ── Search for Fyers chat ─────────────────────────────────────────────────
    logger.info(f"  Searching for '{wa_sender}' in WhatsApp...")
    search_selectors = [
        'div[data-testid="search-container"] div[contenteditable="true"]',
        'div[contenteditable="true"][data-tab="3"]',
        'div[aria-label="Search input textbox"]',
        'div[title="Search input textbox"]',
        'div[data-testid="chat-list-search"]',
    ]
    search_clicked = False
    for sel in search_selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if el.is_displayed():
                el.click()
                time.sleep(0.5)
                el.send_keys(wa_sender)
                search_clicked = True
                logger.info(f"  Search query entered via: {sel}")
                break
        except Exception:
            continue

    if not search_clicked:
        # Fallback: try clicking the search icon first
        try:
            from selenium.webdriver.common.keys import Keys
            search_icon_sels = [
                'button[aria-label="Search"]',
                'span[data-testid="search"]',
                'div[data-testid="chat-list-search"]',
            ]
            for sel in search_icon_sels:
                try:
                    icon = driver.find_element(By.CSS_SELECTOR, sel)
                    if icon.is_displayed():
                        icon.click()
                        time.sleep(0.5)
                        break
                except Exception:
                    continue
            # Type in active element
            driver.switch_to.active_element.send_keys(wa_sender)
            search_clicked = True
            logger.info("  Search entered via active element fallback.")
        except Exception:
            pass

    if not search_clicked:
        logger.warning(f"  Could not find search box — WhatsApp UI may have changed.")

    time.sleep(2)  # let search results populate

    # ── Click the Fyers chat from search results ──────────────────────────────
    chat_opened = False
    try:
        # Search results show contacts matching query
        result_selectors = [
            f'[data-testid="cell-frame-title"] span[title*="{wa_sender}"]',
            f'span[title*="{wa_sender}"]',
            f'div[title*="{wa_sender}"]',
            f'//span[contains(@title, "{wa_sender}")]',
        ]
        for sel in result_selectors:
            try:
                if sel.startswith('//'):
                    el = driver.find_element(By.XPATH, sel)
                else:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                if el.is_displayed():
                    el.click()
                    chat_opened = True
                    logger.info(f"  Opened '{wa_sender}' chat.")
                    break
            except Exception:
                continue
    except Exception:
        pass

    if not chat_opened:
        # Just press Enter to open first result
        try:
            from selenium.webdriver.common.keys import Keys
            driver.switch_to.active_element.send_keys(Keys.RETURN)
            chat_opened = True
            logger.info("  Opened first search result (Enter key).")
        except Exception:
            logger.warning(f"  Could not open '{wa_sender}' chat — OTP polling will search all visible messages.")

    time.sleep(2)  # let messages render

    # ── Record the last message text (our baseline) ───────────────────────────
    last_msg_text = _get_latest_wa_message(driver)
    logger.info(f"  Baseline message (before OTP): {last_msg_text!r}")

    # Switch back to Fyers tab
    driver.switch_to.window(fyers_window)
    return wa_window, last_msg_text


def _get_latest_wa_message(driver) -> str:
    """
    Extract the text of the most recent WhatsApp message in the open chat.
    Returns empty string if none found.
    """
    from selenium.webdriver.common.by import By

    msg_selectors = [
        # WA Web message bubbles (incoming)
        'div.message-in span.selectable-text',
        'div[data-testid="msg-container"] span.selectable-text',
        # Broader: any selectable-text span
        'span.selectable-text.copyable-text',
        # Fallback: all copyable spans
        '[data-testid="msg-container"] span[dir="ltr"]',
    ]
    for sel in msg_selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            visible = [e for e in els if e.is_displayed() and e.text.strip()]
            if visible:
                # Last one = most recent message
                return visible[-1].text.strip()
        except Exception:
            continue
    return ''


def _poll_whatsapp_otp(driver, wa_window: str, fyers_window: str,
                        baseline_text: str, timeout: int = 60) -> str | None:
    """
    Switch to WhatsApp Web tab and poll for a new message containing a 6-digit OTP.

    Compares against baseline_text (message before OTP was triggered).
    Returns the 6-digit OTP string, or None if not found within timeout.

    Strategy:
    - Every 2 seconds, read the latest message
    - If it differs from baseline, extract 6-digit code with regex
    - Return the code immediately
    """
    from selenium.webdriver.common.by import By

    logger.info("WhatsApp: switching to WA tab to wait for OTP message...")
    driver.switch_to.window(wa_window)

    deadline = time.time() + timeout
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        latest = _get_latest_wa_message(driver)

        if latest and latest != baseline_text:
            # New message arrived — hunt for 6-digit number
            otp_match = re.search(r'\b(\d{6})\b', latest)
            if otp_match:
                otp = otp_match.group(1)
                logger.info(f"  OTP found in WhatsApp message: {otp}  (message: {latest!r})")
                driver.switch_to.window(fyers_window)
                return otp
            else:
                # Message changed but no 6-digit number — could be read-receipt
                # Keep polling; might not be the OTP message yet
                logger.debug(f"  Message changed but no 6-digit OTP yet: {latest!r}")
                # Update baseline to this new non-OTP message
                baseline_text = latest

        remaining = int(deadline - time.time())
        if attempt % 5 == 0:
            logger.info(f"  Waiting for Fyers OTP on WhatsApp... ({remaining}s remaining)")

        time.sleep(2)

    logger.error(f"  No WhatsApp OTP found within {timeout}s.")
    driver.switch_to.window(fyers_window)
    return None


# ── Main selenium auth flow ────────────────────────────────────────────────────

def selenium_authenticate(
    headless: bool = True,
    manual_otp: str = "",
    use_whatsapp: bool = False,
) -> str | None:
    """
    Full Fyers login automation via headless Chromium.

    OTP resolution order:
      1. manual_otp (--otp flag) — highest priority
      2. WhatsApp Web auto-read (default when FYERS_WA_SENDER is set)
      3. TOTP via pyotp (if FYERS_TOTP_KEY is set and WA is disabled)
      4. Interactive prompt (fallback — will block on AWS)

    Fyers login page structure (confirmed from live page inspection):
      - All steps are pre-rendered as hidden sections in the DOM (SPA).
      - Radio buttons control which login method is shown.
      - Client ID field: input#fy_client_id
      - OTP: 6 × input.otp-field  (ids: first/second/.../sixth)
      - PIN:  4 × input.pin-field.fy-secure-input
      - Submit buttons have specific IDs at each step.

    Flow:
      1. [Optional] Pre-load WhatsApp Web in a second tab
      2. Navigate to Fyers auth URL in tab 1
      3. Select 'Client ID' radio → fill input#fy_client_id → click #clientIdSubmit
      4. [If WhatsApp mode] Switch to WA tab, wait for OTP message, extract code
      5. Fill 6 OTP digits into input.otp-field fields → click #confirmOtpSubmit
      6. Fill 4 PIN digits into input.pin-field fields → click #verifyPinSubmit
      7. Capture auth_code from redirect URL (127.0.0.1:8000?auth_code=...)
      8. Exchange auth_code → access token → save to logs/token.txt

    Returns access token string, or None on failure.
    """
    from selenium.webdriver.common.by import By

    pin      = config.FYERS_PIN
    totp_key = config.FYERS_TOTP_KEY

    if not pin:
        logger.error("FYERS_PIN must be set in .env for selenium auth.")
        return None

    auth_url = _get_auth_url()
    logger.info(f"Auth URL: {auth_url[:80]}...")

    # Use persistent profile so WhatsApp Web stays logged in
    profile_dir = WA_PROFILE_DIR if use_whatsapp and not manual_otp else None
    driver = _get_driver(headless=headless, user_data_dir=profile_dir)

    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    def visible_of(selector):
        """Return all currently-visible elements matching CSS selector."""
        els = driver.find_elements(By.CSS_SELECTOR, selector)
        return [e for e in els if e.is_displayed()]

    def wait_visible_count(selector, count, timeout=10):
        """Wait until at least `count` elements matching selector are visible."""
        WebDriverWait(driver, timeout).until(
            lambda d: len(visible_of(selector)) >= count
        )
        return visible_of(selector)

    wa_window = None
    wa_baseline = ''

    try:
        # ── Step 0: Pre-load WhatsApp Web (if enabled) ─────────────────────────
        if use_whatsapp and not manual_otp:
            logger.info("Step 0: Pre-loading WhatsApp Web in background tab...")
            # Open Fyers auth URL first so we have the fyers window handle
            driver.get(auth_url)
            fyers_window = driver.current_window_handle
            time.sleep(2)  # let page start loading

            wa_window, wa_baseline = _setup_whatsapp_tab(driver, fyers_window, headless)
            if wa_window is None:
                logger.warning(
                    "WhatsApp tab setup failed. Falling back to TOTP/manual OTP."
                )
            else:
                logger.info("  WhatsApp tab ready. Switching back to Fyers tab.")

            # Already on Fyers window from _setup_whatsapp_tab
            # Let page finish loading
            time.sleep(2)
            logger.info(f"  Fyers page title: {driver.title!r}")
        else:
            # No WhatsApp — just open Fyers directly
            driver.get(auth_url)
            fyers_window = driver.current_window_handle
            time.sleep(4)   # let page JS + Cloudflare initialise
            logger.info(f"  Title: {driver.title!r}")

        # ── Step 1 (already done above if WA) / Step 1: Ensure Fyers page loaded
        # (already navigated above)

        # ── Step 2: Select Client ID login method ─────────────────────────────
        logger.info("Step 2: Selecting Client ID login method...")
        radio = driver.find_element(By.ID, "clientId_rb")
        if not radio.is_selected():
            radio.click()
            time.sleep(0.5)
        logger.info("  'Client ID' radio selected.")

        # ── Step 3: Enter Client ID ───────────────────────────────────────────
        logger.info(f"Step 3: Entering Client ID ({config.FYERS_CLIENT_ID})...")
        cid_field = driver.find_element(By.ID, "fy_client_id")
        cid_field.clear()
        cid_field.send_keys(config.FYERS_CLIENT_ID)
        logger.info("  Client ID entered.")

        # Wait for Turnstile background check to enable the button (~5–10s)
        _wait_button_enabled(driver, "clientIdSubmit", timeout=30)

        # Direct click — confirmed working in debug tests
        driver.find_element(By.ID, "clientIdSubmit").click()
        logger.info("  Submitted Client ID. Fyers will now send WhatsApp OTP.")

        # ── Step 4: Get OTP ───────────────────────────────────────────────────
        logger.info("Step 4: Waiting for OTP screen...")
        try:
            otp_fields = wait_visible_count("input.otp-field", 6, timeout=15)
        except Exception:
            otp_fields = []

        if len(otp_fields) < 6:
            logger.error(f"  OTP screen not reached (found {len(otp_fields)} otp-fields). "
                         "Client ID may have been rejected.")
            return None

        # Resolve OTP — generate/fetch at the LAST moment before filling digits
        # to minimise the gap between code generation and submission (TOTP expires in 30s).
        if manual_otp:
            # 1) Manually provided via --otp flag
            otp_code = manual_otp.strip()
            logger.info(f"  Using manual OTP: {otp_code}")

        elif wa_window:
            # 2) Auto-read from WhatsApp Web
            logger.info("  OTP screen visible. Switching to WhatsApp to read OTP...")
            time.sleep(3)  # give Fyers a moment to send the WhatsApp message
            otp_code = _poll_whatsapp_otp(
                driver, wa_window, fyers_window,
                baseline_text=wa_baseline,
                timeout=60,
            )
            if not otp_code:
                logger.warning("  WhatsApp OTP not found. Falling back to interactive input.")
                otp_code = input("  Enter WhatsApp OTP (6 digits): ").strip()

        elif totp_key:
            # 3) TOTP — use NTP time for accuracy, wait for safe window position.
            # NTP eliminates system clock drift. Wait if near boundary (<8s left).
            try:
                import ntplib
                ntp_time = ntplib.NTPClient().request('pool.ntp.org', version=3).tx_time
                logger.debug(f"  NTP offset: {ntp_time - time.time():+.2f}s")
            except Exception:
                ntp_time = time.time()  # fallback to system clock
            elapsed = int(ntp_time) % 30
            remaining = 30 - elapsed
            if remaining <= 20:  # danger zone: ≤20s left — wait for a fresh window
                wait_secs = remaining + 3  # skip to 3s into next window
                logger.info(f"  TOTP window danger zone ({remaining}s left) — waiting {wait_secs}s for fresh window...")
                time.sleep(wait_secs)
                ntp_time = time.time()
            otp_code = pyotp.TOTP(totp_key).at(ntp_time)
            remaining = 30 - (int(ntp_time) % 30)
            logger.info(f"  TOTP code: {otp_code}  ({remaining}s remaining in window)")

        else:
            # 4) Interactive fallback
            logger.info("  OTP has been sent. Enter it now:")
            otp_code = input("  Enter OTP (6 digits): ").strip()
            logger.info(f"  Manual OTP entered: {otp_code}")

        if not otp_code or len(otp_code) < 6:
            logger.error(f"  OTP is invalid: {otp_code!r}")
            return None

        # Fill digits immediately after generating code — no delays between
        for i, digit in enumerate(otp_code[:6]):
            otp_fields[i].clear()
            otp_fields[i].send_keys(digit)
            time.sleep(0.05)
        logger.info("  OTP entered.")

        # Use JS click directly — button is already confirmed enabled above.
        # Avoid _click_id's 15s _wait_clickable which can eat into the 30s TOTP window.
        _wait_button_enabled(driver, "confirmOtpSubmit", timeout=5)
        driver.execute_script(
            "arguments[0].click();",
            driver.find_element(By.ID, "confirmOtpSubmit")
        )
        logger.info("  Submitted OTP.")

        # ── Step 5: Enter PIN (4 split digit inputs) ──────────────────────────
        logger.info("Step 5: Waiting for PIN screen...")
        try:
            pin_fields = wait_visible_count("input.pin-field", 4, timeout=15)
        except Exception:
            pin_fields = []

        if len(pin_fields) < 4:
            # Dump page state to help diagnose why PIN screen wasn't reached
            try:
                logger.error(f"  Current URL: {driver.current_url}")
                # Look for any error text visible on screen
                err_selectors = [
                    '.error-msg', '.alert', '.invalid-feedback',
                    '[class*="error"]', '[class*="alert"]', '[id*="error"]',
                    'p.text-danger', 'span.text-danger',
                ]
                for sel in err_selectors:
                    els = [e for e in driver.find_elements(By.CSS_SELECTOR, sel)
                           if e.is_displayed() and e.text.strip()]
                    for e in els:
                        logger.error(f"  Page error text ({sel}): {e.text.strip()!r}")
                # Save a snippet of page source for inspection
                src_snippet = driver.page_source[2000:5000]
                debug_path = os.path.join(os.path.dirname(__file__), 'fyers_pin_debug.html')
                with open(debug_path, 'w', encoding='utf-8') as f:
                    f.write(driver.page_source)
                logger.error(f"  Full page source saved to: {debug_path}")
            except Exception as dbg_exc:
                logger.error(f"  Debug dump failed: {dbg_exc}")
            logger.error(f"  PIN screen not reached (found {len(pin_fields)} pin-fields). "
                         "OTP may have been rejected.")
            return None

        for i, digit in enumerate(str(pin)[:4]):
            pin_fields[i].click()   # focus field — fy-secure-input rejects .clear()
            pin_fields[i].send_keys(digit)
            time.sleep(0.10)
        logger.info(f"  PIN entered ({len(str(pin))} digits).")

        _wait_button_enabled(driver, "verifyPinSubmit", timeout=10)
        driver.execute_script(
            "arguments[0].click();",
            driver.find_element(By.ID, "verifyPinSubmit")
        )
        logger.info("  Submitted PIN. Waiting for redirect...")

        # ── Step 6: Capture auth_code from redirect URL ───────────────────────
        # Fyers redirects to http://127.0.0.1:8000?auth_code=XXX&state=...
        # The page shows ERR_CONNECTION_REFUSED but the URL is readable.
        auth_code = None
        for attempt in range(30):
            current_url = driver.current_url
            if 'auth_code=' in current_url:
                auth_code = current_url.split('auth_code=')[1].split('&')[0]
                logger.info(f"  auth_code captured: {auth_code[:20]}...")
                break
            # Rare fallback: embedded in page source
            src = driver.page_source
            if 'auth_code' in src:
                m = re.search(r'auth_code=([A-Za-z0-9_\-]+)', src)
                if m:
                    auth_code = m.group(1)
                    logger.info(f"  auth_code from page source: {auth_code[:20]}...")
                    break
            if attempt % 5 == 0:
                logger.info(f"  [{attempt+1}/30] Waiting... URL: {current_url[-50:]}")
            time.sleep(1)

        if not auth_code:
            logger.error(
                f"No auth_code after 30s. Final URL: {driver.current_url}\n"
                "  Possible causes: wrong PIN, OTP rejected, network issue."
            )
            return None

        # ── Step 7: Exchange auth_code for access token ───────────────────────
        logger.info("Step 7: Exchanging auth_code for access token...")
        token = _exchange_code(auth_code)
        if token:
            _save_token(token)
            logger.info("Selenium auth complete. Token saved.")
            return token

        return None

    except Exception as exc:
        logger.exception(f"Unexpected error during selenium auth: {exc}")
        return None

    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Fyers headless browser auto-authentication'
    )
    parser.add_argument(
        '--visible', action='store_true',
        help='Show browser window (required for first-time WhatsApp QR scan)'
    )
    parser.add_argument(
        '--check', action='store_true',
        help='Print whether today\'s token is valid and exit'
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Re-authenticate even if a valid token already exists'
    )
    parser.add_argument(
        '--otp', metavar='CODE',
        help='Provide the 6-digit WhatsApp OTP manually (bypasses WA Web automation)'
    )
    parser.add_argument(
        '--whatsapp', action='store_true',
        help='Enable WhatsApp Web OTP automation (opt-in; default is TOTP via pyotp)'
    )
    args = parser.parse_args()

    if args.check:
        valid = token_is_valid()
        print(f"Token valid today: {valid}")
        sys.exit(0 if valid else 1)

    if not args.force and token_is_valid():
        print("Valid token already exists for today. Skipping auth. (Use --force to override.)")
        sys.exit(0)

    headless     = not args.visible
    manual_otp   = args.otp or ""
    # WhatsApp OTP mode: opt-in only (--whatsapp flag)
    # Default: TOTP via pyotp (fully automated, no phone dependency)
    use_whatsapp = not args.no_whatsapp if hasattr(args, 'no_whatsapp') and args.no_whatsapp else False
    if hasattr(args, 'whatsapp') and args.whatsapp:
        use_whatsapp = True

    logger.info(
        f"Starting Fyers selenium auth  "
        f"[headless={headless}, otp_mode={'manual' if manual_otp else 'whatsapp' if use_whatsapp else 'totp'}]"
    )

    token = selenium_authenticate(
        headless=headless,
        manual_otp=manual_otp,
        use_whatsapp=use_whatsapp,
    )
    if token:
        print("Authentication successful. Token saved to logs/token.txt")
        sys.exit(0)
    else:
        print(
            "Selenium auth failed.\n"
            "  -> Run: python fyers_selenium_auth.py --visible\n"
            "     (shows browser, lets you scan WhatsApp QR on first run)\n"
            "  -> Check FYERS_CLIENT_ID, FYERS_PIN are correct in .env\n"
            "  -> If WhatsApp OTP times out, try: --otp <code>"
        )
        sys.exit(1)

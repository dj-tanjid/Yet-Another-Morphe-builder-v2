# ---------------------------------------------------------
# Copyright (C) 2026 krvstek
# Copyright (C) 2026 TanJid Creations
# 
# DO NOT REMOVE OR ALTER THIS COPYRIGHT HEADER.
# This file is part of uni-apks.
# Canonical source: https://github.com/krvstek/uni-apks
#
# Licensed under the GNU GPLv3. You may modify this file,
# but you MUST keep this original copyright notice intact
# and prominently state any changes made.
# See the AUTHORS file in the root directory for details.
# ---------------------------------------------------------

import json
import os
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests
from curl_cffi.requests import exceptions as req_exc

from src.core.config import TEMP_DIR
from src.core.logger import epr

_RETRY_DELAYS = (3, 6, 9)
_MAX_ATTEMPTS = len(_RETRY_DELAYS) + 1

_BROWSERS = (
    "chrome124", 
    "chrome120", 
    "safari17_0", 
    "safari15_5", 
    "edge101", 
    "chrome116"
)

class NetworkError(Exception):
    pass

class ResourceNotFoundError(NetworkError):
    pass

def _get_lock(locks: dict, mu: threading.Lock, key) -> threading.Lock:
    with mu:
        return locks.setdefault(key, threading.Lock())

def _retry_sleep(attempt: int) -> None:
    if attempt <= len(_RETRY_DELAYS):
        time.sleep(_RETRY_DELAYS[attempt - 1] + random.uniform(1.0, 3.5))

def _handle_status(resp, url: str, attempt: int) -> bool:
    if resp.status_code in (404, 410):
        raise ResourceNotFoundError(f"Not found ({resp.status_code}): {url}")

    is_cf_challenge = False
    if resp.text:
        text_lower = resp.text.lower()
        if "cf-browser-verification" in text_lower or "just a moment" in text_lower or "attention required" in text_lower:
            is_cf_challenge = True

    if resp.status_code in (403, 503) or resp.status_code >= 500 or is_cf_challenge:
        epr(f"HTTP {resp.status_code} (CF_Challenge: {is_cf_challenge}) for {url}, attempt {attempt}/{_MAX_ATTEMPTS}")
        return True

    if resp.status_code >= 400:
        resp.raise_for_status()
    return False

class NetworkManager:
    def __init__(self) -> None:
        self.cookie_jar = TEMP_DIR / "cookies.json"
        self.browser_cfg = TEMP_DIR / "browser.txt"
        
        self.local = threading.local()
        
        token = os.getenv("GITHUB_TOKEN")
        self._gh_headers: dict[str, str] = {"Authorization": f"token {token}"} if token else {}
        self._domain_locks: dict[str, threading.Lock] = {}
        self._domain_mu = threading.Lock()
        self._dest_locks: dict[Path, threading.Lock] = {}
        self._dest_mu = threading.Lock()
        self._cf_lock = threading.RLock()

    def _create_session(self, browser: str) -> requests.Session:
        sess = requests.Session(impersonate=browser)
        sess.headers.update({
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1"
        })
        return sess

    def _get_session(self):
        if getattr(self.local, "session", None) is None:
            browser = "chrome124"
            if self.browser_cfg.exists():
                try: browser = self.browser_cfg.read_text().strip()
                except: pass
            self.local.browser = browser
            self.local.session = self._create_session(browser)
            self._load_state(self.local.session)
        return self.local.session

    def _save_state(self, session) -> None:
        with self._cf_lock:
            try:
                TEMP_DIR.mkdir(parents=True, exist_ok=True)
                self.browser_cfg.write_text(self.local.browser)
                self.cookie_jar.write_text(json.dumps(session.cookies.get_dict()))
            except Exception:
                pass

    def _load_state(self, session) -> None:
        with self._cf_lock:
            try:
                if self.cookie_jar.exists():
                    cookies = json.loads(self.cookie_jar.read_text())
                    for k, v in cookies.items():
                        session.cookies.set(k, v)
            except Exception:
                pass

    def _clear_state(self) -> None:
        with self._cf_lock:
            try:
                if self.browser_cfg.exists(): self.browser_cfg.unlink()
                if self.cookie_jar.exists(): self.cookie_jar.unlink()
            except Exception:
                pass

    def _bypass_cloudflare_with_playwright(self, url: str, session) -> bool:
        """Boot a visible browser inside Xvfb to solve Cloudflare Turnstile visually."""
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import Stealth
        except ImportError:
            return False

        with self._cf_lock:
            epr("Initiating Playwright Stealth (Headless=False) bypass...")
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=False, 
                        args=[
                            "--disable-blink-features=AutomationControlled", 
                            "--disable-gpu", 
                            "--no-sandbox",
                            "--disable-web-security"
                        ]
                    )
                    context = browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        user_agent=getattr(self.local, "session", self._create_session("chrome124")).headers.get("User-Agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    )
                    Stealth().apply_stealth_sync(context)
                    page = context.new_page()
                    
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass 

                    start_time = time.time()
                    challenge_cleared = False
                    
                    while time.time() - start_time < 25: 
                        content = page.content()
                        title = page.title()
                        
                        if "Just a moment" not in title and "cf-browser-verification" not in content and "Attention Required" not in title:
                            epr("Cloudflare challenge cleared.")
                            challenge_cleared = True
                            break
                            
                        try:
                            for frame in page.frames:
                                if "challenges.cloudflare.com" in frame.url:
                                    box = frame.locator('input[type="checkbox"], .ctp-checkbox-label, #challenge-stage').first
                                    if box.is_visible():
                                        box.click(force=True)
                        except Exception:
                            pass
                        
                        page.wait_for_timeout(1500)
                        
                    if not challenge_cleared:
                        epr("Playwright timeout exceeded, moving on.")
                        browser.close()
                        return False
                    
                    for c in context.cookies():
                        session.cookies.set(c["name"], c["value"], domain=c["domain"])
                    
                    ua = page.evaluate("navigator.userAgent")
                    session.headers.update({"User-Agent": ua})
                    
                    browser.close()
                    self._save_state(session)
                    return True
            except Exception as e:
                epr(f"Playwright bypass failed or timed out: {e}")
                return False

    def _rotate_browser(self, url: str) -> bool:
        """Safely tears down the thread's blocked session and rotates fingerprints globally."""
        with self._cf_lock:
            self._clear_state()
            
            try:
                self.local.session.close()
            except Exception:
                pass
                
            available_browsers = [b for b in _BROWSERS if b != getattr(self.local, "browser", "chrome124")]
            self.local.browser = random.choice(available_browsers)
            self.local.session = self._create_session(self.local.browser)
            
            success = self._bypass_cloudflare_with_playwright(url, self.local.session)
            if success:
                self._save_state(self.local.session)
            return success

    def get(self, url: str, headers: dict[str, str] | None = None) -> str:
        netloc = urlparse(url).netloc
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            sess = self._get_session()
            try:
                with _get_lock(self._domain_locks, self._domain_mu, netloc):
                    time.sleep(random.uniform(1.0, 2.5))
                    resp = sess.get(url, timeout=(10, 25), allow_redirects=True, headers=headers, verify=True)

                if _handle_status(resp, url, attempt):
                    success = self._rotate_browser(url)
                    if not success and attempt >= 3:
                        raise NetworkError(f"Cloudflare hard-blocked Datacenter IP for {url}. Aborting retries.")
                    _retry_sleep(attempt)
                    self._load_state(self.local.session)
                    continue

                self._save_state(sess)
                return resp.text
            except ResourceNotFoundError:
                raise
            except req_exc.RequestException as exc:
                last_exc = exc
                epr(f"Request error for {url}, attempt {attempt}/{_MAX_ATTEMPTS}: {exc}")
                success = self._rotate_browser(url)
                if not success and attempt >= 3:
                    raise NetworkError(f"Cloudflare hard-blocked Datacenter IP for {url}. Aborting retries.")
                _retry_sleep(attempt)
                self._load_state(self.local.session)
        raise NetworkError(f"Request failed after {_MAX_ATTEMPTS} attempts: {url}") from last_exc

    def download(self, url: str, dest: Path, headers: dict[str, str] | None = None) -> None:
        if dest.exists():
            return

        with _get_lock(self._dest_locks, self._dest_mu, dest):
            if dest.exists(): return

            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f"tmp.{dest.name}")
            tmp.unlink(missing_ok=True)
            netloc = urlparse(url).netloc
            last_exc: Exception | None = None
            
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                sess = self._get_session()
                try:
                    with _get_lock(self._domain_locks, self._domain_mu, netloc):
                        time.sleep(random.uniform(1.0, 3.0))
                        resp = sess.get(url, timeout=(10, 300), stream=True, allow_redirects=True, headers=headers, verify=True)

                    if _handle_status(resp, url, attempt):
                        success = self._rotate_browser(url)
                        if not success and attempt >= 3:
                            raise NetworkError(f"Cloudflare hard-blocked Datacenter IP for {url}. Aborting retries.")
                        _retry_sleep(attempt)
                        self._load_state(self.local.session)
                        continue

                    with tmp.open("wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1048576):
                            fh.write(chunk)
                    
                    self._save_state(sess)
                    tmp.replace(dest)
                    return
                except ResourceNotFoundError:
                    raise
                except req_exc.RequestException as exc:
                    tmp.unlink(missing_ok=True)
                    last_exc = exc
                    epr(f"Download error for {url}, attempt {attempt}/{_MAX_ATTEMPTS}: {exc}")
                    success = self._rotate_browser(url)
                    if not success and attempt >= 3:
                        raise NetworkError(f"Cloudflare hard-blocked Datacenter IP for {url}. Aborting retries.")
                    _retry_sleep(attempt)
                    self._load_state(self.local.session)
            raise NetworkError(f"Download failed after {_MAX_ATTEMPTS} attempts: {url}") from last_exc

    def __enter__(self) -> "NetworkManager":
        return self

    def __exit__(self, *_: object) -> None:
        try:
            self.local.session.close()
        except Exception:
            pass

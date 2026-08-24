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

_RETRY_DELAYS = (2, 4, 6)
_MAX_ATTEMPTS = len(_RETRY_DELAYS) + 1
_SOLVER_URL = os.getenv("CF_SOLVER_URL", "http://localhost:8000")

_BROWSERS = (
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
        time.sleep(_RETRY_DELAYS[attempt - 1] + random.uniform(0.5, 1.5))

def _is_challenge(resp) -> bool:
    if resp.status_code in (403, 503):
        body = (resp.text or "").lower()
        if "just a moment" in body or "cf-browser-verification" in body or "attention required" in body:
            return True
    return False

def _handle_status(resp, url: str, attempt: int) -> bool:
    if resp.status_code in (404, 410):
        raise ResourceNotFoundError(f"Not found ({resp.status_code}): {url}")

    if _is_challenge(resp) or resp.status_code in (401, 429) or resp.status_code >= 500:
        epr(f"HTTP {resp.status_code} for {url}, attempt {attempt}/{_MAX_ATTEMPTS}")
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

    def __enter__(self) -> "NetworkManager":
        return self

    def __exit__(self, *_: object) -> None:
        try:
            if hasattr(self.local, "session") and self.local.session:
                self.local.session.close()
        except Exception:
            pass

    def _create_session(self, browser: str) -> requests.Session:
        sess = requests.Session(impersonate=browser)
        sess.headers.update({
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1"
        })
        return sess

    def _get_session(self) -> requests.Session:
        if getattr(self.local, "session", None) is None:
            browser = "chrome120"
            if self.browser_cfg.exists():
                try:
                    browser = self.browser_cfg.read_text().strip()
                except Exception:
                    pass
            self.local.browser = browser
            self.local.session = self._create_session(browser)
            self._load_state(self.local.session)
        return self.local.session

    def _save_state(self, session: requests.Session) -> None:
        with self._cf_lock:
            try:
                TEMP_DIR.mkdir(parents=True, exist_ok=True)
                self.browser_cfg.write_text(self.local.browser)
                self.cookie_jar.write_text(json.dumps(session.cookies.get_dict()))
            except Exception:
                pass

    def _load_state(self, session: requests.Session) -> None:
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
                if self.browser_cfg.exists():
                    self.browser_cfg.unlink()
                if self.cookie_jar.exists():
                    self.cookie_jar.unlink()
            except Exception:
                pass

    def _solve_via_docker_service(self, url: str) -> bool:
        """Attempts fast resolution using the local container solver."""
        with self._cf_lock:
            try:
                epr(f"Solving Cloudflare challenge for {url} via local API...")
                resp = requests.get(f"{_SOLVER_URL}/cookies", params={"url": url}, timeout=60)
                if resp.status_code != 200:
                    epr(f"Solver API returned HTTP {resp.status_code}")
                    return False

                data = resp.json()
                cookies = data.get("cookies", {})
                user_agent = data.get("user_agent")

                if not cookies or not user_agent:
                    epr("Solver API returned invalid payload")
                    return False

                self._clear_state()
                try:
                    self.local.session.close()
                except Exception:
                    pass

                # Force Chrome 120 matching the solver
                self.local.browser = "chrome120"
                sess = self._create_session("chrome120")
                
                if isinstance(cookies, dict):
                    for k, v in cookies.items():
                        sess.cookies.set(k, v)
                elif isinstance(cookies, list):
                    for c in cookies:
                        if isinstance(c, dict) and "name" in c and "value" in c:
                            sess.cookies.set(c["name"], c["value"])

                sess.headers["User-Agent"] = user_agent
                
                self.local.session = sess
                self._save_state(sess)
                epr("Cloudflare bypass cookies synchronized successfully.")
                return True
            except Exception as exc:
                epr(f"Container solver failed: {exc}")
                return False

    def get(self, url: str, headers: dict[str, str] | None = None) -> str:
        netloc = urlparse(url).netloc
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            sess = self._get_session()
            try:
                with _get_lock(self._domain_locks, self._domain_mu, netloc):
                    time.sleep(random.uniform(0.5, 1.5))
                    resp = sess.get(url, timeout=(15, 45), allow_redirects=True, headers=headers, verify=True)

                if _is_challenge(resp):
                    self._solve_via_docker_service(url)
                    _retry_sleep(attempt)
                    continue

                if _handle_status(resp, url, attempt):
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
                _retry_sleep(attempt)
                self._load_state(self.local.session)
        raise NetworkError(f"Request failed after {_MAX_ATTEMPTS} attempts: {url}") from last_exc

    def download(self, url: str, dest: Path, headers: dict[str, str] | None = None) -> None:
        if dest.exists():
            return

        with _get_lock(self._dest_locks, self._dest_mu, dest):
            if dest.exists():
                return

            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f"tmp.{dest.name}")
            tmp.unlink(missing_ok=True)
            netloc = urlparse(url).netloc
            last_exc: Exception | None = None
            
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                sess = self._get_session()
                try:
                    with _get_lock(self._domain_locks, self._domain_mu, netloc):
                        time.sleep(random.uniform(0.5, 2.0))
                        resp = sess.get(url, timeout=(15, 120), stream=True, allow_redirects=True, headers=headers, verify=True)

                    if _is_challenge(resp):
                        self._solve_via_docker_service(url)
                        _retry_sleep(attempt)
                        continue

                    if _handle_status(resp, url, attempt):
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
                    _retry_sleep(attempt)
                    self._load_state(self.local.session)
            raise NetworkError(f"Download failed after {_MAX_ATTEMPTS} attempts: {url}") from last_exc

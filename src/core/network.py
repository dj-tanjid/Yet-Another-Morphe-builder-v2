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


class NetworkError(Exception):
    pass


class ResourceNotFoundError(NetworkError):
    pass


class NetworkManager:
    def __init__(self) -> None:
        self.cookie_jar = TEMP_DIR / "cookies.json"
        self.browser_cfg = TEMP_DIR / "browser.txt"
        self.local = threading.local()
        self._cf_lock = threading.RLock()
        self._domain_locks: dict[str, threading.Lock] = {}
        self._domain_mu = threading.Lock()
        self._dest_locks: dict[Path, threading.Lock] = {}
        self._dest_mu = threading.Lock()

    def _create_session(self, browser: str = "chrome124") -> requests.Session:
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

    def _get_session(self) -> requests.Session:
        if getattr(self.local, "session", None) is None:
            self.local.browser = "chrome124"
            self.local.session = self._create_session(self.local.browser)
            self._load_state(self.local.session)
        return self.local.session

    def _save_state(self, session: requests.Session) -> None:
        with self._cf_lock:
            try:
                TEMP_DIR.mkdir(parents=True, exist_ok=True)
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

    def _solve_via_docker_service(self, url: str) -> bool:
        """Attempts fast resolution using the local container solver."""
        try:
            resp = requests.get(f"{_SOLVER_URL}/cookies", params={"url": url}, timeout=30)
            if resp.status_code != 200:
                return False

            data = resp.json()
            cookies = data.get("cookies", {})
            user_agent = data.get("user_agent")

            sess = self._get_session()
            if isinstance(cookies, dict):
                for k, v in cookies.items():
                    sess.cookies.set(k, v)
            elif isinstance(cookies, list):
                for c in cookies:
                    if isinstance(c, dict) and "name" in c and "value" in c:
                        sess.cookies.set(c["name"], c["value"])

            if user_agent:
                sess.headers["User-Agent"] = user_agent

            self._save_state(sess)
            return True
        except Exception as exc:
            epr(f"Container solver failed for {url}: {exc}")
            return False

    def _solve_via_playwright(self, url: str) -> bool:
        """Full browser solver using Playwright under Xvfb."""
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import Stealth

            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=False,
                    args=["--no-sandbox", "--disable-gpu", "--disable-blink-features=AutomationControlled"]
                )
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=self._get_session().headers.get("User-Agent")
                )
                Stealth().apply_stealth_sync(context)
                page = context.new_page()

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass

                start = time.time()
                cleared = False
                while time.time() - start < 25:
                    if "Just a moment" not in page.title() and "cf-browser-verification" not in page.content():
                        cleared = True
                        break
                    for frame in page.frames:
                        if "challenges.cloudflare.com" in frame.url:
                            box = frame.locator('input[type="checkbox"], .ctp-checkbox-label').first
                            if box.is_visible():
                                box.click(force=True)
                    page.wait_for_timeout(1000)

                if not cleared:
                    browser.close()
                    return False

                sess = self._get_session()
                for c in context.cookies():
                    sess.cookies.set(c["name"], c["value"], domain=c["domain"])
                sess.headers["User-Agent"] = page.evaluate("navigator.userAgent")

                browser.close()
                self._save_state(sess)
                return True
        except Exception as exc:
            epr(f"Playwright bypass failed: {exc}")
            return False

    def get(self, url: str, headers: dict[str, str] | None = None) -> str:
        netloc = urlparse(url).netloc
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            sess = self._get_session()
            try:
                with self._domain_locks.setdefault(netloc, threading.Lock()):
                    time.sleep(random.uniform(0.8, 2.0))
                    resp = sess.get(url, timeout=(10, 25), allow_redirects=True, headers=headers)

                if resp.status_code in (404, 410):
                    raise ResourceNotFoundError(f"Not found ({resp.status_code}): {url}")

                if resp.status_code in (403, 503) or "cf-browser-verification" in (resp.text or "").lower():
                    epr(f"Challenge encountered on {url} (attempt {attempt}/{_MAX_ATTEMPTS})")
                    if not self._solve_via_docker_service(url):
                        self._solve_via_playwright(url)
                    continue

                if resp.status_code >= 400:
                    resp.raise_for_status()

                self._save_state(sess)
                return resp.text
            except ResourceNotFoundError:
                raise
            except req_exc.RequestException as exc:
                epr(f"Request error for {url}: {exc}")
                time.sleep(_RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)])
        raise NetworkError(f"Failed to fetch {url} after {_MAX_ATTEMPTS} attempts")

    def download(self, url: str, dest: Path, headers: dict[str, str] | None = None) -> None:
        if dest.exists():
            return

        with self._dest_locks.setdefault(dest, threading.Lock()):
            if dest.exists():
                return

            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f"tmp.{dest.name}")
            sess = self._get_session()

            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    resp = sess.get(url, timeout=(10, 300), stream=True, allow_redirects=True, headers=headers)
                    if resp.status_code in (403, 503):
                        if not self._solve_via_docker_service(url):
                            self._solve_via_playwright(url)
                        continue

                    resp.raise_for_status()
                    with tmp.open("wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1048576):
                            fh.write(chunk)
                    tmp.replace(dest)
                    return
                except Exception as exc:
                    tmp.unlink(missing_ok=True)
                    epr(f"Download attempt {attempt} failed for {url}: {exc}")
                    time.sleep(2)
            raise NetworkError(f"Failed to download {url}")

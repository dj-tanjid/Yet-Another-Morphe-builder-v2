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

import json  # noqa: I001
import re
import time
import random
from pathlib import Path

from src.core.logger import epr
from src.core.network import NetworkManager, ResourceNotFoundError
from src.scrapers.base import AppMetadata, BaseScraper, DownloadResult, ScraperError, _parse_html

_DEFAULT_ARCH: frozenset[str] = frozenset({"arm64-v8a, armeabi-v7a, x86_64", "arm64-v8a, armeabi-v7a, x86, x86_64", "arm64-v8a, armeabi-v7a"})


class UptodownError(ScraperError):
    pass

class UptodownScraper(BaseScraper):
    def __init__(self, net: NetworkManager) -> None:
        super().__init__(net)
        self._datacode_cache: dict[str, str] = {}

    def fetch_metadata(self, url: str) -> AppMetadata:
        try:
            pkg_html = self.net.get(f"{url}/download")
        except ResourceNotFoundError:
            pkg_html = self.net.get(url)

        soup_pkg = _parse_html(pkg_html)
        pkg_name = None
        
        th = soup_pkg.find("th", string=re.compile("Package Name", re.I))
        if th and (td := th.find_next_sibling("td")):
            pkg_name = td.get_text(strip=True)
            
        if not pkg_name:
            gp = soup_pkg.find("a", href=re.compile(r"play\.google\.com/store/apps/details\?id="))
            if gp:
                m = re.search(r"id=([a-zA-Z0-9_.]+)", gp.get("href", ""))
                if m: pkg_name = m.group(1)
                
        if not pkg_name:
            meta = soup_pkg.find("meta", property="al:android:package")
            if meta: pkg_name = meta.get("content")

        if not pkg_name:
            raise UptodownError("Package name not found")

        detail_app = soup_pkg.find(attrs={"data-code": True})
        if detail_app:
            data_code = str(detail_app["data-code"])
        else:
            m = re.search(r'data-code=["\'](\d+)["\']', pkg_html)
            if m:
                data_code = m.group(1)
            else:
                raise UptodownError("App data-code not found")
            
        self._datacode_cache[url] = data_code
        versions = []

        try:
            resp_text = self.net.get(f"{url}/apps/{data_code}/versions/1")
            payload = json.loads(resp_text)
            for entry in payload.get("data", []):
                if v := entry.get("version"):
                    versions.append(str(v))
            if not versions:
                raise ValueError("Empty JSON versions list")
        except Exception:
            try:
                versions_html = self.net.get(f"{url}/versions")
                soup_v = _parse_html(versions_html)
                for v_tag in soup_v.select(".version"):
                    if v_text := v_tag.get_text(strip=True):
                        versions.append(v_text)
            except Exception as e:
                raise UptodownError(f"Failed to fetch versions from API or HTML fallback: {e}")

        return AppMetadata(pkg_name=pkg_name, versions=versions)

    def _extract_download_link(self, html_text: str, soup) -> str | None:
        for tag in soup.find_all(True):
            if tag.has_attr("data-url"):
                val = tag["data-url"].strip()
                if "dw.uptodown" in val or "core.uptodown" in val or val.startswith("/dwn/"):
                    if val.startswith("http"): return val
                    if val.startswith("/dwn/"): return f"https://dw.uptodown.net{val}"
                    return f"https://dw.uptodown.com/dwn/{val}"
            
            if tag.name == "a" and tag.has_attr("href"):
                val = tag["href"].strip()
                if "dw.uptodown" in val or "core.uptodown" in val or val.startswith("/dwn/"):
                    if val.startswith("http"): return val
                    if val.startswith("/dwn/"): return f"https://dw.uptodown.net{val}"

        match = re.search(r'(https://(?:dw|core)\.uptodown\.(?:com|net)/dwn/[A-Za-z0-9_/\-+=.]+)', html_text)
        if match: return match.group(1)

        match = re.search(r'data-url=["\']([^"\']+)["\']', html_text)
        if match:
            val = match.group(1).strip()
            if "dw.uptodown" in val or "core.uptodown" in val or val.startswith("/dwn/"):
                if val.startswith("http"): return val
                if val.startswith("/dwn/"): return f"https://dw.uptodown.net{val}"
                return f"https://dw.uptodown.com/dwn/{val}"
        return None

    def _extract_with_playwright(self, url: str) -> str | None:
        """Fallback: Boots a visible browser, solves Turnstile, clicks the button, and captures the CDN redirect URL."""
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import Stealth
            
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=False, 
                    args=["--no-sandbox", "--disable-gpu", "--disable-blink-features=AutomationControlled"]
                )
                
                # Enforce strict Windows Desktop profile to bypass Uptodown's mobile app trap
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    is_mobile=False,
                    has_touch=False,
                    extra_http_headers={
                        "Sec-Ch-Ua-Mobile": "?0",
                        "Sec-Ch-Ua-Platform": '"Windows"'
                    }
                )
                Stealth().apply_stealth_sync(context)
                
                cookies = [{"name": k, "value": v, "domain": ".uptodown.com", "path": "/"} for k, v in self.net._get_session().cookies.get_dict().items()]
                if cookies:
                    context.add_cookies(cookies)
                    
                page = context.new_page()
                found_url = None

                # Intercept the network request to steal the CDN URL instead of downloading it in Playwright
                def handle_request(req):
                    nonlocal found_url
                    if req.method == "GET":
                        if "/dwn/" in req.url and ("uptodown.com" in req.url or "uptodown.net" in req.url):
                            found_url = req.url
                        elif req.url.endswith(".apk") or req.url.endswith(".xapk") or req.url.endswith(".apkm"):
                            found_url = req.url

                page.on("request", handle_request)
                context.on("page", lambda new_page: new_page.on("request", handle_request))
                
                epr(f"[*] Navigating Playwright to {url}...")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                
                # Pre-click Cloudflare Turnstile Bypass
                start_time = time.time()
                while time.time() - start_time < 15:
                    try:
                        for frame in page.frames:
                            if "challenges.cloudflare.com" in frame.url:
                                box = frame.locator('input[type="checkbox"], .ctp-checkbox-label').first
                                if box.is_visible():
                                    box.click(force=True)
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                
                # Dismiss Privacy / Cookie Consent
                try:
                    page.locator("#purposes-agree").click(timeout=3000)
                except Exception:
                    pass

                # Target the big green button
                try:
                    btn = page.locator('#detail-download-button, button.download, .button.download, button:has-text("Download")').first
                    btn.wait_for(state="attached", timeout=10000)
                except Exception:
                    # If button not found (e.g. dead variant URL), strip ID and fall back to the base download page
                    if "/download/" in page.url:
                        base_dl_url = page.url.split("/download/")[0] + "/download"
                        epr(f"[*] Button not found. Falling back to base page: {base_dl_url}")
                        page.goto(base_dl_url, wait_until="domcontentloaded", timeout=15000)
                        btn = page.locator('#detail-download-button, button.download, .button.download, button:has-text("Download")').first
                        btn.wait_for(state="attached", timeout=15000)
                    else:
                        raise

                try:
                    btn.scroll_into_view_if_needed()
                    epr("[*] Button found. Clicking to extract CDN URL...")
                    
                    # Physical click to trigger trusted events
                    btn.click(delay=100, force=True)
                    
                    # Wait for the network interceptor to catch the URL
                    wait_start = time.time()
                    while time.time() - wait_start < 25:
                        if found_url:
                            epr(f"[+] Extracted CDN URL: {found_url}")
                            break
                        
                        # Catch Turnstile popups that trigger AFTER clicking download
                        try:
                            for frame in page.frames:
                                if "challenges.cloudflare.com" in frame.url:
                                    box = frame.locator('input[type="checkbox"], .ctp-checkbox-label').first
                                    if box.is_visible():
                                        box.click(force=True)
                        except Exception:
                            pass
                            
                        page.wait_for_timeout(1000)
                        
                except Exception as e:
                    epr(f"Playwright btn click failed: {e}")
                
                browser.close()
                return found_url
        except Exception as e:
            epr(f"Playwright Uptodown fallback failed: {e}")
            
        return None

    def download(self, url: str, version: str, dest: Path, arch: str, dpi: str) -> DownloadResult:
        apparch: set[str] = set(_DEFAULT_ARCH)
        if arch != "all":
            apparch.add(arch)

        data_code = self._datacode_cache.get(url)
        if not data_code:
            try:
                soup_main = _parse_html(self.net.get(url))
                detail_app = soup_main.select_one("#detail-app-name")
                data_code = str(detail_app["data-code"])
                self._datacode_cache[url] = data_code
            except Exception:
                raise UptodownError("App data-code not found")

        version_url_data = self._find_version_url(url, data_code, version)
        
        v_id = version_url_data.get("versionID")
        if v_id:
            page_url = f"{url}/download/{v_id}"
        else:
            page_url = f"{url}/download"

        is_bundle = version_url_data.get("kindFile") in ("xapk", "apkm")
        
        try:
            resp = self.net.get(page_url)
        except ResourceNotFoundError:
            page_url = f"{url}/download"
            resp = self.net.get(page_url)
            
        soup_ver = _parse_html(resp)
        btn_variants = soup_ver.select_one(".button.variants")
        
        if btn_variants and (data_version := btn_variants.get("data-version")):
            page_url, is_bundle = self._pick_variant_url(url, data_code, str(data_version), apparch)
            try:
                resp = self.net.get(page_url)
                soup_ver = _parse_html(resp)
            except ResourceNotFoundError:
                pass 

        out_path = dest.with_suffix(".apkm") if is_bundle else dest
        final_url = self._extract_download_link(resp, soup_ver)

        if final_url:
            if "play.google.com" in final_url:
                raise UptodownError("APK is externally hosted on Google Play")
            self.net.download(final_url, out_path)
            return DownloadResult(path=out_path, is_bundle=is_bundle)

        epr(f"DEBUG: Static URL extraction failed. Running Playwright natively on {page_url}...")
        
        final_url = self._extract_with_playwright(page_url)
        
        if not final_url:
            Path(f"uptodown_error_{dest.stem}.html").write_text(resp, encoding="utf-8")
            raise UptodownError("Playwright URL hook failed or URL attribute not found")
            
        if "play.google.com" in final_url:
            raise UptodownError("APK is externally hosted on Google Play")

        # Hand the extracted CDN URL back to NetworkManager
        self.net.download(final_url, out_path)
        return DownloadResult(path=out_path, is_bundle=is_bundle)

    def _find_version_url(self, url: str, data_code: str, version: str) -> dict:
        for i in range(1, 21):
            try:
                resp_text = self.net.get(f"{url}/apps/{data_code}/versions/{i}")
                payload = json.loads(resp_text)
            except Exception:
                continue
                
            data = payload.get("data")
            if not data:
                break
                
            for entry in data:
                # Fuzzy match to account for API differences
                if version.lower() not in str(entry.get("version", "")).lower():
                    continue
                ver_url_dict = entry.get("versionURL") or {}
                return ver_url_dict | {"kindFile": entry.get("kindFile", "")}
        raise UptodownError("Version not found")

    def _pick_variant_url(self, url: str, data_code: str, data_version: str, apparch: set[str]) -> tuple[str, bool]:
        base_url = url.rsplit("/", 1)[0]
        files_html = json.loads(self.net.get(f"{base_url}/app/{data_code}/version/{data_version}/files")).get("content", "")
        soup = _parse_html(files_html)
        content = soup.select_one(".content")
        if not content:
            raise UptodownError("No content container found for variants")

        candidates: list[tuple[str, bool]] = []
        node_arch = ""
        for child in content.children:
            if not getattr(child, "name", None): continue
            if "variant" not in child.get("class", []):
                node_arch = child.get_text(strip=True)
                continue
            if not node_arch or node_arch not in apparch: continue
            file_type_tag = child.select_one(".v-file > span")
            is_bundle = file_type_tag.get_text(strip=True) == "xapk" if file_type_tag else False
            v_report = child.select_one(".v-report")
            if v_report is None: continue
            file_id = v_report.get("data-file-id")
            if file_id is None: continue
            candidates.append((file_id, is_bundle))

        if not candidates:
            raise UptodownError("No matching variant found")

        for file_id, is_bundle in candidates:
            if not is_bundle:
                return f"{url}/download/{file_id}", False

        file_id, is_bundle = candidates[0]
        return f"{url}/download/{file_id}", is_bundle

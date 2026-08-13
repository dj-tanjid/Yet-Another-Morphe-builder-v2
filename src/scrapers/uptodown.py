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

        detail_app = soup_pkg.select_one("#detail-app-name")
        if not detail_app or "data-code" not in detail_app.attrs:
            m = re.search(r'data-code=["\'](\d+)["\']', pkg_html)
            if m:
                data_code = m.group(1)
            else:
                raise UptodownError("App data-code not found")
        else:
            data_code = str(detail_app["data-code"])
            
        self._datacode_cache[url] = data_code

        versions = []
        try:
            payload = json.loads(self.net.get(f"{url}/apps/{data_code}/versions/1"))
            for entry in payload.get("data", []):
                if v := entry.get("version"):
                    versions.append(str(v))
        except Exception:
            raise UptodownError("Failed to fetch versions from API")

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
        """Fallback: Loads page headlessly, clicks the download button, and sniffs the network for the /dwn/ link."""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
                context = browser.new_context(viewport={"width": 1920, "height": 1080})
                page = context.new_page()
                
                found_urls = []
                
                def handle_request(request):
                    if "/dwn/" in request.url and ("uptodown.com" in request.url or "uptodown.net" in request.url):
                        found_urls.append(request.url)
                        
                page.on("request", handle_request)
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                
                try:
                    btn = page.locator('#detail-download-button, #button-group-download > div, button.download').first
                    btn.wait_for(state="visible", timeout=5000)
                    btn.click(timeout=5000)
                except Exception:
                    pass
                
                page.wait_for_timeout(4000)
                browser.close()
                
                if found_urls:
                    return found_urls[0]
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
        page_url = "/".join((str(version_url_data.get("url", "")), str(version_url_data.get("extraURL", "")), str(version_url_data.get("versionID", ""))))
        is_bundle = version_url_data.get("kindFile") == "xapk"
        
        resp = self.net.get(page_url)
        soup_ver = _parse_html(resp)
        btn_variants = soup_ver.select_one(".button.variants")
        
        if btn_variants and (data_version := btn_variants.get("data-version")):
            page_url, is_bundle = self._pick_variant_url(url, data_code, str(data_version), apparch)
            try:
                resp = self.net.get(page_url)
            except ResourceNotFoundError:
                page_url = page_url.replace("-x", "")
                resp = self.net.get(page_url)
            soup_ver = _parse_html(resp)

        final_url = self._extract_download_link(resp, soup_ver)

        # Dynamic Button Fallback
        if not final_url:
            epr(f"DEBUG: Static URL extraction failed. Running Playwright on {page_url}...")
            final_url = self._extract_with_playwright(page_url)

        if not final_url:
            Path(f"uptodown_error_{dest.stem}.html").write_text(resp, encoding="utf-8")
            raise UptodownError("Download URL attribute not found or APK is externally hosted")
            
        if "play.google.com" in final_url:
            raise UptodownError("APK is externally hosted on Google Play")

        out_path = dest.with_suffix(".apkm") if is_bundle else dest
        self.net.download(final_url, out_path)
        return DownloadResult(path=out_path, is_bundle=is_bundle)

    def _find_version_url(self, url: str, data_code: str, version: str) -> dict:
        for i in range(1, 21):
            payload = json.loads(self.net.get(f"{url}/apps/{data_code}/versions/{i}"))
            data = payload.get("data")
            if not data:
                break
            for entry in data:
                if entry.get("version") != version:
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
                return f"{url}/download/{file_id}-x", False

        file_id, is_bundle = candidates[0]
        return f"{url}/download/{file_id}-x", is_bundle

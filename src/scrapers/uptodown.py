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
            # Fallback to main page if /download is 410 Gone or 404
            pkg_html = self.net.get(url)

        soup_pkg = _parse_html(pkg_html)
        
        # Extract data-code to query the API
        detail_app = soup_pkg.select_one("#detail-app-name")
        if not detail_app or "data-code" not in detail_app.attrs:
            raise UptodownError("App data-code not found")
                
        data_code = str(detail_app["data-code"])
        self._datacode_cache[url] = data_code

        # Query the JSON API for the first page of versions
        versions = []
        api_pkg_name = None
        try:
            payload = json.loads(self.net.get(f"{url}/apps/{data_code}/versions/1"))
            for entry in payload.get("data", []):
                if v := entry.get("version"):
                    versions.append(str(v))
                if not api_pkg_name and entry.get("packagename"):
                    api_pkg_name = entry.get("packagename")
        except Exception:
            raise UptodownError("Failed to fetch versions from API")

        # Try to find package name in HTML first
        pkg_name = None
        th = soup_pkg.find("th", string=re.compile("Package Name", re.I))
        if th and (td := th.find_next_sibling("td")):
            pkg_name = td.get_text(strip=True)
            
        if not pkg_name:
            pkg_name = api_pkg_name

        if not pkg_name:
            match = re.search(r'play\.google\.com/store/apps/details\?id=([a-zA-Z0-9_.]+)', pkg_html)
            if match:
                pkg_name = match.group(1)

        if not pkg_name:
            raise UptodownError("Package name not found")

        return AppMetadata(pkg_name=pkg_name, versions=versions)

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
        ver_url = "/".join((str(version_url_data.get("url", "")), str(version_url_data.get("extraURL", "")), str(version_url_data.get("versionID", ""))))
        is_bundle = version_url_data.get("kindFile") == "xapk"
        
        resp = self.net.get(ver_url)
        soup_ver = _parse_html(resp)
        btn_variants = soup_ver.select_one(".button.variants")
        if btn_variants and (data_version := btn_variants.get("data-version")):
            resp, is_bundle = self._pick_variant_file(url, data_code, str(data_version), apparch)
            soup_ver = _parse_html(resp)

        final_url = None
        dl_btn = soup_ver.select_one("#detail-download-button")
        
        if dl_btn:
            dl_url = dl_btn.get("data-url")
            if dl_url and len(dl_url) > 10 and dl_url != "apps":
                final_url = f"https://dw.uptodown.com/dwn/{dl_url}"
            else:
                final_url = dl_btn.get("href")

        # Aggressive Fallback: Regex scan for Uptodown CDN download links (.net or .com)
        if not final_url:
            match = re.search(r'(https://dw\.uptodown\.(?:com|net)/dwn/[^\s"\'<>]+)', resp)
            if match:
                final_url = match.group(1)
            else:
                match = re.search(r'data-url=["\']([^"\']{20,})["\']', resp)
                if match:
                    final_url = f"https://dw.uptodown.com/dwn/{match.group(1)}"

        if not final_url:
            raise UptodownError("Download URL attribute not found on button or in HTML")

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

    def _pick_variant_file(self, url: str, data_code: str, data_version: str, apparch: set[str]) -> tuple[str, bool]:
        base_url = url.rsplit("/", 1)[0]
        files_html = json.loads(self.net.get(f"{base_url}/app/{data_code}/version/{data_version}/files")).get("content", "")
        soup = _parse_html(files_html)
        content = soup.select_one(".content")
        if not content:
            raise UptodownError("No content container found for variants")

        candidates: list[tuple[str, bool]] = []
        node_arch = ""
        for child in content.children:
            if not getattr(child, "name", None):
                continue

            if "variant" not in child.get("class", []):
                node_arch = child.get_text(strip=True)
                continue

            if not node_arch or node_arch not in apparch:
                continue

            file_type_tag = child.select_one(".v-file > span")
            is_bundle = file_type_tag.get_text(strip=True) == "xapk" if file_type_tag else False
            v_report = child.select_one(".v-report")
            if v_report is None:
                continue

            file_id = v_report.get("data-file-id")
            if file_id is None:
                continue

            candidates.append((file_id, is_bundle))

        if not candidates:
            raise UptodownError("No matching variant found")

        for file_id, is_bundle in candidates:
            if not is_bundle:
                return self.net.get(f"{url}/download/{file_id}-x"), False

        file_id, is_bundle = candidates[0]
        return self.net.get(f"{url}/download/{file_id}-x"), is_bundle

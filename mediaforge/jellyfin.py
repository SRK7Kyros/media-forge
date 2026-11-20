"""Talking to Jellyfin, and never letting it break a run.

An empty API key disables the whole subsystem: the clone tree must stay fully
usable with Jellyfin stopped, uninstalled or unconfigured.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class JellyfinError(RuntimeError):
    """Jellyfin answered with an error or was unreachable."""


@dataclass(frozen=True, slots=True)
class LibraryRef:
    name: str
    item_id: str
    collection_type: str


class HttpJellyfinClient:
    def __init__(self, base_url: str, api_key: str = "", timeout_s: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _request(self, path: str, *, method: str = "GET", auth: bool = True) -> bytes:
        body = b"" if method != "GET" else None
        request = urllib.request.Request(
            self.base_url + path, method=method, data=body
        )
        request.add_header("Accept", "application/json")
        if auth:
            if not self.api_key:
                raise JellyfinError("MF_JELLYFIN_API_KEY is empty")
            # 12.4 rejects `X-Emby-Token` with a 401; this scheme is what both
            # 12.x and 10.x accept.
            request.add_header("Authorization", f'MediaBrowser Token="{self.api_key}"')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise JellyfinError(f"{method} {path} -> HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise JellyfinError(f"{method} {path} -> {exc}") from exc

    def available(self) -> bool:
        try:
            payload = self._request("/System/Info/Public", auth=False)
        except JellyfinError:
            return False
        return b"Version" in payload or b"version" in payload

    def libraries(self) -> list[LibraryRef]:
        payload = json.loads(self._request("/Library/VirtualFolders") or b"[]")
        found: list[LibraryRef] = []
        for entry in payload:
            item_id = entry.get("ItemId") or entry.get("Id") or ""
            found.append(
                LibraryRef(
                    name=str(entry.get("Name", "")),
                    item_id=str(item_id),
                    collection_type=str(entry.get("CollectionType", "") or ""),
                )
            )
        return found

    def refresh(self, library: str | None = None) -> bool:
        if library:
            query = (
                "?metadataRefreshMode=Default&imageRefreshMode=Default"
                "&replaceAllMetadata=false&recursive=true"
            )
            self._request(f"/Items/{library}/Refresh{query}", method="POST")
        else:
            self._request("/Library/Refresh", method="POST")
        return True

    def wait_idle(self, timeout_s: float) -> bool:
        """True when no library scan is still running. Never raises."""
        deadline = timeout_s
        interval = 2.0
        waited = 0.0
        while waited < deadline:
            try:
                tasks = json.loads(self._request("/ScheduledTasks") or b"[]")
            except JellyfinError:
                return False
            busy = [
                task
                for task in tasks
                if str(task.get("State", "Idle")) != "Idle"
                and "scan" in str(task.get("Key", "")).lower()
                + str(task.get("Name", "")).lower()
            ]
            if not busy:
                return True
            sleep(interval)
            waited += interval
        return False


def sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)

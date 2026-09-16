"""Resolve sequence assets through the server-provided resolver endpoint."""

import json
from pathlib import Path
import shutil
import urllib.request


CACHE_ROOT = Path.home() / "VSEInstructorCache" / "resolved"


class AssetResolver:
    """Resolve and cache media and fonts for one generation."""

    def __init__(self, instruction, log=None, extra_headers=None):
        self.log = log
        self.instruction = instruction or {}
        sequence = self.instruction.get("sequence") or self.instruction
        resolver = self.instruction.get("resolver") or {}
        self.url = resolver.get("url") or sequence.get("resolver_url")
        self.headers = {
            "Content-Type": "application/json",
            **(resolver.get("headers") or {}),
            **(extra_headers or {}),
        }
        self.generation_id = (
            (self.instruction.get("generation") or {}).get("_id")
            or "anon"
        )
        self.timeline_id = (
            sequence.get("_id") or self.instruction.get("timeline_id")
        )
        self.cache_root = CACHE_ROOT / str(self.generation_id).replace(":", "_")
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def set_generation(self, generation):
        self.generation_id = (generation or {}).get("_id") or "anon"
        self.cache_root = CACHE_ROOT / str(self.generation_id).replace(":", "_")
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, kind, asset_id, filename):
        safe_id = str(asset_id).replace(":", "_")
        return self.cache_root / kind / safe_id / (filename or "asset.bin")

    def _ask(self, payload):
        if not self.url:
            if self.log:
                self.log.error("[resolver] resolver_url missing")
            return None
        request = urllib.request.Request(
            url=self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=dict(self.headers),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    def _download(self, file_url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(file_url, timeout=120) as response:
            destination.write_bytes(response.read())
        return destination

    def resolve(self, *, kind, asset_id, filename=None, mime=None):
        if not asset_id:
            return None

        requested = self._cache_path(kind, asset_id, filename)
        if requested.exists() and requested.stat().st_size > 0:
            if self.log:
                self.log.info(f"[resolver] cache hit {requested}")
            return requested

        try:
            response = self._ask({
                "kind": kind,
                "id": str(asset_id),
                "filename": filename,
                "mime": mime,
                "generation_id": self.generation_id,
                "timeline_id": self.timeline_id,
            })
        except Exception as exc:
            if self.log:
                self.log.error(f"[resolver] request failed {kind}/{asset_id}: {exc}")
            return None

        if not response or not response.get("ok"):
            if self.log:
                self.log.error(f"[resolver] not ok {kind}/{asset_id}: {response}")
            return None

        data = response.get("data") or {}
        file_url = data.get("url")
        if not file_url:
            if self.log:
                self.log.error(f"[resolver] missing data.url for {kind}/{asset_id}")
            return None

        destination = self._cache_path(
            kind,
            asset_id,
            data.get("filename") or filename or f"{asset_id}.bin",
        )
        if destination.exists() and destination.stat().st_size > 0:
            return destination

        try:
            self._download(file_url, destination)
            if self.log:
                self.log.info(f"[resolver] downloaded {kind}/{asset_id} -> {destination}")
            return destination
        except Exception as exc:
            if self.log:
                self.log.error(f"[resolver] download failed {kind}/{asset_id}: {exc}")
            if destination.exists():
                destination.unlink(missing_ok=True)
            return None

    def clear_cache(self):
        if not self.cache_root.exists():
            return
        try:
            shutil.rmtree(self.cache_root)
            if self.log:
                self.log.info(f"[resolver] cleared cache {self.cache_root}")
        except Exception as exc:
            if self.log:
                self.log.warning(f"[resolver] cache clear failed: {exc}")

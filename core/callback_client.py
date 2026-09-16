"""
Callback client for VSE workers.

The job instruction carries where results should go. This class
reads that block and posts status / media / completion.

No editor_url lives in the addon. Upload and media registration
go to whatever URLs the caller puts in `callback`.
"""

from pathlib import Path
from datetime import datetime, timezone
import json
import base64
import math
import uuid
import urllib.request


class CallbackClient:
    """
    Expected instruction shape:

        {
          "sequence": { ... },
          "generation": { "_id": "gen_..." },
          "callback": {
            "status_url":    "https://…/update_generation_status",
            "upload_url":    "https://…/upload_media",
            "add_media_url": "https://…/add_media",
            "complete_url":  "https://…/generation_complete",
            "headers": { "Authorization": "Bearer …" }   # optional
          }
        }

    All result endpoints must be supplied in `callback`. The worker's
    server URL is reserved for polling and is never used here.
    """

    def __init__(self, instruction, log=None):
        self.instruction = instruction or {}
        self.log = log

        self.generation = self.instruction.get("generation")
        if self.generation is None and self.instruction.get("_id"):
            self.generation = {"_id": self.instruction.get("_id")}

        cb = self.instruction.get("callback") or {}

        self.callback = {
            "status_url": cb.get("status_url"),
            "upload_url": cb.get("upload_url"),
            "add_media_url": cb.get("add_media_url"),
            "complete_url": cb.get("complete_url"),
            "read_upload_url": cb.get("read_upload_url"),
            "headers": {
                "Content-Type": "application/json",
                **(cb.get("headers") or {}),
            },
        }

        if not hasattr(self, "machine_id"):
            self.machine_id = "unknown"

        missing = [
            name for name in ("status_url", "upload_url", "add_media_url", "complete_url")
            if not self.callback.get(name)
        ]
        if missing and self.log:
            self.log.warning(
                f"[callback] missing URLs (will fail when used): {missing}"
            )

    # -------------------------------------------------------------------------
    # HTTP
    # -------------------------------------------------------------------------

    def iso_now(self):
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def _post_json(self, url, payload, timeout=60):
        if not url:
            if self.log:
                self.log.error(
                    f"POST skipped — no URL for keys={list(payload.keys())}"
                )
            return {"ok": False, "error": "callback URL missing"}

        if self.log:
            self.log.info(f"POST {url}")

        try:
            req = urllib.request.Request(
                url=url,
                data=json.dumps(payload, default=str).encode("utf-8"),
                headers=dict(self.callback["headers"]),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception as e:
            if self.log:
                self.log.error(f"POST FAILED {url}: {e}")
            return {"ok": False, "error": str(e)}

    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------

    def update_server_status(self, status, extra=None):
        if not self.generation:
            if self.log:
                self.log.warning(
                    f"update_server_status skipped ({status}): no generation"
                )
            return None

        payload = {
            "_id": self.generation.get("_id"),
            "status": status,
            "time": self.iso_now(),
            "machine": getattr(self, "machine_id", "unknown"),
        }
        if extra:
            payload.update(extra)

        return self._post_json(self.callback["status_url"], payload)

    # -------------------------------------------------------------------------
    # Resolved timeline (clip placements)
    # -------------------------------------------------------------------------

    def build_resolved_timeline_payload(self):
        resolver = getattr(self, "timeline", None) or getattr(
            self, "timeline_resolver", None
        )
        fps = getattr(self, "fps", 24)

        clips_out = []
        if resolver is not None:
            for clip_id, obj in (getattr(resolver, "clips", {}) or {}).items():
                clips_out.append(
                    {
                        "clip_id": clip_id,
                        "track_id": obj.track_id,
                        "editorial_scene": (
                            (obj.clip or {}).get("editorial_scene")
                            if obj.clip
                            else None
                        ),
                        "start_ms": int(obj.start),
                        "end_ms": int(obj.end),
                        "duration_ms": int(obj.duration),
                        "start_frame": int(round(obj.start * fps / 1000)),
                        "end_frame": int(round(obj.end * fps / 1000)),
                        "duration_frames": int(round(obj.duration * fps / 1000)),
                        "local_start_ms": int(obj.local_start),
                        "local_end_ms": int(obj.local_end),
                        "local_duration_ms": int(obj.local_duration),
                        "resolved": bool(obj.resolved),
                    }
                )

        scenes_out = []
        if resolver is not None:
            for scene_id, scene in (
                getattr(resolver, "editorial_scenes", {}) or {}
            ).items():
                scenes_out.append(
                    {
                        "editorial_scene_id": scene_id,
                        "start_ms": int(scene.start),
                        "end_ms": int(scene.end),
                        "duration_ms": int(scene.duration),
                        "local_start_ms": int(scene.local_start),
                        "local_end_ms": int(scene.local_end),
                        "local_duration_ms": int(scene.local_duration),
                        "resolved": bool(scene.resolved),
                    }
                )

        global_scene = None
        if resolver is not None and getattr(resolver, "scene", None):
            s = resolver.scene
            global_scene = {
                "start_ms": int(s.start),
                "end_ms": int(s.end),
                "duration_ms": int(s.duration),
            }

        seq = self.instruction.get("sequence") or {}
        return {
            "fps": fps,
            "generation_id": (self.generation or {}).get("_id"),
            "timeline_id": seq.get("_id") or self.instruction.get("timeline_id"),
            "global_scene": global_scene,
            "editorial_scenes": scenes_out,
            "clips": clips_out,
            "clip_count": len(clips_out),
            "built_at": self.iso_now(),
        }

    # -------------------------------------------------------------------------
    # Render upload (callback URLs only)
    # -------------------------------------------------------------------------

    def upload_rendered_media(self, chunk_size=2 * 1024 * 1024):
        import bpy

        scene = bpy.context.scene
        filepath = Path(scene.render.filepath)

        if not filepath.exists():
            if self.log:
                self.log.error(f"Render file does not exist: {filepath}")
            return None

        total_size = filepath.stat().st_size
        if total_size <= 0:
            if self.log:
                self.log.error("Render file is empty")
            return None

        total_chunks = math.ceil(total_size / chunk_size)
        media_id = str(uuid.uuid4())

        title = self.instruction.get("name", "<unk>")
        description = self.instruction.get("description", "")
        user = self.instruction.get("editor", "<unk>")

        with open(filepath, "rb") as f:
            for index in range(total_chunks):
                chunk_bytes = f.read(chunk_size)
                encoded = base64.b64encode(chunk_bytes).decode("utf-8")

                response = self._post_json(
                    self.callback["upload_url"],
                    {
                        "media_id": media_id,
                        "chunk": encoded,
                        "index": index,
                        "size": len(chunk_bytes),
                        "total_chunks": total_chunks,
                    },
                    timeout=120,
                )

                if not response.get("ok", False):
                    if self.log:
                        self.log.error(f"Failed uploading render chunk {index}")
                    return None

        response = self._post_json(
            self.callback["add_media_url"],
            {
                "_id": media_id,
                "title": title,
                "description": description,
                "user": user,
                "mime": "video/mp4",
                "type": "video",
                "total_size": total_size,
            },
        )

        if not response.get("ok"):
            if self.log:
                self.log.error("Failed to add media metadata")
            return None

        return response.get("data") or {"_id": media_id}

    # -------------------------------------------------------------------------
    # Completion
    # -------------------------------------------------------------------------

    def generation_complete(self, media_id, resolved_timeline=None):
        if not self.generation:
            if self.log:
                self.log.warning("generation_complete skipped: no generation")
            return {"ok": False, "error": "no generation"}

        payload = {
            "_id": self.generation.get("_id"),
            "editor_media": media_id,
            "time": self.iso_now(),
            "machine": getattr(self, "machine_id", "unknown"),
        }
        if resolved_timeline is not None:
            payload["resolved_timeline"] = resolved_timeline

        return self._post_json(self.callback["complete_url"], payload)

    def finish_render_and_report(self):
        """Upload render, attach resolved timings, notify complete."""
        self.update_server_status("UPLOADING_RENDER")
        try:
            media = self.upload_rendered_media()
            if not media:
                self.update_server_status(
                    "FAILED",
                    extra={"error": "render upload failed"},
                )
                return None

            media_id = media.get("_id") if isinstance(media, dict) else media
            resolved = self.build_resolved_timeline_payload()

            result = self.generation_complete(
                media_id=media_id,
                resolved_timeline=resolved,
            )

            if result.get("ok", True):
                self.update_server_status(
                    "DONE",
                    extra={"editor_media": media_id},
                )
            else:
                self.update_server_status(
                    "FAILED",
                    extra={"error": result.get("error", "generation_complete failed")},
                )

            return {
                "media": media,
                "resolved_timeline": resolved,
                "complete_response": result,
            }
        finally:
            resolver = getattr(self, "resolver", None)
            if resolver is not None:
                resolver.clear_cache()
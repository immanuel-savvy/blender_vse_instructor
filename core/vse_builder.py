import bpy

from ..core.logger import Logger

from pathlib import Path

from datetime import datetime, timezone

import urllib.request
import json
import base64
import math
import uuid

from .timeline_resolver import (
    TimelineResolver,
    TimelineResolutionError,
)

from .vse_renderer import Vse_renderer


# =============================================================================
# CACHE
# =============================================================================

CACHE_ROOT = Path.home() / "VSEInstructorCache"

CACHE_ROOT.mkdir(
    parents=True,
    exist_ok=True,
)

MEDIA_EXTENSIONS = {
    "video": [
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
    ],
    "audio": [
        ".wav",
        ".mp3",
        ".ogg",
        ".flac",
        ".aac",
    ],
    "image": [
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".tif",
        ".tiff",
    ],
}


# =============================================================================
# CHANNEL LAYOUT
# =============================================================================

TRACK_TYPE_ROLES = {
    "primary_visual": "video-main",

    "dialogue": "audio",
    "voiceover": "audio",

    "secondary_visual": "video-overlay",
    "overlay": "video-overlay",
    "graphics": "video-overlay",

    "subtitle": "text",
    "title": "text",

    "sfx": "sfx",

    "ambience": "music",
    "music": "music",

    "metadata": None,
}


ROLE_CHANNELS = {
    "video-main": (1, 2),
    "audio": (3, 3),
    "video-overlay": (4, 5),
    "text": (6, 6),
    "sfx": (7, 7),
    "music": (8, 8),
}


# The probe channel must never overlap any permanent channel.
PROBE_CHANNEL = (
    max(
        channel
        for channels in ROLE_CHANNELS.values()
        for channel in channels
    )
    + 1
)


# =============================================================================
# DEFAULTS
# =============================================================================

DEFAULT_IMAGE_DURATION_SECONDS = 5.0
DEFAULT_UNRESOLVED_DURATION_SECONDS = 5.0


class VSEBuilder(Vse_renderer):

    server_url = "https://blender-backend.vercel.app"

    # =========================================================================
    # INITIALIZATION
    # =========================================================================

    def __init__(self, instruction):

        self.log = Logger()

        self.log.info(
            "Initializing VSEBuilder..."
        )

        self.log.info(
            f"Instruction received: {instruction}"
        )

        self.editor_url = (
            "https://editor-backend-xi.vercel.app"
        )

        self.server_url = (
            "https://blender-backend.vercel.app"
        )

        self.instruction = instruction

        self.sequence = instruction.get(
            "sequence",
            instruction,
        )

        self.generation = None

        self.resolving_media = False

        self.sequencer = (
            bpy.context.scene.sequence_editor
        )

        self.timeline = None

        self.fps = getattr(
            bpy.context.scene.render,
            "fps",
            24,
        )

        self.strips = {}

        self._compiled_strips = self.strips

        # ---------------------------------------------------------------------
        # clip_id -> native duration in frames
        # ---------------------------------------------------------------------

        self._native_durations = {}

        # ---------------------------------------------------------------------
        # clip_id -> original clip object
        #
        # This is critical.
        #
        # TimelineResolver only gives us the clip id when requesting a
        # duration. We therefore need an index allowing us to lazily probe
        # that clip if Pass 2 did not already populate its duration.
        # ---------------------------------------------------------------------

        self._clips_by_id = {}

        # ---------------------------------------------------------------------
        # clip_id -> track
        # ---------------------------------------------------------------------

        self._clip_tracks = {}

        # ---------------------------------------------------------------------
        # Prevent repeated failed probing.
        # ---------------------------------------------------------------------

        self._probe_failures = set()

        self._video_counter = 0
        self._audio_counter = 0
        self._image_counter = 0
        self._text_counter = 0
        self._probe_counter = 0

        if self.sequencer is None:

            self.log.info(
                "No sequence editor found. Creating one..."
            )

            self.sequencer = (
                bpy.context.scene.sequence_editor_create()
            )

        else:

            self.log.info(
                "Sequence editor found and ready."
            )

    # =========================================================================
    # NAMING
    # =========================================================================

    def _next_video_name(self):

        self._video_counter += 1

        return f"V{self._video_counter:03d}"

    def _next_audio_name(self):

        self._audio_counter += 1

        return f"A{self._audio_counter:03d}"

    def _next_image_name(self):

        self._image_counter += 1

        return f"IMG{self._image_counter:03d}"

    def _next_text_name(self):

        self._text_counter += 1

        return f"TXT{self._text_counter:03d}"

    def _next_probe_name(self):

        self._probe_counter += 1

        return f"_probe{self._probe_counter:04d}"

    # =========================================================================
    # GENERATION
    # =========================================================================

    def set_generation(self, generation):

        self.log.info(
            f"Setting new generation "
            f"{generation.get('_id')}"
        )

        self.generation = generation

    # =========================================================================
    # MEDIA FETCHING
    # =========================================================================

    def _fetch_chunk_from_server(
        self,
        media_id,
        index,
    ):

        payload = json.dumps(
            {
                "media_id": media_id,
                "index": index,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            url=f"{self.editor_url}/read_upload",
            data=payload,
            headers={
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(
            req,
            timeout=30,
        ) as res:

            return json.loads(
                res.read().decode("utf-8")
            )

    def _infer_extension(
        self,
        clip_ref,
    ):

        mime = clip_ref.get("mime")
        title = clip_ref.get("title")

        MIME_MAP = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",

            "video/mp4": ".mp4",
            "video/quicktime": ".mov",
            "video/webm": ".webm",

            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "audio/ogg": ".ogg",
            "audio/flac": ".flac",
        }

        if mime in MIME_MAP:
            return MIME_MAP[mime]

        if title and "." in title:

            suffix = Path(title).suffix

            if suffix:
                return suffix.lower()

        preferred = clip_ref.get(
            "preferred_type"
        )

        if preferred == "video":
            return ".mp4"

        if preferred == "audio":
            return ".wav"

        if preferred == "image":
            return ".png"

        return ".bin"

    # =========================================================================
    # MEDIA RESOLUTION
    # =========================================================================

    def _is_unresolved_clip_ref(
        self,
        clip_ref,
    ):

        if not clip_ref:
            return True

        if clip_ref.get("type") in {
            "video",
            "image",
            "audio",
            "text",
            "scene",
        }:
            return False

        return (
            "screenplay_blocks" in clip_ref
            or "accepted_types" in clip_ref
            or "preferred_type" in clip_ref
        )

    def _resolve_placeholder(
        self,
        clip_ref,
    ):

        preferred = clip_ref.get(
            "preferred_type",
            "image",
        )

        statics = (
            CACHE_ROOT
            / "statics"
        )

        statics.mkdir(
            parents=True,
            exist_ok=True,
        )

        if preferred == "video":

            return str(
                statics / "video.mp4"
            )

        if preferred == "audio":

            return str(
                statics / "audio.wav"
            )

        return str(
            statics / "image.png"
        )

    def _attach_strip_metadata(
        self,
        strip,
        clip,
        clip_ref,
        resolved,
    ):

        strip["asset_id"] = (
            clip_ref.get("_id")
        )

        strip["instance_id"] = (
            clip.get("instanceId")
        )

        strip["resolved"] = resolved

        strip["preferred_type"] = (
            clip_ref.get("preferred_type")
        )

        strip["accepted_types"] = json.dumps(
            clip_ref.get(
                "accepted_types",
                [],
            )
        )

        strip["screenplay_blocks"] = json.dumps(
            clip_ref.get(
                "screenplay_blocks",
                [],
            )
        )

        strip["description"] = clip_ref.get(
            "description",
            "",
        )

        if "start" in clip:

            try:

                strip["editorial_start"] = json.dumps(
                    clip["start"]
                )

            except Exception:
                pass

        try:

            strip["editorial_clip"] = json.dumps(
                clip,
                default=str,
            )

        except Exception:
            pass

    def _find_cached_media(
        self,
        clip_ref,
    ):

        media_id = clip_ref.get("_id")

        if not media_id:
            return None

        media_dir = (
            CACHE_ROOT
            / media_id.replace(":", "_")
        )

        search_order = []

        preferred = clip_ref.get(
            "preferred_type"
        )

        if preferred:
            search_order.append(
                preferred
            )

        for media_type in clip_ref.get(
            "accepted_types",
            [],
        ):

            if media_type not in search_order:

                search_order.append(
                    media_type
                )

        # If type is explicitly known, prioritize it.
        explicit_type = clip_ref.get(
            "type"
        )

        if explicit_type and explicit_type not in search_order:

            search_order.insert(
                0,
                explicit_type,
            )

        for media_type in search_order:

            for ext in MEDIA_EXTENSIONS.get(
                media_type,
                [],
            ):

                candidate = (
                    media_dir
                    / f"final{ext}"
                )

                if candidate.exists():

                    return {
                        "filepath": str(candidate),

                        "media_type": media_type,

                        "clip_ref": {
                            **clip_ref,
                            "type": media_type,
                        },
                    }

        return None

    def _resolve_media(
        self,
        clip_ref,
    ):

        clip_ref = clip_ref or {}

        self.log.info(
            f"Resolving media: {clip_ref}"
        )

        cached = self._find_cached_media(
            clip_ref
        )

        if cached:

            return {
                **cached,
                "resolved": True,
            }

        # =====================================================================
        # UNRESOLVED ASSET
        # =====================================================================

        if self._is_unresolved_clip_ref(
            clip_ref
        ):

            preferred = clip_ref.get(
                "preferred_type",
                "image",
            )

            # A malformed preferred type should never prevent the timeline
            # from being resolved.
            if preferred not in {
                "video",
                "audio",
                "image",
            }:

                preferred = "image"

            self.log.warning(
                f"Asset '{clip_ref.get('_id')}' "
                f"unresolved. Using placeholder."
            )

            return {
                "filepath": self._resolve_placeholder(
                    {
                        **clip_ref,
                        "preferred_type": preferred,
                    }
                ),

                "media_type": preferred,

                "resolved": False,
            }

        # =====================================================================
        # SERVER MEDIA RESOLUTION
        # =====================================================================

        if not self.resolving_media:

            self.resolving_media = True

            self.update_server_status(
                "RESOLVING_MEDIA"
            )

        media_type = clip_ref.get(
            "type"
        )

        media_id = clip_ref.get(
            "_id"
        )

        # =====================================================================
        # TEXT
        # =====================================================================

        if media_type == "text":

            return {
                "filepath": clip_ref.get(
                    "text",
                    "",
                ),

                "media_type": "text",

                "resolved": True,
            }

        # =====================================================================
        # SCENE
        # =====================================================================

        if media_type == "scene":

            return {
                "filepath": None,

                "media_type": "scene",

                "resolved": True,
            }

        # =====================================================================
        # SUPPORTED MEDIA
        # =====================================================================

        if media_type not in {
            "video",
            "audio",
            "image",
        }:

            self.log.error(
                f"Unsupported media type: "
                f"{media_type}"
            )

            return None

        if not media_id:

            self.log.error(
                "Media has no _id. "
                "Cannot resolve media."
            )

            return None

        media_dir = (
            CACHE_ROOT
            / media_id.replace(":", "_")
        )

        chunks_dir = (
            media_dir
            / "chunks"
        )

        ext = self._infer_extension(
            clip_ref
        )

        final_path = (
            media_dir
            / f"final{ext}"
        )

        media_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        chunks_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        if final_path.exists():

            return {
                "filepath": str(final_path),
                "media_type": media_type,
                "resolved": True,
            }

        # =====================================================================
        # FETCH CHUNKS
        # =====================================================================

        self.log.info(
            "Media not cached. Fetching..."
        )

        index = 0
        total_chunks = None

        while True:

            part_path = (
                chunks_dir
                / f"{index:05d}.part"
            )

            if part_path.exists():

                index += 1

                continue

            try:

                response = (
                    self._fetch_chunk_from_server(
                        media_id,
                        index,
                    )
                )

            except Exception as e:

                self.log.error(
                    f"Failed fetching media "
                    f"'{media_id}' chunk "
                    f"{index}: {e}"
                )

                return None

            if not response.get("ok"):

                self.log.error(
                    f"Failed to fetch chunk "
                    f"{index}: "
                    f"{response.get('error')}"
                )

                return None

            data = response.get(
                "data"
            ) or {}

            chunk = data.get(
                "chunk"
            )

            if not chunk:

                self.log.error(
                    f"Chunk {index} for "
                    f"media '{media_id}' "
                    f"contained no data."
                )

                return None

            part_path.write_bytes(
                base64.b64decode(chunk)
            )

            total_chunks = data.get(
                "total_chunks"
            )

            index += 1

            if (
                total_chunks is not None
                and index >= total_chunks
            ):

                break

        # =====================================================================
        # ASSEMBLE
        # =====================================================================

        with open(
            final_path,
            "wb",
        ) as outfile:

            for part in sorted(
                chunks_dir.iterdir()
            ):

                if not part.is_file():
                    continue

                if not part.name.endswith(
                    ".part"
                ):
                    continue

                outfile.write(
                    part.read_bytes()
                )

        self.log.info(
            f"Media assembled: "
            f"{final_path}"
        )

        return {
            "filepath": str(final_path),
            "media_type": media_type,
            "resolved": True,
        }

    # =========================================================================
    # CUT / SOURCE OFFSET
    # =========================================================================

    def _apply_cut_and_duration(
        self,
        strip,
        clip,
    ):

        """
        Apply only the source offset.

        Editorial duration is controlled by TimelineResolver and applied
        separately during materialization.

        This method intentionally does NOT determine native duration.
        """

        cut = clip.get(
            "cut"
        ) or {}

        source_start_ms = 0

        if cut.get("start") is not None:

            source_start_ms = (
                self.timeline.resolve_ms(
                    cut["start"]
                )
            )

        source_start_frame = (
            self.resolve_frame(
                source_start_ms
            )
        )

        strip.frame_offset_start = max(
            0,
            int(source_start_frame),
        )

        if hasattr(
            strip,
            "frame_offset_end",
        ):

            strip.frame_offset_end = 0

    # =========================================================================
    # DURATION HELPERS
    # =========================================================================

    def _duration_from_clip_definition(
        self,
        clip,
    ):
        """
        Attempt to obtain an explicit duration from the editorial clip.

        This is only a fallback. Native media duration remains authoritative
        whenever media can actually be probed.
        """

        if not clip:
            return None

        candidates = [
            clip.get("duration"),
            clip.get("duration_ms"),
        ]

        for value in candidates:

            if value is None:
                continue

            try:

                if isinstance(value, (int, float)):

                    # duration_ms is already milliseconds.
                    if value == clip.get(
                        "duration_ms"
                    ):

                        return max(
                            1,
                            int(round(value)),
                        )

                    # Editorial duration is conventionally milliseconds in
                    # this system.
                    return max(
                        1,
                        int(round(value)),
                    )

                if isinstance(
                    value,
                    str,
                ):

                    if self.timeline is not None:

                        return max(
                            1,
                            int(
                                self.timeline.resolve_ms(
                                    value
                                )
                            ),
                        )

            except Exception as e:

                self.log.warning(
                    f"Unable to use explicit "
                    f"duration for clip "
                    f"'{clip.get('_id')}': {e}"
                )

        return None

    def _store_native_duration(
        self,
        clip_id,
        duration_frames,
    ):

        if not clip_id:
            return

        duration_frames = max(
            1,
            int(duration_frames),
        )

        self._native_durations[
            clip_id
        ] = duration_frames

    # =========================================================================
    # PASS A/B - NATIVE MEDIA DURATION PROBING
    # =========================================================================

    def _probe_clip(
        self,
        clip,
        track,
        fps,
    ):

        """
        Resolve media and determine its native source duration.

        This function MUST be safe to call for every registered clip.

        It does not create permanent VSE strips.

        Video/audio:
            temporary probe strip -> frame_duration -> remove

        Image:
            deterministic default duration

        Text:
            explicit duration if available, otherwise default duration

        Scene:
            explicit duration if available, otherwise default duration

        Unresolvable media:
            explicit duration if available, otherwise deterministic fallback
        """

        if not clip:
            return None

        clip_id = clip.get(
            "_id"
        )

        if not clip_id:

            self.log.error(
                "[PROBE] Clip has no _id"
            )

            return None

        # Already known.
        if clip_id in self._native_durations:

            return self._native_durations[
                clip_id
            ]

        self.log.info(
            f"[PROBE] Starting clip="
            f"{clip_id}"
        )

        clip_ref = (
            clip.get(
                "clip_ref"
            )
            or {}
        )

        # ---------------------------------------------------------------------
        # Resolve media
        # ---------------------------------------------------------------------

        media = self._resolve_media(
            clip_ref
        )

        if media is None:

            self.log.warning(
                f"[PROBE] Media resolution "
                f"failed for clip "
                f"'{clip_id}'."
            )

            # Try explicit editorial duration.
            explicit_ms = (
                self._duration_from_clip_definition(
                    clip
                )
            )

            if explicit_ms is not None:

                frames = max(
                    1,
                    self.timeline.ms_to_frames(
                        explicit_ms
                    ),
                )

                self._store_native_duration(
                    clip_id,
                    frames,
                )

                self.log.warning(
                    f"[PROBE] Using explicit "
                    f"duration for "
                    f"'{clip_id}': "
                    f"{frames} frames"
                )

                return frames

            # Final deterministic fallback.
            frames = max(
                1,
                round(
                    fps
                    * DEFAULT_UNRESOLVED_DURATION_SECONDS
                ),
            )

            self._store_native_duration(
                clip_id,
                frames,
            )

            self._probe_failures.add(
                clip_id
            )

            self.log.warning(
                f"[PROBE] Using fallback "
                f"duration for "
                f"'{clip_id}': "
                f"{frames} frames"
            )

            return frames

        self.log.info(
            f"[PROBE] clip={clip_id} "
            f"type={media.get('media_type')} "
            f"path={media.get('filepath')}"
        )

        clip["_resolved_media"] = media

        if "clip_ref" in media:

            clip["clip_ref"] = media[
                "clip_ref"
            ]

        media_type = media.get(
            "media_type"
        )

        # ---------------------------------------------------------------------
        # TEXT
        # ---------------------------------------------------------------------

        if media_type == "text":

            explicit_ms = (
                self._duration_from_clip_definition(
                    clip
                )
            )

            if explicit_ms is not None:

                duration = max(
                    1,
                    self.timeline.ms_to_frames(
                        explicit_ms
                    ),
                )

            else:

                duration = max(
                    1,
                    round(
                        fps
                        * DEFAULT_UNRESOLVED_DURATION_SECONDS
                    ),
                )

            self._store_native_duration(
                clip_id,
                duration,
            )

            self.log.info(
                f"[PROBE] text={clip_id} "
                f"native={duration} frames"
            )

            return duration

        # ---------------------------------------------------------------------
        # SCENE
        # ---------------------------------------------------------------------

        if media_type == "scene":

            explicit_ms = (
                self._duration_from_clip_definition(
                    clip
                )
            )

            if explicit_ms is not None:

                duration = max(
                    1,
                    self.timeline.ms_to_frames(
                        explicit_ms
                    ),
                )

            else:

                duration = max(
                    1,
                    round(
                        fps
                        * DEFAULT_UNRESOLVED_DURATION_SECONDS
                    ),
                )

            self._store_native_duration(
                clip_id,
                duration,
            )

            self.log.info(
                f"[PROBE] scene={clip_id} "
                f"native={duration} frames"
            )

            return duration

        # ---------------------------------------------------------------------
        # IMAGE
        # ---------------------------------------------------------------------

        if media_type == "image":

            explicit_ms = (
                self._duration_from_clip_definition(
                    clip
                )
            )

            if explicit_ms is not None:

                duration = max(
                    1,
                    self.timeline.ms_to_frames(
                        explicit_ms
                    ),
                )

            else:

                duration = max(
                    1,
                    round(
                        fps
                        * DEFAULT_IMAGE_DURATION_SECONDS
                    ),
                )

            self._store_native_duration(
                clip_id,
                duration,
            )

            self.log.info(
                f"[PROBE] image={clip_id} "
                f"native={duration} frames"
            )

            return duration

        # ---------------------------------------------------------------------
        # UNSUPPORTED
        # ---------------------------------------------------------------------

        if media_type not in {
            "video",
            "audio",
        }:

            self.log.warning(
                f"[PROBE] Unsupported media "
                f"type '{media_type}' "
                f"for clip '{clip_id}'"
            )

            explicit_ms = (
                self._duration_from_clip_definition(
                    clip
                )
            )

            if explicit_ms is not None:

                duration = max(
                    1,
                    self.timeline.ms_to_frames(
                        explicit_ms
                    ),
                )

            else:

                duration = max(
                    1,
                    round(
                        fps
                        * DEFAULT_UNRESOLVED_DURATION_SECONDS
                    ),
                )

            self._store_native_duration(
                clip_id,
                duration,
            )

            return duration

        filepath = media.get(
            "filepath"
        )

        if not filepath:

            self.log.warning(
                f"[PROBE] No filepath for "
                f"clip '{clip_id}'"
            )

            explicit_ms = (
                self._duration_from_clip_definition(
                    clip
                )
            )

            if explicit_ms is not None:

                duration = max(
                    1,
                    self.timeline.ms_to_frames(
                        explicit_ms
                    ),
                )

            else:

                duration = max(
                    1,
                    round(
                        fps
                        * DEFAULT_UNRESOLVED_DURATION_SECONDS
                    ),
                )

            self._store_native_duration(
                clip_id,
                duration,
            )

            return duration

        # ---------------------------------------------------------------------
        # ACTUAL NATIVE PROBE
        # ---------------------------------------------------------------------

        duration = (
            self._probe_native_duration_frames(
                filepath=filepath,
                kind=media_type,
                clip_id=clip_id,
            )
        )

        if duration is not None:

            self._store_native_duration(
                clip_id,
                duration,
            )

            self.log.info(
                f"[PROBE] clip={clip_id} "
                f"native={duration} frames "
                f"({duration / fps:.3f}s)"
            )

            return duration

        # ---------------------------------------------------------------------
        # PROBE FAILED - FALLBACK
        # ---------------------------------------------------------------------

        self.log.warning(
            f"[PROBE] Failed to determine "
            f"native duration for "
            f"'{clip_id}'."
        )

        explicit_ms = (
            self._duration_from_clip_definition(
                clip
            )
        )

        if explicit_ms is not None:

            fallback = max(
                1,
                self.timeline.ms_to_frames(
                    explicit_ms
                ),
            )

        else:

            fallback = max(
                1,
                round(
                    fps
                    * DEFAULT_UNRESOLVED_DURATION_SECONDS
                ),
            )

        self._store_native_duration(
            clip_id,
            fallback,
        )

        self._probe_failures.add(
            clip_id
        )

        self.log.warning(
            f"[PROBE] Fallback native "
            f"duration for '{clip_id}' = "
            f"{fallback} frames"
        )

        return fallback

    def _probe_native_duration_frames(
        self,
        filepath,
        kind,
        clip_id,
    ):

        """
        Safely probe native media duration.

        The temporary strip exists only for the duration of this method.

        IMPORTANT:

        - Never use an arbitrary/high permanent channel.
        - Never apply editorial cuts to the probe.
        - Read frame_duration.
        - Always remove the strip in finally.
        """

        strip = None

        try:

            name = self._next_probe_name()

            self.log.info(
                f"[PROBE] Creating temporary "
                f"{kind} strip '{name}' "
                f"on channel "
                f"{PROBE_CHANNEL}"
            )

            if kind == "video":

                strip = (
                    self.sequencer.sequences.new_movie(
                        name=name,
                        filepath=filepath,
                        frame_start=1,
                        channel=PROBE_CHANNEL,
                    )
                )

            elif kind == "audio":

                strip = (
                    self.sequencer.sequences.new_sound(
                        name=name,
                        filepath=filepath,
                        frame_start=1,
                        channel=PROBE_CHANNEL,
                    )
                )

            else:

                raise ValueError(
                    f"Unsupported probe type: "
                    f"{kind}"
                )

            # -----------------------------------------------------------------
            # Read native duration BEFORE ANY CUT.
            # -----------------------------------------------------------------

            duration = int(
                strip.frame_duration
            )

            self.log.info(
                f"[PROBE] {clip_id}: "
                f"native frame_duration="
                f"{duration}"
            )

            return max(
                1,
                duration,
            )

        except Exception as e:

            self.log.error(
                f"[PROBE] Failed for "
                f"clip '{clip_id}' "
                f"'{filepath}': {e}"
            )

            return None

        finally:

            if strip is not None:

                try:

                    strip_name = strip.name

                    # FIX: bpy's Sequences.remove() does not accept a
                    # do_unlink keyword argument - only new_movie/etc.
                    # style constructors and some bpy.data collections
                    # (e.g. bpy.data.meshes.remove) support that kwarg.
                    # Passing it here raised a TypeError on every single
                    # call, which this except block silently swallowed
                    # (logged, not re-raised) - so every probe strip
                    # ever created was left behind permanently instead
                    # of being cleaned up. That's why probe strips
                    # accumulate on the probe channel across a build.
                    self.sequencer.sequences.remove(
                        strip
                    )

                    self.log.info(
                        f"[PROBE] Removed "
                        f"temporary strip "
                        f"'{strip_name}'"
                    )

                except Exception as remove_error:

                    self.log.error(
                        f"[PROBE] Failed removing "
                        f"temporary strip "
                        f"'{getattr(strip, 'name', '<unknown>')}': "
                        f"{remove_error}"
                    )

    # =========================================================================
    # CRITICAL: LAZY DURATION PROVIDER
    # =========================================================================

    def get_clip_duration_ms(
        self,
        clip_id,
    ):

        """
        Duration provider used by TimelineResolver.

        This method is intentionally defensive.

        TimelineResolver is allowed to ask for a duration at any point during
        resolution. Therefore we cannot assume Pass 2 has already populated
        _native_durations.

        Resolution order:

            1. Existing probed native duration
            2. Lazily probe the indexed clip
            3. Explicit clip duration
            4. Deterministic fallback

        It should therefore never produce the old:

            No available probe duration for clip ...

        unless the clip genuinely cannot be identified at all.
        """

        if not clip_id:

            raise TimelineResolutionError(
                "Cannot resolve duration for "
                "clip with no id."
            )

        # ---------------------------------------------------------------------
        # 1. Already probed
        # ---------------------------------------------------------------------

        duration_frames = (
            self._native_durations.get(
                clip_id
            )
        )

        if duration_frames is not None:

            return round(
                duration_frames
                * 1000
                / self.fps
            )

        # ---------------------------------------------------------------------
        # 2. Lazy probe
        # ---------------------------------------------------------------------

        clip = (
            self._clips_by_id.get(
                clip_id
            )
        )

        if clip is not None:

            track = (
                self._clip_tracks.get(
                    clip_id
                )
            )

            if self.timeline is not None:

                duration_frames = (
                    self._probe_clip(
                        clip,
                        track,
                        self.fps,
                    )
                )

                if duration_frames is not None:

                    return round(
                        duration_frames
                        * 1000
                        / self.fps
                    )

        # ---------------------------------------------------------------------
        # 3. Explicit duration fallback
        # ---------------------------------------------------------------------

        if clip is not None:

            explicit_ms = (
                self._duration_from_clip_definition(
                    clip
                )
            )

            if explicit_ms is not None:

                duration_frames = max(
                    1,
                    self.timeline.ms_to_frames(
                        explicit_ms
                    ),
                )

                self._store_native_duration(
                    clip_id,
                    duration_frames,
                )

                return round(
                    duration_frames
                    * 1000
                    / self.fps
                )

        # ---------------------------------------------------------------------
        # 4. Deterministic fallback
        # ---------------------------------------------------------------------

        fallback_frames = max(
            1,
            round(
                self.fps
                * DEFAULT_UNRESOLVED_DURATION_SECONDS
            ),
        )

        self._store_native_duration(
            clip_id,
            fallback_frames,
        )

        self.log.warning(
            f"[DURATION] No native duration "
            f"was available for clip "
            f"'{clip_id}'. Using fallback "
            f"{fallback_frames} frames."
        )

        return round(
            fallback_frames
            * 1000
            / self.fps
        )

    # =========================================================================
    # TRACK MEDIA TYPE
    # =========================================================================

    def _infer_track_media_type(
        self,
        track,
    ):

        for clip in track.get(
            "clips",
            [],
        ):

            clip_ref = (
                clip.get(
                    "clip_ref",
                    {},
                )
                or {}
            )

            media_type = (
                clip_ref.get("type")
                or clip_ref.get(
                    "preferred_type"
                )
            )

            if media_type in {
                "video",
                "image",
            }:

                return "video"

            if media_type == "audio":

                return "audio"

            if media_type == "text":

                return "text"

        return None

    # =========================================================================
    # TRACK ROLE
    # =========================================================================

    def _assign_track_role(
        self,
        track,
    ):

        track_type = track.get(
            "type"
        )

        if track_type in TRACK_TYPE_ROLES:

            return TRACK_TYPE_ROLES[
                track_type
            ]

        guess = (
            self._infer_track_media_type(
                track
            )
        )

        if guess == "video":
            return "video-overlay"

        if guess == "audio":
            return "audio"

        if guess == "text":
            return "text"

        return None

    def _locked_channels(
        self,
        track,
    ):

        role = self._assign_track_role(
            track
        )

        if role is None:
            return None

        return ROLE_CHANNELS.get(
            role
        )

    # =========================================================================
    # CHANNEL RESOLUTION
    # =========================================================================

    def _resolve_channels(
        self,
        clip,
        track,
    ):

        """
        Return:

            video_channel,
            audio_channel

        based on the locked role of the track.
        """

        channels = self._locked_channels(
            track
        )

        if channels is None:
            return None, None

        video_channel, audio_channel = (
            channels
        )

        clip_ref = (
            clip.get(
                "clip_ref"
            )
            or {}
        )

        media = (
            clip.get(
                "_resolved_media"
            )
            or {}
        )

        media_type = (
            media.get(
                "media_type"
            )
            or clip_ref.get(
                "type"
            )
            or clip_ref.get(
                "preferred_type"
            )
        )

        if media_type == "audio":

            return (
                None,
                audio_channel,
            )

        if media_type == "text":

            return (
                video_channel,
                None,
            )

        return (
            video_channel,
            audio_channel,
        )

    # =========================================================================
    # PASS B - REAL VIDEO + AUDIO STRIP
    # =========================================================================

    def _add_video_clip(
        self,
        clip,
        track,
        fps,
        start_frame,
        duration_frames,
    ):

        try:

            clip_ref = clip.get(
                "clip_ref"
            )

            media = clip[
                "_resolved_media"
            ]

            filepath = media[
                "filepath"
            ]

            resolved = media[
                "resolved"
            ]

            (
                video_channel,
                audio_channel,
            ) = self._resolve_channels(
                clip,
                track,
            )

            if video_channel is None:

                raise ValueError(
                    "No video channel "
                    "available for video clip."
                )

            if audio_channel is None:

                raise ValueError(
                    "No audio channel "
                    "available for video clip."
                )

            # -----------------------------------------------------------------
            # VIDEO
            # -----------------------------------------------------------------

            video_name = (
                self._next_video_name()
            )

            video = (
                self.sequencer.sequences.new_movie(
                    name=video_name,
                    filepath=filepath,
                    frame_start=int(
                        start_frame
                    ),
                    channel=int(
                        video_channel
                    ),
                )
            )

            self._attach_strip_metadata(
                video,
                clip,
                clip_ref,
                resolved,
            )

            video["strip_role"] = "video"

            self._apply_cut_and_duration(
                video,
                clip,
            )

            video.frame_final_duration = max(
                1,
                int(duration_frames),
            )

            # -----------------------------------------------------------------
            # AUDIO
            # -----------------------------------------------------------------

            audio_name = (
                self._next_audio_name()
            )

            audio = (
                self.sequencer.sequences.new_sound(
                    name=audio_name,
                    filepath=filepath,
                    frame_start=int(
                        start_frame
                    ),
                    channel=int(
                        audio_channel
                    ),
                )
            )

            self._attach_strip_metadata(
                audio,
                clip,
                clip_ref,
                resolved,
            )

            audio["strip_role"] = "audio"

            video["paired_audio"] = (
                audio.name
            )

            audio["paired_video"] = (
                video.name
            )

            self._apply_cut_and_duration(
                audio,
                clip,
            )

            audio.frame_final_duration = max(
                1,
                int(duration_frames),
            )

            self.log.info(
                f"Added VIDEO+AUD "
                f"{video.name}/{audio.name} "
                f"ch={video_channel}/"
                f"{audio_channel} "
                f"start={start_frame} "
                f"dur={duration_frames}"
            )

            return video

        except Exception as e:

            self.log.error(
                f"Failed adding "
                f"VIDEO/AUDIO: {e}"
            )

            return None

    # =========================================================================
    # PASS B - AUDIO ONLY
    # =========================================================================

    def _add_audio_clip(
        self,
        clip,
        track,
        fps,
        start_frame,
        duration_frames,
    ):

        try:

            clip_ref = clip.get(
                "clip_ref"
            )

            media = clip[
                "_resolved_media"
            ]

            filepath = media[
                "filepath"
            ]

            resolved = media[
                "resolved"
            ]

            (
                _,
                audio_channel,
            ) = self._resolve_channels(
                clip,
                track,
            )

            if audio_channel is None:

                raise ValueError(
                    "No audio channel "
                    "available."
                )

            name = (
                self._next_audio_name()
            )

            audio = (
                self.sequencer.sequences.new_sound(
                    name=name,
                    filepath=filepath,
                    frame_start=int(
                        start_frame
                    ),
                    channel=int(
                        audio_channel
                    ),
                )
            )

            self._attach_strip_metadata(
                audio,
                clip,
                clip_ref,
                resolved,
            )

            audio["strip_role"] = (
                "audio_only"
            )

            self._apply_cut_and_duration(
                audio,
                clip,
            )

            audio.frame_final_duration = max(
                1,
                int(duration_frames),
            )

            self.log.info(
                f"Created AUDIO ONLY "
                f"{audio.name} "
                f"ch={audio_channel} "
                f"start={start_frame} "
                f"dur={duration_frames}"
            )

            return audio

        except Exception as e:

            self.log.error(
                f"Failed to add "
                f"AUDIO ONLY: {e}"
            )

            return None

    # =========================================================================
    # PASS B - IMAGE
    # =========================================================================

    def _add_image_clip(
        self,
        clip,
        track,
        fps,
        start_frame,
        duration_frames,
    ):

        try:

            clip_ref = clip.get(
                "clip_ref"
            )

            media = clip[
                "_resolved_media"
            ]

            filepath = media[
                "filepath"
            ]

            resolved = media[
                "resolved"
            ]

            if not filepath:

                self.log.error(
                    "Failed to resolve "
                    "image media."
                )

                return None

            (
                video_channel,
                _,
            ) = self._resolve_channels(
                clip,
                track,
            )

            if video_channel is None:

                raise ValueError(
                    "No video channel "
                    "available for image."
                )

            name = (
                self._next_image_name()
            )

            image_strip = (
                self.sequencer.sequences.new_image(
                    name,
                    filepath,
                    int(video_channel),
                    int(start_frame),
                )
            )

            self._apply_cut_and_duration(
                image_strip,
                clip,
            )

            image_strip.frame_final_duration = max(
                1,
                int(duration_frames),
            )

            self._attach_strip_metadata(
                image_strip,
                clip,
                clip_ref,
                resolved,
            )

            self.log.info(
                f"Created IMAGE strip: "
                f"{image_strip.name} "
                f"ch={video_channel} "
                f"start={start_frame} "
                f"dur={duration_frames}"
            )

            return image_strip

        except Exception as e:

            self.log.error(
                f"Failed to add IMAGE: {e}"
            )

            return None

    # =========================================================================
    # MAIN BUILD
    # =========================================================================

    def build(self):

        seq = self.instruction.get(
            "sequence",
            self.instruction,
        )

        fps = seq.get(
            "fps",
            24,
        )

        scene = bpy.context.scene

        scene.render.fps = fps
        scene.render.fps_base = 1.0

        self.fps = fps

        # ---------------------------------------------------------------------
        # RESET BUILD STATE
        # ---------------------------------------------------------------------

        self._video_counter = 0
        self._audio_counter = 0
        self._image_counter = 0
        self._text_counter = 0
        self._probe_counter = 0

        self.strips = {}

        self._compiled_strips = (
            self.strips
        )

        self._native_durations = {}

        self._clips_by_id = {}

        self._clip_tracks = {}

        self._probe_failures = set()

        self.resolving_media = False

        # ---------------------------------------------------------------------
        # TIMELINE RESOLVER
        # ---------------------------------------------------------------------

        self.timeline_resolver = (
            TimelineResolver(
                sequence=seq,
                fps=fps,
                duration_provider=(
                    self.get_clip_duration_ms
                ),
            )
        )

        self.timeline = (
            self.timeline_resolver
        )

        # ---------------------------------------------------------------------
        # CLEAR VSE
        # ---------------------------------------------------------------------

        self._clear_sequencer()

        tracks = seq.get(
            "tracks",
            [],
        )

        # =====================================================================
        # BUILD CLIP INDEX
        # =====================================================================

        self.log.info(
            "[BUILD] INDEXING ALL CLIPS"
        )

        for index, track in enumerate(
            tracks
        ):

            track_id = track.get(
                "_id",
                f"track-{index}",
            )

            track["_id"] = track_id

            for clip_index, clip in enumerate(
                track.get(
                    "clips",
                    [],
                )
            ):

                clip_id = clip.get(
                    "_id"
                )

                if not clip_id:

                    clip_id = (
                        f"{track_id}-clip-"
                        f"{clip_index}"
                    )

                    clip["_id"] = clip_id

                # -------------------------------------------------------------
                # Store by ID.
                # -------------------------------------------------------------

                self._clips_by_id[
                    clip_id
                ] = clip

                self._clip_tracks[
                    clip_id
                ] = track

                self.log.info(
                    f"[BUILD] Indexed clip "
                    f"{clip_id} "
                    f"track={track_id}"
                )

        # =====================================================================
        # PASS 1
        #
        # Register EVERYTHING with TimelineResolver.
        # =====================================================================

        self.log.info(
            "[BUILD] PASS 1 - registering "
            "tracks and clips"
        )

        for index, track in enumerate(
            tracks
        ):

            track_id = track.get(
                "_id",
                f"track-{index}",
            )

            self.timeline_resolver.register_track(
                track_id
            )

            for clip in track.get(
                "clips",
                [],
            ):

                self.timeline_resolver.register_clip(
                    clip,
                    track_id,
                )

            if (
                self._locked_channels(
                    track
                )
                is None
            ):

                self.log.info(
                    f"[CHANNEL] {track_id} "
                    f"(type={track.get('type')}) "
                    f"-> not positionable, "
                    f"registered only"
                )

        # =====================================================================
        # PASS 2
        #
        # Resolve media and probe native durations.
        #
        # IMPORTANT:
        #
        # We probe EVERY registered clip.
        #
        # Previously this loop skipped tracks without locked channels. That
        # meant TimelineResolver could later request a duration for a clip
        # which had never been probed.
        # =====================================================================

        self.log.info(
            "[BUILD] PASS 2 - resolving "
            "media and probing durations"
        )

        self.update_server_status(
            "RESOLVING_MEDIA"
        )

        for track in tracks:

            for clip in track.get(
                "clips",
                [],
            ):

                self._probe_clip(
                    clip,
                    track,
                    fps,
                )

        self.log.info(
            f"[BUILD] Probed "
            f"{len(self._native_durations)} "
            f"clip durations."
        )

        # =====================================================================
        # PASS 3
        #
        # Resolve complete editorial timeline.
        # =====================================================================

        self.log.info(
            "[BUILD] PASS 3 - resolving "
            "editorial timeline"
        )

        self.update_server_status(
            "RESOLVING_TIMELINE"
        )

        self.timeline_resolver.resolve_timeline()

        # =====================================================================
        # PASS 4
        #
        # Materialize TEXT.
        # =====================================================================

        self.log.info(
            "[BUILD] PASS 4 - materializing "
            "text"
        )

        self._materialize_text_clips(
            tracks
        )

        # =====================================================================
        # PASS 5
        #
        # Materialize all real media strips.
        #
        # Every strip is created exactly once at its final position/channel.
        # =====================================================================

        self.log.info(
            "[BUILD] PASS 5 - materializing "
            "media"
        )

        self.update_server_status(
            "BUILDING_VSE"
        )

        for track in tracks:

            if (
                self._locked_channels(
                    track
                )
                is None
            ):

                continue

            for clip in track.get(
                "clips",
                [],
            ):

                self._materialize_resolved_clip(
                    clip,
                    track,
                    fps,
                )

        # ---------------------------------------------------------------------
        # FINISH
        # ---------------------------------------------------------------------

        self.fit_scene_to_timeline()

        self.update_server_status(
            "VSE_READY"
        )

        self.log.info(
            f"VSE build completed. "
            f"{len(self.strips)} strips "
            f"materialized."
        )

    # =========================================================================
    # MATERIALIZE RESOLVED CLIP
    # =========================================================================

    def _materialize_resolved_clip(
        self,
        clip,
        track,
        fps,
    ):

        clip_id = clip.get(
            "_id"
        )

        media = clip.get(
            "_resolved_media"
        )

        if not media:

            self.log.error(
                f"No resolved media for "
                f"clip '{clip_id}'"
            )

            return None

        media_type = media.get(
            "media_type"
        )

        if media_type in {
            "text",
            "scene",
        }:

            return None

        obj = (
            self.timeline_resolver.clips.get(
                clip_id
            )
        )

        if obj is None:

            self.log.error(
                f"No resolved timing "
                f"for clip '{clip_id}'"
            )

            return None

        start_frame = (
            self.timeline_resolver.ms_to_frames(
                obj.start
            )
        )

        duration_frames = max(
            1,
            self.timeline_resolver.ms_to_frames(
                obj.duration
            ),
        )

        strip = None

        if media_type == "video":

            strip = self._add_video_clip(
                clip,
                track,
                fps,
                start_frame,
                duration_frames,
            )

        elif media_type == "audio":

            strip = self._add_audio_clip(
                clip,
                track,
                fps,
                start_frame,
                duration_frames,
            )

        elif media_type == "image":

            strip = self._add_image_clip(
                clip,
                track,
                fps,
                start_frame,
                duration_frames,
            )

        else:

            self.log.warning(
                f"Unsupported materialization "
                f"type '{media_type}' "
                f"for clip '{clip_id}'"
            )

        if (
            strip is not None
            and clip_id
        ):

            self.strips[
                clip_id
            ] = strip

        return strip

    # =========================================================================
    # TEXT MATERIALIZATION
    # =========================================================================

    def _materialize_text_clips(
        self,
        tracks,
    ):

        for track in tracks:

            if (
                self._locked_channels(
                    track
                )
                is None
            ):

                continue

            for clip in track.get(
                "clips",
                [],
            ):

                media = (
                    clip.get(
                        "_resolved_media"
                    )
                    or {}
                )

                if (
                    media.get(
                        "media_type"
                    )
                    != "text"
                ):

                    continue

                clip_id = clip.get(
                    "_id"
                )

                obj = (
                    self.timeline_resolver.clips.get(
                        clip_id
                    )
                )

                if obj is None:

                    self.log.error(
                        f"No resolved timing "
                        f"for text clip "
                        f"'{clip_id}'"
                    )

                    continue

                start_frame = max(
                    1,
                    self.timeline_resolver.ms_to_frames(
                        obj.start
                    ),
                )

                duration_frames = max(
                    1,
                    self.timeline_resolver.ms_to_frames(
                        obj.duration
                    ),
                )

                end_frame = (
                    start_frame
                    + duration_frames
                )

                strip = (
                    self._create_text_strip(
                        clip,
                        track,
                        start_frame,
                        end_frame,
                    )
                )

                if strip is not None:

                    self.strips[
                        clip_id
                    ] = strip

    def _create_text_strip(
        self,
        clip,
        track,
        start_frame,
        end_frame,
    ):

        clip_ref = clip.get(
            "clip_ref",
            {},
        )

        text = (
            clip_ref.get(
                "value"
            )
            or clip_ref.get(
                "text"
            )
            or "Text strip"
        )

        (
            video_channel,
            _,
        ) = self._resolve_channels(
            clip,
            track,
        )

        if video_channel is None:

            self.log.error(
                f"No text channel for "
                f"clip '{clip.get('_id')}'"
            )

            return None

        name = (
            self._next_text_name()
        )

        txt = (
            self.sequencer.sequences.new_effect(
                name=name,
                type="TEXT",
                frame_start=int(
                    start_frame
                ),
                frame_end=int(
                    end_frame
                ),
                channel=int(
                    video_channel
                ),
            )
        )

        try:

            txt.frame_final_duration = int(
                end_frame
                - start_frame
            )

        except Exception:
            pass

        resolved = not (
            self._is_unresolved_clip_ref(
                clip_ref
            )
        )

        self._attach_strip_metadata(
            txt,
            clip,
            clip_ref,
            resolved,
        )

        txt.text = text

        self.log.info(
            f"[CREATE TEXT] "
            f"{clip.get('_id')} | "
            f"'{text[:40]}' | "
            f"final "
            f"{txt.frame_final_start}-"
            f"{txt.frame_final_end} | "
            f"ch={txt.channel}"
        )

        return txt

    # =========================================================================
    # TIMELINE HELPERS
    # =========================================================================

    def resolve_time(
        self,
        value,
    ):

        return self.timeline.resolve_ms(
            value
        )

    def resolve_frame(
        self,
        value,
    ):

        return self.timeline.resolve_frame(
            value
        )

    def calculate_timeline_range(
        self,
    ):

        strips = list(
            self.sequencer.sequences_all
        )

        if not strips:
            return 0, 0, 0

        start = min(
            s.frame_final_start
            for s in strips
        )

        end = max(
            s.frame_final_end
            for s in strips
        )

        return (
            start,
            end,
            end - start,
        )

    def fit_scene_to_timeline(
        self,
    ):

        scene = bpy.context.scene

        start, end, _ = (
            self.calculate_timeline_range()
        )

        scene.frame_start = start

        scene.frame_end = max(
            start,
            end - 1,
        )

    def _clear_sequencer(
        self,
    ):

        bpy.context.scene.sequence_editor_clear()

        self.sequencer = (
            bpy.context.scene.sequence_editor_create()
        )

        self.log.info(
            "Sequencer cleared"
        )

    # =========================================================================
    # SERVER HELPERS
    # =========================================================================

    def iso_now(
        self,
    ):

        return (
            datetime.now(
                timezone.utc
            ).isoformat(
                timespec="milliseconds"
            ).replace(
                "+00:00",
                "Z",
            )
        )

    def _post_json(
        self,
        url,
        payload,
    ):

        self.log.info(
            f"POST {url}"
        )

        try:

            req = urllib.request.Request(
                url=url,
                data=json.dumps(
                    payload
                ).encode("utf-8"),
                headers={
                    "Content-Type":
                        "application/json"
                },
                method="POST",
            )

            with urllib.request.urlopen(
                req,
                timeout=30,
            ) as res:

                return json.loads(
                    res.read().decode(
                        "utf-8"
                    )
                )

        except Exception as e:

            self.log.error(
                f"POST FAILED {url}: {e}"
            )

            return {
                "ok": False,
                "error": str(e),
            }

    def update_server_status(
        self,
        status,
    ):

        if not self.generation:

            self.log.warning(
                f"update_server_status "
                f"skipped ({status}): "
                f"no generation"
            )

            return

        if not hasattr(
            self,
            "machine_id",
        ):

            self.machine_id = (
                "unknown"
            )

        generation_id = (
            self.generation.get(
                "_id"
            )
        )

        payload = {
            "_id": generation_id,
            "status": status,
            "time": self.iso_now(),
            "machine": self.machine_id,
        }

        return self._post_json(
            f"{self.server_url}/"
            f"update_generation_status",
            payload,
        )

    # =========================================================================
    # RENDER UPLOAD
    # =========================================================================

    def upload_rendered_media(
        self,
        chunk_size=2 * 1024 * 1024,
    ):

        scene = bpy.context.scene

        title = self.instruction.get(
            "name",
            "<unk>",
        )

        description = (
            self.instruction.get(
                "description",
                "",
            )
        )

        user = self.instruction.get(
            "editor",
            "<unk>",
        )

        filepath = Path(
            scene.render.filepath
        )

        if not filepath.exists():

            self.log.error(
                f"Render file does not exist: "
                f"{filepath}"
            )

            return None

        total_size = (
            filepath.stat().st_size
        )

        if total_size <= 0:

            self.log.error(
                f"Render file is empty: "
                f"{filepath}"
            )

            return None

        total_chunks = math.ceil(
            total_size
            / chunk_size
        )

        media_id = str(
            uuid.uuid4()
        )

        with open(
            filepath,
            "rb",
        ) as f:

            for index in range(
                total_chunks
            ):

                chunk_bytes = f.read(
                    chunk_size
                )

                encoded = (
                    base64.b64encode(
                        chunk_bytes
                    ).decode(
                        "utf-8"
                    )
                )

                response = self._post_json(
                    f"{self.editor_url}/"
                    f"upload_media",
                    {
                        "media_id": media_id,
                        "chunk": encoded,
                        "index": index,
                        "size": len(
                            chunk_bytes
                        ),
                        "total_chunks":
                            total_chunks,
                    },
                )

                if not response.get(
                    "ok",
                    False,
                ):

                    self.log.error(
                        f"Failed uploading "
                        f"render chunk "
                        f"{index}"
                    )

                    return None

        response = self._post_json(
            f"{self.editor_url}/"
            f"add_media",
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

        if not response.get(
            "ok"
        ):

            self.log.error(
                "Failed to add "
                "media metadata"
            )

            return None

        return response[
            "data"
        ]

    # =========================================================================
    # GENERATION COMPLETE
    # =========================================================================

    def generation_complete(
        self,
        media_id,
    ):

        return self._post_json(
            f"{self.server_url}/"
            f"generation_complete",
            {
                "_id": self.generation.get(
                    "_id"
                ),
                "editor_media": media_id,
            },
        )

    # =========================================================================
    # BLEND SNAPSHOT
    # =========================================================================

    def save_blend_snapshot(
        self,
        name,
    ):

        output_dir = (
            Path.home()
            / "VSE_Instructor_Projects"
        )

        output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        blend_path = (
            output_dir
            / f"{name}.blend"
        )

        bpy.ops.wm.save_as_mainfile(
            filepath=str(
                blend_path
            )
        )

        self.log.info(
            f"Blend snapshot saved: "
            f"{blend_path}"
        )

        return str(
            blend_path
        )
import bpy

from ..core.logger import Logger

from pathlib import Path
from datetime import datetime, timezone

import urllib.request
import json
import base64
import math
import uuid
import re

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
# LOCAL STATICS
# =============================================================================

LOCAL_AUDIO_STATICS = Path(
    "/Users/mac/Creature/web4/SERVICES/Social_handler/statics/audio"
)


# =============================================================================
# CHANNEL LAYOUT
# =============================================================================

ROLE_WEIGHT = {
    "metadata": 0,
    "music": 10,
    "sfx": 20,
    "audio": 30,
    "video-main": 40,
    "video-overlay": 50,
    "transform": 55,
    "text": 90,
}


ROLE_PREFERRED_START = {
    "music": 1,
    "sfx": 2,
    "audio": 3,
    "video-main": 4,
    "video-overlay": 6,
    "transform": 8,
    "text": 10,
}


MAX_PERMANENT_CHANNEL = 20
PROBE_CHANNEL = MAX_PERMANENT_CHANNEL + 1


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
    "transform": "transform",
    "metadata": None,
}


# =============================================================================
# DEFAULTS
# =============================================================================

DEFAULT_IMAGE_DURATION_SECONDS = 5.0
DEFAULT_UNRESOLVED_DURATION_SECONDS = 5.0


# =============================================================================
# INTERPOLATION
# =============================================================================

INTERPOLATION_MAP = {
    "linear": "LINEAR",
    "constant": "CONSTANT",
    "bezier": "BEZIER",
}


# =============================================================================
# TRANSFORM PROPERTIES
#
# Single source of truth for "this friendly name on a clip's
# `transforms` block maps to this Blender property on the strip,
# and needs this value conversion before keyframing."
#
# Adding a new transform property is a one-line entry here. Every
# keyframe (regardless of `curve`) automatically goes through
# `_apply_generic_keyframe` and gets the right interpolation
# (LINEAR / CONSTANT / BEZIER) applied to every component fcurve
# of the target property.
# =============================================================================


def _convert_float(value):
    """Plain float — translate, scale, opacity, etc."""
    return float(value)


def _convert_rotation(value):
    """JSON in degrees -> Blender radians (sequence rotation)."""
    return math.radians(float(value))


def _convert_color(value):
    """
    '#RRGGBB' / '#RRGGBBAA' / 'RRGGBB' / 'RRGGBBAA'
    -> Blender's normalized RGBA tuple.
    """
    if value is None:
        raise ValueError("Color value cannot be None.")

    text = str(value).strip().lstrip("#")

    if len(text) == 6:
        text += "ff"

    if len(text) != 8:
        raise ValueError(
            f"Invalid color '{value}'. "
            f"Expected #RRGGBB or #RRGGBBAA."
        )

    try:
        return tuple(
            int(text[i:i + 2], 16) / 255.0
            for i in range(0, 8, 2)
        )
    except ValueError as exc:
        raise ValueError(
            f"Invalid hexadecimal color '{value}'."
        ) from exc


# ---------------------------------------------------------------------------
# Property registry
# ---------------------------------------------------------------------------
#
# Schema per entry:
#
#   "friendly_name": {
#       "data_path": "<RNA path relative to the strip>",
#       "requires":  "<attribute that must exist on the strip>",
#       "convert":   <callable: raw JSON value -> Blender value>,
#       "description": "<optional human-readable note>",
#   }
#
# `data_path` is what gets passed to `strip.keyframe_insert()` and
# what the resulting fcurve's data_path ends with. It may be a
# nested path like "transform.offset_x".
#
# `requires` is the top-level attribute we check with hasattr()
# before touching the property. For "transform.*" paths this is
# "transform" (the nested object), not the leaf property name —
# because `hasattr(strip, "transform.offset_x")` is unreliable in
# Python's hasattr.
# ---------------------------------------------------------------------------

TRANSFORM_PROPERTY_MAP = {

    # ---- 2D position / scale / rotation ----

    "translate_x": {
        "data_path": "transform.offset_x",
        "requires": "transform",
        "convert": _convert_float,
        "description": "Horizontal offset in pixels.",
    },
    "translate_y": {
        "data_path": "transform.offset_y",
        "requires": "transform",
        "convert": _convert_float,
        "description": "Vertical offset in pixels.",
    },
    "scale_x": {
        "data_path": "transform.scale_x",
        "requires": "transform",
        "convert": _convert_float,
        "description": "Horizontal scale factor.",
    },
    "scale_y": {
        "data_path": "transform.scale_y",
        "requires": "transform",
        "convert": _convert_float,
        "description": "Vertical scale factor.",
    },
    "rotation": {
        "data_path": "transform.rotation",
        "requires": "transform",
        "convert": _convert_rotation,
        "description": "Rotation in degrees (JSON) -> radians (Blender).",
    },

    # ---- Opacity (works on every strip type) ----

    "opacity": {
        "data_path": "blend_alpha",
        "requires": "blend_alpha",
        "convert": _convert_float,
        "description": "Strip opacity 0.0 (transparent) - 1.0 (opaque).",
    },
    "blend_alpha": {
        "data_path": "blend_alpha",
        "requires": "blend_alpha",
        "convert": _convert_float,
        "description": "Alias for 'opacity'.",
    },

    # ---- Text strip color (RGBA -> 4 component fcurves) ----

    "color": {
        "data_path": "color",
        "requires": "color",
        "convert": _convert_color,
        "description": (
            "RGBA color from hex string. Multi-component -> 4 "
            "fcurves (R,G,B,A) all get the requested curve."
        ),
    },

    # ---- Sound strip properties ----

    "volume": {
        "data_path": "volume",
        "requires": "volume",
        "convert": _convert_float,
        "description": "Audio volume. 1.0 = unity gain.",
    },
    "pitch": {
        "data_path": "pitch",
        "requires": "pitch",
        "convert": _convert_float,
        "description": "Audio pitch shift in semitones.",
    },
    "pan": {
        "data_path": "pan",
        "requires": "pan",
        "convert": _convert_float,
        "description": "Stereo pan. -1.0 = left, 0.0 = center, 1.0 = right.",
    },
}


def _resolve_nested_attr(obj, dotted_path):
    """
    Walk a dotted path on an object, returning the parent object
    of the final attribute, the final attribute name, and the
    current value of the final attribute.

    Example:
        "transform.offset_x" on a strip ->
            parent = strip.transform, name = "offset_x",
            current = strip.transform.offset_x
    """
    parts = dotted_path.split(".")

    cursor = obj
    for part in parts[:-1]:
        cursor = getattr(cursor, part)

    final_name = parts[-1]
    current = getattr(cursor, final_name)

    return cursor, final_name, current


# =============================================================================
# DYNAMIC CHANNEL ALLOCATOR
# =============================================================================

class ChannelAllocator:
    """
    Expanding-span allocator with real occupancy relocation.

    Rule: when a role needs more channels it expands upward and
    pushes EVERY higher role (and their already-placed strips)
    further up. The new span is stored permanently for that role.
    """

    def __init__(self, max_channel=MAX_PERMANENT_CHANNEL):

        self.max_channel = max_channel

        # role -> (low, high)
        self.spans = {}

        # channel -> list of (start_frame, end_frame)
        self.occupancy = {
            c: []
            for c in range(1, max_channel + 1)
        }

        self.preferred = {
            "music": 1,
            "sfx": 2,
            "audio": 3,
            "video-audio": 4,
            "video-main": 5,
            "video-overlay": 7,
            "transform": 9,
            "text": 10,
        }

        self.ordered_roles = sorted(
            ROLE_WEIGHT.keys(),
            key=lambda r: ROLE_WEIGHT.get(r, 0),
        )

    def _overlaps(self, channel, start, end):

        for s, e in self.occupancy.get(channel, []):

            if not (end <= s or start >= e):
                return True

        return False

    def _get_span(self, role):

        if role not in self.spans:

            base = self.preferred.get(role, 5)

            self.spans[role] = (
                base,
                base,
            )

        return self.spans[role]

    def _set_span(self, role, low, high):

        self.spans[role] = (
            max(1, low),
            min(high, self.max_channel),
        )

    def _find_free(self, low, high, start, end):

        for ch in range(low, high + 1):

            if ch > self.max_channel:
                break

            if not self._overlaps(
                ch,
                start,
                end,
            ):
                return ch

        return None

    def _relocate_occupancy(self, from_ch, to_ch):

        if from_ch == to_ch:
            return

        if from_ch not in self.occupancy:
            return

        intervals = self.occupancy[from_ch]

        if not intervals:
            return

        self.occupancy.setdefault(
            to_ch,
            [],
        ).extend(intervals)

        self.occupancy[from_ch] = []

    def _push_higher_roles(self, from_role, amount):

        if amount <= 0:
            return

        my_w = ROLE_WEIGHT.get(
            from_role,
            0,
        )

        channels_to_move = sorted(
            [
                c
                for c in self.occupancy
                if c >= 1
            ],
            reverse=True,
        )

        for role in reversed(self.ordered_roles):

            if ROLE_WEIGHT.get(role, 0) <= my_w:
                continue

            if role not in self.spans:
                continue

            old_lo, old_hi = self.spans[role]

            new_lo = min(
                old_lo + amount,
                self.max_channel,
            )

            new_hi = min(
                old_hi + amount,
                self.max_channel,
            )

            self.spans[role] = (
                new_lo,
                new_hi,
            )

        for ch in channels_to_move:

            owner = None

            for r, (lo, hi) in self.spans.items():

                if (
                    lo <= ch <= hi
                    and ROLE_WEIGHT.get(r, 0) > my_w
                ):
                    owner = r
                    break

            if owner is None:
                continue

            new_ch = min(
                ch + amount,
                self.max_channel,
            )

            if new_ch != ch:

                self._relocate_occupancy(
                    ch,
                    new_ch,
                )

    def allocate(
        self,
        role,
        start_frame,
        end_frame,
        prefer_pair=False,
    ):

        start = int(start_frame)
        end = int(end_frame)

        video_ch = None
        audio_ch = None

        if role in {
            "video-main",
            "video-overlay",
            "text",
            "transform",
        }:

            low, high = self._get_span(role)

            video_ch = self._find_free(
                low,
                high,
                start,
                end,
            )

            if video_ch is None:

                self._set_span(
                    role,
                    low,
                    high + 1,
                )

                self._push_higher_roles(
                    role,
                    1,
                )

                low, high = self._get_span(role)

                video_ch = self._find_free(
                    low,
                    high,
                    start,
                    end,
                )

            if video_ch is None:
                video_ch = high

            self.occupancy.setdefault(
                video_ch,
                [],
            ).append(
                (
                    start,
                    end,
                )
            )

        if role in {
            "audio",
            "sfx",
            "music",
        }:

            low, high = self._get_span(role)

            audio_ch = self._find_free(
                low,
                high,
                start,
                end,
            )

            if audio_ch is None:

                self._set_span(
                    role,
                    low,
                    high + 1,
                )

                self._push_higher_roles(
                    role,
                    1,
                )

                low, high = self._get_span(role)

                audio_ch = self._find_free(
                    low,
                    high,
                    start,
                    end,
                )

            if audio_ch is None:
                audio_ch = low

            self.occupancy.setdefault(
                audio_ch,
                [],
            ).append(
                (
                    start,
                    end,
                )
            )

        elif prefer_pair and video_ch is not None:

            pair_role = "video-audio"

            if pair_role not in self.spans:

                vlow, _ = self._get_span(role)

                self.spans[pair_role] = (
                    max(1, vlow - 1),
                    max(1, vlow - 1),
                )

            low, high = self._get_span(
                pair_role,
            )

            audio_ch = self._find_free(
                low,
                high,
                start,
                end,
            )

            if audio_ch is None:

                self._set_span(
                    pair_role,
                    low,
                    high + 1,
                )

                self._push_higher_roles(
                    pair_role,
                    1,
                )

                low, high = self._get_span(
                    pair_role,
                )

                audio_ch = self._find_free(
                    low,
                    high,
                    start,
                    end,
                )

            if audio_ch is None:
                audio_ch = low

            self.occupancy.setdefault(
                audio_ch,
                [],
            ).append(
                (
                    start,
                    end,
                )
            )

        return video_ch, audio_ch


# =============================================================================
# VSE BUILDER
# =============================================================================

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

        self._native_durations = {}

        self._clips_by_id = {}

        self._clip_tracks = {}

        self._probe_failures = set()

        self._video_counter = 0
        self._audio_counter = 0
        self._image_counter = 0
        self._text_counter = 0
        self._probe_counter = 0
        self._effect_counter = 0

        self._transform_effects = {}

        self.channel_allocator = (
            ChannelAllocator()
        )

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

    def _next_effect_name(self):

        self._effect_counter += 1

        return f"FX{self._effect_counter:03d}"

    def _next_probe_name(self):

        self._probe_counter += 1

        return f"_probe{self._probe_counter:04d}"

    # =========================================================================
    # GENERATION
    # =========================================================================

    def set_generation(self, generation):

        self.log.info(
            f"Setting new generation {generation.get('_id')}"
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
                "Content-Type": "application/json"
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

    def _infer_extension(self, clip_ref):

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
    # LOCAL AUDIO RESOLUTION
    # =========================================================================

    def _resolve_local_audio(self, clip_ref):

        media_id = (
            clip_ref.get("_id")
            or clip_ref.get("clip_ref_id")
            or clip_ref.get("source_asset_id")
        )

        if not media_id:
            return None

        candidates = [
            LOCAL_AUDIO_STATICS / f"{media_id}.wav",
            LOCAL_AUDIO_STATICS / f"{media_id}.mp3",
            LOCAL_AUDIO_STATICS / f"{media_id}.flac",
            LOCAL_AUDIO_STATICS / f"{media_id}.ogg",
            LOCAL_AUDIO_STATICS / f"{media_id}.aac",
        ]

        for path in candidates:

            if path.exists():

                self.log.info(
                    f"[LOCAL AUDIO] Resolved "
                    f"{media_id} -> {path}"
                )

                return {
                    "filepath": str(path),
                    "media_type": "audio",
                    "resolved": True,
                    "clip_ref": {
                        **clip_ref,
                        "type": "audio",
                    },
                }

        return None

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
            "transform",
        }:

            return False

        return (
            "screenplay_blocks" in clip_ref
            or "accepted_types" in clip_ref
            or "preferred_type" in clip_ref
        )

    def _resolve_placeholder(self, clip_ref):

        preferred = clip_ref.get(
            "preferred_type",
            "image",
        )

        statics = CACHE_ROOT / "statics"

        statics.mkdir(
            parents=True,
            exist_ok=True,
        )

        if preferred == "video":
            return str(statics / "video.mp4")

        if preferred == "audio":
            return str(statics / "audio.wav")

        return str(statics / "image.png")

    def _attach_strip_metadata(
        self,
        strip,
        clip,
        clip_ref,
        resolved,
    ):

        strip["asset_id"] = clip_ref.get("_id")

        strip["instance_id"] = clip.get(
            "instanceId"
        )

        strip["resolved"] = resolved

        strip["preferred_type"] = clip_ref.get(
            "preferred_type"
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

    def _find_cached_media(self, clip_ref):

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
            search_order.append(preferred)

        for media_type in clip_ref.get(
            "accepted_types",
            [],
        ):

            if media_type not in search_order:
                search_order.append(media_type)

        explicit_type = clip_ref.get("type")

        if (
            explicit_type
            and explicit_type not in search_order
        ):

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

        if (
            preferred == "audio"
            or "audio" in (
                clip_ref.get(
                    "accepted_types"
                )
                or []
            )
            or explicit_type == "audio"
        ):

            local = self._resolve_local_audio(
                clip_ref
            )

            if local:
                return local

        return None

    def _resolve_media(self, clip_ref):

        clip_ref = clip_ref or {}

        self.log.info(
            f"Resolving media: {clip_ref}"
        )

        preferred = (
            clip_ref.get("preferred_type")
            or clip_ref.get("type")
        )

        accepted = set(
            clip_ref.get(
                "accepted_types"
            )
            or []
        )

        if (
            preferred == "audio"
            or "audio" in accepted
            or preferred is None
        ):

            local = self._resolve_local_audio(
                clip_ref
            )

            if local:
                return local

        cached = self._find_cached_media(
            clip_ref
        )

        if cached:

            return {
                **cached,
                "resolved": True,
            }

        if self._is_unresolved_clip_ref(
            clip_ref
        ):

            preferred = clip_ref.get(
                "preferred_type",
                "image",
            )

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

        if not self.resolving_media:

            self.resolving_media = True

            self.update_server_status(
                "RESOLVING_MEDIA"
            )

        media_type = clip_ref.get("type")
        media_id = clip_ref.get("_id")

        if media_type == "text":

            return {
                "filepath": clip_ref.get(
                    "text",
                    "",
                ),
                "media_type": "text",
                "resolved": True,
            }

        if media_type == "scene":

            return {
                "filepath": None,
                "media_type": "scene",
                "resolved": True,
            }

        if media_type == "transform":

            return {
                "filepath": None,
                "media_type": "transform",
                "resolved": True,
            }

        if media_type not in {
            "video",
            "audio",
            "image",
        }:

            self.log.error(
                f"Unsupported media type: {media_type}"
            )

            return None

        if not media_id:

            self.log.error(
                "Media has no _id. Cannot resolve media."
            )

            return None

        media_dir = (
            CACHE_ROOT
            / media_id.replace(":", "_")
        )

        chunks_dir = media_dir / "chunks"

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

            chunk = data.get("chunk")

            if not chunk:

                self.log.error(
                    f"Chunk {index} for media "
                    f"'{media_id}' contained "
                    f"no data."
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
            f"Media assembled: {final_path}"
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

        cut = clip.get("cut") or {}

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

                if isinstance(
                    value,
                    (int, float),
                ):

                    return max(
                        1,
                        int(round(value)),
                    )

                if (
                    isinstance(value, str)
                    and self.timeline is not None
                ):

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

        if not clip:
            return None

        clip_id = clip.get("_id")

        if not clip_id:

            self.log.error(
                "[PROBE] Clip has no _id"
            )

            return None

        if clip_id in self._native_durations:

            return self._native_durations[
                clip_id
            ]

        self.log.info(
            f"[PROBE] Starting clip={clip_id}"
        )

        clip_ref = (
            clip.get("clip_ref")
            or {}
        )

        media = self._resolve_media(
            clip_ref
        )

        if media is None:

            self.log.warning(
                f"[PROBE] Media resolution "
                f"failed for clip '{clip_id}'."
            )

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

                return frames

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

        if media_type in {
            "text",
            "scene",
            "transform",
        }:

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

            return duration

        if media_type not in {
            "video",
            "audio",
        }:

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

            return duration

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

        return fallback

    def _probe_native_duration_frames(
        self,
        filepath,
        kind,
        clip_id,
    ):

        strip = None

        try:

            name = self._next_probe_name()

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
                    f"Unsupported probe type: {kind}"
                )

            duration = int(
                strip.frame_duration
            )

            return max(
                1,
                duration,
            )

        except Exception as e:

            self.log.error(
                f"[PROBE] Failed for clip "
                f"'{clip_id}' '{filepath}': {e}"
            )

            return None

        finally:

            if strip is not None:

                self._remove_strip_safely(
                    strip
                )

    def _remove_strip_safely(
        self,
        strip,
        max_attempts=2,
    ):

        name = getattr(
            strip,
            "name",
            "<unknown>",
        )

        for attempt in range(
            1,
            max_attempts + 1,
        ):

            try:

                self.sequencer.sequences.remove(
                    strip
                )

            except Exception as e:

                self.log.warning(
                    f"[PROBE] remove() raised "
                    f"for '{name}' "
                    f"(attempt {attempt}): {e}"
                )

            still_present = (
                self.sequencer.sequences.get(
                    name
                )
            )

            if still_present is None:

                return True

            strip = still_present

        return False

    def _purge_stray_probe_strips(self):

        stray = [
            s
            for s in self.sequencer.sequences_all
            if s.channel >= PROBE_CHANNEL
        ]

        for s in stray:

            name = s.name
            channel = s.channel

            try:

                self.sequencer.sequences.remove(
                    s
                )

                self.log.warning(
                    f"[PROBE] Purged stray "
                    f"strip '{name}' "
                    f"left on channel {channel}"
                )

            except Exception as e:

                self.log.error(
                    f"[PROBE] Failed to purge "
                    f"strip '{name}' "
                    f"on channel {channel}: {e}"
                )

    # =========================================================================
    # CRITICAL: LAZY DURATION PROVIDER
    # =========================================================================

    def get_clip_duration_ms(
        self,
        clip_id,
    ):

        if not clip_id:

            raise TimelineResolutionError(
                "Cannot resolve duration "
                "for clip with no id."
            )

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

        clip = self._clips_by_id.get(
            clip_id
        )

        if clip is not None:

            track = self._clip_tracks.get(
                clip_id
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

        return round(
            fallback_frames
            * 1000
            / self.fps
        )

    # =========================================================================
    # TRACK MEDIA TYPE / ROLE
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
                clip.get("clip_ref")
                or {}
            )

            media_type = (
                clip_ref.get("type")
                or clip_ref.get("preferred_type")
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

        guess = self._infer_track_media_type(
            track
        )

        if guess == "video":
            return "video-overlay"

        if guess == "audio":
            return "audio"

        if guess == "text":
            return "text"

        return None

    def _is_positionable_track(
        self,
        track,
    ):

        return (
            self._assign_track_role(track)
            is not None
        )

    def _resolve_channels(
        self,
        clip,
        track,
        start_frame,
        duration_frames,
    ):

        role = self._assign_track_role(
            track
        )

        if role is None:
            return None, None

        clip_ref = (
            clip.get("clip_ref")
            or {}
        )

        media = (
            clip.get("_resolved_media")
            or {}
        )

        media_type = (
            media.get("media_type")
            or clip_ref.get("type")
            or clip_ref.get("preferred_type")
        )

        prefer_pair = (
            media_type == "video"
        )

        return self.channel_allocator.allocate(
            role=role,
            start_frame=int(start_frame),
            end_frame=int(
                start_frame
                + duration_frames
            ),
            prefer_pair=prefer_pair,
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

            video_channel, audio_channel = (
                self._resolve_channels(
                    clip,
                    track,
                    start_frame,
                    duration_frames,
                )
            )

            if video_channel is None:

                raise ValueError(
                    "No video channel "
                    "available for video clip."
                )

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

            if audio_channel is not None:

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

            return video

        except Exception as e:

            self.log.error(
                f"Failed adding VIDEO/AUDIO: {e}"
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

            _, audio_channel = (
                self._resolve_channels(
                    clip,
                    track,
                    start_frame,
                    duration_frames,
                )
            )

            if audio_channel is None:

                raise ValueError(
                    "No audio channel available."
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

            return audio

        except Exception as e:

            self.log.error(
                f"Failed to add AUDIO ONLY: {e}"
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
                    "Failed to resolve image media."
                )

                return None

            video_channel, _ = (
                self._resolve_channels(
                    clip,
                    track,
                    start_frame,
                    duration_frames,
                )
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

            image_strip["strip_role"] = (
                "image"
            )

            return image_strip

        except Exception as e:

            self.log.error(
                f"Failed to add IMAGE: {e}"
            )

            return None

    # =========================================================================
    # DECLARED TRANSFORMS
    #
    # Transforms belong to the clip that declares them.
    #
    # Example:
    #
    # "transforms": {
    #     "translate_x": [
    #         {
    #             "t": {
    #                 "type": "expression",
    #                 "value": "scene:abc:start"
    #             },
    #             "value": 0
    #         },
    #         {
    #             "t": {
    #                 "type": "expression",
    #                 "value": "scene:abc:start + 2"
    #             },
    #             "value": 500
    #         }
    #     ],
    #     "scale_x": [...],
    #     "scale_y": [...],
    #     "rotation": [...],
    #     "color": [...]
    # }
    #
    # Both shapes are accepted for each transform:
    #
    # 1) Bare keyframe list:
    #     "color": [
    #         {"t": 0, "value": "#ffffff"},
    #         {"t": 1, "value": "#ffcc33"}
    #     ]
    #
    # 2) Wrapped with curve:
    #     "color": {
    #         "curve": "constant",
    #         "keyframes": [
    #             {"t": 0, "value": "#ffffff"},
    #             {"t": 1, "value": "#ffcc33"}
    #         ]
    #     }
    #
    # Both are normalized to a single internal shape:
    #     {"curve": "...", "keyframes": [...]}
    #
    # The curve controls Blender keyframe interpolation:
    #     "linear"   -> LINEAR
    #     "constant" -> CONSTANT  (no blending, hold previous value)
    #     "bezier"   -> BEZIER
    #
    # Use "constant" for karaoke-style word highlighting: Blender
    # holds the previous color exactly and jumps instantly to the
    # next one, with no fading or color interpolation.
    #
    # NO separate TRANSFORM effect strip is ever created.
    # The keyframes are written directly onto the target VSE strip.
    # =========================================================================

    def _get_declared_transforms(self, clip):
        """
        Return the transform declaration belonging to a clip.

        The canonical location is:

            clip["transforms"]

        For compatibility, also check clip_ref["transforms"].
        """
        if not clip:
            return {}

        transforms = clip.get("transforms")

        if isinstance(transforms, dict):
            return transforms

        clip_ref = clip.get("clip_ref") or {}

        transforms = clip_ref.get("transforms")

        if isinstance(transforms, dict):
            return transforms

        return {}

    def _normalize_transform(self, transform_data):
        """
        Normalize a single transform's value into a single internal shape.

        Accepts EITHER:

            1) A bare list of keyframes:
                [
                    {"t": 0, "value": "#ffffff"},
                    {"t": 1, "value": "#ffcc33"}
                ]

            2) A dict with curve + keyframes:
                {
                    "curve": "constant",
                    "keyframes": [
                        {"t": 0, "value": "#ffffff"},
                        {"t": 1, "value": "#ffcc33"}
                    ]
                }

        Returns the unified internal shape:

            {
                "curve": "linear" | "constant" | "bezier",
                "keyframes": [ ... ]
            }
        """
        if isinstance(transform_data, list):
            return {
                "curve": "linear",
                "keyframes": transform_data,
            }

        if isinstance(transform_data, dict):
            curve = transform_data.get("curve", "linear")
            keyframes = transform_data.get("keyframes", [])
            return {
                "curve": curve if curve in INTERPOLATION_MAP else "linear",
                "keyframes": keyframes,
            }

        return {
            "curve": "linear",
            "keyframes": [],
        }

    def _resolve_transform_time(self, value):
        """
        Resolve a transform keyframe time into a Blender frame.

        Supported forms:

            12
            12.0

            {
                "type": "expression",
                "value": "scene:abc:start + 2.5"
            }

        The expression is resolved by TimelineResolver, exactly like
        normal clip start/end expressions.
        """
        if value is None:
            raise TimelineResolutionError(
                "Transform keyframe has no time value."
            )

        # Already a numeric frame/time value.
        if isinstance(value, (int, float)):
            return int(round(value))

        if self.timeline is None:
            raise TimelineResolutionError(
                "Cannot resolve transform time before timeline exists."
            )

        # A full timing object, e.g. {"type": "expression", "value": "..."}.
        # Pass it through untouched so TimelineResolver.resolve_ms() can
        # dispatch on its own "type" field (this is what "expression",
        # "percentage", "reference", etc. all need).
        if isinstance(value, dict):
            milliseconds = self.timeline.resolve_ms(value)
            return int(self.timeline.ms_to_frames(milliseconds))

        # A bare expression string with no "type" wrapper.
        if isinstance(value, str):
            milliseconds = self.timeline.resolve_expression(value)
            return int(self.timeline.ms_to_frames(milliseconds))

        raise TimelineResolutionError(
            f"Unsupported transform time value: {value!r}"
        )

    # -------------------------------------------------------------------------
    # Fcurve lookup
    # -------------------------------------------------------------------------

    def _find_strip_fcurves(self, strip, data_path):
        """
        Find ALL fcurves on the scene's action that correspond to
        (strip, data_path) for sequence-strip keyframes.

        Sequence strip keyframes live in the scene's animation action.
        The fcurve data_path is a fully-qualified RNA path that
        contains the strip's name, e.g.:

            sequence_editor.sequences_all["V001"].transform.offset_x

        CRITICAL: for multi-component properties (RGBA color has
        R, G, B, A), Blender creates ONE fcurve per component, all
        sharing the same data_path but with different `array_index`
        values (0, 1, 2, 3). ALL of them must be located and
        modified, otherwise the rendered result will be a blend of
        CONSTANT and BEZIER components — which looks like a
        gradual fade even when the user asked for a sharp switch.
        """
        scene = bpy.context.scene

        if (
            scene is None
            or scene.animation_data is None
            or scene.animation_data.action is None
        ):
            return []

        action = scene.animation_data.action

        strip_name = getattr(strip, "name", None)

        if not strip_name:
            return []

        matches = []

        for fc in action.fcurves:
            if strip_name not in fc.data_path:
                continue
            # Require a "." before the property name so we don't
            # accidentally match "wrap_color" or "bgcolor" when
            # looking for "color".
            if not fc.data_path.endswith(f".{data_path}"):
                continue
            matches.append(fc)

        return matches

    def _set_keyframe_interpolation(
        self,
        strip,
        data_path,
        frame,
        interpolation,
    ):
        """
        Set the interpolation mode of the keyframe at `frame` on
        EVERY fcurve that belongs to (strip, data_path).

        This must iterate over ALL matching fcurves, not just the
        first. For a TEXT strip's `color` property, Blender creates
        4 fcurves (R, G, B, A). If only one of them gets its
        interpolation switched to CONSTANT, the other three stay
        on BEZIER and the rendered color fades smoothly between
        keyframes — which is the exact "not a sharp switch"
        symptom you see when CONSTANT isn't really applied.

        Returns the number of fcurves whose keyframe was
        successfully modified.
        """
        interpolation = str(interpolation).upper()

        if interpolation not in {"LINEAR", "CONSTANT", "BEZIER"}:
            interpolation = "LINEAR"

        strip_name = getattr(strip, "name", None)

        if not strip_name:
            return 0

        matched = self._find_strip_fcurves(
            strip,
            data_path,
        )

        if not matched:

            # Diagnostic: log whatever fcurves DO exist for this
            # strip so a wrong data_path or a missing action is
            # obvious from the log.
            scene = bpy.context.scene
            existing = []

            if (
                scene is not None
                and scene.animation_data is not None
                and scene.animation_data.action is not None
            ):
                existing = [
                    fc.data_path
                    for fc in scene.animation_data.action.fcurves
                    if strip_name in fc.data_path
                ]

            self.log.warning(
                f"[TRANSFORM] No fcurve found for "
                f"strip='{strip_name}' data_path='{data_path}' "
                f"frame={frame}. "
                f"Existing fcurves containing this strip: {existing}"
            )

            return 0

        modified = 0

        for fc in matched:

            for kp in fc.keyframe_points:

                if int(kp.co[0]) != int(frame):
                    continue

                try:
                    kp.interpolation = interpolation
                    # Clamp handles so the graph editor doesn't
                    # show misleading tangents. For CONSTANT
                    # specifically, handles are ignored, but
                    # setting them keeps the representation sane.
                    kp.handle_left_type = "AUTO_CLAMPED"
                    kp.handle_right_type = "AUTO_CLAMPED"

                    # Force the fcurve to re-evaluate.
                    fc.update()

                    actual = kp.interpolation

                    if actual == interpolation:

                        self.log.info(
                            f"[TRANSFORM] Set "
                            f"interpolation={interpolation} "
                            f"on {strip_name}."
                            f"{fc.data_path}[{fc.array_index}] "
                            f"frame={frame}"
                        )

                        modified += 1

                    else:

                        self.log.warning(
                            f"[TRANSFORM] Interpolation "
                            f"did not stick: "
                            f"requested={interpolation} "
                            f"actual={actual} "
                            f"on {strip_name}."
                            f"{fc.data_path}[{fc.array_index}] "
                            f"frame={frame}"
                        )

                except Exception as exc:
                    self.log.warning(
                        f"[TRANSFORM] Could not set "
                        f"interpolation on "
                        f"{strip_name}."
                        f"{fc.data_path}[{fc.array_index}] "
                        f"frame {frame}: {exc}"
                    )

                # Only the first matching keyframe per fcurve.
                break

        if modified == 0:

            self.log.warning(
                f"[TRANSFORM] Matched {len(matched)} fcurve(s) "
                f"for {strip_name}.{data_path} but no keyframe "
                f"was found at frame {frame} on any of them."
            )

        return modified

    def _keyframe_strip_property(
        self,
        strip,
        data_path,
        frame,
        index=None,
        interpolation="LINEAR",
    ):
        """
        Insert a keyframe on a sequence-strip property AND set its
        interpolation mode.

        Keeping this isolated makes Blender-version differences
        (where the keyframe can actually be addressed) easier to
        handle.
        """
        frame = int(frame)

        interpolation = str(interpolation).upper()

        if interpolation not in {"LINEAR", "CONSTANT", "BEZIER"}:
            interpolation = "LINEAR"

        try:
            if index is None:
                strip.keyframe_insert(
                    data_path=data_path,
                    frame=frame,
                )
            else:
                strip.keyframe_insert(
                    data_path=data_path,
                    index=index,
                    frame=frame,
                )

        except Exception as exc:

            self.log.error(
                f"[TRANSFORM] Failed keyframe_insert "
                f"strip={getattr(strip, 'name', '<unknown>')} "
                f"path={data_path} "
                f"index={index} "
                f"frame={frame}: {exc}"
            )

            return False

        # Now set the interpolation on the just-inserted keyframe.
        # For multi-component properties (RGBA color) this must hit
        # every fcurve, otherwise the rendered result is a blend
        # of CONSTANT and BEZIER components and you get a visible
        # smooth fade between the "snap" colors.
        modified = self._set_keyframe_interpolation(
            strip=strip,
            data_path=data_path,
            frame=frame,
            interpolation=interpolation,
        )

        if modified == 0:
            self.log.warning(
                f"[TRANSFORM] keyframe_insert OK but no fcurve "
                f"interpolation was set on "
                f"strip={getattr(strip, 'name', '<unknown>')} "
                f"path={data_path} frame={frame} "
                f"interpolation={interpolation}. "
                f"Keyframe will fall back to Blender's default "
                f"interpolation (BEZIER)."
            )

        return True

    def _apply_generic_keyframe(
        self,
        strip,
        transform_name,
        frame,
        value,
        interpolation="LINEAR",
    ):
        """
        Apply ONE keyframe of ONE transform property to a strip,
        using the property registry (`TRANSFORM_PROPERTY_MAP`).

        Steps:
            1. Look up the property spec.
            2. Verify the strip exposes the required attribute
               (e.g., `transform` for nested properties).
            3. Convert the JSON value to the Blender value.
            4. Walk the dotted data_path and assign.
            5. Insert the keyframe (which also sets the
               interpolation on every component fcurve).
        """
        spec = TRANSFORM_PROPERTY_MAP.get(transform_name)

        if spec is None:
            self.log.warning(
                f"[TRANSFORM] Unknown property "
                f"'{transform_name}' on strip '{strip.name}'. "
                f"Add it to TRANSFORM_PROPERTY_MAP if you want it "
                f"supported. Known properties: "
                f"{sorted(TRANSFORM_PROPERTY_MAP.keys())}"
            )
            return False

        data_path = spec["data_path"]
        requires = spec.get("requires", data_path)
        convert = spec.get("convert", lambda v: v)

        # ---- 1. Attribute check ------------------------------------
        # `hasattr` on a dotted path is unreliable, so we always
        # check the top-level attribute only.
        if not hasattr(strip, requires):
            self.log.warning(
                f"[TRANSFORM] Strip '{strip.name}' has no "
                f"attribute '{requires}'; cannot keyframe "
                f"'{transform_name}' ({data_path})."
            )
            return False

        # ---- 2. Value conversion -----------------------------------
        try:
            converted = convert(value)
        except Exception as exc:
            self.log.error(
                f"[TRANSFORM] Could not convert value "
                f"{value!r} for '{transform_name}' on "
                f"strip '{strip.name}': {exc}"
            )
            return False

        # ---- 3. Assign on the strip --------------------------------
        try:
            parent, attr_name, _ = _resolve_nested_attr(
                strip,
                data_path,
            )
            setattr(parent, attr_name, converted)
        except Exception as exc:
            self.log.error(
                f"[TRANSFORM] Could not assign "
                f"{transform_name}={converted!r} to "
                f"strip '{strip.name}' at {data_path}: {exc}"
            )
            return False

        # ---- 4. Insert keyframe + set interpolation ----------------
        return self._keyframe_strip_property(
            strip,
            data_path,
            frame,
            interpolation=interpolation,
        )

    def _apply_transform(
        self,
        strip,
        transform_name,
        keyframes,
        curve="linear",
    ):
        """
        Apply a single declared transform (with its curve) to `strip`.

        `curve` is one of:

            "linear"   -> LINEAR   (Blender default)
            "constant" -> CONSTANT (hold previous value, jump at keyframe)
            "bezier"   -> BEZIER   (smooth bezier)

        For karaoke-style word highlighting, use "constant" so Blender
        holds the previous color exactly and jumps instantly to the
        next one with no fading.

        Works for any property registered in TRANSFORM_PROPERTY_MAP.
        Adding a new property is a one-line entry in that table — no
        new method needed here.

        Returns a tuple: (applied_count, failed_count)
        """
        transform_name = str(transform_name).strip().lower()

        curve_key = str(curve).strip().lower()

        if curve_key not in INTERPOLATION_MAP:
            curve_key = "linear"

        interpolation = INTERPOLATION_MAP[curve_key]

        if transform_name not in TRANSFORM_PROPERTY_MAP:
            self.log.warning(
                f"[TRANSFORM] Unsupported property "
                f"'{transform_name}' on strip '{strip.name}'. "
                f"Known properties: "
                f"{sorted(TRANSFORM_PROPERTY_MAP.keys())}"
            )
            return 0, 0

        if not isinstance(keyframes, list):
            self.log.warning(
                f"[TRANSFORM] Expected list of keyframes for "
                f"'{transform_name}' on strip '{strip.name}', "
                f"got {type(keyframes).__name__}."
            )
            return 0, 0

        applied = 0
        failed = 0

        for keyframe in keyframes:

            if not isinstance(keyframe, dict):
                self.log.warning(
                    f"[TRANSFORM] Invalid keyframe for "
                    f"'{transform_name}' on strip '{strip.name}': "
                    f"{keyframe!r}"
                )
                failed += 1
                continue

            t = keyframe.get("t")

            if t is None:
                self.log.warning(
                    f"[TRANSFORM] Keyframe has no 't' "
                    f"for property '{transform_name}' "
                    f"on strip '{strip.name}'."
                )
                failed += 1
                continue

            if "value" not in keyframe:
                self.log.warning(
                    f"[TRANSFORM] Keyframe has no 'value' "
                    f"for property '{transform_name}' "
                    f"on strip '{strip.name}'."
                )
                failed += 1
                continue

            value = keyframe.get("value")

            try:
                frame = self._resolve_transform_time(t)

                success = self._apply_generic_keyframe(
                    strip=strip,
                    transform_name=transform_name,
                    frame=frame,
                    value=value,
                    interpolation=interpolation,
                )

                if success:
                    applied += 1

                    self.log.info(
                        f"[TRANSFORM] "
                        f"{strip.name} "
                        f"{transform_name}={value!r} "
                        f"frame={frame} "
                        f"curve={curve_key}"
                    )
                else:
                    failed += 1

            except Exception as exc:
                failed += 1

                self.log.error(
                    f"[TRANSFORM] Failed applying "
                    f"{transform_name}={value!r} "
                    f"to '{strip.name}': {exc}"
                )

        return applied, failed

    def _apply_declared_transforms(
        self,
        clip,
        target_strip=None,
    ):
        """
        Apply every declared transform belonging to `clip`.

        IMPORTANT:

        This method does NOT create a transform effect strip.

        `target_strip` is the actual VSE strip produced from the clip.

        Each transform value is first normalized via
        `_normalize_transform` so it accepts BOTH shapes:

            "color": [ { "t": ..., "value": ... }, ... ]

            "color": {
                "curve": "constant",
                "keyframes": [ { "t": ..., "value": ... }, ... ]
            }

        Then `_apply_transform` is called with the resolved curve
        so that Blender's keyframe interpolation is set correctly
        (LINEAR / CONSTANT / BEZIER).

        Example:

            clip
              |
              +-- transforms
              |
              v
            TEXT strip

        or:

            clip
              |
              +-- transforms
              |
              v
            MOVIE strip
        """
        if not clip:
            return target_strip

        transforms = self._get_declared_transforms(clip)

        if not transforms:
            return target_strip

        clip_id = clip.get("_id")

        # If the caller didn't explicitly provide the target, resolve
        # it from the compiled strip map.
        if target_strip is None and clip_id:
            target_strip = self.strips.get(clip_id)

        if target_strip is None:

            self.log.error(
                f"[TRANSFORM] No target strip available for "
                f"clip '{clip_id}'."
            )
            return None

        self.log.info(
            f"[TRANSFORM] Applying declared transforms "
            f"clip={clip_id} target={target_strip.name}"
        )

        total_applied = 0
        total_failed = 0

        for property_name, transform_data in transforms.items():

            normalized = self._normalize_transform(
                transform_data,
            )

            curve = normalized["curve"]
            keyframes = normalized["keyframes"]

            applied, failed = self._apply_transform(
                strip=target_strip,
                transform_name=property_name,
                keyframes=keyframes,
                curve=curve,
            )

            total_applied += applied
            total_failed += failed

        # Keep useful metadata on the target strip.
        try:
            target_strip["declared_transforms"] = json.dumps(
                transforms,
                default=str,
            )
        except Exception:
            pass

        target_strip["has_declared_transforms"] = bool(total_applied)

        self.log.info(
            f"[TRANSFORM] Completed clip={clip_id} "
            f"target={target_strip.name} "
            f"applied={total_applied} failed={total_failed}"
        )

        return target_strip

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
        self._effect_counter = 0

        self.strips = {}

        self._compiled_strips = (
            self.strips
        )

        self._native_durations = {}

        self._clips_by_id = {}

        self._clip_tracks = {}

        self._probe_failures = set()

        self._transform_effects = {}

        self.resolving_media = False

        self.channel_allocator = (
            ChannelAllocator()
        )

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

        self._clear_sequencer()

        tracks = seq.get(
            "tracks",
            [],
        )

        # ---------------------------------------------------------------------
        # INDEX ALL CLIPS
        # ---------------------------------------------------------------------

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
                        f"{track_id}-clip-{clip_index}"
                    )

                    clip["_id"] = clip_id

                self._clips_by_id[
                    clip_id
                ] = clip

                self._clip_tracks[
                    clip_id
                ] = track

        # ---------------------------------------------------------------------
        # PASS 1 - REGISTER
        # ---------------------------------------------------------------------

        self.log.info(
            "[BUILD] PASS 1 - registering tracks and clips"
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

            if not self._is_positionable_track(
                track
            ):

                self.log.info(
                    f"[CHANNEL] {track_id} "
                    f"(type={track.get('type')}) "
                    f"-> not positionable, "
                    f"registered only"
                )

        # ---------------------------------------------------------------------
        # PASS 2 - RESOLVE MEDIA + PROBE
        # ---------------------------------------------------------------------

        self.log.info(
            "[BUILD] PASS 2 - resolving media "
            "and probing durations"
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

        self._purge_stray_probe_strips()

        # ---------------------------------------------------------------------
        # PASS 3 - RESOLVE EDITORIAL TIMELINE
        # ---------------------------------------------------------------------

        self.log.info(
            "[BUILD] PASS 3 - resolving editorial timeline"
        )

        self.update_server_status(
            "RESOLVING_TIMELINE"
        )

        self.timeline_resolver.resolve_timeline()

        # ---------------------------------------------------------------------
        # PASS 4 - MATERIALIZE TEXT
        # ---------------------------------------------------------------------

        self.log.info(
            "[BUILD] PASS 4 - materializing text"
        )

        self._materialize_text_clips(
            tracks
        )

        # ---------------------------------------------------------------------
        # PASS 5 - MATERIALIZE MEDIA
        #
        # Declared transforms are applied inline, immediately after each
        # clip's actual VSE strip is created (see
        # `_materialize_resolved_clip` and `_create_text_strip`). There is
        # no separate global transform pass: a transform is applied
        # directly to the strip that already exists for its clip, so no
        # standalone TRANSFORM effect strip is ever created.
        # ---------------------------------------------------------------------

        self.log.info(
            "[BUILD] PASS 5 - materializing media "
            "(priority sorted)"
        )

        self.update_server_status(
            "BUILDING_VSE"
        )

        materializable = []

        for track in tracks:

            if not self._is_positionable_track(
                track
            ):
                continue

            role = self._assign_track_role(
                track
            )

            weight = ROLE_WEIGHT.get(
                role,
                0,
            )

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

                media_type = media.get(
                    "media_type"
                )

                if media_type in {
                    "text",
                    "scene",
                }:

                    continue

                materializable.append(
                    (
                        weight,
                        clip,
                        track,
                    )
                )

        materializable.sort(
            key=lambda t: t[0]
        )

        for (
            weight,
            clip,
            track,
        ) in materializable:

            self._materialize_resolved_clip(
                clip,
                track,
                fps,
            )

        self._purge_stray_probe_strips()

        # ---------------------------------------------------------------------
        # FINALIZE
        # ---------------------------------------------------------------------

        self.fit_scene_to_timeline()

        self.update_server_status(
            "VSE_READY"
        )

        self.log.info(
            f"VSE build completed. "
            f"{len(self.strips)} strips materialized."
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
                f"No resolved media for clip "
                f"'{clip_id}'"
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
                f"No resolved timing for "
                f"clip '{clip_id}'"
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

        # ---------------------------------------------------------------------
        # THE IMPORTANT PART
        #
        # The actual strip is now registered as the target BEFORE
        # declared transforms are applied.
        # ---------------------------------------------------------------------

        if (
            strip is not None
            and clip_id
        ):

            self.strips[
                clip_id
            ] = strip

            self._apply_declared_transforms(
                clip=clip,
                target_strip=strip,
            )

        return strip

    # =========================================================================
    # TEXT MATERIALIZATION
    # =========================================================================

    def _materialize_text_clips(
        self,
        tracks,
    ):

        for track in tracks:

            if not self._is_positionable_track(
                track
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

                if media.get(
                    "media_type"
                ) != "text":

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

        clip_ref = (
            clip.get(
                "clip_ref",
                {},
            )
        )

        text = (
            clip_ref.get("text")
            or clip_ref.get("value")
            or "Text strip"
        )

        duration_frames = (
            end_frame
            - start_frame
        )

        video_channel, _ = (
            self._resolve_channels(
                clip,
                track,
                start_frame,
                duration_frames,
            )
        )

        if video_channel is None:

            self.log.error(
                f"No text channel "
                f"for clip '{clip.get('_id')}'"
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

        txt["strip_role"] = "text"

        txt.text = text

        # ---------------------------------------------------------------------
        # APPLY TRANSFORMS TO THIS EXACT TEXT STRIP
        # ---------------------------------------------------------------------

        self._apply_declared_transforms(
            clip=clip,
            target_strip=txt,
        )

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
            )
            .isoformat(
                timespec="milliseconds"
            )
            .replace(
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
                f"update_server_status skipped "
                f"({status}): no generation"
            )

            return

        if not hasattr(
            self,
            "machine_id",
        ):

            self.machine_id = "unknown"

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
            f"{self.server_url}/update_generation_status",
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

        description = self.instruction.get(
            "description",
            "",
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

        total_size = filepath.stat().st_size

        if total_size <= 0:

            self.log.error(
                "Render file is empty"
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

                response = (
                    self._post_json(
                        f"{self.editor_url}/upload_media",
                        {
                            "media_id":
                                media_id,
                            "chunk":
                                encoded,
                            "index":
                                index,
                            "size":
                                len(
                                    chunk_bytes
                                ),
                            "total_chunks":
                                total_chunks,
                        },
                    )
                )

                if not response.get(
                    "ok",
                    False,
                ):

                    self.log.error(
                        f"Failed uploading "
                        f"render chunk {index}"
                    )

                    return None

        response = self._post_json(
            f"{self.editor_url}/add_media",
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
                "Failed to add media metadata"
            )

            return None

        return response["data"]

    # =========================================================================
    # GENERATION COMPLETE
    # =========================================================================

    def generation_complete(
        self,
        media_id,
    ):

        return self._post_json(
            f"{self.server_url}/generation_complete",
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

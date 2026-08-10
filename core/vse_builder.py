import bpy

from ..core.logger import Logger
from pathlib import Path
from datetime import datetime, timezone

import urllib.request
import json
import base64
import math
import uuid

from .timeline_resolver import TimelineResolver, TimelineResolutionError
from .vse_renderer import Vse_renderer


# =============================================================================
# CACHE
# =============================================================================

CACHE_ROOT = Path.home() / "VSEInstructorCache"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)


# =============================================================================
# TRANSFORM MAP
# =============================================================================

TRANSFORM_MAP = {
    "scale": {
        "effect": "TRANSFORM",
        "axes": {
            "x": "scale_start_x",
            "y": "scale_start_y",
            "v": "scale_start_x",
        },
    },
    "translate": {
        "effect": "TRANSFORM",
        "axes": {
            "x": "translate_start_x",
            "y": "translate_start_y",
        },
    },
    "rotate": {
        "effect": "TRANSFORM",
        "axes": {
            "v": "rotation_start",
        },
    },
    "crop": {
        "effect": "CROP",
        "axes": {
            "left": "left",
            "right": "right",
            "top": "top",
            "bottom": "bottom",
        },
    },
    "opacity": {
        "effect": "strip",
        "axes": {"v": "blend_alpha"},
    },
    "volume": {
        "effect": "strip",
        "axes": {"v": "volume"},
    },
    "pan": {
        "effect": "strip",
        "axes": {"v": "pan"},
    },
}


# =============================================================================
# CHANNEL LAYOUT
#
# Bottom of timeline (low channel) -> top (high channel = drawn on top)
#
#   video-main      video + paired audio + transform   (bottom)
#   audio           dialogue / voiceover audio
#   video-overlay   secondary visual / overlay / graphics tracks (+ paired
#                   audio + transform, in case they carry one)
#   text            subtitle / title
#   sfx             sound effects
#   music           ambience / music                   (top of known roles)
#   (buoyant)       anything unrecognized or unpositionable (e.g. metadata)
#                   floats above everything
#
# Track role is now determined by the track's `type` field, not by a
# hardcoded track id. Known types map straight to a role below. Any track
# with a missing or unrecognized `type` falls back to content inference
# (looking at what its clips actually contain).
# =============================================================================

# Every track `type` the editorial payload can send us, mapped to a role.
# A value of None means "no fixed position" -> the track floats ("buoyant")
# above all known roles, in the order it appeared.
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
    "metadata": None,  # not positionable media -> buoyant
}

# Stack order, bottom to top, for every role we recognize.
ROLE_ORDER = [
    "video-main",
    "audio",
    "video-overlay",
    "text",
    "sfx",
    "music",
]

# Channels each role's tracks occupy. video-main / video-overlay need 3
# (video, paired audio, transform) - everything else is a single channel.
ROLE_WIDTH = {
    "video-main": 3,
    "audio": 1,
    "video-overlay": 3,
    "text": 1,
    "sfx": 1,
    "music": 1,
}


MEDIA_EXTENSIONS = {
    "video": [".mp4", ".mov", ".mkv", ".avi", ".webm"],
    "audio": [".wav", ".mp3", ".ogg", ".flac", ".aac"],
    "image": [".png", ".jpg", ".jpeg", ".webp", ".tif"],
}


class VSEBuilder(Vse_renderer):

    server_url = "https://blender-backend.vercel.app"

    def __init__(self, instruction):
        self.log = Logger()
        self.log.info("Initializing VSEBuilder...")
        self.log.info(f"Instruction received: {instruction}")

        self.editor_url = "https://editor-backend-xi.vercel.app"
        self.server_url = "https://blender-backend.vercel.app"

        self.instruction = instruction
        self.sequence = instruction.get("sequence", instruction)
        self.generation = None
        self.resolving_media = False
        self.sequencer = bpy.context.scene.sequence_editor
        self.timeline = None
        self.fps = getattr(bpy.context.scene.render, "fps", 24)
        self.strips = {}
        self._compiled_strips = self.strips

        self._video_counter = 0
        self._audio_counter = 0
        self._image_counter = 0
        self._text_counter = 0
        self._transform_counter = 0

        if self.sequencer is None:
            self.log.info("No sequence editor found. Creating one...")
            self.sequencer = bpy.context.scene.sequence_editor_create()
        else:
            self.log.info("Sequence editor found and ready.")

    # -------------------------------------------------------------------------
    # Naming
    # -------------------------------------------------------------------------

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

    def _next_transform_name(self):
        self._transform_counter += 1
        return f"FX{self._transform_counter:03d}"

    def set_generation(self, generation):
        self.log.info(f"Setting new generation {generation.get('_id')}")
        self.generation = generation

    # -------------------------------------------------------------------------
    # Media helpers
    # -------------------------------------------------------------------------

    def _fetch_chunk_from_server(self, media_id, index):
        payload = json.dumps({"media_id": media_id, "index": index}).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.editor_url}/read_upload",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as res:
            return json.loads(res.read().decode("utf-8"))

    def _infer_extension(self, clip_ref):
        mime = clip_ref.get("mime")
        title = clip_ref.get("title")
        MIME_MAP = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "video/mp4": ".mp4",
            "audio/mpeg": ".mp3",
            "audio/wav": ".wav",
        }
        if mime in MIME_MAP:
            return MIME_MAP[mime]
        if title and "." in title:
            return Path(title).suffix
        return ".bin"

    def _is_unresolved_clip_ref(self, clip_ref):
        if not clip_ref:
            return True
        if clip_ref.get("type") in {"video", "image", "audio", "text", "scene"}:
            return False
        return (
            "screenplay_blocks" in clip_ref
            or "accepted_types" in clip_ref
            or "preferred_type" in clip_ref
        )

    def _resolve_placeholder(self, clip_ref):
        preferred = clip_ref.get("preferred_type", "image")
        if preferred == "video":
            return str(CACHE_ROOT / "statics" / "video.mp4")
        if preferred == "audio":
            return str(CACHE_ROOT / "statics" / "audio.wav")
        return str(CACHE_ROOT / "statics" / "image.png")

    def _attach_strip_metadata(self, strip, clip, clip_ref, resolved):
        strip["asset_id"] = clip_ref.get("_id")
        strip["instance_id"] = clip.get("instanceId")
        strip["resolved"] = resolved
        strip["preferred_type"] = clip_ref.get("preferred_type")
        strip["accepted_types"] = json.dumps(clip_ref.get("accepted_types", []))
        strip["screenplay_blocks"] = json.dumps(clip_ref.get("screenplay_blocks", []))
        strip["description"] = clip_ref.get("description", "")
        if "start" in clip:
            try:
                strip["editorial_start"] = json.dumps(clip["start"])
            except Exception:
                pass
        try:
            strip["editorial_clip"] = json.dumps(clip, default=str)
        except Exception:
            pass

    def _find_cached_media(self, clip_ref):
        media_id = clip_ref.get("_id")
        if not media_id:
            return None
        media_dir = CACHE_ROOT / media_id.replace(":", "_")
        search_order = []
        preferred = clip_ref.get("preferred_type")
        if preferred:
            search_order.append(preferred)
        for media_type in clip_ref.get("accepted_types", []):
            if media_type not in search_order:
                search_order.append(media_type)
        for media_type in search_order:
            for ext in MEDIA_EXTENSIONS.get(media_type, []):
                candidate = media_dir / f"final{ext}"
                if candidate.exists():
                    return {
                        "filepath": str(candidate),
                        "media_type": media_type,
                        "clip_ref": {**clip_ref, "type": media_type},
                    }
        return None

    def _resolve_media(self, clip_ref):
        self.log.info(f"Resolving media: {clip_ref}")
        cached = self._find_cached_media(clip_ref)
        if cached:
            return {**cached, "resolved": True}

        if self._is_unresolved_clip_ref(clip_ref):
            preferred = clip_ref.get("preferred_type", "image")
            self.log.warning(
                f"Asset '{clip_ref.get('_id')}' unresolved. Using placeholder."
            )
            return {
                "filepath": self._resolve_placeholder(clip_ref),
                "media_type": preferred,
                "resolved": False,
            }

        if not self.resolving_media:
            self.resolving_media = True
            self.update_server_status("RESOLVING_MEDIA")

        media_type = clip_ref.get("type")
        media_id = clip_ref.get("_id")

        if media_type == "text":
            return {
                "filepath": clip_ref.get("text", ""),
                "media_type": "text",
                "resolved": True,
            }
        if media_type == "scene":
            return {"filepath": None, "media_type": "scene", "resolved": True}
        if media_type not in {"video", "audio", "image"}:
            self.log.error(f"Unsupported media type: {media_type}")
            return None

        media_dir = CACHE_ROOT / media_id.replace(":", "_")
        chunks_dir = media_dir / "chunks"
        ext = self._infer_extension(clip_ref)
        final_path = media_dir / f"final{ext}"
        media_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)

        if final_path.exists():
            return {
                "filepath": str(final_path),
                "media_type": media_type,
                "resolved": True,
            }

        self.log.info("Media not cached. Fetching...")
        index = 0
        while True:
            part_path = chunks_dir / f"{index:05d}.part"
            if part_path.exists():
                index += 1
                continue
            response = self._fetch_chunk_from_server(media_id, index)
            if not response.get("ok"):
                self.log.error(f"Failed to fetch chunk {index}")
                return None
            data = response["data"]
            part_path.write_bytes(base64.b64decode(data["chunk"]))
            index += 1
            if index >= data["total_chunks"]:
                break

        with open(final_path, "wb") as outfile:
            for part in sorted(chunks_dir.iterdir()):
                outfile.write(part.read_bytes())

        self.log.info(f"Media assembled: {final_path}")
        return {
            "filepath": str(final_path),
            "media_type": media_type,
            "resolved": True,
        }

    # -------------------------------------------------------------------------
    # Cut / duration
    # -------------------------------------------------------------------------

    def _apply_cut_and_duration(self, strip, clip, fps):
        cut = clip.get("cut") or {}
        source_start_ms = 0
        if cut.get("start") is not None:
            source_start_ms = self.timeline.resolve_ms(cut["start"])
        source_start_frame = self.resolve_frame(source_start_ms)
        strip.frame_offset_start = source_start_frame
        if hasattr(strip, "frame_offset_end"):
            strip.frame_offset_end = 0

    # -------------------------------------------------------------------------
    # Add clips
    # -------------------------------------------------------------------------

    def _add_video_clip(self, clip, sequence_payload, track):
        try:
            fps = sequence_payload.get("fps", 24)
            clip_ref = clip.get("clip_ref")
            media = clip["_resolved_media"]
            filepath = media["filepath"]
            resolved = media["resolved"]

            video_channel, audio_channel = self._resolve_channels(clip, track)
            start_frame = 1

            video_name = self._next_video_name()
            video = self.sequencer.sequences.new_movie(
                name=video_name,
                filepath=filepath,
                frame_start=start_frame,
                channel=video_channel,
            )
            self._attach_strip_metadata(video, clip, clip_ref, resolved)
            video["strip_role"] = "video"
            self._apply_cut_and_duration(video, clip, fps)

            audio_name = self._next_audio_name()
            audio = self.sequencer.sequences.new_sound(
                name=audio_name,
                filepath=filepath,
                frame_start=start_frame,
                channel=audio_channel,
            )
            self._attach_strip_metadata(audio, clip, clip_ref, resolved)
            audio["strip_role"] = "audio"
            video["paired_audio"] = audio.name
            audio["paired_video"] = video.name
            self._apply_cut_and_duration(audio, clip, fps)

            self.log.info(f"Added VIDEO+AUD {video.name}/{audio.name}")
            return video, audio
        except Exception as e:
            self.log.error(f"Failed adding VIDEO/AUDIO: {e}")
            return None

    def _add_audio_clip(self, clip, payload, track):
        try:
            fps = payload.get("fps", 24)
            clip_ref = clip.get("clip_ref")
            media = clip["_resolved_media"]
            filepath = media["filepath"]
            resolved = media["resolved"]

            start_frame = 1
            _, audio_channel = self._resolve_channels(clip, track)

            name = self._next_audio_name()
            audio = self.sequencer.sequences.new_sound(
                name=name,
                filepath=filepath,
                frame_start=start_frame,
                channel=audio_channel,
            )
            self._attach_strip_metadata(audio, clip, clip_ref, resolved)
            audio["strip_role"] = "audio_only"
            self._apply_cut_and_duration(audio, clip, fps)
            self.log.info(f"Created AUDIO ONLY {audio.name}")
            return audio
        except Exception as e:
            self.log.error(f"Failed to add AUDIO ONLY: {e}")
            return None

    def _add_image_clip(self, clip, sequence_payload, track):
        try:
            fps = sequence_payload.get("fps", 24)
            clip_ref = clip.get("clip_ref")
            media = clip["_resolved_media"]
            filepath = media["filepath"]
            resolved = media["resolved"]

            if not filepath:
                self.log.error("Failed to resolve image media.")
                return None

            name = self._next_image_name()
            start_frame = 1
            video_channel, _ = self._resolve_channels(clip, track)

            image_strip = self.sequencer.sequences.new_image(
                name, filepath, video_channel, start_frame
            )
            image_strip.frame_final_duration = max(1, int(fps * 5))
            self._attach_strip_metadata(image_strip, clip, clip_ref, resolved)
            self._apply_cut_and_duration(image_strip, clip, fps)
            self.log.info(f"Created IMAGE strip: {image_strip.name}")
            return image_strip
        except Exception as e:
            self.log.error(f"Failed to add IMAGE: {e}")
            return None

    def _hex_to_rgba(self, value):
        value = value.lstrip("#")
        if len(value) == 6:
            r = int(value[0:2], 16) / 255
            g = int(value[2:4], 16) / 255
            b = int(value[4:6], 16) / 255
            a = 1.0
        elif len(value) == 8:
            r = int(value[0:2], 16) / 255
            g = int(value[2:4], 16) / 255
            b = int(value[4:6], 16) / 255
            a = int(value[6:8], 16) / 255
        else:
            return (1, 1, 1, 1)
        return (r, g, b, a)

    def _apply_text_config(self, strip, clip):
        cfg = clip.get("text")
        if not cfg:
            return
        override = cfg.get("override", {})

        placement = override.get("placement")
        if placement:
            placement_map = {
                "top_left": (0.05, 0.90),
                "top_center": (0.50, 0.90),
                "top_right": (0.95, 0.90),
                "center_left": (0.05, 0.50),
                "center": (0.50, 0.50),
                "center_right": (0.95, 0.50),
                "bottom_left": (0.05, 0.10),
                "bottom_center": (0.50, 0.10),
                "bottom_right": (0.95, 0.10),
            }
            if placement in placement_map:
                x, y = placement_map[placement]
                strip.location.x = x
                strip.location.y = y

        style = override.get("style", {})

        if "font_size" in style:
            strip.font_size = style["font_size"]

        if "font" in style:
            font = bpy.data.fonts.get(style["font"])
            if font:
                strip.font = font

        if "bold" in style and hasattr(strip, "use_bold"):
            strip.use_bold = style["bold"]

        if "italic" in style and hasattr(strip, "use_italic"):
            strip.use_italic = style["italic"]

        if "shadow" in style and hasattr(strip, "use_shadow"):
            strip.use_shadow = style["shadow"]

        if "outline" in style and hasattr(strip, "use_outline"):
            strip.use_outline = style["outline"]

        if "outline_width" in style and hasattr(strip, "outline_width"):
            strip.outline_width = style["outline_width"]

        if "line_spacing" in style and hasattr(strip, "line_spacing"):
            strip.line_spacing = style["line_spacing"]

        # Blender 4.x: alignment_x / alignment_y
        if "align_x" in style:
            value = style["align_x"].upper()
            if hasattr(strip, "alignment_x"):
                strip.alignment_x = value
            elif hasattr(strip, "align_x"):
                strip.align_x = value

        if "align_y" in style:
            value = style["align_y"].upper()
            if hasattr(strip, "alignment_y"):
                strip.alignment_y = value
            elif hasattr(strip, "align_y"):
                strip.align_y = value

        if "color" in style and hasattr(strip, "color"):
            strip.color = self._hex_to_rgba(style["color"])

        if "outline_color" in style and hasattr(strip, "outline_color"):
            strip.outline_color = self._hex_to_rgba(style["outline_color"])

        if "shadow_color" in style and hasattr(strip, "shadow_color"):
            strip.shadow_color = self._hex_to_rgba(style["shadow_color"])

    # -------------------------------------------------------------------------
    # Transform effect strip
    # -------------------------------------------------------------------------

    def _ensure_transform_strip(self, base_strip, finding=False):
        for strip in self.sequencer.sequences_all:
            if strip.type == "TRANSFORM" and strip.input_1 == base_strip:
                start = int(base_strip.frame_final_start)
                end = int(base_strip.frame_final_end)
                strip.frame_start = start
                strip.frame_final_end = end
                return strip

        if finding:
            return None

        # Transform sits in the third slot of the visuals block
        transform_channel = base_strip.channel + 2
        name = self._next_transform_name()
        self.log.info(f"Creating TRANSFORM effect {name} for {base_strip.name}")

        start = int(base_strip.frame_final_start)
        end = max(start + 1, int(base_strip.frame_final_end))

        transform = self.sequencer.sequences.new_effect(
            name=name,
            type="TRANSFORM",
            frame_start=start,
            frame_end=end,
            channel=transform_channel,
            input1=base_strip,
        )

        transform["strip_role"] = "transform"
        transform["input_strip"] = base_strip.name

        if hasattr(transform, "translation_unit"):
            transform.translation_unit = "PERCENT"

        if "asset_id" in base_strip:
            transform["asset_id"] = base_strip["asset_id"]
        if "instance_id" in base_strip:
            transform["instance_id"] = base_strip["instance_id"]

        return transform

    def _get_crop_data(self, strip):
        crop_data = getattr(strip, "crop", None)
        if crop_data is None:
            self.log.error(f"Strip '{strip.name}' has no crop data.")
            return None
        return crop_data

    def _keyframe_transform_property(self, transform_strip, property_name, value, frame):
        if not hasattr(transform_strip, property_name):
            self.log.warning(
                f"TRANSFORM strip '{transform_strip.name}' "
                f"has no property '{property_name}'"
            )
            return False

        try:
            setattr(transform_strip, property_name, value)
        except Exception as e:
            self.log.error(
                f"Failed setting {transform_strip.name}.{property_name} = {value}: {e}"
            )
            return False

        try:
            inserted = transform_strip.keyframe_insert(
                data_path=property_name, frame=frame
            )
        except Exception as e:
            self.log.error(
                f"Failed keyframing {transform_strip.name}.{property_name} "
                f"@ {frame}: {e}"
            )
            return False

        self.log.info(
            f"[TRANSFORM KEYFRAME] {transform_strip.name} | "
            f"{property_name}={value} @ {frame} (inserted={inserted})"
        )
        return bool(inserted)

    def _keyframe_crop_property(self, strip, property_name, value, frame):
        crop_data = self._get_crop_data(strip)
        if crop_data is None:
            return False
        if not hasattr(crop_data, property_name):
            self.log.warning(
                f"Crop data on '{strip.name}' has no property '{property_name}'"
            )
            return False
        try:
            setattr(crop_data, property_name, value)
        except Exception as e:
            self.log.error(
                f"Failed setting {strip.name}.crop.{property_name} = {value}: {e}"
            )
            return False
        data_path = f"crop.{property_name}"
        try:
            inserted = strip.keyframe_insert(data_path=data_path, frame=frame)
        except Exception as e:
            self.log.error(
                f"Failed keyframing {strip.name}.{data_path} @ {frame}: {e}"
            )
            return False
        self.log.info(
            f"[CROP KEYFRAME] {strip.name} | {data_path}={value} @ {frame}"
        )
        return bool(inserted)

    def _keyframe_strip_property(self, strip, property_name, value, frame):
        if not hasattr(strip, property_name):
            self.log.warning(
                f"Strip '{strip.name}' has no property '{property_name}'"
            )
            return False
        try:
            setattr(strip, property_name, value)
            strip.keyframe_insert(data_path=property_name, frame=frame)
        except Exception as e:
            self.log.error(
                f"Failed applying {strip.name}.{property_name} = {value} "
                f"@ {frame}: {e}"
            )
            return False
        self.log.info(
            f"[STRIP KEYFRAME] {strip.name} | {property_name}={value} @ {frame}"
        )
        return True

    def _resolve_keyframe_time(self, t, clip, base_strip):
        if not isinstance(t, dict):
            raise ValueError(f"Transform keyframe time must be a timing object, got {t!r}")

        timing_type = t.get("type")

        if timing_type == "percentage":
            try:
                percentage = float(t["value"])
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(f"Invalid transform percentage timing: {t!r}") from e
            if percentage < 0 or percentage > 100:
                raise ValueError(
                    f"Transform percentage must be between 0 and 100, got {percentage}"
                )
            start_frame = int(base_strip.frame_final_start)
            duration_frames = int(base_strip.frame_final_duration)
            frame = start_frame + round(duration_frames * (percentage / 100.0))
            return frame

        return self.timeline.resolve_frame(t, clip_id=clip.get("_id"))

    def _resolve_relative_transform_value(self, raw_value, blender_prop, frame):
        anchor_id = raw_value.get("relative_to")
        offset = float(raw_value.get("offset", 0))
        anchor_strip = self.strips.get(anchor_id)
        if anchor_strip is None:
            self.log.warning(f"relative_to clip '{anchor_id}' not found")
            return offset

        transform = self._ensure_transform_strip(anchor_strip, finding=True)
        target = transform if transform is not None else anchor_strip

        anchor_value = getattr(target, blender_prop, 0.0)
        if target.animation_data and target.animation_data.action:
            for fcu in target.animation_data.action.fcurves:
                if fcu.data_path == blender_prop:
                    anchor_value = fcu.evaluate(frame)
                    break

        result = anchor_value + offset
        self.log.info(
            f"[RELATIVE TRANSFORM] frame {frame}: anchor={anchor_value} "
            f"+ offset={offset} -> {result}"
        )
        return result

    def _apply_transforms(self, clip, base_strip, audio_strip=None):
        transforms = clip.get("transforms")
        if not transforms:
            return

        transform_strip = None
        EFFECT_PRIORITY = {"TRANSFORM": 0, "CROP": 1, "strip": 2}

        sorted_transforms = sorted(
            transforms.items(),
            key=lambda item: EFFECT_PRIORITY.get(
                TRANSFORM_MAP.get(item[0], {}).get("effect"), 99
            ),
        )

        for transform_name, axes in sorted_transforms:
            spec = TRANSFORM_MAP.get(transform_name)
            if not spec:
                self.log.warning(f"Unknown transform '{transform_name}', skipping")
                continue

            effect = spec.get("effect")

            if effect == "TRANSFORM":
                if transform_strip is None:
                    transform_strip = self._ensure_transform_strip(base_strip)
                target = transform_strip
            elif effect == "CROP":
                target = transform_strip or base_strip
            elif transform_name in {"volume", "pan"}:
                target = audio_strip if audio_strip is not None else base_strip
            else:
                has_transform = self._ensure_transform_strip(base_strip, finding=True)
                target = has_transform if has_transform else base_strip

            if target is None:
                self.log.error(f"No target available for transform '{transform_name}'")
                continue

            for axis, keyframes in axes.items():
                if transform_name == "scale" and axis == "v":
                    blender_props = ["scale_start_x", "scale_start_y"]
                else:
                    blender_prop = spec["axes"].get(axis)
                    blender_props = [blender_prop] if blender_prop else []

                blender_props = [p for p in blender_props if p]
                if not blender_props:
                    self.log.warning(
                        f"Unsupported axis '{axis}' for transform '{transform_name}'"
                    )
                    continue

                for blender_prop in blender_props:
                    for kf in keyframes:
                        frame = self._resolve_keyframe_time(kf["t"], clip, base_strip)
                        raw_value = kf["v"]

                        if isinstance(raw_value, dict):
                            value = self._resolve_relative_transform_value(
                                raw_value, blender_prop, frame
                            )
                        else:
                            value = raw_value

                        if effect == "TRANSFORM":
                            success = self._keyframe_transform_property(
                                target, blender_prop, value, frame
                            )
                            if not success:
                                self.log.warning(
                                    f"Could not apply {transform_name}.{axis} "
                                    f"to {target.name}"
                                )
                        elif effect == "CROP":
                            success = self._keyframe_crop_property(
                                target, blender_prop, value, frame
                            )
                            if not success:
                                self.log.warning(
                                    f"Could not apply crop.{axis} to {target.name}"
                                )
                        else:
                            self._keyframe_strip_property(
                                target, blender_prop, value, frame
                            )

    # -------------------------------------------------------------------------
    # Timeline range / channels
    # -------------------------------------------------------------------------

    def calculate_timeline_range(self):
        strips = list(self.sequencer.sequences_all)
        if not strips:
            return 0, 0, 0
        start = min(s.frame_final_start for s in strips)
        end = max(s.frame_final_end for s in strips)
        return start, end, end - start

    def fit_scene_to_timeline(self):
        scene = bpy.context.scene
        start, end, _ = self.calculate_timeline_range()
        scene.frame_start = start
        scene.frame_end = max(start, end - 1)

    def _infer_track_media_type(self, track):
        """
        Best-effort guess at what a track holds, used only when its `type`
        field is missing or not one of the known TRACK_TYPE_ROLES keys.
        """
        for clip in track.get("clips", []):
            clip_ref = clip.get("clip_ref", {}) or {}
            media_type = clip_ref.get("type") or clip_ref.get("preferred_type")
            if media_type in ("video", "image"):
                return "video"
            if media_type == "audio":
                return "audio"
            if media_type == "text":
                return "text"
        return None

    def _assign_track_role(self, track):
        """
        Role is decided from the track's `type` field via TRACK_TYPE_ROLES.
        - A recognized type with a mapped role -> that role.
        - A recognized type mapped to None (e.g. "metadata") -> buoyant,
          on purpose (no fixed position in the visual/audio stack).
        - A missing or unrecognized type -> fall back to guessing from the
          track's actual clip content, same as an unrecognized track used
          to be handled.
        """
        track_type = track.get("type")

        if track_type in TRACK_TYPE_ROLES:
            return TRACK_TYPE_ROLES[track_type]

        guess = self._infer_track_media_type(track)
        if guess == "video":
            return "video-overlay"
        if guess == "audio":
            return "audio"
        if guess == "text":
            return "text"
        return None

    def _resolve_channels(self, clip, track):
        """
        One visual + one audio channel per editorial track.
        Sequential clips on the same track share those channels.
        """
        base = track["_channel_base"]
        width = track.get("_channel_width", 2)

        clip_ref = clip.get("clip_ref") or {}
        media = clip.get("_resolved_media") or {}
        media_type = (
            media.get("media_type")
            or clip_ref.get("type")
            or clip_ref.get("preferred_type")
        )

        # Audio-only track (width 1): single channel for audio
        if media_type == "audio" and width == 1:
            return base, base

        # Text track: single channel
        if media_type == "text":
            return base, base

        # Video / image: visual on base, paired audio on base+1
        return base, base + 1

    # -------------------------------------------------------------------------
    # Main build
    # -------------------------------------------------------------------------

    def build(self):
        seq = self.instruction.get("sequence", self.instruction)
        fps = seq.get("fps", 24)

        scene = bpy.context.scene
        scene.render.fps = fps
        scene.render.fps_base = 1.0

        self._video_counter = 0
        self._audio_counter = 0
        self._image_counter = 0
        self._text_counter = 0
        self._transform_counter = 0

        self.fps = fps
        self.strips = {}
        self._compiled_strips = self.strips

        self.timeline_resolver = TimelineResolver(
            sequence=seq,
            fps=fps,
            duration_provider=self.get_clip_duration_ms,
        )
        self.timeline = self.timeline_resolver

        self._clear_sequencer()
        tracks = seq["tracks"]

        # PASS 1 - register
        for index, track in enumerate(tracks):
            track_id = track.get("_id", f"track-{index}")
            track["_id"] = track_id
            self.timeline_resolver.register_track(track_id)
            for clip in track["clips"]:
                self.timeline_resolver.register_clip(clip, track_id)

        # PASS 2 - channel bases, grouped by role (from track `type`) in
        # ROLE_ORDER, with anything unclassifiable floating above all
        # known roles.
        next_base = 1

        role_buckets = {role: [] for role in ROLE_ORDER}
        buoyant = []

        for track in tracks:
            role = self._assign_track_role(track)
            if role in role_buckets:
                role_buckets[role].append(track)
            else:
                buoyant.append(track)

        for role in ROLE_ORDER:
            width = ROLE_WIDTH[role]
            for track in role_buckets[role]:
                track["_channel_base"] = next_base
                track["_channel_width"] = width
                next_base += width
                self.log.info(
                    f"[CHANNEL] {track.get('_id')} (type={track.get('type')}, "
                    f"role={role}) -> base={track['_channel_base']} width={width}"
                )

        # Buoyant tracks: explicitly unpositioned (e.g. "metadata") or
        # unrecognized/uninferrable - float to the very top, in the order
        # they appeared.
        for track in buoyant:
            guess = self._infer_track_media_type(track)
            width = 3 if guess == "video" else 1
            track["_channel_base"] = next_base
            track["_channel_width"] = width
            next_base += width
            self.log.info(
                f"[CHANNEL] {track.get('_id')} (type={track.get('type')}, "
                f"role=buoyant/{guess}) -> base={track['_channel_base']} width={width}"
            )

        # PASS 3 - materialize non-text
        for track in tracks:
            for clip in track["clips"]:
                self._materialize_clip(clip, track, fps)

        # PASS 4 - resolve editorial timing
        self.timeline_resolver.resolve_timeline()

        # PASS 5 - create TEXT strips at final frames
        self._materialize_text_clips(tracks)

        # PASS 6 - apply timing to movie/sound/image
        self._apply_resolved_timeline()

        # PASS 7 - transforms
        for track in tracks:
            for clip in track["clips"]:
                clip_id = clip.get("_id")
                strip = self.strips.get(clip_id)
                if strip is None:
                    continue
                audio_strip = None
                paired = strip.get("paired_audio")
                if paired:
                    audio_strip = self.sequencer.sequences.get(paired)
                self._apply_transforms(clip, strip, audio_strip=audio_strip)

        self.fit_scene_to_timeline()
        self.log.info("VSE build completed.")

    def _materialize_clip(self, clip, track, fps):
        strip = self.compile_clip(clip, track, fps)
        if strip is None:
            return None
        clip_id = clip.get("_id")
        if clip_id:
            self.strips[clip_id] = strip
        return strip

    def _materialize_text_clips(self, tracks):
        for track in tracks:
            for clip in track["clips"]:
                media = clip.get("_resolved_media") or {}
                if media.get("media_type") != "text":
                    continue

                clip_id = clip.get("_id")
                obj = self.timeline_resolver.clips.get(clip_id)
                if obj is None:
                    self.log.error(f"No resolved timing for text clip '{clip_id}'")
                    continue

                start_frame = max(1, self.timeline_resolver.ms_to_frames(obj.start))
                duration_frames = max(
                    1, self.timeline_resolver.ms_to_frames(obj.duration)
                )
                end_frame = start_frame + duration_frames

                self.log.info(
                    f"[TEXT TIMING] {clip_id} | "
                    f"start_ms={obj.start} duration_ms={obj.duration} | "
                    f"frames {start_frame}-{end_frame}"
                )

                strip = self._create_text_strip(
                    clip=clip,
                    track=track,
                    start_frame=start_frame,
                    end_frame=end_frame,
                )
                if strip is not None:
                    self.strips[clip_id] = strip

    def _create_text_strip(self, clip, track, start_frame, end_frame):
        clip_ref = clip.get("clip_ref", {})
        text = clip_ref.get("value") or "Text strip"

        video_channel, _ = self._resolve_channels(clip, track)

        name = self._next_text_name()
        txt = self.sequencer.sequences.new_effect(
            name=name,
            type="TEXT",
            frame_start=int(start_frame),
            frame_end=int(end_frame),
            channel=video_channel,
        )

        txt.frame_start = int(start_frame)
        try:
            txt.frame_final_end = int(end_frame)
        except Exception:
            pass
        try:
            txt.frame_final_duration = int(end_frame - start_frame)
        except Exception:
            pass

        resolved = not self._is_unresolved_clip_ref(clip_ref)
        self._attach_strip_metadata(txt, clip, clip_ref, resolved)
        txt.text = text
        self._apply_text_config(txt, clip)

        self.log.info(
            f"[CREATE TEXT] {clip.get('_id')} | '{text[:40]}' | "
            f"final {txt.frame_final_start}-{txt.frame_final_end} | "
            f"ch={txt.channel} | len={txt.frame_final_duration}"
        )
        return txt

    def _apply_resolved_timeline(self):
        for clip_id, strip in list(self.strips.items()):
            if strip.type == "TEXT":
                continue

            obj = self.timeline_resolver.clips.get(clip_id)
            if obj is None:
                continue

            start_frame = self.timeline_resolver.ms_to_frames(obj.start)
            duration_frames = max(1, self.timeline_resolver.ms_to_frames(obj.duration))
            end_frame = start_frame + duration_frames

            strip.frame_start = start_frame
            if strip.type == "TRANSFORM":
                strip.frame_final_end = end_frame
            else:
                try:
                    strip.frame_final_duration = duration_frames
                except Exception:
                    strip.frame_final_end = end_frame

            paired_name = strip.get("paired_audio")
            if paired_name:
                audio = self.sequencer.sequences.get(paired_name)
                if audio is not None:
                    audio.frame_start = start_frame
                    try:
                        audio.frame_final_duration = duration_frames
                    except Exception:
                        audio.frame_final_end = end_frame

            for s in self.sequencer.sequences_all:
                if s.type == "TRANSFORM" and getattr(s, "input_1", None) == strip:
                    s.frame_start = start_frame
                    s.frame_final_end = end_frame
                    break

            self.log.info(
                f"[APPLY TIMING] {clip_id} ({strip.type}) -> "
                f"start={start_frame} duration={duration_frames} end={end_frame}"
            )

    def compile_clip(self, clip, track, fps):
        clip_ref = clip.get("clip_ref", {})
        media = self._resolve_media(clip_ref)
        self.log.info(media)
        if not media:
            return None

        media_type = media["media_type"]
        clip["_resolved_media"] = media
        if "clip_ref" in media:
            clip["clip_ref"] = media["clip_ref"]

        if media_type == "text":
            clip["_track_ref"] = track
            return None

        strip = None
        if media_type == "video":
            result = self._add_video_clip(clip, {"fps": fps}, track)
            strip = result[0] if isinstance(result, tuple) else result
        elif media_type == "audio":
            strip = self._add_audio_clip(clip, {"fps": fps}, track)
        elif media_type == "image":
            strip = self._add_image_clip(clip, {"fps": fps}, track)

        return strip

    def get_clip_duration_ms(self, clip_id):
        strip = self.strips.get(clip_id)
        if strip is None:
            raise TimelineResolutionError(
                f"No Blender strip exists for clip '{clip_id}'."
            )
        return round(strip.frame_final_duration * 1000 / self.fps)

    def resolve_time(self, value):
        return self.timeline.resolve_ms(value)

    def resolve_frame(self, value):
        return self.timeline.resolve_frame(value)

    def _clear_sequencer(self):
        seq = self.sequencer
        strips = list(seq.sequences_all)
        if not strips:
            self.log.info("Sequencer already empty")
            return
        for strip in strips:
            try:
                seq.sequences.remove(strip)
            except Exception as e:
                self.log.warning(f"Failed removing strip {strip.name}: {e}")
        self.log.info(f"Cleared {len(strips)} sequencer strips")

    # -------------------------------------------------------------------------
    # Server helpers
    # -------------------------------------------------------------------------

    def iso_now(self):
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def _post_json(self, url, payload):
        self.log.info(f"POST {url}")
        try:
            req = urllib.request.Request(
                url=url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception as e:
            self.log.error(f"POST FAILED {url}: {e}")
            return {"ok": False, "error": str(e)}

    def update_server_status(self, status):
        if not self.generation:
            self.log.warning(f"update_server_status skipped ({status}): no generation")
            return
        if not hasattr(self, "machine_id"):
            self.machine_id = "unknown"
        generation_id = self.generation.get("_id")
        payload = {
            "_id": generation_id,
            "status": status,
            "time": self.iso_now(),
            "machine": self.machine_id,
        }
        return self._post_json(
            f"{self.server_url}/update_generation_status", payload
        )

    def upload_rendered_media(self, chunk_size=2 * 1024 * 1024):
        scene = bpy.context.scene
        title = self.instruction.get("name", "<unk>")
        description = self.instruction.get("description", "")
        user = self.instruction.get("editor", "<unk>")
        filepath = Path(scene.render.filepath)
        total_size = filepath.stat().st_size
        total_chunks = math.ceil(total_size / chunk_size)
        media_id = str(uuid.uuid4())

        with open(filepath, "rb") as f:
            for index in range(total_chunks):
                chunk_bytes = f.read(chunk_size)
                encoded = base64.b64encode(chunk_bytes).decode("utf-8")
                self._post_json(
                    f"{self.editor_url}/upload_media",
                    {
                        "media_id": media_id,
                        "chunk": encoded,
                        "index": index,
                        "size": len(chunk_bytes),
                        "total_chunks": total_chunks,
                    },
                )

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
        if not response.get("ok"):
            self.log.error("Failed to add media metadata")
            return None
        return response["data"]

    def generation_complete(self, media_id):
        return self._post_json(
            f"{self.server_url}/generation_complete",
            {"_id": self.generation.get("_id"), "editor_media": media_id},
        )

    def save_blend_snapshot(self, name):
        output_dir = Path.home() / "VSE_Instructor_Projects"
        output_dir.mkdir(parents=True, exist_ok=True)
        blend_path = output_dir / f"{name}.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
        self.log.info(f"Blend snapshot saved: {blend_path}")
        return str(blend_path)
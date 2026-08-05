import bpy
from ..core.logger import Logger
from pathlib import Path
import urllib.request
import json
import base64
from .timeline_resolver import TimelineResolver 
from pathlib import Path
from .vse_renderer import Vse_renderer
from datetime import datetime, timezone
import os
import math
import uuid


CACHE_ROOT = Path.home() / "VSEInstructorCache"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

TRANSFORM_MAP = {

    # ----------------------------
    # SPATIAL (Transform Effect)
    # ----------------------------
    "scale": {
        "effect": "TRANSFORM",
        "axes": {
            "x": "scale_start_x",
            "y": "scale_start_y",
            "v": "scale_start_x",  # uniform shortcut
        }
    },

    "translate": {
        "effect": "TRANSFORM",
        "axes": {
            "x": "translate_start_x",
            "y": "translate_start_y",
        }
    },

    "rotate": {
        "effect": "TRANSFORM",
        "axes": {
            "v": "rotation_start",
        }
    },

    # ----------------------------
    # VISUAL (native strip)
    # ----------------------------
    "opacity": {
        "effect": "strip",
        "axes": {
            "v": "blend_alpha",
        }
    },

    "crop": {
        "effect": "TRANSFORM",
        "axes": {
            "left": "crop_left",
            "right": "crop_right",
            "top": "crop_top",
            "bottom": "crop_bottom",
        },
    },


    # ----------------------------
    # AUDIO (native audio strip)
    # ----------------------------
    "volume": {
        "effect": "strip",
        "axes": {
            "v": "volume",
        }
    },

    "pan": {
        "effect": "strip",
        "axes": {
            "v": "pan",
        }
    }

}

MEDIA_EXTENSIONS = {
    "video": [".mp4", ".mov", ".mkv", ".avi", ".webm"],
    "audio": [".wav", ".mp3", ".ogg", ".flac", ".aac"],
    "image": [".png", ".jpg", ".jpeg", ".webp", ".tif"],
}

class VSEBuilder(Vse_renderer):
    server_url = "https://blender-backend.vercel.app"

    def __init__(self, instruction):
        """instruction: normalized dict from parse_instruction"""
        self.log = Logger()

        self.log.info("Initializing VSEBuilder...")
        self.log.info(f"Instruction received: {instruction}")

        self.editor_url = 'https://editor-backend-xi.vercel.app'
        self.server_url = 'https://blender-backend.vercel.app'

        self.instruction = instruction
        self.generation = None
        self.resolving_media = False
        self.sequencer = bpy.context.scene.sequence_editor

        self.timeline = None

        if self.sequencer is None:
            self.log.info("No sequence editor found. Creating one...")
            self.sequencer = bpy.context.scene.sequence_editor_create()
        else:
            self.log.info("Sequence editor found and ready.")

    def set_generation(self, generation):
        self.log.info(f"setting new generation {generation.get('_id')}")
        self.generation = generation
    
    def _fetch_chunk_from_server(self, media_id, index):
        payload = json.dumps({
            "media_id": media_id,
            "index": index
        }).encode("utf-8")

        req = urllib.request.Request(
            url=f"{self.editor_url}/read_upload",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
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

        return ".bin"  # absolute fallback


    def _is_unresolved_clip_ref(self, clip_ref):
        """
        Determines whether the clip reference still represents an
        editorial asset instead of resolved media.

        Resolved media always contains a valid media type such as:
            video
            image
            audio
            text
            scene

        Unresolved assets usually contain screenplay_blocks,
        accepted_types and preferred_type instead.
        """

        if not clip_ref:
            return True

        media_types = {
            "video",
            "image",
            "audio",
            "text",
            "scene",
        }

        media_type = clip_ref.get("type")

        if media_type in media_types:
            return False

        return (
            "screenplay_blocks" in clip_ref
            or "accepted_types" in clip_ref
            or "preferred_type" in clip_ref
        )

    def _resolve_placeholder(self, clip_ref):
        """
        Returns an appropriate placeholder for unresolved assets.
        """

        preferred = clip_ref.get("preferred_type", "image")

        if preferred == "video":
            return str(CACHE_ROOT / "statics/video.mp4")

        if preferred == "audio":
            return str(CACHE_ROOT / "statics/audio.wav")

        return str(CACHE_ROOT / "statics/image.png")

    def _attach_strip_metadata(
        self,
        strip,
        clip,
        clip_ref,
        resolved
    ):
        """
        Stores editorial metadata directly on the Blender strip.

        This allows assets to be replaced later without rebuilding
        the timeline.
        """

        strip["asset_id"] = clip_ref.get("_id")

        strip["instance_id"] = clip.get("instanceId")

        strip["resolved"] = resolved

        strip["preferred_type"] = clip_ref.get("preferred_type")

        strip["accepted_types"] = json.dumps(
            clip_ref.get("accepted_types", [])
        )

        strip["screenplay_blocks"] = json.dumps(
            clip_ref.get("screenplay_blocks", [])
        )

        strip["description"] = clip_ref.get("description", "")

    def _find_cached_media(self, clip_ref):
        """
        Looks for already-resolved media on disk.

        Returns:
            (filepath, media_type) or (None, None)
        """
        media_id = clip_ref.get("_id")
        if not media_id:
            return None, None

        media_dir = CACHE_ROOT / media_id.replace(":", "_")

        search_order = []

        preferred = clip_ref.get("preferred_type")
        if preferred:
            search_order.append(preferred)

        for t in clip_ref.get("accepted_types", []):
            if t not in search_order:
                search_order.append(t)

        for media_type in search_order:
            for ext in MEDIA_EXTENSIONS.get(media_type, []):
                candidate = media_dir / f"final{ext}"

                if candidate.exists():
                    self.log.info(
                        f"Found cached {media_type}: {candidate}"
                    )
                    return str(candidate), media_type

        return None, None

    def _resolve_media(self, clip_ref):
        self.log.info(f"Resolving media: {clip_ref}")

        cached_path, cached_type = self._find_cached_media(clip_ref)

        if cached_path:
            return {
                "filepath": cached_path,
                "media_type": cached_type,
                "resolved": True,
            }

        if self._is_unresolved_clip_ref(clip_ref):
            preferred = clip_ref.get("preferred_type", "image")

            self.log.warning(
                f"Asset '{clip_ref.get('_id')}' unresolved. "
                "Using placeholder."
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
            return {
                "filepath": None,
                "media_type": "scene",
                "resolved": True,
            }

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
                self.log.info(f"Chunk {index} already cached")
                index += 1
                continue

            response = self._fetch_chunk_from_server(media_id, index)

            if not response.get("ok"):
                self.log.error(f"Failed to fetch chunk {index}")
                return None

            data = response["data"]
            binary = base64.b64decode(data["chunk"])
            part_path.write_bytes(binary)

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

    def _apply_cut_and_duration(self, strip, clip, fps):
        """
        Applies source trimming and playback duration
        WITHOUT affecting timeline placement.
        """

        duration_ms = clip.get("duration_ms", None)
        cut = clip.get("cut")

        if duration_ms is not None:
            duration_ms = self.timeline.resolve_ms(duration_ms)

        # ----------------------------
        # SOURCE IN / OUT (milliseconds)
        # ----------------------------
        source_start_ms = 0
        source_end_ms = None

        if cut:
            source_start_ms = (
                self.timeline.resolve_ms(cut["start_ms"])
                if cut.get("start_ms") is not None
                else 0
            )
            source_end_ms = (
                self.timeline.resolve_ms(cut["end_ms"])
                if cut.get("end_ms") is not None
                else None
            )

        # ----------------------------
        # DETERMINE FINAL DURATION
        # ----------------------------
        if source_end_ms is not None:
            available_ms = max(0, source_end_ms - source_start_ms)
        else:
            available_ms = None  # unknown until media length

        if duration_ms is not None:
            duration_ms = int(duration_ms)
            final_duration_ms = (
                min(duration_ms, available_ms)
                if available_ms is not None
                else duration_ms
            )
        else:
            final_duration_ms = available_ms

        # ----------------------------
        # CONVERT TO FRAMES
        # ----------------------------
        self.log.info(
            f"""
            duration_ms={duration_ms}
            source_start_ms={source_start_ms}
            source_end_ms={source_end_ms}
            available_ms={available_ms}
            final_duration_ms={final_duration_ms}
            """
        )

        source_start_frame = self.resolve_frame(source_start_ms)

        if final_duration_ms is not None:
            final_duration_frames = max(
                1,
                self.resolve_frame(final_duration_ms)
            )
        else:
            # play remaining media
            final_duration_frames = strip.frame_duration - source_start_frame

        self.log.info(
            f"""
            source_start_frame={source_start_frame}
            final_duration_frames={final_duration_frames}
            """
        )

        # ----------------------------
        # APPLY TO STRIP (CRITICAL)
        # ----------------------------
        # Apply trim
        strip.frame_offset_start = source_start_frame

        # Counter Blender's implicit forward shift
        strip.frame_start -= source_start_frame

        strip.frame_final_duration = final_duration_frames

        self.log.info(
            f"[TRIM] {strip.name} | "
            f"source_in={source_start_ms}ms, "
            f"duration={final_duration_ms}ms"
        )

    #-----------------------------------------------------------------------
    # VIDEO + AUDIO PAIR
    # -------------------------------------------------------------------------
    def _add_video_clip(self, clip, sequence_payload, track_id=None):
        try:
            # Log one clip payload for debugging inconsistent time formats
            try:
                self.log.info(json.dumps(clip, indent=2))
            except Exception:
                self.log.info(f"clip: {clip}")

            fps = sequence_payload.get("fps", 24)
            clip_ref = clip.get("clip_ref")
            media = clip["_resolved_media"]
            filepath = media["filepath"]
            resolved = media["resolved"]
            name = clip.get("instanceId")

            layer = int(clip.get("layer", 1))

            start_frame = self.timeline.resolve_frame(
                clip["start_ms"]
            )
            asset_id = clip_ref.get("_id", "unknown")

            # ----------------------------
            # CREATE VIDEO STRIP
            # ----------------------------
            video = self.sequencer.sequences.new_movie(
                name=f"{asset_id}_VID",
                filepath=filepath,
                frame_start=start_frame,
                channel=layer
            )

            self._attach_strip_metadata(
                video,
                clip,
                clip_ref,
                resolved
            )

            self._apply_cut_and_duration(video, clip, fps)

            # ----------------------------
            # CREATE AUDIO STRIP
            # ----------------------------
            audio = self.sequencer.sequences.new_sound(
                name=f"{asset_id}_AUD",
                filepath=filepath,
                frame_start=start_frame,
                channel=layer + 1
            )

            self._attach_strip_metadata(
                audio,
                clip,
                clip_ref,
                resolved
            )

            self._apply_cut_and_duration(audio, clip, fps)

            self.log.info(
                f"Added VIDEO+AUD '{name}' @ frame {start_frame}"
            )

            return video, audio

        except Exception as e:
            self.log.error(f"Failed adding VIDEO/AUDIO: {e}")
            return None

    # -------------------------------------------------------------------------
    # AUDIO ONLY
    # -------------------------------------------------------------------------
    def _add_audio_clip(self, clip, payload, track_id):
        try:
            self.log.info(f"Adding AUDIO ONLY clip: {clip}")

            fps = payload.get("fps")

            name = clip.get("_id", clip.get("instanceId", "unknown"))
            clip_ref = clip.get("clip_ref")
            media = clip["_resolved_media"]
            filepath = media["filepath"]
            resolved = media["resolved"]

            start_frame = self.resolve_frame(self.timeline.resolve_ms(
                clip.get("start_ms", 0)
            ))
            layer = int(clip.get('layer', 1))

            audio = self.sequencer.sequences.new_sound(
                name=f"{name}_AUDONLY",
                filepath=filepath,
                frame_start=start_frame,
                channel=layer
            )

            self._attach_strip_metadata(
                audio,
                clip,
                clip_ref,
                resolved
            )

            self._apply_cut_and_duration(audio, clip, fps)

            self.log.info(f"Created AUDIO ONLY strip {audio.name} @ frame {start_frame}")
            return audio

        except Exception as e:
            self.log.error(f"Failed to add AUDIO ONLY: {e}")
            return None

    # -------------------------------------------------------------------------
    # TEXT STRIP
    # -------------------------------------------------------------------------
    def _add_text_clip(self, clip, sequence_payload, track_id):
        try:
            self.log.info(f"Adding TEXT clip: {clip}")

            name = clip.get('_id', clip.get('instanceId', 'unknown'))
            start_ms = self.timeline.resolve_ms(
                clip.get('start_ms')
            )
            duration_ms = self.timeline.resolve_ms(
                clip.get(
                    'duration_ms',
                    {
                        'type': 'milliseconds',
                        'value': 5000
                    }
                )
            )

            start_frame = self.resolve_frame(start_ms)
            end_frame = self.resolve_frame(start_ms + duration_ms)
            layer = int(clip.get('layer', 1))

            clip_ref = clip.get("clip_ref", {})
            
            text = clip_ref.get("text")

            self.log.info(f"TEXT '{text}' from {start_frame} → {end_frame}")

            txt = self.sequencer.sequences.new_effect(
                name=name,
                type="TEXT",
                frame_start=start_frame,
                frame_end=end_frame,
                channel=layer
            )

            resolved = not self._is_unresolved_clip_ref(clip_ref)

            self._attach_strip_metadata(
                txt,
                clip,
                clip_ref,
                resolved
            )

            txt.text = text or "Text strip"
            self._apply_text_config(txt, clip)

            self.log.info(f"Created TEXT strip: {txt.name}")

            return txt

        except Exception as e:
            self.log.error(f"Failed to add TEXT: {e}")
            return None

    # -----------------------------------------------------

    def _add_image_clip(self, clip, sequence_payload, track_id):
        try:
            self.log.info(f"Adding IMAGE clip: {clip}")

            fps = sequence_payload.get("fps", 24)
            clip_ref = clip.get("clip_ref")
            media = clip["_resolved_media"]
            filepath = media["filepath"]
            resolved = media["resolved"]
            if not filepath:
                self.log.error("Failed to resolve image media.")
                return None

            name = clip.get("_id", clip.get("instanceId", "unknown"))
            start_ms = self.timeline.resolve_ms(
                clip.get("start_ms")
            )
            duration_ms = self.timeline.resolve_ms(
                clip.get(
                    "duration_ms",
                    {
                        "type": "milliseconds",
                        "value": 5000
                    }
                )
            )
            start_frame = self.resolve_frame(start_ms)
            duration_frames = max(
                1,
                self.resolve_frame(duration_ms)
            )
            layer = int(clip.get("layer", 1))

            image_strip = self.sequencer.sequences.new_image(
                name=f"{name}_IMG",
                filepath=filepath,
                frame_start=start_frame,
                frame_end=start_frame + duration_frames,
                channel=layer
            )

            self._attach_strip_metadata(
                image_strip,
                clip,
                clip_ref,
                resolved
            )

            self._apply_cut_and_duration(
                image_strip,
                clip,
                fps
            )

            self.log.info(
                f"Created IMAGE strip: {image_strip.name} "
                f"from frame {image_strip.frame_start} "
                f"to {image_strip.frame_final_end}"
            )
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
        """
        Applies the Editorial text configuration onto a Blender TEXT strip.

        The renderer never invents styles.
        It only applies whatever exists inside clip["text"].
        """

        cfg = clip.get("text")
        if not cfg:
            return

        override = cfg.get("override", {})

        # ----------------------------------------------------
        # Placement
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # Style
        # ----------------------------------------------------

        style = override.get("style", {})

        if "font_size" in style:
            strip.font_size = style["font_size"]

        if "font" in style:
            font = bpy.data.fonts.get(style["font"])
            if font:
                strip.font = font

        if "bold" in style:
            strip.use_bold = style["bold"]

        if "italic" in style:
            strip.use_italic = style["italic"]

        if "shadow" in style:
            strip.use_shadow = style["shadow"]

        if "outline" in style:
            strip.use_outline = style["outline"]

        if "outline_width" in style:
            strip.outline_width = style["outline_width"]

        if "line_spacing" in style:
            strip.line_spacing = style["line_spacing"]

        if "align_x" in style:
            strip.align_x = style["align_x"].upper()

        if "align_y" in style:
            strip.align_y = style["align_y"].upper()

        # ----------------------------------------------------
        # Color
        # ----------------------------------------------------

        color = style.get("color")

        if color:
            strip.color = self._hex_to_rgba(color)

        outline_color = style.get("outline_color")
        if outline_color:
            strip.outline_color = self._hex_to_rgba(outline_color)

        shadow_color = style.get("shadow_color")
        if shadow_color:
            strip.shadow_color = self._hex_to_rgba(shadow_color)
            
    def _ensure_transform_strip(self, base_strip, finding=False):
        for s in self.sequencer.sequences:
            if (
                s.type == 'TRANSFORM'
                and s.input_1 == base_strip
            ):
                return s

        if finding: return None

        self.log.info(f"Creating TRANSFORM effect for {base_strip.name}")

        return self.sequencer.sequences.new_effect(
            name=f"{base_strip.name}_XFORM",
            type='TRANSFORM',
            frame_start=int(base_strip.frame_start),
            frame_end=int(base_strip.frame_final_end),
            channel=base_strip.channel + 1,
            input1=base_strip
        )


    def _resolve_keyframe_time(
        self,
        t,
        clip
    ):

        if isinstance(t,str) and t.endswith("%"):

            pct=float(t[:-1])/100

            return (
                clip.frame_start +
                int(
                    clip.frame_final_duration *
                    pct
                )
            )


        return self.timeline.resolve_frame(t)

    def _apply_transforms(self, clip, base_strip):
        transforms = clip.get("transforms")
        if not transforms:
            return

        fps = bpy.context.scene.render.fps
        start = base_strip.frame_start
        duration = base_strip.frame_final_duration

        transform_strip = None
        
        EFFECT_PRIORITY = {
            "TRANSFORM": 0,
            "strip": 1,
        }

        sorted_transforms = sorted(
            transforms.items(),
            key=lambda item: EFFECT_PRIORITY.get(
                TRANSFORM_MAP.get(item[0], {}).get("effect"),
                99
            )
        )
        for transform_name, axes in sorted_transforms:
            spec = TRANSFORM_MAP.get(transform_name)

            if not spec:
                self.log.warning(f"Unknown transform '{transform_name}', skipping")
                continue

            # Decide target strip
            if spec.get("effect") == "TRANSFORM":
                if transform_strip is None:
                    transform_strip = self._ensure_transform_strip(base_strip)
                target = transform_strip
            else:
                has_tx = self._ensure_transform_strip(base_strip, finding=True)
                if has_tx:
                    target = has_tx
                else:
                    target = base_strip

            for axis, keyframes in axes.items():
                blender_prop = spec["axes"].get(axis)

                if not blender_prop:
                    self.log.warning(
                        f"Unsupported axis '{axis}' for transform '{transform_name}'"
                    )
                    continue

                if not hasattr(target, blender_prop):
                    self.log.warning(
                        f"Strip '{target.name}' has no property '{blender_prop}'"
                    )
                    continue

                for kf in keyframes:
                    frame = self._resolve_keyframe_time(
                        kf["t"],
                        base_strip
                    )
                    value = kf["v"]

                    setattr(target, blender_prop, value)
                    target.keyframe_insert(
                        data_path=blender_prop,
                        frame=frame
                    )

                    self.log.info(
                        f"[TRANSFORM] {clip.get('instanceId')} | "
                        f"{transform_name}.{axis} → "
                        f"{blender_prop}={value} @ {frame}"
                    )


    # -------------------------------------------------------------------------
    # MAIN BUILD
    # -------------------------------------------------------------------------
    def build(self):

        seq = self.instruction.get("sequence", self.instruction)

        fps = seq.get("fps",24)

        self.timeline = TimelineResolver(
            seq,
            fps
        )
        self._clear_sequencer()

        tracks = seq["tracks"]

        #
        # PASS 1
        # Editorial graph
        #

        for index, track in enumerate(tracks):

            track_id = track.get(
                "_id",
                f"track-{index}"
            )

            track["_id"] = track_id

            self.timeline.register_track(
                track_id
            )

            for clip in track["clips"]:

                self.timeline.register_clip(
                    clip,
                    track_id
                )

        #
        # PASS 2
        # Blender compilation
        #

        for track in tracks:

            for clip in track["clips"]:

                self.compile_clip(
                    clip,
                    track,
                    fps
                )


    def compile_clip(
        self,
        clip,
        track,
        fps
    ):

        clip_ref = clip.get(
            "clip_ref",
            {}
        )

        media = self._resolve_media(clip_ref)

        if not media:
            return None

        media_type = media["media_type"]

        clip["_resolved_media"] = media

        strip = None

        if media_type == "video":
            result = self._add_video_clip(
                clip,
                {
                    "fps": fps
                },
                track["_id"]
            )

            if isinstance(result, tuple) and len(result) == 2:
                video, _audio = result
                strip = video
            else:
                strip = result

        elif media_type == "audio":
            strip = self._add_audio_clip(
                clip,
                {"fps": fps},
                track["_id"]
            )

        elif media_type == "image":
            strip = self._add_image_clip(
                clip,
                track,
                track["_id"]
            )

        elif media_type == "text":
            strip = self._add_text_clip(
                clip,
                {"fps": fps},
                track["_id"]
            )

        if strip:
            self._apply_transforms(
                clip,
                strip
            )

        return strip
   
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
            seq.sequences.remove(strip)

        self.log.info(f"Cleared {len(strips)} sequencer strips")

    def iso_now(self):
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    
    def _post_json(self, url, payload):
        self.log.info(f"Preparing to POST to {url} with payload: {payload}")
        try:
            self.log.info(f"POST {url}")
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
            self.log.warning("machine_id missing, defaulting")
            self.machine_id = "unknown"

        self.log.info(
            f"Updating server status to '{status}' "
            f"for generation {self.generation.get('_id')}"
        )

        self.log.info(f"Generation ID: {self.generation.get('_id')}")

        self.log.info(f"Calling POST to {self.server_url}/update_generation_status")   

        self.log.info(f"Machine ID: {self.machine_id}")

        self.log.info(f"Status: {status}")

        self.log.info(f"Timestamp: {self.iso_now()}") 
        
        payload = {
            "_id": self.generation.get("_id"),
            "status": status,
            "time": self.iso_now(),
            "machine": self.machine_id
        }
        url = f"{self.server_url}/update_generation_status"
        
        self.log.info(f"Preparing to POST to {url} with payload: {payload}")
        try:
            self.log.info(f"POST {url}")
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

    def upload_rendered_media(
        self,
        chunk_size=2 * 1024 * 1024  # 2MB
    ):
        scene = bpy.context.scene
        
        title = self.instruction.get("name", "<unk>")
        description = self.instruction.get("description", "")
        user = self.instruction.get("editor", "<unk>")
        mime = 'video/mp4'
        media_type = 'video'

        filepath = Path(scene.render.filepath)
        total_size = filepath.stat().st_size
        total_chunks = math.ceil(total_size / chunk_size)

        media_id = str(uuid.uuid4())

        with open(filepath, "rb") as f:
            for index in range(total_chunks):
                chunk_bytes = f.read(chunk_size)
                encoded = base64.b64encode(chunk_bytes).decode("utf-8")

                payload = {
                    "media_id": media_id,
                    "chunk": encoded,
                    "index": index,
                    "size": len(chunk_bytes),
                    "total_chunks": total_chunks,
                }

                self._post_json(
                    f"{self.editor_url}/upload_media",
                    payload
                )

        payload = {
            "_id": media_id,
            "title": title,
            "description": description,
            "user": user,
            "mime": mime,
            "type": media_type,
            "total_size": total_size,
        }

        response = self._post_json(
            f"{self.editor_url}/add_media",
            payload
        )

        if not response.get("ok"):
            return self.log.error("Failed to add media metadata")

        media = response["data"]

        return media



    def generation_complete(self, media_id):
        payload = {
            "_id": self.generation.get('_id'),
            "editor_media": media_id,
        }

        self._post_json(
            f"{VSEBuilder.server_url}/generation_complete",
            payload
        )

    def save_blend_snapshot(name):
        output_dir = Path.home() / "VSE_Instructor_Projects"
        output_dir.mkdir(parents=True, exist_ok=True)

        blend_path = output_dir / f"{name}.blend"

        bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
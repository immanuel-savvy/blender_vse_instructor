import bpy
from ..core.logger import Logger
from pathlib import Path
import urllib.request
import json
import base64
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


    def _resolve_media(self, clip_ref):
        self.log.info(f"Resolving media: {clip_ref}")

        if(not self.resolving_media): 
            self.resolving_media = True
            self.update_server_status('RESOLVING_MEDIA')
            
        media_type = clip_ref.get("type")
        media_id = clip_ref.get("_id")

        if media_type == "text":
            return clip_ref.get("text", "Text strip")
        
        if (media_type == 'scene'):
            return None

        if media_type not in {"video", "audio", "image"}:
            self.log.error(f"Unsupported media type: {media_type}")
            return None

        media_dir = CACHE_ROOT / media_id.replace(":", "_")
        chunks_dir = media_dir / "chunks"
        ext = self._infer_extension(clip_ref)
        final_path = media_dir / f"final{ext}"

        media_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir.mkdir(parents=True, exist_ok=True)

        # ----------------------------
        # CACHE HIT
        # ----------------------------
        if final_path.exists():
            self.log.info(f"Using cached media: {final_path}")
            return str(final_path)

        self.log.info("Media not cached. Fetching from server...")

        # ----------------------------
        # DOWNLOAD CHUNKS
        # ----------------------------
        index = 0
        total_chunks = None

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
            base64_chunk = data["chunk"]
            total_chunks = data["total_chunks"]

            binary = base64.b64decode(base64_chunk)
            part_path.write_bytes(binary)

            self.log.info(f"Downloaded chunk {index+1}/{total_chunks}")

            index += 1
            if index >= total_chunks:
                break

        # ----------------------------
        # ASSEMBLE FINAL BINARY (ONCE)
        # ----------------------------
        self.log.info("Assembling final binary...")

        with open(final_path, "wb") as outfile:
            for part in sorted(chunks_dir.iterdir()):
                outfile.write(part.read_bytes())

        self.log.info(f"Media assembled: {final_path}")

        return str(final_path)

 
    
    def _apply_cut_and_duration(self, strip, clip, fps):
        """
        Applies source trimming and playback duration
        WITHOUT affecting timeline placement.
        """

        duration_ms = clip.get("duration_ms", None)
        cut = clip.get("cut")

        # ----------------------------
        # SOURCE IN / OUT (milliseconds)
        # ----------------------------
        source_start_ms = 0
        source_end_ms = None

        if cut:
            source_start_ms = int(cut.get("start_ms", 0))
            source_end_ms = cut.get("end_ms")
            if source_end_ms is not None:
                source_end_ms = int(source_end_ms)

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
        source_start_frame = self._ms_to_frames(source_start_ms, fps)

        if final_duration_ms is not None:
            final_duration_frames = max(
                1,
                self._ms_to_frames(final_duration_ms, fps)
            )
        else:
            # play remaining media
            final_duration_frames = strip.frame_duration - source_start_frame

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

            
    #-------------------------------------------------------------------
    def _ms_to_frames(self, ms, fps=24):
        frames = int((ms / 1000.0) * fps)
        self.log.info(f"Converting ms → frames: {ms}ms @ {fps}fps = {frames}")
        return frames

    #-----------------------------------------------------------------------
    # VIDEO + AUDIO PAIR
    # -------------------------------------------------------------------------
    def _add_video_clip(self, clip, sequence_payload):
        try:
            fps = sequence_payload.get("fps", 24)
            clip_ref = clip.get("clip_ref")
            filepath = self._resolve_media(clip_ref)
            name = clip.get("instanceId")

            start_ms = int(clip.get("start_ms", 0))
            layer = int(clip.get("layer", 1))

            start_frame = self._ms_to_frames(start_ms, fps)

            # ----------------------------
            # CREATE VIDEO STRIP
            # ----------------------------
            video = self.sequencer.sequences.new_movie(
                name=f"{name}_VID",
                filepath=filepath,
                frame_start=start_frame,
                channel=layer
            )

            self._apply_cut_and_duration(video, clip, fps)

            # ----------------------------
            # CREATE AUDIO STRIP
            # ----------------------------
            audio = self.sequencer.sequences.new_sound(
                name=f"{name}_AUD",
                filepath=filepath,
                frame_start=start_frame,
                channel=layer + 1
            )
            # audio.use_sound_length = False

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
    def _add_audio_clip(self, clip, payload):
        try:
            self.log.info(f"Adding AUDIO ONLY clip: {clip}")

            fps = payload.get("fps")

            name = clip.get("instanceId")
            clip_ref = clip.get("clip_ref")
            filepath = self._resolve_media(clip_ref)

            start_frame = self._ms_to_frames(int(clip.get('start_ms', 0)), fps)
            layer = int(clip.get('layer', 1))

            audio = self.sequencer.sequences.new_sound(
                name=f"{name}_AUDONLY",
                filepath=filepath,
                frame_start=start_frame,
                channel=layer
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
    def _add_text_clip(self, clip, sequence_payload):
        try:
            self.log.info(f"Adding TEXT clip: {clip}")

            name = clip.get('instanceId')
            start_ms = clip.get('start_ms', 0)
            duration_ms = clip.get('duration_ms', 5000)

            start_frame = self._ms_to_frames(start_ms)
            end_frame = self._ms_to_frames(start_ms + duration_ms)
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

            txt.text = text
            self.log.info(f"Created TEXT strip: {txt.name}")

            return txt

        except Exception as e:
            self.log.error(f"Failed to add TEXT: {e}")
            return None

    # -----------------------------------------------------

    def _add_image_clip(self, clip, sequence_payload):
        try:
            self.log.info(f"Adding IMAGE clip: {clip}")

            fps = sequence_payload.get("fps", 24)
            clip_ref = clip.get("clip_ref")
            filepath = self._resolve_media(clip_ref)
            if not filepath:
                self.log.error("Failed to resolve image media.")
                return None

            name = clip.get("instanceId")
            start_ms = int(clip.get("start_ms", 0))
            duration_ms = int(clip.get("duration_ms", 5000) ) # default 5s
            start_frame = self._ms_to_frames(start_ms, fps)
            duration_frames = self._ms_to_frames(duration_ms, fps)
            layer = int(clip.get("layer", 1))

            image_strip = self.sequencer.sequences.new_image(
                name=f"{name}_IMG",
                filepath=filepath,
                frame_start=start_frame,
                channel=layer
            )
            image_strip.frame_final_duration = duration_frames

            self.log.info(f"Created IMAGE strip: {image_strip.name} from frame {start_frame} to {start_frame+duration_frames}")
            return image_strip

        except Exception as e:
            self.log.error(f"Failed to add IMAGE: {e}")
            return None


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


    def _resolve_keyframe_time(self, t, start_frame, duration_frames, fps):
        if isinstance(t, str) and t.endswith("%"):
            pct = float(t[:-1]) / 100.0
            return start_frame + int(duration_frames * pct)

        # assume ms
        return start_frame + self._ms_to_frames(t, fps)

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
                        kf["t"], start, duration, fps
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
        self.log.info("===== BEGIN VSE BUILD =====")

        self._clear_sequencer()
        
        seq = self.instruction.get("sequence", self.instruction)
        fps = seq.get("fps", 24)
        tracks = seq.get("tracks", [])

        self.log.info(f"Sequence FPS: {fps}")
        self.log.info(f"Tracks found: {len(tracks)}")

        if not tracks:
            self.log.error("No tracks found. Nothing to build.")
            return

        for track_index, track in enumerate(tracks):
            self.log.info(f"=== Processing Track #{track_index} ===")
            self.log.info(f"Track data: {track}")

            track_payload = {"track": track, "fps": fps}

            for clip_index, clip in enumerate(track.get("clips", [])):
                self.log.info(f"-- Clip #{clip_index}: {clip}")

                clip_ref = clip.get("clip_ref", {})
                mediatype = clip_ref.get("type")
                self.log.info(f"Mediatype = {mediatype}")

                strip = None
                video_strip = None
                audio_strip = None

                if mediatype == "video":
                    result = self._add_video_clip(clip, track_payload)

                    if result:
                        video_strip, audio_strip = result

                elif mediatype == "audio":
                    audio_strip = self._add_audio_clip(clip, track_payload)

                elif mediatype == "text":
                    strip = self._add_text_clip(clip, track_payload)

                elif mediatype == "image":
                    strip = self._add_image_clip(clip, track_payload)

                else:
                    self.log.error(f"Unsupported mediatype: {mediatype}")

                # Apply transform strips
                if video_strip:
                    self._apply_transforms(clip, video_strip)

                if audio_strip:
                    self._apply_transforms(clip, audio_strip)

                if strip:
                    self._apply_transforms(clip, strip)


        self.resolving_media = False
        self.setup_timeline_from_output(self.instruction.get('output', {}))

        self.log.info("===== VSE BUILD COMPLETE =====")

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
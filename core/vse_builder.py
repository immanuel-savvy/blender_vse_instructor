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

class VSEBuilder(Vse_renderer):
    server_url = "https://blender-backend.vercel.app"

    def __init__(self, instruction):
        """instruction: normalized dict from parse_instruction"""
        self.log = Logger()

        self.log.info("Initializing VSEBuilder...")
        self.log.info(f"Instruction received: {instruction}")

        self.editor_url = 'https://editor-backend-xi.vercel.app'

        self.instruction = instruction
        self.generation = None
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

        if(not self.resolving_media): self.update_server_status('RESOLVING_MEDIA')
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

 
    def _apply_cut_and_duration(self, strip, cut, duration_ms, fps):
        self.log.info(f"Applying cut/duration to strip {strip.name}")
        self.log.info(f"Initial strip frame_duration: {strip.frame_duration}")

        if cut:
            self.log.info(f"Cut data: {cut}")

            cut_start = self._ms_to_frames(cut.get("start", 0), fps)
            cut_end   = self._ms_to_frames(cut.get("end", 0), fps)

            self.log.info(f"Computed cut_start(frames): {cut_start}")
            self.log.info(f"Computed cut_end(frames): {cut_end}")

            strip.frame_offset_start = cut_start
            strip.frame_offset_end   = max(0, strip.frame_duration - cut_end)

            self.log.info(f"Applied strip.frame_offset_start = {strip.frame_offset_start}")
            self.log.info(f"Applied strip.frame_offset_end   = {strip.frame_offset_end}")

        if duration_ms:
            duration_frames = self._ms_to_frames(duration_ms, fps)
            strip.frame_final_duration = duration_frames

            self.log.info(f"Duration override: {duration_ms}ms = {duration_frames} frames")
            self.log.info(f"Applied strip.frame_final_duration = {strip.frame_final_duration}")
            
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
            self.log.info(f"Adding VIDEO clip: {clip}")

            fps = sequence_payload.get('fps')
            clip_ref = clip.get('clip_ref')
            filepath = self._resolve_media(clip_ref)
            cut = clip_ref.get('cut')
            name = clip.get('instanceId')

            start_ms = clip.get('start_ms', 0)
            start_frame = self._ms_to_frames(start_ms, fps)
            layer = clip.get('layer', 1)
            duration_ms = clip.get('duration_ms', 0)

            self.log.info(f"Resolved file path = {filepath}")
            self.log.info(f"Start frame = {start_frame}, Layer = {layer}, FPS = {fps}")
            self.log.info(f"Duration override = {duration_ms}ms")

            cut_start_frame = self._ms_to_frames(cut.get('start', 0))
            calculated_start_frame = start_frame - cut_start_frame

            # Create VIDEO strip
            video = self.sequencer.sequences.new_movie(
                name=f"{name}_VID",
                filepath=filepath,
                frame_start=calculated_start_frame,
                channel=layer
            )
            self.log.info(f"Created VIDEO strip: {video.name}")

            # Trim logic
            self._apply_cut_and_duration(video, cut, duration_ms, fps)

            # video.frame_start = start_frame
            # Create AUDIO strip
            audio = self.sequencer.sequences.new_sound(
                name=f"{name}_AUD",
                filepath=filepath,
                frame_start=calculated_start_frame,
                channel=layer + 1
            )
            self.log.info(f"Created AUDIO strip: {audio.name}")

            self._apply_cut_and_duration(audio, cut, duration_ms, fps)
            # audio.frame_start = start_frame
            # strip.frame_start = start_frame

            self.log.info(f"Added VIDEO + AUDIO for {filepath} at {start_frame}")
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

            start_frame = self._ms_to_frames(clip.get('start_ms', 0), fps)
            layer = clip.get('layer', 1)

            audio = self.sequencer.sequences.new_sound(
                name=f"{name}_AUDONLY",
                filepath=filepath,
                frame_start=start_frame,
                channel=layer
            )

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
            layer = clip.get('layer', 1)

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
            start_ms = clip.get("start_ms", 0)
            duration_ms = clip.get("duration_ms", 5000)  # default 5s
            start_frame = self._ms_to_frames(start_ms, fps)
            duration_frames = self._ms_to_frames(duration_ms, fps)
            layer = clip.get("layer", 1)

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

    # -------------------------------------------------------------------------
    # MAIN BUILD
    # -------------------------------------------------------------------------
    def build(self):
        self.log.info("===== BEGIN VSE BUILD =====")

        seq = self.instruction.get("sequence", self.instruction)
        fps = seq.get("fps", 24)
        tracks = seq.get("tracks", [])

        self.log.info(f"Sequence FPS: {fps}")
        self.log.info(f"Tracks found: {len(tracks)}")

        if not tracks:
            self.log.error("No tracks found. Nothing to build.")
            return

        self.resolving_media = True
        for track_index, track in enumerate(tracks):
            self.log.info(f"=== Processing Track #{track_index} ===")
            self.log.info(f"Track data: {track}")

            track_payload = {"track": track, "fps": fps}

            for clip_index, clip in enumerate(track.get("clips", [])):
                self.log.info(f"-- Clip #{clip_index}: {clip}")

                clip_ref = clip.get("clip_ref", {})
                mediatype = clip_ref.get("type")
                self.log.info(f"Mediatype = {mediatype}")

                if mediatype == "video":
                    self._add_video_clip(clip, track_payload)

                elif mediatype == "audio":
                    self._add_audio_clip(clip, track_payload)

                elif mediatype == "text":
                    self._add_text_clip(clip, track_payload)

                elif mediatype == 'image':
                    self._add_image_clip(clip, track_payload)

                else:
                    self.log.error(f"Unsupported mediatype: {mediatype}")

        self.resolving_media = False
        self.setup_timeline_from_output(self.instruction.get('output', {}))

        self.log.info("===== VSE BUILD COMPLETE =====")


    def iso_now():
        return (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    
    def _post_json(self, url, payload):
        self.log.info(f"Sending request {url} with payload::{payload}")
        req = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))

    def update_server_status(self, status):
        if (self.generation == None):
            return

        self._post_json(f"{VSEBuilder.server_url}/update_generation_status", {
            "_id": self.generation.get("_id"),
            "status": status,
            "time": self.iso_now()
        })

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
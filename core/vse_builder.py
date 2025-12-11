import bpy
from ..core.logger import Logger
from pathlib import Path
import os
import urllib.request

class VSEBuilder:
    def __init__(self, instruction):
        """instruction: normalized dict from parse_instruction"""
        self.log = Logger.get()

        self.log.info("Initializing VSEBuilder...")
        self.log.info(f"Instruction received: {instruction}")

        self.instruction = instruction
        self.sequencer = bpy.context.scene.sequence_editor

        if self.sequencer is None:
            self.log.info("No sequence editor found. Creating one...")
            self.sequencer = bpy.context.scene.sequence_editor_create()
        else:
            self.log.info("Sequence editor found and ready.")

    # -------------------------------------------------------------------------
    # MEDIA RESOLVER
    # -------------------------------------------------------------------------
    def _resolve_media(self, clip_ref):
        self.log.info(f"Resolving media for clip_ref: {clip_ref}")

        mediatype = clip_ref.get("mediatype")
        mediaid = clip_ref.get("mediaid")

        if mediatype in ["video", "audio"]:
            self.log.info(f"Media type is {mediatype}, id = {mediaid}")

            path = Path(mediaid)
            if path.exists():
                self.log.info(f"Local file exists: {path}")
                return str(path)

            elif mediaid.startswith("http"):
                tmp_dir = Path(bpy.app.tempdir)
                filename = Path(mediaid).name
                local_path = tmp_dir / filename

                self.log.info(f"Media is remote. Will download to: {local_path}")

                if not local_path.exists():
                    self.log.info(f"Downloading {mediaid} ...")
                    urllib.request.urlretrieve(mediaid, local_path)
                    self.log.info("Download complete.")

                return str(local_path)

            else:
                self.log.error(f"Media not found at path or url: {mediaid}")
                return None

        elif mediatype == "text":
            self.log.info(f"Media is TEXT, content: {clip_ref.get('text')}")
            return clip_ref.get("text", "Text strip")

        else:
            self.log.error(f"Unsupported media type: {mediatype}")
            return None

    # -------------------------------------------------------------------------
    # CUT & DURATION HANDLING
    # -------------------------------------------------------------------------
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
            

    # -------------------------------------------------------------------------
    def _ms_to_frames(self, ms, fps=24):
        frames = int((ms / 1000.0) * fps)
        self.log.info(f"Converting ms → frames: {ms}ms @ {fps}fps = {frames}")
        return frames

    # -------------------------------------------------------------------------
    # VIDEO + AUDIO PAIR
    # -------------------------------------------------------------------------
    def _add_video_clip(self, clip, sequence_payload):
        try:
            self.log.info(f"Adding VIDEO clip: {clip}")

            fps = sequence_payload.get('fps')
            clip_ref = clip.get('clipRef')
            filepath = clip_ref.get('mediaid')
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
            clip_ref = clip.get("clipRef")
            filepath = clip_ref.get("mediaid")

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

            text = clip.get("clipRef", {}).get("text")

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

    # -------------------------------------------------------------------------
    # MAIN BUILD
    # -------------------------------------------------------------------------
    def build(self):
        self.log.info("===== BEGIN VSE BUILD =====")

        seq = self.instruction.get("sequence", {})
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

                mediatype = clip.get("clipRef", {}).get("mediatype")
                self.log.info(f"Mediatype = {mediatype}")

                if mediatype == "video":
                    self._add_video_clip(clip, track_payload)

                elif mediatype == "audio":
                    self._add_audio_clip(clip, track_payload)

                elif mediatype == "text":
                    self._add_text_clip(clip, track_payload)

                else:
                    self.log.error(f"Unsupported mediatype: {mediatype}")

        self.log.info("===== VSE BUILD COMPLETE =====")

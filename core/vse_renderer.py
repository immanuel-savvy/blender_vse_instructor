import bpy


class Vse_renderer:

    # =========================================================================
    # OUTPUT PRESET -> BLENDER ENUM MAPPINGS
    # =========================================================================

    CONTAINER_FORMAT_MAP = {
        "mp4": "MPEG4",
        "mov": "QUICKTIME",
        "mkv": "MKV",
        "avi": "AVI",
        "webm": "WEBM",
        "ogg": "OGG",
        "dv": "DV",
        "flv": "FLASH",
    }

    VIDEO_CODEC_MAP = {
        "h264": "H264",
        "h265": "H265",
        "hevc": "H265",
        "mpeg4": "MPEG4",
        "mpeg2": "MPEG2",
        "mpeg1": "MPEG1",
        "theora": "THEORA",
        "ffv1": "FFV1",
        "dnxhd": "DNXHD",
        "png": "PNG",
        "qtrle": "QTRLE",
        # Blender doesn't expose a standalone VP9 codec enum outside the
        # WEBM container path; closest available mapping.
        "vp9": "WEBM",
    }

    AUDIO_CODEC_MAP = {
        "aac": "AAC",
        "mp3": "MP3",
        "ac3": "AC3",
        "flac": "FLAC",
        "pcm": "PCM",
        "opus": "OPUS",
        "vorbis": "VORBIS",
        "none": "NONE",
    }

    AUDIO_CHANNEL_MAP = {
        1: "MONO",
        2: "STEREO",
        4: "SURROUND4",
        6: "SURROUND51",
        8: "SURROUND71",
    }

    # Preset fields that don't have a direct Blender FFmpeg API knob.
    # H264 exports default to yuv420p / bt709 / limited range anyway,
    # which is what the "YouTube Long" preset asks for, so this is a
    # no-op today but keeps future presets from failing silently.
    _UNSUPPORTED_VIDEO_FIELDS = (
        "pixel_format",
        "color_space",
        "color_range",
        "profile",
        "level",
    )

    # =========================================================================
    # BITRATE PARSING
    # =========================================================================

    @staticmethod
    def _parse_bitrate_kbps(value, default=0):
        """
        Normalize a bitrate value into an int in kbps, which is the unit
        Blender's FFmpegSettings (video_bitrate / maxrate / minrate /
        audio_bitrate) expects.

        Accepts:
            "12M"   -> 12000
            "16M"   -> 16000
            "192k"  -> 192
            "6000"  -> 6000
            6000    -> 6000
            None    -> `default`
        """
        if value is None:
            return default

        if isinstance(value, (int, float)):
            return int(value)

        text = str(value).strip().upper()

        if not text:
            return default

        try:
            if text.endswith("M"):
                return int(round(float(text[:-1]) * 1000))

            if text.endswith("K"):
                return int(round(float(text[:-1])))

            return int(round(float(text)))

        except ValueError:
            return default

    # =========================================================================
    # TIMELINE + OUTPUT SETUP
    # =========================================================================

    def setup_timeline_from_output(self, output_spec, default_output="//render/output.mp4"):
        """
        Set timeline, resolution, FPS, and full FFmpeg output encoding
        (video + audio) from an output_preset dict shaped like:

            {
                "format": "mp4",
                "video": {
                    "codec": "h264",
                    "width": 1920,
                    "height": 1080,
                    "fps": 24,
                    "rate_control": "vbr",
                    "bitrate": "12M",
                    "max_bitrate": "16M",
                    "pixel_format": "yuv420p",
                    "color_space": "bt709",
                    "color_range": "limited",
                    "gop": 48,
                    "keyframe_interval": 2,
                    "profile": "high",
                    "level": "4.2",
                },
                "audio": {
                    "codec": "aac",
                    "sample_rate": 48000,
                    "channels": 2,
                    "bitrate": "192k",
                },
            }
        """
        scene = bpy.context.scene
        ffmpeg = scene.render.ffmpeg

        video_spec = output_spec.get("video", {}) or {}
        audio_spec = output_spec.get("audio", {}) or {}

        # ----------------------------
        # FPS
        # ----------------------------
        fps = int(video_spec.get("fps", 24))
        scene.render.fps = fps
        scene.render.fps_base = 1.0

        # ----------------------------
        # Resolution
        # ----------------------------
        scene.render.resolution_x = int(video_spec.get("width", 1920))
        scene.render.resolution_y = int(video_spec.get("height", 1080))
        scene.render.resolution_percentage = 100

        # ----------------------------
        # Container / video codec
        # ----------------------------
        container = str(output_spec.get("format", "mp4")).lower()
        video_codec = str(video_spec.get("codec", "h264")).lower()

        scene.render.filepath = default_output
        scene.render.image_settings.file_format = 'FFMPEG'

        ffmpeg.format = self.CONTAINER_FORMAT_MAP.get(container, "MPEG4")
        ffmpeg.codec = self.VIDEO_CODEC_MAP.get(video_codec, "H264")

        # ----------------------------
        # Video bitrate / rate control
        # ----------------------------
        ffmpeg.video_bitrate = self._parse_bitrate_kbps(
            video_spec.get("bitrate"),
            default=ffmpeg.video_bitrate,
        )

        max_bitrate = video_spec.get("max_bitrate")

        if max_bitrate is not None:

            ffmpeg.maxrate = self._parse_bitrate_kbps(
                max_bitrate,
                default=ffmpeg.video_bitrate,
            )

            # Blender doesn't expose a literal VBR/CBR switch, only a
            # bitrate + minrate/maxrate trio. Pinning minrate to the
            # target bitrate approximates CBR; leaving it at 0 (the
            # default) gives ffmpeg room to vary the rate, i.e. VBR.
            if str(video_spec.get("rate_control", "")).lower() == "cbr":
                ffmpeg.minrate = ffmpeg.video_bitrate
            else:
                ffmpeg.minrate = 0

        # ----------------------------
        # GOP size / keyframe interval
        # ----------------------------
        if video_spec.get("gop") is not None:
            ffmpeg.gopsize = int(video_spec["gop"])

        elif video_spec.get("keyframe_interval") is not None:
            # keyframe_interval is given in seconds -> convert to frames.
            ffmpeg.gopsize = int(round(float(video_spec["keyframe_interval"]) * fps))

        # ----------------------------
        # Fields with no direct Blender FFmpeg API equivalent
        # ----------------------------
        for field in self._UNSUPPORTED_VIDEO_FIELDS:

            if video_spec.get(field) is not None:

                self.log.info(
                    f"[OUTPUT PRESET] '{field}'={video_spec[field]!r} has "
                    f"no direct Blender FFmpeg API equivalent; relying on "
                    f"the codec's default behavior."
                )

        # ----------------------------
        # Audio
        # ----------------------------
        audio_codec = str(audio_spec.get("codec", "aac")).lower()

        ffmpeg.audio_codec = self.AUDIO_CODEC_MAP.get(audio_codec, "AAC")

        ffmpeg.audio_bitrate = self._parse_bitrate_kbps(
            audio_spec.get("bitrate"),
            default=ffmpeg.audio_bitrate,
        )

        if audio_spec.get("sample_rate") is not None:
            ffmpeg.audio_mixrate = int(audio_spec["sample_rate"])

        channels = int(audio_spec.get("channels", 2))

        ffmpeg.audio_channels = self.AUDIO_CHANNEL_MAP.get(channels, "STEREO")

        # ----------------------------
        # Timeline Range (CORRECT)
        # ----------------------------
        seq = scene.sequence_editor
        strips = seq.sequences_all if seq else []

        if strips:

            final_starts = [s.frame_final_start for s in strips]
            final_ends = [s.frame_final_end for s in strips]

            scene.frame_start = int(min(final_starts))
            scene.frame_end = int(max(final_ends))

        else:

            scene.frame_start = 1
            scene.frame_end = fps * 5  # fallback

        self.log.info(
            f"Timeline set: {scene.frame_start} → {scene.frame_end} @ {fps}fps | "
            f"Resolution: {scene.render.resolution_x}x{scene.render.resolution_y} | "
            f"Video: {ffmpeg.codec} {ffmpeg.video_bitrate}kb/s "
            f"(max {getattr(ffmpeg, 'maxrate', 0)}kb/s, gop {ffmpeg.gopsize}) | "
            f"Audio: {ffmpeg.audio_codec} {ffmpeg.audio_bitrate}kb/s "
            f"@ {ffmpeg.audio_mixrate}Hz {ffmpeg.audio_channels}"
        )

    # =========================================================================
    # RENDER
    # =========================================================================

    def render_sequence(self, on_start=None, on_complete=None, use_animation=True):
        """
        Render the sequencer with optional hooks.
        """
        scene = bpy.context.scene

        from pathlib import Path
        output_dir = Path.home() / "VSE_Instructor_Renders"
        output_dir.mkdir(parents=True, exist_ok=True)
        scene.render.filepath = str(output_dir / f"{self.instruction.get('_id', 'output')}.mp4")

        scene.render.use_sequencer = True

        # Pre-render handler
        def _start_handler(scene):
            self.log.info("Render started")
            if callable(on_start):
                on_start(scene)

        # Post-render handler
        def _complete_handler(scene):
            self.log.info("Render complete")
            if callable(on_complete):
                on_complete(scene)
            # Remove handlers to prevent repeated calls
            bpy.app.handlers.render_pre.remove(_start_handler)
            bpy.app.handlers.render_post.remove(_complete_handler)

        # Attach handlers
        bpy.app.handlers.render_pre.append(_start_handler)
        bpy.app.handlers.render_post.append(_complete_handler)

        # Render
        scene.render.engine = 'BLENDER_EEVEE_NEXT'  # BLENDER_WORKBENCH or 'CYCLES' depending on your project

        scene.render.use_sequencer = True

        if use_animation:
            bpy.ops.render.render(animation=True)
        else:
            bpy.ops.render.render(write_still=True)
import bpy

class Vse_renderer:
    def setup_timeline_from_output(self, output_spec, default_output="//render/output.mp4"):
        """
        Set timeline, resolution, FPS, and output path according to output_spec.
        """
        scene = bpy.context.scene
        video_spec = output_spec.get("video", {})
        audio_spec = output_spec.get("audio", {})

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
        # Output / Encoding
        # ----------------------------
        container = output_spec.get("container", "MPEG4")

        scene.render.filepath = default_output
        scene.render.image_settings.file_format = 'FFMPEG'
        scene.render.ffmpeg.format = container.upper()
        scene.render.ffmpeg.codec = video_spec.get("codec", "H264").upper()
        scene.render.ffmpeg.video_bitrate = int(video_spec.get("bitrate", 24000000))
        scene.render.ffmpeg.audio_codec = audio_spec.get("codec", "AAC").upper()
        scene.render.ffmpeg.audio_bitrate = int(audio_spec.get("bitrate", 256000))

        channels = int(audio_spec.get("channels", 2))
        scene.render.ffmpeg.audio_channels = (
            'MONO' if channels == 1 else 'STEREO'
        )

        # ----------------------------
        # Timeline Range (CORRECT)
        # ----------------------------
        seq = scene.sequence_editor
        strips = seq.sequences_all if seq else []

        if strips:
            final_starts = []
            final_ends = []

            for s in strips:
                # These are the ONLY reliable values
                final_starts.append(s.frame_final_start)
                final_ends.append(s.frame_final_end)

            scene.frame_start = int(min(final_starts))
            scene.frame_end = int(max(final_ends))

        else:
            scene.frame_start = 1
            scene.frame_end = fps * 5  # fallback

        self.log.info(
            f"Timeline set: {scene.frame_start} → {scene.frame_end} @ {fps}fps | "
            f"Resolution: {scene.render.resolution_x}x{scene.render.resolution_y}"
        )

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


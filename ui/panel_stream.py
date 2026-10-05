import bpy


class VIEW3D_PT_vse_instructor_stream(bpy.types.Panel):
    bl_label = "Viewport Stream"
    bl_idname = "VIEW3D_PT_vse_instructor_stream"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "VSE Instructor"

    def draw(self, context):
        from ..core.stream_capture import (
            STREAM_DEBUG,
            _get_state,
            ensure_stream_timer,
        )

        layout = self.layout
        scene = context.scene
        state = _get_state()

        box = layout.box()
        box.label(text="Streaming Server", icon="NETWORK_DRIVE")
        box.prop(scene, "vse_instructor_stream_host", text="Host")
        box.prop(scene, "vse_instructor_stream_port", text="Port")

        layout.separator()
        layout.label(text="Cameras")

        scene_camera_names = {obj.name for obj in scene.objects}
        cameras = sorted(
            (
                obj for obj in bpy.data.objects
                if obj.type == "CAMERA" and obj.name in scene_camera_names
            ),
            key=lambda camera: camera.name.casefold(),
        )
        if any(camera.vse_instructor_stream_enabled for camera in cameras):
            ensure_stream_timer()
        if not cameras:
            layout.box().label(text="No cameras in this scene")
        else:
            for camera in cameras:
                stream = state["streams"].get(camera.name)
                row = layout.row(align=True)
                row.prop(
                    camera,
                    "vse_instructor_stream_enabled",
                    text=camera.name,
                )
                settings_row = layout.row(align=True)
                settings_row.prop(
                    camera,
                    "vse_instructor_stream_resolution",
                    text="Resolution",
                )
                settings_row.prop(
                    camera,
                    "vse_instructor_stream_fps",
                    text="FPS",
                )
                encoder_row = layout.row(align=True)
                encoder_row.prop(
                    camera,
                    "vse_instructor_stream_encoder",
                    text="Encoder",
                )
                if stream is None:
                    row.label(text="Idle", icon="PAUSE")
                elif stream["status"] == "error":
                    row.label(text="Error", icon="ERROR")
                    if stream.get("last_error"):
                        layout.label(
                            text=stream["last_error"][:100],
                            icon="INFO",
                        )
                elif stream["status"] == "starting":
                    row.label(text="Starting", icon="TIME")
                else:
                    row.label(
                        text=f"{stream['encoder']}: {stream['frame_count']} frames",
                        icon="PLAY",
                    )
                    if STREAM_DEBUG:
                        metrics = stream["metrics"]
                        with stream["metrics_lock"]:
                            capture_time_ms = metrics["capture_time_ms"]
                            writer_time_ms = metrics["ffmpeg_write_time_ms"]
                        layout.label(
                            text=(
                                f"{stream['capture_method']} | "
                                f"capture {capture_time_ms:.1f} ms | "
                                f"FFmpeg write {writer_time_ms:.1f} ms | "
                                f"{stream['dropped_frames']} dropped"
                            ),
                            icon="TIME",
                        )

        active_count = sum(
            stream["status"] == "streaming"
            for stream in state["streams"].values()
        )
        layout.label(text=f"Active streams: {active_count}")
        layout.operator(
            "vse_instructor.stream_stop",
            text="Stop All",
            icon="PAUSE",
        )


classes = (
    VIEW3D_PT_vse_instructor_stream,
)

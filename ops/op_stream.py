import bpy


class VSE_INSTRUCTOR_OT_StreamStop(bpy.types.Operator):
    bl_idname = "vse_instructor.stream_stop"
    bl_label = "Stop All Streams"

    def execute(self, context):
        try:
            from ..core.stream_capture import stop_stream

            stop_stream(clear_selection=True)
            self.report({"INFO"}, "All camera streams stopped")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to stop camera streams: {exc}")
            return {"CANCELLED"}

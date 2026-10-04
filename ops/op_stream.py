import bpy


class VSE_INSTRUCTOR_OT_StreamStart(bpy.types.Operator):
    bl_idname = "vse_instructor.stream_start"
    bl_label = "Start Stream"

    def execute(self, context):
        try:
            from ..core.stream_capture import start_stream
            start_stream()
            self.report({"INFO"}, "Viewport streaming started")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to start stream: {exc}")
            return {"CANCELLED"}


class VSE_INSTRUCTOR_OT_StreamStop(bpy.types.Operator):
    bl_idname = "vse_instructor.stream_stop"
    bl_label = "Stop Stream"

    def execute(self, context):
        try:
            from ..core.stream_capture import stop_stream
            stop_stream()
            self.report({"INFO"}, "Viewport streaming stopped")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to stop stream: {exc}")
            return {"CANCELLED"}

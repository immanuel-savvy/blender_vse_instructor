import bpy
from . import runtime

class RENDER_SEQUENCE_OT_Operator(bpy.types.Operator):
    bl_idname = "vse_instructor.render_sequence"
    bl_label = "Render Sequence"

    def execute(self, context):
        builder = runtime._LAST_BUILDER
        if builder is None:
            self.report({'ERROR'}, "Apply an instruction first")
            return {'CANCELLED'}

        def on_complete(scene=None):
            if builder.callback.get("upload_url"):
                builder.finish_render_and_report()
            else:
                builder.log.info("No callback on instruction - render saved locally")

        builder.render_sequence(on_complete=on_complete)
        return {'FINISHED'}


class VSE_INSTRUCTOR_OT_UploadRender(bpy.types.Operator):
    bl_idname = "vse_instructor.upload_render"
    bl_label = "Upload Last Render"

    def execute(self, context):
        builder = runtime._LAST_BUILDER
        if builder is None:
            self.report({'ERROR'}, "Nothing to upload - apply and render first")
            return {'CANCELLED'}
        if not builder.callback.get("upload_url"):
            self.report({'ERROR'}, "Instruction has no callback.upload_url")
            return {'CANCELLED'}
        if not builder.finish_render_and_report():
            self.report({'ERROR'}, "Upload failed")
            return {'CANCELLED'}
        self.report({'INFO'}, "Upload complete")
        return {'FINISHED'}

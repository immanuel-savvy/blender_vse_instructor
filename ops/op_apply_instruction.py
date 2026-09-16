import bpy

class APPLY_INSTRUCTION_OT_Operator(bpy.types.Operator):
    bl_idname = "vse_instructor.apply_instruction"
    bl_label = "Build Sequence"

    def execute(self, context):
        from ..core.vse_builder import VSEBuilder
        from . import runtime

        instruction = context.scene.get("vse_instruction")

        if not instruction:
            self.report({'ERROR'}, "No instruction loaded")
            return {'CANCELLED'}

        try:
            builder = VSEBuilder(dict(instruction))
            builder.machine_id = context.scene.vse_instructor_server_props.machine_id
            if instruction.get("generation"):
                builder.set_generation(instruction["generation"])
            builder.build()
        except Exception as exc:
            self.report({'ERROR'}, f"Build failed: {exc}")
            return {'CANCELLED'}

        runtime._LAST_BUILDER = builder

        self.report({'INFO'}, "Sequence built successfully")
        return {'FINISHED'}

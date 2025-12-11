import bpy

class APPLY_INSTRUCTION_OT_Operator(bpy.types.Operator):
    bl_idname = "vse_instructor.apply_instruction"
    bl_label = "Build Sequence"

    def execute(self, context):
        instruction = bpy.app.driver_namespace['vse_instruction']

        if not instruction:
            self.report({'ERROR'}, "No instruction loaded")
            return {'CANCELLED'}

        from ..core.vse_builder import VSEBuilder
        builder = VSEBuilder(instruction)
        builder.build()

        self.report({'INFO'}, "Sequence built successfully")
        return {'FINISHED'}

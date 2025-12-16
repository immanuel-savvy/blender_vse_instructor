import bpy

class RENDER_SEQUENCE_OT_Operator(bpy.types.Operator):
    bl_idname = "vse_instructor.render_sequence"
    bl_label = "Render Sequence"

    def execute(self, context):
        from ..core.vse_builder import VSEBuilder  # your builder module

        instruction = bpy.app.driver_namespace['vse_instruction']

        builder = VSEBuilder(instruction)

        # Setup timeline according to output spec in instruction
        # output_spec = builder.instruction.get("output", {})
        # builder.setup_timeline_from_output(output_spec)

        # Render with hooks
        builder.render_sequence(
            on_start=lambda s: print("Render started"),
            on_complete=lambda s: print("Render finished!")
        )
        return {'FINISHED'}

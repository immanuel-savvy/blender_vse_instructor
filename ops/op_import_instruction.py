import bpy
import json
from pathlib import Path

# -------------------------------
# Operator to Load JSON Instruction
# -------------------------------
class IMPORT_INSTRUCTION_OT_Operator(bpy.types.Operator):
    bl_idname = "vse_instructor.import_instruction"
    bl_label = "Load Instruction"

    def execute(self, context):
        print('LOAD -- INSTRUCTION')
        props = context.scene.vse_instructor_props
        file_path = props.file_path

        if not file_path:
            self.report({'ERROR'}, "No file selected")
            return {'CANCELLED'}

        try:
            path = Path(bpy.path.abspath(file_path))
            with open(path, "r", encoding="utf-8") as instruction_file:
                instruction = json.load(instruction_file)

            # Store it somewhere safe for other operators
            context.scene["vse_instruction"] = instruction

            print("=== Instruction Loaded ===")
            print(instruction)  # logs parsed JSON to Blender console

            self.report({'INFO'}, "Instruction loaded successfully")
            return {'FINISHED'}

        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}


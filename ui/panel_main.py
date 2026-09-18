import bpy
from bpy.types import Panel, PropertyGroup
from bpy.props import StringProperty
# -------------------------------
# UI Panel
# -------------------------------


class VSEInstructorProperties(PropertyGroup):
    file_path: StringProperty(
        name="Instruction File",
        description="Select instruction JSON file",
        subtype="FILE_PATH"
    )


class VSE_INSTRUCTOR_PT_MainPanel(Panel):
    bl_label = "VSE Instructor"
    bl_idname = "VSE_INSTRUCTOR_PT_MainPanel"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "VSE Instructor"

    def draw(self, context):
                prop = context.scene.vse_instructor_props
                layout = self.layout

                box = layout.box()
                box.label(text="Instruction")
                box.prop(prop, "file_path")
                row = box.row(align=True)
                row.operator("vse_instructor.import_instruction", text="Load")
                row.operator("vse_instructor.apply_instruction", text="Apply")
                row.operator("vse_instructor.render_sequence", text="Render")
                box.operator("vse_instructor.upload_render", text="Upload last render")
                box.operator("sequencer.export_json", text="Export JSON", icon="EXPORT")
      

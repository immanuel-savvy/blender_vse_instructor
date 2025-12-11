import bpy
from bpy.types import Panel
from bpy.types import PropertyGroup
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
      layout.label(text="Select Instruction") 
      layout.prop(prop, 'file_path')

      layout.operator("vse_instructor.import_instruction", text='Load Instruction')  

      layout.operator('vse_instructor.apply_instruction', text='Apply')
      

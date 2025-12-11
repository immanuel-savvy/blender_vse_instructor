from .panel_main import VSE_INSTRUCTOR_PT_MainPanel, VSEInstructorProperties
import bpy

classes = [
    VSE_INSTRUCTOR_PT_MainPanel,
    VSEInstructorProperties
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.vse_instructor_props = bpy.props.PointerProperty(type=VSEInstructorProperties)


def unregister():
    del bpy.types.Scene.vse_instructor_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    

__all__ = ['register', 'unregister']
import bpy
from .panel_main import VSE_INSTRUCTOR_PT_MainPanel, VSEInstructorProperties
from .panel_server import VSE_INSTRUCTOR_PT_ServerPanel, VSEInstructorServerProperties, VSEServerLogLine
from .panel_logs import VSE_INSTRUCTOR_PT_Logs


classes = [
    VSEServerLogLine,
    VSE_INSTRUCTOR_PT_MainPanel,
    VSE_INSTRUCTOR_PT_ServerPanel,
    VSEInstructorProperties,
    VSEInstructorServerProperties,
    VSE_INSTRUCTOR_PT_Logs
]

def register_class_safe(cls):
    try:
        bpy.utils.register_class(cls)
    except ValueError:
        # already registered
        pass

def unregister_class_safe(cls):
    try:
        bpy.utils.unregister_class(cls)
    except ValueError:
        # already unregistered
        pass


def register():
    for cls in classes:
        register_class_safe(cls)

    # Pointer properties
    if not hasattr(bpy.types.Scene, "vse_instructor_props"):
        bpy.types.Scene.vse_instructor_props = bpy.props.PointerProperty(type=VSEInstructorProperties)

    if not hasattr(bpy.types.Scene, "vse_instructor_server_props"):
        bpy.types.Scene.vse_instructor_server_props = bpy.props.PointerProperty(type=VSEInstructorServerProperties)


def unregister():
    # Remove pointer properties first
    if hasattr(bpy.types.Scene, "vse_instructor_props"):
        del bpy.types.Scene.vse_instructor_props
    if hasattr(bpy.types.Scene, "vse_instructor_server_props"):
        del bpy.types.Scene.vse_instructor_server_props

    # Unregister classes in reverse order
    for cls in reversed(classes):
        unregister_class_safe(cls)

    

__all__ = ['register', 'unregister']
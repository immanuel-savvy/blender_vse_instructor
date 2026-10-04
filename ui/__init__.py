import bpy
from .panel_main import VSE_INSTRUCTOR_PT_MainPanel, VSEInstructorProperties
from .panel_server import VSE_INSTRUCTOR_PT_ServerPanel, VSEInstructorServerProperties, VSEServerLogLine
from .panel_logs import VSE_INSTRUCTOR_PT_Logs, VSE_INSTRUCTOR_UL_Logs
from .panel_stream import VIEW3D_PT_vse_instructor_stream


def _camera_stream_enabled_update(self, context):
    from ..core.stream_capture import ensure_stream_timer

    ensure_stream_timer()


classes = [
    VSEServerLogLine,
    VSE_INSTRUCTOR_UL_Logs,
    VSE_INSTRUCTOR_PT_MainPanel,
    VSE_INSTRUCTOR_PT_ServerPanel,
    VSEInstructorProperties,
    VSEInstructorServerProperties,
    VSE_INSTRUCTOR_PT_Logs,
    VIEW3D_PT_vse_instructor_stream,
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

    # Register stream properties from the viewport panel module
    if not hasattr(bpy.types.Scene, "vse_instructor_stream_host"):
        bpy.types.Scene.vse_instructor_stream_host = bpy.props.StringProperty(
            name="Stream Host",
            default="127.0.0.1",
            description="Hostname of the mediasoup server",
        )
    if not hasattr(bpy.types.Scene, "vse_instructor_stream_port"):
        bpy.types.Scene.vse_instructor_stream_port = bpy.props.IntProperty(
            name="Stream Port",
            default=3000,
            min=1,
            max=65535,
            description="Port of the mediasoup server",
        )
    if not hasattr(bpy.types.Object, "vse_instructor_stream_enabled"):
        bpy.types.Object.vse_instructor_stream_enabled = bpy.props.BoolProperty(
            name="Stream Camera",
            description="Stream this camera while the timeline is playing",
            default=False,
            update=_camera_stream_enabled_update,
        )


def unregister():
    from ..core.stream_capture import stop_stream

    stop_stream(clear_selection=True)

    # Remove pointer properties first
    if hasattr(bpy.types.Scene, "vse_instructor_props"):
        del bpy.types.Scene.vse_instructor_props
    if hasattr(bpy.types.Scene, "vse_instructor_server_props"):
        del bpy.types.Scene.vse_instructor_server_props
    if hasattr(bpy.types.Scene, "vse_instructor_stream_host"):
        del bpy.types.Scene.vse_instructor_stream_host
    if hasattr(bpy.types.Scene, "vse_instructor_stream_port"):
        del bpy.types.Scene.vse_instructor_stream_port
    if hasattr(bpy.types.Object, "vse_instructor_stream_enabled"):
        del bpy.types.Object.vse_instructor_stream_enabled

    # Unregister classes in reverse order
    for cls in reversed(classes):
        unregister_class_safe(cls)

    

__all__ = ['register', 'unregister']
import bpy
from .panel_main import VSE_INSTRUCTOR_PT_MainPanel, VSEInstructorProperties
from .panel_server import VSE_INSTRUCTOR_PT_ServerPanel, VSEInstructorServerProperties, VSEServerLogLine
from .panel_logs import VSE_INSTRUCTOR_PT_Logs, VSE_INSTRUCTOR_UL_Logs
from .panel_stream import VIEW3D_PT_vse_instructor_stream


def _camera_stream_setting_update(self, context):
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
            update=_camera_stream_setting_update,
        )
    if not hasattr(bpy.types.Object, "vse_instructor_stream_resolution"):
        bpy.types.Object.vse_instructor_stream_resolution = bpy.props.EnumProperty(
            name="Stream Resolution",
            description="Resolution used by this camera's stream",
            items=[
                ("480p", "480p", "854 x 480"),
                ("720p", "720p", "1280 x 720"),
                ("1080p", "1080p", "1920 x 1080"),
                ("1440p", "1440p", "2560 x 1440"),
                ("2160p", "2160p", "3840 x 2160"),
                ("scene", "Scene", "Use the scene render dimensions"),
            ],
            default="1080p",
            update=_camera_stream_setting_update,
        )
    if not hasattr(bpy.types.Object, "vse_instructor_stream_fps"):
        bpy.types.Object.vse_instructor_stream_fps = bpy.props.IntProperty(
            name="Stream FPS",
            description="Capture rate for this camera's stream",
            default=30,
            min=1,
            max=60,
            update=_camera_stream_setting_update,
        )
    if not hasattr(bpy.types.Object, "vse_instructor_stream_encoder"):
        bpy.types.Object.vse_instructor_stream_encoder = bpy.props.EnumProperty(
            name="Stream Encoder",
            description="FFmpeg H.264 encoder used by this camera's stream",
            items=[
                ("auto", "Automatic", "Prefer a platform hardware encoder"),
                ("software", "Software", "Use the libx264 software encoder"),
                ("apple", "Apple VideoToolbox", "Use Apple's VideoToolbox encoder"),
                ("nvidia", "NVIDIA NVENC", "Use the NVIDIA NVENC encoder"),
                ("amd", "AMD AMF", "Use the AMD AMF encoder"),
            ],
            default="auto",
            update=_camera_stream_setting_update,
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
    if hasattr(bpy.types.Object, "vse_instructor_stream_resolution"):
        del bpy.types.Object.vse_instructor_stream_resolution
    if hasattr(bpy.types.Object, "vse_instructor_stream_fps"):
        del bpy.types.Object.vse_instructor_stream_fps
    if hasattr(bpy.types.Object, "vse_instructor_stream_encoder"):
        del bpy.types.Object.vse_instructor_stream_encoder

    # Unregister classes in reverse order
    for cls in reversed(classes):
        unregister_class_safe(cls)

    

__all__ = ['register', 'unregister']
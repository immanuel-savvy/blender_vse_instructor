import bpy


class VIEW3D_PT_vse_instructor_stream(bpy.types.Panel):
    bl_label = "Viewport Stream"
    bl_idname = "VIEW3D_PT_vse_instructor_stream"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "VSE Instructor"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        box.label(text="Server", icon='NETWORK_DRIVE')
        col = box.column(align=True)
        col.prop(scene, "vse_instructor_stream_host", text="Host")
        col.prop(scene, "vse_instructor_stream_port", text="Port")

        status = scene.vse_instructor_stream_status
        frames = scene.vse_instructor_stream_frames
        error = scene.vse_instructor_stream_error

        box = layout.box()
        row = box.row()
        if status == "streaming":
            row.label(text="Status: Streaming", icon='PLAY')
            row.label(text=f"{frames} frames")
        elif status == "starting":
            row.label(text="Status: Starting…", icon='TIME')
        elif status == "error":
            row.label(text="Status: Error", icon='ERROR')
            if error:
                box.label(text=error, icon='INFO')
        else:
            row.label(text="Status: Idle", icon='PAUSE')

        layout.separator()
        row = layout.row(align=True)

        col = row.column()
        col.enabled = status not in {"streaming", "starting"}
        col.operator("vse_instructor.stream_start", icon='PLAY', text="Start Stream")

        col = row.column()
        col.enabled = status in {"streaming", "starting", "error"}
        col.operator("vse_instructor.stream_stop", icon='PAUSE', text="Stop Stream")

        layout.separator()
        col = layout.column(align=True)
        col.scale_y = 0.85
        col.label(text="1. Make sure Node server is running", icon='INFO')
        col.label(text="2. Click Start Stream")
        col.label(text="3. Press Play on the timeline")


classes = (
    VIEW3D_PT_vse_instructor_stream,
)


def register():
    bpy.types.Scene.vse_instructor_stream_host = bpy.props.StringProperty(
        name="Stream Host",
        default="127.0.0.1",
        description="Hostname of the mediasoup server",
    )
    bpy.types.Scene.vse_instructor_stream_port = bpy.props.IntProperty(
        name="Stream Port",
        default=3000,
        min=1,
        max=65535,
        description="Port of the mediasoup server",
    )
    bpy.types.Scene.vse_instructor_stream_status = bpy.props.StringProperty(
        name="Stream Status",
        default="idle",
    )
    bpy.types.Scene.vse_instructor_stream_error = bpy.props.StringProperty(
        name="Stream Error",
        default="",
    )
    bpy.types.Scene.vse_instructor_stream_frames = bpy.props.IntProperty(
        name="Stream Frames",
        default=0,
    )

    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.vse_instructor_stream_host
    del bpy.types.Scene.vse_instructor_stream_port
    del bpy.types.Scene.vse_instructor_stream_status
    del bpy.types.Scene.vse_instructor_stream_error
    del bpy.types.Scene.vse_instructor_stream_frames

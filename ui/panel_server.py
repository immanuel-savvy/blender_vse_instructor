import bpy
from bpy.types import Panel
from bpy.types import PropertyGroup
from bpy.props import StringProperty, CollectionProperty


def connection_status_update(self, context):
    for area in context.screen.areas:
        if area.type == 'SEQUENCE_EDITOR':
            area.tag_redraw()

class VSEServerLogLine(PropertyGroup):
    text: StringProperty()

class VSEInstructorServerProperties(PropertyGroup):
    server_url: StringProperty(
        name="Server URL",
        description="Backend server endpoint",
        default="https://blender-backend.vercel.app"
    )

    connection_status: StringProperty(
        name="Status",
        description="Server connection status",
        default="Offline",
        update=connection_status_update
    )

    server_running: bpy.props.BoolProperty(
        name="Server Running",
        description="Is the VSE server running?",
        default=False
    )

    last_message: StringProperty(
        name="Last Message",
        description="Last message from server",
        default=""
    )

    logs: CollectionProperty(type=VSEServerLogLine)


class VSE_INSTRUCTOR_PT_ServerPanel(bpy.types.Panel):
    bl_label = "Server"
    bl_idname = "VSE_INSTRUCTOR_PT_ServerPanel"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "VSE Instructor"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props = context.scene.vse_instructor_server_props

        layout.label(text="Connection")
        layout.prop(props, "server_url")

        layout.separator()
        layout.label(text="Server Control")

        # Dynamic label
        op = layout.operator(
            "vse_instructor.server_toggle",
            text="Stop Server" if props.server_running else "Start Server"
        )

        row = layout.row()
        row.label(text="Status:")
        row.label(text=props.connection_status)

        # layout.separator()
        # layout.label(text="Traffic")
        # layout.label(text=props.traffic_info)

        layout.separator()
        layout.label(text="Last Message")
        box = layout.box()
        box.label(text=props.last_message if props.last_message else "—")

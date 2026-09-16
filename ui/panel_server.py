import bpy
from bpy.types import Panel
from bpy.types import PropertyGroup
from bpy.props import StringProperty, CollectionProperty, IntProperty


def connection_status_update(self, context):
    for area in context.screen.areas:
        if area.type == 'SEQUENCE_EDITOR':
            area.tag_redraw()


class VSEServerLogLine(PropertyGroup):
    text: StringProperty()

class VSEInstructorServerProperties(PropertyGroup):
    server_url: StringProperty(
        name="Server URL",
        description="Backend used only for polling jobs",
        default="",
    )

    connection_status: StringProperty(
        name="Status",
        description="Server connection status",
        default="Idle",
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

    poll_interval: IntProperty(
        name="Poll interval (s)",
        default=60,
        min=5,
        max=600,
    )

    machine_id: StringProperty(
        name="Machine ID",
        default="savvy-m1-air-2020",
    )

    logs: CollectionProperty(type=VSEServerLogLine)

    log_index: IntProperty(default=0)


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
        layout.prop(props, "machine_id")
        layout.prop(props, "poll_interval")

        layout.separator()
        layout.label(text="Server Control")

        # Dynamic label
        layout.operator(
            "vse_instructor.server_toggle",
            text="Stop Polling" if props.server_running else "Start Polling"
        )

        row = layout.row()
        row.label(text="Status:")
        row.label(text=props.connection_status)

        layout.separator()
        layout.label(text="Last Message")
        box = layout.box()
        box.label(text=props.last_message if props.last_message else "")
import bpy
from bpy.types import Panel
from bpy.types import PropertyGroup
from bpy.props import StringProperty, CollectionProperty


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
        default="Disconnected"
    )

    last_message: StringProperty(
        name="Last Message",
        description="Last message from server",
        default=""
    )

    traffic_info: StringProperty(
        name="Traffic",
        description="Traffic stats or last activity",
        default="Idle"
    )

    logs: CollectionProperty(type=VSEServerLogLine)



class VSE_INSTRUCTOR_PT_ServerPanel(Panel):
    bl_label = "Server"
    bl_idname = "VSE_INSTRUCTOR_PT_ServerPanel"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "VSE Instructor"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props = context.scene.vse_instructor_server_props

        # -----------------------------
        # Connection
        # -----------------------------
        layout.label(text="Connection")
        layout.prop(props, "server_url")

        row = layout.row()
        row.label(text="Status:")
        row.label(text=props.connection_status)

        # -----------------------------
        # Traffic
        # -----------------------------
        layout.separator()
        layout.label(text="Traffic")
        layout.label(text=props.traffic_info)

        # -----------------------------
        # Last Message
        # -----------------------------
        layout.separator()
        layout.label(text="Last Message")
        box = layout.box()
        box.label(text=props.last_message if props.last_message else "—")

        

import bpy
from bpy.types import Panel, UIList

# -------------------------------
# UI List (renders each log line, scrollable)
# -------------------------------

class VSE_INSTRUCTOR_UL_Logs(UIList):
    bl_idname = "VSE_INSTRUCTOR_UL_Logs"

    def draw_item(
        self,
        context,
        layout,
        data,
        item,
        icon,
        active_data,
        active_propname,
        index,
    ):
        row = layout.row(align=True)
        row.label(text=item.text)

    def filter_items(self, context, data, propname):
        # Keep natural (insertion) order rather than Blender's default
        # alphabetical UIList sort, which would scramble log order.
        items = getattr(data, propname)
        flags = [self.bitflag_filter_item] * len(items)
        order = list(range(len(items)))
        return flags, order


# -------------------------------
# UI Panel
# -------------------------------

class VSE_INSTRUCTOR_PT_Logs(Panel):
    bl_label = "Debug Logs"
    bl_idname = "VSE_INSTRUCTOR_PT_Logs"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "VSE Logs"

    def draw(self, context):
        layout = self.layout
        props = context.scene.vse_instructor_server_props

        layout.separator()
        layout.label(text="Logs")

        if not props.logs:
            layout.box().label(text="No logs yet")
        else:
            layout.template_list(
                "VSE_INSTRUCTOR_UL_Logs", "",
                props, "logs",
                props, "log_index",
                rows=15,
            )
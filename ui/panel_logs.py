import bpy
from bpy.types import Panel
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

    log_box = layout.box()
    logs = props.logs

    if not logs:
        log_box.label(text="No logs yet")
    else:
        # show last 15 logs
        for item in logs[-15:]:
            log_box.label(text=item.text)
    

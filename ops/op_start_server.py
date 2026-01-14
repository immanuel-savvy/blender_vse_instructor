import bpy
from ..core.poll_server import poll_backend_for_render, IS_RENDERING, HANDLERS_ATTACHED, logger

class VSE_INSTRUCTOR_OT_ServerToggle(bpy.types.Operator):
    bl_idname = "vse_instructor.server_toggle"
    bl_label = "Start Server"

    _timer_registered: bool = False

    def execute(self, context):
        global IS_RENDERING, HANDLERS_ATTACHED

        props = context.scene.vse_instructor_server_props

        if props.server_running:
            # Stop server
            IS_RENDERING = False
            HANDLERS_ATTACHED = False
            props.server_running = False
            props.connection_status = "Offline"
            self.report({'INFO'}, "Server stopped")
            logger.info("VSE Server polling stopped")
            return {'FINISHED'}

        # Start server
        IS_RENDERING = False
        HANDLERS_ATTACHED = False
        try:
            bpy.app.timers.register(poll_backend_for_render, first_interval=1)
            props.server_running = True
            props.connection_status = "Polling"
            self.report({'INFO'}, "Server started — polling enabled")
            logger.info("VSE Server polling started")
        except Exception as e:
            props.connection_status = "Error"
            self.report({'ERROR'}, f"Failed to start server: {e}")
            logger.error("Failed to start VSE server polling:", e)
            return {'CANCELLED'}

        return {'FINISHED'}

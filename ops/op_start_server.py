import bpy
from ..core import poll_server as worker

class VSE_INSTRUCTOR_OT_ServerToggle(bpy.types.Operator):
    bl_idname = "vse_instructor.server_toggle"
    bl_label = "Toggle Polling"

    _timer_registered: bool = False

    def execute(self, context):
        props = context.scene.vse_instructor_server_props

        if props.server_running:
            # Stop polling.
            worker.IS_RENDERING = False
            worker.HANDLERS_ATTACHED = False
            props.server_running = False
            if bpy.app.timers.is_registered(worker.poll_backend_for_render):
                bpy.app.timers.unregister(worker.poll_backend_for_render)
            props.connection_status = "Idle"
            self.report({'INFO'}, "Polling stopped")
            worker.logger.info("VSE Server polling stopped")
            return {'FINISHED'}

        # Start polling.
        worker.IS_RENDERING = False
        worker.HANDLERS_ATTACHED = False
        if not props.server_url:
            self.report({'ERROR'}, "Set Server URL first")
            return {'CANCELLED'}
        try:
            props.server_running = True
            props.connection_status = "Polling"
            if not bpy.app.timers.is_registered(worker.poll_backend_for_render):
                bpy.app.timers.register(
                    worker.poll_backend_for_render,
                    first_interval=1.0,
                )
            self.report({'INFO'}, "Polling started")
            worker.logger.info("VSE Server polling started")
        except Exception as e:
            props.connection_status = "Error"
            self.report({'ERROR'}, f"Failed to start server: {e}")
            worker.logger.error(f"Failed to start VSE server polling: {e}")
            return {'CANCELLED'}

        return {'FINISHED'}

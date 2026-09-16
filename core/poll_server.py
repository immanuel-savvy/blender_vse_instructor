import json
import urllib.request
import bpy
from .logger import Logger
from .vse_builder import VSEBuilder

# ------------------------------
# Constants & globals
# ------------------------------
IS_RENDERING = False
HANDLERS_ATTACHED = False

logger = Logger()

# ------------------------------
# Render sequence logic
# ------------------------------
def render_sequence(builder):
    generation_id = (builder.generation or {}).get('_id')
    global HANDLERS_ATTACHED

    if HANDLERS_ATTACHED:
        builder.log.warning("Render handlers already attached")
        return

    HANDLERS_ATTACHED = True
    props = bpy.context.scene.vse_instructor_server_props

    def on_start(scene=None):
        builder.log.info(f"[Render] Started generation {generation_id}")
        builder.update_server_status("RENDERING")
        props.connection_status = "Busy"

    def on_complete(scene=None):
        global IS_RENDERING, HANDLERS_ATTACHED

        builder.log.info(f"[Render] Completed generation {generation_id}")

        # Remove handlers first
        if on_start in bpy.app.handlers.render_pre:
            bpy.app.handlers.render_pre.remove(on_start)
        if on_complete in bpy.app.handlers.render_complete:
            bpy.app.handlers.render_complete.remove(on_complete)

        builder.finish_render_and_report()
        props.connection_status = "Idle"

        IS_RENDERING = False
        HANDLERS_ATTACHED = False

        # Resume only while polling remains enabled by the button.
        if props.server_running and not bpy.app.timers.is_registered(
            poll_backend_for_render
        ):
            bpy.app.timers.register(
                poll_backend_for_render,
                first_interval=props.poll_interval,
            )

    bpy.app.handlers.render_pre.append(on_start)
    bpy.app.handlers.render_complete.append(on_complete)

    builder.render_sequence(on_complete=None, on_start=None)

# ------------------------------
# Start a render job
# ------------------------------
def start_render_job(generation):
    global IS_RENDERING

    IS_RENDERING = True
    instruction = dict(generation.get('config') or {})
    instruction.setdefault("generation", {
        "_id": generation.get("_id"),
        **(generation.get("generation") or {}),
    })
    if generation.get("callback"):
        instruction["callback"] = generation["callback"]
    builder = VSEBuilder(instruction)

    builder.machine_id = bpy.context.scene.vse_instructor_server_props.machine_id

    builder.set_generation(generation)

    # builder.generation = generation

    builder.update_server_status("STARTED")
    builder.build()
    # builder.save_blend_snapshot(generation.get('_id', 'render_job'))
    render_sequence(builder)


def update_ui():
    for area in bpy.context.screen.areas:
        if area.type == 'SEQUENCE_EDITOR':
            area.tag_redraw()
# ------------------------------
# Polling backend
# ------------------------------
def poll_backend_for_render():
    global IS_RENDERING
    scene = bpy.context.scene
    props = scene.vse_instructor_server_props

    if not props.server_running:
        return None

    logger.info("Polling backend...")
    if IS_RENDERING:
        props.connection_status = "Busy"
        return props.poll_interval

    props.connection_status = "Polling"
    update_ui()
    try:
        if not props.server_url:
            logger.error("Cannot poll: server URL is not configured")
            props.connection_status = "Offline"
            return props.poll_interval

        payload = json.dumps({"machine": props.machine_id}).encode("utf-8")
        logger.info("Sending probe for generation request")
        req = urllib.request.Request(
            url=f"{props.server_url.rstrip('/')}/probe_new_generation",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=5) as res:
            response = json.loads(res.read().decode("utf-8"))

        logger.info(f"Probe response: {response.get('message')}")
        props.last_message = response.get("message", "")

        # No job
        if not response.get("ok"):
            props.connection_status = "Idle"
            update_ui()
            return props.poll_interval

        # Job found
        generation = response.get("data")
        if not generation:
            props.connection_status = "Idle"
            update_ui()
            return props.poll_interval

        # Start render
        props.connection_status = "Busy"
        update_ui()
        start_render_job(generation)
        return None  # stop timer until render completes

    except Exception as e:
        logger.error(f"Polling error: {e}")
        props.connection_status = "Error"
        update_ui()
        return props.poll_interval


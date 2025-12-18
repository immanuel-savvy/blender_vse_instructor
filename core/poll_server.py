import json
import urllib.request
import bpy
from .logger import Logger

MACHINE_ID = "savvy-m1-air-2020"

IS_RENDERING = False
HANDLERS_ATTACHED = False
POLL_INTERVAL = 60


from .vse_builder import VSEBuilder


def render_sequence(builder):
    generation_id = builder.generation.get('_id')

    global HANDLERS_ATTACHED

    # 🚫 Prevent double handler registration
    if HANDLERS_ATTACHED:
        builder.log.warning("Render handlers already attached")
        return

    HANDLERS_ATTACHED = True

    def on_start(scene=None):
        builder.log.info(f"[Render] Started generation {generation_id}")
        builder.update_server_status("RENDERING")

    def on_complete(scene=None):
        global IS_RENDERING, HANDLERS_ATTACHED

        builder.log.info(f"[Render] Completed generation {generation_id}")

        # 🛑 VERY IMPORTANT: Remove handlers FIRST
        if on_start in bpy.app.handlers.render_pre:
            bpy.app.handlers.render_pre.remove(on_start)

        if on_complete in bpy.app.handlers.render_complete:
            bpy.app.handlers.render_complete.remove(on_complete)

        # Upload once
        media = builder.upload_rendered_media()
        if media:
            builder.generation_complete(media.get('_id'))

        builder.update_server_status("DONE")

        IS_RENDERING = False
        HANDLERS_ATTACHED = False

        # 🔁 Resume polling
        bpy.app.timers.register(
            poll_backend_for_render,
            first_interval=POLL_INTERVAL
        )

    bpy.app.handlers.render_pre.append(on_start)
    bpy.app.handlers.render_complete.append(on_complete)

    builder.render_sequence(
        on_complete=None,  # avoid double-wiring
        on_start=None
    )


def start_render_job(generation):
    global IS_RENDERING

    IS_RENDERING = True

    builder = VSEBuilder(generation.get('config'))
    builder.set_generation(generation)

    builder.build()

    render_sequence(
        builder
    )

logger = Logger()
def poll_backend_for_render():
    global IS_RENDERING

    logger.info("Polling...")

    # 🚫 Pause polling while rendering
    if IS_RENDERING:
        return POLL_INTERVAL

    try:
        payload = json.dumps({
            "machine": MACHINE_ID
        }).encode("utf-8")

        logger.info("Sending Probe for generation request")
        req = urllib.request.Request(
            url=f"{VSEBuilder.server_url}/probe_new_generation",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        with urllib.request.urlopen(req, timeout=5) as res:
            response = json.loads(res.read().decode("utf-8"))

        logger.info(f"Probe response: {response.get('message')}")

        # 🔍 No job
        if not response.get("ok"):
            return POLL_INTERVAL

        # 🎯 Job found
        generation = response.get("data")
        if not generation:
            return POLL_INTERVAL

        # Stop polling → start render
        logger.info("Found generation")
        start_render_job(generation)

        return None  # stop timer until render completes

    except Exception as e:
        logger.error("Polling error:", e)
        return POLL_INTERVAL

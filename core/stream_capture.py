import bpy
import gpu
import numpy as np
import subprocess
import urllib.request
import json

ns = bpy.app.driver_namespace
STREAM_KEY = "vse_instructor_stream"


def _get_state():
    if STREAM_KEY not in ns:
        ns[STREAM_KEY] = {
            "offscreen": None,
            "ffmpeg": None,
            "started": False,
            "frame_count": 0,
            "handler": None,
            "timer": None,
            "status": "idle",
            "last_error": "",
        }
    return ns[STREAM_KEY]


def _set_status(status, error=""):
    state = _get_state()
    state["status"] = status
    state["last_error"] = error

    scene = bpy.context.scene
    if hasattr(scene, "vse_instructor_stream_status"):
        scene.vse_instructor_stream_status = status
    if hasattr(scene, "vse_instructor_stream_error"):
        scene.vse_instructor_stream_error = error
    if hasattr(scene, "vse_instructor_stream_frames"):
        scene.vse_instructor_stream_frames = state["frame_count"]


def _find_view3d():
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'WINDOW':
                    return area.spaces.active, region
    return None, None


def _start_ffmpeg(w, h, host, rtp_port, rtcp_port):
    cmd = [
        "ffmpeg",
        "-f", "rawvideo",
        "-pix_fmt", "rgba",
        "-s", f"{w}x{h}",
        "-r", "24",
        "-i", "-",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",
        "-g", "15",
        "-keyint_min", "15",
        "-bf", "0",
        "-f", "rtp",
        "-payload_type", "96",
        "-ssrc", "22222222",
        f"rtp://{host}:{rtp_port}?rtcpport={rtcp_port}",
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.STDOUT)


def _drain():
    state = _get_state()

    if not bpy.context.screen.is_animation_playing:
        if state["status"] == "streaming":
            _set_status("idle")
        return 0.1

    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        _set_status("error", "No camera in scene")
        return 0.05

    space, region = _find_view3d()
    if space is None:
        _set_status("error", "No 3D Viewport found")
        return 0.05

    scale = scene.render.resolution_percentage / 100.0
    w = int(scene.render.resolution_x * scale)
    h = int(scene.render.resolution_y * scale)

    if (
        state["offscreen"] is None
        or state["offscreen"].width != w
        or state["offscreen"].height != h
    ):
        if state["offscreen"]:
            state["offscreen"].free()
        state["offscreen"] = gpu.types.GPUOffScreen(w, h)

        if state["ffmpeg"]:
            try:
                state["ffmpeg"].stdin.close()
                state["ffmpeg"].terminate()
            except Exception:
                pass
            state["ffmpeg"] = None
            state["started"] = False

    if not state["started"]:
        _set_status("starting")
        try:
            host = scene.vse_instructor_stream_host
            port = scene.vse_instructor_stream_port

            req = urllib.request.Request(
                f"http://{host}:{port}/reset",
                method="POST",
                data=b"",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                rtp = data["rtpPort"]
                rtcp = data["rtcpPort"]
                print(f"[Stream] Reset OK → RTP {rtp} RTCP {rtcp}")

            state["ffmpeg"] = _start_ffmpeg(w, h, host, rtp, rtcp)
            state["started"] = True
            state["frame_count"] = 0
            _set_status("streaming")
            print("[Stream] FFmpeg started")
        except Exception as e:
            msg = str(e)
            print("[Stream] Error starting:", msg)
            _set_status("error", msg)
            return 1.0

    view_matrix = camera.matrix_world.inverted()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    projection_matrix = camera.calc_matrix_camera(depsgraph, x=w, y=h)

    overlay = space.overlay
    show = overlay.show_overlays
    overlay.show_overlays = False
    try:
        state["offscreen"].draw_view3d(
            scene,
            bpy.context.view_layer,
            space,
            region,
            view_matrix,
            projection_matrix,
            do_color_management=True,
        )
    finally:
        overlay.show_overlays = show

    with state["offscreen"].bind():
        fb = gpu.state.active_framebuffer_get()
        buf = fb.read_color(0, 0, w, h, 4, 0, 'UBYTE')
        arr = np.array(buf.to_list(), dtype=np.uint8).reshape(h, w, 4)

    # Blender/GPU framebuffers are bottom-origin; FFmpeg expects top-origin,
    # so vertically flip the captured buffer before writing it out.
    arr = np.flipud(arr)

    try:
        state["ffmpeg"].stdin.write(arr.tobytes())
        state["ffmpeg"].stdin.flush()
        state["frame_count"] += 1
        if state["frame_count"] % 24 == 0:
            print(f"[Stream] Sent {state['frame_count']} frames")
        if hasattr(bpy.context.scene, "vse_instructor_stream_frames"):
            bpy.context.scene.vse_instructor_stream_frames = state["frame_count"]
    except Exception as e:
        msg = str(e)
        print("[Stream] FFmpeg pipe error:", msg)
        state["started"] = False
        state["ffmpeg"] = None
        _set_status("error", msg)
        return None

    return 0.04


def start_stream():
    stop_stream()

    state = _get_state()
    state["started"] = False
    state["frame_count"] = 0
    _set_status("idle")

    def on_draw():
        pass

    state["handler"] = bpy.types.SpaceView3D.draw_handler_add(
        on_draw, (), 'WINDOW', 'POST_PIXEL'
    )
    state["timer"] = _drain
    bpy.app.timers.register(_drain, first_interval=0.1)
    print("[Stream] Capture armed – press Play on the timeline")


def stop_stream():
    state = _get_state()

    if state.get("handler"):
        try:
            bpy.types.SpaceView3D.draw_handler_remove(state["handler"], 'WINDOW')
        except Exception:
            pass
        state["handler"] = None

    if state.get("timer") and bpy.app.timers.is_registered(state["timer"]):
        bpy.app.timers.unregister(state["timer"])
        state["timer"] = None

    if state.get("ffmpeg"):
        try:
            if state["ffmpeg"].stdin:
                state["ffmpeg"].stdin.close()
            state["ffmpeg"].terminate()
            state["ffmpeg"].wait(timeout=1.5)
        except Exception:
            try:
                state["ffmpeg"].kill()
            except Exception:
                pass
        state["ffmpeg"] = None

    if state.get("offscreen"):
        try:
            state["offscreen"].free()
        except Exception:
            pass
        state["offscreen"] = None

    state["started"] = False
    _set_status("idle")
    print("[Stream] Stopped")

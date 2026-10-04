import json
import os
import secrets
import shutil
import subprocess
import urllib.request

import bpy
import gpu
import numpy as np


STREAM_KEY = "vse_instructor_stream"
ns = bpy.app.driver_namespace


def _get_state():
    state = ns.get(STREAM_KEY)
    if state is None or "streams" not in state:
        if state is not None:
            old_timer = state.get("timer")
            if old_timer and bpy.app.timers.is_registered(old_timer):
                bpy.app.timers.unregister(old_timer)
            old_process = state.get("ffmpeg")
            if old_process is not None:
                try:
                    if old_process.stdin and not old_process.stdin.closed:
                        old_process.stdin.close()
                    if old_process.poll() is None:
                        old_process.terminate()
                        old_process.wait(timeout=1.5)
                except Exception:
                    if old_process.poll() is None:
                        old_process.kill()
            old_offscreen = state.get("offscreen")
            if old_offscreen is not None:
                old_offscreen.free()
        state = {
            "streams": {},
            "timer": None,
            "stopping": False,
        }
        ns[STREAM_KEY] = state
    return state


def _resolve_ffmpeg_path():
    candidates = [
        shutil.which("ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError(
        "Could not find FFmpeg. Install it or make it available at "
        "/opt/homebrew/bin/ffmpeg or /usr/local/bin/ffmpeg."
    )


def _server_url(scene):
    host = scene.vse_instructor_stream_host.strip()
    if not host:
        raise ValueError("Set the streaming server host before selecting a camera.")
    return f"http://{host}:{scene.vse_instructor_stream_port}"


def request_stream_start(camera_id, ssrc, scene):
    payload = json.dumps({
        "cameraId": camera_id,
        "ssrc": ssrc,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{_server_url(scene)}/streams/start",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.loads(response.read().decode("utf-8"))

    if result.get("cameraId") != camera_id:
        raise ValueError(
            f"Streaming server returned cameraId {result.get('cameraId')!r}; "
            f"expected {camera_id!r}."
        )
    host = result.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ValueError(
            "Streaming server response must contain a non-empty RTP destination host."
        )

    ports = (result.get("rtpPort"), result.get("rtcpPort"))
    if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
        raise ValueError(
            "Streaming server response must contain integer rtpPort and rtcpPort "
            "values between 1 and 65535."
        )
    return host.strip(), ports[0], ports[1]


def request_stream_stop(camera_id, scene, server_url=None):
    payload = json.dumps({"cameraId": camera_id}).encode("utf-8")
    request = urllib.request.Request(
        f"{server_url or _server_url(scene)}/streams/stop",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def _unique_ssrc(streams):
    used = {stream.get("ssrc") for stream in streams.values()}
    while True:
        ssrc = secrets.randbelow(0xFFFFFFFF - 1) + 1
        if ssrc not in used:
            return ssrc


def _start_ffmpeg(width, height, host, rtp_port, rtcp_port, ssrc, fps):
    ffmpeg_path = _resolve_ffmpeg_path()
    command = [
        ffmpeg_path,
        "-f", "rawvideo",
        "-pix_fmt", "rgba",
        "-s", f"{width}x{height}",
        "-r", str(fps),
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
        "-ssrc", str(ssrc),
        f"rtp://{host}:{rtp_port}?rtcpport={rtcp_port}",
    ]
    env = os.environ.copy()
    env["PATH"] = (
        "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:"
        "/usr/local/sbin:/usr/bin:/bin:" + env.get("PATH", "")
    )
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )


def _find_view3d():
    manager = bpy.context.window_manager
    for window in manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "WINDOW":
                        return area.spaces.active, region
    return None, None


def _tag_redraw():
    manager = bpy.context.window_manager
    for window in manager.windows:
        for area in window.screen.areas:
            if area.type in {"VIEW_3D", "SEQUENCE_EDITOR"}:
                area.tag_redraw()


def _current_scene_cameras(scene):
    scene_camera_names = {obj.name for obj in scene.objects}
    return {
        obj.name: obj
        for obj in bpy.data.objects
        if obj.type == "CAMERA" and obj.name in scene_camera_names
    }


def _free_local_resources(stream):
    process = stream.get("ffmpeg")
    if process is not None:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1.5)
        except Exception:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=1.5)
            except Exception as exc:
                print(f"[Stream] Could not fully stop FFmpeg: {exc}")
        stream["ffmpeg"] = None

    offscreen = stream.get("offscreen")
    if offscreen is not None:
        try:
            offscreen.free()
        except Exception as exc:
            print(f"[Stream] Could not free GPUOffScreen: {exc}")
        stream["offscreen"] = None


def _stop_camera_stream(camera_id, scene, notify_server=True):
    manager = _get_state()
    stream = manager["streams"].get(camera_id)
    if stream is None:
        return

    _free_local_resources(stream)
    if notify_server and stream.get("server_started"):
        try:
            request_stream_stop(camera_id, scene, stream.get("server_url"))
        except Exception as exc:
            print(f"[Stream] Failed to stop server stream for {camera_id}: {exc}")
    manager["streams"].pop(camera_id, None)
    print(f"[Stream] Stopped camera {camera_id}")


def _start_camera_stream(camera_id, camera, scene):
    manager = _get_state()
    stream = {
        "camera": camera,
        "offscreen": None,
        "ffmpeg": None,
        "started": False,
        "frame_count": 0,
        "rtp_port": None,
        "rtcp_port": None,
        "ssrc": _unique_ssrc(manager["streams"]),
        "status": "starting",
        "last_error": "",
        "server_started": False,
        "server_url": None,
        "host": None,
        "fps": None,
        "width": None,
        "height": None,
    }
    manager["streams"][camera_id] = stream

    try:
        base_url = _server_url(scene)
        stream["server_url"] = base_url
        stream["host"], stream["rtp_port"], stream["rtcp_port"] = request_stream_start(
            camera_id,
            stream["ssrc"],
            scene,
        )
        stream["server_started"] = True

        scale = scene.render.resolution_percentage / 100.0
        width = max(1, int(scene.render.resolution_x * scale))
        height = max(1, int(scene.render.resolution_y * scale))
        fps = scene.render.fps / scene.render.fps_base
        stream["fps"] = fps

        stream["ffmpeg"] = _start_ffmpeg(
            width,
            height,
            stream["host"],
            stream["rtp_port"],
            stream["rtcp_port"],
            stream["ssrc"],
            fps,
        )
        stream["offscreen"] = gpu.types.GPUOffScreen(width, height)
        stream["width"] = width
        stream["height"] = height
        stream["started"] = True
        stream["status"] = "streaming"
        print(
            f"[Stream] Started {camera_id}: RTP {stream['rtp_port']}, "
            f"RTCP {stream['rtcp_port']}, SSRC {stream['ssrc']} ({base_url})"
        )
    except Exception as exc:
        stream["status"] = "error"
        stream["last_error"] = str(exc)
        _free_local_resources(stream)
        if stream.get("server_started"):
            try:
                request_stream_stop(camera_id, scene, stream.get("server_url"))
            except Exception as stop_exc:
                print(
                    f"[Stream] Failed to release server stream for {camera_id}: "
                    f"{stop_exc}"
                )
        stream["server_started"] = False
        print(f"[Stream] Failed to start {camera_id}: {exc}")


def reconcile_streams(scene=None):
    manager = _get_state()
    scene = scene or bpy.context.scene
    cameras = _current_scene_cameras(scene)
    streams = manager["streams"]

    for camera_id in tuple(streams):
        camera = cameras.get(camera_id)
        if camera is None or not camera.vse_instructor_stream_enabled:
            _stop_camera_stream(camera_id, scene)
        else:
            streams[camera_id]["camera"] = camera

    for camera_id, camera in cameras.items():
        if camera.vse_instructor_stream_enabled and camera_id not in streams:
            _start_camera_stream(camera_id, camera, scene)


def _capture_stream(stream, scene, space, region, width, height):
    camera = stream["camera"]
    depsgraph = bpy.context.evaluated_depsgraph_get()
    view_matrix = camera.matrix_world.inverted()
    projection_matrix = camera.calc_matrix_camera(
        depsgraph,
        x=width,
        y=height,
    )

    overlay = space.overlay
    show_overlays = overlay.show_overlays
    overlay.show_overlays = False
    try:
        stream["offscreen"].draw_view3d(
            scene,
            bpy.context.view_layer,
            space,
            region,
            view_matrix,
            projection_matrix,
            do_color_management=True,
        )
    finally:
        overlay.show_overlays = show_overlays

    with stream["offscreen"].bind():
        framebuffer = gpu.state.active_framebuffer_get()
        buffer = framebuffer.read_color(0, 0, width, height, 4, 0, "UBYTE")
        frame = np.array(buffer.to_list(), dtype=np.uint8).reshape(
            height,
            width,
            4,
        )

    frame = np.flipud(frame)
    stream["ffmpeg"].stdin.write(frame.tobytes())
    stream["ffmpeg"].stdin.flush()
    stream["frame_count"] += 1


def _resize_stream_capture(stream, width, height):
    _free_local_resources(stream)
    stream["ffmpeg"] = _start_ffmpeg(
        width,
        height,
        stream["host"],
        stream["rtp_port"],
        stream["rtcp_port"],
        stream["ssrc"],
        stream["fps"],
    )
    stream["offscreen"] = gpu.types.GPUOffScreen(width, height)
    stream["width"] = width
    stream["height"] = height


def _drain():
    manager = _get_state()
    scene = bpy.context.scene
    if scene is None:
        return 0.2

    reconcile_streams(scene)
    streams = manager["streams"]
    _tag_redraw()

    if not streams:
        manager["timer"] = None
        return None

    for camera_id, stream in streams.items():
        process = stream.get("ffmpeg")
        if (
            stream["status"] == "streaming"
            and process is not None
            and process.poll() is not None
        ):
            stream["status"] = "error"
            stream["last_error"] = "FFmpeg process exited unexpectedly."
            _free_local_resources(stream)
            if stream.get("server_started"):
                try:
                    request_stream_stop(
                        camera_id,
                        scene,
                        stream.get("server_url"),
                    )
                except Exception as exc:
                    print(
                        f"[Stream] Failed to release server stream for "
                        f"{camera_id}: {exc}"
                    )
                stream["server_started"] = False

    screen = bpy.context.screen
    if screen is None or not screen.is_animation_playing:
        return 0.1

    space, region = _find_view3d()
    if space is None:
        for stream in streams.values():
            if stream["status"] == "streaming":
                stream["last_error"] = "No 3D Viewport found"
        return 0.1

    scale = scene.render.resolution_percentage / 100.0
    width = max(1, int(scene.render.resolution_x * scale))
    height = max(1, int(scene.render.resolution_y * scale))

    for camera_id, stream in tuple(streams.items()):
        if stream["status"] != "streaming":
            continue
        try:
            if stream["width"] != width or stream["height"] != height:
                _resize_stream_capture(stream, width, height)
            process = stream["ffmpeg"]
            if process is None or process.poll() is not None:
                raise RuntimeError("FFmpeg process exited unexpectedly.")
            _capture_stream(stream, scene, space, region, width, height)
        except Exception as exc:
            stream["status"] = "error"
            stream["last_error"] = str(exc)
            _free_local_resources(stream)
            if stream.get("server_started"):
                try:
                    request_stream_stop(camera_id, scene, stream.get("server_url"))
                except Exception as stop_exc:
                    print(
                        f"[Stream] Failed to release server stream for "
                        f"{camera_id}: {stop_exc}"
                    )
                stream["server_started"] = False
            print(f"[Stream] Capture failed for {camera_id}: {exc}")

    _tag_redraw()
    return 0.04


def ensure_stream_timer():
    manager = _get_state()
    if manager["stopping"]:
        return
    timer = manager.get("timer")
    if timer is None or not bpy.app.timers.is_registered(timer):
        manager["timer"] = _drain
        bpy.app.timers.register(_drain, first_interval=0.1)


def start_stream():
    ensure_stream_timer()


def stop_stream(clear_selection=True):
    manager = _get_state()
    manager["stopping"] = True
    scene = bpy.context.scene
    if clear_selection and scene is not None:
        cameras = _current_scene_cameras(scene)
        for stream in manager["streams"].values():
            camera = stream.get("camera")
            if camera is not None:
                cameras[camera.name] = camera
        for camera in cameras.values():
            if camera.vse_instructor_stream_enabled:
                camera.vse_instructor_stream_enabled = False

    timer = manager.get("timer")
    if timer is not None and bpy.app.timers.is_registered(timer):
        bpy.app.timers.unregister(timer)
    manager["timer"] = None

    for camera_id in tuple(manager["streams"]):
        _stop_camera_stream(camera_id, scene)
    manager["streams"].clear()
    manager["stopping"] = False
    _tag_redraw()

import json
import os
import platform
import secrets
import shutil
import subprocess
import threading
import time
import urllib.request
from functools import lru_cache
from queue import Empty, Full, Queue

import bpy
import gpu
import numpy as np
from numpy.lib.stride_tricks import as_strided


STREAM_KEY = "vse_instructor_stream"
STREAM_RESOLUTIONS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "2160p": (3840, 2160),
}
STREAM_ENCODERS = {
    "software": {
        "codec": "libx264",
        "args": ["-preset", "ultrafast", "-tune", "zerolatency"],
    },
    "apple": {
        "codec": "h264_videotoolbox",
        "args": [],
    },
    "nvidia": {
        "codec": "h264_nvenc",
        "args": ["-preset", "p1", "-tune", "ll"],
    },
    "amd": {
        "codec": "h264_amf",
        "args": [],
    },
}
CAPTURE_METRIC_INTERVAL = 5.0
CAPTURE_METRIC_ALPHA = 0.05
STREAM_DEBUG = True
STREAM_RETRY_INITIAL_DELAY = 2.0
STREAM_RETRY_MAX_DELAY = 30.0
ns = bpy.app.driver_namespace


def _debug_log(message):
    if STREAM_DEBUG:
        print(message)


def _interleaved_view(buffer, height, width):
    expected_bytes = height * width * 4
    source = np.asarray(buffer)
    if source.dtype != np.uint8 or source.nbytes != expected_bytes:
        raise RuntimeError(
            f"Unexpected GPU buffer: dtype={source.dtype} "
            f"nbytes={source.nbytes} expected={expected_bytes}"
        )
    if not (source.flags.c_contiguous or source.flags.f_contiguous):
        raise RuntimeError(
            "Unexpected GPU buffer has neither C- nor Fortran-contiguous "
            f"storage: shape={source.shape} strides={source.strides}"
        )
    return as_strided(
        source,
        shape=(height, width, 4),
        strides=(width * 4, 4, 1),
        writeable=False,
    )


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
    else:
        metric_defaults = _new_capture_metrics() if STREAM_DEBUG else None
        for stream in state["streams"].values():
            stream.setdefault("capture_method", "auto")
            stream.setdefault(
                "capture_capabilities",
                {"buffer_memoryview": None},
            )
            stream.setdefault("capture_memoryview_error", None)
            stream.setdefault("dropped_frames", 0)
            stream.setdefault("frame_count", 0)
            stream.setdefault("encoder", "unknown")
            stream.setdefault("bitrate", 0)
            stream.setdefault("retry_at", 0.0)
            stream.setdefault("retry_delay", STREAM_RETRY_INITIAL_DELAY)
            if STREAM_DEBUG:
                metrics = stream.setdefault("metrics", metric_defaults.copy())
                for key, value in metric_defaults.items():
                    metrics.setdefault(key, value)
                if stream.get("metrics_lock") is None:
                    stream["metrics_lock"] = threading.Lock()
            else:
                stream.setdefault("metrics", {})
                if "metrics_lock" not in stream:
                    stream["metrics_lock"] = None
            if "capture_array" not in stream:
                width = stream.get("width")
                height = stream.get("height")
                stream["capture_array"] = (
                    np.empty((height, width, 4), dtype=np.uint8)
                    if width and height
                    else None
                )
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


@lru_cache(maxsize=4)
def _available_stream_encoders(ffmpeg_path):
    result = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-encoders"],
        check=True,
        capture_output=True,
        text=True,
    )
    available = set()
    for line in (result.stdout + result.stderr).splitlines():
        fields = line.split()
        if len(fields) >= 2:
            available.add(fields[1])
    return available


def _resolve_stream_encoder(preference, ffmpeg_path):
    if preference == "auto":
        if platform.system() == "Darwin":
            candidates = ("apple", "software")
        else:
            candidates = ("nvidia", "amd", "software")
    else:
        candidates = (preference,)

    available = _available_stream_encoders(ffmpeg_path)
    for encoder_name in candidates:
        encoder = STREAM_ENCODERS[encoder_name]
        if encoder["codec"] in available:
            return encoder_name

    requested = ", ".join(STREAM_ENCODERS[name]["codec"] for name in candidates)
    raise RuntimeError(
        f"FFmpeg does not provide the requested stream encoder ({requested})."
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


def _start_ffmpeg(
    width,
    height,
    host,
    rtp_port,
    rtcp_port,
    ssrc,
    fps,
    encoder_name,
    bitrate,
):
    ffmpeg_path = _resolve_ffmpeg_path()
    encoder = STREAM_ENCODERS[encoder_name]
    gop = max(1, round(fps * 0.5))
    command = [
        ffmpeg_path,
        "-f", "rawvideo",
        "-pix_fmt", "rgba",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "-",
        "-c:v", encoder["codec"],
        *encoder["args"],
        "-profile:v", "baseline",
        "-pix_fmt", "yuv420p",
        "-color_range", "tv",
        "-b:v", str(bitrate),
        "-maxrate", str(bitrate),
        "-bufsize", str(bitrate * 2),
        "-g", str(gop),
        "-keyint_min", str(gop),
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


def _stream_bitrate(width, height, fps):
    pixels = width * height
    if pixels <= 854 * 480:
        bitrate_at_30_fps = 2_000_000
    elif pixels <= 1280 * 720:
        bitrate_at_30_fps = 4_000_000
    elif pixels <= 1920 * 1080:
        bitrate_at_30_fps = 8_000_000
    elif pixels <= 2560 * 1440:
        bitrate_at_30_fps = 12_000_000
    else:
        bitrate_at_30_fps = 20_000_000

    return round(bitrate_at_30_fps * fps / 30)


def _get_stream_config(camera, scene):
    preset = camera.vse_instructor_stream_resolution
    if preset == "scene":
        scale = scene.render.resolution_percentage / 100.0
        width = max(1, int(scene.render.resolution_x * scale))
        height = max(1, int(scene.render.resolution_y * scale))
    else:
        try:
            width, height = STREAM_RESOLUTIONS[preset]
        except KeyError as exc:
            raise ValueError(f"Unsupported stream resolution: {preset}") from exc

    return (
        width,
        height,
        float(camera.vse_instructor_stream_fps),
        camera.vse_instructor_stream_encoder,
    )


def _new_capture_metrics():
    return {
        "capture_frames": 0,
        "capture_errors": 0,
        "dropped_frames": 0,
        "draw_time_ms": 0.0,
        "readback_time_ms": 0.0,
        "conversion_time_ms": 0.0,
        "bytes_time_ms": 0.0,
        "capture_time_ms": 0.0,
        "capture_max_ms": 0.0,
        "ffmpeg_write_time_ms": 0.0,
        "ffmpeg_write_max_ms": 0.0,
        "ffmpeg_write_samples": 0,
        "last_report_time": time.monotonic(),
        "last_report_frames": 0,
        "last_report_drops": 0,
    }


def _update_capture_metrics(stream, timings):
    if not STREAM_DEBUG:
        return

    metrics = stream["metrics"]
    with stream["metrics_lock"]:
        _update_capture_metrics_locked(metrics, timings)


def _update_capture_metrics_locked(metrics, timings):
    sample_count = metrics["capture_frames"]
    for key, value in timings.items():
        metric_key = f"{key}_time_ms"
        previous = metrics[metric_key]
        if sample_count == 0:
            metrics[metric_key] = value
        else:
            metrics[metric_key] = (
                previous * (1.0 - CAPTURE_METRIC_ALPHA)
                + value * CAPTURE_METRIC_ALPHA
            )
    metrics["capture_max_ms"] = max(
        metrics["capture_max_ms"],
        timings["capture"],
    )
    metrics["capture_frames"] += 1


def _record_ffmpeg_write_time(stream, elapsed_ms):
    if not STREAM_DEBUG:
        return

    metrics = stream["metrics"]
    with stream["metrics_lock"]:
        samples = metrics["ffmpeg_write_samples"]
        if samples == 0:
            metrics["ffmpeg_write_time_ms"] = elapsed_ms
        else:
            metrics["ffmpeg_write_time_ms"] = (
                metrics["ffmpeg_write_time_ms"] * (1.0 - CAPTURE_METRIC_ALPHA)
                + elapsed_ms * CAPTURE_METRIC_ALPHA
            )
        metrics["ffmpeg_write_max_ms"] = max(
            metrics["ffmpeg_write_max_ms"],
            elapsed_ms,
        )
        metrics["ffmpeg_write_samples"] += 1


def _report_capture_metrics(stream, now=None):
    if not STREAM_DEBUG:
        return

    metrics = stream["metrics"]
    now = now or time.monotonic()
    with stream["metrics_lock"]:
        elapsed = now - metrics["last_report_time"]
        if elapsed < CAPTURE_METRIC_INTERVAL:
            return

        frame_delta = metrics["capture_frames"] - metrics["last_report_frames"]
        dropped_delta = stream["dropped_frames"] - metrics["last_report_drops"]
        total_attempts = frame_delta + dropped_delta
        drop_rate = dropped_delta / max(total_attempts, 1) * 100.0
        capture_fps = frame_delta / elapsed
        metrics_snapshot = metrics.copy()
        metrics["last_report_time"] = now
        metrics["last_report_frames"] = metrics["capture_frames"]
        metrics["last_report_drops"] = stream["dropped_frames"]
        metrics["capture_max_ms"] = 0.0
        metrics["ffmpeg_write_max_ms"] = 0.0
        metrics["ffmpeg_write_samples"] = 0

    queue_depth = stream["frame_queue"].qsize() if stream["frame_queue"] else 0
    budget_ms = 1000.0 / stream["fps"]
    _debug_log(
        f"[Stream Metrics] camera={stream['camera'].name} "
        f"capture_fps={capture_fps:.1f} "
        f"errors={metrics_snapshot['capture_errors']} "
        f"dropped={stream['dropped_frames']} drop_rate={drop_rate:.1f}% "
        f"queue={queue_depth} method={stream['capture_method']} "
        f"draw={metrics_snapshot['draw_time_ms']:.2f}ms "
        f"readback={metrics_snapshot['readback_time_ms']:.2f}ms "
        f"conversion={metrics_snapshot['conversion_time_ms']:.2f}ms "
        f"bytes={metrics_snapshot['bytes_time_ms']:.2f}ms "
        f"capture_avg={metrics_snapshot['capture_time_ms']:.2f}ms "
        f"capture_max={metrics_snapshot['capture_max_ms']:.2f}ms "
        f"ffmpeg_write_avg={metrics_snapshot['ffmpeg_write_time_ms']:.2f}ms "
        f"ffmpeg_write_max={metrics_snapshot['ffmpeg_write_max_ms']:.2f}ms "
        f"ffmpeg_writes={metrics_snapshot['ffmpeg_write_samples']} "
        f"budget={budget_ms:.2f}ms "
        f"encoder={stream['encoder']} bitrate={stream['bitrate'] // 1000}kbps"
    )


def _buffer_to_numpy(buffer, stream, height, width):
    expected_shape = (height, width, 4)
    capture_array = stream.get("capture_array")
    if capture_array is None or capture_array.shape != expected_shape:
        capture_array = np.empty(expected_shape, dtype=np.uint8)
        stream["capture_array"] = capture_array

    np.copyto(capture_array, _interleaved_view(buffer, height, width)[::-1])
    method = "strided_reinterpret"

    if stream.get("capture_method") != method:
        stream["capture_method"] = method
        _debug_log(
            f"[Stream] {stream['camera'].name} capture method: {method}"
        )
    return capture_array


def _push_latest_frame(frame_queue, frame_bytes):
    dropped_frame = False
    while True:
        try:
            frame_queue.put_nowait(frame_bytes)
            return dropped_frame
        except Full:
            try:
                frame_queue.get_nowait()
                dropped_frame = True
            except Empty:
                continue


def _free_local_resources(stream):
    stop_event = stream.get("writer_stop")
    if stop_event is not None:
        stop_event.set()

    process = stream.get("ffmpeg")
    if process is not None:
        writer = stream.get("writer_thread")
        writer_active = writer is not None and writer.is_alive()

        def close_stdin():
            if process.stdin and not process.stdin.closed:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError, ValueError) as exc:
                    print(f"[Stream] Could not close FFmpeg input: {exc}")

        try:
            if not writer_active:
                close_stdin()
            if process.poll() is None:
                if writer_active:
                    process.terminate()
                    process.wait(timeout=1.5)
                else:
                    try:
                        process.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=1.5)
        except Exception:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=1.5)
            except Exception as exc:
                print(f"[Stream] Could not fully stop FFmpeg: {exc}")

        if writer_active:
            writer.join(timeout=1.5)
        close_stdin()
        stream["ffmpeg"] = None
        stream["writer_thread"] = None
        stream["writer_stop"] = None
        stream["frame_queue"] = None

    offscreen = stream.get("offscreen")
    if offscreen is not None:
        try:
            offscreen.free()
        except Exception as exc:
            print(f"[Stream] Could not free GPUOffScreen: {exc}")
        stream["offscreen"] = None
    stream["capture_array"] = None


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
    _debug_log(f"[Stream] Stopped camera {camera_id}")


def _schedule_stream_retry(stream):
    retry_delay = stream["retry_delay"]
    stream["retry_at"] = time.monotonic() + retry_delay
    stream["retry_delay"] = min(
        retry_delay * 2.0,
        STREAM_RETRY_MAX_DELAY,
    )
    return retry_delay


def _retry_timer_interval(streams, now=None):
    now = time.monotonic() if now is None else now
    retry_deadlines = [
        stream.get("retry_at", now)
        for stream in streams.values()
        if stream["status"] == "error"
    ]
    if not retry_deadlines:
        return 0.1
    return max(0.1, min(retry_deadlines) - now)


def _start_camera_stream(camera_id, camera, scene):
    manager = _get_state()
    stream = manager["streams"].get(camera_id)
    if stream is None:
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
            "bitrate": None,
            "encoder_preference": None,
            "encoder": None,
            "width": None,
            "height": None,
            "resolution": None,
            "capture_method": "auto",
            "capture_capabilities": {"buffer_memoryview": None},
            "capture_memoryview_error": None,
            "capture_array": None,
            "metrics": _new_capture_metrics() if STREAM_DEBUG else {},
            "metrics_lock": threading.Lock() if STREAM_DEBUG else None,
            "next_capture_time": time.monotonic(),
            "frame_queue": None,
            "writer_stop": None,
            "writer_thread": None,
            "writer_error": None,
            "dropped_frames": 0,
            "retry_at": 0.0,
            "retry_delay": STREAM_RETRY_INITIAL_DELAY,
        }
        manager["streams"][camera_id] = stream

    stream["camera"] = camera
    stream["status"] = "starting"
    stream["last_error"] = ""
    stream["retry_at"] = 0.0
    stream["started"] = False
    stream["writer_error"] = None
    stream["ffmpeg"] = None
    stream["offscreen"] = None
    stream["frame_queue"] = None
    stream["writer_stop"] = None
    stream["writer_thread"] = None
    stream["server_started"] = False
    stream["server_url"] = None
    stream["host"] = None

    try:
        width, height, fps, encoder_preference = _get_stream_config(camera, scene)
        stream["width"] = width
        stream["height"] = height
        stream["fps"] = fps
        stream["encoder_preference"] = encoder_preference
        stream["resolution"] = camera.vse_instructor_stream_resolution

        base_url = _server_url(scene)
        stream["server_url"] = base_url
        stream["host"], stream["rtp_port"], stream["rtcp_port"] = request_stream_start(
            camera_id,
            stream["ssrc"],
            scene,
        )
        stream["server_started"] = True

        ffmpeg_path = _resolve_ffmpeg_path()
        encoder_name = _resolve_stream_encoder(encoder_preference, ffmpeg_path)
        bitrate = _stream_bitrate(width, height, fps)
        stream["fps"] = fps
        stream["bitrate"] = bitrate
        stream["encoder"] = encoder_name

        stream["ffmpeg"] = _start_ffmpeg(
            width,
            height,
            stream["host"],
            stream["rtp_port"],
            stream["rtcp_port"],
            stream["ssrc"],
            fps,
            encoder_name,
            bitrate,
        )
        stream["offscreen"] = gpu.types.GPUOffScreen(width, height)
        stream["width"] = width
        stream["height"] = height
        stream["capture_array"] = np.empty(
            (height, width, 4),
            dtype=np.uint8,
        )
        stream["frame_queue"] = Queue(maxsize=1)
        stream["writer_stop"] = threading.Event()
        stream["writer_thread"] = threading.Thread(
            target=_write_stream_frames,
            args=(stream,),
            name=f"StreamWriter-{camera_id}",
            daemon=True,
        )
        stream["writer_thread"].start()
        stream["started"] = True
        stream["status"] = "streaming"
        stream["retry_at"] = 0.0
        stream["retry_delay"] = STREAM_RETRY_INITIAL_DELAY
        _debug_log(
            f"[Stream] Started {camera_id}: {width}x{height}@{fps:g} "
            f"{encoder_name} ({encoder_preference}) "
            f"{bitrate / 1_000_000:.1f}Mbps "
            f"RTP={stream['rtp_port']} RTCP={stream['rtcp_port']} "
            f"SSRC={stream['ssrc']} capture=auto zero_copy=false "
            f"({base_url})"
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
        retry_delay = _schedule_stream_retry(stream)
        print(
            f"[Stream] Failed to start {camera_id}: {exc}. "
            f"Retrying in {retry_delay:.1f}s."
        )


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
            width, height, fps, encoder_preference = _get_stream_config(camera, scene)
            stream = streams[camera_id]
            configuration_changed = (
                stream.get("width") != width
                or stream.get("height") != height
                or stream.get("fps") != fps
                or stream.get("encoder_preference") != encoder_preference
                or stream.get("resolution") != camera.vse_instructor_stream_resolution
            )
            if stream["status"] == "streaming" and configuration_changed:
                _stop_camera_stream(camera_id, scene)
                _start_camera_stream(camera_id, camera, scene)
            elif (
                stream["status"] == "error"
                and time.monotonic() >= stream.get("retry_at", 0.0)
            ):
                _start_camera_stream(camera_id, camera, scene)

    for camera_id, camera in cameras.items():
        if camera.vse_instructor_stream_enabled and camera_id not in streams:
            _start_camera_stream(camera_id, camera, scene)


def _capture_stream(stream, scene, space, region, width, height):
    if STREAM_DEBUG:
        capture_start = time.perf_counter()
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
        if STREAM_DEBUG:
            draw_start = time.perf_counter()
        stream["offscreen"].draw_view3d(
            scene,
            bpy.context.view_layer,
            space,
            region,
            view_matrix,
            projection_matrix,
            do_color_management=True,
        )
        if STREAM_DEBUG:
            draw_end = time.perf_counter()
    finally:
        overlay.show_overlays = show_overlays

    with stream["offscreen"].bind():
        framebuffer = gpu.state.active_framebuffer_get()
        if STREAM_DEBUG:
            readback_start = time.perf_counter()
        buffer = framebuffer.read_color(0, 0, width, height, 4, 0, "UBYTE")
        if STREAM_DEBUG:
            readback_end = time.perf_counter()
            conversion_start = time.perf_counter()
        frame = _buffer_to_numpy(buffer, stream, height, width)
        if STREAM_DEBUG:
            conversion_end = time.perf_counter()

    frame_queue = stream["frame_queue"]
    if STREAM_DEBUG:
        bytes_start = time.perf_counter()
    frame_bytes = frame.tobytes()
    if STREAM_DEBUG:
        bytes_end = time.perf_counter()
    if _push_latest_frame(frame_queue, frame_bytes):
        stream["dropped_frames"] += 1
    stream["frame_count"] += 1
    if STREAM_DEBUG:
        capture_end = time.perf_counter()
        _update_capture_metrics(
            stream,
            {
                "draw": (draw_end - draw_start) * 1000.0,
                "readback": (readback_end - readback_start) * 1000.0,
                "conversion": (conversion_end - conversion_start) * 1000.0,
                "bytes": (bytes_end - bytes_start) * 1000.0,
                "capture": (capture_end - capture_start) * 1000.0,
            },
        )


def _write_stream_frames(stream):
    process = stream["ffmpeg"]
    frame_queue = stream["frame_queue"]
    stop_event = stream["writer_stop"]
    while not stop_event.is_set():
        try:
            frame = frame_queue.get(timeout=0.1)
        except Empty:
            continue
        try:
            if process.poll() is not None:
                raise RuntimeError("FFmpeg process exited unexpectedly.")
            if STREAM_DEBUG:
                write_start = time.perf_counter()
            process.stdin.write(frame)
            if STREAM_DEBUG:
                write_end = time.perf_counter()
                _record_ffmpeg_write_time(
                    stream,
                    (write_end - write_start) * 1000.0,
                )
        except Exception as exc:
            stream["writer_error"] = str(exc)
            stop_event.set()
            return


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

    for camera_id, stream in tuple(streams.items()):
        process = stream.get("ffmpeg")
        if (
            stream["status"] == "streaming"
            and stream.get("writer_error")
        ):
            stream["status"] = "error"
            stream["last_error"] = stream["writer_error"]
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
            retry_delay = _schedule_stream_retry(stream)
            print(
                f"[Stream] Stream failed for {camera_id}: "
                f"{stream['last_error']}. Retrying in {retry_delay:.1f}s."
            )
        elif (
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
            retry_delay = _schedule_stream_retry(stream)
            print(
                f"[Stream] Stream failed for {camera_id}: "
                f"{stream['last_error']}. Retrying in {retry_delay:.1f}s."
            )

    if STREAM_DEBUG:
        metrics_now = time.monotonic()
        for stream in streams.values():
            if stream["status"] == "streaming":
                _report_capture_metrics(stream, metrics_now)

    if not any(stream["status"] == "streaming" for stream in streams.values()):
        return _retry_timer_interval(streams)

    screen = bpy.context.screen
    if screen is None or not screen.is_animation_playing:
        return 0.1

    space, region = _find_view3d()
    if space is None:
        for stream in streams.values():
            if stream["status"] == "streaming":
                stream["last_error"] = "No 3D Viewport found"
        return 0.1

    now = time.monotonic()
    for camera_id, stream in tuple(streams.items()):
        if stream["status"] != "streaming":
            continue
        try:
            if now < stream["next_capture_time"]:
                continue
            process = stream["ffmpeg"]
            if process is None or process.poll() is not None:
                raise RuntimeError("FFmpeg process exited unexpectedly.")
            _capture_stream(
                stream,
                scene,
                space,
                region,
                stream["width"],
                stream["height"],
            )
            interval = 1.0 / stream["fps"]
            next_capture_time = stream["next_capture_time"] + interval
            now = time.monotonic()
            if next_capture_time <= now:
                missed_intervals = int((now - next_capture_time) / interval) + 1
                next_capture_time += missed_intervals * interval
            stream["next_capture_time"] = next_capture_time
        except Exception as exc:
            if STREAM_DEBUG:
                with stream["metrics_lock"]:
                    stream["metrics"]["capture_errors"] += 1
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
            retry_delay = _schedule_stream_retry(stream)
            print(
                f"[Stream] Capture failed for {camera_id}: {exc}. "
                f"Retrying in {retry_delay:.1f}s."
            )

    _tag_redraw()
    active_streams = [
        stream
        for stream in streams.values()
        if stream["status"] == "streaming"
    ]
    if not active_streams:
        return _retry_timer_interval(streams)
    now = time.monotonic()
    return max(
        0.001,
        min(stream["next_capture_time"] - now for stream in active_streams),
    )


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

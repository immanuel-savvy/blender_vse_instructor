Blender VSE Instructor - Add-on scaffold

Structure included in this document. Copy each file into the matching path and install the add-on in Blender (Edit -> Preferences -> Add-ons -> Install...).

This add-on includes operators for building and rendering VSE sequences, backend job polling, and a multi-camera viewport streaming panel.

The add-on can:
- load sequence instruction JSON,
- build the VSE timeline,
- render final output,
- poll for remote jobs,
- stream selected scene cameras concurrently to a configured host and port.

For viewport streaming, select cameras directly in the panel. Each selected
camera gets its own FFmpeg process and GPU offscreen buffer; streams remain
allocated while playback is paused and send frames while the timeline plays.
Set each camera's stream resolution (480p through 2160p, or the scene dimensions)
and capture FPS independently of Blender's render settings. Changing either
setting restarts that camera's stream with the new configuration. If FFmpeg
cannot keep up, pending frames are replaced with the newest captured frame to
avoid building an encoder backlog. Encoders use a resolution- and FPS-scaled
bitrate target and a keyframe interval of approximately half a second. Choose
Automatic, Software, Apple VideoToolbox, NVIDIA NVENC, or AMD
AMF per camera. Automatic prefers VideoToolbox on macOS and NVENC/AMF on other
platforms when FFmpeg lists one as available, then falls back to libx264.
Explicit choices fail with an encoder availability error rather than silently
switching. Set `STREAM_DEBUG = True` in `core/stream_capture.py` to enable
capture timings, FFmpeg write/back-pressure timings, periodic metric logs, and
the panel's capture diagnostics. It defaults to `False` so production avoids
diagnostic timing calls, locks, and reporting. Capture tries Blender's GPU
buffer memoryview API on the first
frame and falls back to a preallocated NumPy buffer populated via `to_list()`
when memoryview is unavailable. The selected readback path is cached per
stream. With debug enabled, per-stage capture timings, measured capture rate,
error count, queue drops, and frame-budget usage are reported every five
seconds; FFmpeg stdin write time is measured separately to expose pipe
back-pressure. The panel also shows the selected method, capture and writer
average times, and drop count. Buffer memoryview capability is detected once
per stream.
GPU readback still crosses into host memory; the pipeline does not provide
zero-copy GPU-to-encoder transfer. The Stop All button clears the camera
selections and releases all local stream resources. Startup and runtime stream
failures retry with exponential backoff (2, 4, 8, 16, then 30 seconds); the
delay resets after a successful start.

The streaming backend must implement `POST /streams/start` with a JSON body
containing `cameraId` and `ssrc`, and return `rtpPort` and `rtcpPort`. The
addon sends `POST /streams/stop` with `cameraId` when a camera is deselected.
The addon does not implement or modify the backend.

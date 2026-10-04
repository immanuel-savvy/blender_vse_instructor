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
The Stop All button clears the camera selections and releases all local stream
resources.

The streaming backend must implement `POST /streams/start` with a JSON body
containing `cameraId` and `ssrc`, and return `rtpPort` and `rtcpPort`. The
addon sends `POST /streams/stop` with `cameraId` when a camera is deselected.
The addon does not implement or modify the backend.

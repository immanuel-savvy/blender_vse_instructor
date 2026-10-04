Blender VSE Instructor - Add-on scaffold

Structure included in this document. Copy each file into the matching path and install the add-on in Blender (Edit -> Preferences -> Add-ons -> Install...).

This scaffold includes basic operator stubs and a working register/unregister so Blender can load the add-on. It also includes a viewport streaming panel for sending the active 3D view over FFmpeg/RTP to a media server.

The add-on can:
- load sequence instruction JSON,
- build the VSE timeline,
- render final output,
- poll for remote jobs,
- stream the viewport to a configured host and port.

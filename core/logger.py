import bpy

class Logger:
    _buffer = []

    @classmethod
    def _push_ui(cls, line):
        scene = bpy.context.scene if bpy.context else None
        if not scene:
            return

        props = getattr(scene, "vse_instructor_server_props", None)
        if not props:
            return

        item = props.logs.add()
        item.text = line

        # keep log size sane
        if len(props.logs) > 500:
            props.logs.remove(0)

        # keep the (future) UIList scrolled to the newest entry
        if hasattr(props, "log_index"):
            props.log_index = len(props.logs) - 1

        # also update "Last Message"
        props.last_message = line

    @classmethod
    def info(cls, msg: str):
        line = f"[INFO] {msg}"
        cls._buffer.append(line)
        print(line)
        cls._push_ui(line)

    @classmethod
    def warning(cls, msg: str):
        line = f"[WARNING] {msg}"
        cls._buffer.append(line)
        print(line)
        cls._push_ui(line)

    @classmethod
    def error(cls, msg: str):
        line = f"[ERROR] {msg}"
        cls._buffer.append(line)
        print(line)
        cls._push_ui(line)
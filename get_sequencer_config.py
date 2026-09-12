import bpy
import json
from pathlib import Path


def get_strip_type(strip):
    """
    Normalize Blender's strip types into our config terminology.
    """
    mapping = {
        "MOVIE": "video",
        "IMAGE": "image",
        "SOUND": "audio",
        "SCENE": "scene",
        "META": "meta",
        "COLOR": "color",
        "TEXT": "text",
        "MASK": "mask",
        "ADJUSTMENT": "adjustment",
    }

    return mapping.get(strip.type, strip.type.lower())


def get_strip_path(strip):
    """
    Return the source filepath where applicable.
    """
    if hasattr(strip, "filepath"):
        return strip.filepath

    return None


def get_strip_config(strip):
    """
    Convert one Blender VSE strip into a serializable config object.
    """

    data = {
        "_id": str(strip.name),

        "name": strip.name,

        "type": get_strip_type(strip),

        "channel": strip.channel,

        "frame_start": strip.frame_final_start,

        "frame_end": strip.frame_final_end,

        "duration": strip.frame_final_duration,

        "source_frame_start": getattr(strip, "frame_start", None),

        "frame_offset_start": getattr(strip, "frame_offset_start", 0),

        "frame_offset_end": getattr(strip, "frame_offset_end", 0),

        "mute": getattr(strip, "mute", False),

        "lock": getattr(strip, "lock", False),

        "filepath": get_strip_path(strip),
    }

    # --------------------------------------------------------
    # Movie / image specific
    # --------------------------------------------------------

    if strip.type in {"MOVIE", "IMAGE"}:
        data["filepath"] = strip.filepath

        if hasattr(strip, "elements"):
            data["elements"] = [
                {
                    "filename": element.filename
                }
                for element in strip.elements
            ]

    # --------------------------------------------------------
    # Sound specific
    # --------------------------------------------------------

    if strip.type == "SOUND":
        data["filepath"] = strip.sound.filepath if strip.sound else None

        data["volume"] = getattr(strip, "volume", 1.0)

        data["pan"] = getattr(strip, "pan", 0.0)

    # --------------------------------------------------------
    # Scene strip
    # --------------------------------------------------------

    if strip.type == "SCENE":
        data["scene"] = strip.scene.name if strip.scene else None

    # --------------------------------------------------------
    # Text strip
    # --------------------------------------------------------

    if strip.type == "TEXT":
        data["text"] = strip.text

        data["font"] = getattr(strip, "font", None)

        data["font_size"] = getattr(strip, "font_size", None)

        data["align_x"] = getattr(strip, "align_x", None)

        data["align_y"] = getattr(strip, "align_y", None)

    # --------------------------------------------------------
    # Transform
    # --------------------------------------------------------

    if hasattr(strip, "transform") and strip.transform:
        transform = strip.transform

        data["transform"] = {
            "offset_x": transform.offset_x,
            "offset_y": transform.offset_y,
            "scale_x": transform.scale_x,
            "scale_y": transform.scale_y,
            "rotation": transform.rotation,
        }

    return data


def generate_sequencer_config(scene=None):
    """
    Generate a complete JSON-compatible representation
    of the current Blender VSE.
    """

    if scene is None:
        scene = bpy.context.scene

    sequence_editor = scene.sequence_editor

    if sequence_editor is None:
        return {
            "name": scene.name,
            "fps": scene.render.fps,
            "fps_base": scene.render.fps_base,
            "frame_start": scene.frame_start,
            "frame_end": scene.frame_end,
            "tracks": [],
        }

    strips = list(sequence_editor.sequences_all)

    # --------------------------------------------------------
    # Group strips by channel
    # --------------------------------------------------------

    channels = {}

    for strip in strips:
        channels.setdefault(strip.channel, []).append(strip)

    tracks = []

    for channel in sorted(channels):

        channel_strips = sorted(
            channels[channel],
            key=lambda strip: (
                strip.frame_final_start,
                strip.name,
            ),
        )

        track = {
            "_id": f"channel_{channel}",

            "name": f"channel_{channel}",

            "channel": channel,

            "clips": [
                get_strip_config(strip)
                for strip in channel_strips
            ],
        }

        tracks.append(track)

    # --------------------------------------------------------
    # Final config
    # --------------------------------------------------------

    config = {
        "name": scene.name,

        "fps": scene.render.fps,

        "fps_base": scene.render.fps_base,

        "resolution": {
            "x": scene.render.resolution_x,
            "y": scene.render.resolution_y,
            "percentage": scene.render.resolution_percentage,
        },

        "frame_start": scene.frame_start,

        "frame_end": scene.frame_end,

        "duration": scene.frame_end - scene.frame_start + 1,

        "tracks": tracks,
    }

    return config


def save_sequencer_config(filepath, scene=None):
    """
    Generate and save the VSE config as JSON.
    """

    config = generate_sequencer_config(scene)

    filepath = Path(filepath)

    filepath.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with filepath.open("w", encoding="utf-8") as file:
        json.dump(
            config,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return config


# ============================================================
# RUN
# ============================================================

config = save_sequencer_config(
    "/tmp/sequencer_config.json"
)

print(
    json.dumps(
        config,
        indent=2,
        ensure_ascii=False,
    )
)
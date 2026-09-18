import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bpy


EXPORT_ROOT = Path.home() / "VSEInstructorCache" / "sequencer_exports"
EXPORT_ROOT.mkdir(parents=True, exist_ok=True)


def _strip_to_dict(strip):
    """Serialize one VSE strip into a plain dict."""
    data = {
        "name": strip.name,
        "type": strip.type,
        "channel": strip.channel,
        "frame_start": strip.frame_start,
        "frame_final_start": strip.frame_final_start,
        "frame_final_end": strip.frame_final_end,
        "frame_final_duration": strip.frame_final_duration,
        "frame_offset_start": getattr(strip, "frame_offset_start", 0),
        "frame_offset_end": getattr(strip, "frame_offset_end", 0),
        "mute": strip.mute,
        "lock": strip.lock,
        "blend_type": getattr(strip, "blend_type", None),
        "blend_alpha": getattr(strip, "blend_alpha", 1.0),
    }

    for key in (
        "asset_id",
        "instance_id",
        "resolved",
        "preferred_type",
        "description",
        "strip_role",
        "editorial_start",
        "editorial_clip",
        "fit_scale",
        "has_declared_transforms",
    ):
        if key in strip:
            try:
                data[key] = strip[key]
            except Exception:
                pass

    if hasattr(strip, "filepath") and strip.filepath:
        data["filepath"] = strip.filepath
    if hasattr(strip, "sound") and strip.sound:
        data["sound_filepath"] = getattr(strip.sound, "filepath", None)

    if strip.type == "TEXT":
        data["text"] = getattr(strip, "text", "")
        data["font_size"] = getattr(strip, "font_size", None)

    if hasattr(strip, "transform"):
        transform = strip.transform
        data["transform"] = {
            "offset_x": transform.offset_x,
            "offset_y": transform.offset_y,
            "scale_x": transform.scale_x,
            "scale_y": transform.scale_y,
            "rotation": transform.rotation,
        }

    return data


def export_sequencer_to_json(timeline_id: str, extra: Optional[dict] = None) -> Path:
    """Write the current sequencer state to a timeline-specific JSON file."""
    scene = bpy.context.scene
    sequencer = scene.sequence_editor

    if sequencer is None:
        raise RuntimeError("No sequence editor present.")

    strips = [
        _strip_to_dict(strip)
        for strip in sequencer.sequences_all
        if not strip.name.startswith("_probe")
    ]
    strips.sort(key=lambda item: (
        item["channel"],
        item["frame_final_start"],
        item["name"],
    ))

    payload = {
        "timeline_id": timeline_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "fps": scene.render.fps,
        "fps_base": scene.render.fps_base,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "resolution_x": scene.render.resolution_x,
        "resolution_y": scene.render.resolution_y,
        "strip_count": len(strips),
        "strips": strips,
    }

    if extra:
        payload["extra"] = extra

    out_path = EXPORT_ROOT / f"{timeline_id}.json"
    out_path.write_text(
        json.dumps(payload, indent=2, default=str),
        encoding="utf-8",
    )
    return out_path


class SEQUENCER_OT_export_json(bpy.types.Operator):
    """Export the current VSE sequencer to a JSON config file."""

    bl_idname = "sequencer.export_json"
    bl_label = "Export Sequencer JSON"
    bl_options = {"REGISTER"}

    timeline_id: bpy.props.StringProperty(
        name="Timeline ID",
        description="Used as the output filename (without extension)",
        default="",
    )

    def execute(self, context):
        timeline_id = (self.timeline_id or "").strip()
        if not timeline_id:
            timeline_id = context.scene.get("timeline_id") or "unknown_timeline"

        try:
            path = export_sequencer_to_json(timeline_id)
            self.report({"INFO"}, f"Sequencer exported to {path}")
            print(f"[EXPORT] Sequencer JSON written to {path}")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
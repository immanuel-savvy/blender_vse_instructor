from dataclasses import dataclass


@dataclass
class TimelineObject:

    _id: str

    start: int = 0
    end: int = 0
    duration: int = 0

    @property
    def center(self):
        return self.start + (self.duration // 2)


class TimelineResolver:

    def __init__(self, sequence, fps):

        self.sequence = sequence
        self.fps = fps

        #
        # Timeline graph
        #

        self.scene = TimelineObject("scene")

        self.tracks = {}

        self.track_order = []

        self.clips = {}

        self.clip_order = []

    # ----------------------------------------------------------

    def ms_to_frames(self, ms):
        return round(ms * self.fps / 1000)

    # ----------------------------------------------------------

    def frames_to_ms(self, frames):
        return round(frames * 1000 / self.fps)

    # ----------------------------------------------------------

    def resolve_ms(self, value):

        if value is None:
            return 0

        if isinstance(value, (int, float)):
            return int(value)

        t = value["type"]

        if t == "milliseconds":
            return int(value["value"])

        if t == "seconds":
            return int(float(value["value"]) * 1000)

        if t == "reference":
            return self.resolve_reference(
                value["value"]
            )

        if t == "expression":
            return self.resolve_expression(
                value["value"]
            )

        raise Exception(f"Unknown timing type '{t}'")

    # ----------------------------------------------------------

    def resolve_frame(self, value):

        return self.ms_to_frames(
            self.resolve_ms(value)
        )

    # ----------------------------------------------------------

    def register_track(self, track_id):

        if track_id in self.tracks:
            return

        self.tracks[track_id] = TimelineObject(track_id)

        self.track_order.append(track_id)

    # ----------------------------------------------------------

    def register_clip(
        self,
        clip,
        track_id
    ):

      start = self.resolve_ms(
          clip["start_ms"]
      )

      duration = self.resolve_ms(
          clip.get(
              "duration_ms",
              {
                  "type": "milliseconds",
                  "value": 5000
              }
          )
      )


      obj = TimelineObject(
          clip["_id"],
          start=start,
          end=start+duration,
          duration=duration
      )


      self.clips[
          clip["_id"]
      ] = obj


      self.clip_order.append(
          clip["_id"]
      )
      self.scene.start = min(
          clip.start
          for clip in self.clips.values()
      )
      self.scene.end = max(
          clip.end
          for clip in self.clips.values()
      )

      self.scene.duration = (
          self.scene.end -
          self.scene.start
      )

      track = self.tracks[track_id]

      track.start = (
          start
          if track.duration == 0
          else min(track.start, start)
      )

      track.end = max(track.end, start + duration)
      track.duration = track.end - track.start
    # ----------------------------------------------------------

    def resolve_reference(self, ref):

        #
        # scene:end
        # track:first:end
        # clip:last:start
        # clip:<uuid>:duration
        #

        tokens = ref.split(":")

        if len(tokens) == 2:

            structure, prop = tokens

            selector = None

        elif len(tokens) == 3:

            structure, selector, prop = tokens

        else:

            raise Exception(
                f"Invalid reference '{ref}'"
            )

        obj = self._resolve_object(
            structure,
            selector
        )

        return getattr(obj, prop)

    # ----------------------------------------------------------

    def _resolve_object(
        self,
        structure,
        selector
    ):

        if structure == "scene":

            return self.scene

        if structure == "track":

            return self._resolve_track(selector)

        if structure == "clip":

            return self._resolve_clip(selector)

        raise Exception(
            f"Unknown structure '{structure}'"
        )

    # ----------------------------------------------------------

    def _resolve_track(self, selector):

        if selector is None:

            raise Exception(
                "Track selector required."
            )

        if selector == "first":

            return self.tracks[
                self.track_order[0]
            ]

        if selector == "last":

            return self.tracks[
                self.track_order[-1]
            ]

        return self.tracks[selector]

    # ----------------------------------------------------------

    def _resolve_clip(self, selector):

        if selector is None:

            raise Exception(
                "Clip selector required."
            )

        if selector == "first":

            return self.clips[
                self.clip_order[0]
            ]

        if selector == "last":

            return self.clips[
                self.clip_order[-1]
            ]

        return self.clips[selector]

    # ----------------------------------------------------------

    def resolve_expression(self, expression):

        expr = expression

        #
        # Replace every timeline reference
        #

        import re

        pattern = (
            r"(scene:[a-zA-Z_]+"
            r"|track:[^:\s]+:[a-zA-Z_]+"
            r"|clip:[^:\s]+:[a-zA-Z_]+)"
        )

        refs = sorted(
            set(re.findall(pattern, expr)),
            key=len,
            reverse=True
        )

        for ref in refs:

            expr = expr.replace(
                ref,
                str(self.resolve_reference(ref))
            )

        return int(
            eval(
                expr,
                {"__builtins__": {}}
            )
        )
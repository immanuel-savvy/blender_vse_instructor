from dataclasses import dataclass, field
import ast
import operator
import re


# ============================================================
#
# CONSTANTS
#
# ============================================================

VALID_PROPERTIES = {
    "start",
    "end",
    "duration",
    "center",
}

SCENE_DIRECT_PROPERTIES = {
    "start",
}

REFERENCE_PATTERN = re.compile(
    r"(scene(?::[^:\s]+)?(?::[a-zA-Z_][a-zA-Z0-9_]*)"
    r"|track:[^:\s]+:[a-zA-Z_][a-zA-Z0-9_]*"
    r"|clip:[^:\s]+:[a-zA-Z_][a-zA-Z0-9_]*)"
)


# ============================================================
#
# TIMELINE POSITIONING
#
# ============================================================

#
# Blender timelines conventionally begin at frame 1.
#
# This prevents the first strip from occupying frame 0 and gives
# the VSE a deterministic initial insertion position.
#

TIMELINE_START_FRAME = 1


#
# Leave one frame between editorial scenes.
#
# This is deliberately expressed in frames rather than milliseconds
# because the VSE ultimately operates in frames.
#

SCENE_GAP_FRAMES = 1


#
# A percentage-only scene has no mathematically unique duration.
#

UNDERDETERMINED_SCENE_DURATION_MS = 1000


#
# Fixed point settings.
#

PERCENTAGE_MAX_ITERATIONS = 200

PERCENTAGE_TOLERANCE_MS = 1


# ============================================================
#
# ERRORS
#
# ============================================================

class TimelineResolutionError(Exception):
    pass


class TimelineCircularDependencyError(
    TimelineResolutionError
):
    pass


class TimelinePercentageResolutionError(
    TimelineResolutionError
):
    pass


# ============================================================
#
# TIMELINE OBJECT
#
# ============================================================

@dataclass
class TimelineObject:

    _id: str

    # Source objects
    clip: dict | None = None
    track_id: str | None = None

    # --------------------------------------------------------
    # Resolved timeline values.
    #
    # All values are milliseconds.
    #
    # For editorial scenes:
    #
    #   start/end = absolute timeline position
    #   duration  = local scene duration
    #
    # For clips:
    #
    #   start/end = absolute timeline position
    #   duration  = clip duration
    # --------------------------------------------------------

    start: int = 0
    end: int = 0
    duration: int = 0

    clips: list = field(
        default_factory=list
    )

    # Resolution state
    resolved: bool = False
    resolving: bool = False

    # Internal percentage state.
    percentage_resolving: bool = False

    # --------------------------------------------------------
    #
    # Local timeline values.
    #
    # These are primarily useful for editorial scenes.
    #
    # --------------------------------------------------------

    local_start: int = 0
    local_end: int = 0
    local_duration: int = 0

    @property
    def center(self):

        return (
            self.start +
            (self.duration // 2)
        )


# ============================================================
#
# TIMELINE RESOLVER
#
# ============================================================

class TimelineResolver:

    def __init__(
        self,
        sequence,
        fps,
        duration_provider=None
    ):

        self.sequence = sequence
        self.fps = fps

        #
        # The host application supplies actual media duration.
        #

        self.duration_provider = (
            duration_provider
        )

        #
        # Global timeline.
        #

        self.scene = TimelineObject(
            "scene"
        )

        #
        # Editorial tracks.
        #

        self.tracks = {}
        self.track_order = []

        #
        # Clips.
        #

        self.clips = {}
        self.clip_order = []

        #
        # Editorial scenes.
        #

        self.editorial_scenes = {}
        self.editorial_scene_order = []

        #
        # Resolution phase.
        #

        self.phase = None

        #
        # Current clip resolution context.
        #

        self.current_clip_id = None

        #
        # Current editorial scene being solved.
        #

        self.current_percentage_scene_id = None

        #
        # Resolved percentage bases.
        #
        # {
        #     "scene-001": 120000
        # }
        #

        self.percentage_bases = {}

    # ========================================================
    #
    # TIME CONVERSION
    #
    # ========================================================

    def ms_to_frames(
        self,
        ms
    ):

        return round(
            ms * self.fps / 1000
        )

    # --------------------------------------------------------

    def frames_to_ms(
        self,
        frames
    ):

        return round(
            frames * 1000 / self.fps
        )

    # --------------------------------------------------------

    def timeline_start_ms(
        self
    ):
        """
        Absolute start of the editorial timeline.
        """

        return self.frames_to_ms(
            TIMELINE_START_FRAME
        )

    # --------------------------------------------------------

    def scene_gap_ms(
        self
    ):
        """
        Gap between editorial scenes.
        """

        return self.frames_to_ms(
            SCENE_GAP_FRAMES
        )

    # ========================================================
    #
    # TIMING RESOLUTION
    #
    # ========================================================

    def resolve_ms(
        self,
        value,
        clip_id=None
    ):
        """
        Resolve any timeline timing value into milliseconds.

        Supported:

            integer
            float

            {
                "type": "milliseconds",
                "value": 5000
            }

            {
                "type": "seconds",
                "value": 5
            }

            {
                "type": "percentage",
                "value": 10
            }

            {
                "type": "percentage",
                "value": 50,
                "of": "scene:edscene-001:duration"
            }

            {
                "type": "reference",
                "value": "clip:clip-001:end"
            }

            {
                "type": "expression",
                "value": "clip:clip-001:end + 500"
            }
        """

        if value is None:
            return 0

        if isinstance(
            value,
            (int, float)
        ):

            return int(value)

        if not isinstance(
            value,
            dict
        ):

            raise TimelineResolutionError(
                f"Invalid timing value: {value!r}"
            )

        if "type" not in value:

            raise TimelineResolutionError(
                f"Timing value has no type: {value!r}"
            )

        timing_type = value["type"]

        # ----------------------------------------------------
        # milliseconds
        # ----------------------------------------------------

        if timing_type == "milliseconds":

            try:

                return int(
                    value["value"]
                )

            except (
                KeyError,
                TypeError,
                ValueError
            ):

                raise TimelineResolutionError(
                    f"Invalid milliseconds value: "
                    f"{value!r}"
                )

        # ----------------------------------------------------
        # from_strip
        # ----------------------------------------------------

        if timing_type == "from_strip":

            source_clip_id = value.get(
                "clip_id"
            )

            if not source_clip_id:

                raise TimelineResolutionError(
                    f"from_strip timing requires "
                    f"'clip_id': {value!r}"
                )

            if self.duration_provider is None:

                raise TimelineResolutionError(
                    f"No duration provider available "
                    f"for clip '{source_clip_id}'."
                )

            return int(
                self.duration_provider(
                    source_clip_id
                )
            )

        # ----------------------------------------------------
        # seconds
        # ----------------------------------------------------

        if timing_type == "seconds":

            try:

                return int(
                    float(
                        value["value"]
                    ) * 1000
                )

            except (
                KeyError,
                TypeError,
                ValueError
            ):

                raise TimelineResolutionError(
                    f"Invalid seconds value: "
                    f"{value!r}"
                )

        # ----------------------------------------------------
        # percentage
        # ----------------------------------------------------

        if timing_type == "percentage":

            return self.resolve_percentage(
                value,
                clip_id=clip_id
            )

        # ----------------------------------------------------
        # reference
        # ----------------------------------------------------

        if timing_type == "reference":

            return self.resolve_reference(
                value["value"],
                clip_id=clip_id
            )

        # ----------------------------------------------------
        # expression
        # ----------------------------------------------------

        if timing_type == "expression":

            return self.resolve_expression(
                value["value"],
                clip_id=clip_id
            )

        raise TimelineResolutionError(
            f"Unknown timing type '{timing_type}'"
        )

    # --------------------------------------------------------

    def resolve_frame(
        self,
        value,
        clip_id=None
    ):

        return self.ms_to_frames(
            self.resolve_ms(
                value,
                clip_id=clip_id
            )
        )

    # ========================================================
    #
    # PERCENTAGE
    #
    # ========================================================

    def resolve_percentage(
        self,
        value,
        clip_id=None
    ):

        try:

            percentage = float(
                value["value"]
            )

        except (
            KeyError,
            TypeError,
            ValueError
        ):

            raise TimelinePercentageResolutionError(
                f"Invalid percentage value: "
                f"{value!r}"
            )

        if percentage < 0:

            raise TimelinePercentageResolutionError(
                f"Percentage cannot be negative, "
                f"got {percentage}"
            )

        if percentage > 100:

            raise TimelinePercentageResolutionError(
                f"Percentage cannot exceed 100, "
                f"got {percentage}"
            )

        target = value.get(
            "of"
        )

        #
        # No explicit target means the current clip's
        # editorial scene.
        #

        if target is None:

            if clip_id is None:

                clip_id = (
                    self.current_clip_id
                )

            if clip_id is None:

                raise TimelinePercentageResolutionError(
                    "Percentage timing requires a clip "
                    "context when 'of' is omitted."
                )

            clip_obj = self.clips.get(
                clip_id
            )

            if clip_obj is None:

                raise TimelinePercentageResolutionError(
                    f"Unknown clip '{clip_id}'"
                )

            if clip_obj.clip is None:

                raise TimelinePercentageResolutionError(
                    f"Clip '{clip_id}' has no source data."
                )

            editorial_scene_id = (
                clip_obj.clip.get(
                    "editorial_scene"
                )
            )

            if not editorial_scene_id:

                raise TimelinePercentageResolutionError(
                    f"Percentage timing for clip "
                    f"'{clip_id}' requires an "
                    f"'editorial_scene' when "
                    f"'of' is omitted."
                )

            target = (
                f"scene:"
                f"{editorial_scene_id}:"
                f"duration"
            )

        target = (
            self._normalize_percentage_target(
                target
            )
        )

        scene_id = (
            self._percentage_target_editorial_scene(
                target
            )
        )

        if scene_id is not None:

            target_duration = (
                self.resolve_percentage_basis(
                    scene_id
                )
            )

        else:

            target_duration = (
                self.resolve_reference(
                    target,
                    clip_id=clip_id
                )
            )

        if target_duration < 0:

            raise TimelinePercentageResolutionError(
                f"Percentage target '{target}' "
                f"resolved to a negative duration: "
                f"{target_duration}"
            )

        return int(
            round(
                target_duration *
                percentage /
                100.0
            )
        )

    # --------------------------------------------------------

    def _percentage_target_editorial_scene(
        self,
        target
    ):

        if not isinstance(
            target,
            str
        ):

            return None

        tokens = target.split(":")

        if len(tokens) != 3:
            return None

        structure, selector, prop = tokens

        if structure != "scene":
            return None

        if prop != "duration":
            return None

        if selector not in self.editorial_scenes:
            return None

        return selector

    # --------------------------------------------------------

    def _normalize_percentage_target(
        self,
        target
    ):

        if isinstance(
            target,
            dict
        ):

            if target.get(
                "type"
            ) != "reference":

                raise TimelinePercentageResolutionError(
                    "Percentage 'of' object must be "
                    "a reference timing object."
                )

            target = target.get(
                "value"
            )

        if not isinstance(
            target,
            str
        ):

            raise TimelinePercentageResolutionError(
                f"Invalid percentage target: "
                f"{target!r}"
            )

        if target in self.editorial_scenes:

            return (
                f"scene:"
                f"{target}:"
                f"duration"
            )

        if target == "scene":

            return (
                "scene:duration"
            )

        if target in self.clips:

            return (
                f"clip:"
                f"{target}:"
                f"duration"
            )

        if target in self.tracks:

            return (
                f"track:"
                f"{target}:"
                f"duration"
            )

        tokens = target.split(":")

        if len(tokens) == 2:

            structure, selector = tokens

            if structure == "scene":

                if selector in self.editorial_scenes:

                    return (
                        f"scene:"
                        f"{selector}:"
                        f"duration"
                    )

            elif structure == "clip":

                if selector in self.clips:

                    return (
                        f"clip:"
                        f"{selector}:"
                        f"duration"
                    )

            elif structure == "track":

                if selector in self.tracks:

                    return (
                        f"track:"
                        f"{selector}:"
                        f"duration"
                    )

        return target

    # ========================================================
    #
    # REGISTRATION
    #
    # ========================================================

    def register_track(
        self,
        track_id
    ):

        if track_id in self.tracks:
            return

        self.tracks[
            track_id
        ] = TimelineObject(
            track_id
        )

        self.track_order.append(
            track_id
        )

    # --------------------------------------------------------

    def register_clip(
        self,
        clip,
        track_id
    ):

        self.register_track(
            track_id
        )

        if "_id" not in clip:

            raise TimelineResolutionError(
                f"Cannot register clip without "
                f"'_id': {clip!r}"
            )

        clip_id = clip[
            "_id"
        ]

        if clip_id in self.clips:

            raise TimelineResolutionError(
                f"Duplicate clip id '{clip_id}'"
            )

        obj = TimelineObject(
            clip_id
        )

        obj.clip = clip
        obj.track_id = track_id

        self.clips[
            clip_id
        ] = obj

        self.clip_order.append(
            clip_id
        )

        self.tracks[
            track_id
        ].clips.append(
            obj
        )

        scene_id = clip.get(
            "editorial_scene"
        )

        if scene_id:

            if (
                scene_id
                not in self.editorial_scenes
            ):

                self.editorial_scenes[
                    scene_id
                ] = TimelineObject(
                    scene_id
                )

                self.editorial_scene_order.append(
                    scene_id
                )

            self.editorial_scenes[
                scene_id
            ].clips.append(
                obj
            )

    # ========================================================
    #
    # DEFAULT DURATION
    #
    # ========================================================

    def _default_duration_ms(
        self,
        clip_id
    ):

        if self.duration_provider is not None:

            return int(
                self.duration_provider(
                    clip_id
                )
            )

        raise TimelineResolutionError(
            f"No duration provider available "
            f"for clip '{clip_id}'."
        )

    # --------------------------------------------------------

    def _default_duration_value(
        self,
        clip_id
    ):

        return {
            "type": "from_strip",
            "clip_id": clip_id
        }

    # ========================================================
    #
    # PERCENTAGE DEPENDENCY ANALYSIS
    #
    # ========================================================

    def _timing_depends_on_scene(
        self,
        value,
        scene_id,
        clip_id=None,
        visited=None
    ):

        if visited is None:
            visited = set()

        if isinstance(
            value,
            (int, float)
        ):

            return False

        if value is None:
            return False

        if not isinstance(
            value,
            dict
        ):

            return False

        timing_type = value.get(
            "type"
        )

        if timing_type in {
            "milliseconds",
            "seconds",
            "from_strip",
        }:

            return False

        if timing_type == "percentage":

            target = value.get(
                "of"
            )

            if target is None:

                if clip_id is None:
                    return True

                obj = self.clips.get(
                    clip_id
                )

                if obj is None:
                    return True

                source = obj.clip

                if source is None:
                    return True

                return (
                    source.get(
                        "editorial_scene"
                    )
                    == scene_id
                )

            try:

                target = (
                    self._normalize_percentage_target(
                        target
                    )
                )

            except TimelineResolutionError:

                return True

            target_scene = (
                self._percentage_target_editorial_scene(
                    target
                )
            )

            if target_scene == scene_id:
                return True

            return self._reference_depends_on_scene(
                target,
                scene_id,
                visited
            )

        if timing_type == "reference":

            return self._reference_depends_on_scene(
                value.get("value"),
                scene_id,
                visited
            )

        if timing_type == "expression":

            expression = value.get(
                "value"
            )

            if not isinstance(
                expression,
                str
            ):

                return False

            refs = set(
                REFERENCE_PATTERN.findall(
                    expression
                )
            )

            for ref in refs:

                if self._reference_depends_on_scene(
                    ref,
                    scene_id,
                    visited
                ):

                    return True

            return False

        return False

    # --------------------------------------------------------

    def _reference_depends_on_scene(
        self,
        ref,
        scene_id,
        visited=None
    ):

        if visited is None:
            visited = set()

        if not isinstance(
            ref,
            str
        ):

            return False

        key = (
            scene_id,
            ref
        )

        if key in visited:
            return True

        visited.add(
            key
        )

        tokens = ref.split(":")

        if len(tokens) == 2:

            structure, prop = tokens

            if (
                structure == "scene"
                and prop == "duration"
            ):

                return False

            return False

        if len(tokens) != 3:
            return False

        structure, selector, prop = tokens

        #
        # Direct editorial scene dependency.
        #

        if (
            structure == "scene"
            and selector == scene_id
            and prop in {
                "duration",
                "end",
                "center",
            }
        ):

            return True

        #
        # Another editorial scene.
        #

        if (
            structure == "scene"
            and selector in self.editorial_scenes
        ):

            target_scene = (
                self.editorial_scenes[
                    selector
                ]
            )

            for clip_obj in target_scene.clips:

                source = clip_obj.clip

                if source is None:
                    continue

                if self._timing_depends_on_scene(
                    source.get("start"),
                    scene_id,
                    clip_id=clip_obj._id,
                    visited=visited
                ):

                    return True

                if self._timing_depends_on_scene(
                    source.get(
                        "duration",
                        self._default_duration_value(
                            clip_obj._id
                        )
                    ),
                    scene_id,
                    clip_id=clip_obj._id,
                    visited=visited
                ):

                    return True

            return False

        #
        # Clip dependency.
        #

        if structure == "clip":

            obj = self.clips.get(
                selector
            )

            if obj is None:
                return False

            source = obj.clip

            if source is None:
                return False

            if self._timing_depends_on_scene(
                source.get("start"),
                scene_id,
                clip_id=selector,
                visited=visited
            ):

                return True

            if self._timing_depends_on_scene(
                source.get(
                    "duration",
                    self._default_duration_value(
                        selector
                    )
                ),
                scene_id,
                clip_id=selector,
                visited=visited
            ):

                return True

            return False

        #
        # Track dependency.
        #

        if structure == "track":

            track = self.tracks.get(
                selector
            )

            if track is None:
                return False

            for clip in track.clips:

                if self._reference_depends_on_scene(
                    f"clip:{clip._id}:{prop}",
                    scene_id,
                    visited
                ):

                    return True

            return False

        return False

    # ========================================================
    #
    # SCENE ANALYSIS
    #
    # ========================================================

    def _scene_has_percentage_dependency(
        self,
        scene_id
    ):

        scene = self.editorial_scenes.get(
            scene_id
        )

        if scene is None:

            raise TimelinePercentageResolutionError(
                f"Unknown editorial scene "
                f"'{scene_id}'"
            )

        for clip_obj in scene.clips:

            source = clip_obj.clip

            if source is None:
                continue

            if self._timing_depends_on_scene(
                source.get("start"),
                scene_id,
                clip_id=clip_obj._id
            ):

                return True

            if self._timing_depends_on_scene(
                source.get(
                    "duration",
                    self._default_duration_value(
                        clip_obj._id
                    )
                ),
                scene_id,
                clip_id=clip_obj._id
            ):

                return True

        return False

    # --------------------------------------------------------

    def _scene_has_absolute_duration(
        self,
        scene_id
    ):

        scene = self.editorial_scenes.get(
            scene_id
        )

        if scene is None:
            return False

        for clip_obj in scene.clips:

            source = clip_obj.clip

            if source is None:
                continue

            duration_value = source.get(
                "duration",
                self._default_duration_value(
                    clip_obj._id
                )
            )

            if not self._timing_depends_on_scene(
                duration_value,
                scene_id,
                clip_id=clip_obj._id
            ):

                return True

        return False

    # --------------------------------------------------------

    def _scene_initial_basis(
        self,
        scene_id
    ):

        scene = self.editorial_scenes.get(
            scene_id
        )

        if scene is None:

            raise TimelinePercentageResolutionError(
                f"Unknown editorial scene "
                f"'{scene_id}'"
            )

        seed = 0

        for clip_obj in scene.clips:

            source = clip_obj.clip

            if source is None:
                continue

            duration_value = source.get(
                "duration",
                self._default_duration_value(
                    clip_obj._id
                )
            )

            if self._timing_depends_on_scene(
                duration_value,
                scene_id,
                clip_id=clip_obj._id
            ):

                continue

            try:

                duration = (
                    self._resolve_absolute_timing_only(
                        duration_value
                    )
                )

            except TimelineResolutionError:

                duration = 0

            seed = max(
                seed,
                abs(int(duration))
            )

        for clip_obj in scene.clips:

            source = clip_obj.clip

            if source is None:
                continue

            start_value = source.get(
                "start"
            )

            if start_value is None:
                continue

            if self._timing_depends_on_scene(
                start_value,
                scene_id,
                clip_id=clip_obj._id
            ):

                continue

            try:

                start = (
                    self._resolve_absolute_timing_only(
                        start_value
                    )
                )

            except TimelineResolutionError:

                start = 0

            seed = max(
                seed,
                abs(int(start))
            )

        return max(
            seed,
            UNDERDETERMINED_SCENE_DURATION_MS
        )

    # --------------------------------------------------------

    def _resolve_absolute_timing_only(
        self,
        value
    ):

        if isinstance(
            value,
            (int, float)
        ):

            return int(value)

        if not isinstance(
            value,
            dict
        ):

            raise TimelineResolutionError(
                f"Invalid timing value: {value!r}"
            )

        timing_type = value.get(
            "type"
        )

        if timing_type == "milliseconds":

            return int(
                value["value"]
            )

        if timing_type == "seconds":

            return int(
                float(
                    value["value"]
                ) * 1000
            )

        raise TimelineResolutionError(
            "Timing is not intrinsically absolute."
        )

    # ========================================================
    #
    # INVALIDATION
    #
    # ========================================================

    def _invalidate_editorial_scene_clips(
        self,
        scene_id
    ):

        scene = self.editorial_scenes.get(
            scene_id
        )

        if scene is None:
            return

        for clip_obj in scene.clips:

            clip_obj.start = 0
            clip_obj.end = 0
            clip_obj.duration = 0

            clip_obj.local_start = 0
            clip_obj.local_end = 0
            clip_obj.local_duration = 0

            clip_obj.resolved = False
            clip_obj.resolving = False

        scene.local_start = 0
        scene.local_end = 0
        scene.local_duration = 0

        scene.resolved = False

        track_ids = {
            clip_obj.track_id
            for clip_obj in scene.clips
            if clip_obj.track_id is not None
        }

        for track_id in track_ids:

            track = self.tracks.get(
                track_id
            )

            if track is None:
                continue

            track.start = 0
            track.end = 0
            track.duration = 0

            track.resolved = False
            track.resolving = False

    # ========================================================
    #
    # SCENE POSITIONING
    # ========================================================

    def _editorial_scene_offset_ms(
        self,
        scene_id
    ):
        """
        Return the absolute timeline position of an editorial
        scene.

        Scene order is authoritative.

        Example at 24fps:

            Scene 1:
                start = frame 1

            Scene 2:
                start = Scene 1 end + 1 frame

            Scene 3:
                start = Scene 2 end + 1 frame

        This is intentionally separate from the scene's local
        duration calculation.
        """

        try:

            index = (
                self.editorial_scene_order.index(
                    scene_id
                )
            )

        except ValueError:

            raise TimelineResolutionError(
                f"Editorial scene '{scene_id}' "
                f"is not registered."
            )

        position_ms = (
            self.timeline_start_ms()
        )

        for previous_scene_id in (
            self.editorial_scene_order[
                :index
            ]
        ):

            previous_scene = (
                self.editorial_scenes[
                    previous_scene_id
                ]
            )

            #
            # Ensure previous scene has been solved.
            #

            self.compute_editorial_scene(
                previous_scene_id
            )

            position_ms += (
                previous_scene.duration
            )

            position_ms += (
                self.scene_gap_ms()
            )

        return int(
            position_ms
        )

    # ========================================================
    #
    # PERCENTAGE BASIS
    #
    # ========================================================

    def resolve_percentage_basis(
        self,
        scene_id
    ):

        if scene_id in self.percentage_bases:

            return int(
                self.percentage_bases[
                    scene_id
                ]
            )

        scene = self.editorial_scenes.get(
            scene_id
        )

        if scene is None:

            raise TimelinePercentageResolutionError(
                f"Unknown editorial scene "
                f"'{scene_id}'"
            )

        if scene.percentage_resolving:

            basis = self.percentage_bases.get(
                scene_id
            )

            if basis is not None:

                return int(
                    basis
                )

            basis = self._scene_initial_basis(
                scene_id
            )

            self.percentage_bases[
                scene_id
            ] = int(
                basis
            )

            return int(
                basis
            )

        scene.percentage_resolving = True

        previous_percentage_scene = (
            self.current_percentage_scene_id
        )

        self.current_percentage_scene_id = (
            scene_id
        )

        try:

            basis = self._scene_initial_basis(
                scene_id
            )

            self.percentage_bases[
                scene_id
            ] = int(
                basis
            )

            converged = False
            last_duration = None

            for _ in range(
                PERCENTAGE_MAX_ITERATIONS
            ):

                self.percentage_bases[
                    scene_id
                ] = int(
                    basis
                )

                self._invalidate_editorial_scene_clips(
                    scene_id
                )

                for clip_obj in scene.clips:

                    self.compute_clip(
                        clip_obj._id
                    )

                #
                # IMPORTANT:
                #
                # We calculate duration using LOCAL clip
                # coordinates here, not absolute timeline
                # coordinates.
                #

                if not scene.clips:

                    actual_start = 0
                    actual_end = 0
                    actual_duration = 0

                else:

                    actual_start = min(
                        clip.local_start
                        for clip in scene.clips
                    )

                    actual_end = max(
                        clip.local_end
                        for clip in scene.clips
                    )

                    actual_duration = max(
                        0,
                        actual_end -
                        actual_start
                    )

                scene.local_start = int(
                    actual_start
                )

                scene.local_end = int(
                    actual_end
                )

                scene.local_duration = int(
                    actual_duration
                )

                scene.duration = int(
                    actual_duration
                )

                last_duration = (
                    actual_duration
                )

                if abs(
                    actual_duration -
                    basis
                ) <= PERCENTAGE_TOLERANCE_MS:

                    basis = int(
                        actual_duration
                    )

                    converged = True

                    break

                basis = int(
                    actual_duration
                )

                if basis <= 0:
                    break

            if (
                not converged
                and (
                    last_duration is None
                    or last_duration <= 0
                )
            ):

                basis = max(
                    UNDERDETERMINED_SCENE_DURATION_MS,
                    int(basis or 0)
                )

                self.percentage_bases[
                    scene_id
                ] = basis

                self._invalidate_editorial_scene_clips(
                    scene_id
                )

                for clip_obj in scene.clips:

                    self.compute_clip(
                        clip_obj._id
                    )

                if scene.clips:

                    scene.local_start = min(
                        clip.local_start
                        for clip in scene.clips
                    )

                    scene.local_end = max(
                        clip.local_end
                        for clip in scene.clips
                    )

                    scene.local_duration = max(
                        0,
                        scene.local_end -
                        scene.local_start
                    )

                else:

                    scene.local_start = 0
                    scene.local_end = 0
                    scene.local_duration = 0

                scene.duration = (
                    scene.local_duration
                )

                if scene.duration <= 0:

                    scene.duration = (
                        UNDERDETERMINED_SCENE_DURATION_MS
                    )

                    scene.local_end = (
                        scene.local_start +
                        scene.duration
                    )

                self.percentage_bases[
                    scene_id
                ] = int(
                    scene.duration
                )

                return int(
                    scene.duration
                )

            if not converged:

                raise TimelinePercentageResolutionError(
                    f"Could not converge percentage "
                    f"timing for editorial scene "
                    f"'{scene_id}' after "
                    f"{PERCENTAGE_MAX_ITERATIONS} "
                    f"iterations. "
                    f"Last duration: "
                    f"{last_duration} ms."
                )

            self.percentage_bases[
                scene_id
            ] = int(
                basis
            )

            return int(
                basis
            )

        finally:

            scene.percentage_resolving = False

            self.current_percentage_scene_id = (
                previous_percentage_scene
            )

    # ========================================================
    #
    # EDITORIAL SCENE
    #
    # ========================================================

    def compute_editorial_scene(
        self,
        scene_id
    ):

        scene = self.editorial_scenes.get(
            scene_id
        )

        if scene is None:

            raise TimelineResolutionError(
                f"Unknown editorial scene "
                f"'{scene_id}'"
            )

        if scene.resolved:
            return

        if scene.resolving:

            if scene_id in self.percentage_bases:
                return

            raise TimelineCircularDependencyError(
                f"Circular dependency involving "
                f"editorial scene '{scene_id}'"
            )

        scene.resolving = True

        try:

            #
            # Resolve all previous scenes first.
            #
            # This guarantees that Scene 2 can never resolve
            # itself at frame 0 while Scene 1 exists.
            #

            index = (
                self.editorial_scene_order.index(
                    scene_id
                )
            )

            for previous_scene_id in (
                self.editorial_scene_order[
                    :index
                ]
            ):

                self.compute_editorial_scene(
                    previous_scene_id
                )

            #
            # Empty scene.
            #

            if not scene.clips:

                scene.local_start = 0
                scene.local_end = 0
                scene.local_duration = 0

                scene.start = (
                    self._editorial_scene_offset_ms(
                        scene_id
                    )
                )

                scene.duration = 0

                scene.end = scene.start

                scene.resolved = True

                return

            #
            # Determine whether this scene has a percentage
            # dependency.
            #

            has_percentage_dependency = (
                self._scene_has_percentage_dependency(
                    scene_id
                )
            )

            if has_percentage_dependency:

                self.resolve_percentage_basis(
                    scene_id
                )

            else:

                for clip_obj in scene.clips:

                    self.compute_clip(
                        clip_obj._id
                    )

            #
            # Calculate LOCAL bounds.
            #

            scene.local_start = min(
                clip.local_start
                for clip in scene.clips
            )

            scene.local_end = max(
                clip.local_end
                for clip in scene.clips
            )

            scene.local_duration = max(
                0,
                scene.local_end -
                scene.local_start
            )

            scene.duration = (
                scene.local_duration
            )

            #
            # Absolute scene position.
            #

            scene.start = (
                self._editorial_scene_offset_ms(
                    scene_id
                )
            )

            scene.end = (
                scene.start +
                scene.duration
            )

            #
            # Percentage basis remains LOCAL duration.
            #

            if has_percentage_dependency:

                self.percentage_bases[
                    scene_id
                ] = int(
                    scene.duration
                )

            scene.resolved = True

        finally:

            scene.resolving = False

    # ========================================================
    #
    # CLIP
    #
    # ========================================================

    def compute_clip(
        self,
        clip_id
    ):

        obj = self.clips.get(
            clip_id
        )

        if obj is None:

            raise TimelineResolutionError(
                f"Unknown clip '{clip_id}'"
            )

        if obj.resolved:
            return

        if obj.resolving:

            raise TimelineCircularDependencyError(
                f"Circular dependency involving "
                f"clip '{clip_id}'"
            )

        obj.resolving = True

        previous_clip_id = (
            self.current_clip_id
        )

        self.current_clip_id = (
            clip_id
        )

        try:

            clip = obj.clip

            if clip is None:

                raise TimelineResolutionError(
                    f"Clip '{clip_id}' has no source data."
                )

            #
            # Determine the editorial scene.
            #

            editorial_scene_id = (
                clip.get(
                    "editorial_scene"
                )
            )

            editorial_scene = None

            if editorial_scene_id:

                editorial_scene = (
                    self.editorial_scenes.get(
                        editorial_scene_id
                    )
                )

                if editorial_scene is None:

                    raise TimelineResolutionError(
                        f"Unknown editorial scene "
                        f"'{editorial_scene_id}' "
                        f"for clip '{clip_id}'."
                    )

            #
            # Start.
            #

            if "start" not in clip:

                raise TimelineResolutionError(
                    f"Clip '{clip_id}' has no 'start'."
                )

            start_value = clip["start"]

            #
            # FIX: "reference" and "expression" timing values
            # resolve through resolve_reference()/resolve_expression(),
            # which always return an ABSOLUTE timeline position -
            # every clip/scene "start"/"end"/"center" this resolver
            # produces (obj.start below, scene.start above) is
            # absolute, never scene-relative.
            #
            # The old code treated the resolved value of clip["start"]
            # as always being LOCAL (scene-relative) and unconditionally
            # added the scene's absolute offset on top of it:
            #
            #     obj.start = scene_offset + obj.local_start
            #
            # That is only correct when clip["start"] is a genuinely
            # local value (bare milliseconds/seconds, or a percentage
            # of the scene). For a clip whose "start" is
            # {"type": "reference", "value": "clip:X:end"} or
            # {"type": "reference", "value": "scene:X:start"} - which
            # is the overwhelming majority of real editorial data -
            # the resolved value is ALREADY absolute, so adding the
            # scene offset again double-counted it. The effect
            # compounds with every "clip:X:end" hop down a chain,
            # which is why later scenes/clips drift further and
            # further from their intended position while the very
            # first scene (whose offset is only ~1 frame) looked
            # almost right.
            #
            # Fix: only add the scene offset for genuinely local
            # timing types. Reference/expression results are used
            # as the absolute start directly, and local_start is
            # derived FROM that (for the percentage solver, which
            # still needs true local/scene-relative coordinates).
            #

            start_timing_type = (
                start_value.get("type")
                if isinstance(start_value, dict)
                else None
            )

            start_is_absolute = (
                start_timing_type in (
                    "reference",
                    "expression",
                )
            )

            resolved_start = self.resolve_ms(
                start_value,
                clip_id=clip_id
            )

            #
            # Duration.
            #

            duration_value = clip.get(
                "duration"
            )

            if duration_value is None:
                duration_value = self._default_duration_value(
                    clip_id
                )

            duration = self.resolve_ms(
                duration_value,
                clip_id=clip_id
            )

            if duration < 0:

                raise TimelineResolutionError(
                    f"Clip '{clip_id}' resolved to "
                    f"negative duration: "
                    f"{duration}"
                )

            #
            # Scene offset (or timeline start for scene-less clips).
            #

            if editorial_scene is not None:

                scene_offset = (
                    self._editorial_scene_offset_ms(
                        editorial_scene_id
                    )
                )

            else:

                scene_offset = (
                    self.timeline_start_ms()
                )

            #
            # Absolute + local values.
            #

            if start_is_absolute:

                obj.start = int(
                    resolved_start
                )

                obj.local_start = max(
                    0,
                    obj.start -
                    scene_offset
                )

            else:

                obj.local_start = int(
                    resolved_start
                )

                obj.start = (
                    scene_offset +
                    obj.local_start
                )

            obj.local_duration = int(
                duration
            )

            obj.local_end = (
                obj.local_start +
                obj.local_duration
            )

            obj.duration = int(
                duration
            )

            obj.end = (
                obj.start +
                obj.duration
            )

            obj.resolved = True

        finally:

            self.current_clip_id = (
                previous_clip_id
            )

            obj.resolving = False

    # ========================================================
    #
    # TRACK
    #
    # ========================================================

    def compute_track(
        self,
        track_id
    ):

        track = self.tracks.get(
            track_id
        )

        if track is None:

            raise TimelineResolutionError(
                f"Unknown track '{track_id}'"
            )

        if track.resolved:
            return

        if track.resolving:

            raise TimelineCircularDependencyError(
                f"Circular dependency involving "
                f"track '{track_id}'"
            )

        track.resolving = True

        try:

            for clip in track.clips:

                self.compute_clip(
                    clip._id
                )

            if not track.clips:

                track.start = 0
                track.end = 0
                track.duration = 0

            else:

                track.start = min(
                    clip.start
                    for clip in track.clips
                )

                track.end = max(
                    clip.end
                    for clip in track.clips
                )

                track.duration = (
                    track.end -
                    track.start
                )

            track.resolved = True

        finally:

            track.resolving = False

    # --------------------------------------------------------

    def compute_tracks(
        self
    ):

        for track_id in self.track_order:

            self.compute_track(
                track_id
            )

    # ========================================================
    #
    # GLOBAL SCENE
    #
    # ========================================================

    def compute_scene(
        self
    ):

        if self.scene.resolved:
            return

        if self.scene.resolving:

            raise TimelineCircularDependencyError(
                "Circular dependency involving scene"
            )

        self.scene.resolving = True

        try:

            #
            # The global scene begins at frame 1.
            #

            self.scene.start = (
                self.timeline_start_ms()
            )

            #
            # Resolve editorial scenes sequentially.
            #

            for scene_id in (
                self.editorial_scene_order
            ):

                self.compute_editorial_scene(
                    scene_id
                )

            #
            # Resolve remaining clips.
            #

            for clip_id in self.clip_order:

                self.compute_clip(
                    clip_id
                )

            if not self.clips:

                self.scene.end = (
                    self.scene.start
                )

                self.scene.duration = 0

            else:

                self.scene.end = max(
                    clip.end
                    for clip in self.clips.values()
                )

                self.scene.duration = max(
                    0,
                    self.scene.end -
                    self.scene.start
                )

            self.scene.resolved = True

        finally:

            self.scene.resolving = False

    # ========================================================
    #
    # REFERENCES
    #
    # ========================================================

    def resolve_reference(
        self,
        ref,
        clip_id=None
    ):

        if not isinstance(
            ref,
            str
        ):

            raise TimelineResolutionError(
                f"Reference must be a string: "
                f"{ref!r}"
            )

        tokens = ref.split(":")

        if len(tokens) == 2:

            structure, prop = tokens

            if structure != "scene":

                raise TimelineResolutionError(
                    f"Invalid two-part reference "
                    f"'{ref}'"
                )

            selector = None

        elif len(tokens) == 3:

            structure, selector, prop = tokens

        else:

            raise TimelineResolutionError(
                f"Invalid reference '{ref}'"
            )

        if prop not in VALID_PROPERTIES:

            raise TimelineResolutionError(
                f"Unknown timeline property "
                f"'{prop}'"
            )

        # ----------------------------------------------------
        # Global scene
        # ----------------------------------------------------

        if structure == "scene":

            if selector is None:

                if prop in (
                    SCENE_DIRECT_PROPERTIES
                ):

                    return getattr(
                        self.scene,
                        prop
                    )

                self.compute_scene()

                return getattr(
                    self.scene,
                    prop
                )

            target_scene = (
                self.editorial_scenes.get(
                    selector
                )
            )

            if target_scene is None:

                raise TimelineResolutionError(
                    f"Unknown editorial scene "
                    f"'{selector}'"
                )

            #
            # During percentage solving the duration is the
            # provisional LOCAL basis.
            #

            if (
                target_scene.percentage_resolving
                and selector in self.percentage_bases
            ):

                basis = int(
                    self.percentage_bases[
                        selector
                    ]
                )

                #
                # Scene start is still an ABSOLUTE position.
                #

                scene_start = (
                    self._editorial_scene_offset_ms(
                        selector
                    )
                )

                if prop == "duration":

                    return basis

                if prop == "start":

                    return scene_start

                if prop == "end":

                    return (
                        scene_start +
                        basis
                    )

                if prop == "center":

                    return (
                        scene_start +
                        (
                            basis // 2
                        )
                    )

            scene = (
                self._resolve_editorial_scene(
                    selector
                )
            )

            return getattr(
                scene,
                prop
            )

        # ----------------------------------------------------
        # Track / clip
        # ----------------------------------------------------

        obj = self._resolve_object(
            structure,
            selector
        )

        return getattr(
            obj,
            prop
        )

    # --------------------------------------------------------

    def _resolve_object(
        self,
        structure,
        selector
    ):

        if structure == "track":

            return self._resolve_track(
                selector
            )

        if structure == "clip":

            return self._resolve_clip(
                selector
            )

        raise TimelineResolutionError(
            f"Unknown structure '{structure}'"
        )

    # ========================================================
    #
    # TRACK REFERENCES
    #
    # ========================================================

    def _resolve_track(
        self,
        selector
    ):

        if selector is None:

            raise TimelineResolutionError(
                "Track selector required."
            )

        if selector == "first":

            if not self.track_order:

                raise TimelineResolutionError(
                    "No tracks registered."
                )

            selector = (
                self.track_order[0]
            )

        elif selector == "last":

            if not self.track_order:

                raise TimelineResolutionError(
                    "No tracks registered."
                )

            selector = (
                self.track_order[-1]
            )

        if selector not in self.tracks:

            raise TimelineResolutionError(
                f"Unknown track '{selector}'"
            )

        self.compute_track(
            selector
        )

        return self.tracks[
            selector
        ]

    # ========================================================
    #
    # CLIP REFERENCES
    #
    # ========================================================

    def _resolve_clip(
        self,
        selector
    ):

        if selector is None:

            raise TimelineResolutionError(
                "Clip selector required."
            )

        if selector == "first":

            if not self.clip_order:

                raise TimelineResolutionError(
                    "No clips registered."
                )

            selector = (
                self.clip_order[0]
            )

        elif selector == "last":

            if not self.clip_order:

                raise TimelineResolutionError(
                    "No clips registered."
                )

            selector = (
                self.clip_order[-1]
            )

        if selector not in self.clips:

            raise TimelineResolutionError(
                f"Unknown clip '{selector}'"
            )

        obj = self.clips[
            selector
        ]

        if not obj.resolved:

            self.compute_clip(
                selector
            )

        return obj

    # ========================================================
    #
    # EDITORIAL SCENE REFERENCES
    #
    # ========================================================

    def _resolve_editorial_scene(
        self,
        selector
    ):

        if selector not in self.editorial_scenes:

            raise TimelineResolutionError(
                f"Unknown editorial scene "
                f"'{selector}'"
            )

        self.compute_editorial_scene(
            selector
        )

        return self.editorial_scenes[
            selector
        ]

    # ========================================================
    #
    # EXPRESSIONS
    #
    # ========================================================

    def resolve_expression(
        self,
        expression,
        clip_id=None
    ):

        if not isinstance(
            expression,
            str
        ):

            raise TimelineResolutionError(
                f"Expression must be a string: "
                f"{expression!r}"
            )

        expr = expression

        refs = sorted(
            set(
                REFERENCE_PATTERN.findall(
                    expr
                )
            ),
            key=len,
            reverse=True
        )

        for ref in refs:

            resolved = self.resolve_reference(
                ref,
                clip_id=clip_id
            )

            expr = expr.replace(
                ref,
                str(resolved)
            )

        return self._safe_eval_integer(
            expr
        )

    # --------------------------------------------------------

    def _safe_eval_integer(
        self,
        expression
    ):

        try:

            tree = ast.parse(
                expression,
                mode="eval"
            )

        except SyntaxError as exc:

            raise TimelineResolutionError(
                f"Invalid timeline expression "
                f"'{expression}'"
            ) from exc

        operators = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv,
            ast.Mod: operator.mod,
            ast.Pow: operator.pow,
        }

        unary_operators = {
            ast.UAdd: operator.pos,
            ast.USub: operator.neg,
        }

        def evaluate(
            node
        ):

            if isinstance(
                node,
                ast.Expression
            ):

                return evaluate(
                    node.body
                )

            if isinstance(
                node,
                ast.Constant
            ):

                if isinstance(
                    node.value,
                    (int, float)
                ):

                    return node.value

                raise TimelineResolutionError(
                    "Only numeric constants are "
                    "allowed in timeline expressions."
                )

            if isinstance(
                node,
                ast.BinOp
            ):

                left = evaluate(
                    node.left
                )

                right = evaluate(
                    node.right
                )

                op = operators.get(
                    type(node.op)
                )

                if op is None:

                    raise TimelineResolutionError(
                        f"Unsupported operator "
                        f"'{type(node.op).__name__}'"
                    )

                try:

                    return op(
                        left,
                        right
                    )

                except ZeroDivisionError as exc:

                    raise TimelineResolutionError(
                        "Division by zero in timeline "
                        f"expression '{expression}'"
                    ) from exc

            if isinstance(
                node,
                ast.UnaryOp
            ):

                value = evaluate(
                    node.operand
                )

                op = unary_operators.get(
                    type(node.op)
                )

                if op is None:

                    raise TimelineResolutionError(
                        f"Unsupported unary operator "
                        f"'{type(node.op).__name__}'"
                    )

                return op(
                    value
                )

            raise TimelineResolutionError(
                f"Unsupported expression element "
                f"'{type(node).__name__}'"
            )

        result = evaluate(
            tree
        )

        return int(
            result
        )

    # ========================================================
    #
    # RESET
    #
    # ========================================================

    def reset(
        self
    ):

        #
        # Global scene.
        #

        self.scene.start = 0
        self.scene.end = 0
        self.scene.duration = 0

        self.scene.local_start = 0
        self.scene.local_end = 0
        self.scene.local_duration = 0

        self.scene.resolved = False
        self.scene.resolving = False
        self.scene.percentage_resolving = False

        #
        # Tracks.
        #

        for track in self.tracks.values():

            track.start = 0
            track.end = 0
            track.duration = 0

            track.local_start = 0
            track.local_end = 0
            track.local_duration = 0

            track.resolved = False
            track.resolving = False
            track.percentage_resolving = False

        #
        # Clips.
        #

        for clip in self.clips.values():

            clip.start = 0
            clip.end = 0
            clip.duration = 0

            clip.local_start = 0
            clip.local_end = 0
            clip.local_duration = 0

            clip.resolved = False
            clip.resolving = False
            clip.percentage_resolving = False

        #
        # Editorial scenes.
        #

        for scene in self.editorial_scenes.values():

            scene.start = 0
            scene.end = 0
            scene.duration = 0

            scene.local_start = 0
            scene.local_end = 0
            scene.local_duration = 0

            scene.resolved = False
            scene.resolving = False
            scene.percentage_resolving = False

        #
        # Percentage bases.
        #

        self.percentage_bases.clear()

        #
        # Context.
        #

        self.current_clip_id = None
        self.current_percentage_scene_id = None
        self.phase = None

    # ========================================================
    #
    # FULL RESOLUTION
    #
    # ========================================================

    def resolve_timeline(
        self
    ):
        """
        Resolve the complete timeline.

        Resolution order:

            1. Editorial scenes sequentially
            2. Individual clips
            3. Tracks
            4. Global scene

        Editorial scene starts are absolute and sequential.

        Clip starts inside editorial scenes are scene-relative
        UNLESS the "start" timing itself is a reference or
        expression, in which case it is already absolute (see
        the fix note in compute_clip).

        The first scene begins at frame 1.

        Every subsequent scene receives a one-frame gap.
        """

        self.reset()

        # ----------------------------------------------------
        # PASS 1
        #
        # Editorial scenes.
        #
        # Sequential resolution is important.
        # ----------------------------------------------------

        self.phase = (
            "editorial scenes"
        )

        for scene_id in (
            self.editorial_scene_order
        ):

            self.compute_editorial_scene(
                scene_id
            )

        # ----------------------------------------------------
        # PASS 2
        #
        # Every clip.
        # ----------------------------------------------------

        self.phase = (
            "clips"
        )

        for clip_id in self.clip_order:

            self.compute_clip(
                clip_id
            )

        # ----------------------------------------------------
        # PASS 3
        #
        # Tracks.
        # ----------------------------------------------------

        self.phase = (
            "tracks"
        )

        self.compute_tracks()

        # ----------------------------------------------------
        # PASS 4
        #
        # Global scene.
        # ----------------------------------------------------

        self.phase = (
            "scene"
        )

        self.compute_scene()

        self.phase = None

        return self.scene
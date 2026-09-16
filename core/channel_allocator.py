ROLE_WEIGHT = {
    "metadata": 0,
    "music": 10,
    "sfx": 20,
    "audio": 30,
    "video-audio": 35,
    "video-main": 40,
    "video-overlay": 50,
    "transform": 55,
    "text": 90,
}


class ChannelAllocator:
    """
    Expanding-span allocator with real occupancy relocation.

    Rule: when a role needs more channels it expands upward and
    pushes EVERY higher role (and their already-placed strips)
    further up. The new span is stored permanently for that role.
    """

    def __init__(self, max_channel=20):

        self.max_channel = max_channel

        # role -> (low, high)
        self.spans = {}

        # channel -> list of (start_frame, end_frame)
        self.occupancy = {
            c: []
            for c in range(1, max_channel + 1)
        }

        self.preferred = {
            "music": 1,
            "sfx": 2,
            "audio": 3,
            "video-audio": 4,
            "video-main": 5,
            "video-overlay": 7,
            "transform": 9,
            "text": 10,
        }

        self.ordered_roles = sorted(
            ROLE_WEIGHT.keys(),
            key=lambda r: ROLE_WEIGHT.get(r, 0),
        )

    def _overlaps(self, channel, start, end):

        for s, e in self.occupancy.get(channel, []):

            if not (end <= s or start >= e):
                return True

        return False

    def _get_span(self, role):

        if role not in self.spans:

            base = self.preferred.get(role, 5)

            self.spans[role] = (
                base,
                base,
            )

        return self.spans[role]

    def _set_span(self, role, low, high):

        self.spans[role] = (
            max(1, low),
            min(high, self.max_channel),
        )

    def _find_free(self, low, high, start, end):

        for ch in range(low, high + 1):

            if ch > self.max_channel:
                break

            if not self._overlaps(
                ch,
                start,
                end,
            ):
                return ch

        return None

    def _relocate_occupancy(self, from_ch, to_ch):

        if from_ch == to_ch:
            return

        if from_ch not in self.occupancy:
            return

        intervals = self.occupancy[from_ch]

        if not intervals:
            return

        self.occupancy.setdefault(
            to_ch,
            [],
        ).extend(intervals)

        self.occupancy[from_ch] = []

    def _push_higher_roles(self, from_role, amount):

        if amount <= 0:
            return

        my_w = ROLE_WEIGHT.get(
            from_role,
            0,
        )

        channels_to_move = sorted(
            [
                c
                for c in self.occupancy
                if c >= 1
            ],
            reverse=True,
        )

        for role in reversed(self.ordered_roles):

            if ROLE_WEIGHT.get(role, 0) <= my_w:
                continue

            if role not in self.spans:
                continue

            old_lo, old_hi = self.spans[role]

            new_lo = min(
                old_lo + amount,
                self.max_channel,
            )

            new_hi = min(
                old_hi + amount,
                self.max_channel,
            )

            self.spans[role] = (
                new_lo,
                new_hi,
            )

        for ch in channels_to_move:

            owner = None

            for r, (lo, hi) in self.spans.items():

                if (
                    lo <= ch <= hi
                    and ROLE_WEIGHT.get(r, 0) > my_w
                ):
                    owner = r
                    break

            if owner is None:
                continue

            new_ch = min(
                ch + amount,
                self.max_channel,
            )

            if new_ch != ch:

                self._relocate_occupancy(
                    ch,
                    new_ch,
                )

    def allocate(
        self,
        role,
        start_frame,
        end_frame,
        prefer_pair=False,
    ):

        start = int(start_frame)
        end = int(end_frame)

        video_ch = None
        audio_ch = None

        if role in {
            "video-main",
            "video-overlay",
            "text",
            "transform",
        }:

            low, high = self._get_span(role)

            video_ch = self._find_free(
                low,
                high,
                start,
                end,
            )

            if video_ch is None:

                self._set_span(
                    role,
                    low,
                    high + 1,
                )

                self._push_higher_roles(
                    role,
                    1,
                )

                low, high = self._get_span(role)

                video_ch = self._find_free(
                    low,
                    high,
                    start,
                    end,
                )

            if video_ch is None:
                video_ch = high

            self.occupancy.setdefault(
                video_ch,
                [],
            ).append(
                (
                    start,
                    end,
                )
            )

        if role in {
            "audio",
            "sfx",
            "music",
        }:

            low, high = self._get_span(role)

            audio_ch = self._find_free(
                low,
                high,
                start,
                end,
            )

            if audio_ch is None:

                self._set_span(
                    role,
                    low,
                    high + 1,
                )

                self._push_higher_roles(
                    role,
                    1,
                )

                low, high = self._get_span(role)

                audio_ch = self._find_free(
                    low,
                    high,
                    start,
                    end,
                )

            if audio_ch is None:
                audio_ch = low

            self.occupancy.setdefault(
                audio_ch,
                [],
            ).append(
                (
                    start,
                    end,
                )
            )

        elif prefer_pair and video_ch is not None:

            pair_role = "video-audio"

            if pair_role not in self.spans:

                vlow, _ = self._get_span(role)

                self.spans[pair_role] = (
                    max(1, vlow - 1),
                    max(1, vlow - 1),
                )

            low, high = self._get_span(
                pair_role,
            )

            audio_ch = self._find_free(
                low,
                high,
                start,
                end,
            )

            if audio_ch is None:

                self._set_span(
                    pair_role,
                    low,
                    high + 1,
                )

                self._push_higher_roles(
                    pair_role,
                    1,
                )

                low, high = self._get_span(
                    pair_role,
                )

                audio_ch = self._find_free(
                    low,
                    high,
                    start,
                    end,
                )

            if audio_ch is None:
                audio_ch = low

            self.occupancy.setdefault(
                audio_ch,
                [],
            ).append(
                (
                    start,
                    end,
                )
            )

        return video_ch, audio_ch


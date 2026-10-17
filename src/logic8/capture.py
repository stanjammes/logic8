"""In-memory capture model.

A capture is stored per channel as a sorted array of *transition indices*: sample
index ``t`` is in the array when sample ``t`` differs from sample ``t - 1``. Together
with the level at sample 0 this describes the channel exactly, is compact for the
mostly-idle signals a logic analyser sees, and is what the analysis and decoders
work on.
"""
from bisect import bisect_right
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Channel:
    index: int
    initial: int
    transitions: np.ndarray  # int64 sample indices, ascending

    def level_at(self, sample):
        return self.initial ^ (bisect_right(self.transitions, sample) & 1)

    def edges(self):
        """Yield (sample, new_level) for each transition."""
        level = self.initial
        for t in self.transitions:
            level ^= 1
            yield int(t), level


@dataclass
class Capture:
    sample_rate: float
    num_samples: int
    channels: dict = field(default_factory=dict)  # index -> Channel
    trigger_sample: int | None = None

    @property
    def duration(self):
        return self.num_samples / self.sample_rate

    def time(self, sample):
        return sample / self.sample_rate

    def channel(self, index):
        try:
            return self.channels[index]
        except KeyError:
            raise KeyError(f"channel {index} not in capture (have {sorted(self.channels)})") from None

    @classmethod
    def from_samples(cls, samples, sample_rate, channels=range(8), trigger_sample=None):
        """Build from a uint8 array where bit n of each byte is channel n."""
        samples = np.asarray(samples, dtype=np.uint8)
        cap = cls(sample_rate, len(samples), trigger_sample=trigger_sample)
        for ch in channels:
            bits = (samples >> ch) & 1
            trans = np.flatnonzero(np.diff(bits)) + 1
            cap.channels[ch] = Channel(ch, int(bits[0]) if len(bits) else 0, trans.astype(np.int64))
        return cap

    def to_samples(self):
        """Expand back to a uint8 sample array (bit n = channel n)."""
        out = np.zeros(self.num_samples, dtype=np.uint8)
        for ch in self.channels.values():
            toggles = np.zeros(self.num_samples + 1, dtype=np.uint8)
            np.bitwise_xor.at(toggles, ch.transitions, 1)
            bits = np.bitwise_xor.accumulate(toggles[:-1]) ^ ch.initial
            out |= bits.astype(np.uint8) << ch.index
        return out

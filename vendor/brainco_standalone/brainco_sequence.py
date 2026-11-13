"""Bounded, loss-visible sample ordering within one acknowledged EEG stream."""
from collections import OrderedDict


class SequenceError(RuntimeError):
    pass


class SampleOrder:
    def __init__(self, window):
        if window < 1:
            raise ValueError('Reorder window must be positive')
        self.window = int(window)
        self.pending = {}
        self.history = OrderedDict()
        self.highest = None
        self.last = None
        self.duplicates = 0
        self.reordered = 0
        self.missing = 0
        self.emitted = 0

    def add(self, index, values, raw):
        if index in self.pending:
            if self.pending[index][1] != raw:
                raise SequenceError(f'EEG conflicting duplicate counter={index}; payload differs, possible stream reset')
            self.duplicates += 1
            return
        if self.last is not None and index <= self.last:
            if index in self.history and self.history[index] == raw:
                self.duplicates += 1
                return
            raise SequenceError(f'EEG late/reset counter={index}, last_emitted={self.last}, '
                                f'highest={self.highest}, reorder_window={self.window}; cannot safely stitch')
        if self.highest is not None and index < self.highest:
            if self.highest - index > self.window:
                raise SequenceError(f'EEG backward counter={index}, highest={self.highest}, '
                                    f'reorder_window={self.window}; possible reset or excessive reordering')
            self.reordered += 1
        self.highest = index if self.highest is None else max(index, self.highest)
        self.pending[index] = (values, raw)

    def take(self, flush=False):
        if self.highest is None:
            return []
        cutoff = self.highest if flush else self.highest - self.window
        rows = []
        for index in sorted(i for i in self.pending if i <= cutoff):
            values, raw = self.pending.pop(index)
            if self.last is not None:
                self.missing += index - self.last - 1
            self.last = index
            self.history[index] = raw
            while len(self.history) > max(100, self.window * 4):
                self.history.popitem(last=False)
            self.emitted += 1
            rows.append((index, values))
        return rows

    def diagnostics(self):
        return dict(reorder_window_samples=self.window, reordered_samples=self.reordered,
                    identical_duplicates=self.duplicates, missing_samples=self.missing,
                    emitted_samples=self.emitted, pending_samples=len(self.pending),
                    highest_counter=self.highest, last_emitted_counter=self.last)

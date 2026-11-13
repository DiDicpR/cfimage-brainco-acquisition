"""Rolling receive-clock estimate. Not a hardware trigger or latency calibration."""
from collections import deque
import threading
import numpy as np


class SampleClock:
    def __init__(self, rate, window=120):
        self.rate = rate
        self.window = window
        self.points = deque()
        self.lock = threading.Lock()
        self.generation = 0

    def observe(self, counter, received):
        with self.lock:
            if self.points:
                t, c = self.points[-1]
                if counter == c or received <= t:
                    return
                if counter < c:
                    self.points.clear()
                    self.generation += 1
            self.points.append((float(received), int(counter)))
            while len(self.points) > 2 and received-self.points[0][0] > self.window:
                self.points.popleft()

    def map_event(self, target):
        with self.lock:
            points = list(self.points)
            generation = self.generation
        if len(points) < 3 or points[-1][0]-points[0][0] < 1:
            return dict(status='warming_up', generation=generation)
        if not points[0][0] <= target <= points[-1][0]:
            return dict(status='outside_observed_range', generation=generation)
        data = np.asarray(points)
        x = data[:, 0]-points[-1][0]
        y = data[:, 1]-points[-1][1]
        slope, intercept = np.polyfit(x, y, 1)
        residual = y-(slope*x+intercept)
        # Reject large packet stalls instead of calling extrapolation verified.
        rms = float(np.sqrt(np.mean(residual**2))/max(slope, 1))
        if not .98*self.rate < slope < 1.02*self.rate or rms > .05:
            return dict(status='unstable', generation=generation, residual_rms_s=rms)
        fraction = float(points[-1][1]+intercept+slope*(target-points[-1][0]))
        return dict(status='host_arrival_estimate', generation=generation,
                    fractional_counter=fraction, sample=int(round(fraction)),
                    effective_hz=float(slope), ppm_vs_nominal=float((slope/self.rate-1)*1e6),
                    residual_rms_s=rms, anchor_count=len(points),
                    anchor_start=points[0], anchor_end=points[-1])

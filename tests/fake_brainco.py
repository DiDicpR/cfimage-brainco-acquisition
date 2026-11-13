import json,threading,time,sqlite3
from pathlib import Path
import numpy as np
import pytest
from src.acquisition_devices.brainco_native import StandaloneBrainCoDevice, NativeAudit, kit_module
from src.acquisition_devices.base import PendingSync

ROOT=Path(__file__).resolve().parents[2]

class Source32:
    ready_timeout=2
    def describe(self):
        return dict(real=False,sample_rate=250,channels=[f'Ch{i}' for i in range(32)],units='test_native',timestamp_quality='estimated_unverified')
    def start(self,on_sample,on_error):
        self.done=threading.Event()
        def stream():
            base=time.perf_counter();i=0
            while not self.done.is_set():
                due=int((time.perf_counter()-base)*250)
                while i<=due:
                    on_sample(base+i/250,[50000.125+i/100]*32,i);i+=1
                self.done.wait(.001)
        self.thread=threading.Thread(target=stream,daemon=True);self.thread.start()
    def stop(self):
        self.done.set();self.thread.join(2)
    def diagnostics(self):return {'test_fake':True}


def make_device():return StandaloneBrainCoDevice(dict(channel_count=32,sampling_rate_hz=250),Source32)


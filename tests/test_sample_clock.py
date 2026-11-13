import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'vendor/brainco_standalone'))
from sample_clock import SampleClock


class ClockTests(unittest.TestCase):
    def test_seven_hours_both_drift_directions(self):
        for ppm in [-3000, -1000, 1500]:
            rate=250*(1+ppm/1e6)
            c=SampleClock(250)
            for n in range(25201):
                c.observe(round(n*rate),100+n)
            value=c.map_event(25299.5)
            self.assertEqual(value['status'],'host_arrival_estimate')
            self.assertLess(abs(value['fractional_counter']-25199.5*rate),1)
            self.assertAlmostEqual(value['ppm_vs_nominal'],ppm,delta=10)
            self.assertEqual(c.map_event(25301)['status'],'outside_observed_range')

    def test_warmup_reset_and_stall(self):
        c=SampleClock(250)
        self.assertEqual(c.map_event(0)['status'],'warming_up')
        for n in range(10):c.observe(n*250,n)
        c.observe(2500,20)
        self.assertEqual(c.map_event(9)['status'],'unstable')
        c.observe(0,21)
        value=c.map_event(21)
        self.assertEqual(value['status'],'warming_up')
        self.assertEqual(value['generation'],1)

    def test_marker_uses_counter_not_drifted_nominal_timestamp(self):
        import threading
        from types import SimpleNamespace
        from src.acquisition_devices.brainco_native import StandaloneBrainCoDevice
        c=SampleClock(250)
        for n in range(121):c.observe(n*250,100+n)
        logs=[]
        d=StandaloneBrainCoDevice({})
        d.source=SimpleNamespace(sample_clock=c)
        d.archive=SimpleNamespace(accepted=30001,event=lambda *a,**k:logs.append((a,k)))
        d.cap=SimpleNamespace(lock=threading.Lock(),check=lambda:None,
            rows=[(245+i/250,[0]*32,29750+i) for i in range(251)])
        d.segment='test';d.clock_offset=0;d.recording=True
        pending=d.begin_event(101,219500000000)
        result=d.poll_event(pending)
        self.assertEqual(result.sample_index,29876)
        self.assertEqual(result.quality,'software_estimated')
        self.assertEqual(logs[-1][1]['clock_fit']['status'],'host_arrival_estimate')
        self.assertFalse(logs[-1][1]['hardware_sync_verified'])

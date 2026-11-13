import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from dataclasses import replace
from collections import Counter
import tempfile,unittest
from unittest.mock import patch
from src.config import load_config,BrainCoConfig
from src.acquisition import runtime,check_assets
from src.cli import main

ROOT=Path(__file__).resolve().parents[1]

class BrainCoTaskTests(unittest.TestCase):
    def setUp(self):
        self.c=load_config(ROOT/'config/brainco.toml')

    def test_strict_config_and_paths(self):
        self.assertIsInstance(self.c,BrainCoConfig)
        self.assertEqual(self.c.trials,25800)
        self.assertEqual(self.c.assets['manifest'],ROOT/'assets/manifest.csv')
        text=(ROOT/'config/brainco.toml').read_text(encoding='utf-8')
        for old,new in [('break_every = 80','break_every = 50'),('repetitions = 10','repetitions = 100'),('channels = 32','channels = true'),('trial_duration_s = 0.6','trial_duration_s = nan'),('fullscreen = true','fullscreen = 1'),('gain = 6','gain = 7'),('isi_s = 0.1','unknown_isi = 0.1')]:
            with self.subTest(old=old),tempfile.TemporaryDirectory() as folder:
                p=Path(folder)/'config.toml';p.write_text(text.replace(old,new),encoding='utf-8')
                with self.assertRaises(ValueError):load_config(p)

    def test_no_implicit_hardware_or_fast_hardware(self):
        for args in [[],['--fast']]:
            with patch('src.acquisition.launch') as launch,patch('src.acquisition.check_device') as device:
                self.assertEqual(main(['--config',str(ROOT/'config/brainco.toml'),*args]),1)
                launch.assert_not_called();device.assert_not_called()
        with patch('src.acquisition.check_assets') as assets:
            self.assertEqual(main(['--config',str(ROOT/'config/brainco.toml'),'--validate']),0)
            assets.assert_not_called()

    @unittest.skipUnless((ROOT/'assets/stimuli').is_dir(), 'Full asset test requires NAS download')
    def test_schedule_coverage_and_resume(self):
        from src.experiment.scenes import read_scene_catalog
        from src.experiment.fixed_schedule import build_fixed_plan
        e,d=runtime(self.c)
        self.assertEqual(Path(d['devices']['brainco']['sdk_python']).parent,ROOT/'.venv/Scripts')
        stimuli,metadata=read_scene_catalog(self.c.assets['manifest'])
        p,s=build_fixed_plan(stimuli,metadata,{},e)
        self.assertEqual(len(p.trials),25800)
        self.assertEqual(Counter(t.image_id for t in p.trials),Counter({t.image_id:10 for t in stimuli}))
        self.assertEqual(len({m['scene'] for m in metadata.values()}),10)
        done={(t.schedule_sha256,t.schedule_trial_id,t.image_id):1 for t in p.trials[:81] if t.schedule_trial_id!=80}
        resumed,_=build_fixed_plan(stimuli,metadata,done,e)
        self.assertEqual([t.schedule_trial_id for t in resumed.trials[:2]],[80,82])
        self.assertEqual(s['estimated_minutes'],301)

    def test_missing_assets_fail_before_hardware(self):
        c=replace(self.c,assets=dict(self.c.assets,manifest=ROOT/'assets/missing.csv'))
        with self.assertRaises(FileNotFoundError):check_assets(c)

    def test_real_preflight_logic_with_synthetic_native_source(self):
        from fake_brainco import make_device
        from src.operator import preflight_device
        _,d=runtime(self.c)
        cap=make_device()
        with tempfile.TemporaryDirectory() as folder:
            d['_preflight_output_root']=folder
            d['_preflight_annotation']=dict(object_id='TEST',semantic_label='synthetic_test',test_only=True)
            with patch('src.operator.create_configured_device',return_value=cap):
                result=preflight_device('brainco',d,200)
            self.assertEqual(result['channel_count'],32)
            self.assertTrue(result['saved_annotation_verified'])
            self.assertFalse(result['hardware_sync_verified'])
            self.assertFalse(cap.health().recording)

    @unittest.skipUnless((ROOT/'assets/stimuli').is_dir(), 'GUI asset integration requires NAS download')
    def test_gui_preserves_protocol(self):
        from PySide6.QtWidgets import QApplication
        from src.operator_ui import OperatorDialog
        from src.operator import formal_device_modes
        app=QApplication.instance() or QApplication([])
        e,d=runtime(self.c);dialog=OperatorDialog(e,d,formal_device_modes(d),lambda *_:None)
        self.assertEqual(dialog.current_mode(),'brainco')
        self.assertEqual(dialog.target_repeats.value(),10)
        self.assertEqual(dialog.image_duration.value(),.6)
        self.assertEqual(len(dialog.scene_selector.selected_ids()),2580)
        self.assertFalse(dialog.scene_selector.isEnabled())
        dialog.close();dialog.deleteLater();app.processEvents()

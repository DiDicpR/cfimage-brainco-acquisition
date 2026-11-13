import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import Config, load_config
from src.devices import MockDevice
from src.runner import run


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = Config("test", 2, 0.1, 100, 2, self.root / "data")

    def test_complete_session_and_unique_directories(self):
        session = run(self.config, realtime=False)
        other = run(self.config, realtime=False)
        self.assertNotEqual(session, other)
        metadata = json.loads((session / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "completed")
        self.assertEqual(metadata["samples_written"], 20)
        self.assertEqual(metadata["trials_completed"], 2)
        with (session / "samples.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 20)
        self.assertEqual(rows[-1]["trial"], "2")
        self.assertIn("ch_2", rows[0])
        events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
        self.assertEqual([e["event"] for e in events],
                         ["session_start", "trial_start", "trial_end", "trial_start", "trial_end", "session_end"])
        self.assertTrue(all(a["elapsed_s"] <= b["elapsed_s"] for a, b in zip(events, events[1:])))

    def test_failures_and_interruptions_close_device_and_preserve_data(self):
        for error, expected in [(RuntimeError("read failed"), "failed"), (KeyboardInterrupt(), "interrupted")]:
            with self.subTest(status=expected):
                class BrokenDevice(MockDevice):
                    def read_sample(self, sample_index):
                        if sample_index == 2:
                            raise error
                        return super().read_sample(sample_index)
                device = BrokenDevice(100, 2)
                before = set(self.config.output_dir.glob("*"))
                with self.assertRaises(type(error)):
                    run(self.config, realtime=False, device=device)
                session, = set(self.config.output_dir.glob("*")) - before
                state = json.loads((session / "session.json").read_text())
                self.assertEqual(state["status"], expected)
                self.assertEqual(state["samples_written"], 2)
                self.assertFalse(device.is_open)

    def test_open_and_cleanup_failure(self):
        class OpenFailure(MockDevice):
            def open(self):
                super().open()
                raise RuntimeError("open failed")
        device = OpenFailure(100, 2)
        with self.assertRaisesRegex(RuntimeError, "open failed"):
            run(self.config, realtime=False, device=device)
        self.assertFalse(device.is_open)

        class CloseFailure(MockDevice):
            def close(self):
                raise RuntimeError("close failed")
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            run(self.config, realtime=False, device=CloseFailure(100, 2))
        states = [json.loads(p.read_text()) for p in self.config.output_dir.glob("*/session.json")]
        self.assertTrue(all(s["status"] == "failed" for s in states))

    def test_config_validation_and_relative_path(self):
        source = (ROOT / "config/config.toml").read_text(encoding="utf-8")
        path = self.root / "config.toml"
        path.write_text(source, encoding="utf-8")
        self.assertEqual(load_config(path).output_dir, (self.root / "../data").resolve())
        for old, new in [('trials = 3', 'trials = true'),
                         ('trial_duration_s = 1.0', 'trial_duration_s = nan'),
                         ('trial_duration_s = 1.0', 'trial_duration_s = 0.001'),
                         ('channels = 2', 'channels = 0'),
                         ('backend = "mock"', 'backend = "hardware"'),
                         ('trials = 3', 'trial = 3'),
                         ('output_dir = "../data"', 'output_dir = ""')]:
            with self.subTest(value=new):
                path.write_text(source.replace(old, new), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_cli_validation_has_no_side_effects_and_reports_errors(self):
        path = self.root / "config.toml"
        source = (ROOT / "config/config.toml").read_text(encoding="utf-8")
        path.write_text(source.replace('"../data"', '"output"'), encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "main.py"), "--config", str(path), "--validate"],
                                cwd=self.root, capture_output=True,
                                env={**os.environ, "PYTHONIOENCODING": "cp1252"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("配置有效", result.stdout.decode("utf-8"))
        self.assertFalse((self.root / "output").exists())
        result = subprocess.run([sys.executable, str(ROOT / "main.py"), "--config", "missing.toml"],
                                cwd=self.root, capture_output=True)
        self.assertEqual(result.returncode, 1)

    def test_python_m_src_is_a_source_entry_point(self):
        path = self.root / "config.toml"
        source = (ROOT / "config/config.toml").read_text(encoding="utf-8")
        path.write_text(source.replace('"../data"', '"output"'), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "src", "--config", str(path), "--validate"],
            cwd=ROOT, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn("配置有效", result.stdout.decode("utf-8"))
        self.assertFalse((self.root / "output").exists())

    def test_project_is_source_only(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)
        self.assertNotIn("build-system", project)
        self.assertNotIn("scripts", project.get("project", {}))
        self.assertNotIn("setuptools", project.get("tool", {}))
        self.assertIs(project["tool"]["uv"]["package"], False)


if __name__ == "__main__":
    unittest.main()

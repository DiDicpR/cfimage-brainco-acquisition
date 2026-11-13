"""NAS names come from validated project metadata; no network required."""
import configparser
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.project_metadata import load_project_metadata
spec = importlib.util.spec_from_file_location("configure_assets", ROOT / "scripts/configure_assets.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class NasPathTests(unittest.TestCase):
    def test_shared_remote_matches_metadata(self):
        config = configparser.ConfigParser()
        config.read(ROOT / ".dvc/config", encoding="utf-8")
        self.assertEqual(config[next(section for section in config.sections() if "remote" in section and "nas" in section)]["url"],
                         f"s3://oi-task-assets/{load_project_metadata().unique_name}/dvc")

    def test_configuration_reads_metadata_before_running_dvc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".dvc").mkdir()
            (root / ".dvc/config").touch()
            metadata = root / "metadata.toml"
            valid = 'full_name = "ERP"\nunique_name = "ERP-20261022"\ndescription = "Test experiment"\n'
            metadata.write_text(valid, encoding="utf-8")
            with patch.object(configure, "ROOT", root), patch("sys.argv", ["configure_assets.py"]), patch.object(configure.subprocess, "run") as run:
                configure.main()
            self.assertIn("s3://oi-task-assets/ERP-20261022/dvc", run.call_args_list[0].args[0])
            self.assertTrue(all(call.kwargs["cwd"] == root for call in run.call_args_list))
            self.assertTrue(any(call.args[0][-2:] == ["allow_anonymous_login", "true"] for call in run.call_args_list))
            for bad in (valid.replace("ERP-20261022", "erp-20261022"), valid.replace("ERP-20261022", "ERP-20260231"), valid.replace("ERP-20261022", "../escape"), valid.replace('description = "Test experiment"', 'description = ""')):
                metadata.write_text(bad, encoding="utf-8")
                with self.subTest(bad=bad), patch.object(configure, "ROOT", root), patch("sys.argv", ["configure_assets.py"]), patch.object(configure.subprocess, "run") as run:
                    with self.assertRaises(SystemExit):
                        configure.main()
                    run.assert_not_called()
            metadata.unlink()
            with patch.object(configure, "ROOT", root), patch("sys.argv", ["configure_assets.py"]), patch.object(configure.subprocess, "run") as run:
                with self.assertRaises(SystemExit):
                    configure.main()
                run.assert_not_called()

    def run_cp1252(self, arguments):
        environment = os.environ.copy()
        # Override the CI defaults so UTF-8 mode cannot mask a regression.
        environment["PYTHONIOENCODING"] = "cp1252"
        environment["PYTHONUTF8"] = "0"
        return subprocess.run([sys.executable, *arguments], cwd=ROOT,
                              env=environment, capture_output=True, timeout=30)

    def test_cp1252_argument_error_preserves_chinese(self):
        result = self.run_cp1252([
            str(ROOT / "scripts/configure_assets.py"), "--unknown", "中文参数",
        ])
        self.assertEqual(result.returncode, 2)
        self.assertIn("中文参数", result.stderr.decode("utf-8"))
        self.assertNotIn(b"UnicodeEncodeError", result.stderr)

    def test_cp1252_invalid_metadata_is_readable_and_never_runs_dvc(self):
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "metadata.toml"
            text = 'full_name = "测试"\nunique_name = "bad"\ndescription = "测试说明"\n'
            metadata.write_text(text, encoding="utf-8")
            code = (
                "import importlib.util, sys; from pathlib import Path; "
                "from unittest.mock import patch; "
                f"spec=importlib.util.spec_from_file_location('configure', {str(ROOT / 'scripts/configure_assets.py')!r}); "
                "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
                "module.ROOT=Path(sys.argv[1]); sys.argv=['configure_assets.py']; "
                "guard=patch.object(module.subprocess, 'run', side_effect=AssertionError('DVC must not run')); "
                "guard.start(); module.main()"
            )
            result = self.run_cp1252(["-c", code, directory])
            self.assertEqual(result.returncode, 2)
            self.assertIn("unique_name 必须匹配", result.stderr.decode("utf-8"))
            self.assertNotIn(b"UnicodeEncodeError", result.stderr)
            self.assertEqual(metadata.read_text(encoding="utf-8"), text)

    def test_manual_directory_argument_is_rejected(self):
        with patch("sys.argv", ["configure_assets.py", "old-directory"]), patch.object(configure.subprocess, "run") as run:
            with self.assertRaises(SystemExit):
                configure.main()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

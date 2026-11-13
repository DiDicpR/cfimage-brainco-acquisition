"""Configure DVC using metadata.toml unique_name; never reads or writes credentials."""

import argparse
from pathlib import Path
import subprocess
import sys

BUCKET = "oi-task-assets"
ENDPOINT = "http://truenas-s658v2.omni-intel.cn:9000"


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.project_metadata import load_project_metadata


def main() -> None:
    # Keep Unicode diagnostics readable when Windows redirects output as cp1252.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="Enable local authenticated uploads with this AWS profile")
    args = parser.parse_args()
    root = ROOT
    try:
        unique_name = load_project_metadata(root / "metadata.toml").unique_name
    except (OSError, ValueError) as error:
        parser.error(f"Invalid metadata.toml: {error}")
    if not (root / ".dvc" / "config").is_file():
        parser.error("DVC is not initialized in this template.")

    def dvc(*arguments: str) -> None:
        subprocess.run([sys.executable, "-m", "dvc", *arguments], cwd=root, check=True)

    url = f"s3://{BUCKET}/{unique_name}/dvc"
    dvc("remote", "add", "--force", "--default", "nas", url)
    dvc("remote", "modify", "nas", "endpointurl", ENDPOINT)
    dvc("remote", "modify", "nas", "region", "us-east-1")
    dvc("remote", "modify", "nas", "ssl_verify", "true")
    dvc("remote", "modify", "nas", "allow_anonymous_login", "true")
    if args.profile:
        dvc("remote", "modify", "--local", "nas", "profile", args.profile)
        dvc("remote", "modify", "--local", "nas", "allow_anonymous_login", "false")
    print(f"Configured nas: {url} via {ENDPOINT}")
    print("Shared config uses anonymous reads; --profile enables local authenticated uploads.")
    print("Commit .dvc/config; credentials must remain local. No assets were uploaded.")


if __name__ == "__main__":
    main()

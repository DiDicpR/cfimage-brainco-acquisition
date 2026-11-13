"""Strict project metadata used for NAS directory names."""
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 compatibility
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = ROOT / "metadata.toml"
FIELDS = ("full_name", "unique_name", "description")
UNIQUE_NAME = re.compile(r"^[A-Z]{1,6}-[0-9]{8}$")


@dataclass(frozen=True)
class ProjectMetadata:
    full_name: str
    unique_name: str
    description: str


def load_project_metadata(path=METADATA_PATH):
    """Load top-level metadata and reject unknown or malformed values."""
    path = Path(path)
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    unknown = set(data) - set(FIELDS)
    if unknown:
        raise ValueError(f"未知项目元数据: {sorted(unknown)}")
    missing = set(FIELDS) - set(data)
    if missing:
        raise ValueError(f"缺少项目元数据: {sorted(missing)}")
    for field in FIELDS:
        value = data[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} 必须为非空字符串")
        if value != value.strip():
            raise ValueError(f"{field} 不能包含首尾空白")
    unique_name = data["unique_name"]
    if not UNIQUE_NAME.fullmatch(unique_name):
        raise ValueError("unique_name 必须匹配 ^[A-Z]{1,6}-[0-9]{8}$")
    try:
        datetime.strptime(unique_name.rsplit("-", 1)[1], "%Y%m%d")
    except ValueError as error:
        raise ValueError("unique_name 的日期部分必须是有效日期") from error
    return ProjectMetadata(**{field: data[field] for field in FIELDS})

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

REQUIRED_COLUMNS = {
    "image_id",
    "concept_id",
    "concept_name",
    "category_id",
    "file_path",
}


@dataclass(frozen=True)
class Stimulus:
    image_id: str
    concept_id: str
    concept_name: str
    category_id: str
    file_path: Path
    target_repetitions: int | None = None


def read_manifest(path: Path) -> list[Stimulus]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Manifest is missing columns: {sorted(missing)}")
        root = path.parent
        stimuli: list[Stimulus] = []
        seen: set[str] = set()
        for row in reader:
            image_id = row["image_id"].strip()
            if not image_id or image_id in seen:
                raise ValueError(f"image_id must be unique and non-empty: {image_id!r}")
            seen.add(image_id)
            image_path = Path(row["file_path"].strip())
            if not image_path.is_absolute():
                relative_image = image_path
                image_path = (root / relative_image).resolve()
                # Resolve the portable bundle's existing "simuli" layout;
                # keep frozen IDs and users' image directories intact.
                if not image_path.is_file() and relative_image.parts[0] == "stimuli":
                    portable = root / "simuli" / Path(*relative_image.parts[1:])
                    if portable.is_file():
                        image_path = portable.resolve()
            target_text = (row.get("target_repetitions") or "").strip()
            target = int(target_text) if target_text else None
            if target is not None and target < 1:
                raise ValueError(f"target_repetitions must be positive: {image_id}")
            stimuli.append(
                Stimulus(
                    image_id=image_id,
                    concept_id=row["concept_id"].strip(),
                    concept_name=row["concept_name"].strip(),
                    category_id=row["category_id"].strip() or row["concept_id"].strip() or image_id,
                    file_path=image_path,
                    target_repetitions=target,
                )
            )
    if not stimuli:
        raise ValueError("Manifest does not contain any stimuli")
    return stimuli

"""Frozen within-category positions, cumulative targets and read-only coverage."""
from __future__ import annotations

import csv
import hashlib
import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from .stimulus_manifest import read_manifest
from .planner import build_session_plan, PlanError


def parse_positions(value) -> list[int]:
    if isinstance(value, (list, tuple)):
        value = ','.join(str(v) for v in value)
    text = str(value).strip().replace('，', ',').replace('、', ',')
    if not re.fullmatch(r'\d+(?:\s*[,\s]\s*\d+)*', text):
        raise ValueError('图片序号请输入 1–10 的整数，用逗号分隔，例如 1,2,3。')
    result = sorted(set(int(v) for v in re.split(r'[,\s]+', text)))
    if not result or any(not 1 <= v <= 10 for v in result):
        raise ValueError('训练图片序号必须在 1–10 之间。')
    return result


def catalog_rows(image_root: Path, old_manifests: list[Path]):
    """Freeze alphabetical positions and preserve every previously assigned ID."""
    old_paths, old_ids = {}, {}
    for manifest in old_manifests:
        for stimulus in read_manifest(manifest):
            path = stimulus.file_path.resolve()
            if path in old_paths and old_paths[path] != stimulus.image_id:
                raise ValueError(f'历史图片 ID 冲突：{path}')
            if stimulus.image_id in old_ids and old_ids[stimulus.image_id] != path:
                raise ValueError(f'历史 ID 对应多张图片：{stimulus.image_id}')
            old_paths[path], old_ids[stimulus.image_id] = stimulus.image_id, path
    rows, seen = [], set()
    for split, expected in [('training_images', 10), ('test_images', 1)]:
        categories = sorted((image_root / split).iterdir(), key=lambda p: p.name.casefold())
        for category in categories:
            if not category.is_dir():
                continue
            images = sorted((p for p in category.rglob('*') if p.is_file() and p.suffix.lower() in {'.jpg', '.jpeg', '.png'}), key=lambda p: p.as_posix().casefold())
            if len(images) != expected:
                raise ValueError(f'{category.name} 应有 {expected} 张图片，实际 {len(images)} 张。')
            for position, path in enumerate(images, 1):
                path = path.resolve()
                relative = path.relative_to(image_root.resolve()).as_posix()
                identity = old_paths.get(path, 'image-v2-' + hashlib.sha256(relative.encode()).hexdigest()[:24])
                if identity in seen or (identity in old_ids and old_ids[identity] != path):
                    raise ValueError(f'图片 ID 冲突：{identity}')
                seen.add(identity)
                rows.append(dict(image_id=identity, concept_id=category.name, concept_name=category.name,
                    category_id=category.name, file_path='stimuli/' + relative,
                    target_repetitions=4 if split == 'training_images' else 10,
                    partition='training' if split == 'training_images' else 'test', image_position=position))
    if not set(old_paths).issubset({image_root.parent.resolve() / row['file_path'] for row in rows}):
        raise ValueError('完整图片目录缺少历史清单中的图片，不能迁移。')
    return rows


def read_catalog(path: Path):
    stimuli = read_manifest(path)
    with path.open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != len(stimuli):
        raise ValueError('序号清单长度异常。')
    metadata, slots, by_category = {}, set(), defaultdict(set)
    for stimulus, row in zip(stimuli, rows):
        split = row.get('partition')
        position = int(row.get('image_position', '0'))
        if split not in {'training', 'test'} or not 1 <= position <= (10 if split == 'training' else 1):
            raise ValueError('请使用带 partition / image_position 的完整序号清单。')
        key = split, stimulus.concept_id, position
        if key in slots:
            raise ValueError(f'同一类别出现重复图片序号：{key}')
        slots.add(key)
        by_category[(split, stimulus.concept_id)].add(position)
        metadata[stimulus.image_id] = (split, position)
    for (split, category), positions in by_category.items():
        if positions != set(range(1, 11) if split == 'training' else [1]):
            raise ValueError(f'{category} 的图片序号不完整。')
    if not any(split == 'training' for split, _ in by_category):
        raise ValueError('清单中没有训练类别。')
    return stimuli, metadata


def settings(experiment):
    config = experiment['collection']
    positions = parse_positions(config.get('positions', [1]))
    repeat = int(config.get('target_repetitions', 10))
    test_repeat = int(config.get('test_repetitions', 10))
    limit = int(config.get('max_blocks_per_session', 0))
    if not 1 <= repeat <= 100 or not 1 <= test_repeat <= 100:
        raise ValueError('目标有效重复次数必须在 1–100 之间。')
    if not 0 <= limit <= 100:
        raise ValueError('区块上限必须在 0–100 之间，0 表示完整选定范围。')
    return positions, repeat, test_repeat, limit


def select_stimuli(stimuli, metadata, experiment):
    positions, repeat, test_repeat, _ = settings(experiment)
    include_test = bool(experiment['collection'].get('include_test', False))
    return [replace(s, target_repetitions=repeat if metadata[s.image_id][0] == 'training' else test_repeat)
            for s in stimuli if (metadata[s.image_id][0] == 'training' and metadata[s.image_id][1] in positions)
            or (metadata[s.image_id][0] == 'test' and include_test)]


def coverage(stimuli, metadata, progress, repeat, test_repeat):
    buckets = defaultdict(list)
    for stimulus in stimuli:
        split, position = metadata[stimulus.image_id]
        goal = repeat if split == 'training' else test_repeat
        done = progress.get(stimulus.image_id, 0)
        buckets[(split, position)].append(dict(category=stimulus.concept_id, image_id=stimulus.image_id,
            filename=stimulus.file_path.name, valid=done, target=goal, missing=max(0, goal - done)))
    result = []
    for (split, position), details in sorted(buckets.items(), key=lambda pair: (pair[0][0] != 'training', pair[0][1])):
        result.append(dict(partition=split, position=position, total_categories=len(details),
            acquired_categories=sum(d['valid'] > 0 for d in details),
            completed_categories=sum(d['missing'] == 0 for d in details),
            remaining_trials=sum(d['missing'] for d in details), details=details))
    return result


def position_summary(stimuli, metadata, progress, experiment):
    positions, repeat, test_repeat, limit = settings(experiment)
    selected = select_stimuli(stimuli, metadata, experiment)
    remaining = sum(max(0, s.target_repetitions - progress.get(s.image_id, 0)) for s in selected)
    total = sum(s.target_repetitions for s in selected)
    per_block = int(experiment['blocks']['trials_per_block'])
    trial_limit = min(remaining, limit * per_block) if limit else remaining
    blocks = min(limit or 10**9, max(math.ceil(trial_limit / per_block),
        max((s.target_repetitions - progress.get(s.image_id, 0) for s in selected), default=0)))
    seconds = trial_limit * (experiment['stimulus']['image_duration_ms'] + experiment['stimulus']['isi_ms']) / 1000
    seconds += max(0, blocks - 1) * experiment['blocks']['minimum_rest_sec']
    if remaining:
        seconds += sum(experiment['baseline'][key] for key in ('eyes_open_sec', 'transition_sec', 'eyes_closed_sec'))
    return dict(strategy='positions', positions=positions, target_repetitions=repeat,
        test_repetitions=test_repeat, include_test=bool(experiment['collection'].get('include_test', False)),
        selected_images=len(selected), total_target=total, total_completed=total - remaining,
        group_remaining=remaining, trial_limit=trial_limit, max_blocks=limit,
        estimated_minutes=math.ceil(seconds / 60), coverage=coverage(stimuli, metadata, progress, repeat, test_repeat))


def query_positions(experiment, participant, mode):
    """Read only: never repair sessions, create tables or connect EEG hardware."""
    if experiment.get('dual_image', {}).get('enabled', False):
        from .paired_progress import preview_paired
        return preview_paired(experiment, participant, mode)
    from .collection import saved_progress
    from ..qc.integrity import validate_participant_manifest
    from ..storage.database import sha256_file
    manifest = Path(experiment['paths']['stimulus_manifest']).resolve()
    stimuli, metadata = read_catalog(manifest)
    database = None
    try:
        path = Path(experiment['paths']['database']).resolve()
        if path.is_file():
            database = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            database.row_factory = sqlite3.Row
            validate_participant_manifest(database, participant_id=participant,
                dataset_root=Path(experiment['paths']['dataset_root']).resolve(),
                current_sha256=sha256_file(manifest), current_stimuli=stimuli,
                allow_identity_preserving_change=True)
        progress, rejected = saved_progress(database, participant, mode, Path(experiment['paths']['dataset_root']).resolve())
        summary = position_summary(stimuli, metadata, progress, experiment)
        previous = database.execute('SELECT session_id,status FROM sessions WHERE participant_id=? AND run_mode=? ORDER BY start_time DESC LIMIT 1', (participant, mode)).fetchone() if database else None
        summary.update(participant_id=participant, run_mode=mode, unverified_trials=rejected,
                       previous_session=dict(previous) if previous else None)
        return summary
    finally:
        if database:
            database.close()


def build_position_plan(stimuli, metadata, progress, experiment, seed):
    summary = position_summary(stimuli, metadata, progress, experiment)
    if not summary['group_remaining']:
        raise PlanError('所选图片序号已达到目标有效次数，无需重复采集。可选择其他序号或提高目标次数。')
    selected = [s for s in select_stimuli(stimuli, metadata, experiment) if progress.get(s.image_id, 0) < s.target_repetitions]
    if any(not s.file_path.is_file() for s in selected):
        raise FileNotFoundError('所选图片中有文件缺失，请恢复图片目录后重试。')
    gap = int(experiment['repetition']['minimum_image_gap'])
    if len(selected) <= gap:
        selected = [replace(s, target_repetitions=progress.get(s.image_id, 0) + 1) for s in selected]
    needed = [s.target_repetitions - progress.get(s.image_id, 0) for s in selected]
    blocks = max(math.ceil(sum(needed) / experiment['blocks']['trials_per_block']), max(needed))
    if summary['max_blocks']:
        blocks = min(blocks, summary['max_blocks'])
    plan = build_session_plan(selected, progress=progress, repetitions=summary['target_repetitions'],
        blocks=blocks, trials_per_block=int(experiment['blocks']['trials_per_block']),
        minimum_image_gap=gap, attention=experiment['attention_probe'], seed=seed,
        plan_version=str(experiment['randomization']['plan_version']) + '-positions-v1',
        prioritize_remaining=True)
    summary.pop('coverage')
    summary.update(planned_trials=len(plan.trials), planned_blocks=plan.block_count,
        image_positions={s.image_id: {'partition': metadata[s.image_id][0], 'position': metadata[s.image_id][1]} for s in selected})
    return plan, summary

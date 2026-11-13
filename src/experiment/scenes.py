"""Scene catalog adapter. Timing, planning, EEG and saved-progress engines are reused."""
from __future__ import annotations
import csv
import sqlite3
from copy import deepcopy
from pathlib import Path
from .stimulus_manifest import read_manifest
from .positions import position_summary, build_position_plan


def read_scene_catalog(path):
    path = Path(path)
    stimuli = read_manifest(path)
    with path.open(encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle))
    metadata = {}
    slots = set()
    for s, row in zip(stimuli, rows, strict=True):
        for name in ('scene', 'view', 'object_id', 'object_name', 'semantic_label'):
            if not row.get(name):
                raise ValueError(f'场景清单缺少 {name}: {s.image_id}')
        if row['view'] not in {'平视', '俯视'}:
            raise ValueError('未知视角')
        slot = row['scene'], row['view'], row['object_id']
        if slot in slots:
            raise ValueError(f'场景/视角/实物重复: {slot}')
        slots.add(slot)
        if s.concept_id != row['object_id']:
            raise ValueError('物体编号与概念编号不一致')
        metadata[s.image_id] = row
    return stimuli, metadata


def settings(experiment):
    cfg = experiment['collection']
    repeat = int(cfg.get('target_repetitions', 10))
    limit = int(cfg.get('max_blocks_per_session', 0))
    if not 1 <= repeat <= 100 or not 0 <= limit <= 100:
        raise ValueError('目标次数须为 1-100，区块上限须为 0-100（0 为完整范围）。')
    selected = cfg.get('selected_image_ids')
    if selected is not None and (not isinstance(selected, list) or not selected or
            any(not isinstance(x, str) for x in selected) or len(selected) != len(set(selected))):
        raise ValueError('请至少勾选一张图片，图片 ID 不能重复。')
    return repeat, limit


def scope(stimuli, metadata, experiment):
    settings(experiment)
    ids = experiment['collection'].get('selected_image_ids')
    selected = set(metadata) if ids is None else set(ids)
    if selected - set(metadata):
        raise ValueError('选中的图片不在当前场景清单中，请重新选择。')
    items = [s for s in stimuli if s.image_id in selected]
    if not items:
        raise ValueError('请至少勾选一张图片。')
    # One selected manifest photograph is one scheduler unit, as in the
    # original one-position-per-THINGS-category paradigm. Physical object and
    # semantic category remain explicit catalog columns, never inferred from order.
    proxy = deepcopy(experiment)
    proxy['collection'].update(strategy='positions', positions=[1], include_test=False,
                               test_repetitions=10)
    return items, {s.image_id: ('training', 1) for s in items}, proxy


def scene_summary(stimuli, metadata, progress, experiment):
    items, slots, proxy = scope(stimuli, metadata, experiment)
    result = position_summary(items, slots, progress, proxy)
    result.pop('coverage', None)
    result.pop('positions', None)
    result.update(strategy='scenes', scenes=sorted({metadata[s.image_id]['scene'] for s in items}),
                  views=sorted({metadata[s.image_id]['view'] for s in items}),
                  selected_image_ids=[s.image_id for s in items],
                  limited_by_image_gap=sum(progress.get(s.image_id, 0) < result['target_repetitions'] for s in items)
                      <= int(experiment['repetition']['minimum_image_gap']))
    return result


def build_scene_plan(stimuli, metadata, progress, experiment, seed):
    if experiment.get('fixed_schedule'):
        from .fixed_schedule import build_fixed_plan
        return build_fixed_plan(stimuli, metadata, progress, experiment)
    items, slots, proxy = scope(stimuli, metadata, experiment)
    plan, original = build_position_plan(items, slots, progress, proxy, seed)
    result = scene_summary(stimuli, metadata, progress, experiment)
    result.update(planned_trials=len(plan.trials), planned_blocks=plan.block_count,
                  image_metadata={s.image_id: {k: metadata[s.image_id][k] for k in
                      ('scene', 'view', 'object_id', 'object_name', 'semantic_label')} for s in items})
    return plan, result


def preview_scenes(experiment, participant, mode):
    if experiment.get('fixed_schedule'):
        from .fixed_schedule import preview_fixed
        return preview_fixed(experiment, participant, mode)
    from .collection import saved_progress
    from ..qc.integrity import validate_participant_manifest
    from ..storage.database import sha256_file
    manifest = Path(experiment['paths']['stimulus_manifest']).resolve()
    stimuli, metadata = read_scene_catalog(manifest)
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
        progress, rejected = saved_progress(database, participant, mode,
            Path(experiment['paths']['dataset_root']).resolve())
        result = scene_summary(stimuli, metadata, progress, experiment)
        previous = database.execute('SELECT session_id,status FROM sessions WHERE participant_id=? AND run_mode=? ORDER BY start_time DESC LIMIT 1',
            (participant, mode)).fetchone() if database else None
        result.update(previous_session=dict(previous) if previous else None, unverified_trials=rejected)
        return result
    finally:
        if database:
            database.close()

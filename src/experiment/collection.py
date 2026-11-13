"""Stable image groups and mode-specific, saved-data-verified resume progress."""
from __future__ import annotations

import hashlib
import json
import math
import mmap
import sqlite3
import struct
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

from .planner import PlanError, build_session_plan
from .stimulus_manifest import read_manifest


def image_groups(stimuli, count: int) -> dict[str, int]:
    """Distribute each category over groups and balance total repetition load."""
    if not 1 <= count <= 100:
        raise ValueError('分组数必须在 1–100 之间。')
    categories = defaultdict(list)
    for stimulus in stimuli:
        categories[stimulus.category_id].append(stimulus)
    digest = lambda value: hashlib.sha256(value.encode('utf-8')).hexdigest()
    loads = [0] * count
    mapping = {}
    for category in sorted(categories, key=digest):
        used = Counter()
        for stimulus in sorted(categories[category], key=lambda s: digest(s.image_id)):
            group = min(range(count), key=lambda i: (used[i], loads[i], i))
            mapping[stimulus.image_id] = group + 1
            used[group] += 1
            loads[group] += stimulus.target_repetitions or 4
    return mapping


def group_mapping(database, participant, stimuli, count, *, persist=False):
    exists = database is not None and database.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_groups'"
    ).fetchone()
    row = database.execute('SELECT * FROM collection_groups WHERE participant_id=?', (participant,)).fetchone() if exists else None
    if row:
        mapping = json.loads(row['mapping_json'])
        if row['group_count'] != count or set(mapping) != {s.image_id for s in stimuli}:
            raise ValueError('该被试已有固定分组，当前图片清单或分组数发生变化。请恢复原清单/分组设置，不能自动重分组。')
        return mapping
    mapping = image_groups(stimuli, count)
    if persist:
        with database:
            database.execute('''CREATE TABLE IF NOT EXISTS collection_groups (
                participant_id TEXT PRIMARY KEY, group_count INTEGER NOT NULL,
                mapping_json TEXT NOT NULL)''')
            database.execute('INSERT INTO collection_groups VALUES (?, ?, ?)',
                             (participant, count, json.dumps(mapping, sort_keys=True)))
    return mapping


def saved_progress(database, participant, mode, dataset_root, *, by_target_side=False, by_schedule=False):
    """Never let simulation or unverified/missing EEG consume formal targets.

    For a process crash the float32 header may still say shape=0, complete=false;
    use actual file length and the saved onset/offset codes, without editing it.
    """
    progress = Counter()
    rejected = 0
    if database is None:
        return {}, 0
    rows = [dict(row) for row in database.execute('''SELECT t.trial_id,t.image_id,t.session_id,s.plan_path
        FROM trials t JOIN sessions s USING(session_id)
        WHERE s.participant_id=? AND s.run_mode=? AND t.run_mode=?
        AND (t.status='valid' OR (? AND t.status='invalid'))
        ORDER BY s.start_time,t.global_index''', (participant, mode, mode, by_schedule))]
    # The old per-trial correlated subquery scanned/sorted the event table for
    # every retained trial (minutes at ~10k trials). Read each relevant session's
    # onsets once; preserve the latest-event rule without changing verification.
    onset_metadata = {}
    if by_schedule or by_target_side:
        for session_id in dict.fromkeys(row['session_id'] for row in rows):
            for event in database.execute('''SELECT trial_id,metadata_json FROM events
                    WHERE session_id=? AND event_type='IMAGE_ONSET' ORDER BY event_seq''', (session_id,)):
                onset_metadata[event['trial_id']] = event['metadata_json']
    for row in rows:
        row['onset_metadata'] = onset_metadata.get(row['trial_id'])
    sessions = defaultdict(list)
    def progress_key(row):
        if by_schedule:
            meta = json.loads(row['onset_metadata'] or '{}')
            if not meta.get('schedule_sha256') or not isinstance(meta.get('schedule_trial_id'), int):
                raise ValueError('固定排程续采缺少原始试次编号，不能用图片次数推算。')
            return meta['schedule_sha256'], meta['schedule_trial_id'], row['image_id']
        if not by_target_side:
            return row['image_id']
        try:
            meta = json.loads(row['onset_metadata'] or '{}')
            if not isinstance(meta, dict):
                return None
            side = meta.get('target_side')
            if (not meta.get('pair_id') or side not in {'left', 'right'}
                    or meta.get(side + '_image_id') != row['image_id']):
                return None
            return row['image_id'], side
        except (ValueError, TypeError):
            return None
    for row in rows:
        if progress_key(row) is not None:
            sessions[row['session_id']].append(row)
    for session_id, trials in sessions.items():
        if mode == 'simulation':
            progress.update(progress_key(t) for t in trials)
            continue
        events = defaultdict(dict)
        for row in database.execute('''SELECT e.trial_id,e.event_type,e.trigger_code,
                y.eeg_sample_index,y.sync_quality FROM events e JOIN eeg_sync y USING(event_id)
                WHERE e.session_id=? AND e.event_type IN ('IMAGE_ONSET','IMAGE_OFFSET')''', (session_id,)):
            events[row['trial_id']][row['event_type']] = row
        session_dir = Path(dataset_root) / Path(trials[0]['plan_path']).parent
        eeg = session_dir / 'eeg' / f'{participant}_{session_id}_task-things_eeg.edf'
        native = None
        if mode == 'brainco':
            recording = database.execute('SELECT edf_path, source_transport FROM eeg_recordings WHERE session_id=? AND status=?',
                (session_id, 'complete')).fetchone()
            if recording and recording['source_transport'] == 'brainco-full-stream-native':
                from ..acquisition_devices.brainco_native import NativeAudit
                try:
                    native = NativeAudit(Path(dataset_root) / recording['edf_path'])
                except (OSError, ValueError, sqlite3.Error):
                    rejected += len(trials)
                    continue
        raw = eeg.with_suffix('.float32')
        handle = view = None
        channels = trigger = 0
        if mode == 'neuracle' and raw.is_file():
            try:
                meta = json.loads(raw.with_suffix('.float32.json').read_text(encoding='utf-8'))
                channels = int(meta['shape'][1])
                trigger = meta['channel_names'].index('TRG')
                if channels <= trigger or meta['dtype'] != '<f4' or meta['layout'] != 'sample_major':
                    raise ValueError('unsupported raw layout')
                handle = raw.open('rb')
                view = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                if handle:
                    handle.close()
                handle = None
        try:
            for trial in trials:
                pair = events[trial['trial_id']]
                valid = set(pair) == {'IMAGE_ONSET', 'IMAGE_OFFSET'}
                if valid:
                    onset, offset = pair['IMAGE_ONSET'], pair['IMAGE_OFFSET']
                    expected = 'hardware_verified' if mode == 'neuracle' else 'software_estimated'
                    valid = all(e['sync_quality'] == expected and e['eeg_sample_index'] is not None for e in pair.values())
                    valid = valid and 0 < onset['eeg_sample_index'] < offset['eeg_sample_index']
                if valid and mode == 'neuracle':
                    for event in pair.values():
                        try:
                            if view is not None:
                                position = ((event['eeg_sample_index'] - 1) * channels + trigger) * 4
                                if event['eeg_sample_index'] > len(view) // (channels * 4):
                                    raise ValueError('sample not saved')
                                valid = valid and struct.unpack_from('<f', view, position)[0] == event['trigger_code']
                            else:
                                from ..qc.saved_trigger import verify_saved_trigger
                                verify_saved_trigger(eeg, event['trigger_code'], event['eeg_sample_index'])
                        except (OSError, ValueError, RuntimeError, struct.error):
                            valid = False
                elif valid and native is not None:
                    try:
                        for event in pair.values():native.verify_marker(event['trigger_code'], event['eeg_sample_index'])
                    except ValueError:valid = False
                elif valid:
                    valid = eeg.is_file() and eeg.stat().st_size > 256
                if valid:
                    progress[progress_key(trial)] += 1
                else:
                    rejected += 1
        finally:
            if view is not None:
                view.close()
            if handle is not None:
                handle.close()
    if by_target_side:
        result = {}
        for (image_id, side), count in progress.items():
            result.setdefault(image_id, {'left': 0, 'right': 0})[side] = count
        return result, rejected
    return dict(progress), rejected


def collection_summary(stimuli, progress, mapping, experiment, *, rejected=0, previous=None):
    settings = experiment.get('collection', {})
    count = int(settings.get('group_count', 10))
    completed = [0] * count
    targets = [0] * count
    for stimulus in stimuli:
        group = mapping[stimulus.image_id] - 1
        target = stimulus.target_repetitions or int(experiment['repetition']['minimum_valid_repetitions'])
        targets[group] += target
        completed[group] += min(target, progress.get(stimulus.image_id, 0))
    selected = int(settings.get('group_index', 0))
    if not 0 <= selected <= count:
        raise ValueError('图片组编号超出范围。')
    if not selected:
        selected = next((i + 1 for i in range(count) if completed[i] < targets[i]), 0)
    remaining = targets[selected - 1] - completed[selected - 1] if selected else 0
    max_blocks = int(settings.get('max_blocks_per_session', 5))
    if not 1 <= max_blocks <= 100:
        raise ValueError('每次采集区块上限必须在 1–100 之间。')
    budget = max_blocks * int(experiment['blocks']['trials_per_block'])
    trial_limit = min(budget, remaining)
    seconds = trial_limit * (int(experiment['stimulus']['image_duration_ms']) + int(experiment['stimulus']['isi_ms'])) / 1000
    seconds += max(0, math.ceil(trial_limit / int(experiment['blocks']['trials_per_block'])) - 1) * int(experiment['blocks']['minimum_rest_sec'])
    seconds += sum(int(experiment['baseline'][key]) for key in ('eyes_open_sec', 'transition_sec', 'eyes_closed_sec')) if trial_limit else 0
    return dict(group_count=count, group_index=selected, group_targets=targets, group_completed=completed,
                total_target=sum(targets), total_completed=sum(completed), group_remaining=remaining,
                max_blocks=max_blocks, trial_limit=trial_limit, estimated_minutes=math.ceil(seconds / 60),
                unverified_trials=rejected, previous_session=previous)


def preview_collection(experiment, participant, mode):
    if experiment.get('dual_image', {}).get('enabled', False):
        from .paired_progress import preview_paired
        return preview_paired(experiment, participant, mode)
    if experiment.get('collection', {}).get('strategy') == 'scenes':
        from .scenes import preview_scenes
        return preview_scenes(experiment, participant, mode)
    if experiment.get('collection', {}).get('strategy') == 'positions':
        from .positions import query_positions
        return query_positions(experiment, participant, mode)
    stimuli = read_manifest(Path(experiment['paths']['stimulus_manifest']).resolve())
    db_path = Path(experiment['paths']['database']).resolve()
    database = None
    try:
        if db_path.is_file():
            database = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
            database.row_factory = sqlite3.Row
        mapping = group_mapping(database, participant, stimuli, int(experiment.get('collection', {}).get('group_count', 10)))
        progress, rejected = saved_progress(database, participant, mode, Path(experiment['paths']['dataset_root']).resolve())
        previous = database.execute('SELECT session_id,status FROM sessions WHERE participant_id=? AND run_mode=? ORDER BY start_time DESC LIMIT 1', (participant, mode)).fetchone() if database else None
        return collection_summary(stimuli, progress, mapping, experiment, rejected=rejected,
                                  previous=dict(previous) if previous else None)
    finally:
        if database:
            database.close()


def build_collection_plan(stimuli, progress, mapping, experiment, *, seed):
    summary = collection_summary(stimuli, progress, mapping, experiment)
    if not summary['group_remaining']:
        raise PlanError('所选图片组已完成；请选择自动续采或其他未完成组。' if summary['group_index'] else '该被试的所有图片组均已完成。')
    selected = [s for s in stimuli if mapping[s.image_id] == summary['group_index'] and
                progress.get(s.image_id, 0) < (s.target_repetitions or experiment['repetition']['minimum_valid_repetitions'])]
    gap = int(experiment['repetition']['minimum_image_gap'])
    # A small tail cannot repeat safely in the same run. Schedule each remaining
    # image once; future sessions finish its targets instead of relaxing the gap.
    if len(selected) <= gap:
        selected = [replace(s, target_repetitions=progress.get(s.image_id, 0) + 1) for s in selected]
    needed = [int(s.target_repetitions or experiment['repetition']['minimum_valid_repetitions']) - progress.get(s.image_id, 0) for s in selected]
    per_block = int(experiment['blocks']['trials_per_block'])
    blocks = min(summary['max_blocks'], max(math.ceil(sum(needed) / per_block), max(needed)))
    plan = build_session_plan(selected, progress=progress,
        repetitions=int(experiment['repetition']['minimum_valid_repetitions']), blocks=blocks,
        trials_per_block=per_block, minimum_image_gap=gap, attention=experiment['attention_probe'],
        seed=seed, plan_version=str(experiment['randomization']['plan_version']) + '-grouped-v1',
        prioritize_remaining=True)
    summary['planned_trials'] = len(plan.trials)
    summary['planned_blocks'] = plan.block_count
    return plan, summary

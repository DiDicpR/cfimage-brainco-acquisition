"""Two-image targets layered onto the existing positions/block/probe planner.

Progress is deliberately side-specific and supplied by verified paired records,
never by the original single-image progress counters. The inherited image-gap
constraint concerns repetitions of the attended target, not distractor exposure.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace

from .planner import PlanError, SessionPlan, build_session_plan
from .positions import select_stimuli as select_position_stimuli
from .positions import settings as position_settings


SIDES = ("left", "right")


def stable_pair_id(first_image_id: str, second_image_id: str) -> str:
    """Identify the same two source images independently of display order."""
    payload = json.dumps(sorted((first_image_id, second_image_id)), ensure_ascii=False,
                         separators=(",", ":"))
    return "pair-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def select_stimuli(stimuli, metadata, experiment):
    """Reuse frozen per-category positions, then optionally restrict concepts."""
    selected = select_position_stimuli(stimuli, metadata, experiment)
    concepts = experiment.get("dual_image", {}).get("concept_ids", [])
    if not isinstance(concepts, (list, tuple)) or any(not isinstance(c, str) or not c for c in concepts):
        raise PlanError("dual_image.concept_ids must be a list of complete concept IDs")
    if len(set(concepts)) != len(concepts):
        raise PlanError("dual_image.concept_ids contains duplicate concept IDs")
    if concepts:
        unknown = set(concepts) - {s.concept_id for s in selected}
        if unknown:
            raise PlanError(f"Selected positions do not contain concept IDs: {sorted(unknown)}")
        selected = [s for s in selected if s.concept_id in set(concepts)]
    if not selected:
        raise PlanError("No images are selected for paired presentation")
    if len({s.concept_id for s in selected}) < 2:
        raise PlanError("Paired presentation requires at least two different selected concepts")
    if len({s.image_id for s in selected}) != len(selected):
        raise PlanError("Selected paired image IDs must be unique")
    for stimulus in selected:
        target = stimulus.target_repetitions
        if target is None or target < 2 or target % 2:
            raise PlanError(f"Paired target repetitions must be positive and even: {stimulus.image_id}")
    _category_partners(selected, experiment)
    return selected


def _category_partners(selected, experiment):
    settings = experiment.get("dual_image", {})
    pairing = settings.get("pairing", "random")
    if pairing not in {"random", "fixed"}:
        raise PlanError("dual_image.pairing must be random or fixed")
    if pairing == "random":
        return None
    concepts = {s.concept_id for s in selected}
    pairs = settings.get("fixed_category_pairs", [])
    if not isinstance(pairs, (list, tuple)) or not pairs:
        raise PlanError("Fixed pairing requires complete fixed_category_pairs")
    partners = {}
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise PlanError("Each fixed category pair must contain exactly two concept IDs")
        first, second = pair
        if not isinstance(first, str) or not isinstance(second, str):
            raise PlanError("Fixed category pairs must use complete string concept IDs")
        if first == second:
            raise PlanError("A fixed category pair must contain different concepts")
        if first not in concepts or second not in concepts:
            raise PlanError("Fixed category pairs contain concepts outside the selected scope")
        if first in partners or second in partners:
            raise PlanError("Every selected concept must occur exactly once in fixed category pairs")
        partners[first], partners[second] = second, first
    if set(partners) != concepts:
        raise PlanError("Fixed category pairs must cover every selected concept exactly once")
    return partners


def _side_counts(progress):
    if not isinstance(progress, Mapping):
        raise PlanError("Paired progress must map image IDs to left/right valid counts")
    result = {}
    for image_id, counts in progress.items():
        if not isinstance(counts, Mapping) or set(counts) - set(SIDES):
            raise PlanError("Paired progress must contain independent left/right counts; single-image counters are unsupported")
        result[image_id] = {}
        for side in SIDES:
            value = counts.get(side, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PlanError("Paired side progress must be a non-negative integer")
            result[image_id][side] = value
    return result


def _missing(stimulus, progress):
    quota = stimulus.target_repetitions // 2
    counts = progress.get(stimulus.image_id, {})
    return {side: max(0, quota - counts.get(side, 0)) for side in SIDES}


def paired_summary(stimuli, metadata, progress, experiment):
    selected = select_stimuli(stimuli, metadata, experiment)
    progress = _side_counts(progress)
    positions, repeat, test_repeat, limit = position_settings(experiment)
    buckets = defaultdict(list)
    total_target = remaining = 0
    for stimulus in selected:
        quota = stimulus.target_repetitions // 2
        counts = progress.get(stimulus.image_id, {})
        missing = _missing(stimulus, progress)
        amount = sum(missing.values())
        total_target += stimulus.target_repetitions
        remaining += amount
        split, position = metadata[stimulus.image_id]
        buckets[(split, position)].append(dict(
            category=stimulus.concept_id, image_id=stimulus.image_id,
            filename=stimulus.file_path.name, target=stimulus.target_repetitions,
            valid=stimulus.target_repetitions - amount, missing=amount,
            valid_left=counts.get("left", 0), valid_right=counts.get("right", 0),
            target_per_side=quota, missing_left=missing["left"], missing_right=missing["right"],
        ))
    coverage = []
    for (split, position), details in sorted(buckets.items(), key=lambda item: (item[0][0] != "training", item[0][1])):
        coverage.append(dict(partition=split, position=position, total_categories=len(details),
            acquired_categories=sum(d["valid"] > 0 for d in details),
            completed_categories=sum(d["missing"] == 0 for d in details),
            remaining_trials=sum(d["missing"] for d in details), details=details))
    per_block = int(experiment["blocks"]["trials_per_block"])
    gap = int(experiment["repetition"]["minimum_image_gap"])
    if per_block < 1 or gap < 0:
        raise PlanError("Block capacity must be positive and minimum image gap non-negative")
    target_counts = [sum(_missing(s, progress).values()) for s in selected]
    incomplete_count = sum(value > 0 for value in target_counts)
    limited_by_gap = bool(incomplete_count and incomplete_count <= gap)
    planned_counts = [min(value, 1) if limited_by_gap else value for value in target_counts]
    trial_limit = min(sum(planned_counts), limit * per_block) if limit else sum(planned_counts)
    blocks = min(limit or 10**9, max(math.ceil(trial_limit / per_block), max(planned_counts, default=0)))
    dual = experiment.get("dual_image", {})
    trial_ms = (int(dual.get("cue_duration_ms", 500)) + int(dual.get("fixation_duration_ms", 500))
                + int(experiment["stimulus"]["image_duration_ms"]) + int(experiment["stimulus"]["isi_ms"]))
    seconds = trial_limit * trial_ms / 1000
    seconds += max(0, blocks - 1) * experiment["blocks"]["minimum_rest_sec"]
    if trial_limit:
        seconds += sum(experiment["baseline"][key] for key in ("eyes_open_sec", "transition_sec", "eyes_closed_sec"))
    return dict(strategy="paired", paradigm="dual_image", pairing=dual.get("pairing", "random"),
        positions=positions, target_repetitions=repeat, test_repetitions=test_repeat,
        include_test=bool(experiment["collection"].get("include_test", False)),
        concept_ids=sorted({s.concept_id for s in selected}), selected_images=len(selected),
        total_target=total_target, total_completed=total_target - remaining,
        group_remaining=remaining, trial_limit=trial_limit, max_blocks=limit,
        estimated_minutes=math.ceil(seconds / 60), coverage=coverage,
        limited_by_image_gap=limited_by_gap, minimum_gap_applies_to="target_image")


def build_paired_plan(stimuli, metadata, progress, experiment, seed):
    """Plan only missing target-side trials, keeping the existing block rules.

    A small remaining image set follows the original one-target-per-image visit
    behavior rather than reducing the configured gap or inserting filler trials.
    Completed images remain available as distractors but never earn target credit.
    """
    stimuli = list(stimuli)
    selected = select_stimuli(stimuli, metadata, experiment)
    progress = _side_counts(progress)
    summary = paired_summary(stimuli, metadata, progress, experiment)
    if not summary["group_remaining"]:
        raise PlanError("All selected images have reached both left/right paired targets")
    if any(not s.file_path.is_file() for s in selected):
        raise FileNotFoundError("A selected paired image file is missing")
    rng = random.Random(seed)
    totals = {s.image_id: sum(progress.get(s.image_id, {}).values()) for s in selected}
    target_stimuli = []
    side_slots = {}
    for stimulus in selected:
        missing = _missing(stimulus, progress)
        slots = [side for side in SIDES for _ in range(missing[side])]
        if not slots:
            continue
        rng.shuffle(slots)
        side_slots[stimulus.image_id] = slots
        repetitions_this_visit = 1 if summary["limited_by_image_gap"] else len(slots)
        target_stimuli.append(replace(stimulus,
            target_repetitions=totals[stimulus.image_id] + repetitions_this_visit))
    needed = [s.target_repetitions - totals[s.image_id] for s in target_stimuli]
    per_block = int(experiment["blocks"]["trials_per_block"])
    blocks = max(math.ceil(sum(needed) / per_block), max(needed))
    if summary["max_blocks"]:
        blocks = min(blocks, summary["max_blocks"])
    plan = build_session_plan(target_stimuli, progress=totals,
        repetitions=summary["target_repetitions"], blocks=blocks,
        trials_per_block=per_block, minimum_image_gap=int(experiment["repetition"]["minimum_image_gap"]),
        attention=experiment["attention_probe"], seed=seed,
        plan_version=str(experiment["randomization"]["plan_version"]) + "-paired-v1",
        prioritize_remaining=True)
    targets = {s.image_id: s for s in selected}
    groups = defaultdict(list)
    for stimulus in sorted(selected, key=lambda s: s.image_id):
        groups[stimulus.concept_id].append(stimulus)
    concepts = sorted(groups)
    fixed = _category_partners(selected, experiment)
    trials = []
    for trial in plan.trials:
        target = targets[trial.image_id]
        partner_concept = fixed[target.concept_id] if fixed else rng.choice(
            [concept for concept in concepts if concept != target.concept_id])
        distractor = rng.choice(groups[partner_concept])
        side = side_slots[trial.image_id].pop()
        left, right = (target, distractor) if side == "left" else (distractor, target)
        trials.append(replace(trial,
            pair_id=stable_pair_id(left.image_id, right.image_id),
            left_object_id=left.concept_id, right_object_id=right.concept_id,
            target_object_id=target.concept_id, target_side=side,
            left_image_id=left.image_id, right_image_id=right.image_id,
            left_file_path=str(left.file_path), right_file_path=str(right.file_path)))
    summary.pop("coverage")
    summary.update(planned_trials=len(trials), planned_blocks=plan.block_count,
        trial_limit=len(trials), image_positions={s.image_id: {"partition": metadata[s.image_id][0],
            "position": metadata[s.image_id][1]} for s in selected})
    return SessionPlan(plan.seed, plan.plan_version, tuple(trials)), summary

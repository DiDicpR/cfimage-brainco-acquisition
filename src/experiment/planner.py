from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from itertools import pairwise
from functools import cached_property

from .stimulus_manifest import Stimulus


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class PlannedTrial:
    global_index: int
    block_index: int
    trial_index: int
    image_id: str
    concept_id: str
    category_id: str
    file_path: str
    repetition_index: int
    probe: bool = False
    arithmetic_expression: str | None = None
    arithmetic_is_correct: bool | None = None
    # In paired trials, the original image fields identify the attended target.
    pair_id: str | None = None
    left_object_id: str | None = None
    right_object_id: str | None = None
    target_object_id: str | None = None
    target_side: str | None = None
    left_image_id: str | None = None
    right_image_id: str | None = None
    left_file_path: str | None = None
    right_file_path: str | None = None
    schedule_trial_id: int | None = None
    schedule_cycle: int | None = None
    schedule_cycle_position: int | None = None
    schedule_sha256: str | None = None


@dataclass(frozen=True)
class SessionPlan:
    seed: int
    plan_version: str
    trials: tuple[PlannedTrial, ...]

    @cached_property
    def block_count(self) -> int:
        return max((trial.block_index for trial in self.trials), default=0)

    @cached_property
    def _blocks(self):
        result = {}
        for trial in self.trials:
            result.setdefault(trial.block_index, []).append(trial)
        return {key: tuple(value) for key, value in result.items()}

    def by_block(self, block_index: int) -> tuple[PlannedTrial, ...]:
        return self._blocks.get(block_index, ())

    def to_dict(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "plan_version": self.plan_version,
            "trials": [asdict(trial) for trial in self.trials],
        }


def validate_session_plan(
    plan: SessionPlan,
    *,
    progress: dict[str, int],
    minimum_image_gap: int,
    trials_per_block: int,
) -> None:
    """Reject plans that cannot safely extend the participant's durable progress."""

    if not plan.trials:
        raise PlanError("Session plan is empty")
    expected_global = list(range(1, len(plan.trials) + 1))
    if [trial.global_index for trial in plan.trials] != expected_global:
        raise PlanError("Plan global indices are not contiguous")
    seen_repetitions: dict[str, list[int]] = {}
    positions: dict[str, list[int]] = {}
    for block_index in range(1, plan.block_count + 1):
        trials = plan.by_block(block_index)
        if len(trials) > trials_per_block:
            raise PlanError(f"Block {block_index} exceeds configured capacity")
        if [trial.trial_index for trial in trials] != list(range(1, len(trials) + 1)):
            raise PlanError(f"Block {block_index} trial indices are not contiguous")
        if len({trial.image_id for trial in trials}) != len(trials):
            raise PlanError(f"Block {block_index} repeats an image")
        category_counts: dict[str, int] = {}
        for trial in trials:
            category_counts[trial.category_id] = category_counts.get(trial.category_id, 0) + 1
        if category_counts and max(category_counts.values()) - min(category_counts.values()) > 1:
            raise PlanError(f"Block {block_index} category counts differ by more than one")
        for trial in trials:
            seen_repetitions.setdefault(trial.image_id, []).append(trial.repetition_index)
            positions.setdefault(trial.image_id, []).append(trial.global_index)
    for image_id, repetitions in seen_repetitions.items():
        start = progress.get(image_id, 0) + 1
        if sorted(repetitions) != list(range(start, start + len(repetitions))):
            raise PlanError(f"Image {image_id} does not continue durable repetition progress")
    for image_id, indices in positions.items():
        if any(right - left <= minimum_image_gap for left, right in pairwise(indices)):
            raise PlanError(f"Image {image_id} violates minimum image gap")


def make_plan(
    image_ids: list[str],
    repetitions: int,
    seed: int,
    probe_probability: float = 0.0,
) -> list[PlannedTrial]:
    """Small deterministic helper retained for unit-level callers."""

    rng = random.Random(seed)
    occurrences = [(image_id, repetition) for repetition in range(1, repetitions + 1) for image_id in image_ids]
    rng.shuffle(occurrences)
    return [
        PlannedTrial(
            global_index=index,
            block_index=1,
            trial_index=index,
            image_id=image_id,
            concept_id=image_id,
            category_id=image_id,
            file_path="",
            repetition_index=repetition,
            probe=rng.random() < probe_probability,
        )
        for index, (image_id, repetition) in enumerate(occurrences, start=1)
    ]


def build_session_plan(
    stimuli: Iterable[Stimulus],
    *,
    progress: dict[str, int],
    repetitions: int,
    blocks: int,
    trials_per_block: int,
    minimum_image_gap: int,
    attention: dict[str, object],
    seed: int,
    plan_version: str,
    auto_size_to_remaining: bool = False,
    prioritize_remaining: bool = False,
) -> SessionPlan:
    """Create a deterministic, constrained session plan.

    An image occurs at most once in a block. The planner retries different
    deterministic shuffles when a cross-block image-gap constraint is hard to
    satisfy; it never silently relaxes that constraint.
    """

    stimulus_list = list(stimuli)
    if not stimulus_list:
        raise PlanError("No stimuli are available")
    capacity = blocks * trials_per_block
    if capacity < 1:
        raise PlanError("Session capacity must be positive")
    if auto_size_to_remaining:
        needed = [
            max(0, (s.target_repetitions or repetitions) - progress.get(s.image_id, 0))
            for s in stimulus_list
        ]
        if not sum(needed):
            raise PlanError("All selected images have reached their valid-trial targets")
        required_blocks = max((sum(needed) + trials_per_block - 1) // trials_per_block, max(needed))
        if required_blocks > blocks:
            raise PlanError(f"Need {required_blocks} blocks to cover remaining targets; configured {blocks}")
        blocks = required_blocks
        capacity = blocks * trials_per_block

    for attempt in range(100):
        rng = random.Random(seed + attempt)
        block_items = _allocate_to_blocks(
            stimulus_list,
            progress=progress,
            repetitions=repetitions,
            blocks=blocks,
            trials_per_block=trials_per_block,
            rng=rng,
            prioritize_remaining=prioritize_remaining,
        )
        try:
            trials = _order_blocks(block_items, minimum_image_gap=minimum_image_gap, rng=rng)
            trials = _assign_probes(trials, attention=attention, expected_capacity=capacity, rng=rng)
        except PlanError:
            continue
        plan = SessionPlan(seed=seed, plan_version=plan_version, trials=tuple(trials))
        validate_session_plan(
            plan,
            progress=progress,
            minimum_image_gap=minimum_image_gap,
            trials_per_block=trials_per_block,
        )
        return plan
    raise PlanError("Could not satisfy image-gap or attention-probe constraints after 100 attempts")


def _category_round_robin(stimuli: list[Stimulus], rng: random.Random) -> list[Stimulus]:
    groups: dict[str, list[Stimulus]] = {}
    for stimulus in stimuli:
        groups.setdefault(stimulus.category_id, []).append(stimulus)
    for group in groups.values():
        rng.shuffle(group)
    categories = list(groups)
    rng.shuffle(categories)
    ordered: list[Stimulus] = []
    while categories:
        next_categories: list[str] = []
        for category in categories:
            group = groups[category]
            if group:
                ordered.append(group.pop())
            if group:
                next_categories.append(category)
        rng.shuffle(next_categories)
        categories = next_categories
    return ordered


def _allocate_to_blocks(
    stimuli: list[Stimulus],
    *,
    progress: dict[str, int],
    repetitions: int,
    blocks: int,
    trials_per_block: int,
    rng: random.Random,
    prioritize_remaining: bool = False,
) -> list[list[tuple[Stimulus, int]]]:
    assigned: list[list[tuple[Stimulus, int]]] = [[] for _ in range(blocks)]
    assigned_image_ids: list[set[str]] = [set() for _ in range(blocks)]
    category_counts: list[dict[str, int]] = [{} for _ in range(blocks)]
    ordered = _category_round_robin(stimuli, rng)
    if prioritize_remaining:
        # Mixed 4/10-repeat targets: start high-repeat images early, so a visit
        # budget does not leave a handful of 10-repeat images stranded at the end.
        ordered.sort(key=lambda s: -(int(s.target_repetitions or repetitions) - progress.get(s.image_id, 0)))
    for stimulus in ordered:
        completed = progress.get(stimulus.image_id, 0)
        needed = max(0, (stimulus.target_repetitions or repetitions) - completed)
        for repetition in range(completed + 1, completed + min(needed, blocks) + 1):
            eligible_blocks = [
                index
                for index, values in enumerate(assigned)
                if len(values) < trials_per_block and stimulus.image_id not in assigned_image_ids[index]
            ]
            if not eligible_blocks:
                break
            score = min(
                (category_counts[index].get(stimulus.category_id, 0), len(assigned[index]))
                for index in eligible_blocks
            )
            choices = [
                index
                for index in eligible_blocks
                if (category_counts[index].get(stimulus.category_id, 0), len(assigned[index])) == score
            ]
            chosen = rng.choice(choices)
            assigned[chosen].append((stimulus, repetition))
            assigned_image_ids[chosen].add(stimulus.image_id)
            category_counts[chosen][stimulus.category_id] = (
                category_counts[chosen].get(stimulus.category_id, 0) + 1
            )
    if not any(assigned):
        raise PlanError("No incomplete stimulus repetitions are available")
    return assigned


def _order_blocks(
    blocks: list[list[tuple[Stimulus, int]]],
    *,
    minimum_image_gap: int,
    rng: random.Random,
) -> list[PlannedTrial]:
    ordered: list[PlannedTrial] = []
    last_seen: dict[str, int] = {}
    global_index = 1
    for block_index, items in enumerate(blocks, start=1):
        remaining = list(items)
        previous_category: str | None = None
        trial_index = 1
        while remaining:
            eligible = [
                item
                for item in remaining
                if global_index - last_seen.get(item[0].image_id, -minimum_image_gap - 1) > minimum_image_gap
            ]
            if not eligible:
                raise PlanError("Image gap cannot be satisfied")
            category_change = [item for item in eligible if item[0].category_id != previous_category]
            stimulus, repetition = rng.choice(category_change or eligible)
            remaining.remove((stimulus, repetition))
            ordered.append(
                PlannedTrial(
                    global_index=global_index,
                    block_index=block_index,
                    trial_index=trial_index,
                    image_id=stimulus.image_id,
                    concept_id=stimulus.concept_id,
                    category_id=stimulus.category_id,
                    file_path=str(stimulus.file_path),
                    repetition_index=repetition,
                )
            )
            last_seen[stimulus.image_id] = global_index
            previous_category = stimulus.category_id
            global_index += 1
            trial_index += 1
    return ordered


def _assign_probes(
    trials: list[PlannedTrial],
    *,
    attention: dict[str, object],
    expected_capacity: int,
    rng: random.Random,
) -> list[PlannedTrial]:
    if not bool(attention.get("enabled", True)):
        return trials
    target = int(attention["probes_per_full_session"])
    if "trials_per_probe" in attention:
        target = len(trials) // int(attention["trials_per_probe"])
    elif len(trials) < expected_capacity:
        target = len(trials) // 20
    if target == 0:
        return trials
    correct_target = target // 2
    incorrect_target = target - correct_target
    minimum_gap = int(attention["minimum_trials_between_probes"])
    minimum_per_block = int(attention.get("minimum_probes_per_block", 0))
    maximum_per_block = int(attention.get("maximum_probes_per_block", target))
    candidates = [trial for trial in trials if trial.trial_index > 10]

    block_ids = {trial.block_index for trial in trials}
    for _ in range(500):
        selected: list[PlannedTrial] = []
        per_block: dict[int, list[PlannedTrial]] = {}
        remaining = {True: correct_target, False: incorrect_target}
        shuffled = list(candidates)
        rng.shuffle(shuffled)
        for trial in shuffled:
            label = bool(rng.getrandbits(1))
            if remaining[label] == 0:
                continue
            in_block = per_block.setdefault(trial.block_index, [])
            if len(in_block) >= maximum_per_block:
                continue
            if any(abs(trial.trial_index - other.trial_index) < minimum_gap for other in in_block):
                continue
            selected_trial = replace(trial, arithmetic_is_correct=label)
            selected.append(selected_trial)
            in_block.append(selected_trial)
            remaining[label] -= 1
            if remaining[True] == 0 and remaining[False] == 0:
                break
        if remaining[True] == 0 and remaining[False] == 0 and all(
            len(per_block.get(block, [])) >= minimum_per_block for block in block_ids
        ):
            selected_ids = {trial.global_index for trial in selected}
            selected_by_id = {trial.global_index: trial for trial in selected}
            return [
                replace(
                    trial,
                    probe=trial.global_index in selected_ids,
                    arithmetic_expression=_arithmetic_expression(
                        rng, is_correct=selected_by_id[trial.global_index].arithmetic_is_correct
                    )
                    if trial.global_index in selected_ids else None,
                    arithmetic_is_correct=selected_by_id[trial.global_index].arithmetic_is_correct
                    if trial.global_index in selected_ids else None,
                )
                for trial in trials
            ]
    raise PlanError("Could not distribute attention probes within block constraints")


def _arithmetic_expression(rng: random.Random, *, is_correct: bool) -> str:
    """Create a non-negative addition or subtraction statement within 100."""

    if rng.choice((True, False)):
        left = rng.randint(1, 99)
        right = rng.randint(1, 100 - left)
        operator = "+"
        result = left + right
    else:
        left = rng.randint(1, 100)
        right = rng.randint(0, left - 1)
        operator = "−"
        result = left - right
    displayed = result
    if not is_correct:
        displayed = rng.choice([value for value in range(101) if value != result])
    return f"{left} {operator} {right} = {displayed}"

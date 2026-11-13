"""Company EEG-BIDS handover for sealed dual-image recordings.

The internal SQLite/JSONL/float32 audit stays in the original session directory.
This exporter copies one real EDF into an independent package; it never edits EEG.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import tempfile
import uuid
import zipfile
from bisect import bisect_right
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .database import SessionStore

BIDS_VERSION = "1.11.2"
NA = "n/a"
PAIR_FIELDS = (
    "pair_id", "left_object_id", "right_object_id", "target_object_id", "target_side",
    "left_image_id", "right_image_id", "target_image_id",
)
EVENT_COLUMNS = (
    "onset", "duration", "trial_type", "stimulus_id", "trial_id", "sample_index",
    "trigger_value", "completed", "timing_source", *PAIR_FIELDS,
    "left_stim_file", "right_stim_file", "validity", "invalid_reason",
    "display_timestamp_ns", "source_eeg_sample_index", "eeg_observed_timestamp_ns",
    "alignment_method", "sync_quality", "trigger_failure_reason", "response", "response_time",
)


def validate_handover_identity(participant_id: str, paradigm_code: str) -> str:
    """Validate syntax only: the operator must supply an existing platform ID/code."""
    participant = str(participant_id).removeprefix("sub-")
    if not re.fullmatch(r"[0-9]{8}", participant):
        raise ValueError("正式交接要求人员平台已有的8位被试编号；不得使用实验内序号。")
    if not str(paradigm_code).strip():
        raise ValueError("双图正式交接必须填写公司登记的 paradigm_code。")
    return participant


def _json_write(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _edf_header(path: Path) -> dict[str, Any]:
    """Read the actual sealed EDF header without loading or converting samples."""
    with path.open("rb") as handle:
        fixed = handle.read(256)
        if len(fixed) != 256 or fixed[:8].strip() != b"0":
            raise ValueError("交接信号不是可读取的 EDF 文件。")
        try:
            header_size = int(fixed[184:192])
            records = int(fixed[236:244])
            record_duration = float(fixed[244:252])
            count = int(fixed[252:256])
            if count < 1 or header_size != 256 + count * 256:
                raise ValueError("EDF channel/header count mismatch")
            signal_header = handle.read(header_size - 256)
            if len(signal_header) != header_size - 256:
                raise ValueError("Truncated EDF header")
            cursor = 0
            fields: dict[str, list[str]] = {}
            for name, width in (
                ("names", 16), ("transducers", 80), ("units", 8),
                ("physical_min", 8), ("physical_max", 8),
                ("digital_min", 8), ("digital_max", 8), ("prefilters", 80),
                ("samples_per_record", 8), ("reserved", 32),
            ):
                fields[name] = [
                    signal_header[cursor + i * width:cursor + (i + 1) * width]
                    .decode("ascii", errors="replace").strip()
                    for i in range(count)
                ]
                cursor += count * width
            samples = [int(value) for value in fields["samples_per_record"]]
            if records < 1 or record_duration <= 0 or any(value < 1 for value in samples):
                raise ValueError("EDF 未正常封存或没有真实样本。")
            expected_size = header_size + records * sum(samples) * 2
            if path.stat().st_size != expected_size:
                raise ValueError("EDF 文件长度与头中的真实记录数不一致。")
            rates = [value / record_duration for value in samples]
            return {
                **fields, "sample_rates": rates, "record_count": records,
                "record_duration": record_duration, "channel_count": count,
                "stored_samples": [records * value for value in samples],
                "start_date": fixed[168:176].decode("ascii").strip(),
                "start_time": fixed[176:184].decode("ascii").strip(),
            }
        except (ValueError, OverflowError) as error:
            raise ValueError(f"EDF 头验证失败：{error}") from error


def _channel_type(name: str, supplied: str | None = None) -> str:
    if supplied:
        kind = supplied.upper()
        if kind not in {"EEG", "EOG", "ECG", "EMG", "TRIG", "MISC", "RESP", "GSR", "TEMP", "PPG"}:
            raise ValueError(f"未知的 BIDS 通道类型：{supplied}")
        return kind
    normalized = name.strip().upper()
    if normalized in {"TRG", "TRIGGER", "STATUS", "STI014", "STI 014"}:
        return "TRIG"
    if normalized.startswith(("ECG", "EKG")):
        return "ECG"
    if normalized.startswith(("EOG", "HEO", "VEO")):
        return "EOG"
    if normalized.startswith("EMG"):
        return "EMG"
    if re.fullmatch(r"(FP|AF|F|FT|FC|C|CP|TP|T|P|PO|O|A|M)(Z|[0-9]+)", normalized):
        return "EEG"
    return "MISC"


def _device_config(store: SessionStore, recording: dict[str, Any]) -> dict[str, Any]:
    devices = store.locked_config.get("devices", {})
    configured = devices.get("devices", devices)
    mode = store.run_mode
    if mode in configured and isinstance(configured[mode], dict):
        return configured[mode]
    for name, config in configured.items():
        if isinstance(config, dict) and str(recording["device_type"]).startswith(str(name)):
            return config
    return {}


def _recording_times(
    store: SessionStore, recording: dict[str, Any], header: dict[str, Any],
    events: list[dict[str, Any]],
) -> tuple[str, str, str]:
    start_ns, end_ns = recording.get("recording_start_ns"), recording.get("recording_end_ns")
    if start_ns is None or end_ns is None or int(end_ns) < int(start_ns):
        raise ValueError("缺少可追溯的 EEG 实际开始/结束单调时钟，不能正式交接。")
    for event in events:
        metadata = event["metadata"]
        anchor = metadata.get("wall_clock_anchor")
        anchor_ns = metadata.get("monotonic_anchor_ns")
        if anchor is not None and anchor_ns is not None:
            wall = datetime.fromisoformat(str(anchor))
            if wall.utcoffset() is None:
                raise ValueError("EEG 墙钟锚点必须包含时区。")
            start = wall + timedelta(microseconds=(int(start_ns) - int(anchor_ns)) / 1000)
            end = wall + timedelta(microseconds=(int(end_ns) - int(anchor_ns)) / 1000)
            return start.isoformat(), end.isoformat(), "wall_clock_monotonic_anchor"
    # Old EDF writers include the actual recording creation time to one second.
    # Explicitly retain that limited precision instead of substituting upload time.
    timezone_name = store.locked_config.get("experiment", {}).get("project", {}).get(
        "timezone", "Asia/Shanghai"
    )
    local_timezone = ZoneInfo(str(timezone_name))
    start = datetime.strptime(
        f'{header["start_date"]} {header["start_time"]}', "%d.%m.%y %H.%M.%S"
    ).replace(tzinfo=local_timezone)
    end = start + timedelta(microseconds=(int(end_ns) - int(start_ns)) / 1000)
    return start.isoformat(), end.isoformat(), "edf_header_second_precision"


def _read_events(store: SessionStore, recording_id: str) -> list[dict[str, Any]]:
    syncs: dict[int, dict[str, Any]] = {}
    for row in store.database.execute(
        "SELECT * FROM eeg_sync WHERE recording_id = ? ORDER BY rowid", (recording_id,)
    ):
        entry = dict(row)
        previous = syncs.get(int(entry["event_id"]))
        # Keep the best observable alignment if audit history contains retries.
        if previous is None or (
            entry.get("sync_quality") == "hardware_verified"
            or previous.get("eeg_sample_index") is None
        ):
            syncs[int(entry["event_id"])] = entry
    output = []
    for row in store.database.execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY event_seq", (store.session_id,)
    ):
        entry = dict(row)
        entry["metadata"] = json.loads(entry["metadata_json"])
        entry["sync"] = syncs.get(int(entry["event_id"]), {})
        output.append(entry)
    return output


def _zero_based_index(event: dict[str, Any], recording: dict[str, Any]) -> tuple[Any, Any]:
    sync, metadata = event["sync"], event["metadata"]
    original = sync.get("eeg_sample_index", metadata.get("eeg_sample_index"))
    if original is None:
        return NA, NA
    method = sync.get("alignment_method", metadata.get("alignment_method"))
    # Neuracle TCP explicitly records TRG samples as first_sample+offset+1.
    base = metadata.get("event_sample_index_base")
    if base is None:
        base = 1 if method == "tcp_trg_channel" else 0
    index = int(original) - int(base)
    count = recording.get("sample_count")
    if index < 0 or (count is not None and index >= int(count)):
        return NA, original
    return index, original


def _event_rows(
    store: SessionStore, recording: dict[str, Any], events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    trials = {
        row["trial_id"]: dict(row)
        for row in store.database.execute(
            "SELECT * FROM trials WHERE session_id = ?", (store.session_id,)
        )
    }
    # Source IMAGE_OFFSET markers form durations, not a second stimulus row.
    boundaries = {
        "IMAGE_ONSET": ("IMAGE_OFFSET", "image"),
        "TARGET_CUE_ONSET": ("TARGET_CUE_OFFSET", "target_cue"),
        "CUE_ONSET": ("CUE_OFFSET", "target_cue"),
        "FIXATION_ONSET": ("FIXATION_OFFSET", "fixation"),
        "ISI_ONSET": ("ISI_OFFSET", "rest"),
        "BASELINE_EYES_OPEN_START": ("BASELINE_EYES_OPEN_END", "rest_eyes_open"),
        "BASELINE_EYES_CLOSED_START": ("BASELINE_EYES_CLOSED_END", "rest_eyes_closed"),
    }
    selected = {"TARGET_ERROR", "DEVICE_ERROR", "SESSION_ABORT", "RECOVERY_ABORT"}
    rate = float(recording["sampling_rate_hz"])
    start_ns = int(recording["recording_start_ns"])
    rows = []
    indexed_offsets: dict[tuple[str, Any], tuple[list[int], list[dict[str, Any]]]] = {}
    for position, event in enumerate(events):
        key = (event["event_type"], event["trial_id"])
        positions, matching = indexed_offsets.setdefault(key, ([], []))
        positions.append(position)
        matching.append(event)
    for position, event in enumerate(events):
        kind = event["event_type"]
        if kind not in boundaries and kind not in selected:
            continue
        trial = trials.get(event["trial_id"], {})
        metadata, sync = event["metadata"], event["sync"]
        row = dict.fromkeys(EVENT_COLUMNS, NA)
        index, source_index = _zero_based_index(event, recording)
        quality = sync.get("sync_quality", metadata.get("sync_quality"))
        hardware = (quality == "hardware_verified" and index != NA
                    and not metadata.get("trigger_error"))
        row["onset"] = (
            index / rate if hardware else (int(event["timestamp_ns"]) - start_ns) / 1e9
        )
        row["sample_index"] = index
        row["source_eeg_sample_index"] = source_index
        row["timing_source"] = "hardware_trigger" if hardware else "software_timestamp"
        row["display_timestamp_ns"] = event["timestamp_ns"]
        row["eeg_observed_timestamp_ns"] = sync.get(
            "eeg_timestamp_ns", metadata.get("eeg_timestamp_ns")
        )
        row["alignment_method"] = sync.get("alignment_method", metadata.get("alignment_method"))
        row["sync_quality"] = quality
        row["trigger_failure_reason"] = (
            metadata.get("trigger_error") or sync.get("failure_reason")
            or metadata.get("failure_reason")
        )
        # The legacy code stores the requested code even if sending failed.
        # Do not promote a configured-but-unsent code to an actual trigger.
        row["trigger_value"] = (
            event["trigger_code"] if event["trigger_code"] is not None
            and not metadata.get("trigger_error") else NA
        )
        row["trial_id"] = event["trial_id"]
        row["validity"] = trial.get("validity")
        row["invalid_reason"] = trial.get("invalid_reason")
        for key in PAIR_FIELDS:
            row[key] = metadata.get(key)
        row["target_image_id"] = metadata.get("target_image_id") or metadata.get(
            "left_image_id" if metadata.get("target_side") == "left" else "right_image_id"
        )
        row["response"] = metadata.get("response")
        reaction_ms = metadata.get("reaction_time_ms")
        row["response_time"] = float(reaction_ms) / 1000 if reaction_ms is not None else None
        row["left_stim_file"] = metadata.get("left_stim_file", metadata.get("left_file_path"))
        row["right_stim_file"] = metadata.get("right_stim_file", metadata.get("right_file_path"))
        row["stimulus_id"] = metadata.get("pair_id", trial.get("image_id"))
        if kind in boundaries:
            offset_type, trial_type = boundaries[kind]
            row["trial_type"] = trial_type
            positions, matching = indexed_offsets.get((offset_type, event["trial_id"]), ([], []))
            offset_index = bisect_right(positions, position)
            offset = matching[offset_index] if offset_index < len(matching) else None
            offset_ns = offset["timestamp_ns"] if offset else (
                trial.get("actual_offset_ns") if kind == "IMAGE_ONSET" else None
            )
            row["duration"] = (
                (int(offset_ns) - int(event["timestamp_ns"])) / 1e9
                if offset_ns is not None and int(offset_ns) >= int(event["timestamp_ns"]) else NA
            )
            # "completed" is presentation completion, independent of E attention validity.
            row["completed"] = int(offset is not None or (
                offset_ns is not None and trial.get("status") in {"valid", "invalid"}
            ))
            if kind == "IMAGE_ONSET":
                if trial.get("status") == "aborted":
                    row["completed"] = 0
                if not all(metadata.get(key) is not None and str(metadata[key]).strip() for key in PAIR_FIELDS[:5]):
                    raise ValueError("双图刺激事件缺少 pair/左右物体/目标标签，拒绝不完整交接。")
                if metadata["target_side"] not in {"left", "right"}:
                    raise ValueError("target_side 必须为 left 或 right。")
                if metadata["target_object_id"] != metadata[
                    "left_object_id" if metadata["target_side"] == "left" else "right_object_id"
                ]:
                    raise ValueError("双图目标物体与目标侧不一致。")
        else:
            row["trial_type"] = kind.lower()
            row["duration"] = 0
            row["completed"] = 1
        rows.append({key: NA if value is None or value == "" else value for key, value in row.items()})
    return sorted(rows, key=lambda row: float(row["onset"]))


def _events_dictionary() -> dict[str, Any]:
    descriptions = {
        "onset": "Event onset in seconds from the first stored EEG sample. Hardware TRG sample alignment is used only when verified; otherwise the observed display monotonic timestamp and recording start clock are used.",
        "duration": "Actual interval between observed display onset and offset, in seconds; n/a when an interrupted stimulus has no observed offset. Never the planned duration.",
        "trial_type": "Observed event kind. One image event represents the simultaneously presented left and right images.",
        "stimulus_id": "Stable pair identifier for a dual-image stimulus; individual original material IDs are retained in left_image_id/right_image_id.",
        "trial_id": "Unique presentation ID. Repeating the same image pair creates a new trial ID.",
        "sample_index": "Zero-based index of an observed EEG alignment; n/a if unknown or outside acquired samples.",
        "trigger_value": "Actual sent trigger code where observable; n/a for events without a sent trigger or failed sending.",
        "completed": "Presentation completed (1) or interrupted without completion (0). Attention errors remain separate in validity/invalid_reason.",
        "timing_source": "hardware_trigger for verified TRG-sample onsets, otherwise software_timestamp. Software timing is not a claim of measured hardware latency correction.",
        "pair_id": "Stable unordered object/image pair identity, unaffected by left/right swaps.",
        "left_object_id": "THINGS concept/object identifier shown on the left.",
        "right_object_id": "THINGS concept/object identifier shown on the right.",
        "target_object_id": "THINGS concept/object identifier the participant was instructed to attend.",
        "target_side": "Side containing the instructed target.",
        "left_image_id": "Stable original THINGS material ID displayed on the left.",
        "right_image_id": "Stable original THINGS material ID displayed on the right.",
        "target_image_id": "Stable original THINGS material ID of the target.",
        "left_stim_file": "Optional original source image reference for the left image. Images are not bundled; resolve the stable material ID against the original THINGS manifest. This is a custom field, not the standard BIDS stim_file.",
        "right_stim_file": "Optional original source image reference for the right image. Images are not bundled; resolve its material ID against the original THINGS manifest.",
        "validity": "Original trial validity, including attention or trigger/timing failures.",
        "invalid_reason": "Original recorded reason for an invalid presentation.",
        "display_timestamp_ns": "Observed display/keypress time in the process monotonic clock, nanoseconds.",
        "source_eeg_sample_index": "Unchanged device alignment index. Neuracle tcp_trg_channel uses a one-based source index; sample_index is converted to zero-based.",
        "eeg_observed_timestamp_ns": "Original observed device/packet arrival monotonic time. Neuracle packet arrival is not the sample acquisition time.",
        "alignment_method": "Original device alignment method, retained without a calibration claim.",
        "sync_quality": "Original measured alignment quality.",
        "trigger_failure_reason": "Original send or verification error, if any.",
        "response": "Actual participant key used to report an attention error, if any.",
        "response_time": "Observed response time in seconds relative to the image display onset, when recorded.",
    }
    dictionary = {key: {"Description": descriptions[key]} for key in EVENT_COLUMNS}
    for key in ("onset", "duration", "response_time"):
        dictionary[key]["Units"] = "s"
    for key in ("display_timestamp_ns", "eeg_observed_timestamp_ns"):
        dictionary[key]["Units"] = "ns"
    dictionary["completed"]["Levels"] = {"0": "interrupted", "1": "completed"}
    dictionary["target_side"]["Levels"] = {"left": "left target", "right": "right target"}
    dictionary["sample_index"]["SampleIndexBase"] = 0
    return dictionary


def _stop_reason(session: dict[str, Any], events: list[dict[str, Any]]) -> str:
    explicit = session.get("stop_reason")
    if explicit:
        return str(explicit)
    kinds = {event["event_type"] for event in events}
    if "DEVICE_ERROR" in kinds:
        reason = " ".join(str(event["metadata"].get("reason", "")) for event in events
                          if event["event_type"] == "DEVICE_ERROR").lower()
        return "device_disconnect" if "disconnect" in reason else "device_error"
    if "SESSION_ABORT" in kinds:
        return "operator_stop"
    if "RECOVERY_ABORT" in kinds:
        return "application_error"
    return "planned_end" if session["status"] == "complete" else "application_error"


def export_bids_package(
    store: SessionStore, *, export_root: Path | None = None,
) -> Path | None:
    """Copy a sealed dual-image real recording into an independent directory and ZIP.

    Returns None for single-image or simulated sessions. Validation failures leave
    original files untouched and must be shown as export errors by the caller.
    Existing sealed packages are reused, never silently replaced.
    """
    experiment = store.locked_config.get("experiment", {})
    dual = experiment.get("dual_image", {})
    if not dual.get("enabled") or store.run_mode in {"simulation", "realtime_simulation"}:
        return None
    paradigm_code = str(dual.get("paradigm_code", "")).strip()
    participant = validate_handover_identity(store.participant_id, paradigm_code)
    task_label = str(dual.get("task_label", "thingspair"))
    if not re.fullmatch(r"[A-Za-z0-9]+", task_label) or task_label == "things":
        raise ValueError("双图 task_label 须为独立字母数字标签；task-things 会被旧平台误识别为 RSVP。")
    try:
        recording_uuid = uuid.UUID(str(store.session_id).removeprefix("ses-")).hex
    except ValueError as error:
        raise ValueError("双图正式会话必须使用稳定的 UUID；导出时不能重新生成记录身份。") from error
    source_session = store.database.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (store.session_id,)
    ).fetchone()
    if source_session is None or source_session["status"] == "running" or not source_session["end_time"]:
        raise ValueError("采集尚未结束封存，不能生成交接 ZIP。")
    session = dict(source_session)
    recordings = list(store.database.execute(
        "SELECT * FROM eeg_recordings WHERE session_id = ?", (store.session_id,)
    ))
    if not recordings:
        raise ValueError("没有真实 EEG EDF，不能生成正式 BIDS 交接包。")
    if len(recordings) != 1:
        raise ValueError("当前交接包只能包含一段连续 EEG，不能混合多个 run。")
    recording = dict(recordings[0])
    if "simulat" in str(recording["source_transport"]).lower():
        return None
    if recording["status"] != "complete":
        raise ValueError("EEG 文件尚未正常关闭，不能生成正式交接包。")
    if store.database.execute(
        "SELECT 1 FROM data_gaps WHERE recording_id = ? AND missing_samples > 0 AND recovered = 0",
        (recording["recording_id"],),
    ).fetchone():
        raise ValueError("记录存在未恢复的数据缺口；请分段采集，不能删除间隙或补零宣称连续。原数据已保留。")
    source_edf = (store.dataset_root / str(recording["edf_path"])).resolve()
    if not source_edf.is_relative_to(store.dataset_root.resolve()) or source_edf.suffix.lower() != ".edf":
        raise ValueError("EEG 源文件必须是原会话目录中的 EDF。")
    source_hash = _sha256(source_edf)
    if source_hash != recording["sha256"]:
        raise ValueError("原始 EEG 校验失败，不能交接被改动的记录。")
    header = _edf_header(source_edf)
    if recording["channel_count"] != header["channel_count"]:
        raise ValueError("?? EDF ??????????")
    if any(not name for name in header["names"]) or len(set(header["names"])) != header["channel_count"]:
        raise ValueError("?? EDF ????????????")
    rate = float(recording["sampling_rate_hz"] or 0)
    if rate <= 0 or any(abs(value - rate) > 1e-8 for value in header["sample_rates"]):
        raise ValueError("实际 EDF 与记录采样率不一致，不能伪造采样参数。")
    acquired_samples = recording["sample_count"]
    if acquired_samples is None or not 0 < int(acquired_samples) <= header["stored_samples"][0]:
        raise ValueError("缺少真实采样点数，不能将 EDF 尾部填充当作有效信号。")
    if header["stored_samples"][0] - int(acquired_samples) >= round(rate * header["record_duration"]):
        raise ValueError("EDF ??????????????????????????????")
    events = _read_events(store, recording["recording_id"])
    event_rows = _event_rows(store, recording, events)
    started_at, ended_at, clock_source = _recording_times(store, recording, header, events)
    config = _device_config(store, recording)
    configured_types = config.get("channel_types") or [None] * header["channel_count"]
    if len(configured_types) != header["channel_count"]:
        raise ValueError("channel_types 必须按实际 EDF 通道逐项配置。")
    channels = []
    registered_channels = list(store.database.execute(
        "SELECT * FROM eeg_channels WHERE recording_id = ? ORDER BY channel_index",
        (recording["recording_id"],),
    ))
    if registered_channels and len(registered_channels) != header["channel_count"]:
        raise ValueError("实际 EDF 和采集记录的通道数不一致。")
    for index, name in enumerate(header["names"]):
        kind = _channel_type(name, configured_types[index])
        unit = header["units"][index] or NA
        if registered_channels:
            registered = registered_channels[index]
            if str(registered["channel_name"])[:16] != name:
                raise ValueError("实际 EDF 和采集记录的通道顺序不一致。")
            expected_unit = registered["unit"]
            if expected_unit and expected_unit != "unknown" and str(expected_unit)[:8] != unit:
                raise ValueError("实际 EDF 和采集记录的通道单位不一致。")
        channels.append({
            "name": name, "type": kind, "units": NA if kind == "TRIG" else unit,
            "sampling_frequency": rate,
        })
    root = Path(export_root or dual.get("export_root") or store.dataset_root / "exports").resolve()
    root.mkdir(parents=True, exist_ok=True)
    package = root / f"recording-{recording_uuid}"
    zip_path = root / f"{package.name}.zip"
    sub_name, ses_name = f"sub-{participant}", f"ses-{recording_uuid}"
    prefix = f"{sub_name}_{ses_name}_task-{task_label}_run-01"
    relative_eeg = Path(sub_name) / ses_name / "eeg"
    if package.exists():
        sealed_eeg = package / relative_eeg / f"{prefix}_eeg.edf"
        sealed_session = package / sub_name / ses_name / "session.json"
        if not sealed_eeg.is_file() or not sealed_session.is_file():
            raise ValueError("同 UUID 的已有交接目录不完整，未覆盖原文件。")
        identity = json.loads(sealed_session.read_text(encoding="utf-8"))
        if _sha256(sealed_eeg) != source_hash or identity.get("source_session_id") != store.session_id:
            raise ValueError("同 UUID 的已有交接包与当前原记录不一致，未覆盖原文件。")
        if zip_path.is_file():
            return package
    else:
        with tempfile.TemporaryDirectory(prefix=".bids-", dir=root) as temporary:
            staging = Path(temporary) / "recording"
            eeg_dir = staging / relative_eeg
            eeg_dir.mkdir(parents=True)
            shutil.copyfile(source_edf, eeg_dir / f"{prefix}_eeg.edf")
            _json_write(staging / "dataset_description.json", {
                "Name": "THINGS dual-image EEG acquisition",
                "BIDSVersion": BIDS_VERSION, "DatasetType": "raw",
                "GeneratedBy": [{"Name": "THINGS-EEG2-Extended", "Version": "0.1.0"}],
            })
            (staging / ".bidsignore").write_text("**/session.json\n", encoding="utf-8")
            with (staging / "participants.tsv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, delimiter="\t")
                writer.writerow(["participant_id"])
                writer.writerow([sub_name])
            _json_write(staging / sub_name / ses_name / "session.json", {
                "schema_version": 1, "participant_id": participant,
                "session_id": recording_uuid, "paradigm_code": paradigm_code,
                "started_at": started_at, "ended_at": ended_at,
                "recording_status": "complete" if session["status"] == "complete" else "interrupted",
                "stop_reason": _stop_reason(session, events),
                "acquisition_software": {"name": "THINGS-EEG2-Extended", "version": "0.1.0"},
                "source_session_id": store.session_id,
                "source_recording_id": recording["recording_id"],
                "source_edf_sha256": source_hash,
                "acquired_sample_count": int(acquired_samples),
                "recording_clock_source": clock_source,
                "bids_validation_status": "not_run",
            })
            reference = recording.get("reference") or config.get("reference") or NA
            eeg_metadata: dict[str, Any] = {
                "TaskName": task_label, "SamplingFrequency": rate,
                "PowerLineFrequency": config.get("powerline_frequency_hz", NA),
                "EEGReference": reference, "SoftwareFilters": config.get("software_filters", NA),
                "RecordingType": "continuous",
                "RecordingDuration": header["stored_samples"][0] / rate,
                "Manufacturer": config.get("manufacturer", NA),
                "ManufacturersModelName": config.get("model", NA),
                "AcquiredSampleCount": int(acquired_samples),
                "AcquiredDuration": int(acquired_samples) / rate,
                "EDFStoredSampleCount": header["stored_samples"][0],
                "EDFTrailingPaddingSamples": header["stored_samples"][0] - int(acquired_samples),
                "EDFExportNote": (
                    "Original EDF copied without conversion. For the legacy Neuracle writer, "
                    "values are integer-rounded/clipped and its last short record is padded; "
                    "lossless float32 remains in the original internal session, outside this package."
                ),
                "EventTimingNote": (
                    "Verified TRG onsets use actual zero-based stored EEG samples. Software event "
                    "onsets use the recording/display monotonic clock, without measured latency "
                    "correction. Durations preserve observed display intervals."
                ),
            }
            for channel_type in ("EEG", "EOG", "ECG", "EMG", "MISC", "TRIG"):
                field = {"TRIG": "TriggerChannelCount", "MISC": "MiscChannelCount"}.get(channel_type, f"{channel_type}ChannelCount")
                eeg_metadata[field] = sum(row["type"] == channel_type for row in channels)
            if any(header["prefilters"]):
                eeg_metadata["EDFHardwarePrefilterDescriptions"] = header["prefilters"]
            _json_write(eeg_dir / f"{prefix}_eeg.json", eeg_metadata)
            with (eeg_dir / f"{prefix}_channels.tsv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=["name", "type", "units", "sampling_frequency"], delimiter="\t"
                )
                writer.writeheader()
                writer.writerows(channels)
            with (eeg_dir / f"{prefix}_events.tsv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=EVENT_COLUMNS, delimiter="\t")
                writer.writeheader()
                writer.writerows(event_rows)
            _json_write(eeg_dir / f"{prefix}_events.json", _events_dictionary())
            if _sha256(eeg_dir / f"{prefix}_eeg.edf") != source_hash:
                raise RuntimeError("EEG 交接副本校验失败。")
            # Atomic publication of a complete directory; no partial ZIP is exposed.
            staging.rename(package)
    with tempfile.NamedTemporaryFile(prefix=f".{package.name}-", suffix=".zip", dir=root, delete=False) as temporary:
        temporary_zip = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        temporary_zip.replace(zip_path)
    finally:
        if temporary_zip.exists():
            temporary_zip.unlink()
    return package

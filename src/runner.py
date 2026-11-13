"""会话执行与记录。UTC 用于跨文件定位，单调时钟用于相对时间。"""

from contextlib import ExitStack
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import platform
import time
from uuid import uuid4

from .config import Config
from .devices import Device, MockDevice


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run(config: Config, *, realtime: bool = True, device: Device | None = None) -> Path:
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:12]
    session = config.output_dir / session_id
    session.mkdir(parents=True, exist_ok=False)
    snapshot = asdict(config)
    snapshot["output_dir"] = str(config.output_dir)
    write_json(session / "config.json", snapshot)
    metadata = {"schema_version": 1, "session_id": session_id, "task_name": config.task_name,
                "backend": "mock" if device is None else "custom",
                "status": "running", "started_at": utc_now(), "realtime": realtime,
                "python_version": platform.python_version(), "samples_written": 0,
                "trials_completed": 0}
    write_json(session / "session.json", metadata)
    device = device if device is not None else MockDevice(config.sample_rate_hz, config.channels)
    logger = logging.getLogger(f"acq_task.{session_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    origin = time.perf_counter()
    with ExitStack() as stack:
        handler = logging.FileHandler(session / "run.log", encoding="utf-8")
        stack.callback(handler.close)
        logger.addHandler(handler)
        stack.callback(logger.removeHandler, handler)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        samples = stack.enter_context((session / "samples.csv").open("w", newline="", encoding="utf-8"))
        events = stack.enter_context((session / "events.jsonl").open("w", encoding="utf-8"))
        writer = csv.writer(samples)
        writer.writerow(["trial", "sample_index", "scheduled_time_s", "elapsed_s", "utc_time"]
                        + [f"ch_{i + 1}" for i in range(config.channels)])

        def event(name: str, **fields: object) -> None:
            record = {"event": name, "utc_time": utc_now(),
                      "elapsed_s": time.perf_counter() - origin, **fields}
            events.write(json.dumps(record, ensure_ascii=False) + "\n")
            events.flush()
            logger.info("%s %s", record["utc_time"], name)

        try:
            device.open()
            event("session_start")
            count = int(config.duration_s * config.sample_rate_hz)
            for trial in range(1, config.trials + 1):
                event("trial_start", trial=trial)
                for index in range(count):
                    scheduled = (trial - 1) * config.duration_s + index / config.sample_rate_hz
                    if realtime:
                        time.sleep(max(0, origin + scheduled - time.perf_counter()))
                    values = device.read_sample((trial - 1) * count + index)
                    if len(values) != config.channels:
                        raise ValueError("设备返回通道数量与配置不一致")
                    writer.writerow([trial, index, scheduled, time.perf_counter() - origin,
                                     utc_now(), *values])
                    metadata["samples_written"] += 1
                if realtime:
                    time.sleep(max(0, origin + trial * config.duration_s - time.perf_counter()))
                samples.flush()
                metadata["trials_completed"] += 1
                event("trial_end", trial=trial)
            metadata["status"] = "completed"
        except KeyboardInterrupt:
            metadata["status"] = "interrupted"
            event("session_interrupted")
            raise
        except Exception as exc:
            metadata["status"] = "failed"
            metadata["error"] = str(exc)
            logger.exception("Session failed")
            event("session_failed", error=str(exc))
            raise
        finally:
            try:
                device.close()
            except Exception as exc:
                logger.exception("Device cleanup failed")
                metadata["cleanup_error"] = str(exc)
                if metadata["status"] == "completed":
                    metadata["status"] = "failed"
                    raise
            finally:
                metadata["ended_at"] = utc_now()
                samples.flush()
                event("session_end", status=metadata["status"])
                write_json(session / "session.json", metadata)
    return session

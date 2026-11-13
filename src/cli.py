import argparse
from pathlib import Path
import sys

from .config import load_config, BrainCoConfig
from .runner import run


def main(argv: list[str] | None = None) -> int:
    # Windows 的重定向输出可能使用 cp1252；统一为 UTF-8 以保留中文。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="数采试验任务模板（模拟设备）")
    parser.add_argument("--config", type=Path, default=Path("config/config.toml"), help="配置文件路径")
    parser.add_argument("--validate", action="store_true", help="仅校验配置，不创建会话")
    parser.add_argument("--fast", action="store_true", help="跳过模拟采样等待，仅用于流程验证")
    parser.add_argument('--gui',action='store_true',help='显式打开强脑正式采集GUI，需brainco配置')
    parser.add_argument('--check-device',action='store_true',help='真实32通道短录制与软件标注回读')
    parser.add_argument('--check-assets',action='store_true',help='验证完整排程及本地图片哈希和解码')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.fast and isinstance(config,BrainCoConfig):
            raise ValueError('--fast只允许mock，不允许加速正式采集')
        if sum((args.validate,args.gui,args.check_device,args.check_assets))>1:
            raise ValueError('选择一个运行操作')
        if args.validate:
            print(f"配置有效：{config.task_name}")
            return 0
        if isinstance(config,BrainCoConfig):
            from .acquisition import launch,check_assets,check_device
            import json
            if args.check_assets:
                print(json.dumps(check_assets(config),ensure_ascii=False));return 0
            if args.check_device:
                print(json.dumps(check_device(config),ensure_ascii=False,indent=2));return 0
            if args.gui:return launch(config)
            raise ValueError('真实设备必须显式选择--gui或--check-device')
        if args.gui or args.check_device or args.check_assets:
            raise ValueError('此操作需要config/brainco.toml')
        session = run(config, realtime=not args.fast)
        print(f"会话完成：{session}")
        return 0
    except KeyboardInterrupt:
        print("实验已中断，已保留会话记录。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"实验失败：{exc}", file=sys.stderr)
        return 1

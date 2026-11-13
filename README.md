# BrainCo scene EEG acquisition (uv)

## 10 台电脑部署

双击 **启动GUI.bat / RUN_GUI.bat** 自动准备 uv 环境并打开启动器；首次连接 NAS 后，一键下载全部图片，再打开采集工作台。工作台提供“上传当前被试全部数据”按钮，使用逐文件读回校验和可恢复快照。

图片和数据默认使用 `\\172.16.4.2\aim\shared\CFIMAGE-20261113` 的独立子目录。S3 支持可通过本机配置切换；SMB 权限不等于 S3 权限。GitHub 不含图片、SDK wheel、采集数据或密钥。

详细步骤：[分发与 NAS 操作说明](docs/DISTRIBUTION.md)。

BCSCN-20261113. Python 3.12, one root .venv managed by uv. No conda.

```powershell
uv sync --locked --python 3.12
uv run --no-sync python main.py --validate
uv run --no-sync python main.py --fast
uv run --no-sync python main.py --config config/brainco.toml --check-assets
uv run --no-sync python main.py --config config/brainco.toml --check-device
uv run --no-sync python main.py --config config/brainco.toml --gui
uv run --no-sync python -m unittest discover -s tests -v
```

Default main.py / RUNME.bat is mock only. Formal protocol: 32 channels, 2580 images x 10 repetitions, 0.6 s image + 0.1 s interval, a break every 80 trials, fixed-schedule resume.

See [Chinese protocol, clock and analyst handoff](docs/BRAINCO_CLOCK_HANDOFF.md) for continuous recording, software markers, drift mitigation and limitations. SDK units remain unverified; no EDF/BDF or EEGQC readiness claim. Real-device timing requires validation.

Assets use the hash-pinned download index and NAS transfer tool. The old template DVC remote is optional and is not the one-click download source. Preserve data/native and all .brainco/wire_archives, not only derived handoff CSV files.

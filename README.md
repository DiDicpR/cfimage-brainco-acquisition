# BrainCo scene EEG acquisition (uv)

## 实验电脑部署（内网加速版）

本版独立分发仓库为 `DiDicpR/cfimage-brainco-acquisition-lan`，目标目录为 `D:\Paradigm-update\cfimage-brainco-acquisition-lan`，与首版目录分开。旧目录中的采集数据和本机配置保留；不要将新空目录当成已经自动迁移了旧版续采进度。

双击 **启动GUI.bat / RUN_GUI.bat** 自动准备 uv 环境、从 NAS 匿名下载并校验全部图片，然后自动打开采集工作台；无需输入 NAS 账号密码。图片最多 4 路并行下载，重试复用已校验文件。填写真实被试编号、通过设备连通性检查后再明确开始采集。数据上传仍使用本机 SMB 授权，启动器保留“数据上传授权”入口；该入口不影响图片准备和采集。

实验机首次安装走局域网 NAS：下载约 161 MiB 的固定环境包（Python 3.12、uv、29 个运行依赖），校验 SHA-256 后用 uv 离线安装到项目 `.venv`，无需逐个从公网下载依赖。后续复用本地环境包。要求 Windows x64、可访问实验室 NAS；不需要预装 Python 或 uv。图片下载流程保持原样。详见 [环境加速与旧版升级](docs/OFFLINE_RUNTIME.md)。

图片默认匿名读取 `http://172.16.4.2:9000/oi-task-assets/CFIMAGE-20261113/`，数据上传仍使用 `\\172.16.4.2\aim\shared\CFIMAGE-20261113\eeg-data`。`asset_backend` 与数据 `backend` 分别配置。GitHub 不含图片、SDK wheel、采集数据或密钥。

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

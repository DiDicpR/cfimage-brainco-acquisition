> BrainCo uv implementation (2026-11-13): see [current protocol and clock/data contract](BRAINCO_CLOCK_HANDOFF.md). The baseline below is extended by GUI, fixed schedule, native SQLite and software packet-clock markers. Use Python 3.12 and root uv .venv. Native units are unverified; EDF/BDF/EEGQC readiness is not claimed.

# 范式部署流程

## 准备

1. 使用模板创建任务仓库，下载或克隆至实验机。
2. 安装 uv。必须使用 uv 管理 Python 3.10+ 环境和项目依赖，统一使用项目根目录的 `.venv`；禁止使用 conda。
3. 锁文件使用 PyPI。如本机设置了其他 `UV_DEFAULT_INDEX`，先执行 `$env:UV_DEFAULT_INDEX = 'https://pypi.org/simple'`；切换公司内部镜像时，由维护者更新锁文件。然后在仓库根目录创建环境并同步第三方依赖：

```powershell
uv python install 3.10
uv sync --locked --python 3.10
uv pip check
uv run --no-sync python --version
uv run python -m unittest discover -s tests -v
```

模拟示例在 Python 3.10 上使用 tomli 解析 TOML，Python 3.11+ 使用标准库 tomllib；仍须通过 uv 管理环境。添加或移除依赖时使用 `uv add` / `uv remove`，同步提交 `pyproject.toml` 和生成或更新的 `uv.lock`。离线部署前，应在联网环境准备所需 Python、依赖和 uv 缓存，并确认离线环境可用。

## 源码运行约定

本项目是源码项目，`[tool.uv] package = false`。`uv sync` 只准备项目内 `.venv` 和 `pyproject.toml` 中选定的第三方依赖，不构建、不安装本模板，也不生成 `acq_task` 包、`acq-task` 命令或可编辑安装入口。运行和校验统一使用 `main.py`、`python -m src` 或 `RUNME.bat`：

```powershell
uv run --no-sync python main.py --validate
uv run --no-sync python -m src --validate
uv run --no-sync python main.py --fast
```

不要执行 `uv pip install -e .`、手工添加 `.pth`，也不要为普通新范式新增 `[build-system]`、`[project.scripts]`、`[tool.setuptools]`、控制台脚本或安装别名。新范式保持 flat `src/`，只增加源码模块、配置和测试。

## Python 版本与旧环境迁移

模板核心支持 Python **3.10+**，`.python-version` 默认选择 3.10；CI 在 Windows/Linux 上覆盖 3.10–3.14。3.10 使用带条件标记的 `tomli` 依赖，3.11+ 使用标准库 `tomllib`。新增 GUI 或设备依赖后，必须根据所有依赖的实际 Python 范围收紧具体范式的 `requires-python`，同步更新 `.python-version`、锁文件、CI 和部署文档。例如 PsychoPy 2025.1.0 声明 Python `<3.11`，采用它的范式应选择 `>=3.10,<3.11`，不能仅凭模块能导入就声明支持新版 Python。

模板需要使用其他受支持版本时，在同一终端设置 `UV_PYTHON`，让依赖同步和后续运行选择一致，而不受默认 `.python-version` 影响。例如：

```powershell
$env:UV_PYTHON = '3.11'
uv sync --locked
uv run --no-sync python --version
uv pip check
```

移除当前终端的 `UV_PYTHON` 后恢复默认 3.10；具体范式的版本范围仍由其 `requires-python` 限制。

已有其他 Python 版本或旧可编辑安装的项目，先结束采集、保存本地源码改动并保留 `data/`、`config/*.local.toml`、凭据、`assets/` 及 DVC 指针/缓存，然后执行：

```powershell
git pull --ff-only
$env:UV_DEFAULT_INDEX = 'https://pypi.org/simple'
uv sync --locked --python 3.10
uv pip check
uv run --no-sync python --version
uv run --no-sync python main.py --validate
uv run --no-sync python -m src --validate
```

uv 会重建版本不匹配的 `.venv`；不要复制旧环境，也不要手工修复旧可编辑安装的元数据。若现有 `.venv` 仍残留 `.pth`、安装元数据或 `acq-task` 命令，关闭程序后重建 `.venv`。仓库内旧 `build/`、`dist/`、`*.egg-info/` 不再参与运行，可在确认不含实验数据、私人配置、凭据或素材后清理；不要用 `git clean`，不要删除或移动上述保留内容。具体范式若添加了依赖组，同步与后续 `uv run` 必须选择该范式要求的完整组，避免精确同步移除 GUI/EEG 依赖。使用素材组时可执行 `uv sync --locked --group assets --python 3.10`，再执行 `uv pip check`。

`uv pip check` 必须通过。遇到锁文件过期提示时先核对 Python 范围、`UV_DEFAULT_INDEX` 和锁文件中的源；不要直接在实验机运行无约束升级或重生成锁文件。镜像切换由维护者统一更新并提交锁文件。

## 部署刺激素材

模拟示例无需素材。具体范式首次配置时，维护者按 [素材管理](assets.md) 更新 `metadata.toml` 并运行素材配置脚本，再把 `.dvc/config` 和 `assets/stimuli.dvc` 提交到该范式仓库。桶 `oi-task-assets` 下按 `<unique_name>/dvc/` 分开存储，实验机沿用仓库配置，不自行更改前缀。

实验机默认匿名读取，无需配置 AK/SK、AWS profile 或 rclone。若设置了代理，按 [素材管理](assets.md) 将 NAS 加入绕过列表，然后执行：

```powershell
uv sync --locked --group assets --python 3.10
uv run --group assets dvc pull assets/stimuli.dvc
uv run --group assets dvc status assets/stimuli.dvc
```

若实验机曾配置上传模式，先执行 `uv run --group assets dvc remote modify --local nas allow_anonymous_login true` 恢复匿名读取；本地旧 profile 已不存在时，按素材管理教程清除引用。维护者发布素材需使用凭据，完整的 profile / rclone 上传及恢复匿名模式步骤见 [素材管理](assets.md)。

上述命令要求维护者已发布素材索引。确认本地状态没有差异后再开始试验；若缺失素材、下载失败或内容被修改，应先解决。`main.py --validate` 当前不校验素材。离线实验机须提前下载素材和准备 uv 依赖缓存，不依赖运行时 NAS 访问。

更新部署时先保存本地改动，再执行 `git pull --ff-only`、`uv sync --locked --group assets`、`dvc pull` 和本地状态检查；恢复历史版本也必须恢复其 DVC 素材。记录部署的 Git 提交及素材索引版本。

## 运行前检查

1. 复制 `config/config.toml` 为 `config/experiment.local.toml`，调整任务和输出位置。
2. 执行 `uv run python main.py --config config/experiment.local.toml --validate`。
3. 先加 `--fast` 完成模拟冒烟测试，再正常运行检查试次持续时间和输出文件。
4. 确认输出目录可写、磁盘空间足够，素材与实际设备版本符合试验设计。

## 正常运行

```powershell
uv run python main.py --config config/experiment.local.toml
```

默认配置也可通过双击 `RUNME.bat` 启动。自定义配置在命令行使用 `RUNME.bat --config config/experiment.local.toml`。Linux/macOS 使用上述 `uv run` 命令。运行批处理文件前必须先执行 `uv sync`，确保使用 uv 创建的 `.venv`。

结束后检查 `session.json` 的状态、样本数、试次数，并核对 `events.jsonl`。中断返回 130；失败返回 1，查看终端信息及会话日志。已失败或中断的会话应保留，重新运行会生成新目录。

正式试验前必须完成真实设备接入及硬件时序验证。当前模板默认输出的是模拟数据。

## 采集结束后本地保存与 EEGQC 上传

文件正常关闭后，将完整记录保存在本地。按 [数据存储与交付规范](../data/README.md) 完成正式交接包导出和核查，再按 [本地保存与 EEGQC 上传](data-transfer.md) 将完整的被试会话目录或保留相对路径的 ZIP 导入 EEGQC，核对识别到的 run 数量，确认被试和范式后评分、上传并保存入库回执。本地原始文件继续保留，实验数据不得放入匿名素材桶 `oi-task-assets`。当前模板不自动上传，模拟输出不能直接作为正式交接包。

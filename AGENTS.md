# 新范式开发与协作指南

本仓库是数采试验任务模板，使用 Python 3.10+，默认版本由 .python-version 固定为 3.10；新增依赖时检查其 Python 支持范围，具体范式可收紧 requires-python 以匹配 GUI 或设备库。开发新范式时，先跑通模拟流程，再增加刺激呈现、事件和真实设备接入。默认示例必须保持可独立运行，不连接硬件、不开始真实采集。

## 先了解模板

当前流程：加载并校验 TOML → 创建独立会话及配置快照 → 打开设备 → 执行试次并记录采样和事件 → 关闭设备 → 保存会话状态。

当前仅实现模拟采集，没有刺激呈现、键盘响应、硬件触发或自动续采。`--validate` 只校验配置，不检查素材；`--fast` 只用于模拟流程验证，不能证明真实时序。不得把这些能力当作已实现。

| 要做的事 | 优先阅读 / 修改 |
| --- | --- |
| 了解运行入口与交付方式 | `README.md`、`main.py`、`RUNME.bat`、`docs/deploy.md` |
| 定义范式身份、名称和简介 | `metadata.toml` |
| 定义范式、试次、刺激、事件及终止条件 | `docs/README.md` |
| 增加实验参数和边界校验 | `config/config.toml`、`src/config.py`、`config/README.md` |
| 增加 CLI 参数、选择设备与启动流程 | `src/cli.py` |
| 修改试次流程、事件、会话记录及清理 | `src/runner.py` |
| 接入设备、实现模拟替身 | `src/devices.py` 中的 `Device` / `MockDevice` |
| 配置素材存储、下载和发布 | `scripts/configure_assets.py`、`docs/assets.md`、`assets/README.md` |
| 修改输出结构、本地保存或 EEGQC 交付 | `docs/data-format.md`、`data/README.md`、`docs/data-transfer.md` |
| 检查成功、失败和中断路径 | `tests/test_template.py` |

源码文件直接位于 `src/`，通过 `src.*` 导入，根目录 `main.py`、`python -m src` 和 `RUNME.bat` 是运行入口。项目只从源码运行：`pyproject.toml` 设置 `[tool.uv] package = false`，`uv sync` 只准备 `.venv` 和第三方依赖，不构建或安装本仓库，也不提供 `acq_task` 命名空间、`acq-task` 控制台命令或可编辑安装入口。不要按常见的 `src/acq_task/` 布局凭空创建第二套实现，也不要新增 `[build-system]`、`[project.scripts]`、`[tool.setuptools]` 或安装别名。普通新范式沿用这一结构，只增加源码模块、配置和测试。新增子包或改变目录结构时，同步检查 `python -m src`、测试和文档。

## 第一步：跑通基线并确定范式身份

必须使用 uv 管理 Python 环境和依赖，统一使用项目根目录的 `.venv`，禁止使用 conda。在仓库根目录执行：

```powershell
uv sync --locked --python 3.10
uv run python -m unittest discover -s tests -v
uv run python main.py --validate
uv run python main.py --fast
```

锁文件使用 PyPI。若本机 `UV_DEFAULT_INDEX` 指向其他镜像，先执行 `$env:UV_DEFAULT_INDEX = 'https://pypi.org/simple'`；切换组织内部镜像需由维护者更新并提交锁文件。模拟运行会在 `data/` 生成会话，不要将其提交。

### 旧可编辑环境迁移

旧版本可能曾用 `uv pip install -e .` 或 `acq-task` 命令运行。迁移时先结束采集，保留 `data/`、`config/*.local.toml`、凭据、`assets/` 和 DVC 指针/缓存，再执行 `git pull --ff-only` 和 `uv sync --locked`。如果现有 `.venv` 仍留有旧可编辑 `.pth`、安装元数据或控制台脚本，关闭程序后重建 `.venv`；仓库内旧 `build/`、`dist/`、`*.egg-info/` 不再参与运行，可在确认不含实验数据、私人配置、凭据或素材后清理。不要用 `git clean`，不要删除或移动上述数据与配置。迁移后用 `uv run --no-sync python main.py --validate`、`uv run --no-sync python -m src --validate` 和测试确认源码入口。

从模板创建具体范式仓库时，必须同步更新根目录 `metadata.toml` 的全部三个字段，并修改 `config/config.toml` 的 `experiment.name`、仓库说明和适用的项目元数据；不要为了改名随意重构源码模块名或 CLI，也不要因此引入打包配置。NAS 素材的一级目录统一使用元数据的 `unique_name`，保留原始大写与日期，不另行分配目录 ID。采集数据保存在本地，完成核查后交由 EEGQC 评分、上传和入库。

### metadata.toml 字段与命名规范

三个字段均为 TOML 顶层的必填非空字符串，不放入额外配置节：

| 字段 | 要求 |
| --- | --- |
| `full_name` | 实验完整名称，可使用中文，应与实验设计及仓库说明中的名称一致。 |
| `unique_name` | 实验唯一名称，格式为 `1–6 个大写英文字母-YYYYMMDD`，例如 `ERP-20261022`；字母部分仅限 `A`–`Z`。 |
| `description` | 简单介绍实验目的和主要任务，描述实际范式，不保留模板通用简介或宣称未实现的能力。 |

`unique_name` 必须匹配 `^[A-Z]{1,6}-[0-9]{8}$`，日期还必须是有效日历日期，使用实验建立日期，不使用每次采集或提交的日期。同日创建多个实验时分配不同的字母简称，在团队范围内确认完整名称标识不重复；实验建立后保持唯一名称稳定，不因运行、上传或重试自动生成新名称。

创建新范式不能保留模板的 `full_name`、`ACQTPL-20261022` 或模板描述。实验名称或说明变化时同步检查元数据；确需变更唯一名称时，在实验说明中记录旧名称与新名称的对应关系，不改写历史采集记录。

`unique_name` 直接作为 NAS 素材的一级目录：`oi-task-assets/<unique_name>/dvc/`。它与 `experiment.name`、每段记录的 `session_id` 分开管理。素材配置脚本通过 `src/project_metadata.py` 读取并严格校验元数据，无效时停止，禁止回退到仓库名或手写前缀。`main.py --validate` 仍只校验实验配置。

## 第二步：先明确实验设计

在 `docs/README.md` 中记录范式目的、版本、试次条件、刺激与响应、随机化和随机种子、事件编码、设备参数、时钟同步、终止条件及数据质控要求。记录正常完成、用户中断、素材缺失和设备异常时的预期行为。

对用户未指定但会影响实验有效性的参数，不要自行编造为正式方案；列出待确认项，先完成不依赖这些决定的模拟框架。每次实现只覆盖明确的范式需求，保持示例和文档与实际能力一致。

## 第三步：配置并准备素材

素材使用 DVC + RustFS S3，endpoint 为 `http://truenas-s658v2.omni-intel.cn:9000`，桶为 `oi-task-assets`，每个范式独立使用 `<unique_name>/dvc/`。先更新 `metadata.toml`，再生成配置：

```powershell
uv sync --locked --group assets
uv run --group assets python scripts/configure_assets.py
```

初始化脚本读取并校验 `metadata.toml`，不再接受位置参数指定目录。复制模板后先替换 `ACQTPL-20261022` 等元数据，再运行脚本并提交 `.dvc/config`。素材本体放入 `assets/stimuli/`，Git 只保存 `assets/*.dvc`、清单和说明。首次发布前没有 `assets/stimuli.dvc`，无素材的模拟示例不需要拉取。

- 实验机默认匿名读取，已有索引后执行 `uv run --group assets dvc pull assets/stimuli.dvc`，再执行 `uv run --group assets dvc status assets/stimuli.dvc` 确认本地无差异。
- 发布素材时先按 `docs/assets.md` 配置本机凭据，通过 `.dvc/config.local` 将 `allow_anonymous_login` 设为 `false`，再执行 `dvc add` / `dvc push`。上传成功后才发布 Git 索引；上传完成后恢复本机匿名模式。
- 默认共享配置必须保持匿名读取。凭据、本机配置和缓存不进 Git，不在聊天或日志中输出密钥。
- 匿名读取覆盖整个素材桶，范式目录不提供读取权限隔离。不要放入被试信息、采集数据或未获授权的素材。
- 在试验前将素材下载到本地；不要在正式试次内下载、通过 NAS 流式播放或修改 DVC 对象。不要自行对共享存储执行远端垃圾回收。

完整的 AWS profile / rclone 上传、匿名恢复及历史版本教程以 `docs/assets.md` 为准。

## 第四步：实现配置、试次和设备

实验参数放入 TOML，通过 `Config` 和 `load_config()` 显式建模和校验。新增字段时同时更新允许字段集合、类型与范围校验、默认示例、配置说明及相关测试；未知配置项必须报错。当前相对路径以配置文件目录为基准，新增素材路径也应明确并记录解析规则。

`config/*.local.toml` 是被忽略的完整本机配置，不会与默认配置合并。不要在配置中放密钥，因为运行时会保存配置快照。

简单试次流程从 `src/runner.py` 扩展；刺激呈现、试次条件生成、响应处理和触发控制变复杂时，拆成职责明确的模块，由执行器协调。保留模拟替身，使无设备、无 NAS 的测试仍可运行。素材加载应检查缺失和无效文件，并在开始正式采集前报告错误。

真实设备实现 `Device.open/read_sample/close`，同步扩展 backend 校验，在 CLI 中显式构造适配器并传入 `run(..., device=...)`。当前配置只接受 `mock`，不能只修改 TOML 就接入硬件。禁止在模块导入时打开设备；`close()` 应能处理部分初始化和重复清理。

当前执行器按主机时钟逐点模拟采样。真实设备应根据其 API 使用缓冲采集，保存硬件时间戳、序号、丢样及同步状态，并将刺激呈现与文件写入合理分离。不要用 Python `sleep` 或模拟测试结果宣称达到毫秒级精度；正式使用前完成硬件实测。

## 第五步：保留可追溯性和失败处理

- 保留会话隔离、配置快照、事件记录及异常资源释放。每次运行创建新目录，不覆盖或自动删除已有采集数据。
- 新事件记录稳定的名称或编码、试次和条件等必要上下文，区分计划呈现时间与实际发生时间；在试验说明中定义含义。
- 为具体范式补充代码版本、素材索引版本和随机化信息的会话记录；这些字段当前并未全部实现，不能只写在文档中宣称已有。
- 保持成功、失败、中断状态及退出码一致：正常 0、失败 1、Ctrl+C 130。设备和呈现资源须在异常路径释放，失败或中断的记录仍应保留。
- 修改数据字段时同步更新 `docs/data-format.md`；不兼容变更提升输出 `schema_version`。当前输出为模拟 CSV / JSON，尚未实现 `data/README.md` 所述的 多模态会话导出。正式数据按被试和本地日期会话组织，每段连续记录使用独立 run，配套文件沿用同一前缀。

## 第六步：验证和交付

代码修改后至少执行基线单元测试、配置校验和 `--fast` 模拟冒烟测试。对新增行为补充有意义的测试，重点覆盖配置边界、试次与事件顺序、随机化可复现、素材缺失、设备异常、用户中断及资源清理。单纯修改文档时核对内容和命令即可。

素材相关变更额外检查本地 DVC 状态；修改存储或认证流程时，使用隔离的临时前缀验证凭据上传、干净目录匿名下载及内容一致性，并清理自己的测试对象，不改动其他范式。常规单元测试不得依赖真实硬件、NAS 或上传密钥。

发布前确认 `metadata.toml` 三个字段已更新为当前范式信息、命名合法且与实验说明一致；按 `docs/deploy.md` 在干净环境同步第三方依赖并从源码验证启动，并对接入的硬件单独验证真实时序。交付说明列明改动、运行命令、验证结果、尚未实现的能力及需实验机完成的检查；不要把模拟通过等同于正式实验验收。

不提交被试个人信息、真实采集数据、凭据或未获授权素材。仓库尚未选择开源许可证，不自行增加许可证或声明第三方素材授权。提交说明使用英文；包含 Codex 贡献的提交添加 `Co-authored-by: Codex <codex@openai.com>`。

# 实验机环境加速与旧版升级

双击 `启动GUI.bat` 即可准备环境并打开启动器；只安装环境时运行 `安装环境.bat`。Windows x64 实验机需要连接实验室网络，使 `172.16.4.2:9000` 可达。第一次约下载 161 MiB，之后复用本地缓存，安装时会显示步骤和下载进度。实际速度取决于内网和磁盘。

## 工作方式

1. `config/runtime-bootstrap.json` 固定环境包 SHA-256、体积和 NAS 地址，同时绑定 `uv.lock` 与 `config/requirements-runtime.txt` 的哈希。
2. 启动脚本绕过系统 HTTP 代理，从 NAS 匿名读取环境包，验证 SHA-256 后解压至项目 `.tools/runtime-<哈希>`。无需向其他电脑分发 NAS 写入密钥。
3. 包内包含官方 Python 3.12 x64、uv 以及按 `uv.lock` 选出的 29 个 Windows 运行 wheels。用 uv 创建根目录 `.venv`，执行 `uv pip sync --offline --no-index --find-links ... --require-hashes config/requirements-runtime.txt`。运行要求与锁文件一致，不安装开发测试或 DVC 依赖。
4. 检查 Qt、OpenGLWidgets、NumPy、Pillow、Boto3、强脑 SDK 导入，然后启动 GUI。后续启动不再下载环境包。网络失败会报错，可以重试，不会悄悄退回慢速公网安装。

GUI 仅依赖 `PySide6-Essentials`，包含实际使用的 QtCore、QtGui、QtWidgets 和 QtOpenGLWidgets。已移除不使用的 WebEngine、PDF、Addons 和 PySide6 汇总包，减少约 167 MiB 下载。采集参数、打标、整段原始流保存和图片下载方式保持不变。

## 已分发旧版电脑

先结束采集并关闭程序；如果仍停在依赖下载，可按 Ctrl+C 停止。更新完整源码后双击 `启动GUI.bat`。若旧 `.venv` 使用其他 Python，脚本将其保留在 `.tools/previous-venv-<随机ID>`，再用包内统一的 Python 创建 `.venv`，避免早期 Python 3.12.0 与新版 Qt 的导入兼容问题。当前环境已使用同一个包内 Python 时直接复用。备份仅用于保留旧文件，不保证移动后的旧环境可直接运行。

Git 克隆目录可执行 `git pull --ff-only`。ZIP 解压的目录请使用新版代码包，保留 `data/`、`assets/stimuli/`、`.local/`、`config/*.local.toml`、本机被试设置和凭据，不能用删除整个旧项目的方式升级。可将新的代码包解压至独立文件夹验证，正式续采前须迁移完整本地数据及本机配置。源码不包含任何受试者数据。

## 维护者更新依赖

开发环境仍可用 `uv sync --locked --python 3.12` 并运行测试。运行包是 lock 派生的部署缓存，不是复制其他电脑的 `.venv`。

1. 更新 `pyproject.toml` 和 `uv.lock`，运行 `uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file config/requirements-runtime.txt`。
2. 使用 `scripts/build_runtime_bundle.py --folder <新的构建目录> --uv <uv.exe路径> --python-url <官方独立Python发行包URL>` 构建，自动生成运行包和配置。构建目录不得包含上次无关 wheels。来源与逐文件哈希保存在包内 `runtime-manifest.json`。
3. 使用本机授权凭据上传包到配置中的 NAS 对象地址，匿名读回验证 SHA-256，在全新目录实测离线安装。不要提交密钥、运行 ZIP 或 wheels。
4. 同步提交锁文件、requirements 和 runtime 配置，保持字节哈希一致，再发布源码。

本流程验证环境安装和 GUI，不替代逐机设备连通性、显示时序及实际采集质控。

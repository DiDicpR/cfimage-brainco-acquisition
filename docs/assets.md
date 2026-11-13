# 素材管理：DVC + RustFS S3

本方案用于公司内网。uv 管理 Python 环境与依赖，DVC 管理素材版本，Git 保存代码、素材索引和说明；禁止使用 conda。实验机默认匿名读取素材，维护者上传时使用凭据。

## 存储与范式划分

- Endpoint：`http://truenas-s658v2.omni-intel.cn:9000`。
- 桶：`oi-task-assets`，管理员已配置匿名只读；模板不创建桶、不修改桶策略。
- 每个范式独立使用 `s3://oi-task-assets/<unique_name>/dvc/`。
- 一级目录严格取根目录 `metadata.toml` 的 `unique_name`，例如 `ERP-20261022`；保留大小写和日期，不使用仓库名或独立的范式 ID。同一实验的不同素材版本共享前缀。
- 模板唯一名称为 `ACQTPL-20261022`；创建具体任务仓库时先更新元数据，再运行配置脚本。

```text
oi-task-assets/
  ERP-20261022/dvc/files/md5/...
  SSVEP-20261022/dvc/files/md5/...
  ACQTPL-20261022/dvc/files/md5/...
```

对象名由 DVC 按内容生成，原始文件名及目录布局通过 DVC 索引恢复到本机 `assets/stimuli/`。不要在 S3 控制台或通过 SMB 修改、移动这些对象，也不要把 RustFS 底层数据目录当普通共享目录操作。

桶允许匿名 ListBucket、GetBucketLocation 和 GetObject；不授予匿名上传、删除及策略管理权限。任何能连接 NAS 的客户端都能列出和读取桶内素材，范式前缀用于组织内容，不提供读取权限隔离。被试信息、采集数据和其他非共享内容不得放入此桶。

## 1. 初始化具体范式

先更新仓库根目录 `metadata.toml` 的三个字段，再在仓库根目录执行：

```powershell
uv sync --locked --group assets --python 3.10
uv run --group assets python scripts/configure_assets.py
uv run --group assets dvc remote list
```

脚本读取自身所在仓库的 `metadata.toml`，校验字段、命名和日期，再配置目录及默认匿名读取（`allow_anonymous_login = true`）。不接受手工目录参数；无效元数据在修改 DVC 配置前报错。脚本不上传数据、不更改实验名称；仍需同步修改 `config/config.toml`。提交元数据和 `.dvc/config`，让实验机沿用同一配置。

已有素材的仓库改变前缀后，维护者需要从完整本地缓存重新上传；旧对象不会自动迁移或删除。复制仓库文件时不要复制 `.dvc/config.local`，其中可能留有机器专用设置。

素材工具位于可选的 `assets` 依赖组，所有 DVC 命令使用 `uv run --group assets`。模拟示例无需素材，可执行 `uv sync --locked` 只同步默认第三方依赖，不下载 DVC 组。模板不附带占位素材，首次发布前没有 `assets/stimuli.dvc`。

锁文件使用 PyPI。本机若设置了其他 `UV_DEFAULT_INDEX`，先在当前 PowerShell 执行 `$env:UV_DEFAULT_INDEX = 'https://pypi.org/simple'`；公司若统一切换内部镜像，由维护者更新并提交锁文件。离线机器需预先准备依赖和素材。

如本机设置了代理，将 NAS 加入绕过列表；保留已有条目：

```powershell
$env:NO_PROXY = (@($env:NO_PROXY, 'truenas-s658v2.omni-intel.cn') | Where-Object { $_ }) -join ','
```

9000 端口当前提供 HTTP，HTTPS 握手未成功。配置保留证书验证；部署 TLS 后统一修改 `.dvc/config` 的 `endpointurl`，并在实验机安装内部 CA。

## 2. 实验机匿名下载

维护者已发布 `assets/stimuli.dvc` 后，实验机无需 AK/SK、AWS profile 或 rclone 配置：

```powershell
git pull --ff-only
uv sync --locked --group assets
uv run --group assets dvc pull assets/stimuli.dvc
uv run --group assets dvc status assets/stimuli.dvc
uv run python main.py --validate
```

若本机以前配置了上传模式，先恢复本机匿名模式：

```powershell
uv run --group assets dvc remote modify --local nas allow_anonymous_login true
```

`.dvc/config.local` 的设置优先于仓库配置。匿名模式会忽略 S3 认证凭据；若该本地文件仍指定了已删除的 AWS profile，使用 `uv run --group assets dvc remote modify --local --unset nas profile` 清除旧引用。

在试验前完成下载和本地状态检查，确认没有内容差异。正式试次从本地磁盘读取，不访问 NAS 或自动拉取。`main.py --validate` 当前只检查实验配置，不检查素材；`dvc status --cloud` 检查远端缓存状态，也不能替代本地状态检查。

## 3. 维护者配置上传凭据

选择下面一种方式。凭据只留在本机，禁止将 AK/SK 或会话令牌放入 Git、脚本、命令参数或日志。维护者账号需具备该范式前缀的 PutObject、GetObject 和相应 ListBucket 权限；DeleteObject 仅供受控清理与测试使用。

### 方式 A：专用 AWS profile

由管理员安全配置本机 `~/.aws/credentials`：

```ini
[oi-task-assets]
aws_access_key_id = <本机配置，不提交>
aws_secret_access_key = <本机配置，不提交>
```

```powershell
uv run --group assets dvc remote modify --local nas profile oi-task-assets
uv run --group assets dvc remote modify --local nas allow_anonymous_login false
```

也可在首次初始化时执行 `uv run --group assets python scripts/configure_assets.py --profile oi-task-assets`。仓库配置仍为匿名读取，profile 名称和认证模式仅写入被 Git 忽略的 `.dvc/config.local`。

### 方式 B：复用 rclone omni-oss 的静态 AK/SK

在专用 PowerShell 终端执行，配置对象和密钥不要输出。先清除本地 profile 引用（若存在），避免与环境凭据混用：

```powershell
# 仅在以前设置过本地 profile 时执行：
uv run --group assets dvc remote modify --local --unset nas profile
```

```powershell
$rcloneConfig = rclone config dump | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Cannot read rclone configuration' }
$assetRemote = $rcloneConfig.'omni-oss'
if (-not $assetRemote.access_key_id -or -not $assetRemote.secret_access_key) {
    throw 'omni-oss is missing S3 credentials'
}
$env:AWS_ACCESS_KEY_ID = $assetRemote.access_key_id
$env:AWS_SECRET_ACCESS_KEY = $assetRemote.secret_access_key
Remove-Item Env:AWS_SESSION_TOKEN -ErrorAction SilentlyContinue
$rcloneConfig = $null
$assetRemote = $null
uv run --group assets dvc remote modify --local nas allow_anonymous_login false
```

这只复用凭据，DVC 仍连接仓库配置的 NAS 域名。后续上传命令必须在同一个终端执行；只加载密钥但未关闭匿名模式，上传仍会被拒绝。

## 4. 发布或更新素材

将素材放入 `assets/stimuli/`，可按 `images/`、`audio/`、`video/` 组织。可在 `assets/manifest.csv` 记录相对路径、来源、授权、用途及版本。配置好上传凭据后执行：

```powershell
uv run --group assets dvc add assets/stimuli
uv run --group assets dvc push assets/stimuli.dvc
if ($LASTEXITCODE -ne 0) { throw 'Asset upload failed; do not publish the Git index' }
uv run --group assets dvc status --cloud assets/stimuli.dvc
```

先确认上传成功，再检查并提交 `assets/stimuli.dvc`、`.gitignore`、`.dvc/config`、素材清单及相关代码，最后发布 Git 提交。修改素材后重复上述步骤。二进制内容和 `.dvc/config.local` 不提交 Git。

操作完成后恢复匿名读取。使用方式 B 的专用终端还应清除进程内密钥，或直接关闭终端：

```powershell
uv run --group assets dvc remote modify --local nas allow_anonymous_login true
# 仅针对方式 B 的专用终端：
Remove-Item Env:AWS_ACCESS_KEY_ID, Env:AWS_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
```

匿名读取模式不能用于上传；上传失败先检查本地 `allow_anonymous_login` 是否为 `false`，再确认凭据。`InvalidAccessKeyId` 表示服务不接受所用 AK；`AccessDenied` 需检查认证模式和账号权限。

相同内容可复用缓存，修改过的视频通常需要保存完整新对象，不应假定二进制差分上传。不要自行对共享桶执行 `dvc gc --cloud` 或设置按时间删除对象的生命周期规则，以免历史版本失效。清理由管理员结合所有需保留的仓库引用统一处理；NAS 另需快照或备份。

## 5. 更新部署和恢复历史版本

先保存本地修改，再执行 `git pull --ff-only`、`uv sync --locked --group assets`、`dvc pull assets/stimuli.dvc` 和本地状态检查。记录部署的 Git 提交与素材索引版本。

恢复历史版本时，先执行 `git checkout <commit-or-tag>`，再同步对应依赖并拉取该版本素材。不要强制覆盖未保存的素材修改。断网时仅能使用已下载的素材和完整本地缓存。

## 验收记录

2026-10-22 已验证指定域名和桶的 S3 基本操作，以及 DVC 凭据上传、干净工作目录匿名拉取、逐字节内容比较和本地状态检查。匿名列目录、查询桶位置、HeadObject 和 GetObject 成功，匿名上传及删除均返回 403。测试对象均已清理。该验证覆盖小对象与当前 DVC 工作流，不代表大视频分段上传或所有 RustFS API 均已验证。

参考：[DVC S3 配置](https://doc.dvc.org/user-guide/data-management/remote-storage/amazon-s3)、[DVC 状态检查](https://origin-doc.dvc.org/command-reference/status)、[RustFS S3 兼容范围](https://docs.rustfs.com/en/reference/s3-compatibility)。

## unique_name 目录切换

素材使用 `oi-task-assets/<unique_name>/dvc/`，一级目录来自 `metadata.toml`。采集数据保存在本地，使用 EEGQC 上传，见 [本地保存与 EEGQC 上传](data-transfer.md)。先更新元数据，再执行 `uv run --group assets python scripts/configure_assets.py`；修改元数据不会自动改写已有 `.dvc/config`，发布前须确认二者一致。本机 `.dvc/config.local` 如覆盖了 `url`，需清除旧覆盖。

切换配置不会迁移已有 NAS 对象。维护者应先从旧 remote 拉取所有需保留的素材版本到完整缓存，再按本页认证上传流程推送到新前缀，最后在空缓存中验证匿名下载成功，再向实验机交付。不要删除旧前缀；历史提交仍可能引用它。模板当前没有刺激文件或 `assets/stimuli.dvc`，无需迁移远端素材；从模板创建的具体实验仓库须按上述步骤迁移自身已有素材。

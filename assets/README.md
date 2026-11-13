# 刺激素材

图片、音频、视频等素材统一通过 DVC 分发，实际内容存放在公司 NAS 的 RustFS S3 中；Git 只保存素材索引和说明。默认匿名读取，实验机无需配置密钥；维护者上传时切换本机认证模式并加载凭据。模拟示例无需素材。

- `stimuli/`：当前范式的素材工作目录，由 DVC 管理，二进制内容不进入 Git。
- `stimuli.dvc`：首次执行 `dvc add assets/stimuli` 后生成，随代码提交。
- `manifest.csv`：素材说明清单，随代码提交；建议记录相对路径、来源、授权、用途及版本。DVC 索引负责内容版本，额外校验和可按试验要求记录。
- 其他临时素材默认忽略，禁止用 `git add -f` 把大文件加入 Git。

每个范式使用独立的 `s3://oi-task-assets/<unique_name>/dvc/` 前缀。复制模板后，先更新 `metadata.toml` 并执行 `uv run --group assets python scripts/configure_assets.py`，再上传素材。不要将被试信息或采集输出放进素材桶。

完整步骤见 [素材管理](../docs/assets.md) 和 [部署说明](../docs/deploy.md)。

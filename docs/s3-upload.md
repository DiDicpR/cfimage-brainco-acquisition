# S3 上传（可选，当前未启用）

当前默认使用已验证可写的 SMB 公共目录，见 `public-upload.md`。
oi-data/CFIMAGE-20261113 的实际 S3 写入测试返回 AccessDenied；管理员授权后方可切换 backend=s3。

切换到 S3 后，结束并保存采集，在工作台点击“上传当前被试全部数据”。S3 目的地为
`s3://oi-data/CFIMAGE-20261113/sub-<被试>/<电脑标识>/`，不需要 SMB 登录。
各电脑上的全部本地会话、连续 EEG、wire_archives、事件、配置和被试进度均进入快照。
其他电脑的同名被试不会自动汇总。上传是备份，不代表 EEG 质量已经合格。

每个对象上传后从 S3 读回并核对 SHA-256，最后发布快照清单；失败时不会发布完整快照。
重试复用已验证的对象，本地数据保留。文件按内容哈希保存，恢复时重建原始路径：

```
uv run --no-sync python -m src.nas_transfer restore --snapshot <上传结果的snapshot_key> --output D:\EEG-restore
```

Windows 授权保存在 `%LOCALAPPDATA%\CFIMAGE\s3-upload.dpapi`，由当前 Windows 账号
通过 DPAPI 加密，不进入数据快照。换 Windows 账号或换电脑需要重新授权。
有授权包时，启动 GUI 的环境脚本会自动导入并删除本机的便携授权包。
启动器的“检查 S3 上传授权”可测试上传、读回和删除一个独立测试对象。
显式配置的 AWS profile 仍然优先；本机 `transfer.local.toml` 的后端覆盖仍然生效。

便携授权包包含安装所需的解密材料，持有者可以恢复账号密钥；它不提供“只有程序能读取”的保证。
应通过私有渠道发送。安装后的 DPAPI 文件不能直接拷到另一台电脑使用。
即使临时将授权包公开、分发后删除并改写 Git 历史，也不能保证撤回他人的副本或 GitHub 缓存。
公开过的密钥应由 NAS 管理员轮换；轮换后各电脑也需要重新安装授权。

客户端只将此授权用于配置中固定的 endpoint、oi-data 桶和 CFIMAGE-20261113 前缀。
这是客户端约束，不是服务端权限收窄。账号实际权限仍取决于 NAS 的策略。
素材继续匿名读取 oi-task-assets，不使用数据上传密钥。

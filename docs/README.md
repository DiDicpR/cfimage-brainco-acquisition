> BrainCo uv implementation (2026-11-13): see [current protocol and clock/data contract](BRAINCO_CLOCK_HANDOFF.md). The baseline below is extended by GUI, fixed schedule, native SQLite and software packet-clock markers. Use Python 3.12 and root uv .venv. Native units are unverified; EDF/BDF/EEGQC readiness is not claimed.

# 试验设计与扩展

模板流程：读取并校验配置 → 创建会话及配置快照 → 打开设备 → 重复试次（开始事件、采样、结束事件）→ 关闭设备 → 保存会话状态。

复制模板后，请补充：

1. 试验目的、范式名称、版本及负责人。
2. 被试匿名标识规则、准备步骤和终止条件。
3. 刺激素材版本、呈现方式、试次随机化规则及随机种子。
4. 设备型号、固件、采样率、通道映射和连接参数。
5. 事件编码、触发方式、时钟同步及实际时序验证结果。
6. 数据质控、丢样处理、失败重试与备份策略。

## 接入真实设备

实现 `Device.open/read_sample/close`，在配置中加入明确的连接参数和 backend 枚举，在 CLI 中显式构造设备并交给 `run(..., device=...)`。不要在导入模块时打开设备。`close` 应允许在部分初始化后调用。

示例执行器以主机单调时钟记录相对时间。真实设备应扩展数据协议以保存硬件时间戳、序号、丢样和同步状态；按设备 API 使用缓冲式采集，分离刺激呈现与文件写入，完成实测后再用于正式试验。

## 已知边界

- 默认示例只有模拟信号和试次事件，没有刺激呈现或触发输出。
- 样本数量为每试次 `int(duration_s * sample_rate_hz)`，小数部分向下取整。
- Ctrl+C 和 Python 异常会尽力保存状态；断电、磁盘故障和强制结束进程不保证落盘，可能留下 `running` 状态，需人工核查。
- 没有自动续采功能；重跑总是创建新会话。

另见 [部署](deploy.md) 和 [数据格式](data-format.md)。

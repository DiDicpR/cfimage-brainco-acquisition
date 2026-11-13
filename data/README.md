> BrainCo uv implementation (2026-11-13): see [current protocol and clock/data contract](../docs/BRAINCO_CLOCK_HANDOFF.md). The baseline below is extended by GUI, fixed schedule, native SQLite and software packet-clock markers. Use Python 3.12 and root uv .venv. Native units are unverified; EDF/BDF/EEGQC readiness is not claimed.

# 本地多模态采集数据存放规范

一次采集会话保存在 `data/sub-{SUB_ID}/ses-{YYMMDD##}/`。会话内各模态共用被试、会话、任务和 run 前缀。采集程序在本地写盘、核查并封存，操作者随后使用 EEGQC 评分、上传和入库。

本规范采用公司多模态目录和 CSV 表。需要交付标准 EEG-BIDS 时，另外生成派生导出并对导出结果运行 BIDS Validator。模板当前运行的是模拟采集，输出见 [模拟数据格式](../docs/data-format.md)。

## 目录和文件名

下例使用 `{P}` 缩写公共文件名前缀。实际文件名必须展开，例如 `sub-00000001_ses-26110901_task-rsvp_run-01_eeg.edf`。同一 run 的行为、视频和 EEG 均使用这一前缀。

```text
data/
└── sub-00000001/
    └── ses-26110901/
        ├── session.json
        ├── eeg/
        │   ├── {P}_eeg.edf       （也可选 {P}_eeg.bdf，每个 run 选一种）
        │   ├── {P}_eeg.json
        │   ├── {P}_channels.csv
        │   ├── {P}_events.csv
        │   └── {P}_events.json
        ├── behavior/
        │   ├── {P}_behavior.csv
        │   └── {P}_behavior.json
        ├── video/
        │   ├── {P}_face.mp4      {P}_face.csv      {P}_face.json
        │   ├── {P}_ego.mp4       {P}_ego.csv       {P}_ego.json
        │   ├── {P}_env1.mp4      {P}_env1.csv      {P}_env1.json
        │   └── {P}_env2.mp4      {P}_env2.csv      {P}_env2.json
        └── emg/
```

- **被试**使用人员平台已有的 8 位编号，以字符串保存，保留前导零。
- **会话**按采集地的会话开始日期取 `YYMMDD`，同一被试当日从 `01` 顺序分配至 `99`。例如 `ses-26110902` 表示该被试在 2026 年 11 月 9 日的第二次会话。跨午夜沿用开始日期。创建目录时检查占用，已有会话保留，编号用尽时停止分配。
- **任务**使用稳定的字母数字标签，例如 `task-rsvp`。完整业务范式代码写入 `session.json.paradigm_code`。一个会话对应一个业务范式。
- **run**在会话内从 `run-01` 顺序编号。每段连续记录对应一个 run，中断后恢复采集时在原会话创建下一 run。各模态沿用该段记录的 run 编号。中断前后的时间间隙原样保留，每段信号从自己的首样本计时。
- 只创建实际采集的模态和机位。各 CSV 使用 UTF-8、逗号分隔和带列名的表头，JSON 使用 UTF-8。配套文件存放在对应模态目录内。

## 会话标识与元数据

`session.json` 放在会话目录。建议保存 `schema_version`、`participant_id`、`session_id`、`paradigm_code`、带时区的 ISO 8601 起止时间、`recording_status`、`stop_reason` 和采集软件名称、版本。目录标签用于定位，`session_id` 建议使用会话创建时生成的稳定 UUID，供 EEGQC 识别来源会话。同一会话各 run 共用该 ID，上传重试沿用原值。平台入库返回的平台 Session ID 单独保存。

建议在 `runs` 数组中逐段记录 run 编号、真实起止时间、完成或中断状态、停止原因及各模态文件的会话相对路径。可为每段分配独立的 `recording_id` UUID，用于追溯原始记录。该数组是存储设计建议，EEGQC 通过扫描 `eeg/` 发现信号文件。历史单段交接包保留原始标识及目录。

## 各模态的文件内容

| 文件 | 存放要求 |
| --- | --- |
| `eeg/{P}_eeg.edf` 或 `.bdf` | 写入真实连续信号，结束时关闭文件。头部的采样率、通道标签、单位及量程与设备一致。以实际样本数计算时长。 |
| `eeg/{P}_eeg.json` | 记录任务、采样率、电源频率、参考、滤波、设备厂家与型号、记录类型和各类通道数。建议沿用已有键名 `TaskName`、`SamplingFrequency`、`PowerLineFrequency`、`EEGReference`、`SoftwareFilters`。 |
| `eeg/{P}_channels.csv` | 每行一个实际存储通道，顺序与信号一致。至少保存 `name,type,units`，区分 EEG、EOG、ECG、触发和其他辅助通道。 |
| `eeg/{P}_events.csv`、`{P}_events.json` | CSV 每行一个事件，前两列 `onset,duration` 以秒计，本 run 实际保存的第一个 EEG 样本对应零秒。JSON 解释事件码、字段、单位和时间来源。刺激呈现、Trigger 发送与设备回读分别标识。 |
| `behavior/{P}_behavior.csv`、`{P}_behavior.json` | CSV 按范式约定逐试次或逐响应保存，记录可关联的试次或事件标识。JSON 说明每行含义、列名、取值、单位、缺失值和时间基准。 |
| `video/{P}_{机位}.mp4` | 每个实际机位独立保存。机位标签为 `face`、`ego`、`env1`、`env2`。 |
| `video/{P}_{机位}.csv` | **每行一个视频事件**。建议保存事件标识、类型、视频内时间，并在可用时记录关联的试次、EEG 事件或帧号。没有事件时保留表头。 |
| `video/{P}_{机位}.json` | 记录视频参数和同步说明，包括机位、分辨率、帧率或可变帧率标记、时间单位、视频时间零点、时钟来源及与 EEG 的对齐方法。 |
| `emg/` 和其他模态 | 采用相同公共前缀。由具体范式定义信号格式、通道、单位、采样率、时钟和配套说明；EMG 的文件扩展名及业务字段待设备协议确定。 |

各设备保留可获得的原始时间戳、样本序号、丢样或丢帧信息。跨设备偏移只填写实测值；同步未测时在配套 JSON 中标明未知。采集端保留可读取的中断、失败和低质量记录，由 EEGQC 生成评分和可用时长。

## EEGQC 交付范围

按 2026-11-09 本地 EEGQC 源码核对，`recording_manifest.discover_sessions` 读取会话 JSON 中的被试、来源会话 ID 和范式，扫描相邻 `eeg/` 下的 EDF/BDF。多个主文件会进入逐段评分和会话汇总。同一会话的范式代码须一致。

`bids_metadata.channel_rows` 当前读取 `*_channels.tsv`。新规范中的 `*_channels.csv` 仍须保存；核查评分时以实际 EDF/BDF 标签及客户端识别结果为准。新的事件 CSV、行为、视频和 EMG 可随完整会话交付，其字段解析、跨模态对齐和专项评分需按范式另行验证。操作步骤见 [本地保存与 EEGQC 上传](../docs/data-transfer.md)。

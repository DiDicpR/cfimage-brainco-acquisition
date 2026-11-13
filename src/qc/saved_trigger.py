"""Read back the on-disk TRG sample; an acknowledged send is not sufficient."""
from pathlib import Path
import struct


def verify_saved_trigger(path: Path, code: int, sample_index: int | None) -> None:
    if sample_index is None or sample_index < 1:
        raise RuntimeError('Trigger 缺少有效 EEG 样本索引。')
    with path.open('rb') as handle:
        header = handle.read(256)
        channels = int(header[252:256])
        header_size = int(header[184:192])
        records = int(header[236:244])
        signal_header = handle.read(256 * channels)
        labels = [signal_header[i * 16:(i + 1) * 16].decode('ascii').strip()
                  for i in range(channels)]
        if 'TRG' not in labels:
            raise RuntimeError('EEG 文件没有 TRG 通道。')
        samples_offset = 216 * channels
        samples = [int(signal_header[samples_offset + i * 8:samples_offset + (i + 1) * 8])
                   for i in range(channels)]
        if path.stat().st_size != header_size + records * sum(samples) * 2:
            raise RuntimeError('EDF 文件大小与头部声明不符。')
        channel = labels.index('TRG')
        record, offset = divmod(sample_index - 1, samples[channel])
        if record >= records:
            raise RuntimeError('Trigger 样本索引超出保存的 EEG。')
        handle.seek(header_size + (record * sum(samples) + sum(samples[:channel]) + offset) * 2)
        saved, = struct.unpack('<h', handle.read(2))
        if saved != code:
            raise RuntimeError(f'Trigger 落盘不一致：样本 {sample_index} 期望 {code}，实际 {saved}。')

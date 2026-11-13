"""Complete EEG frames alongside the SDK's unchanged control/impedance path."""
import numpy as np
import os
from brainco_reference.brainco_acquirer import BrainCoAcquirer
from brainco_wire import FrameDecoder, WireTap


class FullStreamAcquirer(BrainCoAcquirer):
    def _connect_control_session(self):
        if self._sdk is None:
            import bc_ecap_sdk as sdk
            self._prepare_session_state(sdk)
        if self._client is not None:
            return self._wire_target
        addr, port = self._resolve_addr_port()
        self._wire_target = (addr, port)
        gain = getattr(self._sdk.EegSignalGain, f'GAIN_{self._eeg_gain}')
        decoder = FrameDecoder(lambda data: self._sdk.parse_eeg_data(data, gain),
                               self.metadata.sfreq, self._eeg_gain, await_normal_start=True,
                               reorder_samples=max(10, int(self.metadata.sfreq * .2)))
        self.wire = WireTap(decoder, max(20000, int(self.metadata.sfreq * self._buffer_sec)),
                            archive=os.environ.get('BRAINCO_WIRE_ARCHIVE'))
        local_port = self._run_coroutine(self.wire.start(addr, port), timeout=5)
        self._client = self._sdk.ECapClient('127.0.0.1', local_port)
        parser = self._sdk.MessageParser(self._device_id, self._sdk.MsgType.EEGCap)
        self._register_sdk_callbacks()
        self._run_sdk_call(self._client.start_data_stream, parser)
        self._run_coroutine(self.wire.wait_connected(), timeout=6)
        return addr, port

    def _drain_eeg_buffer(self):
        rows = self.wire.take(flush=getattr(self, '_flush_wire', False))
        self._flush_wire = False
        # SDK's four-slot buffer is only a secondary copy, never our EEG source.
        self._sdk.clear_eeg_buffer()
        self.last_sample_indices = [row[0] for row in rows]
        self.last_raw_shape = [len(rows), 33]
        self._raw_packet_count = self.wire.decoder.frames
        data = np.asarray([row[1] for row in rows], dtype=np.float32).T if rows else np.empty((32, 0), dtype=np.float32)
        if data.shape[1]:
            self._append_eeg_samples(data, from_callback=False)
        return data

    def flush_samples(self):
        self._flush_wire = True
        return self.get_new_samples()

    def _stop_loop_thread(self):
        try:
            if getattr(self, 'wire', None) is not None and self._loop is not None:
                self._run_coroutine(self.wire.close(), timeout=3)
        finally:
            super()._stop_loop_thread()

import sys
import unittest
import asyncio
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from brainco_wire import FrameDecoder, WireError, WireTap, crc16
from brainco_worker import CounterClock


def varint(value):
    out=bytearray()
    while value>127:
        out.append((value&127)|128);value>>=7
    out.append(value)
    return bytes(out)


def number(field,value):
    return varint(field<<3)+varint(value)


def message(field,data):
    return varint((field<<3)|2)+varint(len(data))+data


def frame(start=0, slots=(1,2,3,4,8,9,10,11,12,13), rate=1):
    samples=b''.join(message(slot,number(1,start+i)+message(2,bytes([i])*108)) for i,slot in enumerate(slots))
    samples+=number(6,rate)+number(7,4)
    payload=number(1,1)+message(3,message(2,samples))
    result=b'BRNC\x02\x0b'+len(payload).to_bytes(2,'little')+b'\x00\x02\x00'+payload
    return result+crc16(result).to_bytes(2,'little')


class WireTests(unittest.TestCase):
    def decoder(self):
        return FrameDecoder(lambda data:[data[0]*100+c for c in range(32)],250,6)

    def test_ten_samples_all_channels_and_arbitrary_tcp_boundaries(self):
        wire=frame()+frame(10)
        for step in (1,7,256,len(wire)):
            decoder=self.decoder();rows=[]
            for offset in range(0,len(wire),step):
                rows.extend(decoder.feed(wire[offset:offset+step]))
            self.assertEqual([row[0] for row in rows],list(range(20)))
            self.assertEqual(rows[9][1],list(range(900,932)))
            self.assertEqual((decoder.missing,len(decoder.pending)),(0,0))

    def test_crc_corruption_is_rejected(self):
        wire=bytearray(frame());wire[50]^=1
        with self.assertRaisesRegex(WireError,'CRC'):
            self.decoder().feed(wire)

    def test_old_mode_samples_are_discarded_until_normal_acknowledgement(self):
        decoder=FrameDecoder(lambda data:[0.]*32,250,6,await_normal_start=True)
        self.assertEqual(decoder.feed(frame(1000)),[])
        payload=number(1,2)+message(3,number(4,1))
        ack=b'BRNC\x02\x0b'+len(payload).to_bytes(2,'little')+b'\x00\x02\x00'+payload
        ack+=crc16(ack).to_bytes(2,'little')
        rows=decoder.feed(ack+frame(0))
        self.assertEqual([r[0] for r in rows],list(range(10)))
        self.assertEqual(decoder.missing,0)

    def test_rate_mismatch_and_unknown_layout_are_rejected(self):
        with self.assertRaisesRegex(WireError,'sample rate/gain'):
            self.decoder().feed(frame(rate=3))
        with self.assertRaisesRegex(WireError,'layout'):
            self.decoder().feed(frame(slots=(1,2,3,4,14)))

    def test_missing_and_duplicate_device_samples(self):
        decoder=self.decoder()
        decoder.feed(frame(100));decoder.feed(frame(120))
        self.assertEqual(decoder.missing,10)
        with self.assertRaisesRegex(WireError,'reset/wrap/duplicate'):
            decoder.feed(frame(120))

    def test_counter_clock_keeps_internal_and_between_batch_gaps(self):
        clock=CounterClock(250)
        first=clock.batch([100,101,108],10)
        second=clock.batch([112,113],10.02)
        self.assertEqual(first['sample_offsets'],[0,1,8])
        self.assertEqual(second['missing_samples_total'],9)
        self.assertAlmostEqual(second['estimated_start']-first['estimated_start'],12/250)
        with self.assertRaises(ValueError):clock.batch([113],11)


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_does_not_count_as_device_connection(self):
        wire = WireTap(None, 100)
        wire.host, wire.port = '192.0.2.1', 53129
        wire.connected = True
        with self.assertRaisesRegex(WireError, '192.0.2.1:53129'):
            await wire.wait_connected(timeout=.02)

    async def test_failed_upstream_reports_target_promptly(self):
        wire = WireTap(None, 100)
        port = await wire.start('192.0.2.1', 53129)
        real_connect = asyncio.open_connection
        async def connect(host, port):
            if host == '192.0.2.1':
                raise OSError('network unreachable')
            return await real_connect(host, port)
        try:
            with patch('brainco_wire.asyncio.open_connection', side_effect=connect):
                reader, writer = await real_connect('127.0.0.1', port)
                try:
                    with self.assertRaisesRegex(WireError, '192.0.2.1:53129.*network unreachable'):
                        await wire.wait_connected(timeout=.5)
                    self.assertFalse(wire.upstream_ready)
                    self.assertEqual(await asyncio.wait_for(reader.read(), .5), b'')
                finally:
                    writer.close()
                    await writer.wait_closed()
        finally:
            await wire.close()

    async def test_connected_relay_preserves_commands_and_samples(self):
        decoder = FrameDecoder(lambda data:[data[0]*100+c for c in range(32)],250,6)
        wire = WireTap(decoder, 100)
        commands = []
        release = asyncio.Event()
        finished = asyncio.Event()
        async def device(reader, writer):
            try:
                commands.append(await reader.readexactly(5))
                writer.write(frame())
                await writer.drain()
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()
        server = await asyncio.start_server(device, '127.0.0.1', 0)
        try:
            port = await wire.start('127.0.0.1', server.sockets[0].getsockname()[1])
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            try:
                await wire.wait_connected(timeout=.5)
                writer.write(b'start')
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readexactly(len(frame())), .5), frame())
                self.assertEqual(commands, [b'start'])
                self.assertEqual([row[0] for row in wire.take()], list(range(10)))
            finally:
                await wire.close()
                writer.close()
                await writer.wait_closed()
        finally:
            release.set()
            await asyncio.wait_for(finished.wait(), .5)
            server.close()
            await server.wait_closed()


if __name__=='__main__':unittest.main()

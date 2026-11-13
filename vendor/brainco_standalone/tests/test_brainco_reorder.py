import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from test_brainco_wire import frame, number, message, crc16
from brainco_wire import FrameDecoder, WireTap, WireError
from brainco_worker import CounterClock


def decoder():
    return FrameDecoder(lambda b: [b[0]*100+c for c in range(32)],250,6,reorder_samples=50)


def counter_frame(counters):
    payload=b''.join(message(slot,number(1,i)+message(2,bytes([n])*108))
                     for n,(slot,i) in enumerate(zip([1,2,3,4,8,9,10,11,12,13],counters)))
    payload=number(1,1)+message(3,message(2,payload+number(6,1)+number(7,4)))
    packet=b'BRNC\x02\x0b'+len(payload).to_bytes(2,'little')+b'\0\2\0'+payload
    return packet+crc16(packet).to_bytes(2,'little')


class ReorderTests(unittest.TestCase):
    def startup_decoder(self):
        return FrameDecoder(lambda b:[b[0]*100+c for c in range(32)],250,6,
            await_normal_start=True,reorder_samples=50,require_start_command=True,startup_guard_seconds=1.)

    def normal_ack(self):
        payload=number(1,2)+message(3,number(4,1))
        data=b'BRNC\x02\x0b'+len(payload).to_bytes(2,'little')+b'\0\2\0'+payload
        return data+crc16(data).to_bytes(2,'little')

    def test_configuration_ack_does_not_open_stream_before_start_command(self):
        d=self.startup_decoder()
        self.assertEqual(d.feed(self.normal_ack()+frame(1267),received=100),[])
        self.assertIsNone(d.order.highest)
        d.arm_start_command()
        rows=[]
        for n in range(0,300,10):rows+=d.feed(frame(n),received=101+n/250)
        rows+=d.flush_order(final=True)
        self.assertEqual([r[0] for r in rows],list(range(300)))
        self.assertEqual(d.startup_discarded,0)

    def test_counter_1276_to_2_only_resets_unemitted_startup_quarantine(self):
        d=self.startup_decoder();d.feed(self.normal_ack(),received=100);d.arm_start_command()
        self.assertEqual(d.feed(frame(1267),received=100),[])
        self.assertEqual(d.feed(frame(2),received=100.04),[])
        rows=[]
        for n in range(12,302,10):rows+=d.feed(frame(n),received=100.04+(n-2)/250)
        rows+=d.flush_order(final=True)
        self.assertEqual([r[0] for r in rows],list(range(2,302)))
        self.assertEqual(d.startup_discarded,10)
        self.assertEqual(d.startup_resets[0]['previous_highest'],1276)
        self.assertEqual(d.anchor['index'],11)
        self.assertAlmostEqual(d.anchor['received'],100.04)
        with self.assertRaisesRegex(WireError,'late/reset|backward'):d.feed(frame(0),received=102)

    def test_repeated_startup_resets_and_premature_flush_are_rejected(self):
        d=self.startup_decoder();d.feed(self.normal_ack());d.arm_start_command()
        d.feed(frame(1267),received=100);d.feed(frame(2),received=100.04)
        self.assertEqual(d.flush_order(final=True),[])
        d.feed(frame(100),received=100.1)
        with self.assertRaisesRegex(WireError,'Unstable EEG startup'):d.feed(frame(0),received=100.2)

    def test_mixed_first_frame_quarantined_before_any_slot_is_emitted(self):
        mixed=counter_frame([1462,1463,1464,1465,2,3,4,5,6,7])
        for step in (1,7,len(mixed)):
            d=self.startup_decoder();d.feed(self.normal_ack());d.arm_start_command()
            for pos in range(0,len(mixed),step):
                self.assertEqual(d.feed(mixed[pos:pos+step],received=100),[])
            self.assertEqual(d.startup_discarded,10)
            self.assertIsNone(d.anchor)
            self.assertIsNone(d.order.highest)
            self.assertEqual(d.flush_order(final=True),[])
            rows=[]
            for n in range(8,308,10):rows+=d.feed(frame(n),received=100.04+(n-8)/250)
            rows+=d.flush_order(final=True)
            self.assertEqual([r[0] for r in rows],list(range(8,308)))
            self.assertEqual(d.missing,0)
            self.assertEqual(d.anchor,dict(index=17,received=100.04))
            self.assertEqual(d.startup_resets[0]['reason'],'mixed_frame')
            with self.assertRaisesRegex(WireError,'late/reset|backward'):
                d.feed(mixed,received=102)

    def test_mixed_frame_and_clean_frame_in_same_receive(self):
        d=self.startup_decoder();d.feed(self.normal_ack());d.arm_start_command()
        d.feed(frame(1452),received=99.96)
        d.feed(counter_frame([1462,1463,1464,1465,2,3,4,5,6,7])+frame(8),received=100)
        self.assertEqual(d.startup_discarded,20)
        self.assertEqual(d.anchor,dict(index=17,received=100))
        self.assertEqual(sorted(d.order.pending),list(range(8,18)))

    def test_unverified_and_repeated_mixed_frames_fail_closed(self):
        for counters in ([1462,1463,1464,1465,502,503,504,505,506,507],
                         [1462,1463,1464,1465,2,4,5,6,7,8],
                         [1462,1463,2,3,1000,1001,4,5,6,7]):
            d=self.startup_decoder();d.feed(self.normal_ack());d.arm_start_command()
            with self.assertRaisesRegex(WireError,'unverified mixed-frame'):
                d.feed(counter_frame(counters),received=100)
        d=self.startup_decoder();d.feed(self.normal_ack());d.arm_start_command()
        mixed=counter_frame([1462,1463,1464,1465,2,3,4,5,6,7])
        d.feed(mixed,received=100)
        with self.assertRaisesRegex(WireError,'repeated or unverified'):
            d.feed(mixed,received=100.04)

    def test_fragmented_cross_frame_order_and_duplicate_keep_every_channel(self):
        raw=b''.join(frame(i) for i in [10,0,30,20,30,40,60,50,70,80,90])
        for step in [1,7,500,len(raw)]:
            d=decoder();rows=[]
            for i in range(0,len(raw),step): rows+=d.feed(raw[i:i+step],received=100.)
            rows+=d.flush_order(final=True)
            self.assertEqual([r[0] for r in rows],list(range(100)))
            for index,values in rows:self.assertEqual(values,[(index%10)*100+c for c in range(32)])
            self.assertEqual(d.missing,0)
            self.assertEqual(d.order.duplicates,10)
            self.assertGreater(d.order.reordered,0)

    def test_within_frame_slots_can_be_out_of_order(self):
        payload=b''.join(message(slot,number(1,i)+message(2,bytes([i])*108))
                         for slot,i in zip([1,2,3,4,8,9,10,11,12,13],[4,0,8,2,9,1,3,7,5,6]))
        payload=number(1,1)+message(3,message(2,payload+number(6,1)+number(7,4)))
        packet=b'BRNC\x02\x0b'+len(payload).to_bytes(2,'little')+b'\0\2\0'+payload
        d=decoder();d.feed(packet+crc16(packet).to_bytes(2,'little'))
        self.assertEqual([x[0] for x in d.flush_order(final=True)],list(range(10)))

    def test_conflicting_duplicates_are_not_silently_merged(self):
        d=decoder();d.feed(frame(0))
        # Same counter 1 has different ADC content (slot 0 instead of slot 1).
        with self.assertRaisesRegex(WireError,'conflicting duplicate'):d.feed(frame(1))

    def test_identical_duplicate_after_emission_is_deduplicated(self):
        d=decoder();d.feed(b''.join(frame(i) for i in range(0,100,10)))
        d.feed(frame(0));self.assertEqual(d.order.duplicates,10)

    def test_reset_and_unverified_wrap_remain_errors(self):
        for first in [1000,2**32-10]:
            d=decoder();d.feed(frame(first))
            with self.assertRaisesRegex(WireError,'backward'):d.feed(frame(0))

    def test_excessive_late_data_does_not_rewrite_emitted_timeline(self):
        d=decoder();d.feed(frame(100));d.feed(frame(180))
        with self.assertRaisesRegex(WireError,'backward|late/reset'):d.feed(frame(110))

    def test_real_gap_preserved_and_tail_flushed(self):
        d=decoder();self.assertEqual(d.feed(frame(100)+frame(120)),[])
        rows=d.flush_order(final=True);self.assertEqual(len(rows),20);self.assertEqual(d.missing,10)
        c=CounterClock(250);batch=c.batch([r[0] for r in rows],101,anchor=d.anchor)
        self.assertEqual(batch['missing_samples_total'],10)
        self.assertEqual(batch['sample_offsets'][10],20)
        self.assertEqual(d.flush_order(final=True),[])

    def test_reorder_latency_does_not_shift_clock_anchor(self):
        d=decoder();d.feed(frame(10),received=100.)
        d.feed(frame(0),received=100.01);rows=d.feed(frame(60),received=100.2)
        c=CounterClock(250);b=c.batch([r[0] for r in rows],100.2,anchor=d.anchor)
        self.assertAlmostEqual(b['estimated_start'],100-19/250)
        self.assertEqual(b['device_sample_indices'][0],0)

    def test_wire_archive_retains_corrupt_input_before_decode_failure(self):
        class Reader:
            async def read(self,n):return b'not a valid BrainCo frame'
        class Writer:
            def write(self,data):raise AssertionError('invalid frame should fail decoding')
            async def drain(self):pass
        async def run(root):
            tap=WireTap(decoder(),1000,archive=root/'attempt')
            with self.assertRaises(WireError):await tap._relay(Reader(),Writer(),True)
            await tap.close()
            self.assertEqual((root/'attempt/rx.bin').read_bytes(),b'not a valid BrainCo frame')
            self.assertEqual(json.loads((root/'attempt/chunks.jsonl').read_text())['offset'],0)
        with tempfile.TemporaryDirectory() as path:asyncio.run(run(Path(path)))


if __name__=='__main__':unittest.main()

import unittest, tempfile, sqlite3, json
from pathlib import Path
from types import SimpleNamespace
from src.storage.database import SCHEMA
from src.session_handoff import export_handoff


class HandoffTests(unittest.TestCase):
    def test_native_copy_events_and_idempotent_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'native';root.mkdir()
            signal=root/'continuous.sqlite3'
            db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
            try:
                db.executescript(SCHEMA)
                db.execute("INSERT INTO sessions VALUES ('s','s001',1,'2026-11-13T01:00:00+00:00','2026-11-13T01:01:00+00:00','h',1,'p','brainco','brainco','completed')")
                source=sqlite3.connect(signal)
                source.execute('CREATE TABLE sentinel(n INTEGER)');source.execute('INSERT INTO sentinel VALUES (32)');source.commit();source.close()
                db.execute("INSERT INTO eeg_recordings VALUES ('r','s','brainco','native','continuous.sqlite3','h',1,250,250,32,1,2,NULL,'closed')")
                db.execute("INSERT INTO events VALUES (1,1,'s',NULL,'IMAGE_ONSET',123,NULL,101,'{}')")
                db.execute("INSERT INTO eeg_sync VALUES (1,'r',126,NULL,'packet','software_estimated',NULL,NULL)")
                store=SimpleNamespace(database=db,dataset_root=root,participant_id='s001',session_id='s',session_dir=root,run_mode='brainco',locked_config={'experiment':{'provenance':{'paradigm_code':'BCSCN-20261113'}}})
                out=export_handoff(store)
                self.assertEqual(export_handoff(store),out)
                state=json.loads((out/'session.json').read_text())
                self.assertEqual(len(state['runs']),1)
                self.assertFalse(state['eegqc_ready'])
                copy=sqlite3.connect(out/state['runs'][0]['signal'])
                try:self.assertEqual(copy.execute('SELECT n FROM sentinel').fetchone()[0],32)
                finally:copy.close()
                self.assertIn('0.5',next((out/'eeg').glob('*_events.csv')).read_text())
            finally:db.close()

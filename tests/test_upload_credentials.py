import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.upload_credentials import save_credentials, load_credentials, authenticated_session


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(endpoint='http://172.16.4.2:9000', data_bucket='oi-data', prefix='CFIMAGE-20261113', profile='')
        self.value = dict(schema_version=1, **{k:v for k,v in self.config.items() if k!='profile'},
                          access_key='FAKE_ACCESS_TEST', secret_key='FAKE_SECRET_TEST')

    def test_dpapi_roundtrip_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'auth.dpapi'
            save_credentials(self.value,self.config,path)
            self.assertNotIn(b'FAKE_SECRET_TEST',path.read_bytes())
            self.assertEqual(load_credentials(self.config,path)['aws_access_key_id'],'FAKE_ACCESS_TEST')
            data=bytearray(path.read_bytes());data[-1]^=1;path.write_bytes(data)
            with self.assertRaisesRegex(RuntimeError,'Windows'):
                load_credentials(self.config,path)

    def test_destination_change_never_receives_stored_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'auth.dpapi';save_credentials(self.value,self.config,path)
            with self.assertRaisesRegex(ValueError,'destination'):
                load_credentials(dict(self.config,endpoint='https://other.invalid'),path)

    def test_no_network_metadata_fallback_when_unprovisioned(self):
        with patch('src.upload_credentials.load_credentials',return_value=None),patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(RuntimeError,'SMB'):
                authenticated_session(self.config)

    def test_dpapi_ignores_unrelated_aws_profile(self):
        with patch('src.upload_credentials.load_credentials',return_value={'aws_access_key_id':'FAKE','aws_secret_access_key':'FAKE'}),patch.dict(os.environ,{'AWS_PROFILE':'missing-test-profile'}):
            session=authenticated_session(self.config)
            self.assertEqual(session.get_credentials().access_key,'FAKE')

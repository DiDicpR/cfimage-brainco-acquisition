import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from brainco_discovery import candidate_addresses, discover_lan
from brainco_adapter import resolve_target


class DiscoveryTests(unittest.TestCase):
    def test_scope_excludes_company_large_network_and_local_addresses(self):
        rows = [dict(IPAddress=ip, PrefixLength=prefix) for ip, prefix in (
            ('192.168.31.110',24), ('172.16.1.206',21), ('169.254.1.2',16),
            ('100.64.1.1',24), ('8.8.8.1',24), ('127.0.0.1',24), ('::1',128))]
        addresses = candidate_addresses(rows)
        self.assertEqual(len(addresses),253)
        self.assertNotIn('192.168.31.110',addresses)
        self.assertIn('192.168.31.152',addresses)
        self.assertTrue(all(x.startswith('192.168.31.') for x in addresses))

    def test_unique_candidate_and_ambiguity(self):
        with patch('brainco_discovery.active_interfaces',return_value=[dict(IPAddress='192.168.31.1',PrefixLength=30)]):
            with patch('brainco_discovery.port_open',return_value=True):
                self.assertEqual(discover_lan(),('192.168.31.2',53129))
            with patch('brainco_discovery.port_open',return_value=False):
                self.assertIsNone(discover_lan())
        with patch('brainco_discovery.active_interfaces',return_value=[dict(IPAddress='192.168.31.1',PrefixLength=29)]):
            with patch('brainco_discovery.port_open',return_value=True):
                with self.assertRaisesRegex(RuntimeError,'多个'):discover_lan()

    def test_mdns_preferred_and_manual_address_never_scanned(self):
        cfg=dict(auto_discover=True,address='192.168.99.1',port=1234)
        with patch('brainco_discovery.discover_lan',return_value=('192.168.31.2',53129)) as fallback:
            self.assertEqual(resolve_target(cfg,lambda:('192.168.31.3',53129)),('192.168.31.3',53129))
            fallback.assert_not_called()
            self.assertEqual(resolve_target(cfg,lambda:None),('192.168.31.2',53129))
            fallback.assert_called_once()
            cfg['auto_discover']=False
            self.assertEqual(resolve_target(cfg,lambda:self.fail('manual discovery')),('192.168.99.1',1234))
            fallback.assert_called_once()


if __name__ == '__main__':
    unittest.main()

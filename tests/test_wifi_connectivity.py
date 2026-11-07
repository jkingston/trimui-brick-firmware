import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = (ROOT / 'board/trimui/brick/rootfs-overlay/usr/libexec/'
         'brick-wifi-connectivity')
DHCP_HOOK = (ROOT / 'board/trimui/brick/rootfs-overlay/usr/share/udhcpc/'
             'default.script.d/50-brick-wifi')


class WifiConnectivityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.responses = self.root / 'responses.json'
        self.curl = self.root / 'curl'
        self.curl.write_text(r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
url = args[-1]
responses = json.loads(pathlib.Path(os.environ['FAKE_RESPONSES']).read_text())
response = responses[url]

def argument(name):
    return args[args.index(name) + 1]

pathlib.Path(argument('--output')).write_text(response.get('body', ''))
pathlib.Path(argument('--dump-header')).write_text(response.get('headers', ''))
sys.stdout.write('%s\n%s\n%s\n' % (
    response.get('code', '000'),
    len(response.get('body', '').encode()),
    response.get('type', '')))
sys.stderr.write(response.get('error', ''))
sys.exit(response.get('rc', 0))
''')
        self.curl.chmod(0o755)
        self.http = 'http://probe.test/generate_204'
        self.https = 'https://probe.test/generate_204'
        self.capport = 'https://portal.test/api'

    def tearDown(self):
        self.temp.cleanup()

    def run_probe(self, responses, capport=None):
        self.responses.write_text(json.dumps(responses))
        env = os.environ.copy()
        env.update({
            'WIFI_STATE_DIR': str(self.state),
            'WIFI_CURL': str(self.curl),
            'WIFI_HTTP_PROBE_URL': self.http,
            'WIFI_HTTPS_PROBE_URL': self.https,
            'FAKE_RESPONSES': str(self.responses),
        })
        command = [str(PROBE), 'check']
        if capport is not None:
            command.append(capport)
        result = subprocess.run(command, env=env, text=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=True)
        return dict(line.split('=', 1)
                    for line in result.stdout.splitlines())

    @staticmethod
    def success(code='204', body='', content_type=''):
        return {'rc': 0, 'code': code, 'body': body, 'type': content_type}

    def test_dual_204_is_online(self):
        result = self.run_probe({
            self.http: self.success(),
            self.https: self.success(),
        })
        self.assertEqual(result['state'], 'online')
        self.assertEqual(result['reason'], 'dual-probe')

    def test_https_204_is_sufficient_when_http_fails(self):
        result = self.run_probe({
            self.http: {'rc': 28, 'code': '000'},
            self.https: self.success(),
        })
        self.assertEqual(result['state'], 'online')
        self.assertEqual(result['reason'], 'https-probe')

    def test_http_redirect_is_captive(self):
        result = self.run_probe({
            self.http: self.success('302'),
            self.https: {'rc': 28, 'code': '000'},
        })
        self.assertEqual(result['state'], 'captive')
        self.assertEqual(result['reason'], 'http-intercept')

    def test_dns_failure_is_limited_not_captive(self):
        result = self.run_probe({
            self.http: {'rc': 6, 'code': '000'},
            self.https: {'rc': 6, 'code': '000'},
        })
        self.assertEqual(result['state'], 'limited')
        self.assertEqual(result['reason'], 'dns')

    def test_bad_certificate_is_limited_not_captive(self):
        result = self.run_probe({
            self.http: self.success(),
            self.https: {'rc': 60, 'code': '000'},
        })
        self.assertEqual(result['state'], 'limited')
        self.assertEqual(result['reason'], 'tls-validation')

    def test_authenticated_capport_api_takes_precedence(self):
        result = self.run_probe({
            self.capport: self.success(
                '200', '{"captive":true,"user-portal-url":"https://portal.test/"}',
                'application/captive+json'),
        }, self.capport)
        self.assertEqual(result['state'], 'captive')
        self.assertEqual(result['reason'], 'capport-api')
        self.assertTrue((self.state / 'capport.json').exists())

    def test_capport_parser_ignores_nested_captive_member(self):
        result = self.run_probe({
            self.capport: self.success(
                '200', '{"nested":{"captive":true},"captive":false}',
                'application/captive+json; charset=utf-8'),
        }, self.capport)
        self.assertEqual(result['state'], 'online')
        self.assertEqual(result['capport_result'], 'false')

    def test_invalid_capport_json_falls_back_to_probes(self):
        result = self.run_probe({
            self.capport: self.success(
                '200', '{"captive":true,"captive":false}',
                'application/captive+json'),
            self.http: self.success(),
            self.https: self.success(),
        }, self.capport)
        self.assertEqual(result['state'], 'online')
        self.assertEqual(result['reason'], 'dual-probe')
        self.assertEqual(result['capport_result'], 'invalid-json')

    def test_unrestricted_capport_identifier_skips_probes(self):
        result = self.run_probe({},
                                'urn:ietf:params:capport:unrestricted')
        self.assertEqual(result['state'], 'online')
        self.assertEqual(result['reason'], 'capport-unrestricted')


class WifiDhcpHookTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.calls = self.root / 'calls'
        self.private_calls = self.root / 'private-calls'
        self.connectivity = self.root / 'connectivity'
        self.connectivity.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$WIFI_TEST_CALLS"\n'
            'if [ "${WIFI_TEST_CAPTIVE:-0}" = 1 ]; then\n'
            '  printf "state=captive\\n" > "$WIFI_STATE_DIR/connectivity"\n'
            'fi\n')
        self.connectivity.chmod(0o755)
        self.private = self.root / 'private-mac'
        self.private.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$WIFI_TEST_PRIVATE_CALLS"\n')
        self.private.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_hook(self, action, option='', **extra):
        env = os.environ.copy()
        env.update({
            'WIFI_STATE_DIR': str(self.state),
            'WIFI_CONNECTIVITY': str(self.connectivity),
            'WIFI_PRIVATE_MAC': str(self.private),
            'WIFI_TEST_CALLS': str(self.calls),
            'WIFI_TEST_PRIVATE_CALLS': str(self.private_calls),
            'interface': 'wlan0',
            'ip': '192.0.2.10',
            'lease': '3600',
            'opt114': option,
        })
        env.update(extra)
        subprocess.run([str(DHCP_HOOK), action], env=env, check=True)
        for _ in range(50):
            if self.calls.exists():
                break
            time.sleep(0.01)

    def wait_for_private_calls(self, count):
        for _ in range(100):
            if (self.private_calls.exists() and
                    len(self.private_calls.read_text().splitlines()) >= count):
                return
            time.sleep(0.01)
        self.fail('private-MAC hook calls did not complete')

    def test_decodes_option_114_and_starts_check(self):
        uri = 'https://portal.test/api'
        self.run_hook('bound', uri.encode().hex())
        metadata = (self.state / 'dhcp').read_text()
        self.assertIn(f'capport_uri={uri}\n', metadata)
        self.assertEqual(self.calls.read_text(), f'check {uri}\n')

    def test_rejects_non_https_capport_uri(self):
        self.run_hook('bound', b'http://portal.test/api'.hex())
        self.assertIn('capport_uri=\n', (self.state / 'dhcp').read_text())
        self.assertEqual(self.calls.read_text(), 'check \n')

    def test_deconfig_clears_metadata(self):
        self.state.mkdir()
        (self.state / 'dhcp').write_text('address=192.0.2.10\n')
        self.run_hook('deconfig')
        self.assertFalse((self.state / 'dhcp').exists())
        self.assertEqual(self.calls.read_text(), 'clear\n')

    def test_records_lease_and_marks_captive_profile(self):
        self.run_hook('bound', WPA_ID='4', WIFI_TEST_CAPTIVE='1')
        self.wait_for_private_calls(2)
        calls = self.private_calls.read_text().splitlines()
        self.assertEqual(calls[0], 'lease 4 3600')
        self.assertIn('mark-captive 4', calls)


if __name__ == '__main__':
    unittest.main()

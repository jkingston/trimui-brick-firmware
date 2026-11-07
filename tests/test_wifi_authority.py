"""Product Wi-Fi authority protocol and ownership tests."""
from pathlib import Path
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'package/brick-wifi/brick-wifi-authority.c'
PACKAGE_CONFIG = ROOT / 'package/brick-wifi/Config.in'


class FakeSupplicant:
    def __init__(self, path):
        self.path = path
        self.commands = []
        self.running = True
        self.state = 'COMPLETED'
        self.address = '192.0.2.20'
        self.current = 0
        # The Brick's downstream supplicant reports WPA2-PSK for saved profiles.
        self.networks = {0: ('Home', 'WPA-PSK WPA-EAP')}
        self.next_id = 1
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        self.sock.settimeout(0.1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def close(self):
        self.running = False
        self.thread.join(timeout=2)
        self.sock.close()

    def response(self, command):
        self.commands.append(command)
        if command == 'STATUS':
            address = f'ip_address={self.address}\n' if self.address else ''
            return (f'id={self.current}\nssid=Home\nwpa_state={self.state}\n' +
                    address).encode()
        if command == 'LIST_NETWORKS':
            rows = ''.join(f'{key}\t{name}\tany\t[CURRENT]\n'
                           for key, (name, _) in self.networks.items())
            return ('network id / ssid / bssid / flags\n' + rows).encode()
        if command == 'SCAN_RESULTS':
            return (b'bssid / frequency / signal level / flags / ssid\n'
                    b'02:00:00:00:00:01\t2412\t-43\t[WPA2-PSK-CCMP][SAE-H2E][ESS]\tHome\n'
                    b'02:00:00:00:00:02\t2437\t-61\t[WPA2-PSK-CCMP][ESS]\tCafe\n'
                    b'02:00:00:00:00:03\t2462\t-70\t[WPA2-SAE-CCMP][ESS]\tFuture\n'
                    b'02:00:00:00:00:04\t2462\t-71\t[WPA2-PSK-CCMP][ESS]\t\\x00\\x00\n')
        match = re.fullmatch(r'GET_NETWORK (\d+) (\w+)', command)
        if match:
            network = int(match.group(1))
            field = match.group(2)
            if network not in self.networks:
                return b'FAIL\n'
            return {'key_mgmt': self.networks[network][1],
                    'disabled': '0', 'mac_addr': '3'}.get(field, 'FAIL').encode() + b'\n'
        if command == 'ADD_NETWORK':
            network = self.next_id
            self.next_id += 1
            self.networks[network] = ('Cafe', 'WPA-PSK')
            return f'{network}\n'.encode()
        match = re.fullmatch(r'SELECT_NETWORK (\d+)', command)
        if match:
            self.current = int(match.group(1))
            self.state = 'ASSOCIATING'
            return b'OK\n'
        if command == 'DISCONNECT':
            self.state = 'DISCONNECTED'
            return b'OK\n'
        return b'OK\n'

    def serve(self):
        while self.running:
            try:
                command, peer = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            self.sock.sendto(self.response(command.decode()), peer)


class WifiAuthorityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name) / 'brick-wifi-authority'
        subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                        str(SOURCE), '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.data = self.root / 'data'
        self.ctrl = self.root / 'ctrl'
        for directory in (self.state, self.data, self.ctrl):
            directory.mkdir()
        (self.data / 'enabled').write_text('1\n')
        (self.state / 'enabled').write_text('test-generation\n')
        (self.state / 'connectivity').write_text('state=online\n')
        self.wpa = FakeSupplicant(self.ctrl / 'wlan0')
        self.calls = self.root / 'calls'
        self.helpers = {}
        for name in ('service', 'private', 'forget', 'captive', 'connectivity', 'recover'):
            helper = self.root / name
            helper.write_text('#!/bin/sh\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$TEST_CALLS"\n')
            helper.chmod(0o755)
            self.helpers[name] = helper
        env = dict(os.environ,
                   WIFI_AUTHORITY_SOCKET=str(self.state / 'control.sock'),
                   WIFI_STATE_DIR=str(self.state), WIFI_DATA_DIR=str(self.data),
                   WIFI_CTRL_DIR=str(self.ctrl), TEST_CALLS=str(self.calls),
                   WIFI_SERVICE=str(self.helpers['service']),
                   WIFI_PRIVATE_MAC=str(self.helpers['private']),
                   WIFI_FORGET=str(self.helpers['forget']),
                   WIFI_CAPTIVE_CONTROL=str(self.helpers['captive']),
                   WIFI_CONNECTIVITY=str(self.helpers['connectivity']),
                   WIFI_RECOVER=str(self.helpers['recover']))
        self.process = subprocess.Popen([str(self.binary)], env=env)
        deadline = time.monotonic() + 2
        while not (self.state / 'control.sock').exists():
            if time.monotonic() > deadline:
                self.fail('authority socket did not appear')
            time.sleep(0.01)
        self.client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.client.settimeout(2)
        self.client.connect(str(self.state / 'control.sock'))

    def tearDown(self):
        self.client.close()
        self.process.terminate()
        self.process.wait(timeout=3)
        self.wpa.close()
        self.temp.cleanup()

    def receive(self, prefix):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            packet = self.client.recv(16384).decode()
            if packet.startswith(prefix):
                return packet
        self.fail(f'did not receive {prefix}')

    @staticmethod
    def header(packet, name):
        match = re.search(rf'\b{name}=([^ \n]+)', packet.splitlines()[0])
        if not match:
            raise AssertionError(f'missing {name}')
        return match.group(1)

    def command(self, generation, request, kind, network='', value=0,
                security='unsupported', ssid_hex='', credential_hex=''):
        network_field = f' network={network}' if network else ''
        packet = (f'command version=1 generation={generation} request={request} '
                  f'kind={kind}{network_field} value={value} security={security} '
                  f'ssid_hex={ssid_hex} credential_hex={credential_hex}\n')
        self.client.send(packet.encode())
        return self.receive('result ')

    def test_snapshot_scope_stale_replay_connect_and_radio_persistence(self):
        snapshot = self.receive('snapshot ')
        generation = int(self.header(snapshot, 'generation'))
        deadline = time.monotonic() + 2
        while (not self.calls.exists() or
               'connectivity check' not in self.calls.read_text()) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn('connectivity clear', self.calls.read_text())
        self.assertIn('connectivity check', self.calls.read_text())
        self.assertIn('name_hex=486f6d65 security=wpa2_personal', snapshot)
        cafe = re.search(r'network id=([^ ]+) name_hex=43616665 ', snapshot).group(1)
        self.assertIn('name_hex=467574757265 security=unsupported', snapshot)
        self.assertNotIn('name_hex=0000', snapshot)

        stale = self.command(generation + 1, 1, 'scan')
        self.assertIn('status=stale', stale)
        scans = self.wpa.commands.count('SCAN')

        accepted = self.command(generation, 2, 'scan')
        self.assertIn('status=accepted', accepted)
        self.assertEqual(self.wpa.commands.count('SCAN'), scans + 1)

        connected = self.command(generation, 3, 'connect', network=cafe,
                                 security='wpa2_personal',
                                 credential_hex='6361666570617373')
        self.assertIn('status=accepted', connected)
        self.assertIn('SET_NETWORK 1 psk "cafepass"', self.wpa.commands)
        add_count = self.wpa.commands.count('ADD_NETWORK')
        replay = self.command(generation, 3, 'connect', network=cafe,
                              security='wpa2_personal',
                              credential_hex='6361666570617373')
        self.assertIn('status=accepted', replay)
        self.assertEqual(self.wpa.commands.count('ADD_NETWORK'), add_count)

        selected = self.command(generation, 30, 'connect', network='n0',
                                security='wpa2_personal')
        self.assertIn('status=accepted', selected)
        self.assertIn('SET_NETWORK 0 proto RSN', self.wpa.commands)
        self.assertIn('SET_NETWORK 0 pairwise CCMP', self.wpa.commands)

        disabled = self.command(generation, 4, 'set_enabled', value=0)
        self.assertIn('status=accepted', disabled)
        self.assertEqual((self.data / 'enabled').read_text(), '0\n')
        self.assertIn('service stop', self.calls.read_text())

    def test_malformed_and_unknown_network_commands_are_rejected(self):
        snapshot = self.receive('snapshot ')
        generation = int(self.header(snapshot, 'generation'))
        result = self.command(generation, 9, 'forget', network='missing')
        self.assertIn('status=rejected', result)
        self.assertIn('reason_hex=756e6b6e6f776e2d6e6574776f726b', result)

    def test_saved_network_controls_delegate_to_single_product_helpers(self):
        snapshot = self.receive('snapshot ')
        generation = int(self.header(snapshot, 'generation'))
        home = re.search(r'network id=(n0) name_hex=486f6d65 ', snapshot).group(1)

        self.assertIn('status=accepted',
                      self.command(generation, 20, 'set_auto_connect',
                                   network=home, value=0))
        self.assertIn('SET_NETWORK 0 disabled 1', self.wpa.commands)

        self.assertIn('status=accepted',
                      self.command(generation, 21, 'set_private_address',
                                   network=home, value=0))
        calls = self.calls.read_text()
        self.assertIn('private set-mode 0 device', calls)
        self.assertIn('private prepare 0', calls)
        self.assertIn('RECONNECT', self.wpa.commands)

        self.assertIn('status=accepted',
                      self.command(generation, 22, 'open_captive', network=home))
        self.assertIn('captive open 0', self.calls.read_text())

        self.assertIn('status=accepted',
                      self.command(generation, 23, 'recheck', network=home))
        deadline = time.monotonic() + 2
        while ('connectivity check' not in self.calls.read_text()
               and time.monotonic() < deadline):
            time.sleep(0.01)
        self.assertIn('connectivity check', self.calls.read_text())

        self.assertIn('status=accepted',
                      self.command(generation, 24, 'forget', network=home))
        self.assertIn('forget network 0', self.calls.read_text())

    def test_package_owns_connectivity_probe_runtime_dependencies(self):
        config = PACKAGE_CONFIG.read_text()
        for symbol in ('BR2_PACKAGE_LIBCURL_CURL',
                       'BR2_PACKAGE_LIBCURL_FORCE_TLS',
                       'BR2_PACKAGE_CA_CERTIFICATES'):
            self.assertIn(f'select {symbol}', config)

    def test_connectivity_check_waits_for_dhcp_address(self):
        self.receive('snapshot ')
        deadline = time.monotonic() + 2
        while (not self.calls.exists() or
               'connectivity check' not in self.calls.read_text()) and time.monotonic() < deadline:
            time.sleep(0.01)
        initial = self.calls.read_text().count('connectivity check')

        self.wpa.state = 'DISCONNECTED'
        self.wpa.address = ''
        time.sleep(1.2)
        self.wpa.state = 'COMPLETED'
        time.sleep(1.2)
        self.assertEqual(self.calls.read_text().count('connectivity check'), initial)

        self.wpa.address = '192.0.2.21'
        deadline = time.monotonic() + 2
        while (self.calls.read_text().count('connectivity check') == initial and
               time.monotonic() < deadline):
            time.sleep(0.01)
        self.assertEqual(self.calls.read_text().count('connectivity check'), initial + 1)

    def test_prolonged_disconnect_starts_bounded_recovery_once(self):
        self.receive('snapshot ')
        self.wpa.state = 'DISCONNECTED'
        self.wpa.address = ''
        deadline = time.monotonic() + 8
        while (not self.calls.exists() or 'recover ' not in self.calls.read_text()) and \
              time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn('recover ', self.calls.read_text())
        time.sleep(1.2)
        self.assertEqual(self.calls.read_text().count('recover '), 1)


if __name__ == '__main__':
    unittest.main()

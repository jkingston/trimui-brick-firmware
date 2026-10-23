#!/usr/bin/env python3
"""Local PTY coverage; never opens a hardware serial port."""
import hashlib
import importlib.util
import os
import pathlib
import pty
import re
import subprocess
import tempfile
import termios
import unittest
import uuid
import types

HERE=pathlib.Path(__file__).resolve().parent
SERIAL=HERE.parent/'tools/serial_console.py'
spec=importlib.util.spec_from_file_location('serial_tools',SERIAL)
serial=importlib.util.module_from_spec(spec)
spec.loader.exec_module(serial)

class ReceiverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='brx-tests-')
        cls.executable=pathlib.Path(cls.temp.name)/'receiver'
        subprocess.run(['cc','-O2',str(HERE.parent/'package/brick-devtools/brick-receive.c'),'-o',str(cls.executable)],check=True)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def setUp(self):
        self.path=pathlib.Path('/tmp/brx-test-'+uuid.uuid4().hex)
        self.master,self.slave=pty.openpty()
        os.set_blocking(self.master,False)
        self.original=termios.tcgetattr(self.slave)
        self.transport=serial.Transport(self.master)
        self.process=None

    def tearDown(self):
        if self.process and self.process.poll() is None:
            self.process.kill();self.process.wait()
        if self.process:self.process.stderr.close()
        os.close(self.master);os.close(self.slave)
        self.path.unlink(missing_ok=True)

    def start(self,size,timeout=5):
        self.process=subprocess.Popen([str(self.executable),str(self.path),str(size),str(timeout)],stdin=self.slave,stdout=self.slave,stderr=subprocess.PIPE)

    def marker(self,name,count):
        return re.compile(rb'(?:^|\n)'+name.encode()+b' '+f'{count:08x}'.encode()+rb'\r?\n')

    def test_megabyte_every_byte_value(self):
        payload=bytes(range(256))*4096
        self.start(len(payload))
        self.transport.exchange(b'',self.marker('BRX_READY',len(payload)),5)
        for start in range(0,len(payload),4096):
            count=start+4096
            marker='BRX_DONE' if count==len(payload) else 'BRX_ACK'
            self.transport.exchange(payload[start:count],self.marker(marker,count),5)
        self.assertEqual(self.process.wait(timeout=2),0)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(),hashlib.sha256(payload).digest())
        self.assertEqual(termios.tcgetattr(self.slave),self.original)

    def test_partial_timeout_removes_file_and_restores_tty(self):
        self.start(65536,1)
        self.transport.exchange(b'',self.marker('BRX_READY',65536),2)
        self.transport.exchange(b'partial\x03\x04\x00',self.marker('BRX_FAIL',0),3)
        self.assertNotEqual(self.process.wait(timeout=2),0)
        self.assertFalse(self.path.exists())
        self.assertEqual(termios.tcgetattr(self.slave),self.original)

    def test_existing_file_not_overwritten(self):
        self.path.write_bytes(b'keep')
        self.start(1)
        self.assertNotEqual(self.process.wait(timeout=2),0)
        self.assertEqual(self.path.read_bytes(),b'keep')
        self.assertEqual(termios.tcgetattr(self.slave),self.original)

    def test_symlink_not_followed(self):
        target=pathlib.Path(self.temp.name)/'target'
        target.write_bytes(b'keep')
        self.path.symlink_to(target)
        self.start(1)
        self.assertNotEqual(self.process.wait(timeout=2),0)
        self.assertEqual(target.read_bytes(),b'keep')
        self.assertTrue(self.path.is_symlink())

    def test_zero_length_is_valid_receiver_only(self):
        self.start(0)
        self.transport.exchange(b'',self.marker('BRX_DONE',0),2)
        self.assertEqual(self.process.wait(timeout=2),0)
        self.assertEqual(self.path.read_bytes(),b'')
        self.assertEqual(termios.tcgetattr(self.slave),self.original)

    def test_host_chunk_protocol_through_foreground_shell(self):
        self.process=subprocess.Popen(['/bin/sh'],stdin=self.slave,stdout=self.slave,stderr=subprocess.PIPE)
        payload=bytes(range(256))*4096+b'nonaligned tail\x00\x04\x03'
        serial.transfer_raw(types.SimpleNamespace(transport=self.transport),payload,str(self.executable),str(self.path),5)
        self.assertEqual(self.path.read_bytes(),payload)
        self.assertEqual(termios.tcgetattr(self.slave),self.original)
        os.write(self.master,b'exit\n')
        self.assertEqual(self.process.wait(timeout=2),0)

if __name__=='__main__':unittest.main()

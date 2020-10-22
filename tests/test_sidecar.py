import os
import shutil
import socket
import unittest
from multiprocessing import Process

from backup_utils.backup_file import BackupFile

from backup_server.src.backup_scheduler.node_handler_process import NodeHandlerProcess
from sidecar.src.sidecar_process import SidecarProcess


class TestSidecar(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from pytest_cov.embed import cleanup_on_sigterm
        except ImportError:
            pass
        else:
            cleanup_on_sigterm()
        shutil.rmtree('/tmp/backup_output', ignore_errors=True)
        os.mkdir('/tmp/backup_output')
        self.sidecar_process = SidecarProcess(1234, 5)
        self.p = Process(target=self.sidecar_process)
        self.p.start()
        while True:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.connect(('127.0.0.1', 1234))
                sock.close()
                break
            except ConnectionRefusedError:
                pass

    def tearDown(self) -> None:

        if self.p.is_alive():
            self.p.terminate()
        shutil.rmtree('/tmp/backup_output', ignore_errors=True)

    def test_simple_backup(self):
        node_handler_process = NodeHandlerProcess('localhost', 1234,
                                                  'sidecar/src/sidecar_process.py',
                                                  '/tmp/backup_output/out',
                                                  'dummy_checksum')
        node_handler_process = Process(target=node_handler_process)
        node_handler_process.start()
        node_handler_process.join()
        expected_file = BackupFile.create_from_path('sidecar/src/sidecar_process.py', "/tmp/backup_output/out2")
        backup_file = BackupFile("/tmp/backup_output/out")
        self.assertEqual(expected_file.get_hash(), backup_file.get_hash())

    def test_backup_same_checksum(self):
        expected_file = BackupFile.create_from_path('sidecar/src/sidecar_process.py', "/tmp/backup_output/out2")
        node_handler_process = NodeHandlerProcess('localhost', 1234,
                                                  'sidecar/src/sidecar_process.py',
                                                  '/tmp/backup_output/out',
                                                  expected_file.get_hash())
        node_handler_process = Process(target=node_handler_process)
        node_handler_process.start()
        node_handler_process.join()
        self.assertTrue(os.path.exists('/tmp/backup_output/out.SAME'))

import json
import socket
import unittest
from multiprocessing import Process, Pipe

from src.client_listener.client_listener import ClientListener


class MockNodeHandler:
    BARRIER = None

    def __init__(self, node_address: str, node_port: int,
                 node_path: str, write_file_path: str):
        self.node_address = node_address
        self.node_port = node_port
        self.node_path = node_path
        self.write_file_path = write_file_path

    def __call__(self, *args, **kwargs):
        open(self.write_file_path, "w").close()
        open(self.write_file_path + ".CORRECT", "w").close()
        MockNodeHandler.BARRIER.wait(timeout=10)


class TestBackupScheduler(unittest.TestCase):
    def setUp(self):
        try:
            from pytest_cov.embed import cleanup_on_sigterm
        except ImportError:
            pass
        else:
            cleanup_on_sigterm()
        self.backup_scheduler_recv, client_listener_send = Pipe(False)
        client_listener_recv, self.backup_scheduler_send = Pipe(False)
        client_listener = ClientListener(3333, 5, client_listener_send,
                                         client_listener_recv)
        self.p = Process(target=client_listener)
        self.p.start()

    def tearDown(self) -> None:
        if self.p.is_alive():
            self.p.terminate()
        self.backup_scheduler_send.close()
        self.backup_scheduler_recv.close()

    def test_send_and_receive_command(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(('localhost', 3333))
        sock.sendall('{"command": "dummy", "args": {"one": "one"}}'.encode('utf-8'))
        command, args = self.backup_scheduler_recv.recv()
        self.assertEqual(command, 'dummy')
        self.assertEqual(args, {"one": "one"})
        self.backup_scheduler_send.send(("OK", {}))
        msg = sock.recv(2048).rstrip()
        self.assertEqual(json.loads(msg), {"message": "OK", "data": {}})
        sock.close()

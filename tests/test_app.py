import os
import socket
import unittest

import app


class SonyUdpTarget(unittest.TestCase):
    """A camera stand-in: a UDP socket playing the Sony VISCA-over-IP peer."""

    def setUp(self):
        self.camera = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.camera.bind(('127.0.0.1', 0))
        self.camera.settimeout(1)
        self.env = dict(os.environ)
        os.environ.update({
            'VISCA_HOST': '127.0.0.1',
            'VISCA_PORT': str(self.camera.getsockname()[1]),
            'VISCA_LOCAL_PORT': '0',
        })
        os.environ.pop('VISCA_PROTOCOL', None)
        app.ptz = app.ViscaController()
        self.client = app.app.test_client()

    def tearDown(self):
        if app.ptz.sock:
            app.ptz.sock.close()
        self.camera.close()
        os.environ.clear()
        os.environ.update(self.env)

    def recv(self):
        return self.camera.recv(64)

    def test_command_connects_to_configured_camera_first(self):
        # No UI and no explicit connect: a bare API call is enough.
        r = self.client.post('/api/command', json={'cmd': 'move_left'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.recv(), b'\x02\x00\x00\x01\x00\x00\x00\x00\x01')
        # Pan left at the default speeds 0x0C/0x0A, header type 0x0100,
        # length 9, sequence 1.
        self.assertEqual(self.recv(), bytes.fromhex('0100 0009 00000001 8101 0601 0c0a 0103 ff'))

    def test_sequence_numbers_increase(self):
        self.client.post('/api/command', json={'cmd': 'zoom_stop'})
        self.client.post('/api/command', json={'cmd': 'stop'})
        self.recv()
        self.assertEqual(self.recv()[4:8], (1).to_bytes(4, 'big'))
        self.assertEqual(self.recv()[4:8], (2).to_bytes(4, 'big'))

    def test_status_reports_locked_target(self):
        port = self.camera.getsockname()[1]
        body = self.client.post('/api/connect').get_json()
        self.assertTrue(body['connected'])
        self.assertTrue(body['locked'])
        self.assertEqual(body['target'], {'mode': 'sony_udp', 'ip': '127.0.0.1', 'port': port})

    def test_socket_connect_binds_to_camera(self):
        sio = app.socketio.test_client(app.app)
        events = {e['name']: e['args'][0] for e in sio.get_received()}
        self.assertTrue(events['connection_status']['success'])
        self.assertTrue(events['camera_config']['locked'])
        self.assertEqual(self.recv(), b'\x02\x00\x00\x01\x00\x00\x00\x00\x01')

    def test_ui_cannot_retarget_a_bound_controller(self):
        other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        other.bind(('127.0.0.1', 0))
        other.settimeout(0.3)
        self.addCleanup(other.close)
        sio = app.socketio.test_client(app.app)
        self.recv()
        sio.emit('connect_camera', {'mode': 'sony_udp', 'ip': '127.0.0.1', 'port': other.getsockname()[1]})
        self.client.post('/api/command', json={'cmd': 'stop'})
        with self.assertRaises(socket.timeout):
            other.recv(64)
        # The bound camera got the command instead.
        self.assertEqual(self.recv(), b'\x02\x00\x00\x01\x00\x00\x00\x00\x01')
        self.assertEqual(self.recv()[8:], bytes.fromhex('8101 0601 0000 0303 ff'))

    def test_explicit_connect_resets_the_sequence(self):
        # A camera that rebooted expects sequence 1 again; a reconnect is the
        # way to tell it, and must not be skipped because a socket is open.
        self.client.post('/api/command', json={'cmd': 'stop'})
        self.client.post('/api/connect')
        self.client.post('/api/command', json={'cmd': 'stop'})
        packets = [self.recv() for _ in range(4)]
        self.assertEqual(packets[2], b'\x02\x00\x00\x01\x00\x00\x00\x00\x01')
        self.assertEqual(packets[3][4:8], (1).to_bytes(4, 'big'))

    def test_unknown_suffix_and_bad_values_are_rejected(self):
        for body in ({'cmd': 'move_sideways'}, {'cmd': 'iris_set', 'val': 'x'}, {'cmd': 'iris_set', 'val': 70000}):
            self.assertEqual(self.client.post('/api/command', json=body).status_code, 400, body)

    def test_unknown_command_is_rejected(self):
        r = self.client.post('/api/command', json={'cmd': 'self_destruct'})
        self.assertEqual(r.status_code, 400)

    def test_unknown_protocol_fails_loudly(self):
        os.environ['VISCA_PROTOCOL'] = 'carrier-pigeon'
        with self.assertRaises(ValueError):
            app.configured_target()


class TcpTarget(unittest.TestCase):
    def setUp(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(1)
        self.server.settimeout(1)
        self.env = dict(os.environ)
        os.environ.update({
            'VISCA_HOST': '127.0.0.1',
            'VISCA_PROTOCOL': 'tcp',
            'VISCA_PORT': str(self.server.getsockname()[1]),
        })
        app.ptz = app.ViscaController()

    def tearDown(self):
        if app.ptz.sock:
            app.ptz.sock.close()
        self.server.close()
        os.environ.clear()
        os.environ.update(self.env)

    def test_tcp_sends_plain_visca(self):
        r = app.app.test_client().post('/api/command', json={'cmd': 'stop'})
        self.assertEqual(r.status_code, 200)
        conn, _ = self.server.accept()
        conn.settimeout(1)
        self.addCleanup(conn.close)
        data = b''
        while not data.endswith(bytes.fromhex('8101 0601 0000 0303 ff')):
            data += conn.recv(64)
        # Address set and IF_Clear go first, no Sony header anywhere.
        self.assertTrue(data.startswith(bytes.fromhex('883001ff')))


class FailedConnect(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)
        os.environ.pop('VISCA_HOST', None)
        app.ptz = app.ViscaController()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_a_failed_connect_leaves_nothing_connected(self):
        camera = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        camera.bind(('127.0.0.1', 0))
        self.addCleanup(camera.close)
        app.ptz.connect_sony_udp('127.0.0.1', camera.getsockname()[1], 0)
        # Nothing listens on this port of the loopback.
        closed = socket.socket(); closed.bind(('127.0.0.1', 0)); port = closed.getsockname()[1]; closed.close()
        ok, _ = app.ptz.connect_tcp('127.0.0.1', port)
        self.assertFalse(ok)
        self.assertIsNone(app.ptz.connection_type)
        self.assertFalse(app.status()['connected'])


class Unbound(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)
        os.environ.pop('VISCA_HOST', None)
        app.ptz = app.ViscaController()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_without_camera_command_reports_disconnected(self):
        r = app.app.test_client().post('/api/command', json={'cmd': 'stop'})
        self.assertEqual(r.status_code, 502)


if __name__ == '__main__':
    unittest.main()

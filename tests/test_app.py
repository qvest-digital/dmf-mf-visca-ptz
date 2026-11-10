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
        sio = app.socketio.test_client(app.app)
        sio.emit('connect_camera', {'mode': 'tcp', 'ip': '192.0.2.1', 'port': 5678})
        self.assertEqual(app.ptz.target['ip'], '127.0.0.1')
        self.assertEqual(app.ptz.connection_type, 'sony_udp')

    def test_unknown_command_is_rejected(self):
        r = self.client.post('/api/command', json={'cmd': 'self_destruct'})
        self.assertEqual(r.status_code, 400)

    def test_unknown_protocol_fails_loudly(self):
        os.environ['VISCA_PROTOCOL'] = 'carrier-pigeon'
        with self.assertRaises(ValueError):
            app.configured_target()


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

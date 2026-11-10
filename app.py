# Before anything else imports socket or time: unpatched, a blocking connect
# or sleep stalls the event loop and with it every client and /healthz.
import eventlet
eventlet.monkey_patch()

import os
import socket
import serial
import threading
import time
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or os.urandom(32).hex()
# Same origin unless listed: any page a browser has open could otherwise drive
# the camera through it.
_origins = [o.strip() for o in os.environ.get('CORS_ALLOWED_ORIGINS', '').split(',') if o.strip()]
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins=_origins or None)

class ViscaController:
    def __init__(self):
        self.connection_type = None 
        self.ser = None
        self.sock = None
        self.peer = None
        self.seq = 0
        self.target = None
        self.sent = 0
        self.address = 1 
        self.pan_speed = 0x0C 
        self.tilt_speed = 0x0A 
        self.zoom_speed = 0x04 

    def _to_nibbles(self, val):
        # Converte un intero in 4 nibbles VISCA (es. 50 -> 0x32 -> 00 00 03 02)
        val = int(val)
        if not 0 <= val <= 0xFFFF:
            raise ValueError(f"{val} does not fit four VISCA nibbles")
        hex_str = f"{val:04x}" 
        return bytes([int(c, 16) for c in hex_str])

    def _send_raw(self, packet):
        try:
            if self.connection_type == 'serial' and self.ser:
                self.ser.write(packet)
            elif self.connection_type == 'tcp' and self.sock:
                self.sock.send(packet)
            elif self.connection_type == 'sony_udp' and self.sock:
                # Sony VISCA over IP: payload type 0x0100 (VISCA command),
                # payload length and a sequence number ahead of the VISCA bytes.
                self.seq = (self.seq + 1) & 0xFFFFFFFF
                header = b'\x01\x00' + len(packet).to_bytes(2, 'big') + self.seq.to_bytes(4, 'big')
                self.sock.send(header + packet)
            else:
                return False
            self.sent += 1
            return True
        except Exception as e:
            print(f"TX Raw Error: {e}")
            if self.connection_type == 'tcp':
                # The link is gone: the next ensure_connected() reopens it.
                self.connection_type = None
            return False

    def send_visca(self, payload):
        header = bytes([0x80 + self.address])
        terminator = b'\xFF'
        full_packet = header + payload + terminator
        return self._send_raw(full_packet)

    def init_camera(self):
        """Sequenza di avvio obbligatoria per Cisco PrecisionHD"""
        # 1. Address Set (Fondamentale, altrimenti ignora i comandi)
        self._send_raw(b'\x88\x30\x01\xFF')
        time.sleep(0.1)
        # 2. IF_Clear (Svuota buffer)
        self._send_raw(b'\x88\x01\x00\x01\xFF')
        time.sleep(0.1)
        # 3. Disabilita IR per evitare latenza sul brandeggio
        self.ir_control(True)

    def _reset(self):
        self.connection_type = None
        self.target = None

    def connect_serial(self, port, baudrate=9600):
        self._reset()
        try:
            if self.sock: self.sock.close()
            self.ser = serial.Serial(port, baudrate, timeout=0.1)
            self.connection_type = 'serial'
            self.target = {'mode': 'serial', 'port': port, 'baud': int(baudrate)}
            self.init_camera()
            return True, f"Connected Serial: {port} @ {baudrate}"
        except Exception as e:
            return False, str(e)

    def connect_sony_udp(self, ip, port=52381, local_port=52381):
        self._reset()
        try:
            if self.ser: self.ser.close()
            if self.sock: self.sock.close()
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # The camera answers on UDP 52381 whatever the sender's source
            # port. Nothing here reads the answers; binding there keeps them
            # from being refused with ICMP where they do arrive.
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(('0.0.0.0', int(local_port)))
            # Resolves the host once rather than on every packet.
            self.sock.connect((ip, int(port)))
            self.peer = (ip, int(port))
            self.seq = 0
            # Control command RESET: the camera expects sequence numbers from 1 again.
            self.sock.send(b'\x02\x00\x00\x01\x00\x00\x00\x00\x01')
            self.connection_type = 'sony_udp'
            self.target = {'mode': 'sony_udp', 'ip': ip, 'port': int(port)}
            return True, f"Connected Sony VISCA over IP: {ip}:{port}"
        except Exception as e:
            return False, str(e)

    def connect_tcp(self, ip, port=5678):
        self._reset()
        try:
            if self.ser: self.ser.close()
            if self.sock: self.sock.close()
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(0.5)
            self.sock.connect((ip, int(port)))
            self.connection_type = 'tcp'
            self.target = {'mode': 'tcp', 'ip': ip, 'port': int(port)}
            self.init_camera()
            return True, f"Connected TCP: {ip}:{port}"
        except Exception as e:
            return False, str(e)

    # --- CORE COMMANDS ---
    def set_speed(self, p_speed, t_speed, z_speed):
        self.pan_speed = max(1, min(24, int(p_speed)))
        self.tilt_speed = max(1, min(20, int(t_speed)))
        self.zoom_speed = max(0, min(7, int(z_speed)))

    def move(self, direction, p_override=None, t_override=None):
        p = int(p_override) if p_override is not None else self.pan_speed
        t = int(t_override) if t_override is not None else self.tilt_speed
        p = max(1, min(24, p))
        t = max(1, min(20, t))

        if direction == 'stop':       self.send_visca(b'\x01\x06\x01\x00\x00\x03\x03')
        elif direction == 'up':       self.send_visca(b'\x01\x06\x01' + bytes([p, t]) + b'\x03\x01')
        elif direction == 'down':     self.send_visca(b'\x01\x06\x01' + bytes([p, t]) + b'\x03\x02')
        elif direction == 'left':     self.send_visca(b'\x01\x06\x01' + bytes([p, t]) + b'\x01\x03')
        elif direction == 'right':    self.send_visca(b'\x01\x06\x01' + bytes([p, t]) + b'\x02\x03')
        elif direction == 'upleft':   self.send_visca(b'\x01\x06\x01' + bytes([p, t]) + b'\x01\x01')
        elif direction == 'upright':  self.send_visca(b'\x01\x06\x01' + bytes([p, t]) + b'\x02\x01')
        elif direction == 'downleft': self.send_visca(b'\x01\x06\x01' + bytes([p, t]) + b'\x01\x02')
        elif direction == 'downright':self.send_visca(b'\x01\x06\x01' + bytes([p, t]) + b'\x02\x02')

    def move_raw(self, pan_dir, tilt_dir, p_override=None, t_override=None):
        p = int(p_override) if p_override is not None else self.pan_speed
        t = int(t_override) if t_override is not None else self.tilt_speed
        p = max(1, min(24, p))
        t = max(1, min(20, t))

        def dir_byte(val):
            if val < 0: return 0x01
            elif val > 0: return 0x02
            else: return 0x03

        self.send_visca(b'\x01\x06\x01' + bytes([p, t, dir_byte(pan_dir), dir_byte(tilt_dir)]))

    def zoom(self, action, z_override=None):
        z = int(z_override) if z_override is not None else self.zoom_speed
        z = max(0, min(7, z))
        if action == 'tele':   self.send_visca(b'\x01\x04\x07' + bytes([0x20 + z]))
        elif action == 'wide': self.send_visca(b'\x01\x04\x07' + bytes([0x30 + z]))
        elif action == 'stop': self.send_visca(b'\x01\x04\x07\x00')

    def focus(self, action):
        if action == 'far':      self.send_visca(b'\x01\x04\x08\x20')
        elif action == 'near':   self.send_visca(b'\x01\x04\x08\x30')
        elif action == 'stop':   self.send_visca(b'\x01\x04\x08\x00')
        elif action == 'auto':   self.send_visca(b'\x01\x04\x38\x02')
        elif action == 'manual': self.send_visca(b'\x01\x04\x38\x03')

    # --- EXPOSURE & OPTICS COMMANDS ---
    def set_exposure(self, mode):
        # Cisco supporta solo 00 (Auto) e 03 (Manual)
        if mode == 'auto':   
            self.send_visca(b'\x01\x04\x39\x00')
        elif mode == 'manual': 
            self.send_visca(b'\x01\x04\x39\x03')
            # Workaround per Cisco: Inserisce valori di default sicuri per evitare lo schermo nero
            time.sleep(0.05)
            self.set_shutter_direct(10)
            time.sleep(0.05)
            self.set_iris_direct(25)
            time.sleep(0.05)
            self.set_gain_direct(15)
        
    def set_iris_direct(self, val):
        self.send_visca(b'\x01\x04\x4B' + self._to_nibbles(val))

    def set_gain_direct(self, val):
        self.send_visca(b'\x01\x04\x4C' + self._to_nibbles(val))

    def set_shutter_direct(self, val):
        self.send_visca(b'\x01\x04\x4A' + self._to_nibbles(val))

    # --- WHITE BALANCE COMMANDS ---
    def set_wb(self, mode):
        # 00 = Auto, 06 = Manual (Cisco spec)
        if mode == 'auto':   self.send_visca(b'\x01\x04\x35\x00')
        elif mode == 'manual': self.send_visca(b'\x01\x04\x35\x06')

    def set_wb_table(self, val):
        # Comando Table Direct (0x75)
        self.send_visca(b'\x01\x04\x75' + self._to_nibbles(val))

    # --- NUOVE FUNZIONI CISCO IMPLEMENTATE ---
    def set_backlight(self, state):
        if state == 'on': self.send_visca(b'\x01\x04\x33\x02')
        else:             self.send_visca(b'\x01\x04\x33\x03')

    def set_flip(self, state):
        if state == 'on': self.send_visca(b'\x01\x04\x66\x02')
        else:             self.send_visca(b'\x01\x04\x66\x03')

    def set_video_format(self, format_val):
        # Cisco Video Format Set: 8x 01 35 00 0p 00 ff
        self.send_visca(b'\x01\x35\x00' + bytes([int(format_val)]) + b'\x00')

    # --- AUX COMMANDS ---
    def call_led(self, state):
        if state == 'on':     self.send_visca(b'\x01\x33\x01\x01')
        elif state == 'off':  self.send_visca(b'\x01\x33\x01\x00')
        elif state == 'blink':self.send_visca(b'\x01\x33\x01\x02')

    def ir_control(self, disable):
        if disable: self.send_visca(b'\x01\x06\x09\x03')
        else:       self.send_visca(b'\x01\x06\x09\x02')

ptz = ViscaController()

# One camera, one sequence of packets: connecting swaps the socket and every
# Sony packet takes the next sequence number, so callers hold this throughout.
lock = threading.RLock()

# A deployment binds the controller to one camera through the environment.
# With VISCA_HOST set the target is fixed: the UI and the API connect to it
# and cannot point the controller anywhere else.
PROTOCOLS = {'sony-udp': 'sony_udp', 'tcp': 'tcp'}
DEFAULT_PORTS = {'sony_udp': 52381, 'tcp': 5678}


def configured_target():
    host = os.environ.get('VISCA_HOST', '').strip()
    if not host:
        return None
    protocol = os.environ.get('VISCA_PROTOCOL', 'sony-udp').strip()
    if protocol not in PROTOCOLS:
        raise ValueError(f"VISCA_PROTOCOL must be one of {', '.join(PROTOCOLS)}, not {protocol!r}")
    mode = PROTOCOLS[protocol]
    port = int(os.environ.get('VISCA_PORT') or DEFAULT_PORTS[mode])
    return {'mode': mode, 'ip': host, 'port': port}


def connect(target):
    if target['mode'] == 'sony_udp':
        local_port = int(os.environ.get('VISCA_LOCAL_PORT') or 52381)
        return ptz.connect_sony_udp(target['ip'], target['port'], local_port)
    if target['mode'] == 'tcp':
        return ptz.connect_tcp(target['ip'], target['port'])
    return ptz.connect_serial(target['port'], int(target['baud']))


def ensure_connected(force=False):
    """Connects to the configured camera unless already connected to it.
    force reconnects anyway, which for Sony VISCA over IP also resets the
    camera's sequence numbering."""
    target = configured_target()
    if target is None:
        if ptz.connection_type is None:
            return False, "Disconnected"
        return True, f"Connected: {ptz.target}"
    if not force and ptz.connection_type == target['mode'] and ptz.target == target:
        return True, f"Connected: {target['ip']}:{target['port']}"
    return connect(target)


def status():
    target = configured_target()
    # For UDP, connected means a socket is open towards the camera: there is
    # no handshake, so it says nothing about whether the camera listens.
    return {
        'connected': ptz.connection_type is not None,
        'target': ptz.target,
        'configured': target,
        'locked': target is not None,
    }


COMMANDS = {
    'move_raw', 'stop', 'zoom_tele', 'zoom_wide', 'zoom_stop',
    'focus_far', 'focus_near', 'focus_stop', 'focus_auto', 'focus_manual',
    'exposure_auto', 'exposure_manual', 'iris_set', 'gain_set', 'shutter_set',
    'wb_auto', 'wb_manual', 'wb_table_set', 'backlight_on', 'backlight_off',
    'flip_on', 'flip_off', 'format_set', 'led_on', 'led_off', 'led_blink',
} | {'move_' + d for d in ('stop', 'up', 'down', 'left', 'right', 'upleft', 'upright', 'downleft', 'downright')}


def dispatch(data):
    """Sends one command. Returns the number of packets sent; raises
    ValueError for an unknown command or a value that is no number."""
    cmd = data['cmd']
    if cmd not in COMMANDS:
        raise ValueError(f"unknown cmd {cmd!r}")
    val = data.get('val', 0)
    p_spd = data.get('p_spd')
    t_spd = data.get('t_spd')
    z_spd = data.get('z_spd')
    before = ptz.sent
    try:
        if cmd == 'move_raw':
            ptz.move_raw(int(data.get('p_dir', 0)), int(data.get('t_dir', 0)), p_spd, t_spd)
        elif cmd.startswith('move_'): ptz.move(cmd.split('_')[1], p_spd, t_spd)
        elif cmd == 'stop':           ptz.move('stop')
        elif cmd.startswith('zoom_'): ptz.zoom(cmd.split('_')[1], z_spd)
        elif cmd.startswith('focus_'): ptz.focus(cmd.split('_')[1])

        elif cmd == 'exposure_auto':   ptz.set_exposure('auto')
        elif cmd == 'exposure_manual': ptz.set_exposure('manual')
        elif cmd == 'iris_set':        ptz.set_iris_direct(int(val))
        elif cmd == 'gain_set':        ptz.set_gain_direct(int(val))
        elif cmd == 'shutter_set':     ptz.set_shutter_direct(int(val))

        elif cmd == 'wb_auto':         ptz.set_wb('auto')
        elif cmd == 'wb_manual':       ptz.set_wb('manual')
        elif cmd == 'wb_table_set':    ptz.set_wb_table(int(val))

        # Nuovi controlli Cisco
        elif cmd == 'backlight_on':    ptz.set_backlight('on')
        elif cmd == 'backlight_off':   ptz.set_backlight('off')
        elif cmd == 'flip_on':         ptz.set_flip('on')
        elif cmd == 'flip_off':        ptz.set_flip('off')
        elif cmd == 'format_set':      ptz.set_video_format(int(val))

        elif cmd.startswith('led_'):   ptz.call_led(cmd.split('_')[1])
    except (TypeError, ValueError) as e:
        raise ValueError(f"{cmd}: {e}")
    return ptz.sent - before


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/healthz')
def healthz():
    return 'ok'

@app.route('/api/status')
def api_status():
    return jsonify(status())

@app.route('/api/connect', methods=['POST'])
def api_connect():
    with lock:
        success, msg = ensure_connected(force=True)
        return jsonify(dict(status(), success=success, msg=msg)), (200 if success else 502)

@app.route('/api/command', methods=['POST'])
def api_command():
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get('cmd'), str):
        return jsonify({'success': False, 'msg': 'body needs a "cmd" string'}), 400
    with lock:
        success, msg = ensure_connected()
        if not success:
            return jsonify({'success': False, 'msg': msg}), 502
        try:
            sent = dispatch(data)
        except ValueError as e:
            return jsonify({'success': False, 'msg': str(e)}), 400
    if not sent:
        return jsonify({'success': False, 'msg': 'nothing was sent to the camera'}), 502
    return jsonify({'success': True})

@socketio.on('connect')
def handle_socket_connect():
    # Every page load connects the socket, and with it the camera.
    with lock:
        success, msg = ensure_connected()
        emit('camera_config', status())
    emit('connection_status', {'success': success, 'msg': msg})

@socketio.on('connect_camera')
def handle_connect(data):
    with lock:
        if configured_target() is not None:
            success, msg = ensure_connected(force=True)
        elif data['mode'] in ('tcp', 'sony_udp'):
            success, msg = connect({'mode': data['mode'], 'ip': data['ip'], 'port': int(data['port'])})
        else:
            success, msg = ptz.connect_serial(data['port'], int(data['baud']))
        emit('camera_config', status())
    emit('connection_status', {'success': success, 'msg': msg})

@socketio.on('ptz_command')
def handle_ptz(data):
    with lock:
        if ensure_connected()[0]:
            try:
                dispatch(data)
            except ValueError as e:
                print(f"Rejected command: {e}")

@socketio.on('config_change')
def handle_config(data):
    with lock:
        if 'pan_speed' in data:
            ptz.set_speed(data['pan_speed'], data['tilt_speed'], data['zoom_speed'])
        if 'ir_disable' in data:
            ptz.ir_control(data['ir_disable'])

if __name__ == '__main__':
    if configured_target() is not None:
        with lock:
            print(ensure_connected()[1])
    socketio.run(app, host=os.environ.get('LISTEN_HOST', '0.0.0.0'),
                 port=int(os.environ.get('LISTEN_PORT', '5000')),
                 debug=os.environ.get('FLASK_DEBUG') == '1')

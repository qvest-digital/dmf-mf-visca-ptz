# 🎥 VISCA PTZ Web Controller

![Project
Status](https://img.shields.io/badge/status-active-brightgreen)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Flask](https://img.shields.io/badge/flask-webapp-black)
![VISCA](https://img.shields.io/badge/protocol-VISCA-purple)

A modern, responsive **Web-based controller for VISCA PTZ cameras** with
full Gamepad (Joystick) support.

Designed for smooth, broadcast-style control via browser, supporting
**Sony VISCA over IP (UDP)**, **TCP (IP VISCA)** and **Serial (RS-232)**
connections.

------------------------------------------------------------------------

## ✨ Features

-   🎮 **Analog Gamepad Control**
    -   Smooth pan/tilt with exponential curve
    -   Instant direction change (no stop glitch)
    -   Adjustable deadzone
-   🔍 **Proportional Zoom Control**
    -   Trigger-based analog zoom
    -   Proper zoom stop detection
-   🎯 **Raw VISCA Motion Engine**
    -   Independent pan & tilt direction control
    -   Direct speed override
-   🌐 **Web-Based Interface**
    -   Works from any device on the network
    -   Touch and mouse friendly
-   🔌 **Connection Support**
    -   Sony VISCA over IP (UDP 52381)
    -   TCP/IP (VISCA over IP)
    -   Serial (RS-232)
-   ⚡ **Low-Latency Real-Time Control**
    -   WebSocket-based communication
    -   Optimized command deduplication

------------------------------------------------------------------------

## 📸 Interface Overview

                Main Control Interface
  --------------------------------------------------
   Web-based PTZ Control Panel with Gamepad Support

------------------------------------------------------------------------

## 🛠️ Requirements

-   **Python 3.8+**
-   A VISCA-compatible PTZ camera
-   One of:
    -   Serial connection (USB to RS-232 adapter)
    -   VISCA over IP support

------------------------------------------------------------------------

## 🚀 Installation

### 1️⃣ Clone the Repository

``` bash
git clone https://github.com/simoneluconi/visca-ptz-web-controller.git
cd visca-ptz-web-controller
```

### 2️⃣ Create Virtual Environment (Recommended)

``` bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

### 3️⃣ Install Dependencies

``` bash
pip install -r requirements.txt
```

------------------------------------------------------------------------

## ▶️ Running the Application

``` bash
python app.py
```

Then open:

    http://localhost:5000

Or from another device:

    http://YOUR_PC_IP:5000

------------------------------------------------------------------------

## 📌 Binding to One Camera

With `VISCA_HOST` set, the controller connects to that camera at startup,
on every page load and on every API call, and the UI shows the target
without letting it be changed.

  Variable            Default                Meaning
  ------------------- ---------------------- ----------------------------------
  `VISCA_HOST`        unset                  Camera address; unset leaves the UI free
  `VISCA_PROTOCOL`    `sony-udp`             `sony-udp` or `tcp`
  `VISCA_PORT`        `52381` / `5678`       Camera port, per protocol
  `VISCA_LOCAL_PORT`  `52381`                Local UDP port; Sony cameras reply to 52381
  `LISTEN_HOST`       `0.0.0.0`              Web server bind address
  `LISTEN_PORT`       `5000`                 Web server port
  `CORS_ALLOWED_ORIGINS` same origin         Comma-separated origins allowed to open the socket
  `SECRET_KEY`        random per start       Flask session key

``` bash
docker build -t visca-ptz .
docker run -p 5000:5000 -e VISCA_HOST=192.168.0.100 visca-ptz
```

### HTTP API

  Endpoint              Method   Body                              Result
  --------------------- -------- --------------------------------- -------------------------------
  `/api/status`         GET                                        target, configured, connected
  `/api/connect`        POST                                       connects to the configured camera
  `/api/command`        POST     `{"cmd": "move_left", ...}`       same commands as the UI socket
  `/healthz`            GET                                        `ok`
  `/auth/login`, `/auth/callback`, `/auth/logout`  GET              sign-in, with `OIDC_ISSUER` only

A Sony camera answers from port 65000 to port 52381. Behind NAT those
replies are lost; commands still execute. Over UDP, `connected` means a
socket towards the camera is open, not that the camera answered.
`POST /api/connect` and the Reconnect button reopen it and reset the
camera's sequence numbering. One controller per camera: two would
interleave their sequence numbers.

### Sign-in

With `OIDC_ISSUER` set, the page, the Socket.IO channel and `/api/*` need
a sign-in through an OpenID Connect provider: the authorization code flow
with PKCE (S256), state and nonce, the ID token verified against the
issuer's discovery document and keys. The issuer is contacted on the first
sign-in, not at start; while it is unreachable `/auth/login` answers `503`
and logs why. `/healthz` stays open for probes.

  Variable                   Default                  Meaning
  -------------------------- ------------------------ ----------------------------------
  `OIDC_ISSUER`              unset                    Issuer URL; unset leaves sign-in off and the controller open
  `OIDC_CLIENT_ID`           none, required           The OIDC client
  `OIDC_REDIRECT_URL`        none, required           `<public origin>/auth/callback` as the browser reaches it; never derived from forwarded headers
  `OIDC_CLIENT_SECRET_FILE`  unset                    File holding the client secret; unset, the client is public and authenticated by PKCE alone
  `OIDC_ROLE_CLAIM`          `groups`                 ID token claim holding the user's roles or groups; a dotted path reaches into nested claims
  `OIDC_SCOPES`              `openid,profile,email`   Scopes asked for, comma-separated
  `OIDC_OPERATOR_ROLE`       `visca-ptz-operator`     Role claim value that may control the camera; a group path counts by its last element

A user whose role claim holds the operator role controls the camera. Any
other signed-in user sees the page and `GET /api/status`; the camera is not
connected for them, their socket commands are dropped and `POST /api/*`
answers `403`. A `POST` to `/api/*` also has to come from the redirect URL's
origin, by its `Origin` header or, without one, `Sec-Fetch-Site:
same-origin`. Without a session, `/` redirects to `/auth/login`, `/api/*`
answers `401` and the socket is refused.

Sessions are held in memory and named by Flask's signed session cookie,
`HttpOnly`, `SameSite=Lax` and, when the redirect URL is `https`, `Secure`.
When the ID token's `exp` has passed, the next request renews it with the
refresh token. A session ends when the issuer refuses the refresh token, its
`refresh_expires_in` passes, at `/auth/logout`, or when the process restarts;
an open page is then sent to sign in again. `/auth/logout` also ends the
session at the issuer when it advertises an `end_session_endpoint`. There is
no bearer-token access: a caller of `/api/*` holds a browser session.

------------------------------------------------------------------------

## 🎮 Gamepad Controls

  Control         Function
  --------------- ---------------------
  Left Stick      Pan / Tilt (Analog)
  RT (R2)         Zoom In
  LT (L2)         Zoom Out
  D-Pad Up/Down   Focus Near/Far
  A Button        Emergency Stop
  Y Button        Auto Focus

### Movement Engine

The system uses:

-   Exponential sensitivity curve (EXPO)
-   Deadzone filtering
-   Continuous `move_raw` VISCA commands
-   Direction-based motion control

This allows:

✔ Instant direction reversal\
✔ No jitter at center crossing\
✔ Smooth slow movements\
✔ Broadcast-style responsiveness

------------------------------------------------------------------------

## ⚙️ Configuration

You can modify sensitivity directly in `index.html`:

``` javascript
const DEADZONE = 0.10;
const EXPO = 1.8;
const MAX_PAN = 24;
const MAX_TILT = 20;
```

------------------------------------------------------------------------

## 📂 Project Structure

``` text
visca-ptz-web-controller/
├── app.py              # Flask backend + VISCA engine
├── auth.py             # OpenID Connect sign-in
├── requirements.txt    # Python dependencies
├── Dockerfile          # Container image
├── tests/              # Unit tests (python -m unittest discover -s tests)
├── templates/
│   └── index.html      # Frontend (HTML + JS + Gamepad logic)
```

------------------------------------------------------------------------

## 🔧 Supported Commands

-   Pan / Tilt (Raw Direction Control)
-   Zoom (Variable Speed)
-   Focus (Auto / Manual / Near / Far)
-   Iris Control
-   Gain Control
-   White Balance
-   IR Remote Enable/Disable
-   LED Control

------------------------------------------------------------------------

## 🧠 How It Works

The backend:

-   Wraps VISCA commands in correct header/footer format
-   Supports both Serial and TCP sockets
-   Sends optimized packets only when motion changes

The frontend:

-   Reads Gamepad API
-   Applies exponential smoothing
-   Sends WebSocket commands in real time

------------------------------------------------------------------------

## 🌍 Deployment Ideas

-   Run on Raspberry Pi
-   Install as Docker container
-   Deploy behind Nginx reverse proxy
-   Use on tablet as wireless PTZ controller

------------------------------------------------------------------------

## 📜 License

MIT License

------------------------------------------------------------------------

## 💡 Future Improvements

-   Adjustable sensitivity via UI
-   Multi-camera preset switching
-   Rate limiting for serial stability
-   Motion smoothing filter
-   Camera position feedback (if supported)

------------------------------------------------------------------------

## 👤 Author

Created by Simone Luconi\
Industrial automation & software development enthusiast\
Specialized in IoT, AI integration, and intelligent control systems.

------------------------------------------------------------------------

If you found this useful, consider starring the repository ⭐

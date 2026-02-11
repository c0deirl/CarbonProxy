# CarbonProxy

A robust, modern web-based proxy and dashboard for the Elegoo Centauri Carbon 3D printer. This application connects to the printer via the SDCP protocol, multiplexes the webcam stream and data connection, and serves a beautiful, mobile-friendly interface for remote monitoring.


## 🚀 Key Features

*   **Connection Multiplexing (The "4-Connection Limit" Fix)**:
    *   **Webcam Proxy**: Maintains a *single* connection to the printer's camera and broadcasts the stream to unlimited clients. This bypasses the printer's hardware limit on concurrent video streams.
    *   **SDCP Data Proxy**: Provides a WebSocket endpoint that allows multiple external applications to receive real-time printer data while consuming only *one* socket on the printer.
*   **Modern Dashboard**:
    *   Sleek, dark-mode interface inspired by modern developer tools.
    *   Fully responsive design that works perfectly on mobile, tablet, and desktop.
    *   Real-time status indicators for printing state, temperatures, fans, and progress.
*   **Auto-Discovery**: Automatically finds your Centauri Carbon on the network (UDP broadcast) and configures itself.
*   **Robust Connectivity**:
    *   Auto-reconnection logic for both data and video streams.
    *   Smart camera URL scanning to find the correct video feed across various firmware versions.
    *   Handles known protocol quirks and spelling errors in the printer's SDCP implementation.

## 🐳 Docker Usage (Recommended)

### 1. Quick Start
The easiest way to run CarbonProxy is with Docker Compose.

```bash
# Clone the repo
git clone https://github.com/c0deirl/CarbonProxy.git
cd CarbonProxy

# Start the container
docker-compose up -d --build
```

Access the dashboard at **`http://<your-server-ip>:8090`**.

### 2. Configuration
If your server is on a different subnet than the printer (meaning auto-discovery won't work), you can manually set the printer's IP in `docker-compose.yml`:

```yaml
environment:
  - PRINTER_IP=192.168.1.50
  # Optional: Force a specific Mainboard ID if needed
  # - MAINBOARD_ID=...
```

## 🛠️ Manual Installation (Python)

1.  **Create a virtual environment**:
    ```bash
    python -m venv venv
    # Windows
    venv\Scripts\activate
    # Linux/Mac
    source venv/bin/activate
    ```
2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
3.  **Run the server**:
    ```bash
    python main.py
    ```

## 🔌 SDCP Data Proxy

CarbonProxy exposes a WebSocket endpoint that acts as a transparent proxy for the SDCP protocol. This is incredibly useful if you have multiple applications (e.g., Home Assistant, custom scripts, and the official slicer) that all need to talk to the printer simultaneously.

**Endpoint:** `ws://<your-server-ip>:8090/sdcp`

### How it works:
1.  **Single Upstream Connection**: The application maintains one persistent WebSocket connection to the printer's port 3030.
2.  **Broadcast**: Any status update or message received from the printer is immediately broadcast to **all** clients connected to the proxy endpoint.
3.  **Forwarding**: Any command sent by a client to the proxy is forwarded directly to the printer.
4.  **Instant State**: New clients immediately receive the last known status packet upon connection, so they don't have to wait for the next broadcast.

### Example Usage (Python):
```python
import asyncio
import websockets

async def listen():
    async with websockets.connect("ws://localhost:8090/sdcp") as websocket:
        while True:
            message = await websocket.recv()
            print(f"Received from printer via proxy: {message}")

asyncio.run(listen())
```

## 🏗️ Architecture

*   **Backend**: Built with **FastAPI** (Python).
    *   `PrinterClient`: Handles the raw SDCP protocol, auto-discovery, and socket management.
    *   `StreamManager`: Handles the MJPEG video stream, probing for valid URLs, and broadcasting frames to multiple queues.
*   **Frontend**: HTML5/CSS3/JavaScript.
    *   No build steps required (vanilla JS).
    *   Uses efficient polling for status updates and an `<img>` tag for the MJPEG stream.

## ⚠️ Disclaimer
This software is not affiliated with Elegoo. It is an independent community project. Use at your own risk.

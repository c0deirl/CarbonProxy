import asyncio
import logging
import os
import httpx
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import websockets.exceptions

from printer_client import PrinterClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CarbonProxy")

app = FastAPI(title="Elegoo Centauri Carbon Proxy")

# Initialize Printer Client
printer = PrinterClient()

# Templates
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.on_event("startup")
async def startup_event():
    # Try to discover printer
    discovered = await printer.discover()
    
    # Check env vars
    manual_ip = os.getenv("PRINTER_IP")
    if manual_ip and not printer.ip:
        printer.set_manual_ip(manual_ip)
        
    manual_id = os.getenv("MAINBOARD_ID")
    if manual_id:
        printer.mainboard_id = manual_id
        logger.info(f"Manual MainboardID set: {manual_id}")
    
    if printer.ip:
        printer.start()
    else:
        logger.warning("No printer IP found. Set PRINTER_IP environment variable.")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/status")
async def get_status():
    return {
        "connected": printer.connected,
        "ip": printer.ip,
        "status": printer.status
    }

class DynamicStreamResponse(StreamingResponse):
    def __init__(self, content, **kwargs):
        super().__init__(content, **kwargs)
        self.dynamic_media_type = None

    async def stream_response(self, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        async for chunk in self.body_iterator:
            if not isinstance(chunk, bytes):
                chunk = chunk.encode(self.charset)
            await send({"type": "http.response.body", "body": chunk, "more_body": True})

        await send({"type": "http.response.body", "body": b"", "more_body": False})

class StreamManager:
    def __init__(self):
        self.clients = set()
        self.active = False
        self.content_type = "multipart/x-mixed-replace; boundary=boundary"
        self._stream_task = None

    async def subscribe(self):
        queue = asyncio.Queue()
        self.clients.add(queue)
        if not self.active:
            self._start_stream()
        return queue

    async def unsubscribe(self, queue):
        self.clients.discard(queue)
        if not self.clients:
            self.active = False
            # The task will eventually stop when it notices active is False or we can cancel it
            if self._stream_task:
                self._stream_task.cancel()
                try:
                    await self._stream_task
                except asyncio.CancelledError:
                    pass
                self._stream_task = None
                logger.info("Stopped upstream camera connection (no clients)")

    def _start_stream(self):
        self.active = True
        self._stream_task = asyncio.create_task(self._stream_loop())
        logger.info("Started upstream camera connection")

    async def _stream_loop(self):
        # Prioritize discovered URL, then fallback
        target_url = printer.camera_url
        
        candidates = []
        if target_url:
            candidates.append(target_url)
        if printer.ip:
             candidates.extend([
                 f"http://{printer.ip}:8080/?action=stream",
                 f"http://{printer.ip}:3031/stream",
                 f"http://{printer.ip}:3031/video",
                 f"http://{printer.ip}:3031/",
                 f"http://{printer.ip}:8081/?action=stream",
                 f"http://{printer.ip}/webcam/?action=stream"
             ])
        
        if not candidates:
            logger.error("No camera candidates available")
            self.active = False
            return

        # Keep trying to connect as long as we have clients
        while self.clients:
            logger.info(f"Starting camera connection attempt loop. Candidates: {len(candidates)}")
            async with httpx.AsyncClient(verify=False, timeout=httpx.Timeout(5.0, connect=2.0)) as client:
                success = False
                # Find working URL
                for url in candidates:
                    # Check if we should still be running
                    if not self.clients: 
                        break
                    
                    logger.info(f"Trying camera URL: {url}")
                    try:
                        # Probe with HEAD first (sometimes safer for finding valid endpoints without starting stream)
                        try:
                            head_resp = await client.head(url)
                            if head_resp.status_code == 200:
                                logger.info(f"HEAD probe successful for {url}")
                            elif head_resp.status_code == 405: # Method Not Allowed - might only support GET
                                pass 
                            elif head_resp.status_code >= 400:
                                logger.warning(f"HEAD probe returned {head_resp.status_code} for {url}")
                                # If HEAD fails with error, GET might also fail, but we try anyway
                        except Exception as ex:
                            logger.debug(f"HEAD probe failed for {url}: {ex}")

                        # Connect with GET
                        async with client.stream("GET", url, timeout=None) as response:
                            if response.status_code == 200:
                                 logger.info(f"Found working stream at {url}")
                                 ct = response.headers.get("content-type")
                                 if ct:
                                     self.content_type = ct
                                     logger.info(f"Upstream content-type: {ct}")
                                 
                                 success = True
                                 
                                 # Stream loop
                                 async for chunk in response.aiter_bytes():
                                     if not self.clients: # Check if clients are still connected
                                         break
                                     # Broadcast
                                     for queue in list(self.clients):
                                         queue.put_nowait(chunk)
                                 
                                 logger.info(f"Stream from {url} ended")
                                 if not self.clients:
                                     return # Exit if no clients left
                                 break # Break candidate loop to restart outer loop (reconnect)
                            else:
                                logger.warning(f"Stream returned {response.status_code} from {url}")
                    except Exception as e:
                        logger.error(f"Stream error for {url}: {e}")
                        # Don't sleep here, just try next candidate immediately
                
                if not success:
                    logger.error("All camera candidates failed. Retrying in 5 seconds...")
                    # Wait before retrying entire list to avoid hammering
                    try:
                        await asyncio.sleep(5)
                    except asyncio.CancelledError:
                        break
        
        self.active = False
        logger.info("Stream loop exited (no clients or cancelled)")

stream_manager = StreamManager()

@app.get("/stream")
async def proxy_stream():
    queue = await stream_manager.subscribe()
    
    async def stream_generator():
        try:
            while True:
                chunk = await queue.get()
                yield chunk
        except asyncio.CancelledError:
            logger.info("Client disconnected from stream")
            raise
        finally:
            await stream_manager.unsubscribe(queue)

    return DynamicStreamResponse(stream_generator(), media_type=stream_manager.content_type)

@app.websocket("/sdcp")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await printer.register_proxy_client(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Forward data from client to printer
            # Note: We should probably validate or sanitize this data 
            # to ensure it's a valid SDCP command, but for a transparent proxy,
            # passing it through is often desired.
            await printer.forward_to_printer(data)
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        await printer.unregister_proxy_client(websocket)
    except Exception as e:
        logger.error(f"WebSocket proxy error: {e}")
        await printer.unregister_proxy_client(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8090)

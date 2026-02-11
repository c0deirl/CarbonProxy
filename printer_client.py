import asyncio
import json
import socket
import logging
import websockets
import time
from typing import Optional, Dict, Any, Callable

logger = logging.getLogger(__name__)

class PrinterClient:
    def __init__(self):
        self.ip: Optional[str] = None
        self.mainboard_id: Optional[str] = None
        self.ws_url: Optional[str] = None
        self.camera_url: Optional[str] = None
        self.status: Dict[str, Any] = {}
        self.connected = False
        self._ws = None
        self._stop_event = asyncio.Event()
        self.on_status_update: Optional[Callable[[Dict], None]] = None
        self._proxy_clients = set() # Set of connected proxy websockets

    async def register_proxy_client(self, ws):
        """Register a new client to receive broadcasted messages."""
        self._proxy_clients.add(ws)
        logger.info(f"Client registered. Total clients: {len(self._proxy_clients)}")
        try:
            # Send current status immediately upon connection
            if self.status:
                # Wrap it in a structure that mimics what the printer sends if needed,
                # or just send the raw status object? 
                # SDCP clients expect specific topics. 
                # Ideally, we should cache the last raw messages for topics.
                # For now, let's just send the status update as if it just arrived.
                # We need to reconstruct a valid SDCP message.
                msg = {
                    "Topic": f"sdcp/status/{self.mainboard_id}",
                    "Status": self.status
                }
                await ws.send_text(json.dumps(msg))
        except Exception as e:
            logger.error(f"Error sending initial status to client: {e}")

    async def unregister_proxy_client(self, ws):
        """Remove a client from the broadcast list."""
        self._proxy_clients.discard(ws)
        logger.info(f"Client unregistered. Total clients: {len(self._proxy_clients)}")

    async def forward_to_printer(self, message: str):
        """Forward a message from a client to the printer."""
        if self._ws and self.connected:
            try:
                await self._ws.send(message)
            except Exception as e:
                logger.error(f"Error forwarding message to printer: {e}")
        else:
            logger.warning("Cannot forward message: Not connected to printer.")

    async def discover(self, timeout: int = 5) -> bool:
        """
        Discover the printer using UDP broadcast.
        """
        logger.info("Starting printer discovery...")
        loop = asyncio.get_running_loop()
        
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: DiscoveryProtocol(),
                local_addr=('0.0.0.0', 0)
            )
            
            # Send broadcast
            sock = transport.get_extra_info('socket')
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            transport.sendto(b"M99999", ('255.255.255.255', 3000))
            
            start_time = time.time()
            while time.time() - start_time < timeout:
                if protocol.response:
                    data = protocol.response
                    self.ip = data.get("Data", {}).get("MainboardIP")
                    self.mainboard_id = data.get("Data", {}).get("MainboardID")
                    logger.info(f"Discovered printer at {self.ip} with ID {self.mainboard_id}")
                    transport.close()
                    return True
                await asyncio.sleep(0.1)
            
            transport.close()
            logger.warning("Discovery timed out.")
            return False
            
        except Exception as e:
            logger.error(f"Discovery failed: {e}")
            return False

    def set_manual_ip(self, ip: str):
        self.ip = ip
        logger.info(f"Manual IP set to {self.ip}")

    async def connect(self):
        if not self.ip:
            raise ValueError("Printer IP not set. Run discover() or set_manual_ip().")

        self.ws_url = f"ws://{self.ip}:3030/websocket"
        logger.info(f"Connecting to {self.ws_url}...")

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self._ws = ws
                    self.connected = True
                    logger.info("Connected to printer WebSocket.")
                    
                    # Start heartbeat task
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                    
                    # Request Attributes (Cmd 1) and Camera (Cmd 386)
                    # If we don't have MainboardID, we send to generic topic.
                    await self._send_command(1) 
                    await self._send_command(386)
                    
                    try:
                        async for message in ws:
                            await self._handle_message(message)
                    except websockets.ConnectionClosed:
                        logger.warning("WebSocket connection closed. Reconnecting...")
                    finally:
                        self.connected = False
                        heartbeat_task.cancel()
                        
            except Exception as e:
                logger.error(f"Connection error: {e}. Retrying in 5s...")
                await asyncio.sleep(5)

    async def _heartbeat_loop(self):
        while self.connected:
            try:
                await self._ws.send("ping")
                # Also retry getting attributes if we still don't have ID
                if not self.mainboard_id:
                     await self._send_command(1)
                else:
                     # Request status update periodically just in case
                     await self._send_command(0)

                await asyncio.sleep(5) # Send ping every 5 seconds
            except Exception:
                break

    async def _send_command(self, cmd: int, data: Dict = {}):
        if not self._ws:
            return
        
        # If we don't have ID, try generic request topic
        topic = f"sdcp/request/{self.mainboard_id}" if self.mainboard_id else "sdcp/request"
        
        payload = {
            "Id": "uuid-placeholder", 
            "Data": {
                "Cmd": cmd,
                "Data": data,
                "RequestID": "uuid-req",
                "MainboardID": self.mainboard_id or "",
                "TimeStamp": int(time.time()),
                "From": 1 
            },
            "Topic": topic
        }
        logger.debug(f"Sending Cmd {cmd} to {topic}")
        await self._ws.send(json.dumps(payload))

    async def _handle_message(self, message: str):
        # Broadcast raw message to all connected proxy clients
        if self._proxy_clients:
            # We must iterate over a copy because the set might change during iteration
            # or if a send fails and we remove the client.
            for client in list(self._proxy_clients):
                try:
                    await client.send_text(message)
                except Exception as e:
                    logger.error(f"Error broadcasting to client: {e}")
                    await self.unregister_proxy_client(client)

        try:
            if message == "pong":
                return

            data = json.loads(message)
            topic = data.get("Topic", "")
            
            # Try to extract MainboardID from topic if missing
            if not self.mainboard_id and topic:
                parts = topic.split("/")
                if len(parts) >= 3:
                    # sdcp/status/{ID}
                    # sdcp/response/{ID}
                    possible_id = parts[-1]
                    if len(possible_id) > 10: # Simple heuristic
                        self.mainboard_id = possible_id
                        logger.info(f"Extracted MainboardID from topic: {self.mainboard_id}")
                        # Now that we have ID, request camera again
                        await self._send_command(386)

            # Handle Status Updates
            if "sdcp/status" in topic:
                status_data = data.get("Status", {}) or data.get("Data", {}).get("Status", {})
                # Merge status
                self._update_status(status_data)
                
                # Also update MainboardID if present
                if not self.mainboard_id and "MainboardID" in data:
                    self.mainboard_id = data["MainboardID"]

            # Handle Responses
            elif "sdcp/response" in topic:
                cmd = data.get("Data", {}).get("Cmd")
                resp_data = data.get("Data", {}).get("Data", {})
                
                if cmd == 1: # Attributes
                     if not self.mainboard_id:
                         self.mainboard_id = data.get("Data", {}).get("MainboardID")
                
                if cmd == 386: # Camera URL
                    # The response format for 386 needs to be checked.
                    # Ack: 3 usually means "Busy" or "Not Ready" or "Error"
                    logger.info(f"Camera Response: {resp_data}")
                    
                    if isinstance(resp_data, dict):
                        # Check for success or specific URL field
                        url = resp_data.get("Url") or resp_data.get("URL") or resp_data.get("Data", {}).get("Url")
                        if url:
                            self.camera_url = url
                            logger.info(f"Camera URL set to: {self.camera_url}")
                        else:
                             # If we got a response but no URL, it might be an error code
                             ack = resp_data.get("Ack")
                             if ack == 3:
                                  logger.warning("Printer returned Ack: 3 (Busy/Error) for camera request.")
                                  # We should try to guess the URL anyway if we have the IP
                                  # or retry later.
                    elif isinstance(resp_data, str):
                         self.camera_url = resp_data

        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    def _update_status(self, new_status: Dict):
        # Handle known field spelling errors from OpenCentauri docs
        if "CurrenCoord" in new_status:
             new_status["CurrentCoord"] = new_status["CurrenCoord"]
        
        if "RelaseFilmState" in new_status:
             new_status["ReleaseFilmState"] = new_status["RelaseFilmState"]

        if "MaximumCloudSDCPSercicesAllowed" in new_status:
             new_status["MaximumCloudSDCPServicesAllowed"] = new_status["MaximumCloudSDCPSercicesAllowed"]

        self.status.update(new_status)
        if self.on_status_update:
            self.on_status_update(self.status)

    def start(self):
        asyncio.create_task(self.connect())

class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.response = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            message = json.loads(data.decode())
            # Check if it looks like the printer response
            if "Id" in message and "Data" in message:
                self.response = message
        except:
            pass

"""
WebSocket management for real-time updates
"""
from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict, Set
import asyncio

class ConnectionManager:
    """Manage WebSocket connections for real-time job updates."""
    
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.global_connections: Set[WebSocket] = set()
    
    async def connect(self, websocket: WebSocket, job_id: str = None):
        """Connect a WebSocket client."""
        await websocket.accept()
        
        if job_id:
            if job_id not in self.active_connections:
                self.active_connections[job_id] = set()
            self.active_connections[job_id].add(websocket)
            print(f"✅ WebSocket connected for job {job_id}")
        else:
            self.global_connections.add(websocket)
            print(f"✅ Global WebSocket connected")
    
    def disconnect(self, websocket: WebSocket, job_id: str = None):
        """Disconnect a WebSocket client."""
        if job_id and job_id in self.active_connections:
            self.active_connections[job_id].discard(websocket)
            if not self.active_connections[job_id]:
                del self.active_connections[job_id]
            print(f"❌ WebSocket disconnected for job {job_id}")
        else:
            self.global_connections.discard(websocket)
            print(f"❌ Global WebSocket disconnected")
    
    async def send_job_update(self, job_id: str, message: dict):
        """Send update to all clients watching a specific job."""
        if job_id in self.active_connections:
            disconnected = set()
            for connection in self.active_connections[job_id]:
                try:
                    await connection.send_json(message)
                except Exception as e:
                    print(f"⚠️ Failed to send to WebSocket: {e}")
                    disconnected.add(connection)
            
            # Remove disconnected clients
            for conn in disconnected:
                self.disconnect(conn, job_id)
    
    async def broadcast(self, message: dict):
        """Broadcast message to all global connections."""
        disconnected = set()
        for connection in self.global_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"⚠️ Failed to broadcast: {e}")
                disconnected.add(connection)
        
        # Remove disconnected clients
        for conn in disconnected:
            self.disconnect(conn)

# Global instance
manager = ConnectionManager()

async def send_job_update_notification(job_id: str, status: str, progress: int, message: str, result: dict = None):
    """Send WebSocket notification for job update."""
    try:
        update_message = {
            "job_id": job_id,
            "status": status,
            "progress": progress,
            "message": message,
            "timestamp": asyncio.get_event_loop().time()
        }
        
        if result:
            update_message["result"] = result
        
        await manager.send_job_update(job_id, update_message)
        await manager.broadcast(update_message)
        
    except Exception as e:
        print(f"⚠️ Error sending WebSocket notification: {e}")

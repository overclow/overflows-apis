#!/usr/bin/env python3
"""
ForgeRealm Digital Artifacts Engine - Refactored Modular Version
Transform your creative vision into stunning 3D realities

🎨 Instant Forge: Upload images → Animated 3D artifacts
🧠 Vision Forge: Text descriptions → AI-generated 3D artifacts  
🌍 Location Forge: GPS coordinates → Digital twins from Street View
🎭 Fusion Forge: Blend elements → Unique hybrid artifacts
⚡ Advanced artisan tools for professional digital crafting
"""

import os
import sys
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Load environment variables
load_dotenv(override=True)

# Add current directory to path for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Import modular components
from api.models import *
from api.job_manager import get_job_status, update_job_status, get_all_jobs
from api.websocket_manager import manager
from api.routes import animation, vision, analysis, jobs, generation, fusion

# Import database service
from db_service import initialize_database, DatabaseService

# Initialize database
db_service = DatabaseService()
print("🔌 Initializing database service...")
try:
    db_initialized = initialize_database()
    if db_initialized:
        print("✅ Database connection established")
    else:
        print("⚠️ Database unavailable - using in-memory storage")
except Exception as e:
    print(f"⚠️ Database initialization failed: {e}")

# Initialize FastAPI app
app = FastAPI(
    title="ForgeRealm Digital Artifacts API - Modular Edition", 
    version="2.0.0",
    description="""
🏗️ **ForgeRealm - Forge Your Digital Artifacts**

Refactored modular architecture for better maintainability and scalability.

## 🎯 Features:
- **Modular Design**: Organized by functionality (animation, vision, analysis)
- **Clean Separation**: Models, routes, utilities in separate modules
- **Easy Maintenance**: Smaller, focused files instead of monolithic structure
- **Scalable**: Easy to add new features without affecting existing code
"""
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include route modules
app.include_router(animation.router)
app.include_router(vision.router)
app.include_router(vision.router_enhance)  # Add the enhance router
app.include_router(analysis.router)
app.include_router(jobs.router)
app.include_router(generation.router)
app.include_router(fusion.router)

# ========= WEBSOCKET ENDPOINTS =========

@app.websocket("/ws/jobs/{job_id}")
async def websocket_job_updates(websocket: WebSocket, job_id: str):
    """WebSocket endpoint for real-time job updates."""
    await manager.connect(websocket, job_id)
    try:
        while True:
            # Keep connection alive and listen for client messages
            data = await websocket.receive_text()
            # Echo back for heartbeat
            await websocket.send_text(f"Job {job_id}: {data}")
    except WebSocketDisconnect:
        manager.disconnect(websocket, job_id)

@app.websocket("/ws/jobs")
async def websocket_all_jobs(websocket: WebSocket):
    """WebSocket endpoint for all job updates."""
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"Global: {data}")
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ========= ROOT ENDPOINT =========

@app.get("/")
async def root():
    """API information and available endpoints."""
    return {
        "name": "ForgeRealm Digital Artifacts API",
        "version": "2.0.0",
        "architecture": "Modular",
        "status": "operational",
        "endpoints": {
            "animation": "/animate/*",
            "vision": "/enhance/*",
            "analysis": "/analyze/*",
            "jobs": "/jobs/*",
            "status": "/status/{job_id}",
            "websocket": "/ws/jobs/{job_id}"
        },
        "documentation": "/docs",
        "modules": [
            "api.models - Request/Response models",
            "api.job_manager - Job tracking and status",
            "api.s3_utils - Cloud storage utilities",
            "api.image_utils - Image processing",
            "api.websocket_manager - Real-time updates",
            "api.routes.animation - Animation endpoints",
            "api.routes.vision - Vision and prompts",
            "api.routes.analysis - Image analysis",
            "api.routes.jobs - Job management"
        ]
    }

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "database": "connected" if db_service else "unavailable",
        "modules_loaded": True
    }

# ========= STARTUP EVENT =========

@app.on_event("startup")
async def startup_event():
    """Initialize services on startup."""
    print("=" * 80)
    print("🚀 ForgeRealm Digital Artifacts Engine - Modular Edition")
    print("=" * 80)
    print("✅ Modular architecture loaded")
    print("📁 Project structure:")
    print("   /api/models.py - Data models")
    print("   /api/job_manager.py - Job tracking")
    print("   /api/s3_utils.py - S3 operations")
    print("   /api/image_utils.py - Image processing")
    print("   /api/websocket_manager.py - WebSocket handling")
    print("   /api/routes/ - API endpoints")
    print("=" * 80)

if __name__ == "__main__":
    uvicorn.run(
        "animation_api_modular:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )

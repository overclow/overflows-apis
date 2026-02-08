#!/usr/bin/env python3
"""
ForgeRealm Digital Artifacts Engine
Forge your digital artifacts - transform creative vision into stunning 3D realities:

🎨 Instant Forge: Upload images → Animated 3D artifacts
🧠 Vision Forge: Text descriptions → AI-generated 3D artifacts  
🌍 Location Forge: GPS coordinates → Digital twins from Street View
🎭 Fusion Forge: Blend elements → Unique hybrid artifacts
⚡ Advanced artisan tools for professional digital crafting
"""

import os, time, json, subprocess, shutil, tempfile, uuid, asyncio, threading, base64, pathlib, re, mimetypes, io, sys
import numpy as np
from typing import Optional, List, Union, Dict
import boto3, requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks, Form, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn
from concurrent.futures import ThreadPoolExecutor
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageSequence

from db_service import DatabaseService

# Import database service
from db_service import (
    get_db_service, initialize_database, 
    update_job_status_db, get_job_db, get_all_jobs_db, delete_job_db
)
db_service = DatabaseService()
# Load environment variables
load_dotenv(override=True)  # Force reload of environment variables

# Debug environment variables
print(f"🔧 Environment check:")
print(f"   USE_GEMINI: {os.getenv('USE_GEMINI', 'NOT SET')}")
print(f"   GEMINI_API_KEY: {'SET' if os.getenv('GEMINI_API_KEY') else 'NOT SET'}")

# Import functions from test3danimate
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    from test3danimate import (
        s3_client,
        upload_glb_presigned,
        download_file, retexture_with_meshy,
        API_V1, MESHY_KEY, AWS_REGION, S3_BUCKET, PRESIGN_TTL,
        HEIGHT_METERS, ANIM_FPS, USE_RETEXTURE, RETEXTURE_PROMPT
    )
except ImportError as e:
    print(f"⚠️ Warning: Could not import from test3danimate: {e}")
    print("Some functions may not work without the main pipeline module.")

# Import mapapi functions for image generation and street view
try:
    import mapapi
except ImportError:
    print("⚠️ Warning: mapapi not available. Image/coordinate processing may not work.")
    mapapi = None

# RAG Configuration for image generation with references
# Note: CLIP model loading moved to on-demand loading to prevent startup crashes
RAG_DEPENDENCIES_AVAILABLE = False
FAISS_AVAILABLE = False
clip_model = None
clip_preprocess = None

try:
    import google.generativeai as genai
    
    # RAG Configuration with only Gemini (essential)
    USE_GEMINI = os.getenv("USE_GEMINI", "0") == "1"
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-image")
    
    if USE_GEMINI and GEMINI_API_KEY:
        genai.configure(api_key=GEMINI_API_KEY)
        print("✅ Gemini image generation enabled")
    else:
        print("⚠️ Gemini disabled - set USE_GEMINI=1 and GEMINI_API_KEY to enable")
    
    # Replicate API Configuration
    REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY")
    if REPLICATE_API_KEY:
        print("✅ Replicate API key configured")
    else:
        print("⚠️ Replicate API key not set - WAN 2.2 i2v-fast will not work")
    
    # Try to import optional RAG dependencies
    try:
        import numpy as np
        import open_clip
        import torch
        from PIL import Image as PILImage
        
        # Try to import FAISS
        try:
            import faiss
            FAISS_AVAILABLE = True
            print("✅ FAISS available for RAG image search")
        except ImportError:
            faiss = None
            FAISS_AVAILABLE = False
            print("⚠️ FAISS not available - RAG will use text-only generation")
        
        RAG_DEPENDENCIES_AVAILABLE = True
        RAG_IMAGE_INDEX_PATH = os.getenv("RAG_IMAGE_INDEX_PATH", "index_store/game_clip_vitb32")
        CLIP_MODEL = "ViT-B-32"
        CLIP_PRETRAIN = "laion2b_s34b_b79k"
        DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"✅ RAG dependencies available - CLIP model will load on demand")
        
    except ImportError as e:
        RAG_DEPENDENCIES_AVAILABLE = False
        print(f"⚠️ RAG dependencies not available: {e}")
        print("🔄 Gemini will work in text-only mode")
        
except ImportError as e:
    print(f"❌ Critical error: Gemini not available: {e}")
    USE_GEMINI = False
    GEMINI_API_KEY = None

# Import jobs API router
from jobs_api import router as jobs_router

app = FastAPI(
    title="ForgeRealm Digital Artifacts API", 
    version="1.0.0",
    description="""
🏗️ **ForgeRealm - Forge Your Digital Artifacts**

Transform your creative vision into stunning 3D digital artifacts with our comprehensive AI-powered pipeline.

## 🎯 What You Can Forge:
- **3D Models** from images, text descriptions, or GPS coordinates
- **Animated Characters** with rigging and motion sequences  
- **Digital Twins** of real-world locations using Street View
- **Fusion Artifacts** by blending multiple creative elements
- **Enhanced Textures** with AI-powered material generation

## ⚡ Forge Modes:
- **Instant Forge**: Upload an image → Get animated 3D model
- **Vision Forge**: Describe your artifact → AI generates and animates
- **Location Forge**: Enter coordinates → Create digital twin from Street View  
- **Fusion Forge**: Combine multiple elements → Unique hybrid artifacts
- **Motion Forge**: Add life to your creations with professional animations

## 🛠️ Artisan Tools:
- Advanced material enhancement and retexturing
- Professional rigging for character animation
- Cloud storage with instant download URLs
- Real-time progress tracking for all forge operations
- Asset management and metadata preservation

Start forging your digital realm today! 🚀
""")

# Include the jobs router
app.include_router(jobs_router, prefix="/api", tags=["Jobs"])

# ========= WEBSOCKET MANAGER FOR REAL-TIME UPDATES =========

class ConnectionManager:
    """Manages WebSocket connections for real-time job updates."""
    
    def __init__(self):
        # Store active connections by job_id
        self.active_connections: Dict[str, List[WebSocket]] = {}
        # Store all connections for broadcast
        self.all_connections: List[WebSocket] = []
    
    async def connect(self, websocket: WebSocket, job_id: str = None):
        """Accept a new WebSocket connection."""
        await websocket.accept()
        self.all_connections.append(websocket)
        
        if job_id:
            if job_id not in self.active_connections:
                self.active_connections[job_id] = []
            self.active_connections[job_id].append(websocket)
            print(f"🔌 WebSocket connected for job: {job_id} (total: {len(self.active_connections[job_id])})")
        else:
            print(f"🔌 WebSocket connected (broadcast mode)")
    
    def disconnect(self, websocket: WebSocket, job_id: str = None):
        """Remove a WebSocket connection."""
        if websocket in self.all_connections:
            self.all_connections.remove(websocket)
        
        if job_id and job_id in self.active_connections:
            if websocket in self.active_connections[job_id]:
                self.active_connections[job_id].remove(websocket)
                print(f"🔌 WebSocket disconnected for job: {job_id} (remaining: {len(self.active_connections[job_id])})")
                
                # Clean up empty job lists
                if not self.active_connections[job_id]:
                    del self.active_connections[job_id]
    
    async def send_job_update(self, job_id: str, data: dict):
        """Send update to all clients listening to a specific job."""
        if job_id in self.active_connections:
            # Add timestamp
            data["timestamp"] = time.time()
            data["job_id"] = job_id
            
            disconnected = []
            for connection in self.active_connections[job_id]:
                try:
                    await connection.send_json(data)
                except Exception as e:
                    print(f"❌ Failed to send to WebSocket: {e}")
                    disconnected.append(connection)
            
            # Clean up disconnected clients
            for connection in disconnected:
                self.disconnect(connection, job_id)
    
    async def broadcast(self, data: dict):
        """Broadcast message to all connected clients."""
        data["timestamp"] = time.time()
        
        disconnected = []
        for connection in self.all_connections:
            try:
                await connection.send_json(data)
            except Exception as e:
                print(f"❌ Failed to broadcast to WebSocket: {e}")
                disconnected.append(connection)
        
        # Clean up disconnected clients
        for connection in disconnected:
            if connection in self.all_connections:
                self.all_connections.remove(connection)

# Initialize connection manager
manager = ConnectionManager()

@app.on_event("startup")
async def startup_event():
    """Initialize the ForgeRealm Digital Artifacts Engine."""
    print("🚀 Starting ForgeRealm Digital Artifacts Engine...")
    print(f"⚡ Thread pool configured with {thread_pool._max_workers} workers")
    print("�️ Database-first architecture - jobs will be loaded on demand")
    
    # Initialize any startup tasks here (no job loading needed)
    try:
        # Test database connectivity if available
        service = get_db_service()
        if service and service.ensure_connection():
            total_jobs = await asyncio.get_event_loop().run_in_executor(
                None, lambda: len(service.list_jobs(limit=1000))  # Get more jobs for count
            )
            print(f"📊 Database contains {total_jobs} artifacts in total")
        else:
            print("💾 Using in-memory storage fallback")
    except Exception as e:
        print(f"⚠️ Database check failed, using fallback: {e}")
    
    print("✅ ForgeRealm Digital Artifacts Engine ready!")
    print("🎨 Ready to forge amazing digital artifacts!")

# Add CORS middleware to allow frontend requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",     # React dev server
        "http://localhost:5173",     # Vite dev server  
        "http://localhost:8080",     # Vue dev server
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173", 
        "http://127.0.0.1:8080",
        "*"  # Allow all origins for development (remove in production)
    ],
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods (GET, POST, PUT, DELETE, etc.)
    allow_headers=["*"],  # Allow all headers
)

# ========= REQUEST MODELS =========
class PromptRequest(BaseModel):
    prompt: str
    animation_ids: Optional[List[int]] = []
    height_meters: Optional[float] = 1.75
    fps: Optional[int] = 60
    use_retexture: Optional[bool] = False

class CoordinatesRequest(BaseModel):
    latitude: float
    longitude: float
    animation_ids: Optional[List[int]] = [106, 30, 55]
    height_meters: Optional[float] = 1.75
    fps: Optional[int] = 60
    use_retexture: Optional[bool] = False

class ImageRequest(BaseModel):
    animation_ids: Optional[List[int]] = [106, 30, 55]
    height_meters: Optional[float] = 1.75
    fps: Optional[int] = 60
    use_retexture: Optional[bool] = True

# ========= RETEXTURING REQUEST MODEL =========
class RetextureRequest(BaseModel):
    presigned_url: str
    prompt: Optional[str] = None
    job_id: Optional[str] = None

# ========= CONFIRMATION REQUEST MODEL =========
class ConfirmationRequest(BaseModel):
    job_id: str
    action: str  # "continue" or "cancel"
    message: Optional[str] = None

# ========= DESIGN EDIT REQUEST MODEL =========
class DesignEditRequest(BaseModel):
    prompt: str = Field(..., description="Text prompt describing the image to generate or edit")
    job_id: Optional[str] = Field(None, description="Job ID to save the edited image to (for confirmation workflow)")
    enable_base64_output: bool = Field(False, description="Whether to return image as base64")
    enable_sync_mode: bool = Field(False, description="Whether to use synchronous mode")
    output_format: str = Field("png", description="Output image format (png, jpg, webp)")
    timeout_seconds: Optional[int] = Field(300, description="Maximum time to wait for completion (seconds)")

# ========= DESIGN POSES REQUEST MODEL =========
class DesignPosesRequest(BaseModel):
    character_image: str = Field(..., description="Publicly accessible image URL of the character")
    job_id: Optional[str] = Field(None, description="Job ID to save the posed image to (for workflow tracking)")
    timeout_seconds: Optional[int] = Field(300, description="Maximum time to wait for completion (seconds)")

# ========= INFUSE REQUEST MODEL =========
class InfuseRequest(BaseModel):
    animation_ids: Optional[List[int]] = [106, 30, 55]
    height_meters: Optional[float] = 1.75
    fps: Optional[int] = 60
    use_retexture: Optional[bool] = True
    fusion_style: Optional[str] = "blend"  # Options: "blend", "merge", "hybrid"
    fusion_strength: Optional[float] = 0.5  # 0.0 to 1.0, balance between images

# ========= WAN 2.2 ANIMATE REQUEST MODEL =========
class WanAnimateRequest(BaseModel):
    mode: str = "animate"
    image: str
    prompt: str
    resolution: Optional[str] = "480p"
    video: Optional[str] = None
    seed: Optional[int] = 42

# ========= WAN 2.2 I2V FAST REQUEST MODEL (Replicate) =========
class Wan22FastRequest(BaseModel):
    """Request model for WAN 2.2 i2v-fast via Replicate
    
    Pricing:
    - 480p resolution: $0.05 per output video
    - 720p resolution: $0.11 per output video
    """
    image: str = Field(description="URL of the image to animate")
    prompt: str = Field(description="Animation prompt describing the desired motion")
    resolution: Optional[str] = Field(default="480p", description="Video resolution: 480p ($0.05) or 720p ($0.11)")
    go_fast: Optional[bool] = Field(default=True, description="Enable fast processing mode")
    num_frames: Optional[int] = Field(default=81, description="Number of video frames (81 recommended)")
    sample_shift: Optional[int] = Field(default=12, description="Sample shift factor (1-20, default: 12)")
    frames_per_second: Optional[int] = Field(default=16, description="FPS for output video (5-30, default: 16)")
    last_image: Optional[str] = Field(default=None, description="Optional last frame image for smoother transitions")
    interpolate_output: Optional[bool] = Field(default=True, description="Interpolate to 30 FPS using ffmpeg")
    disable_safety_checker: Optional[bool] = Field(default=False, description="Disable content safety checker")
    lora_scale_transformer: Optional[float] = Field(default=1.0, description="LoRA transformer scale")
    lora_scale_transformer_2: Optional[float] = Field(default=1.0, description="LoRA transformer 2 scale")

class Wan22AnimateRequest(BaseModel):
    """Request model for WAN 2.2 Animate-Animation via Replicate
    
    Transfer motion from a reference video to animate a character image.
    
    Pricing:
    - Approximately 333 seconds for $1
    - Cost: ~$0.003 per second (0.3 cents/sec)
    - Example: 5-second video = ~$0.015 (1.5 cents)
    """
    video: str = Field(description="URL of reference video showing the desired movement/action")
    character_image: str = Field(description="URL of character image to animate with the video's motion")
    go_fast: Optional[bool] = Field(default=True, description="Enable fast processing mode")
    refert_num: Optional[int] = Field(default=1, description="Reference number (1 recommended)")
    resolution: Optional[str] = Field(default="720", description="Video resolution: 480, 720, 1080")
    merge_audio: Optional[bool] = Field(default=True, description="Merge audio from reference video")
    frames_per_second: Optional[int] = Field(default=24, description="FPS for output video (5-30, default: 24)")

class IdeogramV3TurboRequest(BaseModel):
    """Request model for Ideogram V3 Turbo via Replicate
    
    Add text on images with high-quality typography rendering.
    
    Pricing:
    - $0.03 per output image
    """
    prompt: str = Field(description="Text prompt including the text to render on the image and style description")
    image: Optional[str] = Field(default=None, description="Optional input image URL for inpainting (requires mask)")
    mask: Optional[str] = Field(default=None, description="Optional mask image URL for inpainting (white areas will be replaced)")
    aspect_ratio: Optional[str] = Field(default="3:2", description="Aspect ratio: 1:1, 3:2, 16:9, 9:16, 4:3, 3:4, etc.")
    resolution: Optional[str] = Field(default="1024x1024", description="Output resolution (1024x1024 recommended)")
    magic_prompt_option: Optional[str] = Field(default="Auto", description="Magic prompt enhancement: Auto, On, Off")
    style_type: Optional[str] = Field(default=None, description="Optional style preset to apply")

class LumaRayRequest(BaseModel):
    """Request model for Luma Ray 2 I2V animation"""
    prompt: str = Field(description="Animation prompt describing the motion/action")
    image: Optional[str] = Field(default=None, description="Base64 encoded image or image URL (optional)")
    size: Optional[str] = Field(default="1280*720", description="Video resolution (e.g., '1280*720', '1080*1920')")
    duration: Optional[str] = Field(default="5", description="Video duration in seconds")
    also_make_gif: Optional[bool] = Field(default=False, description="Also create a GIF version")
    freeze: Optional[List[str]] = Field(default=None, description="List of objects to keep frozen/static during animation")
    animate: Optional[List[str]] = Field(default=None, description="List of objects to animate (only these will move)")

# ========= LUMA LABS DREAM MACHINE REQUEST MODEL =========
class LumaLabsRequest(BaseModel):
    """Request model for Luma Labs Dream Machine API"""
    prompt: str = Field(description="Animation prompt describing the scene/motion")
    model: Optional[str] = Field(default="ray-2", description="Model to use (default: ray-2)")
    resolution: Optional[str] = Field(default="720p", description="Video resolution: 720p, 1080p")
    duration: Optional[str] = Field(default="5s", description="Video duration: 5s or 10s")
    concepts: Optional[List[str]] = Field(default=None, description="List of concept keys (e.g., ['dolly_zoom', 'pan_left'])")
    image_url: Optional[str] = Field(default=None, description="URL of keyframe image for image-to-video")

# ========= GIF TO SPRITE SHEET REQUEST MODEL =========
class GifToSpriteSheetRequest(BaseModel):
    """Request model for converting GIF to sprite sheet"""
    gif_url: str = Field(description="URL to the GIF file (S3 URL or publicly accessible URL)")
    sheet_type: Optional[str] = Field(default="horizontal", description="Sprite sheet layout: 'horizontal' or 'vertical'")
    job_id: Optional[str] = Field(default=None, description="Job ID to associate with this conversion (optional)")
    max_frames: Optional[int] = Field(default=50, description="Maximum number of frames to process (to prevent memory issues)")
    background_color: Optional[str] = Field(default="transparent", description="Background color: 'transparent', 'white', 'black', or hex color")

# ========= RAG IMAGE GENERATION REQUEST MODEL =========
class RagImageGenRequest(BaseModel):
    """Request model for RAG-based image generation with Gemini"""
    prompt: str = Field(description="Text description for image generation")
    k: Optional[int] = Field(default=3, description="Number of reference images to retrieve from RAG index")
    job_id: Optional[str] = Field(default=None, description="Job ID to associate with this generation (optional)")

# ========= SMART ANIMATE REQUEST MODEL =========
class SmartAnimateRequest(BaseModel):
    """Request model for smart animation with Llama 3.2 Vision object detection"""
    image_url: Optional[str] = Field(default=None, description="URL of the image to animate (if not uploading file)")
    animation_prompt: str = Field(description="What you want to animate in the image (e.g., 'make the character jump', 'move the car forward')")
    resolution: Optional[str] = Field(default="480p", description="Video resolution (480p, 720p, 1080p)")
    seed: Optional[int] = Field(default=42, description="Random seed for reproducible results")
    preserve_background: Optional[bool] = Field(default=True, description="Preserve the background and non-animated elements")
    job_id: Optional[str] = Field(default=None, description="Job ID to associate with this animation (optional)")

# ========= RESPONSE MODELS =========
class S3Assets(BaseModel):
    """Model for S3 asset URLs organized by type"""
    images: Dict[str, str] = Field(default_factory=dict, description="Generated/source images with their S3 URLs")
    models: Dict[str, str] = Field(default_factory=dict, description="3D model files with their S3 URLs") 
    animations: Dict[str, str] = Field(default_factory=dict, description="Animation files with their S3 URLs")
    retextured: Dict[str, str] = Field(default_factory=dict, description="Retextured model files with their S3 URLs")
    intermediate: Dict[str, str] = Field(default_factory=dict, description="Intermediate processing files with their S3 URLs")

class AnimationResult(BaseModel):
    action_id: int
    final_textured_glb: Optional[str]
    raw_anim_glb: Optional[str]
    raw_anim_fbx: Optional[str]
    download_url: Optional[str]
    s3_assets: Optional[S3Assets] = None
    processing_info: Optional[Dict] = None
    job_id: str
    status: str
    progress: int
    message: str
    result: Optional[dict] = None

# ========= JOB TRACKING =========
# Note: Job storage moved to MongoDB database service
# Legacy variable kept for compatibility during transition
jobs = {}  # Will be replaced by database service after initialization

def simple_scan_job_folder(job_id: str) -> dict:
    """Simple job folder scanner for startup loading."""
    job_folder = f"jobs/{job_id}"
    folder_info = {
        "folder_exists": False,
        "files": [],
        "total_files": 0,
        "models": [],
        "images": [],
        "animations": [],
        "metadata_files": []
    }
    
    try:
        if os.path.exists(job_folder):
            folder_info["folder_exists"] = True
            
            # Walk through the job folder
            for root, dirs, files in os.walk(job_folder):
                for file in files:
                    file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(file_path, job_folder)
                    
                    file_info = {
                        "name": file,
                        "path": rel_path
                    }
                    
                    folder_info["files"].append(file_info)
                    
                    # Categorize files by type
                    file_lower = file.lower()
                    if file_lower.endswith(('.glb', '.fbx', '.obj', '.dae', '.blend')):
                        folder_info["models"].append(file_info)
                    elif file_lower.endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp')):
                        folder_info["images"].append(file_info)
                    elif file_lower.endswith(('.json', '.txt', '.log', '.csv')):
                        folder_info["metadata_files"].append(file_info)
                    elif 'anim' in file_lower or 'motion' in file_lower:
                        folder_info["animations"].append(file_info)
            
            folder_info["total_files"] = len(folder_info["files"])
    
    except Exception as e:
        print(f"❌ Error scanning job folder {job_folder}: {e}")
        folder_info["error"] = str(e)
    
    return folder_info

def load_jobs_from_folders():
    """Load existing jobs from job folders on server startup."""
    jobs_dir = "jobs"
    loaded_count = 0
    
    try:
        if not os.path.exists(jobs_dir):
            print("📂 No jobs directory found - starting with empty job tracking")
            return
        
        print(f"🔍 Scanning {jobs_dir} for existing job folders...")
        
        all_folders = os.listdir(jobs_dir)
        print(f"📂 Found {len(all_folders)} items in jobs directory: {all_folders}")
        
        for job_folder in all_folders:
            job_path = os.path.join(jobs_dir, job_folder)
            
            # Skip non-directories
            if not os.path.isdir(job_path):
                print(f"🚫 Skipping non-directory: {job_folder}")
                continue
            
            # Job folder should be named with UUID format
            # Valid UUID formats: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx or 32 hex chars
            is_uuid_format = False
            if len(job_folder) == 36 and job_folder.count('-') == 4:
                # Standard UUID format: 8-4-4-4-12
                parts = job_folder.split('-')
                if (len(parts) == 5 and 
                    len(parts[0]) == 8 and len(parts[1]) == 4 and len(parts[2]) == 4 and 
                    len(parts[3]) == 4 and len(parts[4]) == 12 and
                    all(c in '0123456789abcdefABCDEF-' for c in job_folder)):
                    is_uuid_format = True
            elif len(job_folder) == 32 and all(c in '0123456789abcdefABCDEF' for c in job_folder):
                # 32 hex character format
                is_uuid_format = True
            elif len(job_folder) >= 8 and all(c in '0123456789abcdefABCDEF-' for c in job_folder):
                # Shortened UUID format (at least 8 chars, hex + dashes)
                is_uuid_format = True
            
            if not is_uuid_format:
                print(f"🚫 Skipping non-UUID folder: {job_folder}")
                continue
            
            # Use folder name as job ID
            job_id = job_folder
            print(f"📁 Loading job: {job_id}")
            
            try:
                # Scan job folder for status information
                folder_info = simple_scan_job_folder(job_id)
                
                # Determine job status based on folder contents
                status = "unknown"
                progress = 0
                message = "Job restored from folder"
                result_data = {}
                
                # Check for completion indicators
                if folder_info.get("models") and len(folder_info["models"]) > 0:
                    # Has models - likely completed or at least partially successful
                    if folder_info.get("animations") and len(folder_info["animations"]) > 0:
                        status = "completed"
                        progress = 100
                        message = "Job completed (restored from folder)"
                    else:
                        status = "processing"
                        progress = 75
                        message = "Job partially completed (restored from folder)"
                    
                    # Build result data from folder contents
                    latest_model = folder_info["models"][0] if folder_info["models"] else None
                    if latest_model:
                        model_path = os.path.join(job_path, latest_model["path"])
                        result_data["final_textured_glb"] = model_path
                        result_data["storage_type"] = "legacy_folder_based"
                    
                    if folder_info.get("animations"):
                        animations = []
                        for anim in folder_info["animations"]:
                            anim_path = os.path.join(job_path, anim["path"])
                            animations.append({
                                "action_id": 0,  # Unknown from folder
                                "raw_anim_glb" if anim["name"].endswith(".glb") else "raw_anim_fbx": anim_path
                            })
                        result_data["animations"] = animations
                        result_data["total_animations"] = len(animations)
                
                elif folder_info.get("images") and len(folder_info["images"]) > 0:
                    # Has images but no models - likely failed or in progress
                    status = "failed"
                    progress = 25
                    message = "Job failed or incomplete (restored from folder)"
                    
                    # Add source image if available
                    source_img = folder_info["images"][0] if folder_info["images"] else None
                    if source_img:
                        result_data["source_image"] = os.path.join(job_path, source_img["path"])
                
                else:
                    # Empty or minimal folder
                    status = "failed"
                    progress = 0
                    message = "Job folder exists but appears incomplete"
                
                # Load any saved metadata
                metadata_path = os.path.join(job_path, "job_metadata.json")
                saved_is_infused = False  # Track if this is an infused job
                
                if os.path.exists(metadata_path):
                    try:
                        with open(metadata_path, "r") as f:
                            saved_metadata = json.load(f)
                        
                        # Use saved status if available and more recent
                        if saved_metadata.get("status"):
                            status = saved_metadata["status"]
                        if saved_metadata.get("progress"):
                            progress = saved_metadata["progress"]
                        if saved_metadata.get("message"):
                            message = saved_metadata["message"]
                        if saved_metadata.get("result"):
                            result_data.update(saved_metadata["result"])
                        
                        # Restore isInfused status
                        if saved_metadata.get("isInfused"):
                            saved_is_infused = True
                            
                        print(f"📄 Loaded metadata for job {job_id}: {status} ({progress}%) - isInfused: {saved_is_infused}")
                    except Exception as e:
                        print(f"⚠️ Could not load metadata for job {job_id}: {e}")
                
                # Load 3D model metadata with S3 URLs
                model_metadata_path = os.path.join(job_path, "3d_model_output.json")
                if os.path.exists(model_metadata_path):
                    try:
                        with open(model_metadata_path, "r") as f:
                            model_metadata = json.load(f)
                        
                        # Extract S3 information if available
                        if model_metadata.get("s3_upload"):
                            s3_upload_info = model_metadata["s3_upload"]
                            result_data["s3_upload"] = s3_upload_info
                            result_data["model_s3_url"] = s3_upload_info.get("s3_direct_url")
                            print(f"🔗 Loaded S3 URLs for job {job_id}: {s3_upload_info.get('s3_direct_url', 'N/A')[:50]}...")
                        
                        # Also include model generation info
                        if model_metadata.get("method"):
                            result_data["generation_method"] = model_metadata["method"]
                        if model_metadata.get("params"):
                            result_data["generation_params"] = model_metadata["params"]
                        if model_metadata.get("source_image"):
                            result_data["source_image"] = model_metadata["source_image"]
                            
                    except Exception as e:
                        print(f"⚠️ Could not load 3D model metadata for job {job_id}: {e}")
                
                # Check for Gemini image and ensure s3_gemini_url is available
                s3_gemini_url = None
                
                # First, check if s3_gemini_url is already saved in metadata
                if result_data and result_data.get("s3_gemini_url"):
                    s3_gemini_url = result_data["s3_gemini_url"]
                    print(f"✅ Job {job_id} already has s3_gemini_url: {s3_gemini_url.get('s3_direct_url', 'N/A')[:50]}...")
                else:
                    # Look for Gemini generated images in the job folder
                    gemini_image_candidates = [
                        os.path.join(job_path, "gemini_generated.png"),
                        os.path.join(job_path, "gemini_generated.jpg"),
                        os.path.join(job_path, "generated_image.png"),
                        os.path.join(job_path, "generated_image.jpg"),
                        os.path.join(job_path, "fused_image.png"),
                        os.path.join(job_path, "fused_image.jpg")
                    ]
                    
                    gemini_image_path = None
                    for candidate in gemini_image_candidates:
                        if os.path.exists(candidate):
                            gemini_image_path = candidate
                            break
                    
                    if gemini_image_path:
                        print(f"🎨 Found Gemini image for job {job_id}: {os.path.basename(gemini_image_path)}")
                        print(f"📤 Uploading Gemini image to S3...")
                        
                        # Upload Gemini image to S3
                        s3_gemini_url = upload_gemini_image_to_s3(job_id, gemini_image_path)
                        
                        if s3_gemini_url:
                            # Update result data with s3_gemini_url
                            if not result_data:
                                result_data = {}
                            result_data["s3_gemini_url"] = s3_gemini_url
                            
                            print(f"✅ Gemini image uploaded to S3 for job {job_id}")
                            
                        else:
                            print(f"❌ Failed to upload Gemini image for job {job_id}")
                    else:
                        print(f"⚠️ No Gemini image found for job {job_id}")
                
                # Check for optimized 3D image and ensure s3_optimized_url is available
                s3_optimized_url = None
                
                # First, check if s3_optimized_url is already saved in metadata
                if result_data and result_data.get("s3_optimized_url"):
                    s3_optimized_url = result_data["s3_optimized_url"]
                    print(f"✅ Job {job_id} already has s3_optimized_url: {s3_optimized_url.get('s3_direct_url', 'N/A')[:50]}...")
                else:
                    # Look for optimized 3D images in the job folder
                    optimized_image_candidates = [
                        os.path.join(job_path, "3d_optimized.jpg"),
                        os.path.join(job_path, "3d_optimized.png"),
                        os.path.join(job_path, "optimized_3d.jpg"),
                        os.path.join(job_path, "optimized_3d.png")
                    ]
                    
                    optimized_image_path = None
                    for candidate in optimized_image_candidates:
                        if os.path.exists(candidate):
                            optimized_image_path = candidate
                            break
                    
                    if optimized_image_path:
                        print(f"🎯 Found optimized 3D image for job {job_id}: {os.path.basename(optimized_image_path)}")
                        print(f"📤 Uploading optimized 3D image to S3...")
                        
                        # Upload optimized 3D image to S3
                        s3_optimized_url = upload_optimized_3d_image_to_s3(job_id, optimized_image_path)
                        
                        if s3_optimized_url:
                            # Update result data with s3_optimized_url
                            if not result_data:
                                result_data = {}
                            result_data["s3_optimized_url"] = s3_optimized_url
                            
                            print(f"✅ Optimized 3D image uploaded to S3 for job {job_id}")
                            
                        else:
                            print(f"❌ Failed to upload optimized 3D image for job {job_id}")
                    else:
                        print(f"⚠️ No optimized 3D image found for job {job_id}")
                
                # If we uploaded any images, save the updated metadata
                if s3_gemini_url or s3_optimized_url:
                    try:
                        metadata_path = os.path.join(job_path, "job_metadata.json")
                        metadata_to_save = {
                            "job_id": job_id,
                            "status": status,
                            "progress": progress,
                            "message": message,
                            "result": result_data,
                            "updated_at": time.time(),
                            "s3_images_added": True
                        }
                        
                        with open(metadata_path, "w") as f:
                            json.dump(metadata_to_save, f, indent=2)
                        
                        print(f"💾 Saved updated s3 image URLs to job metadata: {job_id}")
                        
                    except Exception as save_error:
                        print(f"⚠️ Could not save updated metadata for job {job_id}: {save_error}")
                
                # Ensure mandatory fields - set to None if missing
                if not s3_gemini_url:
                    s3_gemini_url = None
                    print(f"⚠️ Job {job_id} missing mandatory s3_gemini_url - will be set to None")
                
                if not s3_optimized_url:
                    s3_optimized_url = None
                    print(f"⚠️ Job {job_id} missing mandatory s3_optimized_url - will be set to None")
                
                # Add mandatory fields to result data
                if result_data is None:
                    result_data = {}
                result_data["s3_gemini_url"] = s3_gemini_url
                result_data["s3_optimized_url"] = s3_optimized_url

                # Detect if this is an infused job from folder contents or metadata
                is_infused_job = saved_is_infused  # From saved metadata
                
                # Also detect fusion jobs from folder contents
                if not is_infused_job:
                    # Check for fusion analysis file
                    fusion_analysis_path = os.path.join(job_path, "fusion_analysis.json")
                    if os.path.exists(fusion_analysis_path):
                        is_infused_job = True
                        print(f"🔀 Detected fusion job from fusion_analysis.json: {job_id}")
                    
                    # Check result data for fusion indicators
                    if result_data and (
                        result_data.get("job_type") == "infusion" or
                        "fusion_style" in result_data or
                        "fusion_data" in result_data or
                        "fused_image_path" in result_data
                    ):
                        is_infused_job = True
                        print(f"🔀 Detected fusion job from result data: {job_id}")

                # Special handling for infused jobs - upload all source images and fused images (only if not already uploaded)
                infused_image_urls = {}
                if is_infused_job:
                    print(f"🔀 Processing infused job images for S3 upload: {job_id}")
                    
                    # Check if images have already been uploaded previously
                    images_already_uploaded = False
                    try:
                        job_dir = f"jobs/{job_id}"
                        metadata_path = os.path.join(job_dir, "job_metadata.json")
                        if os.path.exists(metadata_path):
                            with open(metadata_path, "r") as f:
                                existing_metadata = json.load(f)
                            images_already_uploaded = existing_metadata.get("isImagesUploaded", False)
                    except Exception as e:
                        print(f"⚠️ Could not check isImagesUploaded flag: {e}")
                    
                    if images_already_uploaded:
                        print(f"✅ Images already uploaded for job {job_id} (isImagesUploaded=True), skipping upload process")
                        # Load existing infused image URLs from metadata for display
                        infused_image_urls = load_infused_image_urls_from_metadata(job_id)
                    else:
                        print(f"📤 Images not yet uploaded for job {job_id}, proceeding with upload process")
                        
                        # Load existing infused image URLs from metadata to avoid duplicates
                        infused_image_urls = load_infused_image_urls_from_metadata(job_id)
                        
                        # Upload missing images in the job folder with proper tagging
                        if folder_info.get("images"):
                            for idx, image_info in enumerate(folder_info["images"]):
                                image_path = os.path.join(job_path, image_info["path"])
                                image_filename = image_info["name"].lower()
                                
                                # Determine image type and upload with appropriate tag
                                image_tag = None
                                if "source" in image_filename or "input" in image_filename:
                                    # Check if it's source image 1 or 2
                                    if "1" in image_filename or "first" in image_filename:
                                        image_tag = "source_image_1"
                                    elif "2" in image_filename or "second" in image_filename:
                                        image_tag = "source_image_2"
                                    else:
                                        # Generic source image numbering
                                        source_count = len([k for k in infused_image_urls.keys() if k.startswith("source_image_")])
                                        image_tag = f"source_image_{source_count + 1}"
                                        
                                elif "fused" in image_filename or "fusion" in image_filename or "combined" in image_filename:
                                    if "3d_optimized" in image_filename or "optimized" in image_filename:
                                        image_tag = "fused_image_3d_optimized"
                                    else:
                                        image_tag = "fused_image"
                                elif "gemini" in image_filename or "generated" in image_filename:
                                    image_tag = "gemini_generated"
                                else:
                                    # Default tagging for unidentified images
                                    image_tag = f"infusion_image_{idx + 1}"
                                
                                # Reload infused URLs before each check to get latest state
                                current_infused_urls = load_infused_image_urls_from_metadata(job_id)
                                print(f"🔍 Checking {image_tag} - currently have {len(current_infused_urls)} uploaded images for job {job_id}")
                                if current_infused_urls:
                                    print(f"📋 Existing image tags: {list(current_infused_urls.keys())}")
                                
                                # Only upload if this image tag doesn't exist yet for this specific job
                                if image_tag not in current_infused_urls:
                                    print(f"🖼️ Uploading missing infused job image: {image_filename} as {image_tag}")
                                    
                                    s3_url = upload_infused_image_to_s3(job_id, image_path, image_tag)
                                    if s3_url:
                                        # Update both local and current URLs
                                        infused_image_urls[image_tag] = s3_url
                                        current_infused_urls[image_tag] = s3_url
                                        print(f"✅ Uploaded {image_tag}: {s3_url.get('s3_direct_url', 'N/A')[:50]}...")
                                        
                                        # Immediately save to metadata after successful upload to prevent re-upload
                                        try:
                                            # Update result data with new upload
                                            if not result_data:
                                                result_data = {}
                                            result_data["infused_image_urls"] = current_infused_urls
                                            result_data["total_infused_images"] = len(current_infused_urls)
                                            
                                            # Create temporary job entry to save metadata
                                            temp_job = {
                                                "job_id": job_id,
                                                "status": status,
                                                "progress": progress,
                                                "message": message,
                                                "result": result_data,
                                                "updated_at": time.time(),
                                                "isInfused": True
                                            }
                                            jobs[job_id] = temp_job
                                            
                                            # Save metadata immediately to prevent duplicate uploads
                                            save_job_metadata(job_id)
                                            print(f"💾 Saved metadata after uploading {image_tag} for job {job_id}")
                                            
                                        except Exception as metadata_error:
                                            print(f"⚠️ Failed to save metadata after uploading {image_tag}: {metadata_error}")
                                    else:
                                        print(f"❌ Failed to upload {image_tag}")
                                else:
                                    print(f"✅ {image_tag} already uploaded for job {job_id}, skipping")
                        
                        # After completing all uploads for this job, set the isImagesUploaded flag
                        if folder_info.get("images"):
                            print(f"🏁 Completed image upload process for job {job_id}, marking as uploaded")
                            # Mark images as uploaded to prevent future re-uploads
                            if not result_data:
                                result_data = {}
                            result_data["isImagesUploaded"] = True
                            
                            # Create temporary job entry with the flag
                            temp_job = {
                                "job_id": job_id,
                                "status": status,
                                "progress": progress,
                                "message": message,
                                "result": result_data,
                                "updated_at": time.time(),
                                "isInfused": True,
                                "isImagesUploaded": True  # Add to job level as well
                            }
                            jobs[job_id] = temp_job
                            
                            # Save metadata with the flag to prevent future uploads
                            save_job_metadata(job_id)
                            print(f"✅ Marked job {job_id} as isImagesUploaded=True")
                    
                    # Use the final state of infused URLs
                    final_infused_urls = load_infused_image_urls_from_metadata(job_id)
                    
                    # Store infused image URLs in result data
                    if final_infused_urls:
                        if not result_data:
                            result_data = {}
                        result_data["infused_image_urls"] = final_infused_urls
                        result_data["total_infused_images"] = len(final_infused_urls)
                        print(f"🔗 Total infused images for job {job_id}: {len(final_infused_urls)}")

                # Check for Luma Ray animation assets and upload to S3 if needed
                signed_gif_url = None
                signed_input_image = None
                
                # Check if this is a Luma Ray job (animate type)
                job_type = result_data.get("job_type") if result_data else None
                animation_type = result_data.get("animation_type") if result_data else None
                
                if job_type == "animate" and animation_type == "luma_ray_design":
                    print(f"🎬 Found Luma Ray animation job {job_id}, checking for GIF and input image...")
                    
                    # Look for GIF file
                    gif_candidates = [
                        os.path.join(job_path, f"luma_ray_animation_{job_id}.gif"),
                        os.path.join(job_path, "luma_ray_animation.gif"),
                        os.path.join(job_path, "animation.gif")
                    ]
                    
                    gif_path = None
                    for candidate in gif_candidates:
                        if os.path.exists(candidate):
                            gif_path = candidate
                            break
                    
                    if gif_path:
                        print(f"🎬 Found Luma Ray GIF: {os.path.basename(gif_path)}")
                        signed_gif_url = upload_luma_gif_to_s3(job_id, gif_path)
                        
                        if signed_gif_url:
                            print(f"✅ Luma Ray GIF uploaded to S3: {signed_gif_url.get('s3_direct_url', 'N/A')[:50]}...")
                        else:
                            print(f"❌ Failed to upload Luma Ray GIF for job {job_id}")
                    else:
                        print(f"⚠️ No Luma Ray GIF found for job {job_id}")
                    
                    # Look for input image
                    input_image_candidates = [
                        os.path.join(job_path, "input_image.jpg"),
                        os.path.join(job_path, "input_image.png"),
                        os.path.join(job_path, "input_image.jpeg")
                    ]
                    
                    input_image_path = None
                    for candidate in input_image_candidates:
                        if os.path.exists(candidate):
                            input_image_path = candidate
                            break
                    
                    if input_image_path:
                        print(f"📷 Found input image: {os.path.basename(input_image_path)}")
                        signed_input_image = upload_input_image_to_s3(job_id, input_image_path)
                        
                        if signed_input_image:
                            print(f"✅ Input image uploaded to S3: {signed_input_image.get('s3_direct_url', 'N/A')[:50]}...")
                        else:
                            print(f"❌ Failed to upload input image for job {job_id}")
                    else:
                        print(f"⚠️ No input image found for job {job_id}")
                    
                    # Update result data with Luma Ray assets
                    if signed_gif_url or signed_input_image:
                        if not result_data:
                            result_data = {}
                        
                        if signed_gif_url:
                            result_data["signed_gif_url"] = signed_gif_url
                        
                        if signed_input_image:
                            result_data["signed_input_image"] = signed_input_image
                        
                        # Save updated metadata
                        try:
                            metadata_path = os.path.join(job_path, "job_metadata.json")
                            metadata_to_save = {
                                "job_id": job_id,
                                "status": status,
                                "progress": progress,
                                "message": message,
                                "result": result_data,
                                "updated_at": time.time(),
                                "luma_assets_uploaded": True
                            }
                            
                            with open(metadata_path, "w") as f:
                                json.dump(metadata_to_save, f, indent=2)
                            
                            print(f"💾 Saved Luma Ray assets to job metadata: {job_id}")
                            
                        except Exception as save_error:
                            print(f"⚠️ Could not save Luma Ray asset metadata for job {job_id}: {save_error}")

                # Create job entry
                jobs[job_id] = {
                    "job_id": job_id,
                    "status": status,
                    "progress": progress,
                    "message": message,
                    "result": result_data if result_data else None,
                    "updated_at": time.time(),
                    "restored_from_folder": True,
                    "folder_path": job_path,
                    "s3_gemini_url": s3_gemini_url,  # Mandatory field
                    "s3_optimized_url": s3_optimized_url,  # Mandatory field
                    "isInfused": is_infused_job,  # Mark as true if this is a fusion job
                    "signed_gif_url": signed_gif_url if 'signed_gif_url' in locals() else None,  # Luma Ray GIF
                    "signed_input_image": signed_input_image if 'signed_input_image' in locals() else None,  # Input image
                    "assets_summary": {
                        "models_count": len(folder_info.get("models", [])),
                        "images_count": len(folder_info.get("images", [])),
                        "animations_count": len(folder_info.get("animations", [])),
                        "total_files": folder_info.get("total_files", 0),
                        "total_size_mb": folder_info.get("total_size_mb", 0)
                    }
                }
                
                loaded_count += 1
                print(f"✅ Job {job_id} loaded: {status} ({progress}%) - {folder_info.get('total_files', 0)} files")
                
            except Exception as e:
                print(f"❌ Error loading job {job_id}: {e}")
                continue
        
        print(f"🎉 Loaded {loaded_count} jobs from folders into memory")
        
        # Summary of loaded jobs by status and infusion status
        status_summary = {}
        infusion_summary = {"total_infused": 0, "total_regular": 0}
        
        for job in jobs.values():
            status = job.get("status", "unknown")
            status_summary[status] = status_summary.get(status, 0) + 1
            
            # Count infusion jobs
            if job.get("isInfused", False):
                infusion_summary["total_infused"] += 1
            else:
                infusion_summary["total_regular"] += 1
        
        print(f"📊 Job status summary: {dict(status_summary)}")
        print(f"🔀 Infusion summary: {infusion_summary['total_infused']} fusion jobs, {infusion_summary['total_regular']} regular jobs")
        
    except Exception as e:
        print(f"❌ Error loading jobs from folders: {e}")

def save_job_metadata(job_id: str):
    """Save job metadata to disk for persistence."""
    try:
        if job_id not in jobs:
            return
        
        job_data = jobs[job_id]
        job_dir = f"jobs/{job_id}"
        
        if not os.path.exists(job_dir):
            os.makedirs(job_dir, exist_ok=True)
        
        metadata_path = os.path.join(job_dir, "job_metadata.json")
        
        # Create metadata to save
        metadata = {
            "job_id": job_id,
            "status": job_data.get("status"),
            "progress": job_data.get("progress"),
            "message": job_data.get("message"),
            "result": job_data.get("result"),
            "updated_at": job_data.get("updated_at"),
            "s3_gemini_url": job_data.get("s3_gemini_url"),  # Include mandatory s3_gemini_url
            "s3_optimized_url": job_data.get("s3_optimized_url"),  # Include mandatory s3_optimized_url
            "isInfused": job_data.get("isInfused", False),  # Include isInfused status
            "isImagesUploaded": job_data.get("isImagesUploaded", False) or (job_data.get("result", {}).get("isImagesUploaded", False)),  # Include image upload flag from job or result
            "saved_at": time.time()
        }
        
        # Include infused image URLs if this is an infused job
        if job_data.get("isInfused") and job_data.get("result"):
            result_data = job_data.get("result", {})
            if result_data.get("infused_image_urls"):
                # Save infused URLs in a separate fusion_urls section for better organization
                metadata["fusion_urls"] = {
                    "infused_image_urls": result_data["infused_image_urls"],
                    "total_infused_images": result_data.get("total_infused_images", 0),
                    "saved_timestamp": time.time()
                }
                print(f"💾 Saving {len(result_data['infused_image_urls'])} infused image URLs in fusion_urls section for job {job_id}")
        
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)
            
        print(f"💾 Saved metadata for job {job_id}")
        
    except Exception as e:
        print(f"❌ Error saving metadata for job {job_id}: {e}")

def load_job_metadata(job_id: str) -> dict:
    """Load job metadata from disk.
    
    Args:
        job_id: Job identifier
        
    Returns:
        Dictionary containing job metadata, or empty dict if not found
    """
    try:
        job_dir = f"jobs/{job_id}"
        metadata_path = os.path.join(job_dir, "job_metadata.json")
        
        if not os.path.exists(metadata_path):
            return {}
        
        with open(metadata_path, "r") as f:
            metadata = json.load(f)
            
        print(f"📁 Loaded metadata for job {job_id}")
        return metadata
        
    except Exception as e:
        print(f"❌ Error loading metadata for job {job_id}: {e}")
        return {}

def is_url_for_current_job(url_data: dict, job_id: str) -> bool:
    """Check if an existing S3 URL belongs to the current job by examining the URL structure.
    
    Args:
        url_data: Dictionary containing S3 URL information
        job_id: Current job identifier
        
    Returns:
        True if the URL belongs to the current job, False otherwise
    """
    try:
        if not isinstance(url_data, dict):
            return False
        
        # Check both presigned_url and s3_direct_url for job_id
        for url_key in ['presigned_url', 's3_direct_url', 's3_key']:
            url = url_data.get(url_key, '')
            if url and job_id in str(url):
                return True
        
        # Also check metadata if available
        if url_data.get('job_id') == job_id:
            return True
            
        return False
        
    except Exception as e:
        print(f"❌ Error checking URL ownership: {e}")
        return False

def load_infused_image_urls_from_metadata(job_id: str) -> dict:
    """Load existing infused image URLs from job metadata to avoid duplicate uploads.
    Only returns URLs that actually belong to the current job.
    
    Args:
        job_id: Job identifier
        
    Returns:
        Dictionary of existing infused image URLs for this job or empty dict if none found
    """
    try:
        job_dir = f"jobs/{job_id}"
        metadata_path = os.path.join(job_dir, "job_metadata.json")
        valid_urls = {}
        
        if os.path.exists(metadata_path):
            with open(metadata_path, "r") as f:
                metadata = json.load(f)
            
            # Check new fusion_urls section first
            if metadata.get("fusion_urls") and metadata["fusion_urls"].get("infused_image_urls"):
                candidate_urls = metadata["fusion_urls"]["infused_image_urls"]
                
                # Filter URLs to only include ones that belong to this job
                for image_tag, url_data in candidate_urls.items():
                    if is_url_for_current_job(url_data, job_id):
                        valid_urls[image_tag] = url_data
                        print(f"✅ Found valid {image_tag} URL for job {job_id}")
                    else:
                        print(f"🔍 Skipping {image_tag} URL - belongs to different job")
                
                if valid_urls:
                    print(f"📂 Loaded {len(valid_urls)} valid infused image URLs from fusion_urls section for job {job_id}")
                    return valid_urls
            
            # Fallback to old location for backward compatibility
            candidate_urls = metadata.get("infused_image_urls", {})
            if candidate_urls:
                # Filter URLs to only include ones that belong to this job
                for image_tag, url_data in candidate_urls.items():
                    if is_url_for_current_job(url_data, job_id):
                        valid_urls[image_tag] = url_data
                        print(f"✅ Found valid {image_tag} URL for job {job_id} (legacy location)")
                    else:
                        print(f"🔍 Skipping {image_tag} URL - belongs to different job (legacy)")
                
                if valid_urls:
                    print(f"📂 Loaded {len(valid_urls)} valid infused image URLs from legacy location for job {job_id}")
                    return valid_urls
        
        # Also check job memory state
        if job_id in jobs:
            job_data = jobs[job_id]
            result_data = job_data.get("result", {})
            candidate_urls = result_data.get("infused_image_urls", {})
            
            if candidate_urls:
                # Filter URLs to only include ones that belong to this job
                for image_tag, url_data in candidate_urls.items():
                    if is_url_for_current_job(url_data, job_id):
                        valid_urls[image_tag] = url_data
                        print(f"✅ Found valid {image_tag} URL for job {job_id} (memory)")
                    else:
                        print(f"🔍 Skipping {image_tag} URL - belongs to different job (memory)")
                
                if valid_urls:
                    print(f"🧠 Found {len(valid_urls)} valid infused image URLs in memory for job {job_id}")
                    return valid_urls
        
        print(f"🔍 No existing infused image URLs found for job {job_id}")
        return {}
        
    except Exception as e:
        print(f"❌ Error loading infused image URLs for job {job_id}: {e}")
        return {}

def update_3d_model_metadata(job_id: str, s3_upload_info: dict):
    """Update the 3d_model_output.json file with S3 URLs after upload."""
    try:
        if not job_id:
            return
        
        metadata_path = f"jobs/{job_id}/3d_model_output.json"
        
        if os.path.exists(metadata_path):
            # Load existing metadata
            with open(metadata_path, "r") as f:
                metadata = json.load(f)
            
            # Add S3 information
            metadata["s3_upload"] = {
                "presigned_url": s3_upload_info.get("presigned_url"),
                "s3_direct_url": s3_upload_info.get("s3_url"),
                "s3_key": s3_upload_info.get("s3_key"),
                "upload_timestamp": s3_upload_info.get("upload_timestamp"),
                "uploaded_at": time.time()
            }
            
            # Save updated metadata
            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)
            
            print(f"✅ Updated 3d_model_output.json with S3 URLs for job {job_id}")
            
        else:
            print(f"⚠️ 3d_model_output.json not found for job {job_id} at {metadata_path}")
            
    except Exception as e:
        print(f"❌ Error updating 3d_model_output.json for job {job_id}: {e}")

# Create thread pool for heavy CPU/IO operations
thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="3d_generation")

# Initialize database service (non-blocking)
print("🔌 Attempting to initialize database service...")
try:
    db_initialized = initialize_database()
    if db_initialized:
        print("✅ Database service initialized successfully")
        # Keep jobs dict as fallback, but prefer database
        jobs = {}  # Maintain fallback capability
        use_database = True
    else:
        raise Exception("Database initialization returned False")
except Exception as e:
    print(f"⚠️ Database initialization failed: {e}")
    print("🔄 Continuing with in-memory storage - server will start regardless")
    jobs = {}  # Fallback to in-memory storage
    use_database = False

def update_job_status(job_id: str, status: str, progress: int = 0, message: str = "", result: dict = None):
    """Update job status using database service or fallback to memory."""
    global use_database, jobs
    
    if use_database:
        # Use database service
        try:
            update_job_status_db(job_id, status, progress, message, result)
            
            # Send WebSocket notification after successful database update (safe for sync/async contexts)
            try:
                loop = asyncio.get_running_loop()
                asyncio.create_task(send_job_update_notification(job_id, status, progress, message, result))
            except RuntimeError:
                # No event loop - skip WebSocket notification in sync contexts
                pass
            
            return
        except Exception as e:
            print(f"❌ Database update failed for job {job_id}: {e}")
            print("🔄 Falling back to in-memory storage for this operation")
    
    # Fallback to in-memory storage (original implementation)
    with threading.Lock():
        # Get existing job status or create new one
        existing_job = jobs.get(job_id, {})
        existing_result = existing_job.get("result", {})
        
        # Merge results if both exist
        merged_result = existing_result.copy() if existing_result else {}
        if result:
            merged_result.update(result)
        
        # Determine if this is an infused job
        is_infused = False
        if merged_result:
            # Check if this is a fusion/infusion job
            if (merged_result.get("job_type") == "infusion" or 
                "fusion_style" in merged_result or 
                "fusion_data" in merged_result or
                "fused_image_path" in merged_result):
                is_infused = True
        
        # Also preserve existing isInfused status if already set
        if existing_job.get("isInfused"):
            is_infused = True
        
        # Create basic job status
        job_status = {
            "job_id": job_id,
            "status": status,
            "progress": progress,
            "message": message,
            "result": merged_result,
            "isInfused": is_infused,
            "created_at": existing_job.get("created_at", time.time()),
            "updated_at": time.time()
        }
        
        jobs[job_id] = job_status
        
        # Send WebSocket notification for in-memory updates (safe for sync/async contexts)
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(send_job_update_notification(job_id, status, progress, message, merged_result))
        except RuntimeError:
            # No event loop - skip WebSocket notification in sync contexts
            pass
        
        # Save job metadata to disk
        try:
            save_job_metadata(job_id)
        except Exception as e:
            print(f"❌ Failed to save metadata for job {job_id}: {e}")

async def send_job_update_notification(job_id: str, status: str, progress: int, message: str, result: dict = None):
    """Send WebSocket notification for job update."""
    try:
        update_data = {
            "type": "job_update",
            "job_id": job_id,
            "status": status,
            "progress": progress,
            "message": message,
            "result": result or {},
            "timestamp": time.time()
        }
        
        await manager.send_job_update(job_id, update_data)
        print(f"📡 Sent WebSocket update for job {job_id}: {status} ({progress}%)")
    except Exception as e:
        # Don't fail the job update if WebSocket fails
        print(f"⚠️ Failed to send WebSocket notification for job {job_id}: {e}")

def get_job_status(job_id: str) -> Optional[dict]:
    """Get job status from database or memory fallback."""
    global use_database, jobs
    
    if use_database:
        try:
            return get_job_db(job_id)
        except Exception as e:
            print(f"❌ Database get failed for job {job_id}: {e}")
    
    # Fallback to memory
    return jobs.get(job_id)

def get_all_jobs() -> dict:
    """Get all jobs from database or memory fallback."""
    global use_database, jobs
    
    if use_database:
        try:
            return get_all_jobs_db()
        except Exception as e:
            print(f"❌ Database get all jobs failed: {e}")
    
    # Fallback to memory
    return jobs.copy()

def delete_job_status(job_id: str) -> bool:
    """Delete job from database or memory fallback."""
    global use_database, jobs
    
    if use_database:
        try:
            return delete_job_db(job_id)
        except Exception as e:
            print(f"❌ Database delete failed for job {job_id}: {e}")
    
    # Fallback to memory
    if job_id in jobs:
        del jobs[job_id]
        return True
    return False

def job_exists(job_id: str) -> bool:
    """Check if job exists in database or memory fallback."""
    global use_database, jobs
    
    if use_database:
        try:
            db = get_db_service()
            return db.job_exists(job_id)
        except Exception as e:
            print(f"❌ Database exists check failed for job {job_id}: {e}")
    
    # Fallback to memory
    return job_id in jobs

# Legacy compatibility functions that maintain the same interface

def create_presigned_download_url(file_path: str, filename: str) -> str:
    """Create presigned URL for downloading result files."""
    try:
        s3 = s3_client()
        key = f"results/{uuid.uuid4()}_{filename}"
        
        # Upload file to S3 with proper error handling
        with open(file_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET,
                key,
                ExtraArgs={"ContentType": "model/gltf-binary"}
            )
        
        # Generate presigned URL (valid for 24 hours)
        url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=24 * 3600  # 24 hours
        )
        return url
    except Exception as e:
        print(f"Error creating download URL: {e}")
        return None

def upload_glb_to_s3_with_urls(file_path: str, filename: str) -> dict:
    """Upload GLB to S3 and return both presigned URL and direct S3 URL."""
    try:
        s3 = s3_client()
        key = f"meshy_tmp/{filename}"
        
        print(f"📤 Uploading GLB to S3: {file_path} -> {key}")
        
        # Upload file to S3 with proper error handling
        with open(file_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET,
                key,
                ExtraArgs={"ContentType": "model/gltf-binary"}
            )
        
        # Generate presigned URL (valid for 24 hours)
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=24 * 3600  # 24 hours
        )
        
        # Generate direct S3 URL
        # Get region from environment or default
        s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
        s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{key}"
        
        print(f"✅ GLB uploaded successfully to S3")
        print(f"📍 Direct S3 URL: {s3_direct_url}")
        
        return {
            "presigned_url": presigned_url,
            "s3_url": s3_direct_url,
            "s3_key": key,
            "upload_timestamp": time.time()
        }
        
    except Exception as e:
        print(f"❌ Error uploading GLB to S3: {e}")
        return None

# ========= RAG HELPER FUNCTIONS =========================================

def lazy_load_clip_model():
    """Safely load CLIP model on-demand to prevent startup crashes."""
    global clip_model, clip_preprocess
    
    if not RAG_DEPENDENCIES_AVAILABLE:
        print("⚠️ RAG dependencies not available - cannot load CLIP model")
        return False
    
    if clip_model is not None:
        return True  # Already loaded
    
    try:
        print(f"🔄 Lazy loading CLIP model {CLIP_MODEL} on {DEVICE}...")
        
        # Set multiprocessing start method to avoid segmentation faults on macOS
        try:
            import multiprocessing
            if multiprocessing.get_start_method(allow_none=True) != 'spawn':
                multiprocessing.set_start_method('spawn', force=True)
        except Exception as mp_e:
            print(f"⚠️ Could not set multiprocessing method: {mp_e}")
        
        # Try loading CLIP with reduced precision to avoid memory issues
        with torch.no_grad():
            clip_model, _, clip_preprocess = open_clip.create_model_and_transforms(
                CLIP_MODEL, 
                pretrained=CLIP_PRETRAIN, 
                device=DEVICE
            )
            clip_model.eval()
            
            # Test the model with a dummy input to ensure it works
            test_input = ["test prompt"]
            tokenizer = open_clip.get_tokenizer(CLIP_MODEL)
            test_tokens = tokenizer(test_input).to(DEVICE)
            test_features = clip_model.encode_text(test_tokens)
            
            print(f"✅ CLIP model lazy loaded and tested successfully on {DEVICE}")
            return True
            
    except Exception as e:
        print(f"❌ Failed to lazy load CLIP model: {e}")
        print(f"🔄 RAG image search will remain disabled")
        clip_model = None
        clip_preprocess = None
        return False

def load_img_store():
    """Load the FAISS image index and metadata for RAG queries."""
    try:
        # Check if FAISS is available
        if not FAISS_AVAILABLE or faiss is None:
            print("⚠️ FAISS not available - cannot load image index")
            return None, []
        
        faiss_path = f"{RAG_IMAGE_INDEX_PATH}.faiss"
        meta_path = f"{RAG_IMAGE_INDEX_PATH}.meta.jsonl"
        
        if not os.path.exists(faiss_path) or not os.path.exists(meta_path):
            raise FileNotFoundError(f"RAG index not found at {RAG_IMAGE_INDEX_PATH}")
        
        index = faiss.read_index(faiss_path)
        metas = []
        with open(meta_path, "r", encoding="utf-8") as f:
            for line in f:
                metas.append(json.loads(line.strip()))
        
        print(f"✅ Loaded RAG index with {len(metas)} images")
        return index, metas
        
    except Exception as e:
        print(f"❌ Error loading RAG index: {e}")
        return None, []

def embed_texts_clip(texts: List[str]) -> np.ndarray:
    """Embed text queries using CLIP for RAG search."""
    try:
        # Try to lazy load CLIP model if not already loaded
        if not clip_model and not lazy_load_clip_model():
            print("❌ CLIP model not available and lazy loading failed")
            return np.zeros((len(texts), 512), dtype=np.float32)
        
        # Check if torch is available
        try:
            import torch
            import open_clip
        except ImportError:
            print("❌ torch/open_clip not available for text embedding")
            return np.zeros((len(texts), 512), dtype=np.float32)
        
        # Use torch.no_grad() context manager instead of decorator
        with torch.no_grad():
            tokenizer = open_clip.get_tokenizer(CLIP_MODEL)
            text_tokens = tokenizer(texts).to(DEVICE)
            text_features = clip_model.encode_text(text_tokens)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
            return text_features.detach().cpu().numpy().astype("float32")
        
    except Exception as e:
        print(f"❌ Error embedding text with CLIP: {e}")
        return np.zeros((len(texts), 512), dtype=np.float32)

def upload_asset_to_s3(file_path: str, asset_type: str, job_id: str, asset_name: str = None) -> Optional[str]:
    """Upload any file asset to S3 and return presigned URL.
    
    Args:
        file_path: Local path to file
        asset_type: Type of asset (image, model, animation, etc.)
        job_id: Job ID for organization
        asset_name: Custom asset name, defaults to original filename
        
    Returns:
        Presigned URL or None if failed
    """
    try:
        if not os.path.exists(file_path):
            print(f"⚠️ Asset file not found: {file_path}")
            return None
            
        s3 = s3_client()
        
        # Generate asset name
        if not asset_name:
            asset_name = os.path.basename(file_path)
        
        # Create organized S3 key
        timestamp = int(time.time())
        key = f"assets/{job_id}/{asset_type}/{timestamp}_{asset_name}"
        
        # Determine content type
        content_type = "application/octet-stream"
        file_ext = os.path.splitext(file_path)[1].lower()
        
        content_type_map = {
            '.glb': 'model/gltf-binary',
            '.fbx': 'application/octet-stream',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg', 
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.json': 'application/json',
            '.txt': 'text/plain'
        }
        
        if file_ext in content_type_map:
            content_type = content_type_map[file_ext]
        
        # Upload to S3
        print(f"📤 Uploading {asset_type} asset: {os.path.basename(file_path)} -> s3://{S3_BUCKET}/{key}")
        
        with open(file_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET, 
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {
                        "job_id": job_id,
                        "asset_type": asset_type,
                        "upload_time": str(timestamp)
                    }
                }
            )
        
        # Generate presigned URL (valid for 7 days)
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=7 * 24 * 3600  # 7 days
        )
        
        print(f"✅ Asset uploaded successfully: {key}")
        return presigned_url
        
    except Exception as e:
        print(f"❌ Error uploading asset {file_path}: {e}")
        return None

def upload_optimized_3d_image_to_s3(job_id: str, optimized_image_path: str) -> Optional[str]:
    """Upload optimized 3D image to S3 and return presigned URL.
    
    Args:
        job_id: Job identifier
        optimized_image_path: Local path to optimized 3D image
        
    Returns:
        Presigned URL or None if failed
    """
    try:
        if not os.path.exists(optimized_image_path):
            print(f"⚠️ Optimized 3D image not found: {optimized_image_path}")
            return None
            
        s3 = s3_client()
        
        # Create organized S3 key for optimized 3D image
        timestamp = int(time.time())
        filename = os.path.basename(optimized_image_path)
        key = f"assets/{job_id}/optimized_3d_images/{timestamp}_{filename}"
        
        # Determine content type
        file_ext = os.path.splitext(optimized_image_path)[1].lower()
        content_type = "image/jpeg" if file_ext in ['.jpg', '.jpeg'] else "image/png"
        
        # Upload to S3
        print(f"🎯 Uploading optimized 3D image: {filename} -> s3://{S3_BUCKET}/{key}")
        
        with open(optimized_image_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET, 
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {
                        "job_id": job_id,
                        "asset_type": "optimized_3d_image",
                        "upload_time": str(timestamp),
                        "purpose": "3d-processing"
                    }
                }
            )
        
        # Generate presigned URL (valid for 7 days)
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=7 * 24 * 3600  # 7 days
        )
        
        # Also generate direct S3 URL
        s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
        s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{key}"
        
        print(f"✅ Optimized 3D image uploaded successfully: {key}")
        print(f"🔗 Direct URL: {s3_direct_url}")
        
        return {
            "presigned_url": presigned_url,
            "s3_direct_url": s3_direct_url,
            "s3_key": key,
            "upload_timestamp": timestamp
        }
        
    except Exception as e:
        print(f"❌ Error uploading optimized 3D image {optimized_image_path}: {e}")
        return None

def analyze_image_for_fusion(image_path: str) -> str:
    """Analyze image using vision AI to describe it for fusion purposes.
    
    Args:
        image_path: Path to image file
        
    Returns:
        Detailed description of the image for fusion prompting
    """
    try:
        if not os.path.exists(image_path):
            print(f"⚠️ Image not found for analysis: {image_path}")
            return "Unknown image - file not found"
        
        if not mapapi:
            print("⚠️ mapapi not available for image analysis")
            return "Image analysis not available"
        
        # Use Llama 3.2 Vision for detailed analysis
        print(f"🔍 Analyzing image for fusion: {os.path.basename(image_path)}")
        
        # Enhanced prompt for fusion-specific analysis
        fusion_analysis_prompt = (
            "Analyze this image in detail for 3D model fusion purposes. Describe: "
            "1) Main objects and their shapes, materials, textures "
            "2) Colors, lighting, and style characteristics "
            "3) Geometric features and proportions "
            "4) Distinctive visual elements that could be fused with other objects "
            "5) Overall aesthetic and design style. "
            "Be specific about physical properties and visual characteristics that would work well in 3D model fusion."
        )
        
        # Store original environment and set fusion analysis prompt
        original_prompt = os.environ.get("LLAMA_VISION_PROMPT")
        os.environ["LLAMA_VISION_PROMPT"] = fusion_analysis_prompt
        
        try:
            analysis_result = mapapi.analyze_image_with_llama(image_path)
            print(f"✅ Image analysis completed: {analysis_result[:100]}...")
            return analysis_result
        finally:
            # Restore original prompt
            if original_prompt:
                os.environ["LLAMA_VISION_PROMPT"] = original_prompt
            elif "LLAMA_VISION_PROMPT" in os.environ:
                del os.environ["LLAMA_VISION_PROMPT"]
        
    except Exception as e:
        print(f"❌ Error analyzing image {image_path}: {e}")
        return f"Error analyzing image: {str(e)}"

def generate_fusion_prompt_with_ollama(analysis1: str, analysis2: str, fusion_style: str = "blend", fusion_strength: float = 0.5) -> str:
    """Generate a fusion prompt using Ollama/GPT to combine two image analyses.
    
    Args:
        analysis1: Analysis of first image
        analysis2: Analysis of second image  
        fusion_style: Style of fusion ("blend", "merge", "hybrid")
        fusion_strength: Balance between images (0.0 to 1.0)
        
    Returns:
        Generated fusion prompt for image generation
    """
    try:
        print(f"🧠 Generating fusion prompt with style: {fusion_style}, strength: {fusion_strength}")
        
        # Create fusion instruction based on style and strength
        if fusion_style == "blend":
            fusion_instruction = f"seamlessly blend and combine the visual elements, with {int(fusion_strength * 100)}% emphasis on the second image characteristics"
        elif fusion_style == "merge":
            fusion_instruction = f"merge the geometric and structural elements while maintaining distinct features from both, balanced at {int(fusion_strength * 100)}%"
        elif fusion_style == "hybrid":
            fusion_instruction = f"create a hybrid that takes the best features from both, with {int(fusion_strength * 100)}% influence from the second image"
        else:
            fusion_instruction = "creatively combine and fuse the elements together"
        
        # Create comprehensive fusion prompt
        system_prompt = (
            "You are an expert 3D artist and prompt engineer. Your task is to create a detailed, "
            "vivid prompt for generating a new 3D-optimized image that fuses two described objects together. "
            "Focus on creating something visually coherent, 3D-friendly, and suitable for model generation."
        )
        
        user_prompt = f"""
Based on these two image analyses, create a detailed fusion prompt for generating a new 3D model:

IMAGE 1 ANALYSIS:
{analysis1}

IMAGE 2 ANALYSIS: 
{analysis2}

FUSION INSTRUCTIONS:
{fusion_instruction}

Generate a detailed prompt that describes a new object/scene that {fusion_instruction}. 
The prompt should:
1. Create a cohesive, imaginative fusion of both images
2. Be optimized for 3D model generation (clear geometry, good proportions)
3. Include specific details about materials, textures, colors, and lighting
4. Specify a clean background suitable for 3D modeling
5. Result in something visually striking and technically feasible

Write only the fusion prompt, nothing else:
"""
        
        # Try to use Ollama API if available, otherwise use a fallback method
        try:
            # Check if ollama is available via environment or direct API call
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            ollama_model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
            
            import requests
            
            ollama_payload = {
                "model": ollama_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "stream": False
            }
            
            print(f"🤖 Requesting fusion prompt from Ollama ({ollama_model})...")
            response = requests.post(f"{ollama_host}/api/chat", json=ollama_payload, timeout=60)
            
            if response.status_code == 200:
                ollama_result = response.json()
                fusion_prompt = ollama_result["message"]["content"].strip()
                print(f"✅ Fusion prompt generated via Ollama: {fusion_prompt[:100]}...")
                return fusion_prompt
            else:
                print(f"⚠️ Ollama request failed: {response.status_code}")
                raise Exception(f"Ollama API error: {response.status_code}")
        
        except Exception as ollama_error:
            print(f"⚠️ Ollama not available ({ollama_error}), using fallback fusion method...")
            
            # Fallback: Create fusion prompt using template-based approach
            return create_fallback_fusion_prompt(analysis1, analysis2, fusion_style, fusion_strength)
        
    except Exception as e:
        print(f"❌ Error generating fusion prompt: {e}")
        return create_fallback_fusion_prompt(analysis1, analysis2, fusion_style, fusion_strength)

def create_fallback_fusion_prompt(analysis1: str, analysis2: str, fusion_style: str, fusion_strength: float) -> str:
    """Fallback method to create fusion prompt using template-based approach."""
    try:
        # Extract key elements from analyses using simple text processing
        def extract_key_elements(analysis):
            elements = {
                "objects": [],
                "colors": [],
                "materials": [],
                "shapes": []
            }
            
            # Simple keyword extraction
            words = analysis.lower().split()
            
            # Common object keywords
            object_keywords = ["car", "building", "tree", "person", "animal", "furniture", "vehicle", "structure"]
            for keyword in object_keywords:
                if keyword in words:
                    elements["objects"].append(keyword)
            
            # Color keywords
            color_keywords = ["red", "blue", "green", "yellow", "black", "white", "brown", "gray", "orange", "purple"]
            for color in color_keywords:
                if color in words:
                    elements["colors"].append(color)
            
            # Material keywords  
            material_keywords = ["metal", "wood", "stone", "glass", "fabric", "plastic", "concrete", "leather"]
            for material in material_keywords:
                if material in words:
                    elements["materials"].append(material)
            
            return elements
        
        elements1 = extract_key_elements(analysis1)
        elements2 = extract_key_elements(analysis2)
        
        # Create fusion based on style
        if fusion_style == "blend":
            fusion_objects = elements1["objects"][:1] + elements2["objects"][:1]
            fusion_colors = elements1["colors"][:2] + elements2["colors"][:2]
            fusion_materials = elements1["materials"][:1] + elements2["materials"][:1]
        elif fusion_style == "merge":
            fusion_objects = elements1["objects"] + elements2["objects"]
            fusion_colors = list(set(elements1["colors"] + elements2["colors"]))[:3]
            fusion_materials = list(set(elements1["materials"] + elements2["materials"]))[:2]
        else:  # hybrid
            fusion_objects = (elements1["objects"][:1] + elements2["objects"][:1])
            fusion_colors = elements2["colors"] if fusion_strength > 0.5 else elements1["colors"]
            fusion_materials = elements1["materials"] + elements2["materials"]
        
        # Generate template-based prompt
        objects_str = " and ".join(fusion_objects) if fusion_objects else "interesting geometric object"
        colors_str = ", ".join(fusion_colors[:3]) if fusion_colors else "vibrant colors"
        materials_str = " and ".join(fusion_materials) if fusion_materials else "mixed materials"
        
        fallback_prompt = (
            f"Create a detailed 3D model of a unique fusion between {objects_str}. "
            f"The object should have {colors_str} coloring and be made of {materials_str}. "
            f"Design it with clean, well-defined geometry suitable for 3D modeling. "
            f"Include realistic lighting and textures. Set against a pure white background. "
            f"The fusion should be creative, visually appealing, and technically feasible for 3D generation. "
            f"Emphasize clear surfaces, good proportions, and distinctive features from both original concepts."
        )
        
        print(f"🔄 Using fallback fusion prompt: {fallback_prompt[:100]}...")
        return fallback_prompt
        
    except Exception as e:
        print(f"❌ Error in fallback fusion prompt: {e}")
        return (
            "Create a unique 3D object that creatively combines geometric and organic elements. "
            "Use vibrant colors and mixed materials. Design with clean geometry suitable for 3D modeling. "
            "Pure white background with realistic lighting and textures."
        )

def upload_gemini_image_to_s3(job_id: str, gemini_image_path: str) -> Optional[str]:
    """Upload Gemini generated image to S3 and return presigned URL.
    
    Args:
        job_id: Job identifier
        gemini_image_path: Local path to Gemini generated image
        
    Returns:
        Presigned URL or None if failed
    """
    try:
        if not os.path.exists(gemini_image_path):
            print(f"⚠️ Gemini image not found: {gemini_image_path}")
            return None
            
        s3 = s3_client()
        
        # Create organized S3 key for Gemini image
        timestamp = int(time.time())
        filename = os.path.basename(gemini_image_path)
        key = f"assets/{job_id}/gemini_images/{timestamp}_{filename}"
        
        # Determine content type
        file_ext = os.path.splitext(gemini_image_path)[1].lower()
        content_type = "image/png" if file_ext == '.png' else "image/jpeg"
        
        # Upload to S3
        print(f"🎨 Uploading Gemini image: {filename} -> s3://{S3_BUCKET}/{key}")
        
        with open(gemini_image_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET, 
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {
                        "job_id": job_id,
                        "asset_type": "gemini_image",
                        "upload_time": str(timestamp),
                        "purpose": "3d-processing"
                    }
                }
            )
        
        # Generate presigned URL (valid for 7 days)
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=7 * 24 * 3600  # 7 days
        )
        
        # Also generate direct S3 URL
        s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
        s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{key}"
        
        print(f"✅ Gemini image uploaded successfully: {key}")
        print(f"🔗 Direct URL: {s3_direct_url}")
        
        return {
            "presigned_url": presigned_url,
            "s3_direct_url": s3_direct_url,
            "s3_key": key,
            "upload_timestamp": timestamp
        }
        
    except Exception as e:
        print(f"❌ Error uploading Gemini image {gemini_image_path}: {e}")
        return None

def upload_luma_gif_to_s3(job_id: str, gif_path: str) -> Optional[dict]:
    """Upload Luma Ray generated GIF to S3 and return presigned URL.
    
    Args:
        job_id: Job identifier
        gif_path: Local path to GIF file
        
    Returns:
        Dictionary with presigned URL and metadata or None if failed
    """
    try:
        if not os.path.exists(gif_path):
            print(f"⚠️ Luma Ray GIF not found: {gif_path}")
            return None
            
        s3 = s3_client()
        
        # Create organized S3 key for GIF
        timestamp = int(time.time())
        filename = os.path.basename(gif_path)
        key = f"assets/{job_id}/luma_animations/{timestamp}_{filename}"
        
        # Upload to S3
        print(f"🎬 Uploading Luma Ray GIF: {filename} -> s3://{S3_BUCKET}/{key}")
        
        with open(gif_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET, 
                key,
                ExtraArgs={
                    "ContentType": "image/gif",
                    "Metadata": {
                        "job_id": job_id,
                        "asset_type": "luma_gif",
                        "upload_time": str(timestamp),
                        "purpose": "animation-result"
                    }
                }
            )
        
        # Generate presigned URL (valid for 7 days)
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=7 * 24 * 3600  # 7 days
        )
        
        # Also generate direct S3 URL
        s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
        s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{key}"
        
        print(f"✅ Luma Ray GIF uploaded successfully: {key}")
        print(f"🔗 Direct URL: {s3_direct_url}")
        
        return {
            "presigned_url": presigned_url,
            "s3_direct_url": s3_direct_url,
            "s3_key": key,
            "upload_timestamp": timestamp
        }
        
    except Exception as e:
        print(f"❌ Error uploading Luma Ray GIF {gif_path}: {e}")
        return None

def upload_input_image_to_s3(job_id: str, input_image_path: str) -> Optional[dict]:
    """Upload input image to S3 and return presigned URL.
    
    Args:
        job_id: Job identifier
        input_image_path: Local path to input image
        
    Returns:
        Dictionary with presigned URL and metadata or None if failed
    """
    try:
        if not os.path.exists(input_image_path):
            print(f"⚠️ Input image not found: {input_image_path}")
            return None
            
        s3 = s3_client()
        
        # Create organized S3 key for input image
        timestamp = int(time.time())
        filename = os.path.basename(input_image_path)
        key = f"assets/{job_id}/input_images/{timestamp}_{filename}"
        
        # Determine content type
        file_ext = os.path.splitext(input_image_path)[1].lower()
        content_type = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg', 
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp'
        }.get(file_ext, 'image/jpeg')
        
        # Upload to S3
        print(f"📷 Uploading input image: {filename} -> s3://{S3_BUCKET}/{key}")
        
        with open(input_image_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET, 
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {
                        "job_id": job_id,
                        "asset_type": "input_image",
                        "upload_time": str(timestamp),
                        "purpose": "animation-source"
                    }
                }
            )
        
        # Generate presigned URL (valid for 7 days)
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=7 * 24 * 3600  # 7 days
        )
        
        # Also generate direct S3 URL
        s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
        s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{key}"
        
        print(f"✅ Input image uploaded successfully: {key}")
        print(f"🔗 Direct URL: {s3_direct_url}")
        
        return {
            "presigned_url": presigned_url,
            "s3_direct_url": s3_direct_url,
            "s3_key": key,
            "upload_timestamp": timestamp
        }
        
    except Exception as e:
        print(f"❌ Error uploading input image {input_image_path}: {e}")
        return None

def upload_poses_image_to_s3(job_id: str, poses_image_path: str) -> Optional[dict]:
    """Upload character poses image to S3 and return presigned URL.
    
    Args:
        job_id: Job identifier
        poses_image_path: Local path to poses image file
        
    Returns:
        Dictionary with presigned URL and metadata or None if failed
    """
    try:
        if not os.path.exists(poses_image_path):
            print(f"⚠️ Poses image not found: {poses_image_path}")
            return None
            
        s3 = s3_client()
        
        # Create organized S3 key for poses image
        timestamp = int(time.time())
        filename = os.path.basename(poses_image_path)
        key = f"assets/{job_id}/poses/{timestamp}_{filename}"
        
        # Determine content type
        file_ext = os.path.splitext(poses_image_path)[1].lower()
        content_type = "image/png" if file_ext == '.png' else "image/jpeg"
        
        # Upload to S3
        print(f"🎭 Uploading poses image: {filename} -> s3://{S3_BUCKET}/{key}")
        
        with open(poses_image_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET, 
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {
                        "job_id": job_id,
                        "asset_type": "character_poses",
                        "upload_time": str(timestamp),
                        "purpose": "character-pose-generation"
                    }
                }
            )
        
        # Generate presigned URL (valid for 7 days)
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=7 * 24 * 3600  # 7 days
        )
        
        # Also generate direct S3 URL
        s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
        s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{key}"
        
        print(f"✅ Poses image uploaded successfully: {key}")
        print(f"🔗 Direct URL: {s3_direct_url}")
        
        return {
            "presigned_url": presigned_url,
            "s3_direct_url": s3_direct_url,
            "s3_key": key,
            "upload_timestamp": timestamp
        }
        
    except Exception as e:
        print(f"❌ Error uploading poses image {poses_image_path}: {e}")
        return None

def upload_infused_image_to_s3(job_id: str, image_path: str, image_tag: str) -> Optional[dict]:
    """Upload infused job image to S3 with specific tagging for source images and fused images.
    
    Args:
        job_id: Job identifier
        image_path: Local path to image file
        image_tag: Tag identifying the image type (source_image_1, source_image_2, fused_image, etc.)
        
    Returns:
        Dictionary with S3 URLs and metadata or None if failed
    """
    try:
        if not os.path.exists(image_path):
            print(f"⚠️ Infused image not found: {image_path}")
            return None
            
        s3 = s3_client()
        
        # Create organized S3 key for infused image
        timestamp = int(time.time())
        filename = os.path.basename(image_path)
        key = f"assets/{job_id}/infused_images/{image_tag}/{timestamp}_{filename}"
        
        # Determine content type
        file_ext = os.path.splitext(image_path)[1].lower()
        content_type_map = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.bmp': 'image/bmp'
        }
        content_type = content_type_map.get(file_ext, 'image/jpeg')
        
        # Upload to S3
        print(f"🔀 Uploading infused image ({image_tag}): {filename} -> s3://{S3_BUCKET}/{key}")
        
        with open(image_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET, 
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {
                        "job_id": job_id,
                        "asset_type": "infused_image",
                        "image_tag": image_tag,
                        "upload_time": str(timestamp),
                        "purpose": "image-fusion"
                    }
                }
            )
        
        # Generate presigned URL (valid for 7 days)
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=7 * 24 * 3600  # 7 days
        )
        
        # Also generate direct S3 URL
        s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
        s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{key}"
        
        print(f"✅ Infused image uploaded successfully: {key}")
        print(f"🏷️ Tag: {image_tag}, Direct URL: {s3_direct_url[:50]}...")
        
        return {
            "presigned_url": presigned_url,
            "s3_direct_url": s3_direct_url,
            "s3_key": key,
            "image_tag": image_tag,
            "upload_timestamp": timestamp
        }
        
    except Exception as e:
        print(f"❌ Error uploading infused image {image_path} with tag {image_tag}: {e}")
        return None

def generate_fusion_image_and_upload_to_s3(job_id: str, fusion_prompt: str, fusion_style: str, fusion_strength: float) -> Optional[dict]:
    """Generate a fusion image using Gemini and upload directly to S3 without local storage.
    
    Args:
        job_id: Job identifier
        fusion_prompt: Text prompt for generating the fusion image
        fusion_style: Fusion technique used
        fusion_strength: Balance between source images
        
    Returns:
        Dictionary with S3 URLs and metadata or None if failed
    """
    try:
        print(f"🎨 Generating fusion image with database-only storage for job {job_id}")
        print(f"📝 Fusion prompt: {fusion_prompt[:100]}...")
        
        if not mapapi:
            print(f"❌ mapapi module not available for image generation")
            return None
        
        # Create temporary file for image generation
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
            temp_image_path = temp_file.name
        
        try:
            # Set environment override for fusion prompt
            original_prompt = os.environ.get("GEMINI_PROMPT_OVERRIDE")
            os.environ["GEMINI_PROMPT_OVERRIDE"] = fusion_prompt
            
            try:
                # Generate fusion image
                gemini_result = mapapi.generate_image_with_gemini(
                    objects_description="",  # Will use prompt override
                    reference_image_path="",  # No reference for fusion
                    output_path=temp_image_path
                )
                
                # Handle both tuple and single return formats
                if isinstance(gemini_result, tuple):
                    final_image_path, content_type = gemini_result
                else:
                    final_image_path = gemini_result
                
                if not final_image_path or not os.path.exists(final_image_path):
                    print(f"❌ Failed to generate fusion image with Gemini")
                    return None
                
                print(f"✅ Fusion image generated successfully")
                
                # Upload directly to S3
                s3 = s3_client()
                timestamp = int(time.time())
                fusion_filename = f"fusion_{fusion_style}_{job_id}_{timestamp}.png"
                s3_key = f"assets/{job_id}/fusion_images/{timestamp}_{fusion_filename}"
                
                print(f"📤 Uploading fusion image to S3: {fusion_filename}")
                
                with open(final_image_path, 'rb') as f:
                    s3.upload_fileobj(
                        f,
                        S3_BUCKET,
                        s3_key,
                        ExtraArgs={
                            "ContentType": "image/png",
                            "Metadata": {
                                "job_id": job_id,
                                "asset_type": "fusion_image",
                                "fusion_style": fusion_style,
                                "fusion_strength": str(fusion_strength),
                                "upload_time": str(timestamp),
                                "generation_prompt": fusion_prompt[:200]  # Store truncated prompt
                            }
                        }
                    )
                
                # Generate presigned URL (valid for 7 days)
                presigned_url = s3.generate_presigned_url(
                    ClientMethod="get_object",
                    Params={"Bucket": S3_BUCKET, "Key": s3_key},
                    ExpiresIn=7 * 24 * 3600
                )
                
                # Generate direct S3 URL
                s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
                s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{s3_key}"
                
                print(f"✅ Fusion image uploaded to S3 successfully!")
                print(f"🔗 S3 URL: {s3_direct_url}")
                
                return {
                    "presigned_url": presigned_url,
                    "s3_direct_url": s3_direct_url,
                    "s3_key": s3_key,
                    "upload_timestamp": timestamp,
                    "fusion_metadata": {
                        "fusion_style": fusion_style,
                        "fusion_strength": fusion_strength,
                        "generation_prompt": fusion_prompt,
                        "filename": fusion_filename
                    }
                }
                
            finally:
                # Restore original prompt
                if original_prompt:
                    os.environ["GEMINI_PROMPT_OVERRIDE"] = original_prompt
                elif "GEMINI_PROMPT_OVERRIDE" in os.environ:
                    del os.environ["GEMINI_PROMPT_OVERRIDE"]
            
        finally:
            # Clean up temporary file
            try:
                os.unlink(temp_image_path)
            except:
                pass
                
    except Exception as e:
        print(f"❌ Error generating fusion image: {e}")
        return None

def upload_uploaded_file_to_s3(job_id: str, file_content: bytes, filename: str, image_tag: str) -> Optional[dict]:
    """Upload user-uploaded file content directly to S3 without local storage.
    
    Args:
        job_id: Job identifier
        file_content: File content as bytes
        filename: Original filename
        image_tag: Tag identifying the image type (source_image_1, source_image_2, etc.)
        
    Returns:
        Dictionary with S3 URLs and metadata or None if failed
    """
    try:
        print(f"📤 Uploading {image_tag} directly to S3 for job {job_id}")
        
        s3 = s3_client()
        
        # Create organized S3 key
        timestamp = int(time.time())
        file_ext = os.path.splitext(filename)[1].lower()
        clean_filename = f"{image_tag}_{timestamp}{file_ext}"
        s3_key = f"assets/{job_id}/source_images/{image_tag}/{timestamp}_{clean_filename}"
        
        # Determine content type
        content_type_map = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.bmp': 'image/bmp'
        }
        content_type = content_type_map.get(file_ext, 'image/jpeg')
        
        # Upload to S3
        print(f"📤 Uploading {image_tag}: {filename} -> s3://{S3_BUCKET}/{s3_key}")
        
        s3.upload_fileobj(
            io.BytesIO(file_content),
            S3_BUCKET,
            s3_key,
            ExtraArgs={
                "ContentType": content_type,
                "Metadata": {
                    "job_id": job_id,
                    "asset_type": "source_image",
                    "image_tag": image_tag,
                    "original_filename": filename,
                    "upload_time": str(timestamp)
                }
            }
        )
        
        # Generate presigned URL (valid for 7 days)
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": s3_key},
            ExpiresIn=7 * 24 * 3600
        )
        
        # Generate direct S3 URL
        s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
        s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{s3_key}"
        
        print(f"✅ {image_tag} uploaded successfully!")
        print(f"🔗 S3 URL: {s3_direct_url}")
        
        return {
            "presigned_url": presigned_url,
            "s3_direct_url": s3_direct_url,
            "s3_key": s3_key,
            "upload_timestamp": timestamp,
            "source_metadata": {
                "image_tag": image_tag,
                "original_filename": filename,
                "file_size": len(file_content)
            }
        }
        
    except Exception as e:
        print(f"❌ Error uploading {image_tag} to S3: {e}")
        return None

def generate_fusion_image_database_only(job_id: str, fusion_prompt: str, fusion_style: str, fusion_strength: float, source_image_1_content: bytes, source_image_2_content: bytes) -> Optional[bytes]:
    """Generate a fusion image using Gemini with both source images as references and return image content as bytes for database storage.
    
    Args:
        job_id: Job identifier
        fusion_prompt: Text prompt for generating the fusion image
        fusion_style: Fusion technique used
        fusion_strength: Balance between source images
        source_image_1_content: First source image content as bytes
        source_image_2_content: Second source image content as bytes
        
    Returns:
        Image content as bytes or None if failed
    """
    try:
        print(f"🎨 Generating fusion image with BOTH source images as references (job {job_id})")
        print(f"📝 Fusion prompt: {fusion_prompt[:100]}...")
        
        if not mapapi:
            print(f"❌ mapapi module not available for image generation")
            return None
        
        # Create temporary files for both source images (needed for Gemini reference)
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp1:
            temp1.write(source_image_1_content)
            temp1_path = temp1.name
        
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp2:
            temp2.write(source_image_2_content)
            temp2_path = temp2.name
        
        # Create temporary file for output
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_out:
            temp_output_path = temp_out.name
        
        try:
            # Enhanced fusion prompt that instructs Gemini to use both reference images
            enhanced_fusion_prompt = f"""
Create a fusion of the two reference images provided. {fusion_prompt}

Use both reference images as the basis for this fusion:
- Reference Image 1: Use as the primary structural base
- Reference Image 2: Blend and incorporate key elements, textures, and characteristics

Fusion Style: {fusion_style}
Fusion Strength: {fusion_strength} (0.0 = more like image 1, 1.0 = more like image 2)

Generate a cohesive fusion that combines the best elements of both images into a single, unified result. 
The output should be optimized for 3D model generation with clear geometry and good lighting.
"""
            
            # Set environment override for fusion prompt
            original_prompt = os.environ.get("GEMINI_PROMPT_OVERRIDE")
            os.environ["GEMINI_PROMPT_OVERRIDE"] = enhanced_fusion_prompt
            
            try:
                # Check if mapapi has a fusion-specific function
                if hasattr(mapapi, 'generate_fusion_image_with_gemini'):
                    print(f"🔄 Using fusion-specific Gemini function with both reference images...")
                    gemini_result = mapapi.generate_fusion_image_with_gemini(
                        fusion_prompt=enhanced_fusion_prompt,
                        reference_image_1_path=temp1_path,
                        reference_image_2_path=temp2_path,
                        output_path=temp_output_path,
                        fusion_style=fusion_style,
                        fusion_strength=fusion_strength
                    )
                else:
                    print(f"🔄 Using standard Gemini function with enhanced prompt and primary reference...")
                    # Use the first image as primary reference and enhanced prompt for fusion
                    gemini_result = mapapi.generate_image_with_gemini(
                        objects_description="",  # Will use prompt override
                        reference_image_path=temp1_path,  # Use first image as primary reference
                        output_path=temp_output_path
                    )
                
                # Handle both tuple and single return formats
                if isinstance(gemini_result, tuple):
                    final_image_path, content_type = gemini_result
                else:
                    final_image_path = gemini_result
                
                if not final_image_path or not os.path.exists(final_image_path):
                    print(f"❌ Failed to generate fusion image with Gemini")
                    return None
                
                # Read image content as bytes
                with open(final_image_path, 'rb') as f:
                    image_content = f.read()
                
                print(f"✅ Fusion image generated successfully using both reference images ({len(image_content)} bytes)")
                return image_content
                
            finally:
                # Restore original prompt
                if original_prompt:
                    os.environ["GEMINI_PROMPT_OVERRIDE"] = original_prompt
                elif "GEMINI_PROMPT_OVERRIDE" in os.environ:
                    del os.environ["GEMINI_PROMPT_OVERRIDE"]
            
        finally:
            # Clean up temporary files
            try:
                os.unlink(temp1_path)
                os.unlink(temp2_path)
                os.unlink(temp_output_path)
            except:
                pass
                
    except Exception as e:
        print(f"❌ Error generating fusion image for database storage: {e}")
        return None

def run_fusion_pipeline_database_only(
    job_id: str,
    source_image_1_content: bytes,
    source_image_2_content: bytes,
    source_image_1_filename: str,
    source_image_2_filename: str,
    fusion_style: str,
    fusion_strength: float,
    animation_ids: List[int],
    height_meters: float,
    fps: int,
    use_retexture: bool
):
    """Database-only fusion pipeline that stores all images directly in the database.
    
    Args:
        job_id: Job identifier
        source_image_1_content: First image content as bytes
        source_image_2_content: Second image content as bytes
        source_image_1_filename: Original filename of first image
        source_image_2_filename: Original filename of second image
        fusion_style: Fusion technique ("blend", "merge", "hybrid")
        fusion_strength: Balance between images (0.0 to 1.0)
        animation_ids: List of animation IDs to apply
        height_meters: Character height for rigging
        fps: Animation frame rate
        use_retexture: Whether to enhance materials and textures
    """
    try:
        print(f"🔀 [Job {job_id}] Starting COMPLETE DATABASE-ONLY fusion pipeline!")
        print(f"📝 Fusion settings: {fusion_style} (strength: {fusion_strength})")
        print(f"💾 All images will be stored directly in database - no S3, no local folders")
        
        # Update initial status and save source images in both S3 and database
        import base64
        
        # Step 1: Upload source images to S3 first
        print(f"📤 Uploading source images to S3...")
        
        # Upload first source image to S3
        source_1_s3_result = upload_uploaded_file_to_s3(
            job_id, source_image_1_content, source_image_1_filename, "source_image_1"
        )
        
        if not source_1_s3_result:
            raise RuntimeError("Failed to upload first source image to S3")
        
        # Upload second source image to S3
        source_2_s3_result = upload_uploaded_file_to_s3(
            job_id, source_image_2_content, source_image_2_filename, "source_image_2"
        )
        
        if not source_2_s3_result:
            raise RuntimeError("Failed to upload second source image to S3")
        
        print(f"✅ Both source images uploaded to S3")
        
        # Step 2: Convert source images to base64 for database storage
        source_1_base64 = base64.b64encode(source_image_1_content).decode('utf-8')
        source_2_base64 = base64.b64encode(source_image_2_content).decode('utf-8')
        
        update_job_status(job_id, "processing", 10, "Source images uploaded to S3 and saved in database", {
            "job_type": "fusion",
            "storage_type": "complete_database_only",
            "fusion_style": fusion_style,
            "fusion_strength": fusion_strength,
            "source_files": [source_image_1_filename, source_image_2_filename],
            "source_images_s3": {
                "source_image_1": source_1_s3_result,
                "source_image_2": source_2_s3_result
            },
            "source_images": {
                "source_image_1": {
                    "filename": source_image_1_filename,
                    "content_base64": source_1_base64,
                    "size_bytes": len(source_image_1_content),
                    "content_type": "image/jpeg",
                    "s3_url": source_1_s3_result["s3_direct_url"],
                    "s3_presigned_url": source_1_s3_result["presigned_url"]
                },
                "source_image_2": {
                    "filename": source_image_2_filename,
                    "content_base64": source_2_base64,
                    "size_bytes": len(source_image_2_content),
                    "content_type": "image/jpeg",
                    "s3_url": source_2_s3_result["s3_direct_url"],
                    "s3_presigned_url": source_2_s3_result["presigned_url"]
                }
            }
        })
        
        print(f"✅ Source images saved in both S3 and database")
        print(f"   - Image 1: {source_image_1_filename} ({len(source_image_1_content)} bytes)")
        print(f"     S3: {source_1_s3_result['s3_direct_url'][:50]}...")
        print(f"   - Image 2: {source_image_2_filename} ({len(source_image_2_content)} bytes)")
        print(f"     S3: {source_2_s3_result['s3_direct_url'][:50]}...")
        
        # Step 2: Analyze images for fusion (create temporary files only for analysis)
        update_job_status(job_id, "processing", 20, "Analyzing source images with AI...")
        print(f"🔍 Analyzing source images for fusion...")
        
        # Create temporary files for analysis
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp1:
            temp1.write(source_image_1_content)
            temp1_path = temp1.name
        
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp2:
            temp2.write(source_image_2_content)
            temp2_path = temp2.name
        
        try:
            # Analyze both images
            analysis1 = analyze_image_for_fusion(temp1_path)
            analysis2 = analyze_image_for_fusion(temp2_path)
            
            print(f"✅ Image analysis completed")
            
        finally:
            # Clean up temporary analysis files
            try:
                os.unlink(temp1_path)
                os.unlink(temp2_path)
            except:
                pass
        
        # Step 3: Generate fusion prompt
        update_job_status(job_id, "processing", 30, "Generating fusion prompt with AI...")
        print(f"🧠 Creating fusion prompt...")
        
        fusion_prompt = generate_fusion_prompt_with_ollama(
            analysis1, analysis2, fusion_style, fusion_strength
        )
        
        print(f"📝 Fusion prompt generated: {fusion_prompt[:100]}...")
        
        # Step 4: Generate fusion image using both source images as references and save directly in database
        update_job_status(job_id, "processing", 40, "Generating fusion image with both source images...")
        print(f"🎨 Generating fusion image using both source images as references...")
        
        fusion_image_content = generate_fusion_image_database_only(
            job_id=job_id,
            fusion_prompt=fusion_prompt,
            fusion_style=fusion_style,
            fusion_strength=fusion_strength,
            source_image_1_content=source_image_1_content,
            source_image_2_content=source_image_2_content
        )
        
        if not fusion_image_content:
            raise RuntimeError("Failed to generate fusion image")
        
        # Upload fusion image to S3
        print(f"📤 Uploading fusion image to S3...")
        fusion_s3_result = upload_uploaded_file_to_s3(
            job_id=job_id,
            file_content=fusion_image_content,
            filename=f"fusion_image_{job_id}.png",
            image_tag="fusion_image"
        )
        
        if not fusion_s3_result:
            print(f"⚠️ Failed to upload fusion image to S3, continuing without S3 URL")
            fusion_s3_result = None
        else:
            print(f"✅ Fusion image uploaded to S3: {fusion_s3_result['s3_direct_url'][:50]}...")
        
        # Convert fusion image to base64 and save in database
        fusion_image_base64 = base64.b64encode(fusion_image_content).decode('utf-8')
        
        # Update job with fusion image stored in database
        update_job_status(job_id, "processing", 60, "Fusion image generated and saved to database", {
            "job_type": "fusion",
            "storage_type": "complete_database_only",
            "fusion_style": fusion_style,
            "fusion_strength": fusion_strength,
            "source_files": [source_image_1_filename, source_image_2_filename],
            "source_images": {
                "source_image_1": {
                    "filename": source_image_1_filename,
                    "content_base64": source_1_base64,
                    "size_bytes": len(source_image_1_content),
                    "content_type": "image/jpeg"
                },
                "source_image_2": {
                    "filename": source_image_2_filename,
                    "content_base64": source_2_base64,
                    "size_bytes": len(source_image_2_content),
                    "content_type": "image/jpeg"
                }
            },
            "fusion_image": {
                "content_base64": fusion_image_base64,
                "size_bytes": len(fusion_image_content),
                "content_type": "image/png",
                "fusion_prompt": fusion_prompt,
                "generated_timestamp": time.time(),
                "s3_url": fusion_s3_result["s3_direct_url"] if fusion_s3_result else None,
                "s3_presigned_url": fusion_s3_result["presigned_url"] if fusion_s3_result else None
            },
            "analysis": {
                "image_1_analysis": analysis1,
                "image_2_analysis": analysis2
            }
        })
        
        print(f"✅ Fusion image generated and saved directly in database ({len(fusion_image_content)} bytes)")
        
        # Complete fusion process - STOP HERE (no auto 3D model generation)
        print(f"✅ Fusion pipeline completed - awaiting user confirmation for 3D model generation")
        
        final_result = {
            "job_type": "fusion",
            "storage_type": "complete_database_only",
            "fusion_settings": {
                "style": fusion_style,
                "strength": fusion_strength,
                "animation_ids": animation_ids,
                "height_meters": height_meters,
                "fps": fps,
                "use_retexture": use_retexture
            },
            "source_images": {
                "source_image_1": {
                    "filename": source_image_1_filename,
                    "content_base64": source_1_base64,
                    "size_bytes": len(source_image_1_content),
                    "content_type": "image/jpeg"
                },
                "source_image_2": {
                    "filename": source_image_2_filename,
                    "content_base64": source_2_base64,
                    "size_bytes": len(source_image_2_content),
                    "content_type": "image/jpeg"
                }
            },
            "fusion_image": {
                "content_base64": fusion_image_base64,
                "size_bytes": len(fusion_image_content),
                "content_type": "image/png",
                "fusion_prompt": fusion_prompt,
                "generated_timestamp": time.time(),
                "s3_url": fusion_s3_result["s3_direct_url"] if fusion_s3_result else None,
                "s3_presigned_url": fusion_s3_result["presigned_url"] if fusion_s3_result else None
            },
            "fusion_prompt": fusion_prompt,
            "analysis": {
                "image_1_analysis": analysis1,
                "image_2_analysis": analysis2
            },
            "animation_requirements": {
                "animation_ids": animation_ids,
                "height_meters": height_meters,
                "fps": fps,
                "status": "planned"
            },
            "next_step": "awaiting_user_confirmation_for_3d_model",
            "fusion_s3_url": fusion_s3_result["s3_direct_url"] if fusion_s3_result else None,
            "fusion_s3_presigned_url": fusion_s3_result["presigned_url"] if fusion_s3_result else None
        }
        
        update_job_status(
            job_id=job_id,
            status="completed",
            progress=100,
            message=f"Fusion image generated and ready! Uploaded to S3 and saved in database. Use the confirmation endpoint to proceed with 3D model generation.",
            result=final_result
        )
        
        print(f"🎉 Fusion job {job_id} completed - fusion image ready for user confirmation!")
        print(f"💾 Assets saved in database:")
        print(f"   - Source Image 1: {len(source_image_1_content)} bytes")
        print(f"   - Source Image 2: {len(source_image_2_content)} bytes")
        print(f"   - Fusion Image: {len(fusion_image_content)} bytes")
        if fusion_s3_result:
            print(f"   - Fusion S3 URL: {fusion_s3_result['s3_direct_url'][:50]}...")
        print(f"🔄 Next: User must confirm to proceed with 3D model generation")
        
    except Exception as e:
        print(f"❌ Error in complete database-only fusion pipeline for job {job_id}: {e}")
        update_job_status(
            job_id=job_id,
            status="failed",
            progress=0,
            message=f"Fusion pipeline failed: {str(e)}",
            result={
                "job_type": "fusion",
                "storage_type": "complete_database_only",
                "error": str(e),
                "fusion_settings": {
                    "style": fusion_style,
                    "strength": fusion_strength
                }
            }
        )

def collect_and_upload_job_assets(job_id: str, job_folder: str, result_data: dict) -> S3Assets:
    """Collect all job assets and upload them to S3, returning organized URLs.
    
    Args:
        job_id: Job identifier
        job_folder: Local folder containing job files
        result_data: Dictionary containing result file paths and metadata
        
    Returns:
        S3Assets object with all uploaded asset URLs
    """
    print(f"📁 Collecting assets for job {job_id} from folder: {job_folder}")
    
    assets = S3Assets()
    
    try:
        # Upload source images
        if 'source_image' in result_data and result_data['source_image']:
            source_path = result_data['source_image']
            if os.path.exists(source_path):
                url = upload_asset_to_s3(source_path, "images", job_id, "source_image.jpg")
                if url:
                    assets.images["source"] = url
        
        # Upload generated images (from 3D generation process)
        generated_images_folder = os.path.join(job_folder, "generated_images")
        if os.path.exists(generated_images_folder):
            for img_file in os.listdir(generated_images_folder):
                if img_file.lower().endswith(('.jpg', '.jpeg', '.png')):
                    img_path = os.path.join(generated_images_folder, img_file)
                    url = upload_asset_to_s3(img_path, "images", job_id, f"generated_{img_file}")
                    if url:
                        assets.images[f"generated_{img_file}"] = url
        
        # Upload 3D model files
        if 'raw_3d_model' in result_data and result_data['raw_3d_model']:
            model_path = result_data['raw_3d_model']
            if os.path.exists(model_path):
                filename = os.path.basename(model_path)
                url = upload_asset_to_s3(model_path, "models", job_id, filename)
                if url:
                    assets.models["raw_3d_model"] = url
        
        # Upload retextured models
        if 'final_textured_glb' in result_data and result_data['final_textured_glb']:
            retextured_path = result_data['final_textured_glb']
            if os.path.exists(retextured_path):
                filename = os.path.basename(retextured_path)
                url = upload_asset_to_s3(retextured_path, "retextured", job_id, filename)
                if url:
                    assets.retextured["final_textured"] = url
        
        # Upload animation files
        if 'raw_anim_glb' in result_data and result_data['raw_anim_glb']:
            anim_glb_path = result_data['raw_anim_glb']
            if os.path.exists(anim_glb_path):
                filename = os.path.basename(anim_glb_path)
                url = upload_asset_to_s3(anim_glb_path, "animations", job_id, filename)
                if url:
                    assets.animations["glb_animation"] = url
        
        if 'raw_anim_fbx' in result_data and result_data['raw_anim_fbx']:
            anim_fbx_path = result_data['raw_anim_fbx']
            if os.path.exists(anim_fbx_path):
                filename = os.path.basename(anim_fbx_path)
                url = upload_asset_to_s3(anim_fbx_path, "animations", job_id, filename)
                if url:
                    assets.animations["fbx_animation"] = url
        
        # Upload intermediate files (logs, metadata, etc.)
        log_files = []
        if os.path.exists(job_folder):
            for file in os.listdir(job_folder):
                if file.endswith(('.log', '.json', '.txt')) and not file.startswith('.'):
                    log_path = os.path.join(job_folder, file)
                    if os.path.isfile(log_path):
                        log_files.append(log_path)
        
        for log_path in log_files:
            filename = os.path.basename(log_path)
            url = upload_asset_to_s3(log_path, "intermediate", job_id, filename)
            if url:
                assets.intermediate[filename] = url
        
        # Summary
        total_assets = (len(assets.images) + len(assets.models) + 
                       len(assets.retextured) + len(assets.animations) + 
                       len(assets.intermediate))
        
        print(f"✅ Successfully uploaded {total_assets} assets to S3 for job {job_id}")
        print(f"   - Images: {len(assets.images)}")
        print(f"   - Models: {len(assets.models)}")
        print(f"   - Retextured: {len(assets.retextured)}")
        print(f"   - Animations: {len(assets.animations)}")
        print(f"   - Intermediate: {len(assets.intermediate)}")
        
    except Exception as e:
        print(f"❌ Error collecting assets for job {job_id}: {e}")
    
    return assets

def gif_to_spritesheet_database_only(gif_url: str, sheet_type: str = "horizontal", max_frames: int = 50, background_color: str = "transparent") -> Optional[dict]:
    """Convert a GIF to a sprite sheet using database-only storage pattern.
    
    Args:
        gif_url: URL to the GIF file (S3 URL or publicly accessible URL)
        sheet_type: 'horizontal' or 'vertical' layout
        max_frames: Maximum number of frames to process (memory safety)
        background_color: Background color for the sprite sheet
        
    Returns:
        Dictionary with S3 URLs and metadata or None if failed
    """
    try:
        print(f"🎬 Converting GIF to sprite sheet: {gif_url[:100]}...")
        print(f"📐 Sheet type: {sheet_type}, Max frames: {max_frames}, Background: {background_color}")
        
        # Download GIF to temporary file
        print(f"📥 Downloading GIF from URL...")
        response = requests.get(gif_url, stream=True, timeout=30)
        response.raise_for_status()
        
        # Create temporary file for GIF
        with tempfile.NamedTemporaryFile(suffix='.gif', delete=False) as temp_gif:
            for chunk in response.iter_content(chunk_size=8192):
                temp_gif.write(chunk)
            temp_gif_path = temp_gif.name
        
        try:
            # Open and process GIF
            print(f"🔍 Analyzing GIF structure...")
            gif = Image.open(temp_gif_path)
            
            # Extract frames with safety limit
            frames = []
            frame_count = 0
            
            for frame in ImageSequence.Iterator(gif):
                if frame_count >= max_frames:
                    print(f"⚠️ Reached frame limit {max_frames}, truncating GIF")
                    break
                    
                # Convert frame to RGBA for consistent handling
                frame_rgba = frame.copy().convert("RGBA")
                frames.append(frame_rgba)
                frame_count += 1
            
            if not frames:
                print(f"❌ No frames found in GIF")
                return None
            
            print(f"✅ Extracted {len(frames)} frames from GIF")
            
            # Get frame dimensions
            frame_width, frame_height = frames[0].size
            num_frames = len(frames)
            
            # Calculate sprite sheet dimensions
            if sheet_type == "horizontal":
                sheet_width = frame_width * num_frames
                sheet_height = frame_height
            else:  # vertical
                sheet_width = frame_width
                sheet_height = frame_height * num_frames
            
            print(f"📏 Sprite sheet dimensions: {sheet_width}x{sheet_height} ({num_frames} frames)")
            
            # Create background based on color preference
            if background_color == "transparent":
                spritesheet = Image.new("RGBA", (sheet_width, sheet_height), (0, 0, 0, 0))
            elif background_color == "white":
                spritesheet = Image.new("RGBA", (sheet_width, sheet_height), (255, 255, 255, 255))
            elif background_color == "black":
                spritesheet = Image.new("RGBA", (sheet_width, sheet_height), (0, 0, 0, 255))
            elif background_color.startswith("#"):
                # Hex color
                try:
                    hex_color = background_color.lstrip('#')
                    rgb = tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
                    spritesheet = Image.new("RGBA", (sheet_width, sheet_height), rgb + (255,))
                except ValueError:
                    print(f"⚠️ Invalid hex color {background_color}, using transparent")
                    spritesheet = Image.new("RGBA", (sheet_width, sheet_height), (0, 0, 0, 0))
            else:
                spritesheet = Image.new("RGBA", (sheet_width, sheet_height), (0, 0, 0, 0))
            
            # Paste frames into sprite sheet
            print(f"🎨 Assembling sprite sheet...")
            for i, frame in enumerate(frames):
                if sheet_type == "horizontal":
                    x_pos = i * frame_width
                    y_pos = 0
                else:  # vertical
                    x_pos = 0
                    y_pos = i * frame_height
                
                spritesheet.paste(frame, (x_pos, y_pos), frame)
            
            # Save sprite sheet to temporary file
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_sprite:
                temp_sprite_path = temp_sprite.name
            
            # Save with PNG format for best quality and transparency support
            spritesheet.save(temp_sprite_path, format="PNG", optimize=True)
            file_size = os.path.getsize(temp_sprite_path)
            
            print(f"💾 Sprite sheet saved temporarily: {file_size} bytes")
            
            # Upload to S3
            s3 = s3_client()
            timestamp = int(time.time())
            sprite_filename = f"spritesheet_{sheet_type}_{num_frames}frames_{timestamp}.png"
            s3_key = f"sprite_sheets/{timestamp}_{sprite_filename}"
            
            print(f"📤 Uploading sprite sheet to S3: {sprite_filename}")
            
            with open(temp_sprite_path, 'rb') as f:
                s3.upload_fileobj(
                    f,
                    S3_BUCKET,
                    s3_key,
                    ExtraArgs={
                        "ContentType": "image/png",
                        "Metadata": {
                            "asset_type": "sprite_sheet",
                            "sheet_type": sheet_type,
                            "frame_count": str(num_frames),
                            "frame_width": str(frame_width),
                            "frame_height": str(frame_height),
                            "sheet_width": str(sheet_width),
                            "sheet_height": str(sheet_height),
                            "background_color": background_color,
                            "upload_time": str(timestamp),
                            "source_gif_url": gif_url
                        }
                    }
                )
            
            # Generate presigned URL (valid for 7 days)
            presigned_url = s3.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": S3_BUCKET, "Key": s3_key},
                ExpiresIn=7 * 24 * 3600
            )
            
            # Generate direct S3 URL
            s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
            s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{s3_key}"
            
            print(f"✅ Sprite sheet uploaded successfully!")
            print(f"📊 Stats: {num_frames} frames, {sheet_width}x{sheet_height}px, {file_size} bytes")
            print(f"🔗 S3 URL: {s3_direct_url}")
            
            # Clean up temporary files
            try:
                os.unlink(temp_sprite_path)
            except:
                pass
            
            return {
                "presigned_url": presigned_url,
                "s3_direct_url": s3_direct_url,
                "s3_key": s3_key,
                "upload_timestamp": timestamp,
                "sprite_metadata": {
                    "sheet_type": sheet_type,
                    "frame_count": num_frames,
                    "frame_width": frame_width,
                    "frame_height": frame_height,
                    "sheet_width": sheet_width,
                    "sheet_height": sheet_height,
                    "background_color": background_color,
                    "file_size_bytes": file_size,
                    "source_gif_url": gif_url
                }
            }
            
        finally:
            # Clean up temporary GIF file
            try:
                os.unlink(temp_gif_path)
            except:
                pass
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Error downloading GIF from {gif_url}: {e}")
        return None
    except Exception as e:
        print(f"❌ Error converting GIF to sprite sheet: {e}")
        return None

async def run_infusion_pipeline(
    job_id: str,
    image1_path: str,
    image2_path: str,
    fusion_style: str,
    fusion_strength: float,
    animation_ids: List[int],
    height_meters: float,
    fps: int,
    use_retexture: bool
):
    """Core pipeline that fuses two images into a new 3D model."""
    try:
        print(f"🔀 [Job {job_id}] Starting INFUSION pipeline - fusing two images into new 3D model!")
        
        # Mark this as an infuse job
        update_job_status(job_id, "processing", 5, "Starting image infusion process...", {
            "job_type": "infusion",
            "fusion_style": fusion_style,
            "fusion_strength": fusion_strength,
            "source_images": [image1_path, image2_path]
        })
        
        # Step 1: Analyze both images with AI vision
        update_job_status(job_id, "processing", 10, "Analyzing first image...")
        print(f"🔍 Analyzing first image: {image1_path}")
        analysis1 = analyze_image_for_fusion(image1_path)
        
        update_job_status(job_id, "processing", 15, "Analyzing second image...")
        print(f"🔍 Analyzing second image: {image2_path}")
        analysis2 = analyze_image_for_fusion(image2_path)
        
        # Step 2: Generate fusion prompt using Ollama/GPT
        update_job_status(job_id, "processing", 20, "Generating fusion prompt with AI...")
        print(f"🧠 Creating fusion prompt...")
        fusion_prompt = generate_fusion_prompt_with_ollama(analysis1, analysis2, fusion_style, fusion_strength)
        
        # Save analysis and fusion data
        job_dir = f"jobs/{job_id}"
        os.makedirs(job_dir, exist_ok=True)
        
        fusion_data = {
            "job_type": "infusion",
            "fusion_style": fusion_style,
            "fusion_strength": fusion_strength,
            "source_images": [image1_path, image2_path],
            "analysis1": analysis1,
            "analysis2": analysis2,
            "fusion_prompt": fusion_prompt,
            "timestamp": time.time()
        }
        
        with open(os.path.join(job_dir, "fusion_analysis.json"), "w") as f:
            json.dump(fusion_data, f, indent=2)
        
        # Upload source images to S3 with proper tagging (only if not already uploaded)
        # Load existing infused image URLs from metadata to avoid duplicates
        infused_image_urls = load_infused_image_urls_from_metadata(job_id)
        
        # Only upload source images if they haven't been uploaded yet
        if "source_image_1" not in infused_image_urls or "source_image_2" not in infused_image_urls:
            update_job_status(job_id, "processing", 22, "Uploading source images to S3...")
            print(f"📤 Uploading source images to S3 with proper tagging...")
            
            # Copy source images to job directory and upload
            source1_local = os.path.join(job_dir, "source_image_1.jpg")
            source2_local = os.path.join(job_dir, "source_image_2.jpg")
            
            try:
                # Copy source images to job folder
                import shutil
                shutil.copy2(image1_path, source1_local)
                shutil.copy2(image2_path, source2_local)
                
                # Upload source image 1 only if not already uploaded
                if "source_image_1" not in infused_image_urls:
                    print(f"📤 Uploading source image 1...")
                    s3_source1_url = upload_infused_image_to_s3(job_id, source1_local, "source_image_1")
                    if s3_source1_url:
                        infused_image_urls["source_image_1"] = s3_source1_url
                        print(f"✅ Source image 1 uploaded: {s3_source1_url['s3_direct_url'][:50]}...")
                        
                        # Save metadata immediately after successful upload
                        update_job_status(job_id, "processing", 22, "Source image 1 uploaded to S3", {
                            "job_type": "infusion",
                            "infused_image_urls": infused_image_urls,
                            "total_infused_images": len(infused_image_urls)
                        })
                    else:
                        print(f"❌ Failed to upload source image 1")
                else:
                    print(f"✅ Source image 1 already uploaded, skipping")
                
                # Upload source image 2 only if not already uploaded
                if "source_image_2" not in infused_image_urls:
                    print(f"📤 Uploading source image 2...")
                    s3_source2_url = upload_infused_image_to_s3(job_id, source2_local, "source_image_2")
                    if s3_source2_url:
                        infused_image_urls["source_image_2"] = s3_source2_url
                        print(f"✅ Source image 2 uploaded: {s3_source2_url['s3_direct_url'][:50]}...")
                        
                        # Save metadata immediately after successful upload
                        update_job_status(job_id, "processing", 23, "Source image 2 uploaded to S3", {
                            "job_type": "infusion",
                            "infused_image_urls": infused_image_urls,
                            "total_infused_images": len(infused_image_urls)
                        })
                    else:
                        print(f"❌ Failed to upload source image 2")
                else:
                    print(f"✅ Source image 2 already uploaded, skipping")
                
                # Final status update for source images processing
                if infused_image_urls:
                    final_status_msg = f"Source images processed for S3 ({len(infused_image_urls)} total images)"
                    update_job_status(job_id, "processing", 24, final_status_msg, {
                        "job_type": "infusion",
                        "infused_image_urls": infused_image_urls,
                        "total_infused_images": len(infused_image_urls)
                    })
                    print(f"🔗 Total infused images: {len(infused_image_urls)}")
            
            except Exception as source_upload_error:
                print(f"⚠️ Failed to process source images: {source_upload_error}")
        else:
            print(f"✅ Both source images already uploaded, skipping upload step")
            update_job_status(job_id, "processing", 24, "Source images already uploaded, continuing...")
        
        # Step 3: Generate fused image using Gemini
        update_job_status(job_id, "processing", 25, "Generating fused image with Gemini...")
        print(f"🎨 Generating fused image with Gemini using prompt: {fusion_prompt[:100]}...")
        
        fused_image_path = os.path.join(job_dir, "fused_image.png")
        
        # Set environment override for fusion prompt
        original_prompt = os.environ.get("GEMINI_PROMPT_OVERRIDE")
        os.environ["GEMINI_PROMPT_OVERRIDE"] = fusion_prompt
        
        try:
            if not mapapi:
                raise RuntimeError("mapapi module not available for image generation")
            
            # Generate fused image
            gemini_result = mapapi.generate_image_with_gemini(
                objects_description="",  # Will use prompt override
                reference_image_path="",  # No reference for fusion
                output_path=fused_image_path
            )
            
            # Handle both tuple and single return formats
            if isinstance(gemini_result, tuple):
                final_fused_image_path, content_type = gemini_result
            else:
                final_fused_image_path = gemini_result
            
            if not final_fused_image_path or not os.path.exists(final_fused_image_path):
                raise RuntimeError("Failed to generate fused image with Gemini")
            
            print(f"✅ Fused image generated: {final_fused_image_path}")
            
            # Upload fused image to S3 (only if not already uploaded)
            print(f"📤 Checking if fused image needs to be uploaded to S3...")
            
            # Check if fused image already uploaded
            if "fused_image" not in infused_image_urls:
                print(f"📤 Uploading fused image to S3...")
                s3_fused_url = upload_infused_image_to_s3(job_id, final_fused_image_path, "fused_image")
                
                if s3_fused_url:
                    infused_image_urls["fused_image"] = s3_fused_url
                    
                    # Update job status with fused image URL and save to metadata
                    update_job_status(job_id, "processing", 30, "Fused image generated and uploaded to S3", {
                        "s3_gemini_url": s3_fused_url,  # Keep for backward compatibility
                        "fused_image_local": final_fused_image_path,
                        "job_type": "infusion",
                        "infused_image_urls": infused_image_urls,
                        "total_infused_images": len(infused_image_urls)
                    })
                    print(f"✅ Fused image uploaded to S3: {s3_fused_url.get('s3_direct_url', 'N/A')[:50]}...")
                else:
                    print(f"❌ Failed to upload fused image to S3")
                    update_job_status(job_id, "processing", 30, "Fused image generated (S3 upload failed)", {
                        "s3_gemini_url": None,
                        "fused_image_local": final_fused_image_path,
                        "job_type": "infusion"
                    })
            else:
                print(f"✅ Fused image already uploaded to S3, skipping upload")
                update_job_status(job_id, "processing", 30, "Fused image already uploaded, continuing...", {
                    "fused_image_local": final_fused_image_path,
                    "job_type": "infusion",
                    "infused_image_urls": infused_image_urls,
                    "total_infused_images": len(infused_image_urls)
                })
                
        finally:
            # Restore original prompt
            if original_prompt:
                os.environ["GEMINI_PROMPT_OVERRIDE"] = original_prompt
            elif "GEMINI_PROMPT_OVERRIDE" in os.environ:
                del os.environ["GEMINI_PROMPT_OVERRIDE"]
        
        # Step 4: Optimize fused image for 3D generation
        update_job_status(job_id, "processing", 32, "Optimizing fused image for 3D generation...")
        print(f"🎯 Optimizing fused image for 3D processing...")
        
        # Generate 3D-optimized version of the fused image
        optimized_fused_image_path = os.path.join(job_dir, "fused_image_3d_optimized.jpg")
        
        try:
            if not mapapi:
                raise RuntimeError("mapapi module not available for 3D optimization")
            
            # Use mapapi's 3D optimization process for the fused image
            print(f"🔧 Running 3D optimization on fused image...")
            optimization_result = mapapi.optimize_image_for_3d_generation(
                input_image_path=final_fused_image_path,
                output_path=optimized_fused_image_path,
                enhancement_prompt=fusion_prompt  # Use the fusion prompt for optimization context
            )
            
            if optimization_result and os.path.exists(optimized_fused_image_path):
                print(f"✅ Fused image optimized for 3D: {optimized_fused_image_path}")
                
                # Upload optimized fused image to S3 (only if not already uploaded)
                if "fused_image_3d_optimized" not in infused_image_urls:
                    print(f"📤 Uploading 3D-optimized fused image to S3...")
                    s3_optimized_fused_url = upload_infused_image_to_s3(job_id, optimized_fused_image_path, "fused_image_3d_optimized")
                    
                    if s3_optimized_fused_url:
                        infused_image_urls["fused_image_3d_optimized"] = s3_optimized_fused_url
                        
                        # Update job status with optimized image URL and save to metadata
                        update_job_status(job_id, "processing", 34, "Fused image optimized for 3D and uploaded to S3", {
                            "s3_optimized_url": s3_optimized_fused_url,  # Set mandatory s3_optimized_url
                            "fused_image_optimized_local": optimized_fused_image_path,
                            "job_type": "infusion",
                            "infused_image_urls": infused_image_urls,
                            "total_infused_images": len(infused_image_urls)
                        })
                        print(f"✅ 3D-optimized fused image uploaded to S3: {s3_optimized_fused_url.get('s3_direct_url', 'N/A')[:50]}...")
                    else:
                        print(f"❌ Failed to upload 3D-optimized fused image to S3")
                        update_job_status(job_id, "processing", 34, "Fused image optimized (S3 upload failed)", {
                            "s3_optimized_url": None,
                            "fused_image_optimized_local": optimized_fused_image_path,
                            "job_type": "infusion"
                        })
                else:
                    print(f"✅ 3D-optimized fused image already uploaded to S3, skipping upload")
                    update_job_status(job_id, "processing", 34, "3D-optimized image already uploaded, continuing...", {
                        "fused_image_optimized_local": optimized_fused_image_path,
                        "job_type": "infusion",
                        "infused_image_urls": infused_image_urls,
                        "total_infused_images": len(infused_image_urls)
                    })
                    
                    # Use the optimized image for 3D model generation
                    final_image_for_3d = optimized_fused_image_path
            else:
                print(f"⚠️ 3D optimization failed, using original fused image for 3D generation")
                final_image_for_3d = final_fused_image_path
                
        except Exception as optimization_error:
            print(f"⚠️ 3D optimization failed: {optimization_error}")
            print(f"📋 Using original fused image for 3D generation")
            final_image_for_3d = final_fused_image_path
        
        # Step 5: Generate 3D model from optimized fused image
        update_job_status(job_id, "processing", 35, "Converting optimized fused image to 3D model...")
        print(f"🎯 Generating 3D model from optimized fused image...")
        
        loop = asyncio.get_event_loop()
        model_path = await loop.run_in_executor(
            thread_pool,
            generate_3d_model_from_image_sync,
            final_image_for_3d,  # Use the optimized image
            job_id
        )
        
        if not model_path or not os.path.exists(model_path):
            raise RuntimeError("3D model generation from optimized fused image failed")
        
        # Step 6: Upload model to S3
        update_job_status(job_id, "processing", 50, "Uploading fused 3D model to cloud storage...")
        
        def _upload_model_to_s3():
            timestamp = int(time.time())
            filename = f"fused_{job_id}_{timestamp}_{os.path.basename(model_path)}"
            return upload_glb_to_s3_with_urls(model_path, filename)
        
        upload_result = await loop.run_in_executor(thread_pool, _upload_model_to_s3)
        
        if not upload_result:
            raise RuntimeError("Failed to upload fused model to S3")
        
        presigned_url = upload_result["presigned_url"]
        s3_direct_url = upload_result["s3_url"]
        
        # Update job status with S3 URLs
        update_job_status(job_id, "processing", 55, "Fused model uploaded to S3", {
            "s3_upload": {
                "presigned_url": presigned_url,
                "s3_direct_url": s3_direct_url,
                "s3_key": upload_result["s3_key"],
                "upload_timestamp": upload_result["upload_timestamp"]
            },
            "job_type": "infusion"
        })
        
        # Update the 3d_model_output.json with S3 URLs
        update_3d_model_metadata(job_id, upload_result)
        
        # Step 7: Retexture (optional)
        rigging_model_url = presigned_url
        if use_retexture:
            update_job_status(job_id, "processing", 60, "Enhancing fused model textures...")
            
            def _retexture():
                from test3danimate import retexture_with_meshy, RETEXTURE_PROMPT
                return retexture_with_meshy(presigned_url, RETEXTURE_PROMPT, job_id)
            
            rigging_model_url = await loop.run_in_executor(thread_pool, _retexture)
        else:
            update_job_status(job_id, "processing", 60, "Skipping texture enhancement for fused model")
        
        # Step 8: Async Rigging and Animation (same as regular pipeline)
        if animation_ids:
            print(f"[{job_id}] 🤖 Starting async rigging for fused model...")
            rig_result = await async_rigging_process(rigging_model_url, height_meters, job_id)
            
            # Check if rigging failed
            rigging_failed = rig_result.get("rigging_failed", False)
            
            if rigging_failed:
                # Complete job without animations
                update_job_status(job_id, "completed", 100, "Infusion completed successfully (rigging failed, no animations)", {
                    "animations": [],
                    "total_animations": 0,
                    "storage_type": "database_s3_only",
                    "final_textured_glb": None,
                    "raw_anim_glb": None,
                    "raw_anim_fbx": None,
                    "processing_method": "infusion_pipeline_rigging_failed",
                    "model_s3_url": s3_direct_url,
                    "rigging_status": "failed",
                    "rigging_error": rig_result.get("error_message", "Unknown rigging error"),
                    "job_type": "infusion",
                    "fusion_data": fusion_data
                })
                print(f"[{job_id}] 🎉 Infusion job completed successfully despite rigging failure!")
                return
            
            # Continue with animations using database-only storage
            rig_glb_url = rig_result.get("rigged_character_glb_url")
            
            if rig_glb_url:
                # Store rigged model information in database
                update_job_status(job_id, "processing", 80, "Storing rigged fused model information...")
                
                def _store_fused_rigged():
                    """Store fused rigged model info in database"""
                    try:
                        rigged_info = {
                            "rigged_character_glb_url": rig_glb_url,
                            "rigged_character_fbx_url": rig_result.get("rigged_character_fbx_url"),
                            "storage_type": "s3_url_only",
                            "job_type": "infusion",
                            "stored_at": time.time()
                        }
                        
                        update_job_status(job_id, "processing", 85, "Fused rigged model stored", {
                            "fused_rigged_models": rigged_info
                        })
                        
                        print(f"[{job_id}] ✅ Stored fused rigged model URLs in database")
                        return rig_glb_url
                        
                    except Exception as e:
                        print(f"[{job_id}] ❌ Error storing fused rigged model: {e}")
                        return None
                
                rig_glb_url_stored = await loop.run_in_executor(thread_pool, _store_fused_rigged)
                
                # Generate animations using URL directly
                if rig_glb_url_stored:
                    print(f"[{job_id}] 🎬 Starting async animations for fused model...")
                    animations_result = await async_animation_process(rig_glb_url_stored, animation_ids, fps, job_id)
                else:
                    animations_result = []
            else:
                animations_result = []
        else:
            animations_result = []
        
        # Step 9: Complete infusion job
        update_job_status(job_id, "completed", 100, "Infusion pipeline completed successfully!", {
            "animations": animations_result,
            "total_animations": len(animations_result),
            "storage_type": "database_s3_only",
            "final_textured_glb_url": rig_glb_url_stored if 'rig_glb_url_stored' in locals() else None,
            "raw_anim_glb": animations_result[0].get("raw_anim_glb") if animations_result else None,
            "raw_anim_fbx": animations_result[0].get("raw_anim_fbx") if animations_result else None,
            "processing_method": "infusion_pipeline_with_3d_optimization",
            "model_s3_url": s3_direct_url,
            "rigging_status": "succeeded" if animation_ids else "skipped",
            "job_type": "infusion",
            "fusion_data": fusion_data,
            "fused_image_path": final_fused_image_path,
            "optimized_fused_image_path": final_image_for_3d
        })
        
        print(f"[{job_id}] 🎉 Infusion pipeline completed successfully!")
        
    except Exception as e:
        update_job_status(job_id, "failed", 0, f"Infusion pipeline failed: {str(e)}", {
            "job_type": "infusion",
            "error": str(e)
        })
        print(f"❌ [Job {job_id}] Infusion pipeline error: {e}")

# ========= CORE PIPELINE FUNCTION =========
async def run_animation_pipeline(
    job_id: str,
    input_image_path: str,
    animation_ids: List[int],
    height_meters: float,
    fps: int,
    use_retexture: bool,
    skip_confirmation: bool = False
):
    """Core pipeline that processes input image through complete 3D animation workflow with async rigging."""
    try:
        print(f"🚀 [Job {job_id}] Starting ASYNC animation pipeline with non-blocking rigging!")
        
        # Use the new async pipeline which doesn't block other requests during rigging
        await async_full_animation_pipeline(
            image_path=input_image_path,
            prompt="3D model from uploaded image",
            animation_actions=animation_ids,
            height_meters=height_meters,
            fps=fps,
            use_retexture=use_retexture,
            job_id=job_id,
            skip_confirmation=skip_confirmation
        )
        
        print(f"✅ [Job {job_id}] Async pipeline completed - rigging and animations were non-blocking!")
        
    except Exception as e:
        update_job_status(job_id, "failed", 0, f"Pipeline failed: {str(e)}")
        print(f"❌ [Job {job_id}] Pipeline error: {e}")

async def async_full_animation_pipeline(
    image_path: str, 
    prompt: str, 
    animation_actions: List[int] = None,
    height_meters: float = 1.8,
    fps: int = 30,
    use_retexture: bool = True,
    job_id: str = None,
    skip_confirmation: bool = False
) -> None:
    """Async version of full animation pipeline for non-blocking execution.
    
    Args:
        image_path: Path to input image
        prompt: Text prompt for generation
        animation_actions: List of animation IDs to apply
        height_meters: Character height for rigging
        fps: Animation frame rate
        use_retexture: Whether to enhance textures
        job_id: Job identifier
        skip_confirmation: If True, skip confirmation checkpoint (useful for workflows)
    """
    if not job_id:
        job_id = str(uuid.uuid4())
    
    print(f"🚀 Starting async animation pipeline for job {job_id}")
    if skip_confirmation:
        print(f"⚡ [WORKFLOW] Confirmation checkpoint will be skipped")
    
    try:
        # Step 1: Generate 3D Model (using thread pool)
        update_job_status(job_id, "processing", 10, "Generating 3D model from image...")
        
        loop = asyncio.get_event_loop()
        model_path = await loop.run_in_executor(
            thread_pool,
            generate_3d_model_from_image_sync,
            image_path,
            job_id,
            skip_confirmation  # Pass skip_confirmation parameter
        )
        
        if not model_path or not os.path.exists(model_path):
            raise RuntimeError("3D model generation failed")
        
        # Step 2: Upload to S3 (using thread pool) 
        update_job_status(job_id, "processing", 25, "Uploading model to cloud storage...")
        
        def _upload_to_s3():
            timestamp = int(time.time())
            filename = f"{job_id}_{timestamp}_{os.path.basename(model_path)}"
            return upload_glb_to_s3_with_urls(model_path, filename)
        
        upload_result = await loop.run_in_executor(thread_pool, _upload_to_s3)
        
        if not upload_result:
            raise RuntimeError("Failed to upload model to S3")
        
        presigned_url = upload_result["presigned_url"]
        s3_direct_url = upload_result["s3_url"]
        
        # Update job status with S3 URLs
        update_job_status(job_id, "processing", 30, "Model uploaded to S3", {
            "s3_upload": {
                "presigned_url": presigned_url,
                "s3_direct_url": s3_direct_url,
                "s3_key": upload_result["s3_key"],
                "upload_timestamp": upload_result["upload_timestamp"]
            }
        })
        
        # Update the 3d_model_output.json with S3 URLs
        update_3d_model_metadata(job_id, upload_result)
        
        # Check if we should skip rigging (no animations requested)
        skip_rigging = not animation_actions or len(animation_actions) == 0
        
        if skip_rigging:
            print(f"[{job_id}] ⚡ Skipping rigging and animations (no animation_actions specified)")
            update_job_status(job_id, "completed", 100, "3D model generation completed (rigging skipped)", {
                "model_url": presigned_url,
                "glb_url": presigned_url,
                "s3_direct_url": s3_direct_url,
                "animation_urls": [],  # Changed from "animations" to "animation_urls" to match workflow expectations
                "total_animations": 0,
                "storage_type": "s3_only",
                "processing_method": "async_no_rigging",
                "rigging_status": "skipped",
                "status": "completed",  # Add explicit status field
                "note": "Rigging and animations were skipped as requested. 3D model is ready for download."
            })
            print(f"[{job_id}] ✅ Job completed successfully without rigging!")
            return
        
        # Step 3: Retexture (optional, using thread pool)
        rigging_model_url = presigned_url
        if use_retexture:
            update_job_status(job_id, "processing", 40, "Enhancing textures...")
            
            def _retexture():
                from test3danimate import retexture_with_meshy, RETEXTURE_PROMPT
                return retexture_with_meshy(presigned_url, RETEXTURE_PROMPT, job_id)
            
            rigging_model_url = await loop.run_in_executor(thread_pool, _retexture)
        else:
            update_job_status(job_id, "processing", 40, "Skipping texture enhancement")
            print(f"[{job_id}] ⚪ Retexturing disabled - using original model for rigging")
        
        # Step 4: Async Rigging (NON-BLOCKING!)
        print(f"[{job_id}] 🤖 Starting async rigging - other requests can process concurrently!")
        rig_result = await async_rigging_process(rigging_model_url, height_meters, job_id)
        
        # Check if rigging failed
        rigging_failed = rig_result.get("rigging_failed", False)
        
        if rigging_failed:
            # Rigging failed, but continue with job completion
            print(f"[{job_id}] ⚠️ Rigging failed, but continuing job completion...")
            
            # Step 5: Complete job without animations
            update_job_status(job_id, "completed", 100, "Job completed successfully (rigging failed, no animations created)", {
                "animations": [],
                "total_animations": 0,
                "storage_type": "database_s3_only",
                "final_textured_glb": None,
                "raw_anim_glb": None,
                "raw_anim_fbx": None,
                "processing_method": "async_non_blocking_rigging_failed",
                "model_s3_url": s3_direct_url,
                "rigging_status": "failed",
                "rigging_error": rig_result.get("error_message", "Unknown rigging error"),
                "note": "3D model was generated successfully, but rigging failed. Model is available for download."
            })
            
            print(f"[{job_id}] 🎉 Job completed successfully despite rigging failure!")
            return  # Exit early
        
        # Rigging succeeded - continue with normal flow
        rig_fbx_url = rig_result.get("rigged_character_fbx_url")
        rig_glb_url = rig_result.get("rigged_character_glb_url")
        
        # Store rigged models in database (no local download)
        update_job_status(job_id, "processing", 60, "Storing rigged model information...")
        
        def _store_rigged_models():
            """Store rigged model URLs in database instead of downloading locally"""
            try:
                # Store rigged model information in database
                rigged_info = {
                    "rigged_character_fbx_url": rig_fbx_url,
                    "rigged_character_glb_url": rig_glb_url,
                    "storage_type": "s3_url_only",
                    "stored_at": time.time()
                }
                
                # Update job in database with rigged model info
                update_job_status(job_id, "processing", 65, "Rigged model information stored", {
                    "rigged_models": rigged_info
                })
                
                print(f"[{job_id}] ✅ Stored rigged model URLs in database")
                return rig_glb_url  # Return URL instead of local path
                
            except Exception as e:
                print(f"[{job_id}] ❌ Error storing rigged model info: {e}")
                return None
        
        rig_glb_url_stored = await loop.run_in_executor(thread_pool, _store_rigged_models)
        
        # Step 5: Async Animations (NON-BLOCKING!)
        if animation_actions and rig_glb_url_stored:
            print(f"[{job_id}] 🎬 Starting async animations - other requests can process concurrently!")
            animations_result = await async_animation_process(rig_glb_url_stored, animation_actions, fps, job_id)
        else:
            animations_result = []
            if not rig_glb_url_stored:
                print(f"[{job_id}] ⚠️ No rigged model URL available for animations")
        
        # Step 6: Complete
        update_job_status(job_id, "completed", 100, "Async animation pipeline completed successfully!", {
            "animations": animations_result,
            "total_animations": len(animations_result),
            "final_textured_glb_url": rig_glb_url_stored,  # Store URL instead of path
            "raw_anim_glb": animations_result[0].get("raw_anim_glb") if animations_result else None,
            "raw_anim_fbx": animations_result[0].get("raw_anim_fbx") if animations_result else None,
            "processing_method": "async_non_blocking_database_only",
            "model_s3_url": s3_direct_url,  # Include the direct S3 URL in final result
            "rigging_status": "succeeded",
            "storage_type": "database_s3_only"
        })
        
        print(f"[{job_id}] 🎉 Async pipeline completed successfully!")
        
    except Exception as e:
        update_job_status(job_id, "failed", 0, f"Async pipeline failed: {str(e)}")
        print(f"[{job_id}] ❌ Async pipeline error: {e}")

def generate_3d_model_from_image_sync(image_path: str, job_id: str = None, skip_confirmation: bool = False) -> str:
    """Synchronous version for thread pool execution.
    
    Args:
        image_path: Path to input image
        job_id: Optional job ID for organizing output files
        skip_confirmation: If True, skip confirmation checkpoint and proceed directly to GLB generation
                          (useful for workflow automation where confirmation is not needed)
        
    Returns:
        Path to generated GLB file
    """
    try:
        if not mapapi:
            raise RuntimeError("mapapi module not available")
        
        print(f"🎯 [Thread {threading.current_thread().name}] Generating 3D model from image: {image_path}")
        
        # Clean up any existing files to ensure fresh generation
        cleanup_existing_files()
        
        # Determine output path - use job-specific folder if job_id provided
        timestamp = int(time.time())
        if job_id:
            job_models_dir = f"jobs/{job_id}/models"
            os.makedirs(job_models_dir, exist_ok=True)
            expected_glb = os.path.join(job_models_dir, f"{job_id}_{timestamp}_3d_model.glb")
            print(f"📁 Job-specific output: {expected_glb}")
        else:
            expected_glb = f"streetview_{timestamp}_3d_model.glb"
            print(f"📁 Default output: {expected_glb}")
        
        # Run the 3D generation pipeline
        try:
            print("🚀 Running 3D generation pipeline...")
            
            # Step 1: Analyze the input image with Llama 3.2 Vision
            print("🔍 Analyzing input image for 3D objects...")
            objects_analysis = mapapi.analyze_image_with_llama(image_path)
            print(f"📋 Objects identified: {objects_analysis}")
            
            # Step 2: Extract and clean 3D-worthy objects
            clean_objects = mapapi.extract_3d_objects(objects_analysis)
            print(f"🏗️ 3D objects for modeling: {clean_objects}")
            
            # Step 3: Generate 3D GLB using Segmind Hunyuan3D
            print("🎨 Converting image to 3D model...")
            
            # CONFIRMATION CHECKPOINT: Ask user before proceeding with expensive GLB generation
            # Skip if skip_confirmation is True (e.g., when called from workflow automation)
            if job_id and not skip_confirmation:
                print(f"⏸️ Requesting confirmation before GLB generation for job {job_id}")
                
                # Get current job result to check available images
                current_job_result = jobs.get(job_id, {}).get("result", {}) if job_id in jobs else {}
                
                # Count available images for confirmation message
                available_images = []
                if current_job_result.get("gemini_image_local"):
                    available_images.append("AI-generated image")
                if current_job_result.get("optimized_3d_image_local"):
                    available_images.append("3D-optimized image")
                if current_job_result.get("edited_image_local"):
                    available_images.append("user-edited image")
                
                image_options_text = ""
                if len(available_images) > 1:
                    image_options_text = f" You can choose from {len(available_images)} image options: {', '.join(available_images)}."
                elif len(available_images) == 1:
                    image_options_text = f" Using {available_images[0]} for 3D generation."
                
                confirmation_message = f"Image analysis complete. Ready to generate 3D model.{image_options_text} This will use API credits. Continue?"
                
                update_job_status(job_id, "waiting_for_confirmation", 75, 
                    confirmation_message, {
                    "objects_analysis": objects_analysis,
                    "clean_objects": clean_objects,
                    "estimated_cost": "~$0.10-0.50 depending on complexity",
                    "processing_time": "~2-5 minutes",
                    "confirmation_required": True,
                    "available_images": available_images,
                    "image_count": len(available_images)
                })
                
                # Return a special status to indicate waiting for confirmation
                return "WAITING_FOR_CONFIRMATION"
            
            # Skip confirmation - proceed directly with GLB generation
            if skip_confirmation:
                print(f"⚡ [WORKFLOW] Skipping confirmation checkpoint - proceeding directly with GLB generation")
            
            # Use mapapi's 3D generation parameters
            gen_params = {
                "seed": int(os.getenv("SEED", "42")),
                "steps": int(os.getenv("STEPS", "30")),
                "num_chunks": int(os.getenv("NUM_CHUNKS", "8000")),
                "max_facenum": int(os.getenv("MAX_FACENUM", "20000")),
                "guidance_scale": float(os.getenv("GUIDANCE_SCALE", "7.5")),
                "generate_texture": os.getenv("GENERATE_TEXTURE", "true").lower() == "true",
                "octree_resolution": int(os.getenv("OCTREE_RESOLUTION", "256")),
                "remove_background": os.getenv("REMOVE_BACKGROUND", "true").lower() == "true",
                "texture_resolution": int(os.getenv("TEXTURE_RESOLUTION", "4096")),
                "bake_normals": os.getenv("BAKE_NORMALS", "true").lower() == "true",
                "bake_ao": os.getenv("BAKE_AO", "true").lower() == "true",
            }
            
            # Headers for Segmind API
            headers = {
                "x-api-key": os.getenv("SEGMIND_API_KEY"),
                "Accept": "application/octet-stream"
            }
            
            segmind_endpoint = os.getenv("SEGMIND_ENDPOINT", "https://api.segmind.com/v1/hunyuan3d-2.1")
            
            # Get MIME type for the image
            import mimetypes
            mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            
            # Validate image before upload
            print(f"🔍 Validating image for 3D generation: {image_path}")
            if not validate_image_for_3d_generation(image_path):
                raise RuntimeError("Image validation failed - unsuitable for 3D generation")
            
            # Upload image and generate 3D model
            print(f"📤 Uploading image to Segmind for 3D generation...")
            with open(image_path, "rb") as imgf:
                # Validate image file before upload
                img_content = imgf.read()
                if len(img_content) == 0:
                    raise RuntimeError("Image file is empty")
                
                print(f"📊 Image validation: {os.path.basename(image_path)}, size: {len(img_content):,} bytes")
                
                # Reset file pointer
                imgf.seek(0)
                files = {"image": (os.path.basename(image_path), imgf, mime)}
                data_form = {k: v for k, v in gen_params.items()}
                
                # Use retry wrapper for better reliability
                success, meta = mapapi.post_hunyuan_to_file_with_retry(
                    segmind_endpoint,
                    headers=headers,
                    data=data_form,
                    files=files,
                    out_path=expected_glb,
                    max_retries=3
                )
            
            if success and os.path.exists(expected_glb):
                print(f"✅ Successfully generated 3D model: {expected_glb}")
                
                # Clean up temporary files from origin folder if we used job-specific folder
                if job_id:
                    temp_files_to_clean = [
                        "streetview_3d_model.glb",  # Remove any accidentally created in origin
                        "3d_model_output.json"     # Metadata will be saved in job folder
                    ]
                    
                    for temp_file in temp_files_to_clean:
                        if os.path.exists(temp_file):
                            try:
                                os.remove(temp_file)
                                print(f"🧹 Cleaned up: {temp_file}")
                            except Exception as e:
                                print(f"⚠️ Could not remove {temp_file}: {e}")
                
                # Save metadata in appropriate location
                metadata = {
                    "method": "image_to_3d_pipeline", 
                    "source_image": image_path,
                    "objects_analysis": objects_analysis,
                    "clean_objects": clean_objects,
                    "params": gen_params,
                    "file": expected_glb,
                    "job_id": job_id,
                    "thread": threading.current_thread().name
                }
                
                # Save metadata in job folder if job_id provided, otherwise root
                if job_id:
                    metadata_path = f"jobs/{job_id}/3d_model_output.json"
                else:
                    metadata_path = "3d_model_output.json"
                
                with open(metadata_path, "w") as f:
                    json.dump(metadata, f, indent=2)
                
                print(f"📁 Model generated: {expected_glb}")
                if job_id:
                    print(f"🧹 Original folder cleaned up")
                    
                return os.path.abspath(expected_glb)
            else:
                print(f"❌ 3D generation failed: {meta}")
                raise RuntimeError(f"Segmind 3D generation failed: {meta}")
                
        except Exception as e:
            print(f"❌ Error in 3D generation pipeline: {e}")
            raise RuntimeError(f"Failed to generate 3D model: {e}")
        
    except Exception as e:
        print(f"Error generating 3D model from image: {e}")
        return None

async def generate_3d_model_from_image(image_path: str, job_id: str = None, skip_confirmation: bool = False) -> str:
    """Async wrapper for 3D model generation that runs in thread pool.
    
    Args:
        image_path: Path to input image
        job_id: Optional job ID for organizing output files
        skip_confirmation: If True, skip confirmation checkpoint and proceed directly to GLB generation
        
    Returns:
        Path to generated GLB file
    """
    loop = asyncio.get_event_loop()
    
    # Run the synchronous function in thread pool
    try:
        result = await loop.run_in_executor(
            thread_pool,
            generate_3d_model_from_image_sync,
            image_path,
            job_id,
            skip_confirmation
        )
        return result
    except Exception as e:
        print(f"❌ Thread pool execution error: {e}")
        return None

async def run_mapapi_generation() -> bool:
    """Run mapapi to generate 3D model from image."""
    try:
        import subprocess
        import asyncio
        
        # Run mapapi.py as a subprocess
        print("🚀 Executing mapapi.py...")
        
        # Try python3 first, then python
        python_cmd = "python3"
        try:
            subprocess.run([python_cmd, "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            python_cmd = "python"
        
        result = subprocess.run(
            [python_cmd, "mapapi.py"],
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
        
        if result.returncode == 0:
            print("✅ mapapi.py completed successfully")
            print(f"STDOUT: {result.stdout[-500:]}")  # Show last 500 chars
            return True
        else:
            print(f"❌ mapapi.py failed with return code {result.returncode}")
            print(f"STDOUT: {result.stdout}")
            print(f"STDERR: {result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        print("❌ mapapi.py timed out after 5 minutes")
        return False
    except Exception as e:
        print(f"❌ Error running mapapi.py: {e}")
        return False

# ========= ASYNC HELPER FUNCTIONS FOR NON-BLOCKING OPERATIONS =========

async def async_get_json(url: str) -> dict:
    """Async version of get_json using asyncio with requests (fallback for aiohttp)."""
    import requests
    
    # Run blocking request in thread pool to make it non-blocking
    loop = asyncio.get_event_loop()
    
    def _get_request():
        try:
            from test3danimate import get_json
            return get_json(url)
        except Exception as e:
            print(f"Error in async_get_json: {e}")
            raise
    
    return await loop.run_in_executor(None, _get_request)

async def async_post_json(url: str, data: dict) -> str:
    """Async version of post_json using asyncio with requests (fallback for aiohttp)."""
    import requests
    
    # Run blocking request in thread pool to make it non-blocking
    loop = asyncio.get_event_loop()
    
    def _post_request():
        try:
            from test3danimate import post_json
            return post_json(url, data)
        except Exception as e:
            print(f"Error in async_post_json: {e}")
            raise
    
    return await loop.run_in_executor(None, _post_request)

async def async_poll_task(task_url: str, label: str, interval: float = 2.0, job_id: str = None):
    """Async version of poll_task for non-blocking task polling."""
    while True:
        data = await async_get_json(task_url)
        status = data.get("status")
        progress = data.get("progress", 0)
        print(f"[{job_id}] {label}: {status} ({progress}%)")
        
        # Update job status if job_id provided
        if job_id:
            update_job_status(job_id, "processing", min(progress, 99), f"{label}: {status} ({progress}%)")
        
        if status in ("SUCCEEDED", "FAILED"):
            return data
        
        # Use asyncio.sleep for non-blocking sleep
        await asyncio.sleep(interval)

async def async_rigging_process(model_url: str, height_meters: float, job_id: str) -> dict:
    """Async rigging process that doesn't block other requests."""
    try:
        print(f"[{job_id}] 🤖 Starting async rigging process...")
        
        # Step 1: Submit rigging task
        update_job_status(job_id, "processing", 45, "Creating character rig...")
        
        rig_task_id = await async_post_json(f"{API_V1}/rigging", {
            "model_url": model_url, 
            "height_meters": height_meters
        })
        
        print(f"[{job_id}] 🎯 Rigging task submitted: {rig_task_id}")
        
        # Step 2: Poll for completion (non-blocking)
        rig_task = await async_poll_task(f"{API_V1}/rigging/{rig_task_id}", "Rigging", 2.0, job_id)
        
        if rig_task.get("status") != "SUCCEEDED":
            err = rig_task.get("task_error", {})
            error_message = err.get('message', str(err))
            
            # Check for specific pose estimation failure
            if "Pose estimation failed" in error_message:
                print(f"[{job_id}] ⚠️ Rigging failed due to pose estimation - continuing without rigging")
                update_job_status(job_id, "processing", 60, "Rigging failed (pose estimation), continuing without animations...", {
                    "rigging_status": "failed", 
                    "rigging_error": "Pose estimation failed - model may not be suitable for rigging",
                    "rigging_note": "The 3D model was generated successfully, but automatic rigging failed. The model can still be used for other purposes."
                })
                
                # Return a special result indicating rigging failure
                return {
                    "rigging_failed": True,
                    "error_type": "pose_estimation_failed",
                    "error_message": error_message,
                    "original_model_url": model_url
                }
            else:
                # For other rigging errors, still continue but with different message
                print(f"[{job_id}] ⚠️ Rigging failed: {error_message}")
                update_job_status(job_id, "processing", 60, f"Rigging failed ({error_message[:50]}...), continuing without animations...", {
                    "rigging_status": "failed",
                    "rigging_error": error_message,
                    "rigging_note": "The 3D model was generated successfully, but rigging failed. The model can still be used for other purposes."
                })
                
                return {
                    "rigging_failed": True,
                    "error_type": "general_rigging_error",
                    "error_message": error_message,
                    "original_model_url": model_url
                }
        
        print(f"[{job_id}] ✅ Rigging completed successfully")
        return rig_task["result"]
        
    except Exception as e:
        print(f"[{job_id}] ❌ Async rigging error: {e}")
        
        # Handle rigging errors gracefully - don't fail the entire job
        update_job_status(job_id, "processing", 60, f"Rigging encountered an error, continuing without animations...", {
            "rigging_status": "failed",
            "rigging_error": str(e),
            "rigging_note": "The 3D model was generated successfully, but rigging encountered an error. The model can still be used for other purposes."
        })
        
        return {
            "rigging_failed": True,
            "error_type": "rigging_exception",
            "error_message": str(e),
            "original_model_url": model_url
        }

async def async_animation_process(rig_glb_url: str, animation_actions: list, fps: int, job_id: str) -> list:
    """Async animation process that doesn't block other requests - Database only."""
    try:
        print(f"[{job_id}] 🎬 Starting async animation process with database-only storage...")
        animations_result = []
        
        for action_id in animation_actions:
            try:
                update_job_status(job_id, "processing", 70 + (action_id * 5), f"Creating animation {action_id}...")
                
                # Submit animation task using the rigged model URL directly
                anim_task_id = await async_post_json(f"{API_V1}/animations", {
                    "model_url": rig_glb_url,  # Use URL directly, no local download
                    "action_id": action_id,
                    "post_process": {"operation_type": "change_fps", "fps": fps}
                })
                
                print(f"[{job_id}] 🎯 Animation {action_id} task submitted: {anim_task_id}")
                
                # Poll for completion (non-blocking)
                anim_task = await async_poll_task(f"{API_V1}/animations/{anim_task_id}", f"Animation {action_id}", 2.0, job_id)
                
                if anim_task.get("status") != "SUCCEEDED":
                    print(f"[{job_id}] ❌ Animation {action_id} failed:", anim_task.get("task_error"))
                    continue
                
                anim_res = anim_task["result"]
                
                # Store animation URLs in database without downloading
                animation_info = {
                    "action_id": action_id,
                    "raw_anim_glb": anim_res.get("animation_glb_url"),
                    "raw_anim_fbx": anim_res.get("animation_fbx_url"),
                    "storage_type": "s3_url_only",
                    "created_at": time.time(),
                    "fps": fps
                }
                
                animations_result.append(animation_info)
                
                # Update job database with animation progress
                update_job_status(job_id, "processing", 75 + (len(animations_result) * 5), 
                                f"Animation {action_id} completed", {
                    "completed_animations": animations_result
                })
                
                print(f"[{job_id}] ✅ Animation {action_id} completed and stored in database")
                
            except Exception as e:
                print(f"[{job_id}] ❌ Error processing animation {action_id}: {e}")
                continue
        
        print(f"[{job_id}] ✅ All animations completed: {len(animations_result)} animations stored in database")
        return animations_result
        
    except Exception as e:
        print(f"[{job_id}] ❌ Async animation error: {e}")
        raise

# ========= CONFIRMATION HELPER FUNCTIONS =========

async def continue_glb_generation_after_confirmation(job_id: str, current_result: dict):
    """Continue GLB generation after user confirmation
    
    This function picks up the 3D model generation process after the user
    has confirmed they want to proceed with the expensive GLB generation step.
    
    Args:
        job_id: Job identifier
        current_result: Current job result data containing analysis information
    """
    try:
        print(f"🚀 Continuing GLB generation for job {job_id} after user confirmation")
        
        # Get analysis data from the current result
        objects_analysis = current_result.get("objects_analysis", "")
        clean_objects = current_result.get("clean_objects", "")
        
        if not objects_analysis:
            raise Exception("Missing analysis data - cannot continue GLB generation")
        
        # Update status
        update_job_status(job_id, "processing", 78, "Starting GLB generation with Segmind Hunyuan3D...")
        
        # Get job data from database using the jobs API
       
        # await db_service.ensure_connection()
        
        try:
            # Support both async and sync database service implementations.
            job_get = getattr(db_service, "get_job", None)
            if job_get is None:
                # Fallback to module-level service provider
                service = get_db_service()
                job_get = getattr(service, "get_job", None) if service else None

            if job_get is None:
                raise Exception("Database service does not provide get_job")

            # If get_job is an async function, await it; otherwise call it normally.
            if asyncio.iscoroutinefunction(job_get):
                job_data = await job_get(job_id)
            else:
                job_data = job_get(job_id)
                # In case it returns a coroutine-like object, await it as well.
                if asyncio.iscoroutine(job_data):
                    job_data = await job_data

            if not job_data:
                raise Exception(f"Job {job_id} not found in database")
        except Exception:
            raise
        
        # Extract image paths from job result data
        result_data = job_data.get("result", {})
        image_to_process = None
        
        # Build priority list from result_data (prefer local paths, fall back to common keys and S3 URLs)
        image_candidates = []

        def _append_if(val):
            if val:
                image_candidates.append(val)

        # Prefer local paths saved during processing
        _append_if(result_data.get("optimized_3d_image_local"))
        _append_if(result_data.get("optimized_3d_image_path"))
        _append_if(result_data.get("optimized_3d"))
        _append_if(result_data.get("gemini_image_local"))
        _append_if(result_data.get("gemini_generated_image_path"))
        _append_if(result_data.get("gemini_generated"))
        _append_if(result_data.get("edited_image_local"))
        _append_if(result_data.get("edited_image_path"))
        _append_if(result_data.get("input_image_path"))
        _append_if(result_data.get("input_image_local"))
        _append_if(result_data.get("source_image"))
        _append_if(result_data.get("source_image_local"))

        # Also include common S3 URL fields as last-resort (these are URLs, not local files)
        s3_gem = result_data.get("s3_gemini_url") or result_data.get("s3_gemini")
        if isinstance(s3_gem, dict):
            _append_if(s3_gem.get("s3_direct_url") or s3_gem.get("presigned_url"))
        elif isinstance(s3_gem, str):
            _append_if(s3_gem)

        s3_opt = result_data.get("s3_optimized_url") or result_data.get("s3_optimized")
        if isinstance(s3_opt, dict):
            _append_if(s3_opt.get("s3_direct_url") or s3_opt.get("presigned_url"))
        elif isinstance(s3_opt, str):
            _append_if(s3_opt)

        # Make sure image_candidates is defined (fallback empty list)
        if not image_candidates:
            image_candidates = []
        
        # Find the first existing image from candidates
        for img_path in image_candidates:
            if img_path:
                # Check if it's a local file that exists
                if not img_path.startswith(('http://', 'https://')) and os.path.exists(img_path):
                    image_to_process = img_path
                    print(f"✅ Found local image: {os.path.basename(img_path)}")
                    break
                # Check if it's an S3 URL (we'll download it later)
                elif img_path.startswith(('http://', 'https://')):
                    image_to_process = img_path
                    print(f"🌐 Found S3 URL: {img_path[:50]}...")
                    break
        
        # Helper to safely extract a usable URL from possible dict or string S3 entries
        def _extract_s3_url(obj):
            if isinstance(obj, dict):
                # Try common keys that may contain a usable URL
                return obj.get("presigned_url") or obj.get("s3_direct_url") or obj.get("s3_url") or obj.get("url")
            elif isinstance(obj, str):
                return obj
            return None

        # If no image found yet, try S3 URLs from result data
        if not image_to_process:
            s3_gem = result_data.get("s3_gemini_url") or result_data.get("s3_gemini") or result_data.get("generated_image_s3_url") 
            s3_opt = result_data.get("s3_optimized_url") or result_data.get("s3_optimized") or result_data.get("optimized_3d_image_s3_url")
            
            # Prefer optimized image URL, fallback to gemini image URL
            if s3_opt or s3_gem:
                image_to_process = _extract_s3_url(s3_opt) or _extract_s3_url(s3_gem)
                if image_to_process:
                    print(f"🌐 Using S3 URL from result data: {image_to_process[:50]}...")

        # Fallback: check job directory for common image files
        if not image_to_process:
            job_dir = f"jobs/{job_id}"
            fallback_images = [
                os.path.join(job_dir, "3d_optimized.jpg"),
                os.path.join(job_dir, "3d_optimized.png"),
                os.path.join(job_dir, "input_image.jpg"),
                os.path.join(job_dir, "input_image.png"),
                os.path.join(job_dir, "input_image.jpeg"),
                os.path.join(job_dir, "gemini_generated.png")
            ]
            
            for img_path in fallback_images:
                if os.path.exists(img_path):
                    image_to_process = img_path
                    print(f"⚠️ Using fallback image discovery for {job_id}: {os.path.basename(img_path)}")
                    break
        
        if not image_to_process:
            raise Exception("No suitable image found for GLB generation")
        
        # Log what type of image we're using
        if image_to_process.startswith(('http://', 'https://')):
            print(f"🎯 Using S3 URL for GLB generation: {image_to_process}")
        else:
            print(f"🎯 Using local image for GLB generation: {os.path.basename(image_to_process)}")
        
        # Continue with GLB generation using the sync function in thread pool
        loop = asyncio.get_event_loop()
        glb_result = await loop.run_in_executor(
            thread_pool,
            continue_glb_generation_sync,
            image_to_process,
            job_id,
            objects_analysis,
            clean_objects
        )
        
        # Handle any pending database updates from the sync function
        try:
            # Check for pending image update
            if '_pending_image_update' in globals():
                pending_job_id, image_update_data = globals().pop('_pending_image_update')
                if pending_job_id == job_id:
                    await db_service.ensure_connection()
                    current_job = await db_service.get_job(job_id)
                    if current_job:
                        result_data = current_job.get("result", {})
                        result_data.update(image_update_data)
                        await db_service.update_job(job_id, {
                            **current_job,
                            "result": result_data,
                            "updated_at": time.time()
                        })
                        print(f"✅ Updated job database with downloaded image info")
            
            # Check for pending GLB update
            if '_pending_glb_update' in globals():
                pending_job_id, glb_update_data = globals().pop('_pending_glb_update')
                if pending_job_id == job_id:
                    await db_service.ensure_connection()
                    current_job = await db_service.get_job(job_id)
                    if current_job:
                        result_data = current_job.get("result", {})
                        result_data.update(glb_update_data)
                        
                        # Also set final_glb_path to the S3 URL for the job
                        if "final_glb_s3_url" in glb_update_data:
                            result_data["final_glb_path"] = glb_update_data["final_glb_s3_url"]
                        
                        await db_service.update_job(job_id, {
                            **current_job,
                            "result": result_data,
                            "updated_at": time.time()
                        })
                        print(f"✅ Updated job database with GLB S3 info and final_glb_path")
        except Exception as db_update_error:
            print(f"⚠️ Failed to update database after GLB generation: {db_update_error}")
        
        # Check if GLB generation was successful (using S3 URL, not local file)
        if glb_result:
            print(f"✅ GLB generation completed successfully: {glb_result}")
            
            # Update job status with successful GLB generation
            update_job_status(job_id, "completed", 100, "GLB generation completed successfully!", {
                "final_glb_path": glb_result,  # Store S3 URL as final_glb_path
                "final_glb_s3_url": glb_result,  # Also store as final_glb_s3_url for compatibility
                "objects_analysis": objects_analysis,
                "clean_objects": clean_objects,
                "processing_method": "confirmed_glb_generation",
                "confirmation_workflow": True,
                "glb_generation_completed_at": time.time(),
                "s3_storage": "enabled"
            })
            
        else:
            raise Exception("GLB generation failed - no S3 URL returned")
        
    except Exception as e:
        print(f"❌ Error in GLB generation continuation: {e}")
        update_job_status(job_id, "failed", 75, f"GLB generation failed after confirmation: {str(e)}", {
            "confirmation_workflow": True,
            "glb_generation_error": str(e)
        })

def continue_glb_generation_sync(image_path: str, job_id: str, objects_analysis: str, clean_objects: str) -> str:
    """Synchronous GLB generation function for thread pool execution - Database and S3 only
    
    This function continues the 3D model generation process using the
    Segmind Hunyuan3D API after user confirmation. Uses only database and S3 storage.
    
    Args:
        image_path: Path to the image to process (can be local path or S3 URL)
        job_id: Job identifier
        objects_analysis: AI analysis of objects in the image
        clean_objects: Cleaned list of 3D-worthy objects
        
    Returns:
        S3 URL to generated GLB file or None if failed
    """
    try:
        print(f"🎯 [Thread {threading.current_thread().name}] Database-only GLB generation for job {job_id}")
        
        # Create temporary files for processing
        timestamp = int(time.time())
        temp_glb_fd, temp_glb_path = tempfile.mkstemp(suffix=".glb")
        os.close(temp_glb_fd)  # Close file descriptor, keep path
        
        print(f"📁 Using temporary GLB output: {temp_glb_path}")
        
        # Use mapapi's 3D generation parameters
        gen_params = {
            "seed": int(os.getenv("SEED", "42")),
            "steps": int(os.getenv("STEPS", "30")),
            "num_chunks": int(os.getenv("NUM_CHUNKS", "8000")),
            "max_facenum": int(os.getenv("MAX_FACENUM", "20000")),
            "guidance_scale": float(os.getenv("GUIDANCE_SCALE", "7.5")),
            "generate_texture": os.getenv("GENERATE_TEXTURE", "true").lower() == "true",
            "octree_resolution": int(os.getenv("OCTREE_RESOLUTION", "256")),
            "remove_background": os.getenv("REMOVE_BACKGROUND", "true").lower() == "true",
            "texture_resolution": int(os.getenv("TEXTURE_RESOLUTION", "4096")),
            "bake_normals": os.getenv("BAKE_NORMALS", "true").lower() == "true",
            "bake_ao": os.getenv("BAKE_AO", "true").lower() == "true",
        }
        
        # Headers for Segmind API
        headers = {
            "x-api-key": os.getenv("SEGMIND_API_KEY"),
            "Accept": "application/octet-stream"
        }
        
        segmind_endpoint = os.getenv("SEGMIND_ENDPOINT", "https://api.segmind.com/v1/hunyuan3d-2.1")
        
        # Get the optimized image from the job database instead of using local paths
        print(f"🌐 Getting optimized image from job database for GLB generation...")
        
        # Get job data from database
        try:
            from db_service import DatabaseService
            db_service = DatabaseService()
            if hasattr(db_service, 'ensure_connection'):
                db_service.ensure_connection()
            
            # Get the job data
            job_data = db_service.get_job(job_id)
            if not job_data:
                raise Exception(f"Job {job_id} not found in database")
            
            result_data = job_data.get("result", {})
            
            # Look for optimized image S3 URL in the database
            optimized_image_url = None
            
            # Check various fields for the optimized image URL
            s3_optimized = result_data.get("s3_optimized_url") or result_data.get("s3_optimized") or result_data.get("optimized_3d_image_s3_url")
            if isinstance(s3_optimized, dict):
                optimized_image_url = s3_optimized.get("s3_direct_url") or s3_optimized.get("presigned_url")
            elif isinstance(s3_optimized, str):
                optimized_image_url = s3_optimized
            
            # Fallback to other image URLs
            if not optimized_image_url:
                s3_gemini = result_data.get("s3_gemini_url") or result_data.get("s3_gemini")
                if isinstance(s3_gemini, dict):
                    optimized_image_url = s3_gemini.get("s3_direct_url") or s3_gemini.get("presigned_url")
                elif isinstance(s3_gemini, str):
                    optimized_image_url = s3_gemini
            
            # Final fallback to input image
            if not optimized_image_url:
                s3_input = result_data.get("s3_input_url") or result_data.get("s3_input")
                if isinstance(s3_input, dict):
                    optimized_image_url = s3_input.get("s3_direct_url") or s3_input.get("presigned_url")
                elif isinstance(s3_input, str):
                    optimized_image_url = s3_input
            
            if not optimized_image_url:
                raise Exception("No image URL found in job database")
            
            print(f"✅ Found image URL in database: {optimized_image_url}")
            
            # Download the image from S3 URL
            response = requests.get(optimized_image_url, timeout=30)
            response.raise_for_status()
            
            # Determine file extension from URL or response headers
            file_ext = ".jpg"
            if "image/png" in response.headers.get("content-type", ""):
                file_ext = ".png"
            elif optimized_image_url.lower().endswith('.png'):
                file_ext = ".png"
            
            # Create a temporary file for processing
            temp_image_fd, local_image_path = tempfile.mkstemp(suffix=file_ext)
            with os.fdopen(temp_image_fd, 'wb') as temp_file:
                temp_file.write(response.content)
            
            print(f"✅ Downloaded image to temporary file: {local_image_path}")
            
        except Exception as db_error:
            raise Exception(f"Failed to get image from database: {db_error}")
        
        # Validate local image before processing
        if not validate_image_for_3d_generation(local_image_path):
            raise Exception("Image validation failed for GLB generation")
        
        # Get MIME type for the image
        import mimetypes
        mime = mimetypes.guess_type(local_image_path)[0] or "image/jpeg"
        
        print(f"📤 Uploading image to Segmind for GLB generation...")
        
        with open(local_image_path, "rb") as imgf:
            # Validate image content
            img_content = imgf.read()
            if len(img_content) == 0:
                raise Exception("Image file is empty")
            
            print(f"📊 Image: {os.path.basename(local_image_path)}, size: {len(img_content):,} bytes")
            
            # Reset file pointer
            imgf.seek(0)
            files = {"image": (os.path.basename(local_image_path), imgf, mime)}
            data_form = {k: v for k, v in gen_params.items()}
            
            # Use retry wrapper for better reliability
            if not mapapi:
                raise Exception("mapapi module not available")
            
            success, meta = mapapi.post_hunyuan_to_file_with_retry(
                segmind_endpoint,
                headers=headers,
                data=data_form,
                files=files,
                out_path=temp_glb_path,
                max_retries=3
            )
        
        if success and os.path.exists(temp_glb_path):
            print(f"✅ GLB generation successful: {temp_glb_path}")
            
            # Upload GLB to S3 immediately
            print(f"📤 Uploading generated GLB to S3...")
            glb_filename = f"{job_id}_{timestamp}_confirmed_3d_model.glb"
            s3_glb_key = f"assets/{job_id}/models/confirmed/{glb_filename}"
            
            try:
                s3 = s3_client()
                
                # Upload GLB file to S3
                with open(temp_glb_path, 'rb') as glb_file:
                    s3.upload_fileobj(
                        glb_file,
                        S3_BUCKET,
                        s3_glb_key,
                        ExtraArgs={
                            "ContentType": "model/gltf-binary",
                            "Metadata": {
                                "job_id": job_id,
                                "generation_method": "confirmed_glb_generation",
                                "upload_timestamp": str(timestamp),
                                "source_image": image_path,
                                "confirmation_workflow": "true"
                            }
                        }
                    )
                
                # Generate presigned URL for the GLB
                presigned_glb_url = s3.generate_presigned_url(
                    ClientMethod="get_object",
                    Params={"Bucket": S3_BUCKET, "Key": s3_glb_key},
                    ExpiresIn=7 * 24 * 3600  # 7 days
                )
                
                # Generate direct S3 URL
                s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
                s3_glb_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{s3_glb_key}"
                
                print(f"✅ GLB uploaded to S3: {s3_glb_direct_url}")
                
                # Update job in database with GLB information
                try:
                    from db_service import DatabaseService
                    db_service = DatabaseService()
                    
                    # Database-only update - no local paths, only S3 URLs and metadata
                    def update_job_with_glb():
                        # This will be called from the async context
                        return {
                            "confirmed_glb": {
                                "s3_url": s3_glb_direct_url,
                                "s3_key": s3_glb_key,
                                "presigned_url": presigned_glb_url,
                                "filename": glb_filename,
                                "size_bytes": os.path.getsize(temp_glb_path),
                                "generation_timestamp": timestamp,
                                "method": "confirmed_glb_generation",
                                "storage_type": "s3_only"
                            },
                            "final_glb_path": s3_glb_direct_url,  # Save S3 URL as final_glb_path
                            "final_glb_s3_url": s3_glb_direct_url,
                            "status": "completed"
                        }
                    
                    # Store the update function for later use
                    global _pending_glb_update
                    _pending_glb_update = (job_id, update_job_with_glb())
                    
                except Exception as db_error:
                    print(f"⚠️ Failed to prepare database update: {db_error}")
                    # Continue with local processing
                
            except Exception as s3_error:
                print(f"⚠️ Failed to upload GLB to S3: {s3_error}")
                # Continue with local file processing even if S3 upload fails
            
            # Save metadata for database update (database-only storage)
            metadata = {
                "method": "confirmed_glb_generation",
                "job_id": job_id,
                "source_image_url": optimized_image_url if 'optimized_image_url' in locals() else None,
                "objects_analysis": objects_analysis,
                "clean_objects": clean_objects,
                "params": gen_params,
                "confirmation_workflow": True,
                "generated_at": time.time(),
                "thread": threading.current_thread().name,
                "image_source": "database_retrieval",
                "glb_uploaded_to_s3": True,
                "s3_glb_url": s3_glb_direct_url if 's3_glb_direct_url' in locals() else None,
                "database_only": True,  # Flag indicating this job uses database-only storage
                "storage_type": "s3_only"
            }
            
            # Clean up temporary image file (always cleanup since we downloaded from database)
            try:
                os.unlink(local_image_path)
                print(f"🧹 Cleaned up temporary image file: {local_image_path}")
            except Exception as cleanup_error:
                print(f"⚠️ Failed to cleanup temp image file: {cleanup_error}")
            
            # Clean up temporary GLB file after S3 upload
            try:
                os.unlink(temp_glb_path)
                print(f"🧹 Cleaned up temporary GLB file: {temp_glb_path}")
            except Exception as cleanup_error:
                print(f"⚠️ Failed to cleanup temp GLB file: {cleanup_error}")
            
            # Return S3 URL instead of local path for database-only mode
            return s3_glb_direct_url if 's3_glb_direct_url' in locals() else None
        else:
            raise Exception(f"Segmind GLB generation failed: {meta}")
    
    except Exception as e:
        print(f"❌ Error in sync GLB generation: {e}")
        return None

# ========= API ENDPOINTS =========

@app.get("/")
async def root():
    """ForgeRealm Digital Artifacts Engine - Forge your creative vision into stunning 3D artifacts."""
    try:
        # Get job stats from database
        service = get_db_service()
        if service and service.ensure_connection():
            all_jobs = service.list_jobs(limit=1000)  # Get more jobs for stats
            active_jobs = len([job for job in all_jobs if job.get('status') == 'processing'])
            completed_jobs = len([job for job in all_jobs if job.get('status') == 'completed'])
            failed_jobs = len([job for job in all_jobs if job.get('status') == 'failed'])
            infused_jobs = len([job for job in all_jobs if job.get('isInfused', False)])
            total_jobs = len(all_jobs)
            
            # Calculate RAG statistics
            rag_jobs = 0
            rag_generated = 0
            rag_optimized = 0
            for job in all_jobs:
                result = job.get("result", {})
                job_type = result.get("job_type", "")
                if job_type in ["rag_image_generation", "image_generation", "rag_image_optimization"]:
                    rag_jobs += 1
                    if result.get("generated_image_s3_url") or result.get("generated_image_base64"):
                        rag_generated += 1
                    if result.get("optimized_3d_image_s3_url") or result.get("optimized_3d_image_base64"):
                        rag_optimized += 1
        else:
            # Fallback to placeholder values when database is unavailable
            active_jobs = completed_jobs = failed_jobs = infused_jobs = total_jobs = 0
            rag_jobs = rag_generated = rag_optimized = 0
    except Exception as e:
        print(f"⚠️ Error getting job stats: {e}")
        active_jobs = completed_jobs = failed_jobs = infused_jobs = total_jobs = 0
        rag_jobs = rag_generated = rag_optimized = 0
    
    return {
        "message": "🏗️ ForgeRealm Digital Artifacts Engine", 
        "tagline": "Forge your digital artifacts",
        "version": "1.0.0",
        "cors_enabled": True,
        "status": "running",
        "threading": {
            "max_workers": thread_pool._max_workers,
            "active_workers": thread_pool._threads and len(thread_pool._threads) or 0,
            "queue_size": thread_pool._work_queue.qsize()
        },
        "forge_summary": {
            "active_forges": active_jobs,
            "completed_artifacts": completed_jobs, 
            "failed_attempts": failed_jobs,
            "total_projects": total_jobs,
            "fusion_artifacts": infused_jobs,
            "rag_artifacts": rag_jobs,
            "rag_generated_images": rag_generated,
            "rag_optimized_images": rag_optimized
        },
        "forge_modes": {
            "instant_forge": "POST /animate/image - Upload image → Animated 3D artifact",
            "vision_forge": "POST /animate/prompt - Describe vision → AI-generated 3D artifact", 
            "location_forge": "POST /animate/coordinates - GPS coordinates → Digital twin from Street View",
            "fusion_forge": "POST /animate/infuse - Blend elements → Unique hybrid artifacts",
            "vision_analysis": "POST /animate/vision - Identify animatable objects with Llama 3.2 Vision",
            "smart_animate": "POST /animate/smart - AI-powered selective animation with Llama 3.2 Vision",
            "rag_forge": "POST /gen/gemini - RAG-based image generation with reference similarity",
            "rag_optimize": "POST /gen/rag-optimize - Separate 3D optimization for RAG images",
            "3d_optimize": "POST /optimize/3d - 🎯 NEW: Transform any image into 3D-model-ready format",
            "wan_artisan": "POST /animate/wan - Advanced animation forging",
            "luma_designer": "POST /animate/design - Professional design artifacts",
            "pose_sculptor": "POST /design/poses - Character pose crafting",
            "image_enhancer": "POST /design/edit - Artifact refinement",
            "texture_master": "POST /retexture - Material enhancement",
            "status": "GET /status/{job_id}",
            "jobs": "GET /jobs",
            "job_files": "GET /jobs/{job_id}/{path}",
            "system_status": "GET /system/status"
        },
        "artisan_tools": {
            "fusion_forge": {
                "endpoint": "POST /animate/infuse",
                "description": "🎭 Fuse two digital elements using AI vision and intelligent prompt generation",
                "status_tracking": "Fusion artifacts are marked with isInfused: true for special handling",
                "asset_management": {
                    "automatic_s3_storage": "All source and fused artifacts automatically stored in cloud",
                    "artifact_tagging": "Elements tagged as source_artifact_1, source_artifact_2, fused_artifact, fused_artifact_3d_optimized",
                    "status_access": "All cloud URLs available in artifact status under infused_image_urls",
                    "metadata_persistence": "Fusion URLs preserved in artifact_metadata.json fusion_urls section",
                    "duplicate_prevention": "Smart validation prevents cross-project conflicts and ensures project-specific artifacts",
                    "upload_tracking": "isImagesUploaded flag prevents redundant uploads during project restoration"
                },
                "capabilities": [
                    "🔍 AI-powered image analysis with Llama Vision",
                    "🧠 Intelligent fusion prompt generation with Ollama/GPT",
                    "🎨 New artifact creation with Gemini AI",
                    "⚡ 3D optimization for enhanced model generation", 
                    "🏗️ Advanced 3D model forging from optimized fusion",
                    "🎭 Multiple fusion styles: blend, merge, hybrid",
                    "☁️ Automatic cloud storage with organized tagging",
                    "📂 Structured fusion URL management in dedicated metadata",
                    "🛡️ Project-specific validation preventing cross-project conflicts",
                    "✅ Smart upload completion tracking preventing redundant operations"
                ]
            },
            "wan_artisan": {
                "endpoint": "POST /animate/wan",
                "description": "🎬 Animate digital artifacts using WAN 2.2 technology from WaveSpeed.ai",
                "capabilities": [
                    "🎥 High-quality artifact animation with AI",
                    "📝 Customizable animation prompts",
                    "📺 Multiple resolution options (480p, 720p, 1080p)",
                    "🎞️ Optional reference video guidance",
                    "🎲 Reproducible results with seed control",
                    "⚡ Fast processing with real-time status updates"
                ],
                "forge_parameters": {
                    "image": "URL of the artifact to animate (required)",
                    "prompt": "Description of the animation to forge (required)",
                    "resolution": "Video resolution (default: 480p)",
                    "video": "Optional reference video URL for style guidance",
                    "seed": "Random seed for reproducible results (default: 42)"
                }
            },
            "smart_animate": {
                "endpoint": "POST /animate/smart",
                "description": "🤖 AI-powered selective animation using Llama 3.2 Vision for intelligent object detection",
                "features": [
                    "Llama 3.2 Vision analyzes image to identify all objects and characters",
                    "Intelligently crafts animation prompts that preserve non-animated elements",
                    "Only animates what you specify - everything else stays perfectly still",
                    "Preserves exact visual appearance, colors, textures, and lighting",
                    "Prevents unwanted changes to background or other scene elements",
                    "Natural and realistic animation for specified actions",
                    "Powered by Luma Ray 2 I2V animation technology",
                    "Real-time job tracking with detailed progress updates"
                ],
                "workflow": [
                    "1. Upload image or provide URL",
                    "2. Llama 3.2 Vision analyzes entire scene and identifies all elements",
                    "3. AI crafts intelligent prompt that isolates animation target",
                    "4. Luma Ray 2 I2V animates only specified elements",
                    "5. Returns video with selective animation and preserved scene"
                ],
                "use_cases": [
                    "Animate a character waving while keeping background static",
                    "Make a car move forward without changing the street scene",
                    "Animate character jumping without affecting surrounding objects",
                    "Create subtle movements (head turn, eye blink) while preserving everything else",
                    "Animate specific objects in complex scenes with multiple elements",
                    "Add general animation while freezing specific important assets (e.g., animate the scene but keep the main character frozen)"
                ],
                "parameters": {
                    "file": "Image file to animate (optional if image_url provided)",
                    "animation_prompt": "Simple description of what to animate (e.g., 'make the character jump')",
                    "image_url": "URL of image to animate (optional if file provided)",
                    "size": "Video resolution in 'width*height' format (default: 1280*720)",
                    "duration": "Video duration in seconds (default: 5)",
                    "preserve_background": "Keep background unchanged (default: true)",
                    "asset": "OPTIONAL - Specific asset to animate (e.g., 'the red car', 'the woman in blue'). When provided, ONLY this asset animates, everything else stays frozen.",
                    "freeze_assets": "OPTIONAL - Comma-separated list of assets to keep frozen (e.g., 'the character, the building, trees'). These assets will be explicitly prevented from animating."
                },
                "output": {
                    "animation_url": "URL of the generated animated video",
                    "local_video_path": "Local path to the video file",
                    "scene_analysis": "Llama 3.2 Vision analysis of all scene elements",
                    "intelligent_prompt": "AI-crafted prompt used for selective animation",
                    "processing_time": "Total processing time in seconds",
                    "metadata": {
                        "user_prompt": "Your original animation request",
                        "size": "Video resolution used",
                        "duration": "Video duration in seconds",
                        "workflow": "Step-by-step processing breakdown"
                    }
                },
                "example": {
                    "input": "Image of person standing in front of building",
                    "animation_prompt": "make the character wave",
                    "result": "Character waves naturally, building/background stay perfectly static"
                },
                "asset_example": {
                    "input": "Image with multiple people and cars",
                    "animation_prompt": "move forward",
                    "asset": "the red car",
                    "result": "ONLY the red car moves forward, all people and other cars stay completely frozen"
                },
                "freeze_assets_example": {
                    "input": "Street scene with people, cars, and trees swaying",
                    "animation_prompt": "add wind and movement",
                    "freeze_assets": "the character, the red car",
                    "result": "Trees and environment move with wind, but the character and red car stay completely frozen"
                }
            },
            "vision_analysis": {
                "endpoint": "POST /animate/vision",
                "description": "🔍 Identify all animatable objects in an image using Llama 3.2 Vision AI",
                "features": [
                    "Advanced object detection using Llama 3.2 Vision",
                    "Accurate parsing with presence indicators (there is, shows, depicts, etc.)",
                    "Identifies characters, animals, vehicles, and environmental elements",
                    "Extracts descriptive attributes (colors, sizes) for specific object identification",
                    "Confidence scores (high/medium/low) with numeric values (0.5-0.95)",
                    "Priority-based categorization (Priority 1-3) for animation suitability",
                    "Categorizes objects by animation potential (high/medium/low)",
                    "Provides animation suggestions for each detected object",
                    "Returns structured array of animatable elements sorted by confidence",
                    "Includes full scene analysis and context for each object",
                    "Works with uploaded files or image URLs",
                    "Prevents false positives - only reports actually visible objects",
                    "Multiple mention tracking increases confidence scores",
                    "Useful for planning animations before execution"
                ],
                "use_cases": [
                    "Discover what can be animated in your image",
                    "Plan selective animations based on scene elements",
                    "Get animation suggestions for specific objects",
                    "Understand scene complexity before animating",
                    "Identify the best elements to animate for impact"
                ],
                "parameters": {
                    "file": "Image file to analyze (optional if image_url provided)",
                    "image_url": "URL of image to analyze (optional if file provided)"
                },
                "output": {
                    "animatable_objects": "Array of objects with name, category, confidence, confidence_score, description, and animation potential",
                    "object_count": "Total number of animatable objects found",
                    "high_confidence_count": "Number of objects detected with high confidence",
                    "full_analysis": "Complete scene analysis from Llama 3.2 Vision",
                    "recommendations": "AI suggestions for animation approaches based on detected objects",
                    "usage_tip": "Guidance for using results with other endpoints"
                },
                "object_categories": [
                    "characters (people, humans, figures) - Priority 1",
                    "animals (pets, creatures) - Priority 1",
                    "vehicles (cars, bikes, boats) - Priority 1",
                    "nature (trees, plants, water) - Priority 2",
                    "objects (items, things) - Priority 2",
                    "environment (buildings, furniture) - Priority 3"
                ],
                "confidence_levels": {
                    "high": "0.85-0.95 - Object clearly visible with presence indicators",
                    "medium": "0.60-0.75 - Object mentioned with some context",
                    "low": "0.50 - Object mentioned without strong indicators"
                },
                "example_response": {
                    "animatable_objects": [
                        {
                            "name": "red car",
                            "category": "vehicles",
                            "confidence": "high",
                            "confidence_score": 0.85,
                            "description": "The image shows a red car in the foreground",
                            "animation_potential": "high",
                            "suggested_animations": ["driving forward", "moving", "wheels turning"]
                        }
                    ],
                    "high_confidence_count": 1
                }
            },
            "luma_ray_design": {
                "endpoint": "POST /animate/design",
                "description": "Create high-quality video animations from uploaded images using Luma Ray 2 I2V technology",
                "features": [
                    "Image-to-video animation using Luma Ray 2 I2V AI",
                    "Customizable animation prompts for motion description",
                    "Multiple video resolution options",
                    "Configurable animation duration",
                    "Optional GIF conversion with ffmpeg",
                    "Local video file storage in job directories",
                    "Real-time processing status updates"
                ],
                "parameters": {
                    "file": "Image file to animate (required)",
                    "prompt": "Animation prompt describing motion/action (default: 'keep the same style; make him jump like there is no tomorrow')",
                    "size": "Video resolution in 'width*height' format (default: '1280*720')",
                    "duration": "Video duration in seconds (default: '5')",
                    "also_make_gif": "Also create GIF version (default: false)"
                },
                "output": {
                    "video_url": "Direct download URL from Luma Ray API",
                    "local_video_path": "Local MP4 file path in job directory",
                    "gif_path": "Local GIF file path (if requested and ffmpeg available)",
                    "processing_time": "Total animation processing time in seconds",
                    "api_job_id": "Luma Ray API job identifier for tracking"
                }
            },
            "design_poses": {
                "endpoint": "POST /design/poses",
                "description": "Generate character poses using Segmind workflow and create 3D models",
                "features": [
                    "Character pose generation using Segmind workflow API",
                    "Automatic 3D model generation from poses image",
                    "S3 upload for both poses image and 3D model GLB file",
                    "Real-time processing status updates",
                    "Metadata persistence for poses and 3D model URLs"
                ],
                "parameters": {
                    "character_image": "URL of the character image to generate poses for (required)",
                    "job_id": "Optional job ID for tracking and organization",
                    "timeout_seconds": "Request timeout in seconds (default: 300)"
                },
                "output": {
                    "image_output": "Direct poses image URL from Segmind API",
                    "local_image_path": "Local poses image file path in job directory",
                    "poses_s3_url": "Direct S3 URL for poses image",
                    "poses_s3_presigned_url": "Presigned S3 URL for poses image (7 days)",
                    "poses_3d_model_local": "Local 3D model GLB file path",
                    "poses_3d_model_s3_url": "Presigned S3 URL for 3D model GLB",
                    "poses_3d_model_generated": "Boolean indicating if 3D model was successfully generated",
                    "processing_time_seconds": "Total processing time in seconds",
                    "request_id": "Segmind API request identifier for tracking"
                }
            },
            "rag_forge": {
                "endpoint": "POST /gen/gemini",
                "description": "🔍 RAG-based image generation with reference similarity and 3D optimization",
                "features": [
                    "FAISS vector database with CLIP embeddings for reference image retrieval",
                    "Gemini 2.5 Flash Image model for AI-generated images",
                    "Automatic 3D optimization using mapapi for model-ready images",
                    "Database-only storage with base64 encoding and S3 upload",
                    "Configurable reference image count (k-value)",
                    "Real-time processing status updates with detailed progress",
                    "Temporary file processing with automatic cleanup"
                ],
                "parameters": {
                    "prompt": "Text description for image generation (required)",
                    "k": "Number of reference images to retrieve from RAG index (default: 3)",
                    "job_id": "Optional job ID for tracking and organization"
                },
                "output": {
                    "generated_image_s3_url": "S3 URL for the RAG-generated image",
                    "optimized_3d_image_s3_url": "S3 URL for the 3D-optimized version",
                    "generated_image_base64": "Base64 encoded image data stored in database",
                    "optimized_3d_image_base64": "Base64 encoded optimized image data",
                    "objects_analysis": "AI analysis results for 3D optimization",
                    "reference_images": "Metadata about retrieved reference images",
                    "3d_optimization_success": "Boolean indicating optimization success",
                    "storage_type": "Database-only storage confirmation"
                }
            },
            "rag_optimize": {
                "endpoint": "POST /gen/rag-optimize",
                "description": "⚡ Separate 3D optimization for existing RAG-generated images",
                "features": [
                    "Post-processing optimization for RAG-generated images",
                    "Database-only workflow with temporary file processing",
                    "Advanced object analysis using mapapi.analyze_image_with_llama",
                    "3D model preparation using mapapi.generate_image_with_gemini",
                    "Automatic S3 upload with organized file naming",
                    "Real-time progress tracking and status updates"
                ],
                "parameters": {
                    "job_id": "Job ID of existing RAG generation to optimize (required)",
                    "optimization_prompt": "Custom prompt for optimization (optional)"
                },
                "output": {
                    "optimized_image_s3_url": "S3 URL for the optimized image",
                    "optimized_image_base64": "Base64 encoded optimized image data",
                    "source_image_s3_url": "Original RAG-generated image S3 URL",
                    "objects_analysis": "Detailed object analysis for 3D optimization",
                    "optimization_timestamp": "Processing completion timestamp",
                    "separate_optimization": "Flag indicating this was separate optimization"
                }
            },
            "3d_optimize": {
                "endpoint": "POST /optimize/3d",
                "description": "🎯 Transform any image into a 3D-model-ready format with AI-powered optimization",
                "features": [
                    "Accepts Gemini-generated images, uploaded files, or image URLs",
                    "AI vision analysis to understand image content",
                    "Auto-generates optimization prompt or accepts custom prompts",
                    "Removes/neutralizes backgrounds for clean 3D reconstruction",
                    "Optimizes camera angle for better 3D capture",
                    "Enhances textures for PBR-ready materials",
                    "Improves lighting for even texture baking",
                    "Database and S3 storage with base64 encoding",
                    "Real-time progress tracking"
                ],
                "use_cases": [
                    "Prepare Gemini-generated images for 3D model creation",
                    "Clean up photos for photogrammetry workflows",
                    "Convert concept art into 3D-ready references",
                    "Optimize character designs for 3D rigging",
                    "Improve texture quality for game assets"
                ],
                "parameters": {
                    "file": "Image file to optimize (optional if image_url provided)",
                    "image_url": "URL of image to optimize (optional if file provided)",
                    "prompt": "Custom optimization instructions (optional - auto-generated if not provided)",
                    "job_id": "Optional job ID for tracking"
                },
                "workflow_integration": {
                    "node_type": "optimize_3d",
                    "config": {
                        "image_url": "Auto-extracted from previous node output",
                        "prompt": "Optional custom optimization prompt"
                    },
                    "example_workflow": [
                        "1. generate_image: Create image with Gemini",
                        "2. optimize_3d: Optimize for 3D model generation",
                        "3. generate_3d: Create 3D model from optimized image"
                    ]
                },
                "output": {
                    "job_id": "Tracking ID for the optimization job",
                    "original_image_s3_url": "S3 URL of original image",
                    "optimized_3d_image_s3_url": "S3 URL of optimized image (ready for 3D)",
                    "has_base64_data": "Boolean indicating base64 data availability",
                    "objects_analysis": "AI analysis of image content",
                    "message": "Success message with next steps"
                },
                "example_curl": 'curl -X POST "http://localhost:8000/optimize/3d" -F "image_url=https://example.com/character.jpg" -F "prompt=Optimize for 3D character modeling"'
            },
            "file_serving": {
                "endpoint": "GET /jobs/{job_id}/{path}",
                "description": "Serve static files from job directories (GLB models, images, videos, etc.)",
                "features": [
                    "Direct file access for job-generated content",
                    "Automatic MIME type detection",
                    "Special handling for 3D model files (.glb, .gltf)",
                    "Security validation to prevent directory traversal",
                    "Support for all file types generated by the API"
                ],
                "parameters": {
                    "job_id": "The job identifier (required)",
                    "path": "Relative path to the file within the job directory (required)"
                },
                "examples": {
                    "glb_model": "/jobs/{job_id}/models/model.glb",
                    "poses_image": "/jobs/{job_id}/character_poses.png",
                    "video": "/jobs/{job_id}/animation.mp4",
                    "metadata": "/jobs/{job_id}/job_metadata.json"
                },
                "security": "Path validation prevents access to files outside job directories"
            }
        }
    }

@app.websocket("/ws/jobs/{job_id}")
async def websocket_job_updates(websocket: WebSocket, job_id: str):
    """
    WebSocket endpoint for real-time job updates.
    
    Connect to this endpoint to receive real-time updates for a specific job.
    
    Usage:
        const ws = new WebSocket(`ws://localhost:8000/ws/jobs/${jobId}`);
        ws.onmessage = (event) => {
            const update = JSON.parse(event.data);
            console.log(`Job ${update.job_id}: ${update.status} - ${update.progress}%`);
            console.log(`Message: ${update.message}`);
        };
    
    Returns:
        Real-time JSON messages with job updates containing:
        - type: "job_update"
        - job_id: Job identifier
        - status: Current status (processing/completed/failed)
        - progress: Progress percentage (0-100)
        - message: Status message
        - result: Current result data
        - timestamp: Update timestamp
    """
    await manager.connect(websocket, job_id)
    
    try:
        # Send initial job status if available
        job_status = get_job_status(job_id)
        if job_status:
            await websocket.send_json({
                "type": "job_update",
                "job_id": job_id,
                "status": job_status.get("status", "unknown"),
                "progress": job_status.get("progress", 0),
                "message": job_status.get("message", ""),
                "result": job_status.get("result", {}),
                "timestamp": time.time()
            })
        
        # Keep connection alive and wait for client messages (optional)
        while True:
            try:
                # Wait for any client messages (for ping/pong or commands)
                data = await websocket.receive_text()
                
                # Handle ping/pong or other client messages
                if data == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": time.time()})
                    
            except WebSocketDisconnect:
                break
                
    except Exception as e:
        print(f"❌ WebSocket error for job {job_id}: {e}")
    finally:
        manager.disconnect(websocket, job_id)

@app.websocket("/ws/jobs")
async def websocket_all_jobs(websocket: WebSocket):
    """
    WebSocket endpoint for real-time updates on all jobs.
    
    Connect to this endpoint to receive real-time updates for all jobs (broadcast mode).
    
    Usage:
        const ws = new WebSocket('ws://localhost:8000/ws/jobs');
        ws.onmessage = (event) => {
            const update = JSON.parse(event.data);
            console.log(`Job ${update.job_id}: ${update.status} - ${update.progress}%`);
        };
    
    Returns:
        Real-time JSON messages with job updates for all jobs
    """
    await manager.connect(websocket)
    
    try:
        # Keep connection alive
        while True:
            try:
                data = await websocket.receive_text()
                
                if data == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": time.time()})
                    
            except WebSocketDisconnect:
                break
                
    except Exception as e:
        print(f"❌ WebSocket error (broadcast): {e}")
    finally:
        manager.disconnect(websocket)

def scan_job_folder(job_id: str) -> dict:
    """Scan local job folder for additional files and information."""
    job_folder = f"jobs/{job_id}"
    folder_info = {
        "folder_exists": False,
        "files": [],
        "directories": [],
        "total_files": 0,
        "total_size_bytes": 0,
        "models": [],
        "images": [],
        "animations": [],
        "metadata_files": []
    }
    
    try:
        if os.path.exists(job_folder):
            folder_info["folder_exists"] = True
            
            # Walk through the job folder
            for root, dirs, files in os.walk(job_folder):
                for file in files:
                    file_path = os.path.join(root, file)
                    rel_path = os.path.relpath(file_path, job_folder)
                    
                    try:
                        file_stat = os.stat(file_path)
                        file_info = {
                            "name": file,
                            "path": rel_path,
                            "size_bytes": file_stat.st_size,
                            "modified_time": file_stat.st_mtime,
                            "modified_readable": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(file_stat.st_mtime))
                        }
                        
                        folder_info["files"].append(file_info)
                        folder_info["total_size_bytes"] += file_stat.st_size
                        
                        # Categorize files by type
                        file_lower = file.lower()
                        if file_lower.endswith(('.glb', '.fbx', '.obj', '.dae', '.blend')):
                            folder_info["models"].append(file_info)
                        elif file_lower.endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp')):
                            folder_info["images"].append(file_info)
                        elif file_lower.endswith(('.json', '.txt', '.log', '.csv')):
                            folder_info["metadata_files"].append(file_info)
                        elif 'anim' in file_lower or 'motion' in file_lower:
                            folder_info["animations"].append(file_info)
                            
                    except OSError as e:
                        print(f"⚠️ Could not stat file {file_path}: {e}")
                
                # Add directory info (relative to job folder)
                for dir in dirs:
                    dir_path = os.path.join(root, dir)
                    rel_dir_path = os.path.relpath(dir_path, job_folder)
                    folder_info["directories"].append(rel_dir_path)
            
            folder_info["total_files"] = len(folder_info["files"])
            
            # Convert size to human readable
            size_mb = folder_info["total_size_bytes"] / (1024 * 1024)
            folder_info["total_size_mb"] = round(size_mb, 2)
            
            print(f"📁 Job {job_id} folder scan: {folder_info['total_files']} files, {folder_info['total_size_mb']} MB")
    
    except Exception as e:
        print(f"❌ Error scanning job folder {job_folder}: {e}")
        folder_info["error"] = str(e)
    
    return folder_info

def get_job_images_base64(job_id: str) -> dict:
    """Get base64 encoded images from job folder and database for client display."""
    job_folder = f"jobs/{job_id}"
    images_base64 = {
        "gemini_generated": None,
        "optimized_3d": None,
        "input_image": None,
        "edited_image": None,
        "rag_generated": None,
        "rag_optimized": None
    }
    
    try:
        # First check database for RAG images (database-only storage)
        if job_id in jobs:
            job_data = jobs[job_id]
            result = job_data.get("result", {})
            
            # Check for RAG generated image from database
            if result.get("generated_image_base64"):
                generated_base64 = result["generated_image_base64"]
                images_base64["rag_generated"] = {
                    "data_url": f"data:image/png;base64,{generated_base64}",
                    "filename": result.get("image_filename", "rag_generated.png"),
                    "size_bytes": len(base64.b64decode(generated_base64)),
                    "mime_type": "image/png",
                    "source": "database"
                }
                print(f"📸 Retrieved RAG generated image from database for job {job_id}")
            
            # Check for RAG optimized image from database (both inline and separate optimization)
            optimized_base64 = result.get("optimized_3d_image_base64")
            if optimized_base64:
                images_base64["rag_optimized"] = {
                    "data_url": f"data:image/jpeg;base64,{optimized_base64}",
                    "filename": f"rag_optimized_{job_id}.jpg",
                    "size_bytes": len(base64.b64decode(optimized_base64)),
                    "mime_type": "image/jpeg",
                    "source": "database",
                    "optimization_type": "inline" if not result.get("separate_optimization_completed") else "separate"
                }
                print(f"📸 Retrieved RAG optimized image from database for job {job_id}")
        
        # Then check local files for other image types
        if not os.path.exists(job_folder):
            return images_base64
        
        # Look for specific image files (non-RAG images still use file system)
        image_files_to_find = {
            "gemini_generated": ["gemini_generated.png", "gemini_generated.jpg"],
            "optimized_3d": ["3d_optimized.jpg", "3d_optimized.png", "3d_optimized_*.jpg", "3d_optimized_*.png"],
            "input_image": ["input_image.jpg", "input_image.png", "input_image.jpeg"],
            "edited_image": ["edited_image.png", "edited_image.jpg", "edited_image.jpeg", "edited_image.webp"]
        }
        
        for image_type, possible_names in image_files_to_find.items():
            # Skip if already found in database
            if images_base64[image_type] is not None:
                continue
                
            for filename in possible_names:
                # Handle glob patterns
                if '*' in filename:
                    import glob
                    pattern = os.path.join(job_folder, filename)
                    matching_files = glob.glob(pattern)
                    if matching_files:
                        # Use the most recent generated image
                        image_path = max(matching_files, key=os.path.getmtime)
                        filename = os.path.basename(image_path)
                    else:
                        continue
                else:
                    image_path = os.path.join(job_folder, filename)
                
                if os.path.exists(image_path):
                    try:
                        # Read and encode image as base64
                        with open(image_path, "rb") as img_file:
                            img_data = img_file.read()
                            
                            # Determine MIME type based on file extension
                            file_ext = os.path.splitext(filename)[1].lower()
                            mime_type = {
                                '.jpg': 'image/jpeg',
                                '.jpeg': 'image/jpeg',
                                '.png': 'image/png',
                                '.gif': 'image/gif',
                                '.bmp': 'image/bmp',
                                '.webp': 'image/webp'
                            }.get(file_ext, 'image/jpeg')
                            
                            # Create data URL
                            base64_data = base64.b64encode(img_data).decode('utf-8')
                            data_url = f"data:{mime_type};base64,{base64_data}"
                            
                            images_base64[image_type] = {
                                "data_url": data_url,
                                "filename": filename,
                                "size_bytes": len(img_data),
                                "mime_type": mime_type,
                                "source": "file_system"
                            }
                            
                            print(f"📸 Encoded {image_type} image: {filename} ({len(img_data):,} bytes)")
                            break  # Found the image, no need to check other names
                            
                    except Exception as e:
                        print(f"❌ Error encoding image {image_path}: {e}")
                        continue
    
    except Exception as e:
        print(f"❌ Error getting job images for {job_id}: {e}")
    
    return images_base64

@app.get("/status/{job_id}")
async def get_job_status_endpoint(job_id: str):
    """Get comprehensive job status from database with local files and base64 images."""
    # Try to get job from database first
    try:
        service = get_db_service()
        if service and service.ensure_connection():
            job_status = service.get_job(job_id)
            if not job_status:
                # Job not found in database, check memory fallback
                if job_id in jobs:
                    job_status = jobs[job_id].copy()
                    job_status["source"] = "memory_fallback"
                else:
                    raise HTTPException(status_code=404, detail="Job not found in database or memory")
            else:
                job_status["source"] = "database"
                # Ensure job_id field is present for compatibility
                if "job_id" not in job_status and "_id" in job_status:
                    job_status["job_id"] = job_status["_id"]
        else:
            # Database unavailable, use memory fallback
            if job_id not in jobs:
                raise HTTPException(status_code=404, detail="Job not found (database unavailable)")
            job_status = jobs[job_id].copy()
            job_status["source"] = "memory_fallback"
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error retrieving job {job_id}: {e}")
        # Final fallback to memory
        if job_id not in jobs:
            raise HTTPException(status_code=404, detail=f"Job not found due to error: {str(e)}")
        job_status = jobs[job_id].copy()
        job_status["source"] = "error_fallback"
        job_status["retrieval_error"] = str(e)
    
    # Load metadata from job_metadata.json
    job_metadata = load_job_metadata(job_id)
    if job_metadata:
        job_status["job_metadata"] = job_metadata
        print(f"📋 Loaded job metadata for {job_id}")
    
    # Enhance with local folder information
    folder_info = scan_job_folder(job_id)
    job_status["local_folder"] = folder_info
    
    # Get base64 encoded images for client display
    images_base64 = get_job_images_base64(job_id)
    job_status["images_base64"] = images_base64
    
    # Add S3 URLs for RAG-generated images if available
    result = job_status.get("result", {})
    if result.get("job_type") in ["rag_image_generation", "image_generation"]:
        s3_url = result.get("generated_image_s3_url")
        if s3_url:
            # Add S3 URL to images data for easy frontend access
            if not job_status.get("images_s3"):
                job_status["images_s3"] = {}
            job_status["images_s3"]["rag_generated"] = {
                "s3_url": s3_url,
                "filename": result.get("image_filename", "rag_generated.png"),
                "prompt": result.get("prompt", ""),
                "generation_timestamp": result.get("generation_timestamp"),
                "model": result.get("model", "unknown")
            }
            print(f"🖼️ Added RAG S3 URL to job status: {s3_url[:50]}...")
    
    # Add S3 URLs for RAG-optimized images if available
    elif result.get("job_type") == "rag_image_optimization":
        optimized_s3_url = result.get("optimized_image_s3_url")
        source_s3_url = result.get("source_image_s3_url")
        if optimized_s3_url:
            # Add optimized S3 URL to images data for easy frontend access
            if not job_status.get("images_s3"):
                job_status["images_s3"] = {}
            job_status["images_s3"]["rag_optimized"] = {
                "s3_url": optimized_s3_url,
                "source_s3_url": source_s3_url,
                "filename": result.get("image_filename", "rag_optimized.png"),
                "prompt": result.get("prompt", ""),
                "optimization_timestamp": result.get("optimization_timestamp"),
                "model": result.get("model", "unknown"),
                "reference_images": result.get("reference_images", [])
            }
            print(f"🎯 Added RAG optimization S3 URL to job status: {optimized_s3_url[:50]}...")
    
    # Add S3 URLs for separate RAG optimization if available
    elif result.get("separate_optimization_completed") and result.get("rag_optimized_s3_url"):
        rag_optimized_s3_url = result.get("rag_optimized_s3_url")
        if rag_optimized_s3_url:
            # Add separate optimization S3 URL to images data
            if not job_status.get("images_s3"):
                job_status["images_s3"] = {}
            job_status["images_s3"]["rag_optimized"] = {
                "s3_url": rag_optimized_s3_url,
                "source_s3_url": result.get("generated_image_s3_url"),
                "filename": f"rag_optimized_{job_id}.jpg",
                "optimization_prompt": result.get("optimization_prompt", ""),
                "optimization_timestamp": result.get("optimization_timestamp"),
                "separate_optimization": True
            }
            print(f"🎯 Added separate RAG optimization S3 URL to job status: {rag_optimized_s3_url[:50]}...")
    
    # Add workflow information for animate jobs
    if job_status.get("result", {}).get("job_type") == "animate" or job_status.get("result", {}).get("animation_type") == "luma_ray_design":
        job_status["workflow"] = {
            "type": "luma_ray_animation",
            "description": "Image-to-Video Animation using Luma Ray 2 I2V technology",
            "steps": [
                "Image upload and validation",
                "Luma Ray 2 I2V API submission", 
                "Video generation processing",
                "Video download and local storage",
                "Optional GIF conversion"
            ],
            "metadata_source": "job_metadata.json",
            "input_image_source": "input_image.jpg from job folder"
        }
    
    # Add summary information
    if folder_info["folder_exists"]:
        job_status["local_assets_summary"] = {
            "models_count": len(folder_info["models"]),
            "images_count": len(folder_info["images"]),
            "animations_count": len(folder_info["animations"]),
            "metadata_files_count": len(folder_info["metadata_files"]),
            "total_files": folder_info["total_files"],
            "total_size_mb": folder_info.get("total_size_mb", 0)
        }
        
        # Add latest model info if available
        if folder_info["models"]:
            latest_model = max(folder_info["models"], key=lambda x: x["modified_time"])
            job_status["latest_model"] = {
                "filename": latest_model["name"],
                "path": latest_model["path"],
                "size_mb": round(latest_model["size_bytes"] / (1024 * 1024), 2),
                "modified": latest_model["modified_readable"]
            }
        
        # Add file system paths for easy access
        job_status["file_paths"] = {
            "job_folder": f"jobs/{job_id}",
            "models_folder": f"jobs/{job_id}/models",
            "images_folder": f"jobs/{job_id}/images", 
            "rigged_models_folder": f"jobs/{job_id}/rigged_models",
            "animations_folder": f"jobs/{job_id}/animations",
            "metadata_file": f"jobs/{job_id}/job_metadata.json"
        }
        
        # Add image availability info
        available_images = []
        for image_type, image_data in images_base64.items():
            if image_data:
                available_images.append({
                    "type": image_type,
                    "filename": image_data["filename"],
                    "size_bytes": image_data["size_bytes"],
                    "mime_type": image_data["mime_type"]
                })
        
        job_status["available_images"] = available_images
        job_status["images_loaded"] = len(available_images)
    
    return job_status

@app.post("/animate/image")
async def forge_artifact_from_image(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    animation_ids: str = Form(""),
    height_meters: float = Form(1.75),
    fps: int = Form(60),
    use_retexture: bool = Form(False),
    skip_confirmation: bool = Form(False)
):
    """🎨 Instant Forge: Transform uploaded image into animated 3D digital artifact.
    
    Args:
        file: Image file to convert to 3D
        animation_ids: Comma-separated animation IDs (empty = skip rigging)
        height_meters: Character height for rigging
        fps: Animation frame rate
        use_retexture: Whether to enhance textures
        skip_confirmation: If True, skip confirmation checkpoint (useful for workflows)
    """
    
    # Validate file type
    if not file.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    # Generate job ID
    job_id = str(uuid.uuid4())
    
    # Parse animation IDs (handle empty string for no animations)
    try:
        if animation_ids.strip():
            anim_ids = [int(x.strip()) for x in animation_ids.split(',')]
        else:
            anim_ids = []  # Empty list means skip rigging
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid animation_ids format")
    
    # Save uploaded file
    job_dir = f"jobs/{job_id}"
    os.makedirs(job_dir, exist_ok=True)
    
    file_extension = os.path.splitext(file.filename)[1]
    input_image_path = os.path.join(job_dir, f"input_image{file_extension}")
    
    with open(input_image_path, "wb") as f:
        content = await file.read()
        f.write(content)
    
    # Initialize forge status
    update_job_status(job_id, "queued", 0, "🏗️ Artifact queued for forging")
    
    # Log if skipping confirmation
    if skip_confirmation:
        print(f"⚡ [WORKFLOW] Job {job_id} will skip confirmation checkpoint")
    
    # Start background forging
    background_tasks.add_task(
        run_animation_pipeline,
        job_id, input_image_path, anim_ids, height_meters, fps, use_retexture, skip_confirmation
    )
    
    return {"forge_id": job_id, "status": "queued", "message": "🚀 Digital artifact forging initiated"}

@app.post("/animate/prompt")
async def forge_artifact_from_vision(
    background_tasks: BackgroundTasks,
    request: PromptRequest
):
    """🧠 Vision Forge: Transform creative text descriptions into stunning 3D digital artifacts."""
    
    job_id = str(uuid.uuid4())
    
    try:
        # Generate image from prompt using mapapi
        update_job_status(job_id, "processing", 5, "Generating image from prompt...")
        
        # Use mapapi to generate image from prompt
        image_path = await generate_image_from_prompt(job_id, request.prompt)
        
        if not image_path:
            raise RuntimeError("Failed to generate image from prompt")
        
        # Initialize forge status
        update_job_status(job_id, "queued", 10, "🎨 Vision materialized, initiating 3D artifact forging...")
        
        # Start background forging
        background_tasks.add_task(
            run_animation_pipeline,
            job_id, image_path, request.animation_ids, 
            request.height_meters, request.fps, request.use_retexture
        )
        
        return {"forge_id": job_id, "status": "queued", "message": "🧠 Vision forge initiated - your concept is becoming reality"}
        
    except Exception as e:
        update_job_status(job_id, "failed", 0, f"Failed to process prompt: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/animate/coordinates")
async def forge_digital_twin_from_location(
    background_tasks: BackgroundTasks,
    request: CoordinatesRequest
):
    """🌍 Location Forge: Create digital twin artifacts from real-world GPS coordinates using Street View data."""
    
    job_id = str(uuid.uuid4())
    
    try:
        # Generate Street View image and 3D model
        update_job_status(job_id, "processing", 5, "Fetching Google Street View...")
        
        # Use mapapi to get Street View and create 3D model
        model_path = await generate_from_coordinates(job_id, request.latitude, request.longitude)
        
        if not model_path:
            raise RuntimeError("Failed to generate 3D model from coordinates")
        
        # Initialize job status
        update_job_status(job_id, "queued", 10, "🌍 Digital twin captured, initiating 3D artifact forging...")
        
        # Start background processing
        background_tasks.add_task(
            run_animation_pipeline,
            job_id, model_path, request.animation_ids,
            request.height_meters, request.fps, request.use_retexture
        )
        
        return {"job_id": job_id, "status": "queued", "message": "Job started"}
        
    except Exception as e:
        update_job_status(job_id, "failed", 0, f"Failed to process coordinates: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/animate/infuse")
async def forge_fusion_artifact(
    background_tasks: BackgroundTasks,
    file1: UploadFile = File(..., description="First digital artifact to fuse"),
    file2: UploadFile = File(..., description="Second digital artifact to fuse"),
    animation_ids: str = "106,30,55",
    height_meters: float = 1.75,
    fps: int = 60,
    use_retexture: bool = True,
    fusion_style: str = "blend",
    fusion_strength: float = 0.5
):
    """🎭 Fusion Forge: Create unique hybrid artifacts by fusing two digital elements.
    
    This artisan tool uses COMPLETE database-only storage:
    - NO local folders created (updated to database-only)
    - Source images saved directly in database and S3
    - Fusion result saved directly in database
    - 3D model saved directly in database
    
    The fusion process:
    1. Analyzes two digital artifacts with AI vision
    2. Generates intelligent fusion prompts using GPT/Ollama
    3. Creates new hybrid artifacts with Gemini
    4. Forges stunning 3D models from the fusion results
    5. Saves everything in database and S3
    
    Args:
        file1: First digital artifact to fuse
        file2: Second digital artifact to fuse  
        animation_ids: Comma-separated animation IDs to apply
        height_meters: Character height for rigging
        fps: Animation frame rate
        use_retexture: Whether to enhance materials and textures
        fusion_style: Fusion technique ("blend", "merge", "hybrid")
        fusion_strength: Balance between artifacts (0.0-1.0)
    
    Returns:
        Forge ID and status for tracking the fusion process
    """
    
    # Validate file types
    if not file1.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File1 must be an image")
    if not file2.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File2 must be an image")
    
    # Validate fusion parameters
    if fusion_style not in ["blend", "merge", "hybrid"]:
        raise HTTPException(status_code=400, detail="fusion_style must be 'blend', 'merge', or 'hybrid'")
    if not (0.0 <= fusion_strength <= 1.0):
        raise HTTPException(status_code=400, detail="fusion_strength must be between 0.0 and 1.0")
    
    # Generate job ID
    job_id = str(uuid.uuid4())
    
    # Parse animation IDs
    try:
        anim_ids = [int(x.strip()) for x in animation_ids.split(',')]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid animation_ids format")
    
    try:
        # Read file contents into memory (NO LOCAL FOLDERS CREATED)
        print(f"🔀 Starting DATABASE-ONLY infusion job {job_id}")
        print(f"   Image 1: {file1.filename} (database-only storage)")
        print(f"   Image 2: {file2.filename} (database-only storage)")
        print(f"   Fusion Style: {fusion_style} (strength: {fusion_strength})")
        print(f"   Storage: Database + S3 only (NO local folders)")
        
        # Read file contents
        file1_content = await file1.read()
        file2_content = await file2.read()
        
        # Initialize job status with infusion metadata
        update_job_status(job_id, "queued", 0, "Database-only infusion job queued for processing", {
            "job_type": "infusion",
            "storage_type": "database_and_s3_only",
            "fusion_style": fusion_style,
            "fusion_strength": fusion_strength,
            "source_files": [file1.filename, file2.filename],
            "animation_ids": anim_ids,
            "note": "NO local folders created - using database + S3 storage"
        })
        
        # Start background infusion processing with database-only approach
        background_tasks.add_task(
            run_fusion_pipeline_database_only,
            job_id, file1_content, file2_content, file1.filename, file2.filename,
            fusion_style, fusion_strength, anim_ids, height_meters, fps, use_retexture
        )
        
        return {
            "job_id": job_id, 
            "status": "queued", 
            "message": "Database-only infusion job started - NO local folders created",
            "job_type": "infusion",
            "storage_type": "database_and_s3_only",
            "fusion_config": {
                "style": fusion_style,
                "strength": fusion_strength,
                "source_files": [file1.filename, file2.filename],
                "animation_ids": anim_ids
            },
            "storage_info": {
                "local_folders": "NONE - No local folders created",
                "database_storage": "YES - All images and results stored in database",
                "s3_storage": "YES - Source images uploaded to S3"
            }
        }
        
    except Exception as e:
        error_msg = f"Failed to initialize database-only infusion job: {str(e)}"
        update_job_status(job_id, "failed", 0, error_msg, {
            "job_type": "infusion",
            "storage_type": "database_and_s3_only"
        })
        raise HTTPException(status_code=500, detail=error_msg)

@app.post("/animate/infuse-database-only")
async def forge_fusion_artifact_database_only(
    background_tasks: BackgroundTasks,
    file1: UploadFile = File(..., description="First digital artifact to fuse"),
    file2: UploadFile = File(..., description="Second digital artifact to fuse"),
    animation_ids: str = "106,30,55",
    height_meters: float = 1.75,
    fps: int = 60,
    use_retexture: bool = True,
    fusion_style: str = "blend",
    fusion_strength: float = 0.5
):
    """🎭 Complete Database-Only Fusion: Create unique hybrid artifacts with zero local storage.
    
    This advanced artisan tool uses COMPLETE database-only storage:
    - NO local folders created
    - Source images saved directly in database as base64
    - Fusion result saved directly in database as base64
    - 3D model saved directly in database as base64
    - All processing uses temporary files that are immediately deleted
    
    The fusion process:
    1. Analyzes two digital artifacts with AI vision
    2. Generates intelligent fusion prompts using GPT/Ollama
    3. Creates new hybrid artifacts with Gemini
    4. Forges 3D models from the fusion results
    5. Saves everything in the database job record
    
    Args:
        file1: First digital artifact to fuse
        file2: Second digital artifact to fuse  
        animation_ids: Comma-separated animation IDs to apply
        height_meters: Character height for rigging
        fps: Animation frame rate
        use_retexture: Whether to enhance materials and textures
        fusion_style: Fusion technique ("blend", "merge", "hybrid")
        fusion_strength: Balance between artifacts (0.0-1.0)
    
    Returns:
        Forge ID and status for tracking the complete database-only fusion process
    """
    
    # Validate file types
    if not file1.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File1 must be an image")
    if not file2.content_type.startswith('image/'):
        raise HTTPException(status_code=400, detail="File2 must be an image")
    
    # Validate fusion parameters
    if fusion_style not in ["blend", "merge", "hybrid"]:
        raise HTTPException(status_code=400, detail="fusion_style must be 'blend', 'merge', or 'hybrid'")
    if not (0.0 <= fusion_strength <= 1.0):
        raise HTTPException(status_code=400, detail="fusion_strength must be between 0.0 and 1.0")
    
    # Generate job ID
    job_id = str(uuid.uuid4())
    
    # Parse animation IDs
    try:
        anim_ids = [int(x.strip()) for x in animation_ids.split(',')]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid animation_ids format")
    
    try:
        # Read file contents into memory (no local storage)
        print(f"🔀 Starting COMPLETE database-only fusion job {job_id}")
        print(f"   Image 1: {file1.filename}")
        print(f"   Image 2: {file2.filename}")
        print(f"   Fusion Style: {fusion_style} (strength: {fusion_strength})")
        print(f"   Storage: COMPLETE database-only (no local folders, no S3)")
        
        # Read file contents
        file1_content = await file1.read()
        file2_content = await file2.read()
        
        # Initialize job status with fusion metadata
        update_job_status(job_id, "queued", 0, "Complete database-only fusion job queued for processing", {
            "job_type": "fusion",
            "storage_type": "complete_database_only",
            "fusion_style": fusion_style,
            "fusion_strength": fusion_strength,
            "source_files": [file1.filename, file2.filename],
            "animation_ids": anim_ids,
            "note": "No local folders created - all images stored in database"
        })
        
        # Start background fusion processing with complete database-only approach
        background_tasks.add_task(
            run_fusion_pipeline_database_only,
            job_id, file1_content, file2_content, file1.filename, file2.filename,
            fusion_style, fusion_strength, anim_ids, height_meters, fps, use_retexture
        )
        
        return {
            "job_id": job_id, 
            "status": "queued", 
            "message": "Complete database-only fusion job started - all images will be stored in database",
            "job_type": "fusion",
            "storage_type": "complete_database_only",
            "fusion_config": {
                "style": fusion_style,
                "strength": fusion_strength,
                "source_files": [file1.filename, file2.filename],
                "animation_ids": anim_ids
            },
            "storage_info": {
                "local_folders": "NONE - No local folders created",
                "s3_storage": "NONE - All images stored in database",
                "database_storage": "YES - Source images, fusion result, and 3D model stored as base64"
            }
        }
        
    except Exception as e:
        error_msg = f"Failed to initialize complete database-only fusion job: {str(e)}"
        update_job_status(job_id, "failed", 0, error_msg, {
            "job_type": "fusion", 
            "storage_type": "complete_database_only"
        })
        raise HTTPException(status_code=500, detail=error_msg)

@app.get("/jobs/{job_id}/fusion-images/{image_type}")
async def get_fusion_image_from_database(job_id: str, image_type: str):
    """Get fusion images stored in the database.
    
    Args:
        job_id: Job identifier
        image_type: Type of image ('source_1', 'source_2', 'fusion', 'model')
    
    Returns:
        Image content or 3D model content from database
    """
    try:
        # Get job from database
        job = get_job_status(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        
        result = job.get("result", {})
        
        if image_type == "source_1":
            source_images = result.get("source_images", {})
            source_1 = source_images.get("source_image_1", {})
            if not source_1.get("content_base64"):
                raise HTTPException(status_code=404, detail="Source image 1 not found in database")
            
            import base64
            image_content = base64.b64decode(source_1["content_base64"])
            filename = source_1.get("filename", "source_1.jpg")
            content_type = source_1.get("content_type", "image/jpeg")
            
            return Response(
                content=image_content,
                media_type=content_type,
                headers={"Content-Disposition": f"inline; filename={filename}"}
            )
            
        elif image_type == "source_2":
            source_images = result.get("source_images", {})
            source_2 = source_images.get("source_image_2", {})
            if not source_2.get("content_base64"):
                raise HTTPException(status_code=404, detail="Source image 2 not found in database")
            
            import base64
            image_content = base64.b64decode(source_2["content_base64"])
            filename = source_2.get("filename", "source_2.jpg")
            content_type = source_2.get("content_type", "image/jpeg")
            
            return Response(
                content=image_content,
                media_type=content_type,
                headers={"Content-Disposition": f"inline; filename={filename}"}
            )
            
        elif image_type == "fusion":
            fusion_image = result.get("fusion_image", {})
            if not fusion_image.get("content_base64"):
                raise HTTPException(status_code=404, detail="Fusion image not found in database")
            
            import base64
            image_content = base64.b64decode(fusion_image["content_base64"])
            content_type = fusion_image.get("content_type", "image/png")
            
            return Response(
                content=image_content,
                media_type=content_type,
                headers={"Content-Disposition": f"inline; filename=fusion_{job_id}.png"}
            )
            
        elif image_type == "model":
            model_3d = result.get("model_3d", {})
            if not model_3d.get("content_base64"):
                raise HTTPException(status_code=404, detail="3D model not found in database")
            
            import base64
            model_content = base64.b64decode(model_3d["content_base64"])
            filename = model_3d.get("filename", f"fusion_model_{job_id}.glb")
            content_type = model_3d.get("content_type", "model/gltf-binary")
            
            return Response(
                content=model_content,
                media_type=content_type,
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
            
        else:
            raise HTTPException(status_code=400, detail="Invalid image_type. Use: source_1, source_2, fusion, or model")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error retrieving image: {str(e)}")

@app.post("/animate/wan")
async def animate_with_wan(
    request: WanAnimateRequest
):
    pass

@app.post("/animate/wan-fast")
async def animate_wan_22_fast(
    request: Wan22FastRequest,
    background_tasks: BackgroundTasks
):
    """🎬 WAN 2.2 i2v-fast: Fast image-to-video animation via Replicate
    
    High-quality image animation using WAN 2.2 i2v-fast model from Replicate.
    
    **Pricing:**
    - 480p resolution: **$0.05** per output video
    - 720p resolution: **$0.11** per output video
    
    **Features:**
    - Fast processing mode with go_fast option
    - Smooth transitions with optional last frame
    - Customizable frame count and FPS
    - Interpolation to 30 FPS
    - Sample shift control for motion quality
    - Optional safety checker
    
    **Parameters:**
    - image: URL of image to animate (required)
    - prompt: Animation description (required)
    - resolution: "480p" or "720p" (default: 480p)
    - go_fast: Fast processing mode (default: True)
    - num_frames: Number of frames, 81 recommended (default: 81)
    - sample_shift: Motion quality factor 1-20 (default: 12)
    - frames_per_second: Output FPS 5-30 (default: 16)
    - last_image: Optional final frame URL for smooth transitions
    - interpolate_output: Interpolate to 30 FPS (default: True)
    
    **Returns:**
    - Job ID for status tracking
    - Estimated cost based on resolution
    - Polling URL for result
    """
    
    try:
        # Check if Replicate API key is configured
        if not REPLICATE_API_KEY:
            raise HTTPException(
                status_code=503,
                detail="Replicate API not configured. Please set REPLICATE_API_KEY environment variable."
            )
        
        # Validate required fields
        if not request.image:
            raise HTTPException(status_code=400, detail="image URL is required")
        if not request.prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        
        # Validate resolution
        if request.resolution not in ["480p", "720p"]:
            raise HTTPException(
                status_code=400,
                detail="resolution must be '480p' or '720p'"
            )
        
        # Calculate pricing
        pricing = {
            "480p": 0.05,
            "720p": 0.11
        }
        estimated_cost = pricing[request.resolution]
        
        # Generate job ID
        job_id = f"wan_fast_{str(uuid.uuid4())}"
        
        print(f"🎬 WAN 2.2 i2v-fast animation request:")
        print(f"   Job ID: {job_id}")
        print(f"   Image: {request.image[:100]}...")
        print(f"   Prompt: {request.prompt}")
        print(f"   Resolution: {request.resolution}")
        print(f"   Estimated cost: ${estimated_cost}")
        print(f"   Go fast: {request.go_fast}")
        print(f"   Num frames: {request.num_frames}")
        print(f"   FPS: {request.frames_per_second}")
        print(f"   Sample shift: {request.sample_shift}")
        
        # Create initial job status
        update_job_status(
            job_id=job_id,
            status="processing",
            progress=10,
            message="Starting WAN 2.2 i2v-fast animation...",
            result={
                "job_type": "wan_22_fast_animation",
                "image": request.image,
                "prompt": request.prompt,
                "resolution": request.resolution,
                "estimated_cost": estimated_cost,
                "settings": {
                    "go_fast": request.go_fast,
                    "num_frames": request.num_frames,
                    "sample_shift": request.sample_shift,
                    "frames_per_second": request.frames_per_second,
                    "interpolate_output": request.interpolate_output
                }
            }
        )
        
        # Process animation in background
        def process_wan_fast():
            try:
                import replicate
                
                print(f"🚀 Calling Replicate WAN 2.2 i2v-fast model...")
                
                update_job_status(
                    job_id=job_id,
                    status="processing",
                    progress=30,
                    message="Generating animation with WAN 2.2 i2v-fast..."
                )
                
                # Prepare input parameters
                input_params = {
                    "image": request.image,
                    "prompt": request.prompt,
                    "go_fast": request.go_fast,
                    "num_frames": request.num_frames,
                    "resolution": request.resolution,
                    "sample_shift": request.sample_shift,
                    "frames_per_second": request.frames_per_second,
                    "interpolate_output": request.interpolate_output,
                    "disable_safety_checker": request.disable_safety_checker,
                    "lora_scale_transformer": request.lora_scale_transformer,
                    "lora_scale_transformer_2": request.lora_scale_transformer_2
                }
                
                # Add optional parameters if provided
                if request.last_image:
                    input_params["last_image"] = request.last_image
                
                # Run Replicate model with authentication
                import replicate
                client = replicate.Client(api_token=REPLICATE_API_KEY)
                output = client.run(
                    "wan-video/wan-2.2-i2v-fast",
                    input=input_params
                )
                
                update_job_status(
                    job_id=job_id,
                    status="processing",
                    progress=70,
                    message="Downloading and processing video..."
                )
                
                # Get video URL from output
                if isinstance(output, str):
                    video_url = output
                elif hasattr(output, 'url'):
                    # url might be a property or method
                    if callable(output.url):
                        video_url = output.url()
                    else:
                        video_url = output.url
                elif isinstance(output, list) and len(output) > 0:
                    video_url = str(output[0])
                else:
                    video_url = str(output)
                
                print(f"✅ WAN 2.2 i2v-fast animation completed: {video_url[:100]}...")
                
                # Download video to local storage
                out_dir = "generated"
                os.makedirs(out_dir, exist_ok=True)
                timestamp = int(time.time())
                out_filename = f"wan_fast_{job_id}_{timestamp}.mp4"
                out_path = os.path.join(out_dir, out_filename)
                
                # Download video
                video_response = requests.get(video_url, timeout=120)
                video_response.raise_for_status()
                
                with open(out_path, 'wb') as f:
                    f.write(video_response.content)
                
                print(f"✅ Video saved to: {out_path}")
                
                # Upload to S3 if available
                s3_url = None
                try:
                    s3_url = upload_asset_to_s3(out_path, "animation", job_id, out_filename)
                    if s3_url:
                        print(f"✅ Video uploaded to S3: {s3_url}")
                except Exception as s3_error:
                    print(f"⚠️ S3 upload failed: {s3_error}")
                
                # Update job with completion
                update_job_status(
                    job_id=job_id,
                    status="completed",
                    progress=100,
                    message=f"WAN 2.2 i2v-fast animation completed! Cost: ${estimated_cost}",
                    result={
                        "job_type": "wan_22_fast_animation",
                        "video_url": video_url,
                        "video_s3_url": s3_url,
                        "local_path": out_path,
                        "image": request.image,
                        "prompt": request.prompt,
                        "resolution": request.resolution,
                        "actual_cost": estimated_cost,
                        "settings": {
                            "go_fast": request.go_fast,
                            "num_frames": request.num_frames,
                            "sample_shift": request.sample_shift,
                            "frames_per_second": request.frames_per_second,
                            "interpolate_output": request.interpolate_output
                        },
                        "model": "wan-video/wan-2.2-i2v-fast",
                        "provider": "Replicate"
                    }
                )
                
            except Exception as e:
                print(f"❌ WAN 2.2 i2v-fast error: {e}")
                update_job_status(
                    job_id=job_id,
                    status="failed",
                    progress=0,
                    message=f"Animation failed: {str(e)}",
                    result={
                        "job_type": "wan_22_fast_animation",
                        "error": str(e),
                        "image": request.image,
                        "prompt": request.prompt
                    }
                )
        
        # Submit to thread pool
        thread_pool.submit(process_wan_fast)
        
        # Return immediate response
        return JSONResponse(content={
            "status": "processing",
            "job_id": job_id,
            "message": "WAN 2.2 i2v-fast animation started",
            "progress": 10,
            "estimated_cost": f"${estimated_cost}",
            "pricing_info": {
                "resolution": request.resolution,
                "cost_per_video": f"${estimated_cost}",
                "currency": "USD"
            },
            "settings": {
                "image": request.image,
                "prompt": request.prompt,
                "resolution": request.resolution,
                "go_fast": request.go_fast,
                "num_frames": request.num_frames,
                "frames_per_second": request.frames_per_second,
                "sample_shift": request.sample_shift
            },
            "polling_url": f"/status/{job_id}",
            "estimated_time": "60-180 seconds depending on resolution and settings"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ WAN 2.2 i2v-fast endpoint error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start WAN 2.2 i2v-fast animation: {str(e)}"
        )

@app.post("/animate/wan-animate")
async def animate_wan_22_animate(
    request: Wan22AnimateRequest,
    background_tasks: BackgroundTasks
):
    """🎬 WAN 2.2 Animate-Animation: Transfer motion from video to character via Replicate
    
    Transfer motion and actions from a reference video to animate a character image.
    Perfect for creating character animations by using motion reference videos.
    
    **Pricing:**
    - Approximately **333 seconds for $1**
    - Cost: **~$0.003 per second** (0.3 cents/sec)
    - Example: 5-second video = **~$0.015** (1.5 cents)
    - Example: 30-second video = **~$0.09** (9 cents)
    
    **Features:**
    - Transfer motion from any reference video
    - Animate character images with realistic movement
    - Merge audio from reference video
    - Fast processing mode available
    - Multiple resolution options
    
    **Parameters:**
    - video: Reference video URL showing the desired movement (required)
    - character_image: Character image URL to animate (required)
    - resolution: "480", "720", or "1080" (default: 720)
    - go_fast: Fast processing mode (default: True)
    - refert_num: Reference number (default: 1)
    - frames_per_second: Output FPS 5-30 (default: 24)
    - merge_audio: Include audio from reference video (default: True)
    
    **Returns:**
    - Job ID for status tracking
    - Estimated cost per second
    - Polling URL for result
    """
    
    try:
        # Check if Replicate API key is configured
        if not REPLICATE_API_KEY:
            raise HTTPException(
                status_code=503,
                detail="Replicate API not configured. Please set REPLICATE_API_KEY environment variable."
            )
        
        # Validate required fields
        if not request.video:
            raise HTTPException(status_code=400, detail="video URL is required")
        if not request.character_image:
            raise HTTPException(status_code=400, detail="character_image URL is required")
        
        # Validate resolution
        if request.resolution not in ["480", "720", "1080"]:
            raise HTTPException(
                status_code=400,
                detail="resolution must be '480', '720', or '1080'"
            )
        
        # Calculate pricing (approximately 333 seconds for $1)
        cost_per_second = 1.0 / 333.0  # ~$0.003 per second
        
        # Generate job ID
        job_id = f"wan_animate_{str(uuid.uuid4())}"
        
        print(f"🎬 WAN 2.2 Animate-Animation request:")
        print(f"   Job ID: {job_id}")
        print(f"   Video: {request.video[:100]}...")
        print(f"   Character: {request.character_image[:100]}...")
        print(f"   Resolution: {request.resolution}")
        print(f"   Cost per second: ${cost_per_second:.4f}")
        print(f"   Go fast: {request.go_fast}")
        print(f"   FPS: {request.frames_per_second}")
        print(f"   Merge audio: {request.merge_audio}")
        
        # Create initial job status
        update_job_status(
            job_id=job_id,
            status="processing",
            progress=10,
            message="Starting WAN 2.2 Animate-Animation...",
            result={
                "job_type": "wan_22_animate_animation",
                "video": request.video,
                "character_image": request.character_image,
                "resolution": request.resolution,
                "cost_per_second": cost_per_second,
                "settings": {
                    "go_fast": request.go_fast,
                    "refert_num": request.refert_num,
                    "frames_per_second": request.frames_per_second,
                    "merge_audio": request.merge_audio
                }
            }
        )
        
        # Process animation in background
        def process_wan_animate():
            try:
                import replicate
                
                print(f"🚀 Calling Replicate WAN 2.2 Animate-Animation model...")
                
                update_job_status(
                    job_id=job_id,
                    status="processing",
                    progress=30,
                    message="Transferring motion from video to character..."
                )
                
                # Prepare input parameters
                input_params = {
                    "video": request.video,
                    "character_image": request.character_image,
                    "go_fast": request.go_fast,
                    "refert_num": request.refert_num,
                    "resolution": request.resolution,
                    "merge_audio": request.merge_audio,
                    "frames_per_second": request.frames_per_second
                }
                
                # Run Replicate model with authentication
                start_time = time.time()
                import replicate
                client = replicate.Client(api_token=REPLICATE_API_KEY)
                output = client.run(
                    "wan-video/wan-2.2-animate-animation",
                    input=input_params
                )
                processing_time = time.time() - start_time
                
                update_job_status(
                    job_id=job_id,
                    status="processing",
                    progress=70,
                    message="Downloading and processing animated video..."
                )
                
                # Get video URL from output
                if isinstance(output, str):
                    video_url = output
                elif hasattr(output, 'url'):
                    # url might be a property or method
                    if callable(output.url):
                        video_url = output.url()
                    else:
                        video_url = output.url
                elif isinstance(output, list) and len(output) > 0:
                    video_url = str(output[0])
                else:
                    video_url = str(output)
                
                print(f"✅ WAN 2.2 Animate-Animation completed: {video_url[:100]}...")
                print(f"⏱️ Processing time: {processing_time:.2f} seconds")
                
                # Calculate actual cost
                actual_cost = processing_time * cost_per_second
                
                # Download video to local storage
                out_dir = "generated"
                os.makedirs(out_dir, exist_ok=True)
                timestamp = int(time.time())
                out_filename = f"wan_animate_{job_id}_{timestamp}.mp4"
                out_path = os.path.join(out_dir, out_filename)
                
                # Download video
                video_response = requests.get(video_url, timeout=120)
                video_response.raise_for_status()
                
                with open(out_path, 'wb') as f:
                    f.write(video_response.content)
                
                print(f"✅ Video saved to: {out_path}")
                
                # Upload to S3 if available
                s3_url = None
                try:
                    s3_url = upload_asset_to_s3(out_path, "animation", job_id, out_filename)
                    if s3_url:
                        print(f"✅ Video uploaded to S3: {s3_url}")
                except Exception as s3_error:
                    print(f"⚠️ S3 upload failed: {s3_error}")
                
                # Update job with completion
                update_job_status(
                    job_id=job_id,
                    status="completed",
                    progress=100,
                    message=f"WAN 2.2 Animate-Animation completed! Cost: ${actual_cost:.4f} ({processing_time:.1f}s)",
                    result={
                        "job_type": "wan_22_animate_animation",
                        "video_url": video_url,
                        "video_s3_url": s3_url,
                        "local_path": out_path,
                        "reference_video": request.video,
                        "character_image": request.character_image,
                        "resolution": request.resolution,
                        "processing_time_seconds": processing_time,
                        "cost_per_second": cost_per_second,
                        "actual_cost": actual_cost,
                        "settings": {
                            "go_fast": request.go_fast,
                            "refert_num": request.refert_num,
                            "frames_per_second": request.frames_per_second,
                            "merge_audio": request.merge_audio
                        },
                        "model": "wan-video/wan-2.2-animate-animation",
                        "provider": "Replicate"
                    }
                )
                
            except Exception as e:
                print(f"❌ WAN 2.2 Animate-Animation error: {e}")
                update_job_status(
                    job_id=job_id,
                    status="failed",
                    progress=0,
                    message=f"Animation failed: {str(e)}",
                    result={
                        "job_type": "wan_22_animate_animation",
                        "error": str(e),
                        "video": request.video,
                        "character_image": request.character_image
                    }
                )
        
        # Submit to thread pool
        thread_pool.submit(process_wan_animate)
        
        # Return immediate response
        return JSONResponse(content={
            "status": "processing",
            "job_id": job_id,
            "message": "WAN 2.2 Animate-Animation started",
            "progress": 10,
            "cost_per_second": f"${cost_per_second:.4f}",
            "pricing_info": {
                "cost_per_second": f"${cost_per_second:.4f}",
                "seconds_per_dollar": 333,
                "currency": "USD",
                "example_5s": "$0.015 (1.5 cents)",
                "example_30s": "$0.09 (9 cents)"
            },
            "settings": {
                "video": request.video,
                "character_image": request.character_image,
                "resolution": request.resolution,
                "go_fast": request.go_fast,
                "frames_per_second": request.frames_per_second,
                "merge_audio": request.merge_audio
            },
            "polling_url": f"/status/{job_id}",
            "estimated_time": "Varies based on video length and resolution"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ WAN 2.2 Animate-Animation endpoint error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start WAN 2.2 Animate-Animation: {str(e)}"
        )

@app.post("/generate/ideogram-turbo")
async def generate_ideogram_v3_turbo(
    request: IdeogramV3TurboRequest,
    background_tasks: BackgroundTasks
):
    """🎨 Ideogram V3 Turbo: Add text on images with high-quality typography
    
    Generate images with professional text rendering using Ideogram V3 Turbo from Replicate.
    Perfect for creating titles, logos, posters, and text-based designs.
    
    **Pricing:**
    - **$0.03 per output image**
    
    **Modes:**
    - **Text-to-Image**: Generate new images with text (default)
    - **Inpainting**: Modify existing images with mask (requires both image + mask)
    
    **Features:**
    - High-quality text rendering with perfect typography
    - Multiple aspect ratios supported
    - Inpainting support for targeted image editing
    - Magic prompt enhancement for better results
    - Fast generation (Turbo model)
    
    **Parameters:**
    - prompt: Text to render and style description (required)
    - image: Optional input image URL for inpainting (requires mask)
    - mask: Optional mask image URL (white areas will be replaced, requires image)
    - aspect_ratio: Image dimensions (default: 3:2)
    - resolution: Output size (default: 1024x1024)
    - magic_prompt_option: Auto, On, or Off (default: Auto)
    - style_type: Optional style preset
    
    **Inpainting Usage:**
    To use inpainting mode, provide BOTH image and mask:
    - image: Base image to modify
    - mask: White areas indicate where to generate new content
    
    **Returns:**
    - Job ID for status tracking
    - Cost per image ($0.03)
    - Polling URL for result
    """
    
    try:
        # Check if Replicate API key is configured
        if not REPLICATE_API_KEY:
            raise HTTPException(
                status_code=503,
                detail="Replicate API not configured. Please set REPLICATE_API_KEY environment variable."
            )
        
        # Validate required fields
        if not request.prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        
        # Generate job ID
        job_id = f"ideogram_turbo_{str(uuid.uuid4())}"
        
        print(f"🎨 Ideogram V3 Turbo request:")
        print(f"   Job ID: {job_id}")
        print(f"   Prompt: {request.prompt[:150]}...")
        if request.image:
            print(f"   Input image: {request.image[:100]}...")
        if request.mask:
            print(f"   Mask image: {request.mask[:100]}...")
        print(f"   Aspect ratio: {request.aspect_ratio}")
        print(f"   Cost: $0.03")
        
        # Create initial job status
        update_job_status(
            job_id=job_id,
            status="processing",
            progress=10,
            message="Starting Ideogram V3 Turbo generation...",
            result={
                "job_type": "ideogram_v3_turbo",
                "prompt": request.prompt,
                "aspect_ratio": request.aspect_ratio,
                "cost_per_image": 0.03,
                "generation_mode": "inpainting" if (request.image and request.mask) else "text-to-image",
                "settings": {
                    "resolution": request.resolution,
                    "magic_prompt_option": request.magic_prompt_option,
                    "style_type": request.style_type,
                    "input_image": request.image,
                    "mask_image": request.mask
                }
            }
        )
        
        # Process generation in background
        def process_ideogram_turbo():
            try:
                import replicate
                
                print(f"🚀 Calling Replicate Ideogram V3 Turbo model...")
                
                update_job_status(
                    job_id=job_id,
                    status="processing",
                    progress=30,
                    message="Generating image with text rendering..."
                )
                
                # Prepare input parameters
                input_params = {
                    "prompt": request.prompt,
                    "aspect_ratio": request.aspect_ratio
                }
                
                # Add optional parameters if provided
                if request.resolution:
                    input_params["resolution"] = request.resolution
                if request.magic_prompt_option:
                    input_params["magic_prompt_option"] = request.magic_prompt_option
                if request.style_type:
                    input_params["style_type"] = request.style_type
                
                # Handle image and mask for inpainting mode
                if request.image and request.mask:
                    # INPAINTING MODE: Both image and mask provided
                    input_params["image"] = request.image
                    input_params["mask"] = request.mask
                    print(f"🎨 INPAINTING MODE: Using image with mask")
                    print(f"   • Image: {request.image[:80]}...")
                    print(f"   • Mask: {request.mask[:80]}...")
                elif request.image and not request.mask:
                    # ERROR: Image without mask
                    print(f"   ⚠️  Input image provided without mask - IGNORED")
                    print(f"   ⚠️  Note: Ideogram V3 Turbo requires BOTH image and mask for inpainting")
                    print(f"   ⚠️  Falling back to text-only generation")
                elif request.mask and not request.image:
                    # ERROR: Mask without image
                    print(f"   ⚠️  Mask provided without image - IGNORED")
                    print(f"   ⚠️  Note: Mask requires an input image")
                    print(f"   ⚠️  Falling back to text-only generation")
                
                generation_mode = "inpainting" if (request.image and request.mask) else "text-to-image"
                print(f"📝 Generation mode: {generation_mode.upper()}")
                print(f"   • Prompt: {request.prompt[:100]}...")
                print(f"   • Aspect ratio: {request.aspect_ratio}")
                print(f"   • Resolution: {request.resolution}")
                print(f"   • Magic prompt: {request.magic_prompt_option}")
                
                # Run Replicate model with authentication
                start_time = time.time()
                import replicate
                client = replicate.Client(api_token=REPLICATE_API_KEY)
                output = client.run(
                    "ideogram-ai/ideogram-v3-turbo",
                    input=input_params
                )
                processing_time = time.time() - start_time
                
                update_job_status(
                    job_id=job_id,
                    status="processing",
                    progress=70,
                    message="Downloading and processing image..."
                )
                
                # Get image URL from output
                if isinstance(output, str):
                    image_url = output
                elif hasattr(output, 'url'):
                    # url might be a property or method
                    if callable(output.url):
                        image_url = output.url()
                    else:
                        image_url = output.url
                elif isinstance(output, list) and len(output) > 0:
                    image_url = str(output[0])
                else:
                    image_url = str(output)
                
                print(f"✅ Ideogram V3 Turbo generation completed: {image_url[:100]}...")
                print(f"⏱️ Processing time: {processing_time:.2f} seconds")
                
                # Download image to local storage
                out_dir = "generated"
                os.makedirs(out_dir, exist_ok=True)
                timestamp = int(time.time())
                out_filename = f"ideogram_turbo_{job_id}_{timestamp}.png"
                out_path = os.path.join(out_dir, out_filename)
                
                # Download image
                img_response = requests.get(image_url, timeout=120)
                img_response.raise_for_status()
                
                with open(out_path, 'wb') as f:
                    f.write(img_response.content)
                
                print(f"✅ Image saved to: {out_path}")
                
                # Upload to S3 if available
                s3_url = None
                try:
                    s3_url = upload_asset_to_s3(out_path, "image", job_id, out_filename)
                    if s3_url:
                        print(f"✅ Image uploaded to S3: {s3_url}")
                except Exception as s3_error:
                    print(f"⚠️ S3 upload failed: {s3_error}")
                
                # Update job with completion
                update_job_status(
                    job_id=job_id,
                    status="completed",
                    progress=100,
                    message=f"Ideogram V3 Turbo generation completed! Cost: $0.03",
                    result={
                        "job_type": "ideogram_v3_turbo",
                        "image_url": image_url,
                        "image_s3_url": s3_url,
                        "local_path": out_path,
                        "prompt": request.prompt,
                        "aspect_ratio": request.aspect_ratio,
                        "processing_time_seconds": processing_time,
                        "cost_per_image": 0.03,
                        "actual_cost": 0.03,
                        "settings": {
                            "resolution": request.resolution,
                            "magic_prompt_option": request.magic_prompt_option,
                            "style_type": request.style_type,
                            "input_image": request.image
                        },
                        "model": "ideogram-ai/ideogram-v3-turbo",
                        "provider": "Replicate"
                    }
                )
                
            except Exception as e:
                print(f"❌ Ideogram V3 Turbo error: {e}")
                update_job_status(
                    job_id=job_id,
                    status="failed",
                    progress=0,
                    message=f"Generation failed: {str(e)}",
                    result={
                        "job_type": "ideogram_v3_turbo",
                        "error": str(e),
                        "prompt": request.prompt
                    }
                )
        
        # Submit to thread pool
        thread_pool.submit(process_ideogram_turbo)
        
        # Return immediate response
        return JSONResponse(content={
            "status": "processing",
            "job_id": job_id,
            "message": "Ideogram V3 Turbo generation started",
            "progress": 10,
            "cost_per_image": "$0.03",
            "pricing_info": {
                "cost_per_image": "$0.03",
                "currency": "USD"
            },
            "settings": {
                "prompt": request.prompt,
                "aspect_ratio": request.aspect_ratio,
                "resolution": request.resolution,
                "input_image": request.image,
                "magic_prompt_option": request.magic_prompt_option
            },
            "polling_url": f"/status/{job_id}",
            "estimated_time": "10-30 seconds for text rendering"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Ideogram V3 Turbo endpoint error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start Ideogram V3 Turbo generation: {str(e)}"
        )

@app.post("/animate/luma")
async def animate_luma(
    request: LumaRayRequest,
    background_tasks: BackgroundTasks
):
    """Animate an image using WAN 2.2 API from WaveSpeed.ai
    
    This endpoint provides image animation using WAN 2.2 technology. It takes an image URL
    and animation prompt, then returns an animated video URL.
    
    Args:
        request: WanAnimateRequest containing:
            - image: URL of the image to animate
            - prompt: Description of the animation to apply
            - resolution: Video resolution (default: "480p")
            - video: Optional reference video URL
            - seed: Random seed for reproducible results (default: 42)
    
    Returns:
        Animation result with video URL and metadata
    """
    
    try:
        # Validate required fields
        if not request.image:
            raise HTTPException(status_code=400, detail="image URL is required")
        if not request.prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        
        print(f"🎬 WAN 2.2 animation request received:")
        print(f"   Image: {request.image[:100]}...")
        print(f"   Prompt: {request.prompt}")
        print(f"   Resolution: {request.resolution}")
        print(f"   Seed: {request.seed}")
        if request.video:
            print(f"   Reference Video: {request.video[:100]}...")
        
        # Call the WAN animate function
        result = await wan_animate_image(request)
        
        if result.get("success"):
            return {
                "success": True,
                "animation_url": result["animation_url"],
                "request_id": result["request_id"],
                "processing_time": result["processing_time"],
                "metadata": {
                    "prompt": result["prompt"],
                    "resolution": result["resolution"],
                    "seed": result["seed"],
                    "original_image": request.image,
                    "reference_video": request.video,
                    "status": "completed"
                },
                "message": f"Image animated successfully in {result['processing_time']:.2f} seconds"
            }
        else:
            # Animation failed
            error_msg = result.get("error", "Unknown animation error")
            status_code = 500 if result.get("status") == "error" else 408 if result.get("status") == "timeout" else 422
            
            return JSONResponse(
                status_code=status_code,
                content={
                    "success": False,
                    "error": error_msg,
                    "request_id": result.get("request_id"),
                    "metadata": {
                        "prompt": result["prompt"],
                        "original_image": request.image,
                        "status": result.get("status", "unknown")
                    },
                    "message": f"Animation failed: {error_msg}"
                }
            )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ WAN animate endpoint error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error during animation: {str(e)}"
        )

@app.post("/animate/luma")
async def luma_labs_animate(
    file: Optional[UploadFile] = File(None, description="Image file for keyframe (optional)"),
    prompt: str = Form(..., description="Animation prompt describing the scene/motion"),
    model: str = Form("ray-2", description="Model to use (default: ray-2)"),
    resolution: str = Form("720p", description="Video resolution: 720p or 1080p"),
    duration: str = Form("5s", description="Video duration: 5s or 10s"),
    concepts: Optional[str] = Form(None, description="Comma-separated list of concept keys (e.g., 'dolly_zoom,pan_left')"),
    image_url: Optional[str] = Form(None, description="URL of keyframe image (optional if file provided)"),
    preserve_background: bool = Form(True, description="Preserve background and non-animated elements"),
    asset: Optional[str] = Form(None, description="Specific asset/object to animate (e.g., 'the red car', 'the woman in blue'). Only this asset will be animated, everything else stays frozen."),
    freeze_assets: Optional[str] = Form(None, description="Comma-separated list of assets that must remain frozen (e.g., 'the character, the building, trees'). These will be explicitly kept static during animation."),
    position: Optional[str] = Form(None, description="Bounding box for animation area: 'x,y,width,height' in pixels (e.g., '100,200,300,400'). Only the specified region will be animated.")
):
    """🎬 Luma Labs Dream Machine: Generate video with Ray-2 model + AI Vision Analysis
    
    This endpoint uses Luma Labs Dream Machine API to generate videos with optional image-to-video
    and concept-based camera movements, plus **AI-powered Llama 3.2 Vision analysis** for intelligent
    freeze/animate control that ensures only what you want changes - nothing more.
    
    **🤖 NEW: AI Vision-Powered Precision**
    - Automatically analyzes image with Llama 3.2 Vision (if image provided)
    - Understands complete scene context: all objects, characters, environment
    - Creates detailed prompts explaining EXACTLY what to change and what NOT to change
    - Prevents expensive mistakes by being crystal clear about frozen vs animated elements
    
    **Features:**
    - Text-to-video generation
    - Image-to-video animation with keyframes
    - Concept-based camera effects (dolly_zoom, pan, etc.)
    - **AI Vision scene analysis for precision control**
    - **Intelligent prompt enhancement based on scene understanding**
    - Selective animation with asset/freeze_assets parameters
    - 720p or 1080p resolution
    - 5s or 10s duration
    
    **How AI Vision Helps:**
    When you provide an image + asset/freeze_assets parameters, the system:
    1. Analyzes image with Llama 3.2 Vision to understand ALL scene elements
    2. Combines vision analysis with your parameters
    3. Creates a detailed prompt that explicitly states:
       - What elements to keep completely frozen (appearance, position, colors, textures)
       - What elements can animate
       - Preservation instructions for lighting, composition, environment
    4. Sends this enhanced prompt to Luma Labs for precise animation
    
    **Why This Matters:**
    Each Luma Labs generation costs money. AI Vision analysis ensures you get exactly what
    you want on the first try by being explicit about what should and shouldn't change.
    
    **Example concepts:**
    - dolly_zoom
    - pan_left, pan_right
    - tilt_up, tilt_down
    - zoom_in, zoom_out
    
    **Selective Animation Examples:**
    
    **Example 1: Animate specific object only**
    ```bash
    curl -X POST "http://localhost:8000/animate/luma" \\
      -F "image_url=https://example.com/street_scene.jpg" \\
      -F "prompt=drives forward" \\
      -F "asset=the red car" \\
      -F "freeze_assets=people, buildings, trees"
    ```
    AI Vision will:
    - Analyze entire scene (people, cars, buildings, trees, sky, etc.)
    - Create prompt: "Scene contains [full analysis]. PRESERVE COMPLETELY: people, buildings, 
      trees, all colors, lighting, textures. ANIMATE ONLY: the red car drives forward."
    
    **Example 2: Freeze specific assets**
    ```bash
    curl -X POST "http://localhost:8000/animate/luma" \\
      -F "file=@character_scene.jpg" \\
      -F "prompt=add wind and movement" \\
      -F "freeze_assets=the main character, the bench"
    ```
    AI Vision will:
    - Identify all scene elements
    - Create prompt specifying character and bench must stay perfectly static
    - Allow natural movement for other elements (trees, grass, etc.)
    
    **Example 3: Text-to-video (no vision needed)**
    ```bash
    curl -X POST "http://localhost:8000/animate/luma" \\
      -F "prompt=A tiger walking in snow" \\
      -F "concepts=dolly_zoom" \\
      -F "resolution=1080p"
    ```
    """
    try:
        job_id = str(uuid.uuid4())
        print(f"\n{'='*80}")
        print(f"🎬 LUMA LABS DREAM MACHINE ANIMATION - Job ID: {job_id}")
        print(f"{'='*80}")
        
        # Get API key
        LUMA_3_KEY = os.environ.get("LUMA_3_KEY")
        if not LUMA_3_KEY:
            raise HTTPException(status_code=500, detail="LUMA_3_KEY not found in environment variables")
        
        # Handle image input
        keyframe_url = None
        temp_image_path = None
        image_source_url = None
        
        if file:
            print(f"📤 Processing uploaded image file: {file.filename}")
            # Save uploaded file temporarily
            job_dir = f"jobs/{job_id}"
            os.makedirs(job_dir, exist_ok=True)
            temp_image_path = os.path.join(job_dir, f"input_{file.filename}")
            
            with open(temp_image_path, "wb") as f:
                content = await file.read()
                f.write(content)
            
            # Upload to S3 and get URL
            print(f"☁️ Uploading image to S3...")
            keyframe_url = upload_asset_to_s3(temp_image_path, "keyframes", job_id, f"keyframe_{file.filename}")
            image_source_url = keyframe_url
            print(f"✅ Image uploaded: {keyframe_url}")
            
        elif image_url:
            keyframe_url = image_url
            image_source_url = image_url
            print(f"🔗 Using provided image URL: {keyframe_url}")
            
            # Download image for vision analysis
            job_dir = f"jobs/{job_id}"
            os.makedirs(job_dir, exist_ok=True)
            temp_image_path = os.path.join(job_dir, "input_from_url.jpg")
            download_file(image_url, temp_image_path)
            print(f"✅ Image downloaded for analysis")
        
        # Parse concepts
        concept_list = []
        if concepts:
            concept_list = [{"key": c.strip()} for c in concepts.split(',') if c.strip()]
            print(f"🎨 Concepts: {[c['key'] for c in concept_list]}")
        
        # Parse freeze_assets if provided
        frozen_items = []
        if freeze_assets:
            frozen_items = [item.strip() for item in freeze_assets.split(',')]
            print(f"❄️ Assets to freeze: {', '.join(frozen_items)}")
        
        # Parse asset parameter into list
        animate_items = []
        if asset:
            animate_items = [asset.strip()]
            print(f"🎯 Assets to animate: {asset}")
        
        # STEP 1: Analyze image with Llama 3.2 Vision if image provided
        scene_analysis = None
        if temp_image_path and os.path.exists(temp_image_path):
            print(f"\n🔍 STEP 1: Analyzing image with Llama 3.2 Vision...")
            
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=20,
                message="Analyzing image with Llama 3.2 Vision for precise prompt creation...",
                result={
                    "job_type": "luma_labs_animation",
                    "prompt": prompt,
                    "keyframe_url": keyframe_url
                }
            )
            
            if not mapapi:
                print(f"⚠️ Warning: Llama 3.2 Vision not available, using basic prompt enhancement")
            else:
                try:
                    scene_analysis = mapapi.analyze_image_with_llama(temp_image_path)
                    print(f"✅ Scene analysis complete:")
                    print(f"   {scene_analysis[:300]}...")
                except Exception as e:
                    print(f"⚠️ Vision analysis failed: {e}, using basic prompt enhancement")
        
        # STEP 2: Build intelligent prompt using Vision analysis + user parameters
        print(f"\n🎨 STEP 2: Crafting precise animation prompt...")
        enhanced_prompt = prompt
        
        if scene_analysis and (asset or frozen_items):
            # Use AI-powered intelligent prompt with scene understanding
            print(f"🤖 Using AI-powered prompt enhancement with scene analysis")
            
            if asset:
                # Asset-specific mode: Detailed prompt explaining what to animate and what NOT to change
                freeze_description = ", ".join(frozen_items) if frozen_items else "all other scene elements, background, lighting, colors, textures, and environment"
                
                enhanced_prompt = f"""Scene contains: {scene_analysis[:200]}.

CRITICAL INSTRUCTIONS - Read carefully to avoid expensive mistakes:

PRESERVE COMPLETELY (DO NOT CHANGE):
- {freeze_description}
- Exact visual appearance, colors, lighting, textures
- Camera angle and composition
- All elements not explicitly mentioned below

ANIMATE ONLY:
- {asset}: {prompt}

Only {asset} should show movement. Everything else must remain perfectly static and unchanged.
Keep the exact look and feel of the original image for all frozen elements."""
                
                print(f"   🎯 ASSET-SPECIFIC MODE with full scene context")
                print(f"   🎬 Animating: {asset}")
                print(f"   🔒 Freezing: {freeze_description}")
                
            elif frozen_items:
                # General mode: Use scene analysis to understand what NOT to change
                freeze_description = ", ".join(frozen_items)
                
                enhanced_prompt = f"""Scene contains: {scene_analysis[:200]}.

CRITICAL INSTRUCTIONS - Read carefully to avoid expensive mistakes:

KEEP COMPLETELY STATIC (DO NOT ANIMATE):
- {freeze_description}
- Preserve their exact appearance, position, colors, textures

ALLOW NATURAL ANIMATION:
- Other scene elements can move naturally
- Animation: {prompt}

Keep {freeze_description} perfectly frozen and unchanged in appearance.
Only animate elements that are not in the freeze list."""
                
                print(f"   🎨 GENERAL MODE with freeze constraints")
                print(f"   🔒 Frozen assets: {freeze_description}")
                
        elif asset or frozen_items:
            # Basic enhancement without vision analysis
            print(f"📝 Using basic prompt enhancement (no vision analysis)")
            
            if asset:
                freeze_description = ", ".join(frozen_items) if frozen_items else "background and all other elements"
                enhanced_prompt = f"{freeze_description} completely static and frozen in place. {asset} {prompt}. Animate only {asset}, keep {freeze_description} still."
                
            elif frozen_items:
                freeze_description = ", ".join(frozen_items)
                enhanced_prompt = f"{freeze_description} frozen in place, do not animate. {prompt}. Keep {freeze_description} static."
        else:
            print(f"📝 Using original user prompt (no constraints)")
        
        # STEP 2.5: Add position constraints if provided
        if position:
            try:
                x, y, width, height = map(int, position.split(','))
                print(f"\n📍 STEP 2.5: Adding position constraint")
                print(f"   Region: x={x}, y={y}, width={width}, height={height}")
                
                position_constraint = f"\n\nSPATIAL CONSTRAINT - CRITICAL:\nAnimate ONLY the region at coordinates: x={x}, y={y}, width={width}px, height={height}px.\nKeep all other areas completely static and unchanged."
                enhanced_prompt += position_constraint
                print(f"   ✅ Position constraint added to prompt")
                
            except Exception as e:
                print(f"   ⚠️ Invalid position format (expected 'x,y,width,height'): {e}")
        
        print(f"\n✅ Final prompt created:")
        print(f"   Original: {prompt}")
        if enhanced_prompt != prompt:
            print(f"   Enhanced: {enhanced_prompt[:200]}...")
            print(f"   Analysis used: {'Yes' if scene_analysis else 'No'}")
        
        # Update status
        update_job_status(
            job_id=job_id,
            status="processing",
            progress=40,
            message="Submitting to Luma Labs Dream Machine...",
            result={
                "job_type": "luma_labs_animation",
                "prompt": prompt,
                "enhanced_prompt": enhanced_prompt if enhanced_prompt != prompt else None,
                "scene_analysis": scene_analysis if scene_analysis else None,
                "vision_analysis_used": bool(scene_analysis),
                "keyframe_url": keyframe_url,
                "concepts": [c['key'] for c in concept_list] if concept_list else None,
                "asset": asset if asset else None,
                "freeze_assets": freeze_assets if freeze_assets else None,
                "frozen_items": frozen_items if frozen_items else None,
                "animate_items": animate_items if animate_items else None,
                "position": position if position else None
            }
        )
        
        # Build API payload
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {LUMA_3_KEY}",
            "content-type": "application/json"
        }
        
        payload = {
            "prompt": enhanced_prompt,  # Use enhanced prompt with freeze/animate structure
            "model": model,
            "resolution": resolution,
            "duration": duration
        }
        
        # Add keyframe if image provided
        if keyframe_url:
            payload["keyframes"] = {
                "frame0": {
                    "type": "image",
                    "url": keyframe_url
                }
            }
            print(f"🖼️ Using keyframe image: {keyframe_url}")
        
        # Add concepts if provided
        if concept_list:
            payload["concepts"] = concept_list
            print(f"🎨 Applying concepts: {[c['key'] for c in concept_list]}")
        
        # Add freeze and animate lists if provided
        freeze_list = frozen_items if frozen_items else (["background"] if asset else None)
        animate_list = animate_items if animate_items else None
        
        if freeze_list:
            payload["freeze"] = freeze_list
            print(f"🔒 Freeze objects: {', '.join(freeze_list)}")
        
        if animate_list:
            payload["animate"] = animate_list
            print(f"🎬 Animate objects: {', '.join(animate_list)}")
        
        print(f"📝 Final payload: {json.dumps(payload, indent=2)}")
        
        # Submit to Luma Labs API
        print(f"📤 Submitting to Luma Labs Dream Machine...")
        submit_url = "https://api.lumalabs.ai/dream-machine/v1/generations"
        
        response = requests.post(submit_url, headers=headers, json=payload, timeout=300)
        
        # Accept both 200 (OK) and 201 (Created) as success responses
        if response.status_code not in [200, 201]:
            error_msg = f"Luma Labs API error: {response.status_code}, {response.text}"
            print(f"❌ {error_msg}")
            raise HTTPException(status_code=response.status_code, detail=error_msg)
        
        resp_data = response.json()
        generation_id = resp_data.get("id")
        
        if not generation_id:
            raise HTTPException(status_code=500, detail=f"No generation ID in response: {resp_data}")
        
        print(f"✅ Generation submitted successfully. ID: {generation_id}")
        
        # Update status
        update_job_status(
            job_id=job_id,
            status="processing",
            progress=50,
            message="Waiting for Luma Labs to generate video...",
            result={
                "job_type": "luma_labs_animation",
                "prompt": prompt,
                "enhanced_prompt": enhanced_prompt if enhanced_prompt != prompt else None,
                "scene_analysis": scene_analysis if scene_analysis else None,
                "vision_analysis_used": bool(scene_analysis),
                "generation_id": generation_id,
                "keyframe_url": keyframe_url,
                "concepts": [c['key'] for c in concept_list] if concept_list else None,
                "asset": asset if asset else None,
                "freeze_assets": freeze_assets if freeze_assets else None,
                "frozen_items": frozen_items if frozen_items else None,
                "animate_items": animate_items if animate_items else None
            }
        )
        
        # Poll for results
        print(f"⏳ Polling for generation results...")
        result_url = f"https://api.lumalabs.ai/dream-machine/v1/generations/{generation_id}"
        max_retries = 120  # 10 minutes with 5-second intervals
        retry_count = 0
        
        while retry_count < max_retries:
            await asyncio.sleep(5)
            
            check_response = requests.get(result_url, headers=headers, timeout=60)
            
            if check_response.status_code != 200:
                print(f"⚠️ Polling error: {check_response.status_code}, {check_response.text}")
                retry_count += 1
                continue
            
            check_data = check_response.json()
            status = check_data.get("state")
            
            print(f"📊 Status: {status} (attempt {retry_count + 1}/{max_retries})")
            
            if status == "completed":
                video_url = check_data.get("assets", {}).get("video")
                
                if not video_url:
                    raise HTTPException(status_code=500, detail=f"Completed but no video URL: {check_data}")
                
                print(f"🎉 Video generation completed!")
                print(f"🎬 Video URL: {video_url}")
                
                # Download video to job directory
                job_dir = f"jobs/{job_id}"
                os.makedirs(job_dir, exist_ok=True)
                out_video_path = os.path.join(job_dir, f"luma_labs_video_{job_id}.mp4")
                
                print(f"⬇️ Downloading video...")
                download_file(video_url, out_video_path)
                print(f"✅ Video saved: {out_video_path}")
                
                # Upload to S3
                print(f"☁️ Uploading to S3...")
                s3_video_url = upload_asset_to_s3(out_video_path, "animations", job_id, "luma_labs_video.mp4")
                print(f"✅ S3 URL: {s3_video_url}")
                
                # Update job status
                update_job_status(
                    job_id=job_id,
                    status="completed",
                    progress=100,
                    message="Luma Labs animation completed successfully!",
                    result={
                        "job_type": "luma_labs_animation",
                        "prompt": prompt,
                        "enhanced_prompt": enhanced_prompt if enhanced_prompt != prompt else None,
                        "scene_analysis": scene_analysis if scene_analysis else None,
                        "vision_analysis_used": bool(scene_analysis),
                        "generation_id": generation_id,
                        "video_url": video_url,
                        "s3_video_url": s3_video_url,
                        "local_video_path": out_video_path,
                        "keyframe_url": keyframe_url,
                        "concepts": [c['key'] for c in concept_list] if concept_list else None,
                        "asset": asset if asset else None,
                        "freeze_assets": freeze_assets if freeze_assets else None,
                        "frozen_items": frozen_items if frozen_items else None,
                        "animate_items": animate_items if animate_items else None,
                        "resolution": resolution,
                        "duration": duration,
                        "model": model
                    }
                )
                
                return {
                    "success": True,
                    "job_id": job_id,
                    "generation_id": generation_id,
                    "video_url": video_url,
                    "s3_video_url": s3_video_url,
                    "local_video_path": out_video_path,
                    "message": "Luma Labs animation completed successfully!",
                    "prompt": prompt,
                    "enhanced_prompt": enhanced_prompt if enhanced_prompt != prompt else None,
                    "scene_analysis": scene_analysis[:300] if scene_analysis else None,
                    "vision_analysis_used": bool(scene_analysis),
                    "resolution": resolution,
                    "duration": duration,
                    "concepts": [c['key'] for c in concept_list] if concept_list else None,
                    "asset": asset if asset else None,
                    "freeze_assets": freeze_assets if freeze_assets else None,
                    "frozen_items": frozen_items if frozen_items else None,
                    "animate_items": animate_items if animate_items else None
                }
            
            elif status == "failed":
                failure_reason = check_data.get("failure_reason", "Unknown error")
                print(f"❌ Generation failed: {failure_reason}")
                
                update_job_status(
                    job_id=job_id,
                    status="failed",
                    progress=0,
                    message=f"Luma Labs generation failed: {failure_reason}",
                    result={
                        "job_type": "luma_labs_animation",
                        "prompt": prompt,
                        "generation_id": generation_id,
                        "error": failure_reason
                    }
                )
                
                raise HTTPException(status_code=500, detail=f"Generation failed: {failure_reason}")
            
            retry_count += 1
        
        # Timeout
        update_job_status(
            job_id=job_id,
            status="failed",
            progress=0,
            message="Luma Labs generation timed out",
            result={
                "job_type": "luma_labs_animation",
                "prompt": prompt,
                "generation_id": generation_id,
                "error": "Timeout after 10 minutes"
            }
        )
        
        raise HTTPException(status_code=408, detail="Generation timed out after 10 minutes")
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Luma Labs animate endpoint error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error during animation: {str(e)}"
        )

@app.post("/animate/easy")
async def easy_animate(
    file: Optional[UploadFile] = File(None, description="Image file to animate (optional if image_url provided)"),
    image_url: Optional[str] = Form(None, description="URL of image to animate (optional if file provided)"),
    prompt: str = Form(..., description="Animation prompt describing the desired video"),
    negative_prompt: Optional[str] = Form(
        "The video is not of a high quality, it has a low resolution, and the audio quality is not clear. Strange motion trajectory, a poor composition and deformed video, low resolution, duplicate and ugly, strange body structure, long and strange neck, bad teeth, bad eyes, bad limbs, bad hands, rotating camera, blurry camera, shaking camera. Deformation, low-resolution, blurry, ugly, distortion.",
        description="Negative prompt describing what to avoid"
    ),
    video_length: int = Form(72, description="Video length in frames (default: 72)"),
    seed: int = Form(43, description="Random seed for reproducibility (default: 43)"),
    steps: int = Form(25, description="Number of denoising steps (default: 25)"),
    cfg: float = Form(7.0, description="CFG scale for prompt adherence (default: 7.0)"),
    scheduler: str = Form("Euler", description="Scheduler type (default: Euler)"),
    frame_rate: int = Form(24, description="Output video frame rate (default: 24)")
):
    """🎬 Easy Animate: High-quality video animation using Segmind Easy Animate API
    
    This endpoint uses the Segmind Easy Animate API to create high-quality video animations
    from static images with detailed prompt control and negative prompts for better quality.
    
    **Features:**
    - High-quality video generation from images
    - Detailed prompt control for specific animations
    - Negative prompts to avoid unwanted effects
    - Configurable video length, frame rate, and quality settings
    - Support for uploaded files or image URLs
    
    **Parameters:**
    - `file` or `image_url`: Source image to animate
    - `prompt`: Detailed description of the desired animation
    - `negative_prompt`: Description of what to avoid in the animation
    - `video_length`: Number of frames (default: 72)
    - `seed`: Random seed for reproducibility
    - `steps`: Number of denoising steps (higher = better quality but slower)
    - `cfg`: CFG scale (higher = stronger prompt adherence)
    - `scheduler`: Denoising scheduler type
    - `frame_rate`: Output video FPS
    
    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/animate/easy" \\
      -F "file=@woman.jpg" \\
      -F "prompt=photo of a beautiful woman in a night party. The video is of high quality, and the view is very clear. High quality, masterpiece, best quality, highres, ultra-detailed, fantastic" \\
      -F "video_length=72" \\
      -F "frame_rate=24"
    ```
    
    Returns:
        Job ID and video S3 URL when complete
    """
    
    try:
        # Check for Segmind API key
        SEGMIND_API_KEY = os.getenv("SEGMIND_API_KEY")
        if not SEGMIND_API_KEY:
            raise HTTPException(
                status_code=503,
                detail="SEGMIND_API_KEY not configured. Please set the environment variable."
            )
        
        # Validate input - need either file or image_url
        if not file and not image_url:
            raise HTTPException(
                status_code=400,
                detail="Either 'file' or 'image_url' must be provided"
            )
        
        # Generate job ID
        job_id = str(uuid.uuid4())
        
        print(f"\n{'='*80}")
        print(f"🎬 EASY ANIMATE - Job ID: {job_id}")
        print(f"{'='*80}")
        print(f"📝 Prompt: {prompt[:100]}...")
        print(f"🎞️ Video length: {video_length} frames @ {frame_rate}fps")
        print(f"⚙️ Steps: {steps}, CFG: {cfg}, Scheduler: {scheduler}")
        
        # Step 1: Get the image and convert to base64
        temp_image_path = None
        image_base64 = None
        s3_input_url = None
        
        try:
            if file:
                print(f"📤 Processing uploaded file: {file.filename}")
                
                # Validate file type
                if not file.content_type.startswith('image/'):
                    raise HTTPException(status_code=400, detail="File must be an image")
                
                # Read file content
                file_content = await file.read()
                
                # Convert to base64
                image_base64 = base64.b64encode(file_content).decode('utf-8')
                
                # Also save to temp file for S3 upload
                file_ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
                temp_fd, temp_image_path = tempfile.mkstemp(suffix=file_ext)
                
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    temp_file.write(file_content)
                
                # Upload to S3
                print(f"☁️ Uploading input image to S3...")
                s3_input_url = upload_asset_to_s3(temp_image_path, "easy_animate", job_id, f"input_{file.filename}")
                print(f"✅ Input S3 URL: {s3_input_url}")
                
            else:
                print(f"🔗 Using provided image URL: {image_url[:50]}...")
                
                # Download image
                response = requests.get(image_url, timeout=30)
                response.raise_for_status()
                
                # Convert to base64
                image_base64 = base64.b64encode(response.content).decode('utf-8')
                
                # Save to temp file for S3 upload
                content_type = response.headers.get('content-type', '')
                file_ext = '.jpg'
                if 'png' in content_type:
                    file_ext = '.png'
                elif 'webp' in content_type:
                    file_ext = '.webp'
                
                temp_fd, temp_image_path = tempfile.mkstemp(suffix=file_ext)
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    temp_file.write(response.content)
                
                # Upload to S3
                print(f"☁️ Uploading input image to S3...")
                s3_input_url = upload_asset_to_s3(temp_image_path, "easy_animate", job_id, f"input_from_url{file_ext}")
                print(f"✅ Input S3 URL: {s3_input_url}")
            
            # Initialize job status
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=20,
                message="Submitting to Easy Animate API...",
                result={
                    "job_type": "easy_animate",
                    "input_image_url": s3_input_url,
                    "prompt": prompt,
                    "video_length": video_length,
                    "frame_rate": frame_rate,
                    "steps": steps,
                    "cfg": cfg
                }
            )
            
            # Step 2: Call Segmind Easy Animate API
            print(f"🎨 Calling Easy Animate API...")
            
            api_url = "https://api.segmind.com/v1/easy-animate"
            
            data = {
                "input_image": image_base64,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "video_length": video_length,
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "scheduler": scheduler,
                "frame_rate": frame_rate
            }
            
            headers = {
                'x-api-key': SEGMIND_API_KEY,
                'Content-Type': 'application/json'
            }
            
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=40,
                message="Generating video with Easy Animate...",
                result={
                    "job_type": "easy_animate",
                    "input_image_url": s3_input_url,
                    "prompt": prompt,
                    "api_status": "generating"
                }
            )
            
            # Make API request
            start_time = time.time()
            response = requests.post(api_url, json=data, headers=headers, timeout=600)
            
            if response.status_code != 200:
                error_msg = f"Easy Animate API error: {response.status_code}"
                try:
                    error_data = response.json()
                    error_msg = f"Easy Animate API error: {error_data.get('error', response.text)}"
                except:
                    error_msg = f"Easy Animate API error: {response.text}"
                
                print(f"❌ {error_msg}")
                
                update_job_status(
                    job_id=job_id,
                    status="failed",
                    progress=0,
                    message=error_msg,
                    result={
                        "job_type": "easy_animate",
                        "error": error_msg,
                        "input_image_url": s3_input_url
                    }
                )
                
                raise HTTPException(status_code=response.status_code, detail=error_msg)
            
            processing_time = time.time() - start_time
            print(f"✅ Video generated in {processing_time:.2f}s")
            
            # Step 3: Save video to S3
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=80,
                message="Saving video to S3...",
                result={
                    "job_type": "easy_animate",
                    "input_image_url": s3_input_url,
                    "processing_time": processing_time
                }
            )
            
            # Save video to temporary file
            video_content = response.content
            temp_video_fd, temp_video_path = tempfile.mkstemp(suffix=".mp4")
            
            with os.fdopen(temp_video_fd, 'wb') as temp_video:
                temp_video.write(video_content)
            
            print(f"💾 Video size: {len(video_content) / 1024 / 1024:.2f} MB")
            
            # Upload to S3
            print(f"☁️ Uploading video to S3...")
            s3_video_url = upload_asset_to_s3(temp_video_path, "easy_animate", job_id, f"output_video.mp4")
            print(f"✅ Video S3 URL: {s3_video_url}")
            
            # Clean up temp video file
            try:
                os.unlink(temp_video_path)
            except:
                pass
            
            # Update job status to completed
            update_job_status(
                job_id=job_id,
                status="completed",
                progress=100,
                message="Easy Animate video generation completed!",
                result={
                    "job_type": "easy_animate",
                    "input_image_url": s3_input_url,
                    "video_s3_url": s3_video_url,
                    "processing_time": processing_time,
                    "prompt": prompt,
                    "video_config": {
                        "video_length": video_length,
                        "frame_rate": frame_rate,
                        "steps": steps,
                        "cfg": cfg,
                        "scheduler": scheduler,
                        "seed": seed
                    }
                }
            )
            
            print(f"✅ Easy Animate completed!")
            
            return {
                "success": True,
                "job_id": job_id,
                "input_image_url": s3_input_url,
                "video_url": s3_video_url,
                "processing_time": processing_time,
                "video_info": {
                    "length_frames": video_length,
                    "frame_rate": frame_rate,
                    "duration_seconds": video_length / frame_rate,
                    "size_mb": len(video_content) / 1024 / 1024
                },
                "message": f"Video generated successfully in {processing_time:.2f} seconds"
            }
            
        finally:
            # Clean up temporary files
            if temp_image_path and os.path.exists(temp_image_path):
                try:
                    os.unlink(temp_image_path)
                    print(f"🧹 Cleaned up temporary files")
                except Exception as cleanup_error:
                    print(f"⚠️ Failed to cleanup temp files: {cleanup_error}")
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Easy Animate error: {str(e)}"
        print(f"❌ {error_msg}")
        
        # Update job status if job_id exists
        if 'job_id' in locals():
            update_job_status(
                job_id=job_id,
                status="failed",
                progress=0,
                message=error_msg,
                result={
                    "job_type": "easy_animate",
                    "error": str(e)
                }
            )
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

@app.post("/animate/smart")
async def smart_animate_with_vision(
    file: Optional[UploadFile] = File(None, description="Image file to animate (optional if image_url provided)"),
    animation_prompt: str = Form(..., description="What you want to animate (e.g., 'make the character jump')"),
    image_url: Optional[str] = Form(None, description="URL of image to animate (optional if file provided)"),
    duration: str = Form("5", description="Video duration in seconds (default: 5)"),
    size: str = Form("1280*720", description="Video resolution in 'width*height' format"),
    preserve_background: bool = Form(True, description="Preserve background and non-animated elements"),
    asset: Optional[str] = Form(None, description="Specific asset/object to animate (e.g., 'the red car', 'the woman in blue'). Only this asset will be animated, everything else stays frozen."),
    freeze_assets: Optional[str] = Form(None, description="Comma-separated list of assets that must remain frozen (e.g., 'the character, the building, trees'). These will be explicitly kept static during animation."),
    position: Optional[str] = Form(None, description="Bounding box for animation area: 'x,y,width,height' in pixels (e.g., '100,200,300,400'). Only the specified region will be animated.")
):
    """🤖 Smart Animate: AI-powered selective animation using Llama 3.2 Vision"""
    
    try:
        # Validate input - need either file or image_url
        if not file and not image_url:
            raise HTTPException(
                status_code=400, 
                detail="Either 'file' or 'image_url' must be provided"
            )
        
        # Generate job ID
        job_id = str(uuid.uuid4())
        
        print(f"🤖 Smart Animation request received for job {job_id}")
        print(f"📝 User animation prompt: {animation_prompt}")
        if asset:
            print(f"🎯 Target asset: {asset}")
        if freeze_assets:
            freeze_list = [item.strip() for item in freeze_assets.split(',')]
            print(f"❄️ Frozen assets: {', '.join(freeze_list)}")
        print(f"🎬 Size: {size}, Duration: {duration}s")
        print(f"🛡️ Preserve background: {preserve_background}")
        
        # Implementation continues...
        # TODO: Add full implementation here
        
        return {
            "success": True,
            "job_id": job_id,
            "message": "Smart animation endpoint - implementation in progress"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Smart animation error: {str(e)}"
        print(f"❌ {error_msg}")
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

@app.post("/enhance/prompt")
async def enhance_prompt(
    prompt: str = Form(..., description="Original prompt to enhance"),
    preserve_original: bool = Form(False, description="Include original prompt in response")
):
    """✨ Prompt Enhancer: Improve prompts using Segmind Bria Prompt Enhancer API
    
    This endpoint uses the Segmind Bria Prompt Enhancer API to transform simple prompts
    into detailed, high-quality prompts suitable for AI image/video generation.
    
    **Features:**
    - AI-powered prompt enhancement and expansion
    - Adds artistic details, quality modifiers, and technical terms
    - Optimizes prompts for better generation results
    - Fast and simple to use
    
    **Use Cases:**
    - Enhance prompts before using /animate/prompt
    - Improve quality of /animate/easy prompts
    - Generate detailed prompts for image generation
    - Add professional artistic language to simple descriptions
    
    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/enhance/prompt" \\
      -F "prompt=a beautiful woman in a night party"
    ```
    
    Returns:
        Enhanced prompt text
    """
    
    try:
        # Check for Segmind API key
        SEGMIND_API_KEY = os.getenv("SEGMIND_API_KEY")
        if not SEGMIND_API_KEY:
            raise HTTPException(
                status_code=503,
                detail="SEGMIND_API_KEY not configured. Please set the environment variable."
            )
        
        # Validate input
        if not prompt or len(prompt.strip()) == 0:
            raise HTTPException(
                status_code=400,
                detail="Prompt cannot be empty"
            )
        
        print(f"\n{'='*80}")
        print(f"✨ PROMPT ENHANCEMENT")
        print(f"{'='*80}")
        print(f"📝 Original prompt: {prompt}")
        
        # Call Segmind Bria Prompt Enhancer API
        api_url = "https://api.segmind.com/v1/bria-prompt-enhancer"
        
        data = {
            "prompt": prompt
        }
        
        headers = {
            'x-api-key': SEGMIND_API_KEY,
            'Content-Type': 'application/json'
        }
        
        print(f"🎨 Calling Bria Prompt Enhancer API...")
        
        start_time = time.time()
        response = requests.post(api_url, json=data, headers=headers, timeout=30)
        
        if response.status_code != 200:
            error_msg = f"Prompt Enhancer API error: {response.status_code}"
            try:
                error_data = response.json()
                error_msg = f"Prompt Enhancer API error: {error_data.get('error', response.text)}"
            except:
                error_msg = f"Prompt Enhancer API error: {response.text}"
            
            print(f"❌ {error_msg}")
            raise HTTPException(status_code=response.status_code, detail=error_msg)
        
        processing_time = time.time() - start_time
        
        # Parse response
        try:
            result = response.json()
            enhanced_prompt = result.get("enhanced_prompt", result.get("result", response.text))
        except:
            # If response is plain text
            enhanced_prompt = response.text
        
        print(f"✅ Prompt enhanced in {processing_time:.2f}s")
        print(f"✨ Enhanced prompt: {enhanced_prompt}")
        
        response_data = {
            "success": True,
            "enhanced_prompt": enhanced_prompt,
            "processing_time": processing_time,
            "character_count": {
                "original": len(prompt),
                "enhanced": len(enhanced_prompt),
                "increase": len(enhanced_prompt) - len(prompt)
            }
        }
        
        if preserve_original:
            response_data["original_prompt"] = prompt
        
        return response_data
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Prompt enhancement error: {str(e)}"
        print(f"❌ {error_msg}")
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

@app.post("/enhance/prompt/ollama")
async def enhance_prompt_ollama(
    prompt: str = Form(..., description="Original prompt to enhance"),
    preserve_original: bool = Form(False, description="Include original prompt in response"),
    model: str = Form("mistral", description="Ollama model to use (default: mistral)"),
    temperature: float = Form(0.7, description="Temperature for generation (0.0-1.0, default: 0.7)")
):
    """✨ Prompt Enhancer (Ollama): Improve prompts using local Ollama with Mistral model
    
    This endpoint uses a local Ollama instance with Mistral (or other models) to transform 
    simple prompts into detailed, high-quality prompts suitable for AI image/video generation.
    
    **Features:**
    - 100% local processing with Ollama
    - No API keys required
    - Privacy-focused (data never leaves your machine)
    - Customizable model selection
    - AI-powered prompt enhancement and expansion
    - Adds artistic details, quality modifiers, and technical terms
    
    **Requirements:**
    - Ollama must be running locally on port 11434
    - Install: https://ollama.ai/
    - Pull model: `ollama pull mistral`
    
    **Supported Models:**
    - mistral (default, recommended)
    - llama3.1:8b
    - llama3.2
    - gemma2
    - Any other Ollama model
    
    **Use Cases:**
    - Enhance prompts before using /animate/prompt
    - Improve quality of /animate/easy prompts
    - Generate detailed prompts for image generation
    - Add professional artistic language to simple descriptions
    - Privacy-sensitive prompt enhancement
    
    **Example Transformation:**
    - Input: "a futuristic cityscape under a starry night"
    - Enhanced: "A breathtaking futuristic cityscape illuminated by neon lights and holographic displays, set beneath a mesmerizing starry night sky with countless twinkling stars and distant galaxies, ultra-detailed, cinematic composition, 8K resolution, masterpiece quality"
    
    **Parameters:**
    - `prompt`: Your original prompt to enhance
    - `preserve_original`: If true, returns both original and enhanced prompts
    - `model`: Ollama model to use (default: mistral)
    - `temperature`: Creativity level (0.0=focused, 1.0=creative)
    
    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/enhance/prompt/ollama" \\
      -F "prompt=a beautiful woman in a night party" \\
      -F "model=mistral" \\
      -F "temperature=0.7"
    ```
    
    Returns:
        Enhanced prompt text from local Ollama
    """
    
    try:
        # Validate input
        if not prompt or len(prompt.strip()) == 0:
            raise HTTPException(
                status_code=400,
                detail="Prompt cannot be empty"
            )
        
        # Validate temperature
        if not (0.0 <= temperature <= 1.0):
            raise HTTPException(
                status_code=400,
                detail="Temperature must be between 0.0 and 1.0"
            )
        
        print(f"\n{'='*80}")
        print(f"✨ OLLAMA PROMPT ENHANCEMENT")
        print(f"{'='*80}")
        print(f"📝 Original prompt: {prompt}")
        print(f"🤖 Model: {model}")
        print(f"🌡️ Temperature: {temperature}")
        
        # Build system prompt for prompt enhancement
        system_prompt = """You are an expert prompt engineer specializing in AI image and video generation.

Your task is to enhance user prompts to make them more detailed, vivid, and effective for AI art generation.

Guidelines:
- Add artistic and technical details (lighting, composition, style, mood)
- Include quality modifiers (masterpiece, ultra-detailed, high resolution, cinematic)
- Expand on visual elements (colors, textures, atmosphere, depth)
- Maintain the core concept and intent of the original prompt
- Make the prompt more specific and descriptive
- Use professional photography and art terminology
- Keep it concise but impactful (aim for 2-4 sentences)

Return ONLY a JSON object with this exact schema:
{"enhanced_prompt": "your enhanced prompt here"}

Do not include any other text or explanations outside the JSON."""

        user_message = f"""Original prompt: "{prompt}"

Enhance this prompt for AI image/video generation. Make it more detailed, artistic, and effective while preserving the original concept.

Return JSON: {{"enhanced_prompt": "..."}}"""
        
        # Call local Ollama API
        ollama_url = "http://localhost:11434/api/chat"
        
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "format": "json",
            "stream": False,
            "options": {
                "temperature": temperature
            }
        }
        
        print(f"🔄 Calling Ollama API at {ollama_url}...")
        
        start_time = time.time()
        
        try:
            response = requests.post(ollama_url, json=payload, timeout=60)
            response.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise HTTPException(
                status_code=503,
                detail="Could not connect to Ollama. Please ensure Ollama is running (http://localhost:11434). Install from https://ollama.ai/"
            )
        except requests.exceptions.Timeout:
            raise HTTPException(
                status_code=504,
                detail=f"Ollama request timed out. Model '{model}' may be too slow or not available."
            )
        
        processing_time = time.time() - start_time
        
        # Parse Ollama response
        try:
            ollama_response = response.json()
            message_content = ollama_response.get("message", {}).get("content", "")
            
            # Parse JSON from message content
            result = json.loads(message_content)
            enhanced_prompt = result.get("enhanced_prompt", "")
            
            if not enhanced_prompt:
                raise ValueError("No enhanced_prompt in response")
            
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            # Fallback: use raw content if JSON parsing fails
            print(f"⚠️ JSON parsing failed: {e}")
            print(f"Raw response: {message_content[:200]}...")
            
            # Try to extract enhanced prompt from text
            if "enhanced_prompt" in message_content:
                try:
                    # Try to extract JSON from text
                    start = message_content.find("{")
                    end = message_content.rfind("}") + 1
                    if start >= 0 and end > start:
                        json_str = message_content[start:end]
                        result = json.loads(json_str)
                        enhanced_prompt = result.get("enhanced_prompt", message_content)
                    else:
                        enhanced_prompt = message_content
                except:
                    enhanced_prompt = message_content
            else:
                enhanced_prompt = message_content
            
            if not enhanced_prompt or len(enhanced_prompt.strip()) == 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Ollama returned empty response. Model '{model}' may not be available. Try: ollama pull {model}"
                )
        
        print(f"✅ Prompt enhanced in {processing_time:.2f}s")
        print(f"✨ Enhanced prompt: {enhanced_prompt}")
        
        response_data = {
            "success": True,
            "enhanced_prompt": enhanced_prompt,
            "processing_time": processing_time,
            "model": model,
            "temperature": temperature,
            "character_count": {
                "original": len(prompt),
                "enhanced": len(enhanced_prompt),
                "increase": len(enhanced_prompt) - len(prompt)
            }
        }
        
        if preserve_original:
            response_data["original_prompt"] = prompt
        
        return response_data
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Ollama prompt enhancement error: {str(e)}"
        print(f"❌ {error_msg}")
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

@app.post("/analyze/image/ollama")
async def analyze_image_ollama(
    image: UploadFile = File(..., description="Image file to analyze"),
    model: str = Form("llama3.2-vision", description="Ollama vision model to use (default: llama3.2-vision)"),
    custom_prompt: Optional[str] = Form(
        "Describe this image in detail, focusing on what would make an interesting animation. Describe the movement, lighting, and cinematic effects that would bring this scene to life.",
        description="Custom prompt for image analysis"
    )
):
    """👁️ Image Analysis (Ollama): Analyze images using local Ollama Llama 3.2 Vision
    
    This endpoint uses a local Ollama instance with Llama 3.2 Vision to analyze images
    and provide detailed descriptions, perfect for understanding scenes before animation.
    
    **Features:**
    - 100% local processing with Ollama
    - No API keys required
    - Privacy-focused (data never leaves your machine)
    - Vision-language model for detailed scene understanding
    - Customizable analysis prompts
    - Focus on animation potential and cinematic effects
    
    **Requirements:**
    - Ollama must be running locally on port 11434
    - Install: https://ollama.ai/
    - Pull model: `ollama pull llama3.2-vision`
    
    **Supported Models:**
    - llama3.2-vision (default, recommended)
    - llava
    - bakllava
    - Any other Ollama vision model
    
    **Use Cases:**
    - Analyze scenes before animation
    - Understand character details and poses
    - Identify animation opportunities
    - Get cinematic descriptions for better prompts
    - Privacy-sensitive image analysis
    
    **Example Analysis Output:**
    "The image shows a woman in an elegant red dress standing in front of a city skyline 
    at night. The lighting creates dramatic shadows on her face. For animation, her hair 
    could gently blow in the wind, the city lights could twinkle in the background, and 
    she could turn her head slightly with a subtle smile. A cinematic dolly zoom could 
    emphasize her presence while the background blurs slightly."
    
    **Parameters:**
    - `image`: Image file to analyze (JPG, PNG, WebP)
    - `model`: Ollama vision model to use
    - `custom_prompt`: Analysis instructions (default focuses on animation potential)
    
    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/analyze/image/ollama" \\
      -F "image=@photo.jpg" \\
      -F "model=llama3.2-vision" \\
      -F "custom_prompt=Describe this image in detail, focusing on characters and objects"
    ```
    
    Returns:
        Detailed image analysis from Ollama Vision
    """
    
    try:
        # Validate file type
        if not image.content_type.startswith('image/'):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid file type. Expected image, got {image.content_type}"
            )
        
        print(f"\n{'='*80}")
        print(f"👁️ OLLAMA IMAGE ANALYSIS")
        print(f"{'='*80}")
        print(f"🖼️ Image: {image.filename}")
        print(f"🤖 Model: {model}")
        print(f"📝 Custom prompt: {custom_prompt[:100]}...")
        
        # Read and encode image to base64
        image_content = await image.read()
        image_base64 = base64.b64encode(image_content).decode('utf-8')
        
        print(f"✅ Image encoded to base64 ({len(image_base64)} chars)")
        
        # Call Ollama Vision API
        ollama_url = "http://localhost:11434/api/chat"
        
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": custom_prompt,
                    "images": [image_base64]
                }
            ],
            "stream": False
        }
        
        print(f"🔄 Calling Ollama Vision API at {ollama_url}...")
        
        start_time = time.time()
        
        try:
            response = requests.post(ollama_url, json=payload, timeout=120)
            response.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise HTTPException(
                status_code=503,
                detail=f"Could not connect to Ollama. Please ensure Ollama is running (http://localhost:11434). Install from https://ollama.ai/ and run: ollama pull {model}"
            )
        except requests.exceptions.Timeout:
            raise HTTPException(
                status_code=504,
                detail=f"Ollama request timed out. Model '{model}' may be slow or not available."
            )
        
        processing_time = time.time() - start_time
        
        # Parse Ollama response
        try:
            ollama_response = response.json()
            message_content = ollama_response.get("message", {}).get("content", "")
            
            if not message_content:
                raise ValueError("No content in Ollama response")
            
            analysis = message_content.strip()
            
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            print(f"⚠️ Response parsing failed: {e}")
            print(f"Raw response: {response.text[:200]}...")
            
            raise HTTPException(
                status_code=500,
                detail=f"Failed to parse Ollama response. Model '{model}' may not be available. Try: ollama pull {model}"
            )
        
        if not analysis or len(analysis.strip()) == 0:
            raise HTTPException(
                status_code=500,
                detail=f"Ollama returned empty analysis. Model '{model}' may not be available. Try: ollama pull {model}"
            )
        
        print(f"✅ Image analyzed in {processing_time:.2f}s")
        print(f"📋 Analysis: {analysis[:200]}...")
        
        response_data = {
            "success": True,
            "analysis": analysis,
            "processing_time": processing_time,
            "model": model,
            "image_filename": image.filename,
            "character_count": len(analysis),
            "custom_prompt_used": custom_prompt
        }
        
        return response_data
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Ollama image analysis error: {str(e)}"
        print(f"❌ {error_msg}")
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

# Helper functions for smart animation
    """✨ Prompt Enhancer: Improve prompts using Segmind Bria Prompt Enhancer API
    
    This endpoint uses the Segmind Bria Prompt Enhancer API to transform simple prompts
    into detailed, high-quality prompts suitable for AI image/video generation.
    
    **Features:**
    - AI-powered prompt enhancement and expansion
    - Adds artistic details, quality modifiers, and technical terms
    - Optimizes prompts for better generation results
    - Fast and simple to use
    
    **Use Cases:**
    - Enhance prompts before using /animate/prompt
    - Improve quality of /animate/easy prompts
    - Generate detailed prompts for image generation
    - Add professional artistic language to simple descriptions
    
    **Example Transformation:**
    - Input: "a futuristic cityscape under a starry night"
    - Enhanced: "A breathtaking futuristic cityscape illuminated by neon lights and holographic displays, set beneath a mesmerizing starry night sky with countless twinkling stars and distant galaxies, ultra-detailed, cinematic composition, 8K resolution, masterpiece quality"
    
    **Parameters:**
    - `prompt`: Your original prompt to enhance
    - `preserve_original`: If true, returns both original and enhanced prompts
    
    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/enhance/prompt" \\
      -F "prompt=a beautiful woman in a night party"
    ```
    
    Returns:
        Enhanced prompt text
    """
    
    try:
        # Check for Segmind API key
        SEGMIND_API_KEY = os.getenv("SEGMIND_API_KEY")
        if not SEGMIND_API_KEY:
            raise HTTPException(
                status_code=503,
                detail="SEGMIND_API_KEY not configured. Please set the environment variable."
            )
        
        # Validate input
        if not prompt or len(prompt.strip()) == 0:
            raise HTTPException(
                status_code=400,
                detail="Prompt cannot be empty"
            )
        
        print(f"\n{'='*80}")
        print(f"✨ PROMPT ENHANCEMENT")
        print(f"{'='*80}")
        print(f"📝 Original prompt: {prompt}")
        
        # Call Segmind Bria Prompt Enhancer API
        api_url = "https://api.segmind.com/v1/bria-prompt-enhancer"
        
        data = {
            "prompt": prompt
        }
        
        headers = {
            'x-api-key': SEGMIND_API_KEY,
            'Content-Type': 'application/json'
        }
        
        print(f"🎨 Calling Bria Prompt Enhancer API...")
        
        start_time = time.time()
        response = requests.post(api_url, json=data, headers=headers, timeout=30)
        
        if response.status_code != 200:
            error_msg = f"Prompt Enhancer API error: {response.status_code}"
            try:
                error_data = response.json()
                error_msg = f"Prompt Enhancer API error: {error_data.get('error', response.text)}"
            except:
                error_msg = f"Prompt Enhancer API error: {response.text}"
            
            print(f"❌ {error_msg}")
            raise HTTPException(status_code=response.status_code, detail=error_msg)
        
        processing_time = time.time() - start_time
        
        # Parse response
        try:
            result = response.json()
            enhanced_prompt = result.get("enhanced_prompt", result.get("result", response.text))
        except:
            # If response is plain text
            enhanced_prompt = response.text
        
        print(f"✅ Prompt enhanced in {processing_time:.2f}s")
        print(f"✨ Enhanced prompt: {enhanced_prompt}")
        
        response_data = {
            "success": True,
            "enhanced_prompt": enhanced_prompt,
            "processing_time": processing_time,
            "character_count": {
                "original": len(prompt),
                "enhanced": len(enhanced_prompt),
                "increase": len(enhanced_prompt) - len(prompt)
            }
        }
        
        if preserve_original:
            response_data["original_prompt"] = prompt
        
        return response_data
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Prompt enhancement error: {str(e)}"
        print(f"❌ {error_msg}")
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

# Helper functions for smart animation
        
        # Generate job ID
        job_id = str(uuid.uuid4())
        
        print(f"🤖 Smart Animation request received for job {job_id}")
        print(f"📝 User animation prompt: {animation_prompt}")
        if asset:
            print(f"🎯 Target asset: {asset}")
        if freeze_assets:
            freeze_list = [item.strip() for item in freeze_assets.split(',')]
            print(f"❄️ Frozen assets: {', '.join(freeze_list)}")
        print(f"🎬 Size: {size}, Duration: {duration}s")
        print(f"🛡️ Preserve background: {preserve_background}")
        
        # Step 1: Get the image (either from upload or URL)
        temp_image_path = None
        image_source_url = None
        
        try:
            if file:
                # Save uploaded file to temporary location
                print(f"📤 Processing uploaded file: {file.filename}")
                
                # Read file content
                file_content = await file.read()
                
                # Create temporary file
                file_ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
                temp_fd, temp_image_path = tempfile.mkstemp(suffix=file_ext)
                
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    temp_file.write(file_content)
                
                # Upload to S3 for animation service
                print(f"📤 Uploading image to S3...")
                s3_result = upload_asset_to_s3(
                    temp_image_path, 
                    "smart_animate_input", 
                    job_id, 
                    f"input_{file.filename}"
                )
                
                if s3_result:
                    image_source_url = s3_result
                    print(f"✅ Image uploaded to S3: {image_source_url[:50]}...")
                else:
                    raise Exception("Failed to upload image to S3")
                    
            else:
                # Use provided URL
                print(f"🔗 Using provided image URL: {image_url[:50]}...")
                image_source_url = image_url
                
                # Download image to temporary file for Llama analysis
                response = requests.get(image_url, timeout=30)
                response.raise_for_status()
                
                # Determine file extension
                content_type = response.headers.get('content-type', '')
                file_ext = '.jpg'
                if 'png' in content_type:
                    file_ext = '.png'
                elif 'webp' in content_type:
                    file_ext = '.webp'
                
                temp_fd, temp_image_path = tempfile.mkstemp(suffix=file_ext)
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    temp_file.write(response.content)
                
                print(f"✅ Image downloaded to temporary file")
            
            # Initialize job status
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=10,
                message="Analyzing image with Llama 3.2 Vision...",
                result={
                    "job_type": "smart_animation",
                    "user_animation_prompt": animation_prompt,
                    "target_asset": asset if asset else None,
                    "freeze_assets": freeze_assets if freeze_assets else None,
                    "position": position if position else None,
                    "size": size,
                    "duration": duration,
                    "preserve_background": preserve_background,
                    "input_image_url": image_source_url
                }
            )
            
            # Step 2: Analyze image with Llama 3.2 Vision to identify all objects
            print(f"🔍 Analyzing image with Llama 3.2 Vision to identify all elements...")
            
            if not mapapi:
                raise HTTPException(
                    status_code=503,
                    detail="Llama 3.2 Vision analysis service not available. Please ensure mapapi module is loaded."
                )
            
            # Get detailed analysis of all objects in the scene
            scene_analysis = mapapi.analyze_image_with_llama(temp_image_path)
            
            print(f"📋 Scene analysis complete:")
            print(f"   {scene_analysis[:200]}...")
            
            # Update job status
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=30,
                message="Crafting intelligent animation prompt...",
                result={
                    "job_type": "smart_animation",
                    "user_animation_prompt": animation_prompt,
                    "target_asset": asset if asset else None,
                    "freeze_assets": freeze_assets if freeze_assets else None,
                    "position": position if position else None,
                    "scene_analysis": scene_analysis,
                    "input_image_url": image_source_url
                }
            )
            
            # Step 3: Craft intelligent animation prompt
            print(f"🎨 Crafting intelligent animation prompt...")
            
            # Parse freeze_assets if provided
            frozen_items = []
            if freeze_assets:
                frozen_items = [item.strip() for item in freeze_assets.split(',')]
                print(f"   ❄️ Assets to freeze: {', '.join(frozen_items)}")
            
            # Parse asset parameter into list
            animate_items = []
            if asset:
                animate_items = [asset.strip()]
                print(f"   🎯 Assets to animate: {asset}")
            
            # Build specialized freeze/animate prompt style
            # Format: "Frozen objects are completely static. Animated objects move. Only animate specified items."
            if asset:
                # Asset-specific mode: Use freeze/animate structure
                # Clear marking: what's frozen vs what animates
                freeze_description = ", ".join(frozen_items) if frozen_items else "background and all other elements"
                
                intelligent_prompt = f"{freeze_description} completely static and frozen in place. {asset} {animation_prompt}. Animate only {asset}, keep {freeze_description} still."
                
                print(f"   🎯 ASSET-SPECIFIC MODE: Only animating '{asset}'")
                if frozen_items:
                    print(f"   ❄️ Explicitly frozen: {', '.join(frozen_items)}")
                print(f"   📝 Freeze/Animate prompt: {intelligent_prompt}")
            else:
                # General mode: Still use freeze/animate structure if freeze_assets provided
                if frozen_items:
                    freeze_description = ", ".join(frozen_items)
                    intelligent_prompt = f"{freeze_description} frozen in place, do not animate. {animation_prompt}. Keep {freeze_description} static."
                    print(f"   🎨 GENERAL MODE with freeze constraints")
                    print(f"   ❄️ Explicitly frozen: {', '.join(frozen_items)}")
                else:
                    # No specific constraints, just use the animation prompt
                    intelligent_prompt = f"{animation_prompt}, while keeping other elements stable."
                    print(f"   🎨 GENERAL MODE: Natural animation")
                
                print(f"   📝 Prompt: {intelligent_prompt}")
            
                if frozen_items:
                    print(f"   ❄️ Freezing specific assets: {', '.join(frozen_items)}")
                print(f"   📝 Focused prompt: {intelligent_prompt}")
            
            # Add position constraints if provided
            if position:
                try:
                    x, y, width, height = map(int, position.split(','))
                    print(f"\n📍 Adding position constraint:")
                    print(f"   Region: x={x}, y={y}, width={width}, height={height}")
                    
                    position_constraint = f" Animate ONLY the region at coordinates x={x}, y={y}, width={width}px, height={height}px. Keep all other areas completely static."
                    intelligent_prompt += position_constraint
                    print(f"   ✅ Position constraint added to prompt")
                    
                except Exception as e:
                    print(f"   ⚠️ Invalid position format (expected 'x,y,width,height'): {e}")
            
            # Update job status
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=50,
                message="Sending to animation service (Luma Ray 2 I2V)...",
                result={
                    "job_type": "smart_animation",
                    "user_animation_prompt": animation_prompt,
                    "target_asset": asset if asset else None,
                    "freeze_assets": freeze_assets if freeze_assets else None,
                    "position": position if position else None,
                    "frozen_items_list": frozen_items if frozen_items else None,
                    "scene_analysis": scene_analysis,
                    "intelligent_prompt": intelligent_prompt,
                    "input_image_url": image_source_url
                }
            )
            
            # Step 4: Send to Luma Ray 2 I2V animation service
            print(f"🎬 Sending to Luma Ray 2 I2V animation service...")
            
            # Create Luma Ray request with intelligent prompt and freeze/animate lists
            luma_request = LumaRayRequest(
                prompt=intelligent_prompt,
                size="1280*720",  # Default high quality
                duration="5",
                image=None,  # Will use temp_image_path directly
                freeze=frozen_items if frozen_items else (["background"] if asset else None),
                animate=animate_items if animate_items else None
            )
            
            print(f"   🔒 Freeze list: {luma_request.freeze}")
            print(f"   🎬 Animate list: {luma_request.animate}")
            
            # Call Luma Ray animation service
            animation_result = await luma_ray_animate_image(temp_image_path, luma_request, job_id)
            
            if animation_result.get("success"):
                print(f"✅ Animation completed successfully!")
                
                video_url = animation_result.get("video_url")
                local_video_path = animation_result.get("local_video_path")
                
                # Update job status
                update_job_status(
                    job_id=job_id,
                    status="completed",
                    progress=100,
                    message="Smart animation completed successfully!",
                    result={
                        "job_type": "smart_animation",
                        "user_animation_prompt": animation_prompt,
                        "target_asset": asset if asset else None,
                        "freeze_assets": freeze_assets if freeze_assets else None,
                        "position": position if position else None,
                        "frozen_items_list": frozen_items if frozen_items else None,
                        "scene_analysis": scene_analysis,
                        "intelligent_prompt": intelligent_prompt,
                        "input_image_url": image_source_url,
                        "animation_url": video_url,
                        "local_video_path": local_video_path,
                        "api_job_id": animation_result.get("api_job_id"),
                        "processing_time": animation_result["processing_time"],
                        "size": size,
                        "duration": duration
                    }
                )
                
                return {
                    "success": True,
                    "job_id": job_id,
                    "animation_url": video_url,
                    "local_video_path": local_video_path,
                    "api_job_id": animation_result.get("api_job_id"),
                    "processing_time": animation_result["processing_time"],
                    "metadata": {
                        "user_prompt": animation_prompt,
                        "target_asset": asset if asset else None,
                        "freeze_assets": freeze_assets if freeze_assets else None,
                        "frozen_items": frozen_items if frozen_items else None,
                        "scene_analysis": scene_analysis[:500] + "...",  # Truncate for response
                        "intelligent_prompt": intelligent_prompt[:500] + "...",  # Truncate for response
                        "size": size,
                        "duration": duration,
                        "preserve_background": preserve_background,
                        "input_image_url": image_source_url
                    },
                    "message": f"🎉 Smart animation completed! Only '{asset if asset else animation_prompt}' was animated{' with frozen assets: ' + ', '.join(frozen_items) if frozen_items else ''} while preserving everything else.",
                    "workflow": {
                        "step_1": "Analyzed image with Llama 3.2 Vision",
                        "step_2": "Identified all scene elements",
                        "step_3": f"Crafted intelligent animation prompt{' (ASSET-SPECIFIC MODE)' if asset else ''}{' with freeze constraints' if frozen_items else ''}",
                        "step_4": f"Animated with Luma Ray 2 I2V preserving all other elements{' - ONLY ' + asset + ' was animated' if asset else ''}"
                    }
                }
            else:
                # Animation failed
                error_msg = animation_result.get("error", "Animation service error")
                
                update_job_status(
                    job_id=job_id,
                    status="failed",
                    progress=50,
                    message=f"Animation failed: {error_msg}",
                    result={
                        "job_type": "smart_animation",
                        "error": error_msg,
                        "target_asset": asset if asset else None,
                        "scene_analysis": scene_analysis,
                        "intelligent_prompt": intelligent_prompt
                    }
                )
                
                raise HTTPException(
                    status_code=500,
                    detail=f"Animation service failed: {error_msg}"
                )
                
        finally:
            # Clean up temporary file
            if temp_image_path and os.path.exists(temp_image_path):
                try:
                    os.unlink(temp_image_path)
                    print(f"🧹 Cleaned up temporary file")
                except Exception as cleanup_error:
                    print(f"⚠️ Failed to cleanup temp file: {cleanup_error}")

@app.post("/enhance/prompt")
async def enhance_prompt(
    prompt: str = Form(..., description="Original prompt to enhance"),
    preserve_original: bool = Form(False, description="Include original prompt in response")
):
    """✨ Prompt Enhancer: Improve prompts using Segmind Bria Prompt Enhancer API
    
    This endpoint uses the Segmind Bria Prompt Enhancer API to transform simple prompts
    into detailed, high-quality prompts suitable for AI image/video generation.
    
    **Features:**
    - AI-powered prompt enhancement and expansion
    - Adds artistic details, quality modifiers, and technical terms
    - Optimizes prompts for better generation results
    - Fast and simple to use
    
    **Use Cases:**
    - Enhance prompts before using /animate/prompt
    - Improve quality of /animate/easy prompts
    - Generate detailed prompts for image generation
    - Add professional artistic language to simple descriptions
    
    **Example Transformation:**
    - Input: "a futuristic cityscape under a starry night"
    - Enhanced: "A breathtaking futuristic cityscape illuminated by neon lights and holographic displays, set beneath a mesmerizing starry night sky with countless twinkling stars and distant galaxies, ultra-detailed, cinematic composition, 8K resolution, masterpiece quality"
    
    **Parameters:**
    - `prompt`: Your original prompt to enhance
    - `preserve_original`: If true, returns both original and enhanced prompts
    
    **Example:**
    ```bash
    curl -X POST "http://localhost:8000/enhance/prompt" \\
      -F "prompt=a beautiful woman in a night party"
    ```
    
    Returns:
        Enhanced prompt text
    """
    
    try:
        # Check for Segmind API key
        SEGMIND_API_KEY = os.getenv("SEGMIND_API_KEY")
        if not SEGMIND_API_KEY:
            raise HTTPException(
                status_code=503,
                detail="SEGMIND_API_KEY not configured. Please set the environment variable."
            )
        
        # Validate input
        if not prompt or len(prompt.strip()) == 0:
            raise HTTPException(
                status_code=400,
                detail="Prompt cannot be empty"
            )
        
        print(f"\n{'='*80}")
        print(f"✨ PROMPT ENHANCEMENT")
        print(f"{'='*80}")
        print(f"📝 Original prompt: {prompt}")
        
        # Call Segmind Bria Prompt Enhancer API
        api_url = "https://api.segmind.com/v1/bria-prompt-enhancer"
        
        data = {
            "prompt": prompt
        }
        
        headers = {
            'x-api-key': SEGMIND_API_KEY,
            'Content-Type': 'application/json'
        }
        
        print(f"🎨 Calling Bria Prompt Enhancer API...")
        
        start_time = time.time()
        response = requests.post(api_url, json=data, headers=headers, timeout=30)
        
        if response.status_code != 200:
            error_msg = f"Prompt Enhancer API error: {response.status_code}"
            try:
                error_data = response.json()
                error_msg = f"Prompt Enhancer API error: {error_data.get('error', response.text)}"
            except:
                error_msg = f"Prompt Enhancer API error: {response.text}"
            
            print(f"❌ {error_msg}")
            raise HTTPException(status_code=response.status_code, detail=error_msg)
        
        processing_time = time.time() - start_time
        
        # Parse response
        try:
            result = response.json()
            enhanced_prompt = result.get("enhanced_prompt", result.get("result", response.text))
        except:
            # If response is plain text
            enhanced_prompt = response.text
        
        print(f"✅ Prompt enhanced in {processing_time:.2f}s")
        print(f"✨ Enhanced prompt: {enhanced_prompt}")
        
        response_data = {
            "success": True,
            "enhanced_prompt": enhanced_prompt,
            "processing_time": processing_time,
            "character_count": {
                "original": len(prompt),
                "enhanced": len(enhanced_prompt),
                "increase": len(enhanced_prompt) - len(prompt)
            }
        }
        
        if preserve_original:
            response_data["original_prompt"] = prompt
        
        return response_data
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Prompt enhancement error: {str(e)}"
        print(f"❌ {error_msg}")
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

def map_yolo_class_to_category(yolo_class: str) -> str:
    """Map YOLO class name to our animation category system.
    
    Args:
        yolo_class: YOLO class name (e.g., "person", "car", "dog")
        
    Returns:
        Category name from our system
    """
    yolo_class_lower = yolo_class.lower()
    
    # Character mapping
    if yolo_class_lower in ["person", "people"]:
        return "characters"
    
    # Animal mapping
    if yolo_class_lower in ["dog", "cat", "horse", "cow", "elephant", "bear", "zebra", "giraffe",
                             "bird", "sheep", "tiger", "lion"]:
        return "animals"
    
    # Ground vehicles
    if yolo_class_lower in ["car", "truck", "bus", "motorcycle", "bicycle", "train"]:
        return "ground_vehicles"
    
    # Flying vehicles
    if yolo_class_lower in ["airplane", "helicopter", "kite"]:
        return "flying_vehicles"
    
    # Water vehicles
    if yolo_class_lower in ["boat", "ship"]:
        return "water_vehicles"
    
    # Furniture
    if yolo_class_lower in ["chair", "couch", "bed", "table", "desk", "bench"]:
        return "furniture"
    
    # Objects
    if yolo_class_lower in ["bottle", "cup", "bowl", "book", "laptop", "phone", "keyboard",
                             "mouse", "backpack", "umbrella", "handbag", "suitcase", "frisbee",
                             "skis", "snowboard", "sports ball", "kite", "baseball bat",
                             "skateboard", "surfboard", "tennis racket"]:
        return "objects"
    
    # Nature/environment
    if yolo_class_lower in ["potted plant", "tree"]:
        return "nature"
    
    # Default to objects
    return "objects"


def detect_objects_with_yolo(image_path: str) -> dict:
    """Detect objects in image using YOLOv8 for accurate bounding boxes.
    
    This provides much more accurate bounding boxes than text-based estimation.
    Uses YOLOv8n (nano) model for fast inference.
    
    Args:
        image_path: Path to image file
        
    Returns:
        Dictionary with:
        - detections: List of detected objects with accurate bounding boxes
        - image_width: Image width in pixels
        - image_height: Image height in pixels
    """
    try:
        from ultralytics import YOLO
        from PIL import Image
        
        print(f"🎯 Running YOLOv8 object detection for accurate bounding boxes...")
        
        # Load YOLOv8 nano model (fastest, good balance)
        # First time will download the model
        model = YOLO('yolov8n.pt')
        
        # Get image dimensions
        with Image.open(image_path) as img:
            image_width, image_height = img.size
        
        # Run inference
        results = model(image_path, verbose=False)
        
        detections = []
        
        # Process results
        for result in results:
            boxes = result.boxes
            for box in boxes:
                # Get box coordinates (xyxy format)
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                
                # Convert to x, y, width, height
                x = int(x1)
                y = int(y1)
                width = int(x2 - x1)
                height = int(y2 - y1)
                
                # Get class name and confidence
                class_id = int(box.cls[0])
                confidence = float(box.conf[0])
                class_name = model.names[class_id]
                
                # Only include detections with reasonable confidence
                if confidence >= 0.25:  # Lower threshold to catch more objects
                    detections.append({
                        "name": class_name,
                        "confidence_score": round(confidence, 2),
                        "bounding_box": {
                            "x": x,
                            "y": y,
                            "width": width,
                            "height": height
                        },
                        # Calculate position description for compatibility
                        "position": calculate_position_from_bbox(x, y, width, height, image_width, image_height),
                        "size": f"{round((width * height) / (image_width * image_height) * 100)}%"
                    })
        
        print(f"✅ YOLOv8 detected {len(detections)} objects with accurate bounding boxes")
        
        return {
            "detections": detections,
            "image_width": image_width,
            "image_height": image_height
        }
        
    except ImportError:
        print(f"⚠️ YOLOv8 not available - install with: pip install ultralytics")
        return None
    except Exception as e:
        print(f"⚠️ YOLOv8 detection failed: {e}")
        return None


def calculate_position_from_bbox(x: int, y: int, width: int, height: int, 
                                  image_width: int, image_height: int) -> str:
    """Calculate position description from bounding box coordinates.
    
    Args:
        x, y: Top-left corner of box
        width, height: Box dimensions
        image_width, image_height: Image dimensions
        
    Returns:
        Position description (e.g., "center-bottom")
    """
    # Calculate center of bounding box
    center_x = x + width / 2
    center_y = y + height / 2
    
    # Horizontal position
    if center_x < image_width * 0.33:
        h_pos = "left"
    elif center_x > image_width * 0.67:
        h_pos = "right"
    else:
        h_pos = "center"
    
    # Vertical position
    if center_y < image_height * 0.33:
        v_pos = "top"
    elif center_y > image_height * 0.67:
        v_pos = "bottom"
    else:
        v_pos = "middle"
    
    return f"{h_pos}-{v_pos}"


def extract_objects_from_llama_analysis(analysis_text: str, image_width: int, image_height: int) -> list:
    """Extract object information from Llama 3.2 Vision analysis text.
    
    Args:
        analysis_text: Raw text analysis from Llama 3.2 Vision
        image_width: Image width in pixels
        image_height: Image height in pixels
        
    Returns:
        List of objects with basic information extracted from text
    """
    objects = []
    
    try:
        # Simple keyword-based extraction (could be enhanced with NLP)
        analysis_lower = analysis_text.lower()
        
        # Common object keywords and their categories
        object_keywords = {
            # Characters
            "person": "characters", "people": "characters", "man": "characters", 
            "woman": "characters", "child": "characters", "human": "characters",
            "character": "characters", "figure": "characters",
            
            # Animals
            "dog": "animals", "cat": "animals", "horse": "animals", "bird": "animals",
            "animal": "animals", "pet": "animals",
            
            # Vehicles
            "car": "ground_vehicles", "truck": "ground_vehicles", "vehicle": "ground_vehicles",
            "bicycle": "ground_vehicles", "motorcycle": "ground_vehicles",
            "airplane": "flying_vehicles", "plane": "flying_vehicles",
            "boat": "water_vehicles", "ship": "water_vehicles",
            
            # Objects
            "tree": "nature", "building": "environment", "house": "environment",
            "chair": "furniture", "table": "furniture",
            "object": "objects", "item": "objects"
        }
        
        # Look for objects mentioned in the analysis
        for keyword, category in object_keywords.items():
            if keyword in analysis_lower:
                # Estimate confidence based on context
                confidence = 0.6  # Base confidence for text extraction
                
                # Boost confidence if mentioned multiple times or with detail
                mention_count = analysis_lower.count(keyword)
                if mention_count > 1:
                    confidence += 0.1
                
                # Look for descriptive words nearby
                if any(word in analysis_lower for word in ["detailed", "clear", "visible", "prominent"]):
                    confidence += 0.1
                
                # Cap confidence at 0.8 for text extraction
                confidence = min(confidence, 0.8)
                
                objects.append({
                    "name": keyword,
                    "category": category,
                    "confidence": confidence,
                    "detection_method": "llama_text_extraction",
                    "mention_count": mention_count
                })
        
        # Remove duplicates by name
        seen_names = set()
        unique_objects = []
        for obj in objects:
            if obj["name"] not in seen_names:
                seen_names.add(obj["name"])
                unique_objects.append(obj)
        
        return unique_objects
        
    except Exception as e:
        print(f"⚠️ Error extracting objects from Llama analysis: {e}")
        return []


def converge_detection_results(yolo_detections: list, llama_objects: list, llama_analysis: str, 
                              image_width: int, image_height: int) -> list:
    """Converge YOLOv8 and Llama 3.2 Vision results into unified, high-confidence objects.
    
    Args:
        yolo_detections: List of objects detected by YOLOv8
        llama_objects: List of objects extracted from Llama analysis
        llama_analysis: Full Llama analysis text
        image_width: Image width in pixels
        image_height: Image height in pixels
        
    Returns:
        List of converged objects with enhanced properties and confidence scores
    """
    converged_objects = []
    
    try:
        # Start with YOLOv8 detections as base (they have accurate bounding boxes)
        for yolo_obj in yolo_detections:
            # Find matching Llama objects
            llama_confidence = 0.0
            llama_match = None
            
            for llama_obj in llama_objects:
                # Simple name matching (could be enhanced with semantic similarity)
                if (llama_obj["name"] in yolo_obj["name"].lower() or 
                    yolo_obj["name"] in llama_obj["name"] or
                    llama_obj["category"] == map_yolo_class_to_category(yolo_obj["name"])):
                    llama_confidence = llama_obj["confidence"]
                    llama_match = llama_obj
                    break
            
            # If no direct match, check if category is mentioned in analysis
            if not llama_match:
                category = map_yolo_class_to_category(yolo_obj["name"])
                category_keywords = {
                    "characters": ["person", "people", "human", "character", "figure"],
                    "animals": ["animal", "pet", "creature"],
                    "ground_vehicles": ["car", "vehicle", "truck", "automobile"],
                    "flying_vehicles": ["plane", "aircraft", "airplane"],
                    "nature": ["tree", "plant", "vegetation"]
                }
                
                if category in category_keywords:
                    for keyword in category_keywords[category]:
                        if keyword in llama_analysis.lower():
                            llama_confidence = 0.5  # Moderate confidence for category match
                            break
            
            # Calculate converged confidence
            yolo_confidence = yolo_obj["confidence_score"]
            
            # Weighted average: YOLOv8 has higher weight due to precise detection
            if llama_confidence > 0:
                converged_confidence = (yolo_confidence * 0.7) + (llama_confidence * 0.3)
            else:
                converged_confidence = yolo_confidence * 0.8  # Slight penalty for no Llama confirmation
            
            # Determine confidence level
            if converged_confidence >= 0.7:
                confidence_level = "high"
            elif converged_confidence >= 0.4:
                confidence_level = "medium"
            else:
                confidence_level = "low"
            
            # Calculate additional properties
            bbox = yolo_obj["bounding_box"]
            area_pixels = bbox["width"] * bbox["height"]
            area_percentage = round((area_pixels / (image_width * image_height)) * 100, 1)
            
            # Enhanced animation assessment
            category = map_yolo_class_to_category(yolo_obj["name"])
            animation_potential, animation_reasoning = assess_animation_potential(
                yolo_obj["name"], category, converged_confidence, area_percentage
            )
            
            # Build comprehensive object entry
            converged_object = {
                # Basic identification
                "name": yolo_obj["name"],
                "category": category,
                "description": f"Detected {yolo_obj['name']} with dual AI analysis",
                
                # Confidence metrics
                "yolo_confidence": round(yolo_confidence, 3),
                "llama_confidence": round(llama_confidence, 3),
                "converged_confidence": round(converged_confidence, 3),
                "confidence_level": confidence_level,
                
                # Spatial properties
                "bounding_box": bbox,
                "position": yolo_obj["position"],
                "size_category": categorize_object_size(area_percentage),
                "area_percentage": area_percentage,
                "center_point": {
                    "x": bbox["x"] + bbox["width"] // 2,
                    "y": bbox["y"] + bbox["height"] // 2
                },
                
                # Animation properties
                "animation_potential": animation_potential,
                "animation_reasoning": animation_reasoning,
                "suggested_animations": get_animation_suggestions(category, yolo_obj["name"]),
                
                # Visual properties
                "visibility": assess_visibility(converged_confidence, area_percentage),
                "occlusion_level": assess_occlusion(bbox, image_width, image_height),
                "complexity": assess_complexity(category, area_percentage),
                "lighting_quality": "good",  # Could be enhanced with image analysis
                
                # Metadata
                "detection_methods": ["yolo"] + (["llama"] if llama_match else []),
                "yolo_class": yolo_obj["name"],
                "llama_match": bool(llama_match)
            }
            
            converged_objects.append(converged_object)
        
        # Sort by converged confidence (highest first)
        converged_objects.sort(key=lambda x: -x["converged_confidence"])
        
        return converged_objects
        
    except Exception as e:
        print(f"⚠️ Error converging detection results: {e}")
        # Fallback: return YOLOv8 results with basic enhancement
        return enhance_yolo_results_fallback(yolo_detections, image_width, image_height)


def assess_animation_potential(object_name: str, category: str, confidence: float, area_percentage: float) -> tuple:
    """Assess animation potential and provide reasoning.
    
    Returns:
        Tuple of (potential_level: str, reasoning: str)
    """
    # Base potential by category
    category_potentials = {
        "characters": ("high", "Characters are highly suitable for complex animations including walking, gestures, and expressions"),
        "animals": ("high", "Animals offer natural movement patterns and are engaging for animation"),
        "ground_vehicles": ("high", "Vehicles can be animated with realistic motion including driving, turning, and speed effects"),
        "flying_vehicles": ("high", "Flying vehicles offer dynamic animation opportunities including flight paths and aerial maneuvers"),
        "water_vehicles": ("medium", "Water vehicles can be animated but may be limited by water physics requirements"),
        "nature": ("medium", "Natural elements like trees can sway and move but with more subtle animations"),
        "furniture": ("low", "Furniture typically has limited animation potential, mainly opening/closing actions"),
        "objects": ("medium", "Objects can be animated depending on their specific type and context")
    }
    
    base_potential, base_reasoning = category_potentials.get(category, ("low", "Limited animation potential"))
    
    # Adjust based on confidence and size
    if confidence < 0.4:
        return ("low", f"{base_reasoning} However, low detection confidence may affect animation quality.")
    elif area_percentage < 1:
        return ("low", f"{base_reasoning} However, the object is very small in the image which limits animation visibility.")
    elif area_percentage > 25:
        return ("high", f"{base_reasoning} Large size in image makes it ideal for prominent animation.")
    else:
        return (base_potential, base_reasoning)


def categorize_object_size(area_percentage: float) -> str:
    """Categorize object size based on area percentage."""
    if area_percentage >= 25:
        return "large"
    elif area_percentage >= 10:
        return "medium"
    elif area_percentage >= 2:
        return "small"
    else:
        return "tiny"


def assess_visibility(confidence: float, area_percentage: float) -> str:
    """Assess object visibility."""
    if confidence >= 0.7 and area_percentage >= 5:
        return "clear"
    elif confidence >= 0.5 and area_percentage >= 2:
        return "good"
    elif confidence >= 0.3:
        return "partial"
    else:
        return "poor"


def assess_occlusion(bbox: dict, image_width: int, image_height: int) -> str:
    """Assess if object is occluded by image boundaries."""
    x, y, w, h = bbox["x"], bbox["y"], bbox["width"], bbox["height"]
    
    # Check if object touches image boundaries
    touches_edge = (x <= 5 or y <= 5 or x + w >= image_width - 5 or y + h >= image_height - 5)
    
    if touches_edge:
        return "partial"
    else:
        return "none"


def assess_complexity(category: str, area_percentage: float) -> str:
    """Assess animation complexity based on object type and size."""
    complex_categories = ["characters", "animals"]
    
    if category in complex_categories:
        if area_percentage >= 15:
            return "high"
        else:
            return "medium"
    else:
        return "low"


def enhance_yolo_results_fallback(yolo_detections: list, image_width: int, image_height: int) -> list:
    """Fallback function to enhance YOLOv8 results when Llama analysis fails."""
    enhanced_objects = []
    
    for detection in yolo_detections:
        bbox = detection["bounding_box"]
        area_percentage = round((bbox["width"] * bbox["height"]) / (image_width * image_height) * 100, 1)
        category = map_yolo_class_to_category(detection["name"])
        
        enhanced_object = {
            "name": detection["name"],
            "category": category,
            "description": f"YOLOv8 detected {detection['name']}",
            "yolo_confidence": detection["confidence_score"],
            "llama_confidence": 0.0,
            "converged_confidence": detection["confidence_score"] * 0.8,
            "confidence_level": "medium" if detection["confidence_score"] >= 0.5 else "low",
            "bounding_box": bbox,
            "position": detection["position"],
            "size_category": categorize_object_size(area_percentage),
            "area_percentage": area_percentage,
            "animation_potential": "medium",
            "animation_reasoning": "Based on YOLOv8 detection only",
            "suggested_animations": get_animation_suggestions(category, detection["name"]),
            "detection_methods": ["yolo"],
            "llama_match": False
        }
        
        enhanced_objects.append(enhanced_object)
    
    return enhanced_objects


def generate_animation_recommendations(objects: list, scene_analysis: str) -> list:
    """Generate comprehensive animation recommendations based on detected objects and scene context."""
    recommendations = []
    
    if not objects:
        return ["No objects detected suitable for animation"]
    
    # Count objects by category
    categories = {}
    high_confidence_objects = []
    
    for obj in objects:
        category = obj["category"]
        categories[category] = categories.get(category, 0) + 1
        
        if obj["confidence_level"] == "high":
            high_confidence_objects.append(obj)
    
    # General recommendations
    total_objects = len(objects)
    high_conf_count = len(high_confidence_objects)
    
    recommendations.append(f"🎯 Detected {total_objects} animatable objects ({high_conf_count} high-confidence)")
    
    # Category-specific recommendations
    if "characters" in categories:
        char_count = categories["characters"]
        recommendations.append(f"👥 {char_count} character(s) detected - ideal for walking, jumping, gesturing, or action sequences")
    
    if "animals" in categories:
        animal_count = categories["animals"]
        recommendations.append(f"🐾 {animal_count} animal(s) detected - suitable for natural movement, running, or species-specific behaviors")
    
    if "ground_vehicles" in categories:
        vehicle_count = categories["ground_vehicles"]
        recommendations.append(f"🚗 {vehicle_count} vehicle(s) detected - perfect for driving, racing, or transportation animations")
    
    if "flying_vehicles" in categories:
        flying_count = categories["flying_vehicles"]
        recommendations.append(f"✈️ {flying_count} flying vehicle(s) detected - excellent for aerial animations and flight sequences")
    
    # Technical recommendations
    large_objects = [obj for obj in objects if obj["area_percentage"] >= 15]
    if large_objects:
        recommendations.append(f"📏 {len(large_objects)} large object(s) ideal for prominent animation effects")
    
    small_objects = [obj for obj in objects if obj["area_percentage"] < 5]
    if small_objects:
        recommendations.append(f"🔍 {len(small_objects)} small object(s) - consider subtle animations or group effects")
    
    # Confidence-based recommendations
    if high_conf_count >= total_objects * 0.8:
        recommendations.append("✅ High detection confidence - animations will be highly reliable")
    elif high_conf_count < total_objects * 0.3:
        recommendations.append("⚠️ Lower detection confidence - consider manual verification before animation")
    
    # Scene-specific recommendations from Llama analysis
    if "multiple" in scene_analysis.lower() or "several" in scene_analysis.lower():
        recommendations.append("🎬 Complex scene detected - use selective animation to focus on key elements")
    
    if "lighting" in scene_analysis.lower():
        recommendations.append("💡 Scene lighting noted - consider lighting effects in animations")
    
    return recommendations


def analyze_prompt_relevance(detected_objects: list, context_analysis: dict, scene_analysis: str) -> dict:
    """Analyze how well detected objects match user prompts for context-aware confidence scoring.
    
    Args:
        detected_objects: List of objects detected by dual AI system
        context_analysis: Processed context prompts and metadata
        scene_analysis: Full scene analysis from Llama 3.2 Vision
        
    Returns:
        Dictionary with prompt relevance analysis and adjusted confidence scores
    """
    if not context_analysis["has_context"]:
        # No context provided - return technical confidence only
        return {
            "mode": "technical_only",
            "prompt_relevance_score": 0.0,
            "context_match_details": {},
            "objects_with_relevance": detected_objects  # No changes to objects
        }
    
    print(f"🎯 Analyzing prompt relevance for {len(detected_objects)} detected objects...")
    
    # Combine all prompts for comprehensive analysis
    all_prompts = context_analysis["all_prompts"]
    combined_prompt_text = " ".join(all_prompts).lower()
    
    # Extract key terms and concepts from prompts
    prompt_keywords = set()
    for prompt in all_prompts:
        # Simple keyword extraction (could be enhanced with NLP)
        words = prompt.lower().replace(',', ' ').replace('.', ' ').split()
        prompt_keywords.update([word for word in words if len(word) > 2])
    
    # Analyze each object for prompt relevance
    objects_with_relevance = []
    relevance_scores = []
    match_details = {}
    
    for obj in detected_objects:
        object_name = obj["name"].lower()
        object_category = obj["category"].lower()
        technical_confidence = obj["converged_confidence"]
        
        # Calculate prompt relevance score
        relevance_score = 0.0
        matches = []
        
        # Direct name matching
        if object_name in combined_prompt_text:
            relevance_score += 0.4
            matches.append(f"direct_name: '{object_name}' found in prompts")
        
        # Category matching
        category_keywords = {
            "characters": ["person", "people", "human", "character", "man", "woman", "child"],
            "animals": ["animal", "pet", "dog", "cat", "bird", "creature"],
            "ground_vehicles": ["car", "vehicle", "truck", "bike", "automobile", "transport"],
            "flying_vehicles": ["plane", "aircraft", "helicopter", "flying"],
            "water_vehicles": ["boat", "ship", "vessel"],
            "nature": ["tree", "plant", "flower", "nature", "vegetation"],
            "objects": ["object", "item", "thing"]
        }
        
        if object_category in category_keywords:
            for keyword in category_keywords[object_category]:
                if keyword in combined_prompt_text:
                    relevance_score += 0.2
                    matches.append(f"category_match: '{keyword}' (category: {object_category})")
                    break
        
        # Contextual matching from scene analysis
        if object_name in scene_analysis.lower():
            relevance_score += 0.2
            matches.append(f"scene_context: '{object_name}' mentioned in scene analysis")
        
        # Semantic similarity (basic implementation)
        object_synonyms = {
            "person": ["human", "individual", "character", "figure"],
            "car": ["vehicle", "automobile", "auto"],
            "dog": ["pet", "canine"],
            "tree": ["plant", "vegetation"]
        }
        
        if object_name in object_synonyms:
            for synonym in object_synonyms[object_name]:
                if synonym in combined_prompt_text:
                    relevance_score += 0.15
                    matches.append(f"semantic_match: '{synonym}' relates to '{object_name}'")
                    break
        
        # Cap relevance score at 1.0
        relevance_score = min(relevance_score, 1.0)
        
        # Calculate context-aware confidence
        # Weight: 60% technical confidence + 40% prompt relevance
        if context_analysis["has_context"]:
            context_aware_confidence = (technical_confidence * 0.6) + (relevance_score * 0.4)
        else:
            context_aware_confidence = technical_confidence
        
        # Update object with relevance data
        enhanced_object = obj.copy()
        enhanced_object.update({
            "technical_confidence": technical_confidence,
            "prompt_relevance_score": round(relevance_score, 3),
            "context_aware_confidence": round(context_aware_confidence, 3),
            "converged_confidence": round(context_aware_confidence, 3),  # Update main confidence
            "prompt_matches": matches,
            "relevance_reasoning": f"Matches {len(matches)} prompt elements" if matches else "No direct prompt matches found"
        })
        
        objects_with_relevance.append(enhanced_object)
        relevance_scores.append(relevance_score)
        match_details[obj["name"]] = {
            "relevance_score": relevance_score,
            "matches": matches,
            "technical_confidence": technical_confidence,
            "context_aware_confidence": context_aware_confidence
        }
    
    # Calculate overall prompt relevance
    overall_relevance = sum(relevance_scores) / len(relevance_scores) if relevance_scores else 0.0
    
    print(f"✅ Prompt relevance analysis completed:")
    print(f"   • Overall relevance: {overall_relevance:.2f}")
    print(f"   • Objects with high relevance (>0.5): {sum(1 for s in relevance_scores if s > 0.5)}")
    print(f"   • Objects with matches: {sum(1 for obj in objects_with_relevance if obj['prompt_matches'])}")
    
    return {
        "mode": "context_aware",
        "prompt_relevance_score": round(overall_relevance, 3),
        "high_relevance_count": sum(1 for s in relevance_scores if s > 0.5),
        "objects_with_matches": sum(1 for obj in objects_with_relevance if obj["prompt_matches"]),
        "context_match_details": match_details,
        "objects_with_relevance": objects_with_relevance,
        "prompt_keywords": list(prompt_keywords),
        "analysis_summary": f"Found {len(objects_with_relevance)} objects, {sum(1 for s in relevance_scores if s > 0.5)} highly relevant to user prompts"
    }


@app.post("/animate/vision")
async def analyze_animatable_objects(
    file: Optional[UploadFile] = File(None, description="Image file to analyze (optional if image_url provided)"),
    image_url: Optional[str] = Form(None, description="URL of image to analyze (optional if file provided)"),
    user_prompt: Optional[str] = Form(None, description="Original user prompt/description for context-aware confidence scoring"),
    enhanced_prompt: Optional[str] = Form(None, description="Enhanced version of user prompt for improved context matching"),
    context_prompts: Optional[str] = Form(None, description="JSON array of additional context prompts for comprehensive matching")
):
    """🔍 AI-Powered Vision Analysis: Context-Aware Dual Model Object Detection
    
    This advanced endpoint combines **Llama 3.2 Vision** and **YOLOv8** for comprehensive image analysis,
    with **prompt-based confidence scoring** that evaluates how well detected objects match user intent.
    
    **🤖 Dual AI Model Architecture:**
    - **YOLOv8**: Precise object detection with accurate bounding boxes and confidence scores
    - **Llama 3.2 Vision**: Contextual scene understanding and detailed object descriptions
    - **Context-Aware Convergence**: Matches detected objects against user prompts for relevance scoring
    
    **🎯 Context-Aware Confidence System:**
    - **Prompt Relevance Analysis**: Evaluates how well detected objects match user prompts
    - **Multi-Prompt Support**: Considers original, enhanced, and additional context prompts
    - **Intent-Based Scoring**: Confidence reflects user intent fulfillment, not just detection accuracy
    - **Semantic Matching**: Uses AI to understand prompt-object relationships beyond keyword matching
    
    **📊 Enhanced Confidence Metrics:**
    - **technical_confidence**: Raw detection quality from AI models
    - **prompt_relevance_confidence**: How well objects match user prompts (0.0-1.0)
    - **general_confidence**: Combined score weighing both technical quality and user intent
    - **context_match_details**: Detailed breakdown of prompt-object matching
    
    **💡 Key Innovation:**
    Traditional vision APIs only measure "can we detect objects accurately?" 
    This system answers "do the detected objects match what the user actually wanted?"
    
    Args:
        file: Image file to analyze (provide either file or image_url)
        image_url: URL of image to analyze (provide either file or image_url)
        user_prompt: Original user prompt/description for context-aware confidence scoring
        enhanced_prompt: Enhanced version of user prompt for improved context matching
        context_prompts: JSON array of additional context prompts for comprehensive matching
        image_url: URL of image to analyze (provide either file or image_url)
    
    Returns:
        JSON response with:
        - analysis_metadata: Processing information and model performance
        - image_dimensions: Width and height of the analyzed image in pixels
        - detected_objects: Array of objects with comprehensive properties
        - scene_analysis: Overall scene understanding from Llama 3.2 Vision
        - confidence_statistics: Aggregated confidence metrics
        - animation_recommendations: AI-generated animation suggestions
        - processing_details: Model performance and convergence information
        
    Example Response:
    ```json
    {
      "success": true,
      "analysis_metadata": {
        "yolo_detections": 3,
        "llama_detections": 2,
        "converged_objects": 2,
        "processing_time": 2.5
      },
      "detected_objects": [
        {
          "name": "red sports car",
          "category": "ground_vehicles",
          "description": "A sleek red sports car positioned in the foreground",
          "yolo_confidence": 0.85,
          "llama_confidence": 0.90,
          "converged_confidence": 0.875,
          "confidence_level": "high",
          "bounding_box": {"x": 640, "y": 720, "width": 384, "height": 216},
          "position": "center-bottom",
          "size_category": "large",
          "area_percentage": 18.5,
          "animation_potential": "high",
          "animation_reasoning": "Vehicles are highly suitable for motion-based animations",
          "suggested_animations": ["driving forward", "wheels spinning", "turning"],
          "visibility": "clear",
          "occlusion_level": "none",
          "complexity": "medium",
          "lighting_quality": "good"
        }
      ]
    }
    ```
    """
    
    # Generate job ID for this vision analysis
    job_id = str(uuid.uuid4())
    start_time = time.time()
    print(f"\n{'='*80}")
    print(f"🔍 CONTEXT-AWARE DUAL AI VISION ANALYSIS - Job ID: {job_id}")
    print(f"{'='*80}")
    
    # Process and prepare context prompts for relevance analysis
    context_analysis = {
        "user_prompt": user_prompt.strip() if user_prompt else None,
        "enhanced_prompt": enhanced_prompt.strip() if enhanced_prompt else None,
        "additional_prompts": [],
        "has_context": False
    }
    
    # Parse additional context prompts if provided
    if context_prompts:
        try:
            import json
            additional = json.loads(context_prompts)
            if isinstance(additional, list):
                context_analysis["additional_prompts"] = [str(p).strip() for p in additional if p]
        except:
            print(f"⚠️ Failed to parse context_prompts JSON, ignoring")
    
    # Determine if we have meaningful context for relevance analysis
    all_prompts = [p for p in [context_analysis["user_prompt"], context_analysis["enhanced_prompt"]] + context_analysis["additional_prompts"] if p]
    context_analysis["has_context"] = len(all_prompts) > 0
    context_analysis["all_prompts"] = all_prompts
    
    if context_analysis["has_context"]:
        print(f"📝 Context-aware analysis enabled:")
        if context_analysis["user_prompt"]:
            print(f"   • User prompt: {context_analysis['user_prompt'][:100]}...")
        if context_analysis["enhanced_prompt"]:
            print(f"   • Enhanced prompt: {context_analysis['enhanced_prompt'][:100]}...")
        if context_analysis["additional_prompts"]:
            print(f"   • Additional prompts: {len(context_analysis['additional_prompts'])} provided")
        print(f"💡 Confidence scoring will include prompt relevance analysis")
    else:
        print(f"⚙️ Technical-only analysis (no context prompts provided)")
        print(f"💡 Confidence scoring will use traditional detection metrics only")
    
    try:
        # Validate input - need either file or image_url
        if not file and not image_url:
            raise HTTPException(
                status_code=400, 
                detail="Either 'file' or 'image_url' must be provided"
            )
        
        print(f"🔍 Starting dual model vision analysis...")
        
        # Step 1: Get the image (either from upload or URL)
        temp_image_path = None
        image_source_url = None
        s3_image_url = None
        
        try:
            if file:
                # Save uploaded file to temporary location
                print(f"📤 Processing uploaded file: {file.filename}")
                
                # Validate file type
                if not file.content_type.startswith('image/'):
                    raise HTTPException(status_code=400, detail="File must be an image")
                
                # Read file content
                file_content = await file.read()
                
                # Create temporary file
                file_ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
                temp_fd, temp_image_path = tempfile.mkstemp(suffix=file_ext)
                
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    temp_file.write(file_content)
                
                print(f"✅ Image saved to temporary file")
                
                # Upload to S3 for permanent storage
                print(f"☁️ Uploading image to S3...")
                s3_image_url = upload_asset_to_s3(temp_image_path, "vision_analysis", job_id, f"input_{file.filename}")
                image_source_url = s3_image_url
                print(f"✅ S3 URL: {s3_image_url}")
                    
            else:
                # Use provided URL
                print(f"🔗 Using provided image URL: {image_url[:50]}...")
                image_source_url = image_url
                
                # Download image to temporary file for analysis
                response = requests.get(image_url, timeout=30)
                response.raise_for_status()
                
                # Determine file extension
                content_type = response.headers.get('content-type', '')
                file_ext = '.jpg'
                if 'png' in content_type:
                    file_ext = '.png'
                elif 'webp' in content_type:
                    file_ext = '.webp'
                
                temp_fd, temp_image_path = tempfile.mkstemp(suffix=file_ext)
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    temp_file.write(response.content)
                
                print(f"✅ Image downloaded to temporary file")
                
                # Also upload to S3 for consistency
                print(f"☁️ Uploading image to S3...")
                s3_image_url = upload_asset_to_s3(temp_image_path, "vision_analysis", job_id, f"input_from_url{file_ext}")
                print(f"✅ S3 URL: {s3_image_url}")
            
            # Get image dimensions
            from PIL import Image
            with Image.open(temp_image_path) as img:
                image_width, image_height = img.size
            print(f"📐 Image dimensions: {image_width}x{image_height}")
            
            # Step 2: YOLOv8 Object Detection
            print(f"\n🎯 PHASE 1: YOLOv8 Object Detection")
            print(f"{'='*50}")
            
            yolo_start_time = time.time()
            yolo_results = detect_objects_with_yolo(temp_image_path)
            yolo_processing_time = time.time() - yolo_start_time
            
            if not yolo_results:
                raise HTTPException(
                    status_code=503,
                    detail="YOLOv8 not available. Please install: pip install ultralytics"
                )
            
            yolo_detections = yolo_results['detections']
            print(f"✅ YOLOv8 completed in {yolo_processing_time:.2f}s")
            print(f"📦 YOLOv8 detected {len(yolo_detections)} objects")
            
            for detection in yolo_detections:
                bbox = detection['bounding_box']
                print(f"   • {detection['name']} (conf: {detection['confidence_score']:.2f}) at {bbox['x']},{bbox['y']},{bbox['width']}x{bbox['height']}")
            
            # Step 3: Llama 3.2 Vision Analysis
            print(f"\n🧠 PHASE 2: Llama 3.2 Vision Analysis")
            print(f"{'='*50}")
            
            llama_start_time = time.time()
            
            # Enhanced prompt for detailed object analysis
            vision_prompt = """Analyze this image in detail for animation potential. Provide a comprehensive analysis including:

1. **Scene Overview**: Describe the overall scene, environment, and context
2. **Detected Objects**: List ALL visible objects, characters, and elements that could be animated
3. **Animation Assessment**: For each object, evaluate its animation potential and suggest specific movements
4. **Technical Details**: Lighting, composition, visual quality, and any challenges for animation
5. **Creative Opportunities**: Unique animation possibilities based on the scene

For each object you identify, provide:
- Object name and description
- Position in the scene (approximate location)
- Size relative to the image
- Animation potential (high/medium/low) with reasoning
- Specific animation suggestions
- Any visual characteristics that affect animation

Be thorough and detailed in your analysis. Focus on identifying elements that would create engaging animations."""
            
            # Call Llama 3.2 Vision
            llama_analysis = ""
            llama_processing_time = 0
            llama_objects = []
            
            try:
                # Read and encode image to base64
                image_content = open(temp_image_path, 'rb').read()
                image_base64 = base64.b64encode(image_content).decode('utf-8')
                
                # Call Ollama Vision API
                ollama_url = "http://localhost:11434/api/chat"
                
                payload = {
                    "model": "llama3.2-vision",
                    "messages": [
                        {
                            "role": "user",
                            "content": vision_prompt,
                            "images": [image_base64]
                        }
                    ],
                    "stream": False
                }
                
                print(f"🔄 Calling Llama 3.2 Vision...")
                
                response = requests.post(ollama_url, json=payload, timeout=120)
                llama_processing_time = time.time() - llama_start_time
                
                if response.status_code == 200:
                    result = response.json()
                    llama_analysis = result.get("message", {}).get("content", "").strip()
                    
                    if llama_analysis:
                        print(f"✅ Llama 3.2 Vision completed in {llama_processing_time:.2f}s")
                        print(f"📝 Analysis: {llama_analysis[:200]}...")
                        
                        # Parse Llama analysis for objects (basic extraction)
                        llama_objects = extract_objects_from_llama_analysis(llama_analysis, image_width, image_height)
                        print(f"🔍 Extracted {len(llama_objects)} objects from Llama analysis")
                    else:
                        print(f"⚠️ Empty response from Llama 3.2 Vision")
                        llama_analysis = "Vision model provided empty response"
                else:
                    print(f"❌ Llama 3.2 Vision failed: HTTP {response.status_code}")
                    llama_analysis = f"Vision analysis failed: HTTP {response.status_code}"
                    
            except requests.exceptions.ConnectionError:
                print(f"❌ Cannot connect to Ollama - using YOLOv8 results only")
                llama_analysis = "Llama 3.2 Vision unavailable - Ollama not running"
            except Exception as e:
                print(f"❌ Llama 3.2 Vision error: {e}")
                llama_analysis = f"Vision analysis error: {str(e)}"
                llama_processing_time = time.time() - llama_start_time
            
            # Step 4: Convergence Engine - Merge and Score Results
            print(f"\n🔀 PHASE 3: Convergence Engine")
            print(f"{'='*50}")
            
            converged_objects = converge_detection_results(
                yolo_detections, 
                llama_objects, 
                llama_analysis,
                image_width, 
                image_height
            )
            
            print(f"✅ Converged {len(yolo_detections)} YOLO + {len(llama_objects)} Llama detections")
            print(f"📊 Initial result: {len(converged_objects)} detected objects")
            
            # Step 5: Context-Aware Prompt Relevance Analysis
            print(f"\n🎯 PHASE 4: Context-Aware Prompt Relevance Analysis")
            print(f"{'='*60}")
            
            relevance_analysis = analyze_prompt_relevance(
                converged_objects,
                context_analysis,
                llama_analysis
            )
            
            # Use context-enhanced objects as final result
            final_objects = relevance_analysis["objects_with_relevance"]
            
            if context_analysis["has_context"]:
                print(f"✅ Context-aware confidence applied to all objects")
                print(f"📈 Prompt relevance score: {relevance_analysis['prompt_relevance_score']:.2f}")
                print(f"🎯 Objects matching prompts: {relevance_analysis['objects_with_matches']}/{len(final_objects)}")
            else:
                print(f"⚙️ Using technical confidence only (no context provided)")
                final_objects = converged_objects  # No changes needed
            
            # Step 5: Generate comprehensive analysis metadata
            total_processing_time = time.time() - start_time
            
            analysis_metadata = {
                "models_used": ["YOLOv8", "Llama 3.2 Vision"],
                "yolo_detections": len(yolo_detections),
                "llama_detections": len(llama_objects),
                "converged_objects": len(converged_objects),
                "processing_time": {
                    "total": round(total_processing_time, 2),
                    "yolo": round(yolo_processing_time, 2),
                    "llama": round(llama_processing_time, 2)
                },
                "image_analysis": {
                    "dimensions": {"width": image_width, "height": image_height},
                    "total_pixels": image_width * image_height,
                    "aspect_ratio": round(image_width / image_height, 2)
                }
            }
            
            # Calculate confidence statistics with context-aware general confidence accumulation
            if final_objects:
                # Extract confidence scores (now context-aware if prompts provided)
                confidences = [obj["converged_confidence"] for obj in final_objects]
                technical_confidences = [obj.get("technical_confidence", obj["converged_confidence"]) for obj in final_objects]
                prompt_relevance_scores = [obj.get("prompt_relevance_score", 0.0) for obj in final_objects]
                
                # Calculate basic statistics
                avg_confidence = sum(confidences) / len(confidences)
                max_confidence = max(confidences)
                min_confidence = min(confidences)
                avg_technical = sum(technical_confidences) / len(technical_confidences)
                avg_relevance = sum(prompt_relevance_scores) / len(prompt_relevance_scores)
                
                # Calculate general confidence using weighted approach
                # Factor in: average confidence, number of objects, confidence distribution, prompt relevance
                # Factor in: average confidence, number of objects, confidence distribution
                confidence_weights = []
                area_weights = []
                
                for obj in final_objects:
                    # Weight by area (larger objects get more weight)
                    area_weight = min(obj.get("area_percentage", 5) / 100, 0.3)  # Cap at 30%
                    area_weights.append(area_weight)
                    confidence_weights.append(obj["converged_confidence"] * area_weight)
                
                # Weighted average confidence (larger objects influence more)
                if sum(area_weights) > 0:
                    weighted_avg_confidence = sum(confidence_weights) / sum(area_weights)
                else:
                    weighted_avg_confidence = avg_confidence
                
                # Penalty/bonus factors
                object_count_factor = min(len(final_objects) / 5, 1.0)  # Bonus for more objects, cap at 5
                high_conf_ratio = sum(1 for c in confidences if c >= 0.7) / len(confidences)
                consistency_bonus = 1.0 - (max_confidence - min_confidence)  # Bonus for consistent confidences
                
                # Context-aware general confidence calculation
                if context_analysis["has_context"]:
                    # Include prompt relevance in general confidence
                    prompt_relevance_factor = relevance_analysis["prompt_relevance_score"]
                    high_relevance_ratio = relevance_analysis["high_relevance_count"] / len(final_objects) if final_objects else 0
                    
                    general_confidence = (
                        weighted_avg_confidence * 0.4 +    # 40% weighted technical average
                        prompt_relevance_factor * 0.3 +    # 30% prompt relevance
                        high_conf_ratio * 0.15 +           # 15% high confidence ratio
                        high_relevance_ratio * 0.1 +       # 10% high relevance ratio
                        consistency_bonus * 0.05           # 5% consistency bonus
                    )
                else:
                    # Traditional technical-only confidence
                    general_confidence = (
                        weighted_avg_confidence * 0.6 +  # 60% weighted average
                        high_conf_ratio * 0.25 +         # 25% high confidence ratio
                        object_count_factor * 0.1 +      # 10% object count factor
                        consistency_bonus * 0.05         # 5% consistency bonus
                    )
                
                # Ensure general confidence stays within bounds
                general_confidence = max(0.0, min(1.0, general_confidence))
                
                # Determine general confidence level
                if general_confidence >= 0.8:
                    general_confidence_level = "excellent"
                elif general_confidence >= 0.7:
                    general_confidence_level = "high"
                elif general_confidence >= 0.5:
                    general_confidence_level = "medium"
                elif general_confidence >= 0.3:
                    general_confidence_level = "low"
                else:
                    general_confidence_level = "poor"
                
                confidence_stats = {
                    # Individual object statistics (now context-aware)
                    "average_confidence": round(avg_confidence, 3),
                    "max_confidence": round(max_confidence, 3),
                    "min_confidence": round(min_confidence, 3),
                    "weighted_average": round(weighted_avg_confidence, 3),
                    
                    # Technical vs Context-aware breakdown
                    "average_technical_confidence": round(avg_technical, 3),
                    "average_prompt_relevance": round(avg_relevance, 3),
                    "analysis_mode": "context_aware" if context_analysis["has_context"] else "technical_only",
                    
                    # Count statistics
                    "high_confidence_count": sum(1 for c in confidences if c >= 0.7),
                    "medium_confidence_count": sum(1 for c in confidences if 0.4 <= c < 0.7),
                    "low_confidence_count": sum(1 for c in confidences if c < 0.4),
                    
                    # General confidence (main accumulated score)
                    "general_confidence": round(general_confidence, 3),
                    "general_confidence_level": general_confidence_level,
                    
                    # Contributing factors for transparency (context-aware)
                    "confidence_factors": {
                        "weighted_average_contribution": round(weighted_avg_confidence * (0.4 if context_analysis["has_context"] else 0.6), 3),
                        "prompt_relevance_contribution": round(relevance_analysis.get("prompt_relevance_score", 0) * 0.3, 3) if context_analysis["has_context"] else 0.0,
                        "high_confidence_ratio": round(high_conf_ratio, 3),
                        "high_relevance_ratio": round(relevance_analysis.get("high_relevance_count", 0) / len(final_objects), 3) if final_objects and context_analysis["has_context"] else 0.0,
                        "consistency_bonus": round(consistency_bonus, 3)
                    },
                    
                    # Prompt relevance metrics (new)
                    "prompt_relevance_analysis": {
                        "enabled": context_analysis["has_context"],
                        "overall_relevance_score": relevance_analysis.get("prompt_relevance_score", 0.0),
                        "objects_with_matches": relevance_analysis.get("objects_with_matches", 0),
                        "high_relevance_count": relevance_analysis.get("high_relevance_count", 0),
                        "prompt_keywords_found": len(relevance_analysis.get("prompt_keywords", [])),
                        "context_prompts_provided": len(context_analysis.get("all_prompts", []))
                    },
                    
                    # Quality indicators
                    "confidence_distribution": {
                        "excellent": sum(1 for c in confidences if c >= 0.9),
                        "high": sum(1 for c in confidences if 0.7 <= c < 0.9),
                        "medium": sum(1 for c in confidences if 0.4 <= c < 0.7),
                        "low": sum(1 for c in confidences if c < 0.4)
                    }
                }
            else:
                confidence_stats = {
                    "average_confidence": 0.0,
                    "max_confidence": 0.0,
                    "min_confidence": 0.0,
                    "weighted_average": 0.0,
                    "high_confidence_count": 0,
                    "medium_confidence_count": 0,
                    "low_confidence_count": 0,
                    "general_confidence": 0.0,
                    "general_confidence_level": "none",
                    "confidence_factors": {
                        "weighted_average_contribution": 0.0,
                        "high_confidence_ratio": 0.0,
                        "object_count_factor": 0.0,
                        "consistency_bonus": 0.0
                    },
                    "confidence_distribution": {
                        "excellent": 0,
                        "high": 0,
                        "medium": 0,
                        "low": 0
                    }
                }
            
            # Generate AI-powered recommendations based on final objects
            recommendations = generate_animation_recommendations(final_objects, llama_analysis)
            
            print(f"✅ Context-aware dual AI vision analysis completed in {total_processing_time:.2f}s")
            print(f"🎯 Found {len(final_objects)} objects with avg confidence: {confidence_stats['average_confidence']:.2f}")
            print(f"📊 General confidence: {confidence_stats['general_confidence']:.3f} ({confidence_stats['general_confidence_level']})")
            
            if context_analysis["has_context"]:
                print(f"🔗 Prompt relevance: {confidence_stats['prompt_relevance_analysis']['overall_relevance_score']:.2f}")
                print(f"🎯 Objects matching prompts: {confidence_stats['prompt_relevance_analysis']['objects_with_matches']}/{len(final_objects)}")
            
            print(f"💡 Quality distribution: {confidence_stats['confidence_distribution']['excellent']} excellent, {confidence_stats['confidence_distribution']['high']} high, {confidence_stats['confidence_distribution']['medium']} medium")
            
            # Save comprehensive results to database
            update_job_status(
                job_id=job_id,
                status="completed",
                progress=100,
                message=f"Context-aware dual AI vision analysis complete: {len(final_objects)} objects detected",
                result={
                    "job_type": "context_aware_dual_vision_analysis",
                    "analysis_metadata": analysis_metadata,
                    "detected_objects": final_objects,
                    "context_analysis": context_analysis,
                    "prompt_relevance_analysis": relevance_analysis,
                    "scene_analysis": llama_analysis,
                    "confidence_statistics": confidence_stats,
                    "animation_recommendations": recommendations,
                    "image_dimensions": {"width": image_width, "height": image_height},
                    "rag_generated_s3_url": s3_image_url,
                    "input_image_s3_url": s3_image_url,
                    "image_source_url": image_source_url,
                    "analysis_timestamp": time.time()
                }
            )
            
            print(f"💾 Comprehensive analysis saved to job: {job_id}")
            
            return {
                "success": True,
                "job_id": job_id,
                
                # Core analysis results
                "analysis_metadata": analysis_metadata,
                "image_dimensions": {"width": image_width, "height": image_height},
                "detected_objects": final_objects,  # Context-aware objects with prompt relevance
                "scene_analysis": llama_analysis,
                
                # Confidence and quality metrics
                "confidence_statistics": confidence_stats,
                "general_confidence": confidence_stats.get("general_confidence", 0.0),
                "general_confidence_level": confidence_stats.get("general_confidence_level", "none"),
                "prompt_relevance_confidence": confidence_stats.get("prompt_relevance_confidence", 0.0),
                
                # Context-aware features
                "prompt_context": {
                    "user_prompt": user_prompt or "none",
                    "enhanced_prompt": enhanced_prompt or "none", 
                    "context_prompts": context_prompts,
                    "relevance_analysis": prompt_relevance_scores
                },
                
                # Animation insights
                "animation_recommendations": recommendations,
                
                # Performance data
                "processing_details": {
                    "total_time": total_processing_time,
                    "models_performance": {
                        "yolo": {"time": yolo_processing_time, "detections": len(yolo_detections)},
                        "llama": {"time": llama_processing_time, "detections": len(llama_objects)}
                    }
                },
                
                # Guidance and tips
                "usage_tips": [
                    f"General confidence: {confidence_stats.get('general_confidence', 0.0):.2f} ({confidence_stats.get('general_confidence_level', 'none')})",
                    "Use 'converged_confidence' for individual object reliability",
                    "Objects with confidence >= 0.7 are highly reliable for animation",
                    "General confidence combines all objects with area and consistency weighting",
                    "Bounding boxes provide precise pixel coordinates for visual overlays",
                    "Check 'animation_reasoning' for detailed animation guidance"
                ],
                
                # Storage URLs
                "rag_generated_s3_url": s3_image_url,
                "image_source_url": image_source_url
            }
            
            if not yolo_results:
                raise HTTPException(
                    status_code=503,
                    detail="YOLOv8 not available. Please install: pip install ultralytics"
                )
            
            print(f"✅ YOLOv8 detected {len(yolo_results['detections'])} objects with accurate bounding boxes")
            
            # Step 3: Convert YOLO detections to our format
            print(f"📦 Formatting detection results...")
            
            object_details = []
            
            # Process each YOLO detection
            for detection in yolo_results['detections']:
                # Map YOLO class to our category
                category = map_yolo_class_to_category(detection['name'])
                
                # Determine animation potential based on category
                if category in ["characters", "animals", "flying_vehicles", "ground_vehicles", "water_vehicles"]:
                    animation_potential = "high"
                elif category in ["objects", "nature", "furniture"]:
                    animation_potential = "medium"
                else:
                    animation_potential = "low"
                
                # Create object entry
                object_entry = {
                    "name": detection['name'],
                    "category": category,
                    "confidence": "high" if detection['confidence_score'] >= 0.5 else "medium",
                    "confidence_score": detection['confidence_score'],
                    "description": f"Detected with YOLOv8: {detection['name']}",
                    "animation_potential": animation_potential,
                    "suggested_animations": get_animation_suggestions(category, detection['name']),
                    "bounding_box": detection['bounding_box'],
                    "position": detection['position'],
                    "size": detection['size'],
                    "detection_method": "yolo"
                }
                
                object_details.append(object_entry)
                bbox = detection['bounding_box']
                print(f"   ✓ {detection['name']} at {detection['position']}, bbox: {bbox['x']},{bbox['y']},{bbox['width']}x{bbox['height']}")
            
            # Sort by confidence score (highest first)
            object_details.sort(key=lambda x: -x["confidence_score"])
            
            # Step 4: Generate intelligent recommendations based on detected objects
            recommendations = []
            
            # Count high-confidence objects
            high_confidence_count = sum(1 for obj in object_details if obj["confidence"] == "high")
            
            # Category-specific recommendations with NEW categories
            characters = [obj for obj in object_details if obj["category"] == "characters"]
            character_actions = [obj for obj in object_details if obj["category"] == "character_actions"]
            animals = [obj for obj in object_details if obj["category"] == "animals"]
                        # Specific roles/types
            
            # Count high-confidence objects
            high_confidence_count = sum(1 for obj in object_details if obj["confidence"] == "high")
            
            # Category-specific recommendations with NEW categories
            characters = [obj for obj in object_details if obj["category"] == "characters"]
            character_actions = [obj for obj in object_details if obj["category"] == "character_actions"]
            animals = [obj for obj in object_details if obj["category"] == "animals"]
            flying_vehicles = [obj for obj in object_details if obj["category"] == "flying_vehicles"]
            ground_vehicles = [obj for obj in object_details if obj["category"] == "ground_vehicles"]
            water_vehicles = [obj for obj in object_details if obj["category"] == "water_vehicles"]
            floating_objects = [obj for obj in object_details if obj["category"] == "floating_objects"]
            nature = [obj for obj in object_details if obj["category"] == "nature"]
            
            if characters:
                char_names = ", ".join([obj["name"] for obj in characters[:3]])
                recommendations.append(f"✓ {len(characters)} character(s) detected ({char_names}) - suitable for walking, jumping, waving, fighting, or action animations")
            
            if character_actions:
                action_names = ", ".join([obj["name"] for obj in character_actions[:3]])
                recommendations.append(f"✓ {len(character_actions)} character action(s) detected ({action_names}) - can be animated or transitioned")
            
            if animals:
                animal_names = ", ".join([obj["name"] for obj in animals[:3]])
                recommendations.append(f"✓ {len(animals)} animal(s) detected ({animal_names}) - suitable for movement, running, flying, or swimming animations")
            
            if flying_vehicles:
                flying_names = ", ".join([obj["name"] for obj in flying_vehicles[:3]])
                recommendations.append(f"🚀 {len(flying_vehicles)} flying vehicle(s) detected ({flying_names}) - suitable for flying, soaring, hovering, or aerial maneuvers")
            
            if ground_vehicles:
                ground_names = ", ".join([obj["name"] for obj in ground_vehicles[:3]])
                recommendations.append(f"🚗 {len(ground_vehicles)} ground vehicle(s) detected ({ground_names}) - suitable for driving, racing, drifting, or moving")
            
            if water_vehicles:
                water_names = ", ".join([obj["name"] for obj in water_vehicles[:3]])
                recommendations.append(f"⛵ {len(water_vehicles)} water vehicle(s) detected ({water_names}) - suitable for sailing, cruising, or moving through water")
            
            if floating_objects:
                float_names = ", ".join([obj["name"] for obj in floating_objects[:3]])
                recommendations.append(f"✨ {len(floating_objects)} floating object(s) detected ({float_names}) - suitable for floating, drifting, or hovering animations")
            
            if nature:
                recommendations.append(f"🌿 {len(nature)} nature element(s) detected - suitable for swaying, blowing, or environmental animations")
            
            # Confidence-based recommendations
            if high_confidence_count == 0 and len(object_details) > 0:
                recommendations.append("⚠️ No high-confidence objects detected - results may be less accurate")
            elif high_confidence_count > 0:
                recommendations.append(f"✓ {high_confidence_count} high-confidence object(s) detected for reliable animation")
            
            # Scene complexity recommendations
            if len(object_details) > 5:
                recommendations.append("💡 Complex scene detected - use the 'asset' parameter in /animate/smart to animate specific objects")
            elif len(object_details) == 1:
                recommendations.append("💡 Simple scene - perfect for focused animation of the single detected object")
            
            if not object_details:
                recommendations.append("❌ No specific animatable objects detected - the image may not contain clear animatable elements")
            
            print(f"✅ Found {len(object_details)} animatable objects ({high_confidence_count} high-confidence)")
            
            # Save to jobs database like RAG jobs do
            update_job_status(
                job_id=job_id,
                status="completed",
                progress=100,
                message=f"Vision analysis complete: Found {len(object_details)} animatable objects",
                result={
                    "job_type": "vision_analysis",
                    "animatable_objects": object_details,
                    "object_count": len(object_details),
                    "high_confidence_count": high_confidence_count,
                    "recommendations": recommendations,
                    "image_dimensions": {
                        "width": image_width,
                        "height": image_height
                    },
                    # Store image URLs like RAG jobs
                    "rag_generated_s3_url": s3_image_url,  # Main image for preview
                    "input_image_s3_url": s3_image_url,  # Alternative field name
                    "image_source_url": image_source_url,  # Original URL if provided
                    "analysis_timestamp": time.time()
                }
            )
            
            print(f"💾 Vision analysis saved to job: {job_id}")
            
            return {
                "success": True,
                "job_id": job_id,  # Include job_id so client can reference it
                "image_dimensions": {
                    "width": image_width,
                    "height": image_height
                },
                "animatable_objects": object_details,
                "object_count": len(object_details),
                "high_confidence_count": high_confidence_count,
                "recommendations": recommendations,
                "message": f"Found {len(object_details)} animatable objects in the image ({high_confidence_count} high-confidence)",
                "usage_tip": "Use bounding_box coordinates to display visual indicators on the image. Each object includes x, y (top-left corner), width, and height in pixels.",
                # Include image URLs for client preview (like RAG jobs)
                "rag_generated_s3_url": s3_image_url,
                "image_source_url": image_source_url
            }
                
        finally:
            # Clean up temporary file
            if temp_image_path and os.path.exists(temp_image_path):
                try:
                    os.unlink(temp_image_path)
                    print(f"🧹 Cleaned up temporary file")
                except Exception as cleanup_error:
                    print(f"⚠️ Failed to cleanup temp file: {cleanup_error}")
        
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Vision analysis error: {str(e)}"
        print(f"❌ {error_msg}")
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

def get_animation_suggestions(category: str, object_name: str) -> list:
    """Generate animation suggestions based on object category and name."""
    
    suggestions = {
        "characters": ["walking", "running", "jumping", "waving", "dancing", "turning head", "gesturing", "fighting", "climbing"],
        "character_actions": ["continuing the action", "transitioning to new action", "smooth movement", "dynamic pose"],
        "animals": ["walking", "running", "jumping", "moving", "tail wagging", "head turning", "prowling", "pouncing"],
        "flying_vehicles": ["flying forward", "soaring", "banking left/right", "ascending", "descending", "hovering", "taking off", "landing", "barrel roll"],
        "ground_vehicles": ["driving forward", "accelerating", "turning", "drifting", "wheels spinning", "moving", "racing", "braking"],
        "water_vehicles": ["sailing", "cruising", "rocking with waves", "moving through water", "turning", "speeding", "diving (submarine)"],
        "floating_objects": ["floating gently", "drifting", "bobbing", "rising", "descending", "swirling", "hovering", "glowing"],
        "nature": ["swaying", "blowing in wind", "growing", "rustling", "flowing", "waving", "blooming"],
        "furniture": ["door opening", "drawer sliding", "chair rocking", "subtle movement"],
        "body_parts": ["moving", "gesturing", "blinking", "smiling", "turning", "waving"],
        "clothing": ["flowing", "fluttering", "waving", "moving with wind", "billowing"],
        "objects": ["rotating", "moving", "floating", "bouncing", "spinning", "rolling"],
        "environment": ["door opening", "window opening", "lights flickering", "subtle movement", "swaying"]
    }
    
    return suggestions.get(category, ["subtle movement", "floating", "gentle motion"])

@app.post("/animate/extract")
async def extract_objects_for_3d(
    file: Optional[UploadFile] = File(None, description="Image file with objects to extract"),
    image: Optional[UploadFile] = File(None, description="Alternative: Image file with objects to extract"),
    image_url: Optional[str] = Form(None, description="URL of image (optional if file provided)"),
    objects: str = Form(..., description="JSON array of objects to extract with bounding boxes")
):
    """🎯 Extract & Optimize: Create 3D-optimized image from selected objects
    
    This endpoint takes an image and selected objects, then creates a **professional 3D-optimized version**
    using Gemini AI - the same process used in prompt-based generation. This regenerates the image with:
    
    **🎨 3D Optimization Features:**
    1. **White background removal** - Clean white background for easy extraction
    2. **Optimal camera angle** - Side-angled/top-degree viewpoint for 3D reconstruction
    3. **PBR-ready textures** - High-fidelity materials suitable for 3D modeling
    4. **Photogrammetry-friendly geometry** - Clear surface definition
    5. **Professional lighting** - Even, neutral lighting for consistent textures
    6. **Clean edges** - Minimal post-processing required
    
    **The Process:**
    1. Receives your image and selected objects with bounding boxes
    2. Creates comprehensive 3D optimization prompt focusing on selected objects
    3. **Regenerates image using Gemini AI** with reference to your original
    4. Applies professional 3D modeling standards:
       - Removes background → pure white
       - Adjusts angle → optimal for 3D reconstruction
       - Enhances textures → PBR-ready materials
       - Optimizes lighting → even, neutral illumination
    5. Uploads both original and 3D-optimized versions to S3
    
    **This REGENERATES the image** - creating a new version optimized specifically for
    3D modeling, rigging, and animation. Same professional process as `/animate/prompt`.
    
    Args:
        file or image: Image file with objects to extract (provide either file/image or image_url)
        image_url: URL of image to optimize (provide either file/image or image_url)
        objects: JSON array of objects to extract, e.g.:
                 '[{"name":"teddy bear","bounding_box":{"x":470,"y":109,"width":260,"height":338}}]'
    
    Returns:
        3D-optimized image with:
        - White background
        - Optimal viewing angle for 3D reconstruction  
        - PBR-ready textures
        - Job ID and S3 URLs for both original and optimized versions
    
    Example:
    ```bash
    curl -X POST "http://localhost:8000/animate/extract" \\
      -F "file=@image.jpg" \\
      -F 'objects=[{"name":"person","bounding_box":{"x":100,"y":200,"width":300,"height":400}}]'
    ```
    """
    
    job_id = str(uuid.uuid4())
    print(f"\n{'='*80}")
    print(f"🎯 EXTRACT & OPTIMIZE FOR 3D - Job ID: {job_id}")
    print(f"{'='*80}")
    
    try:
        # Accept either 'file' or 'image' parameter
        upload_file = file or image
        
        # Validate input
        if not upload_file and not image_url:
            raise HTTPException(
                status_code=400,
                detail="Either 'file'/'image' or 'image_url' must be provided"
            )
        
        # Parse objects JSON
        try:
            objects_list = json.loads(objects)
            if not isinstance(objects_list, list) or len(objects_list) == 0:
                raise ValueError("Objects must be a non-empty array")
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid objects JSON: {str(e)}"
            )
        
        print(f"📦 Extracting {len(objects_list)} object(s):")
        for obj in objects_list:
            print(f"   • {obj.get('name', 'Unknown')}")
        
        # Get the image
        temp_image_path = None
        image_source_url = None
        s3_input_url = None
        
        try:
            if upload_file:
                print(f"📤 Processing uploaded file: {upload_file.filename}")
                
                # Validate file type
                if not upload_file.content_type.startswith('image/'):
                    raise HTTPException(status_code=400, detail="File must be an image")
                
                # Read file content
                file_content = await upload_file.read()
                
                # Create temporary file
                file_ext = os.path.splitext(upload_file.filename)[1] if upload_file.filename else ".jpg"
                temp_fd, temp_image_path = tempfile.mkstemp(suffix=file_ext)
                
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    temp_file.write(file_content)
                
                print(f"✅ Image saved to temporary file")
                
                # Upload to S3
                print(f"☁️ Uploading input image to S3...")
                s3_input_url = upload_asset_to_s3(temp_image_path, "extract_optimize", job_id, f"input_{upload_file.filename}")
                image_source_url = s3_input_url
                print(f"✅ Input S3 URL: {s3_input_url}")
                
            else:
                print(f"🔗 Using provided image URL: {image_url[:50]}...")
                image_source_url = image_url
                
                # Download image
                response = requests.get(image_url, timeout=30)
                response.raise_for_status()
                
                # Determine file extension
                content_type = response.headers.get('content-type', '')
                file_ext = '.jpg'
                if 'png' in content_type:
                    file_ext = '.png'
                elif 'webp' in content_type:
                    file_ext = '.webp'
                
                temp_fd, temp_image_path = tempfile.mkstemp(suffix=file_ext)
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    temp_file.write(response.content)
                
                print(f"✅ Image downloaded to temporary file")
                
                # Upload to S3
                print(f"☁️ Uploading input image to S3...")
                s3_input_url = upload_asset_to_s3(temp_image_path, "extract_optimize", job_id, f"input_from_url{file_ext}")
                print(f"✅ Input S3 URL: {s3_input_url}")
            
            # Initialize job status
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=20,
                message="Creating 3D-optimized image...",
                result={
                    "job_type": "extract_optimize",
                    "input_image_url": s3_input_url,
                    "objects_to_extract": objects_list
                }
            )
            
            # Build optimization prompt based on selected objects
            object_names = [obj.get('name', 'object') for obj in objects_list]
            object_descriptions = ", ".join(object_names)
            
            optimization_prompt = f"""Create a 3D-optimized version of this image focusing on: {object_descriptions}.

Requirements:
- Keep the selected objects ({object_descriptions}) clear and well-defined
- Enhance lighting and contrast for 3D animation
- Maintain clean edges and details
- Optimize for animation: clear silhouettes, good depth perception
- Keep background simple but contextual
- Ensure good color separation between objects and background

Output a clean, 3D-animation-ready version of this image."""
            
            print(f"🎨 Optimization prompt: {optimization_prompt[:150]}...")
            
            # Check if Gemini is available
            if not (USE_GEMINI and GEMINI_API_KEY):
                raise HTTPException(
                    status_code=503,
                    detail="Gemini image generation not available. Please set GEMINI_API_KEY and USE_GEMINI=1"
                )
            
            # Generate 3D-optimized image using Gemini (same as prompt generation)
            # This regenerates the image with: white background, optimal angle, 3D-ready
            print(f"🎨 Generating 3D-optimized image with Gemini...")
            
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=50,
                message="Generating 3D-optimized image with Gemini...",
                result={
                    "job_type": "extract_optimize",
                    "input_image_url": s3_input_url,
                    "objects_to_extract": objects_list,
                    "optimization_prompt": optimization_prompt
                }
            )
            
            # Use mapapi's Gemini image generation for true 3D optimization
            try:
                if not mapapi:
                    raise RuntimeError("mapapi module not available for 3D optimization")
                
                # Read input image file extension
                file_ext = os.path.splitext(temp_image_path)[1][1:].lower()
                
                print(f"🔧 Using Gemini to create 3D-optimized version...")
                
                # Create optimized version path
                optimized_path = temp_image_path.replace(f".{file_ext}", f"_optimized.jpg")
                
                # Get RETEXTURE_PROMPT from environment (same as prompt generation)
                retexture_prompt = os.getenv("RETEXTURE_PROMPT", 
                    "preserve and enhance the existing textures, maintain original colors and patterns, high quality PBR materials")
                
                # Create comprehensive 3D-focused prompt emphasizing exact design preservation
                combined_3d_prompt = (
                    f"Using the reference image provided, create a 3D-optimized version of EXACTLY these objects: {object_descriptions}. "
                    f"CRITICAL: Keep the EXACT design, appearance, colors, textures, patterns, and details from the reference image. "
                    f"DO NOT change the visual design - only optimize for 3D modeling by: "
                    f"1. Removing background and replacing with pure white "
                    f"2. Adjusting to side-angled/top-degree viewpoint (slightly elevated side view) for optimal 3D reconstruction "
                    f"3. Ensuring clean, high-resolution PBR-ready textures while maintaining original look "
                    f"4. Enhancing lighting to be even and neutral without changing colors or materials "
                    f"5. Maintaining photogrammetry-friendly geometry with clear surface definition. "
                    f"The result must look EXACTLY like the reference image but optimized for 3D: same design, same colors, same textures, same style - just with white background and better angle for 3D modeling. "
                    f"Preserve all visual characteristics with maximum fidelity. Output suitable for 3D modeling, rigging, and animation."
                )
                
                print(f"🎯 3D optimization prompt: {combined_3d_prompt[:150]}...")
                
                # Temporarily override prompt for 3D optimization (same as prompt generation)
                original_prompt = os.environ.get("GEMINI_PROMPT_OVERRIDE")
                os.environ["GEMINI_PROMPT_OVERRIDE"] = combined_3d_prompt
                
                try:
                    # Use mapapi's Gemini image generation with reference image
                    print(f"🎨 Calling Gemini to generate 3D-optimized image...")
                    optimized_result = mapapi.generate_image_with_gemini(
                        objects_description=object_descriptions,
                        reference_image_path=temp_image_path,  # Use uploaded image as reference
                        output_path=optimized_path
                    )
                    
                    # Handle return format
                    if isinstance(optimized_result, tuple):
                        final_optimized_path, content_type = optimized_result
                    else:
                        final_optimized_path = optimized_result
                    
                    if not final_optimized_path or not os.path.exists(final_optimized_path):
                        raise RuntimeError("Failed to generate 3D-optimized image with Gemini")
                    
                    optimized_path = final_optimized_path
                    print(f"✅ 3D-optimized image generated with white background and optimal angle")
                    
                finally:
                    # Restore original prompt
                    if original_prompt:
                        os.environ["GEMINI_PROMPT_OVERRIDE"] = original_prompt
                    elif "GEMINI_PROMPT_OVERRIDE" in os.environ:
                        del os.environ["GEMINI_PROMPT_OVERRIDE"]
                    
            except Exception as e:
                error_msg = f"Gemini 3D optimization failed: {str(e)}"
                print(f"❌ {error_msg}")
                import traceback
                traceback.print_exc()
                raise HTTPException(status_code=500, detail=error_msg)
            
            # Upload optimized image to S3
            print(f"☁️ Uploading optimized image to S3...")
            s3_optimized_url = upload_asset_to_s3(optimized_path, "extract_optimize", job_id, f"optimized.{file_ext}")
            print(f"✅ Optimized S3 URL: {s3_optimized_url}")
            
            # Update job status to completed
            update_job_status(
                job_id=job_id,
                status="completed",
                progress=100,
                message="3D-optimized image created successfully!",
                result={
                    "job_type": "extract_optimize",
                    "input_image_url": s3_input_url,
                    "rag_generated_s3_url": s3_input_url,  # For preview compatibility
                    "rag_optimized_s3_url": s3_optimized_url,  # Main optimized image
                    "optimized_image_s3_url": s3_optimized_url,
                    "objects_extracted": objects_list,
                    "optimization_prompt": optimization_prompt
                }
            )
            
            print(f"✅ Extract & Optimize completed!")
            
            return {
                "success": True,
                "job_id": job_id,
                "input_image_url": s3_input_url,
                "optimized_image_url": s3_optimized_url,
                "rag_generated_s3_url": s3_input_url,  # For preview
                "rag_optimized_s3_url": s3_optimized_url,  # For preview
                "objects_extracted": objects_list,
                "message": f"Successfully created 3D-optimized image with {len(objects_list)} object(s)",
                "next_steps": "Use this optimized image for animation with /animate/luma or /animate/smart"
            }
            
        finally:
            # Clean up temporary files
            if temp_image_path and os.path.exists(temp_image_path):
                try:
                    os.unlink(temp_image_path)
                    optimized_path = temp_image_path.replace(file_ext, f"_optimized{file_ext}")
                    if os.path.exists(optimized_path):
                        os.unlink(optimized_path)
                    print(f"🧹 Cleaned up temporary files")
                except Exception as cleanup_error:
                    print(f"⚠️ Failed to cleanup temp files: {cleanup_error}")
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Extract & optimize error: {str(e)}"
        print(f"❌ {error_msg}")
        
        # Update job status if job_id exists
        if 'job_id' in locals():
            update_job_status(
                job_id=job_id,
                status="failed",
                progress=0,
                message=error_msg,
                result={
                    "job_type": "extract_optimize",
                    "error": str(e)
                }
            )
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

@app.post("/animate/design")
async def animate_design_with_luma_ray(
    file: Optional[UploadFile] = File(None, description="Image file to animate (optional - if not provided, will generate from prompt)"),
    prompt: str = "keep the same style; make him jump like there is no tomorrow",
    size: str = "1280*720",
    duration: str = "5",
    also_make_gif: bool = False
):
    """Animate an uploaded image using Luma Ray 2 I2V technology - Database-only storage
    
    This endpoint provides high-quality image-to-video animation using Luma Ray 2 I2V technology.
    Upload an image and provide animation instructions to create realistic video animations.
    If no image is provided, an image will be generated from the prompt first.
    All assets are stored directly in database/S3 without local folders.
    
    Args:
        file: Image file to animate (optional - if not provided, will generate from prompt)
        prompt: Animation prompt describing the motion/action (default: "keep the same style; make him jump like there is no tomorrow")
        size: Video resolution in format "width*height" (default: "1280*720")
        duration: Video duration in seconds (default: "5")
        also_make_gif: Also create a GIF version of the animation (default: False)
    
    Returns:
        Animation result with S3 URLs and database metadata
    """
    
    try:
        # Generate job ID
        job_id = str(uuid.uuid4())
        
        input_image_s3_url = None
        
        # Handle image input - either uploaded or generated
        if file:
            # Validate file type
            if not file.content_type.startswith('image/'):
                raise HTTPException(status_code=400, detail="File must be an image")
            
            print(f"🎬 Luma Ray 2 I2V animation request for job {job_id}")
            print(f"📁 Input image: {file.filename}")
            
            # Upload directly to S3 without local storage
            try:
                # Read file content
                file_content = await file.read()
                
                # Create temporary file for S3 upload
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_file:
                    temp_file.write(file_content)
                    temp_image_path = temp_file.name
                
                # Upload to S3
                timestamp = int(time.time())
                s3_filename = f"{job_id}_{timestamp}_input_image.jpg"
                s3_key = f"assets/{job_id}/input/{s3_filename}"
                
                try:
                    # Initialize S3 client and upload
                    s3_client = boto3.client("s3", region_name="us-west-1")
                    
                    s3_client.upload_file(
                        temp_image_path,
                        S3_BUCKET,
                        s3_key,
                        ExtraArgs={"ContentType": file.content_type}
                    )
                    
                    # Generate S3 URL
                    input_image_s3_url = f"https://{S3_BUCKET}.s3.us-west-1.amazonaws.com/{s3_key}"
                    
                    print(f"✅ Input image uploaded to S3: {input_image_s3_url}")
                    
                except Exception as s3_error:
                    raise Exception(f"Failed to upload input image to S3: {s3_error}")
                finally:
                    # Clean up temporary file
                    try:
                        os.unlink(temp_image_path)
                    except:
                        pass
                
            except Exception as upload_error:
                raise HTTPException(status_code=500, detail=f"Failed to process uploaded image: {upload_error}")
            
        else:
            # No image provided - generate one from prompt and upload to S3
            print(f"🎬 Luma Ray 2 I2V animation request for job {job_id} (generating image from prompt)")
            print(f"🎨 Generating image from prompt: {prompt}")
            
            # Initialize job status for image generation
            update_job_status(job_id, "processing", 5, "Generating image from prompt...", {
                "job_type": "animate",
                "animation_type": "luma_ray_design",
                "input_mode": "generated_from_prompt",
                "animation_prompt": prompt,
                "video_size": size,
                "duration": duration,
                "create_gif": also_make_gif,
                "storage_type": "database_s3_only"
            })
            
            try:
                # Generate image using mapapi (database-only approach)
                loop = asyncio.get_event_loop()
                generated_image_s3_url = await loop.run_in_executor(
                    thread_pool, 
                    generate_image_from_prompt_and_upload_to_s3, 
                    job_id, 
                    prompt
                )
                
                if not generated_image_s3_url:
                    raise Exception("Failed to generate and upload image from prompt")
                
                input_image_s3_url = generated_image_s3_url
                print(f"✅ Image generated and uploaded to S3: {input_image_s3_url}")
                
            except Exception as e:
                error_msg = f"Failed to generate image from prompt: {str(e)}"
                print(f"❌ {error_msg}")
                
                update_job_status(job_id, "failed", 0, error_msg, {
                    "job_type": "animate",
                    "animation_type": "luma_ray_design",
                    "storage_type": "database_s3_only",
                    "error": error_msg
                })
                
                raise HTTPException(status_code=500, detail=error_msg)
        
        print(f"📝 Prompt: {prompt}")
        print(f"📐 Size: {size}")
        print(f"⏱️ Duration: {duration}s")
        print(f"🖼️ Create GIF: {also_make_gif}")
        
        # Initialize/update job status with S3 URL
        update_job_status(job_id, "processing", 15, "Starting Luma Ray 2 I2V animation...", {
            "job_type": "animate",
            "animation_type": "luma_ray_design",
            "input_file": file.filename if file else "generated_from_prompt",
            "input_image_s3_url": input_image_s3_url,
            "animation_prompt": prompt,
            "video_size": size,
            "duration": duration,
            "create_gif": also_make_gif,
            "storage_type": "database_s3_only"
        })
        
        # Create request object
        luma_request = LumaRayRequest(
            prompt=prompt,
            size=size,
            duration=duration,
            also_make_gif=also_make_gif
        )
        
        # Update progress
        update_job_status(job_id, "processing", 20, "Submitting animation to Luma Ray 2 API...")
        
        # Process animation with database-only storage
        result = await luma_ray_animate_image_database_only(input_image_s3_url, luma_request, job_id)
        
        if result.get("success"):
            # Update job status with success - all S3 URLs stored in database
            update_job_status(job_id, "completed", 100, "Luma Ray 2 I2V animation completed successfully!", {
                "job_type": "animate",
                "animation_type": "luma_ray_design",
                "video_s3_url": result["video_s3_url"],
                "api_job_id": result["api_job_id"],
                "processing_time": result["processing_time"],
                "gif_s3_url": result.get("gif_s3_url"),
                "input_image_s3_url": input_image_s3_url,
                "animation_config": {
                    "prompt": prompt,
                    "size": size,
                    "duration": duration,
                    "create_gif": also_make_gif
                },
                "storage_type": "database_s3_only"
            })
            
            response_data = {
                "success": True,
                "job_id": job_id,
                "video_s3_url": result["video_s3_url"],
                "api_job_id": result["api_job_id"],
                "processing_time": result["processing_time"],
                "metadata": {
                    "prompt": result["prompt"],
                    "size": result["size"],
                    "duration": result["duration"],
                    "original_image": file.filename if file else "generated_from_prompt",
                    "status": "completed",
                    "storage_type": "database_s3_only"
                },
                "message": f"Image animated successfully in {result['processing_time']:.2f} seconds"
            }
            
            # Add S3 URLs
            if result.get("gif_s3_url"):
                response_data["gif_s3_url"] = result["gif_s3_url"]
                response_data["message"] += " (with GIF)"
            
            response_data["input_image_s3_url"] = input_image_s3_url
            
            # Add error info if GIF creation failed
            if result.get("gif_error"):
                response_data["gif_error"] = result["gif_error"]
            
            return response_data
        else:
            # Animation failed
            error_msg = result.get("error", "Unknown animation error")
            status_code = 500 if result.get("status") == "error" else 408 if result.get("status") == "timeout" else 422
            
            # Update job status with failure
            update_job_status(job_id, "failed", 0, f"Animation failed: {error_msg}", {
                "job_type": "animate",
                "animation_type": "luma_ray_design",
                "error": error_msg,
                "api_job_id": result.get("api_job_id"),
                "input_image_s3_url": input_image_s3_url,
                "animation_config": {
                    "prompt": prompt,
                    "size": size,
                    "duration": duration
                },
                "storage_type": "database_s3_only"
            })
            
            return JSONResponse(
                status_code=status_code,
                content={
                    "success": False,
                    "job_id": job_id,
                    "error": error_msg,
                    "api_job_id": result.get("api_job_id"),
                    "metadata": {
                        "prompt": result["prompt"],
                        "original_image": file.filename if file else "generated_from_prompt",
                        "status": result.get("status", "unknown"),
                        "storage_type": "database_s3_only"
                    },
                    "message": f"Animation failed: {error_msg}"
                }
            )
        
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Internal server error during Luma Ray animation: {str(e)}"
        print(f"❌ {error_msg}")
        
        # Update job status with error if job_id exists
        if 'job_id' in locals():
            update_job_status(job_id, "failed", 0, error_msg, {
                "job_type": "animate",
                "animation_type": "luma_ray_design",
                "error": str(e),
                "storage_type": "database_s3_only"
            })
        
        raise HTTPException(status_code=500, detail=error_msg)

@app.post("/tools/vision")
async def analyze_for_animation_and_3d(
    file: Optional[UploadFile] = File(None, description="Image file to analyze (optional if image_url provided)"),
    image_url: Optional[str] = Form(None, description="URL of image to analyze (optional if file provided)")
):
    """🔍 3D & Animation Vision Analysis: Deep technical analysis using Llama 3.2 Vision
    
    This endpoint uses Llama 3.2 Vision AI to provide **specialized technical analysis** 
    for animation and 3D modeling workflows. Unlike `/animate/vision` which focuses on 
    object detection, this endpoint provides deep insights into:
    
    **Animation Analysis:**
    - Character pose and rigging potential
    - Joint placement recommendations
    - Movement potential assessment
    - Animation-ready evaluation
    
    **3D Modeling Insights:**
    - Topology recommendations
    - Polygon count estimates
    - Edge flow analysis
    - Geometry optimization suggestions
    
    **Material & Texturing:**
    - PBR material analysis
    - Texture complexity assessment
    - Surface property identification
    - UV unwrapping recommendations
    
    **Lighting & Rendering:**
    - Lighting setup analysis
    - Render engine recommendations
    - Material shader suggestions
    
    **Technical Specifications:**
    - Estimated bone/joint count
    - UV map complexity
    - Suggested subdivision levels
    - FPS recommendations for animation
    
    **Workflow Recommendations:**
    - Best modeling approach
    - Texturing pipeline suggestions
    - Rigging strategy
    - Animation considerations
    
    Args:
        file: Image file to analyze (provide either file or image_url)
        image_url: URL of image to analyze (provide either file or image_url)
    
    Returns:
        Comprehensive technical analysis optimized for 3D and animation workflows
    
    Example:
    ```bash
    curl -X POST "http://localhost:8000/tools/vision" \\
      -F "file=@character.jpg"
    ```
    """
    
    job_id = str(uuid.uuid4())
    print(f"\n{'='*80}")
    print(f"🔍 3D & ANIMATION VISION ANALYSIS - Job ID: {job_id}")
    print(f"{'='*80}")
    
    try:
        # Validate input
        if not file and not image_url:
            raise HTTPException(
                status_code=400,
                detail="Either 'file' or 'image_url' must be provided"
            )
        
        print(f"🔍 Starting specialized 3D/animation analysis...")
        
        # Get the image (either from upload or URL)
        temp_image_path = None
        image_source_url = None
        s3_image_url = None
        
        try:
            if file:
                # Handle uploaded file
                print(f"📤 Processing uploaded file: {file.filename}")
                
                if not file.content_type.startswith('image/'):
                    raise HTTPException(status_code=400, detail="File must be an image")
                
                file_content = await file.read()
                file_ext = os.path.splitext(file.filename)[1] if file.filename else ".jpg"
                temp_fd, temp_image_path = tempfile.mkstemp(suffix=file_ext)
                
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    temp_file.write(file_content)
                
                print(f"✅ Image saved to temporary file")
                
                # Upload to S3
                print(f"☁️ Uploading to S3...")
                s3_image_url = upload_asset_to_s3(temp_image_path, "vision_3d_analysis", job_id, f"input_{file.filename}")
                image_source_url = s3_image_url
                print(f"✅ S3 URL: {s3_image_url}")
                
            else:
                # Use provided URL
                print(f"🔗 Using provided image URL: {image_url[:50]}...")
                image_source_url = image_url
                
                # Download image
                response = requests.get(image_url, timeout=30)
                response.raise_for_status()
                
                content_type = response.headers.get('content-type', '')
                file_ext = '.jpg'
                if 'png' in content_type:
                    file_ext = '.png'
                elif 'webp' in content_type:
                    file_ext = '.webp'
                
                temp_fd, temp_image_path = tempfile.mkstemp(suffix=file_ext)
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    temp_file.write(response.content)
                
                print(f"✅ Image downloaded")
                
                # Upload to S3
                print(f"☁️ Uploading to S3...")
                s3_image_url = upload_asset_to_s3(temp_image_path, "vision_3d_analysis", job_id, f"input_from_url{file_ext}")
                print(f"✅ S3 URL: {s3_image_url}")
            
            # Get image dimensions
            from PIL import Image
            with Image.open(temp_image_path) as img:
                image_width, image_height = img.size
            print(f"📐 Image dimensions: {image_width}x{image_height}")
            
            # Encode image to base64 for Llama Vision
            print(f"🔄 Encoding image for Llama 3.2 Vision...")
            with open(temp_image_path, 'rb') as img_file:
                image_base64 = base64.b64encode(img_file.read()).decode('utf-8')
            
            # Create specialized prompt for 3D/animation analysis
            analysis_prompt = """You are an expert 3D artist and animation technical director. Analyze this image with deep technical knowledge for 3D modeling and animation workflows.

Provide a comprehensive technical analysis in the following JSON format:

{
  "animation_analysis": {
    "pose_description": "detailed description of character pose",
    "rigging_complexity": "simple/moderate/complex",
    "joint_recommendations": ["list of key joints/bones needed"],
    "movement_potential": "description of animation possibilities",
    "animation_ready": "yes/no with reasons"
  },
  "modeling_analysis": {
    "topology_assessment": "description of surface topology and flow",
    "estimated_polygon_count": "low/medium/high with estimate",
    "edge_flow_notes": "critical edge loops and flow patterns",
    "geometry_type": "organic/hard-surface/mixed",
    "subdivision_recommendations": "suggested subdivision levels"
  },
  "material_analysis": {
    "surface_types": ["list of material types detected"],
    "pbr_texture_needs": ["diffuse", "normal", "roughness", etc.],
    "material_complexity": "simple/moderate/complex",
    "shader_recommendations": "suggested shader types"
  },
  "lighting_analysis": {
    "current_lighting": "description of lighting in image",
    "lighting_recommendations": "ideal 3D lighting setup",
    "render_engine_suggestions": ["recommended render engines"]
  },
  "scene_breakdown": {
    "foreground_elements": ["list of foreground objects"],
    "background_elements": ["list of background elements"],
    "composition_notes": "technical composition analysis"
  },
  "technical_specifications": {
    "estimated_bones": "number for rigging",
    "uv_complexity": "simple/moderate/complex",
    "suggested_fps": "recommended animation framerate",
    "scale_reference": "real-world size estimates"
  },
  "workflow_recommendations": {
    "modeling_approach": "recommended modeling workflow",
    "texturing_pipeline": "suggested texturing approach",
    "rigging_strategy": "rigging workflow recommendations",
    "animation_considerations": "key animation workflow notes"
  }
}

Be specific, technical, and actionable. Focus on practical 3D production insights."""

            # Call Ollama Llama 3.2 Vision API
            ollama_url = "http://localhost:11434/api/chat"
            
            payload = {
                "model": "llama3.2-vision",
                "messages": [
                    {
                        "role": "user",
                        "content": analysis_prompt,
                        "images": [image_base64]
                    }
                ],
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": 0.3  # Lower temperature for more technical/consistent output
                }
            }
            
            print(f"🤖 Calling Llama 3.2 Vision API...")
            start_time = time.time()
            
            try:
                response = requests.post(ollama_url, json=payload, timeout=180)
                response.raise_for_status()
            except requests.exceptions.ConnectionError:
                raise HTTPException(
                    status_code=503,
                    detail="Cannot connect to Ollama. Please ensure Ollama is running on localhost:11434"
                )
            except requests.exceptions.Timeout:
                raise HTTPException(
                    status_code=504,
                    detail="Llama Vision analysis timed out. Please try with a smaller image."
                )
            
            processing_time = time.time() - start_time
            
            # Parse response
            try:
                response_data = response.json()
                analysis_text = response_data["message"]["content"]
                
                # Try to parse as JSON
                try:
                    analysis = json.loads(analysis_text)
                except json.JSONDecodeError:
                    # If not valid JSON, return as text
                    analysis = {"raw_analysis": analysis_text}
                    
            except (json.JSONDecodeError, KeyError) as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to parse Llama Vision response: {str(e)}"
                )
            
            print(f"✅ Analysis completed in {processing_time:.2f}s")
            
            # Generate summary recommendations
            recommendations = []
            if "animation_analysis" in analysis:
                anim = analysis["animation_analysis"]
                if anim.get("animation_ready") == "yes":
                    recommendations.append("✓ Image is animation-ready")
                recommendations.append(f"⚙️ Rigging complexity: {anim.get('rigging_complexity', 'unknown')}")
            
            if "modeling_analysis" in analysis:
                model = analysis["modeling_analysis"]
                recommendations.append(f"🎨 Geometry type: {model.get('geometry_type', 'unknown')}")
                recommendations.append(f"📊 Polygon count: {model.get('estimated_polygon_count', 'unknown')}")
            
            if "material_analysis" in analysis:
                mat = analysis["material_analysis"]
                recommendations.append(f"🎭 Material complexity: {mat.get('material_complexity', 'unknown')}")
            
            # Save to database
            update_job_status(
                job_id=job_id,
                status="completed",
                progress=100,
                message="3D/Animation vision analysis completed",
                result={
                    "job_type": "vision_3d_analysis",
                    "analysis": analysis,
                    "recommendations": recommendations,
                    "image_dimensions": {
                        "width": image_width,
                        "height": image_height
                    },
                    "rag_generated_s3_url": s3_image_url,
                    "image_source_url": image_source_url,
                    "processing_time": processing_time,
                    "analysis_timestamp": time.time()
                }
            )
            
            print(f"💾 Analysis saved to job: {job_id}")
            
            # Prepare response
            response_data = {
                "success": True,
                "job_id": job_id,
                "image_dimensions": {
                    "width": image_width,
                    "height": image_height
                },
                "processing_time": processing_time,
                "image_url": s3_image_url,
                "recommendations": recommendations
            }
            
            # Add all analysis sections
            response_data.update(analysis)
            
            return response_data
            
        finally:
            # Clean up temporary file
            if temp_image_path and os.path.exists(temp_image_path):
                try:
                    os.unlink(temp_image_path)
                    print(f"🧹 Cleaned up temporary file")
                except Exception as cleanup_error:
                    print(f"⚠️ Failed to cleanup temp file: {cleanup_error}")
    
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"3D/Animation vision analysis error: {str(e)}"
        print(f"❌ {error_msg}")
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

@app.post("/analyze/documents")
async def analyze_documents_for_design_prompt(
    files: Optional[List[UploadFile]] = File(None, description="Multiple text documents (PDF, TXT, DOC, etc.)"),
    design_context: Optional[str] = Form(None, description="Additional context for design requirements"),
    output_type: Optional[str] = Form("image_generation", description="Output type: 'image_generation', 'enhancement', or 'both'"),
    style_preferences: Optional[str] = Form(None, description="Style preferences (e.g., 'modern', 'minimalist', 'futuristic')"),
    combined_document_content: Optional[str] = Form(None, description="Pre-combined document content (for workflow usage)"),
    document_count: Optional[int] = Form(None, description="Number of documents in combined content")
):
    """📄 Document Analysis & Design Prompt Generator
    
    Analyzes multiple text documents (specifications, requirements, briefs) and generates 
    detailed design prompts optimized for image generation or prompt enhancement.
    
    **Features:**
    - Multi-document analysis (drag & drop support)
    - Learns from specifications and requirements
    - Generates detailed design recommendations
    - Optimized for AI image generation
    - Context-aware prompt creation
    
    **Supported formats:** PDF, TXT, DOC, DOCX, MD, RTF
    """
    
    job_id = str(uuid.uuid4())
    print(f"🆔 Starting document analysis job: {job_id}")
    
    try:
        # Handle both file uploads and pre-combined content
        document_contents = []
        processed_files = []
        
        if combined_document_content:
            # Use pre-combined content (from workflow)
            print(f"📚 Processing pre-combined document content ({document_count or 'unknown'} documents)")
            
            # Try to detect and decode if content is base64 encoded
            decoded_content = combined_document_content
            
            # Check if content looks like base64 or binary
            try:
                import base64
                import re
                
                # If content starts with "DOCUMENT:" and has binary markers, it's likely a DOC file
                if "DOCUMENT:" in combined_document_content and ("\\x" in combined_document_content or "ࡱ" in combined_document_content):
                    print("🔍 Detected binary/encoded content, attempting advanced text extraction...")
                    
                    # Split by document separators
                    doc_sections = combined_document_content.split("=== DOCUMENT SEPARATOR ===")
                    extracted_sections = []
                    
                    for section in doc_sections:
                        if not section.strip():
                            continue
                        
                        # Extract document name
                        doc_name = ""
                        if "DOCUMENT:" in section:
                            doc_name_match = re.search(r'DOCUMENT:\s*([^\n]+)', section)
                            if doc_name_match:
                                doc_name = doc_name_match.group(1).strip()
                        
                        print(f"📄 Extracting text from: {doc_name}")
                        
                        # Try multiple extraction methods
                        extracted_text = ""
                        
                        # Method 1: Extract all printable ASCII text (most reliable for DOC files)
                        # DOC files contain readable text mixed with binary formatting codes
                        printable_text = re.findall(r'[\x20-\x7E]{3,}', section)
                        if printable_text:
                            # Join text fragments with spaces
                            extracted_text = ' '.join(printable_text)
                            # Clean up multiple spaces
                            extracted_text = re.sub(r'\s+', ' ', extracted_text)
                            print(f"   ✅ Method 1 (ASCII extraction): {len(extracted_text)} chars")
                        
                        # Method 2: If still short, try UTF-8 decode with aggressive cleaning
                        if len(extracted_text) < 100:
                            try:
                                # Decode as UTF-8, ignore errors
                                decoded = section.encode('latin-1').decode('utf-8', errors='ignore')
                                # Remove control characters but keep text
                                cleaned = re.sub(r'[\x00-\x1F\x7F-\x9F]', ' ', decoded)
                                cleaned = re.sub(r'\s+', ' ', cleaned)
                                if len(cleaned) > len(extracted_text):
                                    extracted_text = cleaned
                                    print(f"   ✅ Method 2 (UTF-8 decode): {len(extracted_text)} chars")
                            except:
                                pass
                        
                        # Method 3: Look for text between null bytes (DOC file structure)
                        if len(extracted_text) < 100:
                            # Split by null bytes and keep substantial text chunks
                            text_chunks = section.split('\x00')
                            meaningful_chunks = [chunk for chunk in text_chunks if len(chunk) > 10 and re.search(r'[a-zA-Z]{5,}', chunk)]
                            if meaningful_chunks:
                                extracted_text = ' '.join(meaningful_chunks)
                                extracted_text = re.sub(r'[^\x20-\x7E\n]', ' ', extracted_text)
                                extracted_text = re.sub(r'\s+', ' ', extracted_text)
                                print(f"   ✅ Method 3 (Null-byte splitting): {len(extracted_text)} chars")
                        
                        if extracted_text.strip() and len(extracted_text.strip()) > 50:
                            # Add document name header if we found it
                            if doc_name:
                                extracted_sections.append(f"DOCUMENT: {doc_name}\n{extracted_text.strip()}")
                            else:
                                extracted_sections.append(extracted_text.strip())
                            print(f"   📝 Extracted {len(extracted_text.split())} words")
                        else:
                            print(f"   ⚠️ Minimal text extracted, trying base64...")
                            # Try base64 as last resort
                            try:
                                base64_decoded = base64.b64decode(section.encode('utf-8'))
                                text = base64_decoded.decode('utf-8', errors='ignore')
                                text = re.sub(r'[^\x20-\x7E\n]', ' ', text)
                                text = re.sub(r'\s+', ' ', text)
                                if len(text.strip()) > 50:
                                    extracted_sections.append(text.strip())
                                    print(f"   ✅ Base64 decode: {len(text.split())} words")
                            except:
                                pass
                    
                    # Join all extracted sections
                    if extracted_sections:
                        decoded_content = '\n\n=== DOCUMENT SEPARATOR ===\n\n'.join(extracted_sections)
                        print(f"✅ Total extracted content: {len(decoded_content)} chars, {len(decoded_content.split())} words")
                    else:
                        print("⚠️ No text could be extracted from binary content")
                        decoded_content = ""
                
                # If content looks like pure base64 (no DOCUMENT markers)
                elif len(combined_document_content) > 100 and not re.search(r'[a-zA-Z]{20,}', combined_document_content):
                    try:
                        print("🔍 Attempting base64 decode...")
                        base64_decoded = base64.b64decode(combined_document_content)
                        decoded_content = base64_decoded.decode('utf-8', errors='ignore')
                        decoded_content = re.sub(r'[^\x20-\x7E\n]', ' ', decoded_content)
                        decoded_content = re.sub(r'\s+', ' ', decoded_content)
                        print(f"✅ Base64 decoded: {len(decoded_content)} chars, {len(decoded_content.split())} words")
                    except:
                        print("⚠️ Base64 decode failed")
                
            except Exception as e:
                print(f"⚠️ Content decoding attempt failed: {e}, using raw content")
            
            # Validate we got meaningful text
            if decoded_content.strip() and len(decoded_content.split()) > 10:
                document_contents.append({
                    "filename": f"combined_documents_{document_count or 'multiple'}",
                    "content": decoded_content.strip(),
                    "word_count": len(decoded_content.split()),
                    "file_type": ".txt"
                })
                processed_files.append(f"combined_documents_{document_count or 'multiple'}")
                print(f"📝 Processed combined content: {len(decoded_content.split())} words")
                print(f"📋 Content preview: {decoded_content[:200]}...")
                print(f"\n{'='*80}")
                print(f"📄 FULL DECODED DOCUMENT CONTENT:")
                print(f"{'='*80}")
                print(decoded_content)
                print(f"{'='*80}\n")
            else:
                raise HTTPException(status_code=400, detail=f"Could not extract readable text from combined_document_content. Extracted only {len(decoded_content.split())} words.")
            
        elif files and len(files) > 0:
            # Process uploaded files
            if len(files) > 10:
                raise HTTPException(status_code=400, detail="Maximum 10 documents allowed per request")
            
            print(f"📚 Processing {len(files)} uploaded documents...")
            
            for i, file in enumerate(files):
                print(f"📄 Processing document {i+1}: {file.filename}")
                
                # Validate file type
                allowed_extensions = {'.txt', '.md', '.pdf', '.doc', '.docx', '.rtf'}
                file_ext = os.path.splitext(file.filename)[1].lower()
                
                if file_ext not in allowed_extensions:
                    print(f"⚠️ Skipping unsupported file type: {file.filename}")
                    continue
                
                # Read file content
                content = await file.read()
                
                # Extract text based on file type
                try:
                    text_content = ""
                    
                    if file_ext == '.pdf':
                        # PDF text extraction
                        try:
                            import PyPDF2
                            import io
                            pdf_file = io.BytesIO(content)
                            pdf_reader = PyPDF2.PdfReader(pdf_file)
                            text_parts = []
                            for page in pdf_reader.pages:
                                text_parts.append(page.extract_text())
                            text_content = '\n'.join(text_parts)
                            print(f"📄 Extracted PDF with PyPDF2: {len(text_content)} chars")
                        except ImportError:
                            print("⚠️ PyPDF2 not available, using basic extraction")
                            text_content = content.decode('utf-8', errors='ignore')
                        except Exception as e:
                            print(f"⚠️ PDF extraction failed: {e}, using basic extraction")
                            text_content = content.decode('utf-8', errors='ignore')
                    
                    elif file_ext == '.docx':
                        # DOCX text extraction
                        try:
                            import docx
                            import io
                            doc_file = io.BytesIO(content)
                            doc = docx.Document(doc_file)
                            text_parts = [paragraph.text for paragraph in doc.paragraphs]
                            text_content = '\n'.join(text_parts)
                            print(f"📄 Extracted DOCX with python-docx: {len(text_content)} chars")
                        except ImportError:
                            print("⚠️ python-docx not available, using basic extraction")
                            text_content = content.decode('utf-8', errors='ignore')
                        except Exception as e:
                            print(f"⚠️ DOCX extraction failed: {e}, using basic extraction")
                            text_content = content.decode('utf-8', errors='ignore')
                    
                    elif file_ext == '.doc':
                        # Legacy DOC format - harder to parse, try textract or basic extraction
                        try:
                            import textract
                            import io
                            # Save to temp file for textract
                            import tempfile
                            with tempfile.NamedTemporaryFile(suffix='.doc', delete=False) as tmp:
                                tmp.write(content)
                                tmp_path = tmp.name
                            text_content = textract.process(tmp_path).decode('utf-8')
                            os.unlink(tmp_path)
                            print(f"📄 Extracted DOC with textract: {len(text_content)} chars")
                        except ImportError:
                            print("⚠️ textract not available, using basic extraction")
                            # Try to extract readable text from binary
                            import re
                            text_content = content.decode('utf-8', errors='ignore')
                            # Remove binary junk, keep readable text
                            text_content = re.sub(r'[^\x20-\x7E\n]', ' ', text_content)
                            text_content = re.sub(r'\s+', ' ', text_content)
                        except Exception as e:
                            print(f"⚠️ DOC extraction failed: {e}, using basic extraction")
                            import re
                            text_content = content.decode('utf-8', errors='ignore')
                            text_content = re.sub(r'[^\x20-\x7E\n]', ' ', text_content)
                            text_content = re.sub(r'\s+', ' ', text_content)
                    
                    else:
                        # Plain text files (txt, md, rtf)
                        text_content = content.decode('utf-8', errors='ignore')
                    
                    # Clean up the text content
                    text_content = text_content.strip()
                    
                    if text_content and len(text_content) > 50:  # Minimum meaningful content
                        document_contents.append({
                            "filename": file.filename,
                            "content": text_content,
                            "word_count": len(text_content.split()),
                            "file_type": file_ext
                        })
                        processed_files.append(file.filename)
                        print(f"✅ Extracted {len(text_content.split())} words from {file.filename}")
                    else:
                        print(f"⚠️ No meaningful text content found in {file.filename}")
                        
                except Exception as e:
                    print(f"❌ Error processing {file.filename}: {e}")
                    continue
        else:
            raise HTTPException(status_code=400, detail="Either files or combined_document_content is required")
        
        if not document_contents:
            raise HTTPException(status_code=400, detail="No valid text content found in provided documents")
        
        # Combine all document contents for analysis
        combined_text = "\n\n=== DOCUMENT SEPARATOR ===\n\n".join([
            f"DOCUMENT: {doc['filename']}\n{doc['content']}" for doc in document_contents
        ])
        
        total_words = sum(doc['word_count'] for doc in document_contents)
        print(f"📊 Combined analysis: {len(document_contents)} documents, {total_words} total words")
        
        # Get available Ollama models
        print(f"🔍 Checking available Ollama models...")
        ollama_url = "http://localhost:11434"
        
        available_models = []
        try:
            models_response = requests.get(f"{ollama_url}/api/tags", timeout=10)
            if models_response.status_code == 200:
                models_data = models_response.json()
                available_models = [model['name'] for model in models_data.get('models', [])]
                print(f"✅ Found {len(available_models)} available models: {', '.join(available_models[:5])}")
            else:
                print(f"⚠️ Could not fetch models list, using default")
                available_models = ["llama3.2"]
        except Exception as e:
            print(f"⚠️ Error checking models: {e}, using default")
            available_models = ["llama3.2"]
        
        # Select best models for document analysis (prefer larger, more capable models)
        preferred_models = []
        model_priority = [
            "llama3.2", "llama3.2:latest", "llama3.1", "llama3.1:latest",
            "mistral", "mistral:latest", "mixtral", "mixtral:latest",
            "gemma2", "gemma2:latest", "phi3", "phi3:latest"
        ]
        
        for model in model_priority:
            if model in available_models:
                preferred_models.append(model)
            if len(preferred_models) >= 3:  # Use up to 3 models
                break
        
        if not preferred_models:
            preferred_models = available_models[:3] if len(available_models) >= 3 else available_models
        
        if not preferred_models:
            preferred_models = ["llama3.2"]  # Fallback
        
        print(f"🎯 Using {len(preferred_models)} models for analysis: {', '.join(preferred_models)}")
        
        # Analyze with multiple models
        model_analyses = {}
        model_prompts = {}
        
        for model_name in preferred_models:
            print(f"🤖 Analyzing with {model_name}...")
            
            # Three-step process: Summarize → Build Story → Extract Asset Prompt
            analysis_prompt = f"""You are a creative storyteller and asset designer. Your job has THREE STEPS:

STEP 1: SUMMARIZE THE DOCUMENTS
Read and summarize the key points from these documents.

DOCUMENTS:
{combined_text}

STEP 2: BUILD A STORY
Based on the summary, create a compelling story that represents the spec/user stories. Think about:
- Who are the main characters?
- What is their goal or journey?
- What problem are they solving?
- What is the setting/environment?
- What happens in the beginning, middle, and end?

STEP 3: EXTRACT ASSET CREATION PROMPT
From the story, create a detailed prompt for generating visual assets (images, animations, videos) that tell this story.

ADDITIONAL CONTEXT:
- Design Context: {design_context or 'Not specified'}
- Style Preferences: {style_preferences or 'Open to interpretation'}

Respond with JSON:
{{
  "document_summary": "Brief summary of the key points from the documents",
  "story": "The creative story built from the spec/user stories - with beginning, middle, end, characters, setting, and conflict/resolution",
  "asset_prompt": "Detailed prompt for creating visual assets (animation/image/video) that tells this story. Include characters, actions, environment, style, mood, and motion.",
  "story_elements": {{
    "characters": ["character 1", "character 2"],
    "setting": "description of where the story takes place",
    "plot_points": ["event 1", "event 2", "event 3"]
  }},
  "style_keywords": ["visual style", "mood", "aesthetic"]
}}

Remember: Summarize → Build Story → Extract Asset Prompt"""

            start_time = time.time()
            
            try:
                payload = {
                    "model": model_name,
                    "messages": [{"role": "user", "content": analysis_prompt}],
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0.7, "top_p": 0.9}
                }
                
                response = requests.post(f"{ollama_url}/api/chat", json=payload, timeout=300)
                response.raise_for_status()
                
                response_data = response.json()
                analysis_text = response_data["message"]["content"]
                
                # Parse JSON response
                try:
                    model_analysis = json.loads(analysis_text)
                    model_analyses[model_name] = model_analysis
                    model_prompts[model_name] = model_analysis.get("asset_prompt", "")
                    
                    processing_time = time.time() - start_time
                    print(f"✅ {model_name} completed in {processing_time:.1f}s")
                    print(f"   Story: {model_analysis.get('story', '')[:80]}...")
                    print(f"   Asset prompt preview: {model_prompts[model_name][:80]}...")
                    
                except json.JSONDecodeError:
                    print(f"⚠️ {model_name} returned non-JSON, using raw text")
                    model_prompts[model_name] = analysis_text[:500]
                    
            except Exception as e:
                print(f"❌ {model_name} failed: {e}")
                continue
        
        if not model_prompts:
            raise HTTPException(
                status_code=503,
                detail="All models failed to analyze documents. Please ensure Ollama is running."
            )
        
        print(f"📊 Successfully analyzed with {len(model_prompts)}/{len(preferred_models)} models")
        
        # Synthesize best prompt from all model outputs
        print(f"🔀 Synthesizing final story and asset prompt from {len(model_prompts)} model outputs...")
        
        synthesis_prompt = f"""Multiple AI models analyzed the documents, created stories, and generated asset prompts. Combine them into the best final output.

"""
        
        for i, (model, analysis) in enumerate(model_analyses.items(), 1):
            if isinstance(analysis, dict):
                synthesis_prompt += f"\nMODEL {i} ({model}):\n"
                synthesis_prompt += f"Summary: {analysis.get('document_summary', 'N/A')}\n"
                synthesis_prompt += f"Story: {analysis.get('story', 'N/A')}\n"
                synthesis_prompt += f"Asset Prompt: {analysis.get('asset_prompt', 'N/A')}\n"
        
        synthesis_prompt += f"""

ADDITIONAL CONTEXT:
- Design Context: {design_context or 'Not specified'}
- Style Preferences: {style_preferences or 'Not specified'}

Create the BEST combined output by synthesizing all the models' work.

Respond with JSON:
{{
  "final_story": "The best combined story from all models - coherent narrative with beginning, middle, end",
  "final_prompt": "The best combined asset creation prompt - detailed visual description for creating images/animations/videos",
  "alternative_prompt_1": "Alternative variation of the asset prompt with different emphasis",
  "alternative_prompt_2": "Another creative variation of the asset prompt",
  "style_keywords": ["style", "aesthetic", "mood"]
}}

Combine the best story elements and create the ultimate asset creation prompt."""

        # Use the first available model for synthesis
        synthesis_model = preferred_models[0]
        
        try:
            synthesis_payload = {
                "model": synthesis_model,
                "messages": [{"role": "user", "content": synthesis_prompt}],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.5, "top_p": 0.9}  # Lower temp for synthesis
            }
            
            synthesis_response = requests.post(f"{ollama_url}/api/chat", json=synthesis_payload, timeout=180)
            synthesis_response.raise_for_status()
            
            synthesis_data = synthesis_response.json()
            synthesis_text = synthesis_data["message"]["content"]
            
            final_synthesis = json.loads(synthesis_text)
            final_story = final_synthesis.get("final_story", "")
            primary_prompt = final_synthesis.get("final_prompt", "")
            alternative_prompts = [
                final_synthesis.get("alternative_prompt_1", ""),
                final_synthesis.get("alternative_prompt_2", "")
            ]
            alternative_prompts = [p for p in alternative_prompts if p]  # Remove empty
            style_modifiers = final_synthesis.get("style_keywords", [])
            
            print(f"✅ Final synthesis completed")
            print(f"� Final story: {final_story[:100]}...")
            print(f"📝 Final asset prompt: {primary_prompt[:100]}...")
            
        except Exception as e:
            print(f"⚠️ Synthesis failed: {e}, using best single model output")
            # Fallback: use the longest/most detailed prompt
            primary_prompt = max(model_prompts.values(), key=len)
            alternative_prompts = [p for p in model_prompts.values() if p != primary_prompt][:2]
            style_modifiers = []
            final_story = ""
            
            # Extract style keywords and story from model analyses
            for analysis in model_analyses.values():
                if isinstance(analysis, dict):
                    if "style_keywords" in analysis:
                        keywords = analysis["style_keywords"]
                        if isinstance(keywords, list):
                            style_modifiers.extend([str(k) for k in keywords if k])
                    if not final_story and "story" in analysis:
                        final_story = analysis.get("story", "")
                    if isinstance(keywords, list):
                        style_modifiers.extend([str(k) for k in keywords if k])
            style_modifiers = list(dict.fromkeys([s for s in style_modifiers if s]))[:10]  # Unique, max 10
        
        total_processing_time = time.time() - start_time
        
        # Extract story elements and summaries from analyses
        document_summaries = []
        stories = []
        characters_list = []
        settings_list = []
        plot_points_list = []
        
        for analysis in model_analyses.values():
            if isinstance(analysis, dict):
                # Extract document summary
                if "document_summary" in analysis:
                    document_summaries.append(analysis["document_summary"])
                
                # Extract story
                if "story" in analysis:
                    stories.append(analysis["story"])
                
                # Extract story elements
                story_elements = analysis.get("story_elements", {})
                if isinstance(story_elements, dict):
                    chars = story_elements.get("characters", [])
                    if isinstance(chars, list):
                        characters_list.extend([str(c) for c in chars if c])
                    
                    setting = story_elements.get("setting", "")
                    if setting:
                        settings_list.append(str(setting))
                    
                    plots = story_elements.get("plot_points", [])
                    if isinstance(plots, list):
                        plot_points_list.extend([str(p) for p in plots if p])
        
        # Deduplicate
        characters_list = list(dict.fromkeys([c for c in characters_list if c]))[:10]
        settings_list = list(dict.fromkeys([s for s in settings_list if s]))[:5]
        plot_points_list = list(dict.fromkeys([p for p in plot_points_list if p]))[:15]
        
        # Create comprehensive recommendations
        char_summary = ", ".join(characters_list[:3]) if characters_list else "various characters"
        recommendations = [
            f"📚 Analyzed {len(document_contents)} documents ({total_words} words)",
            f"🤖 Used {len(model_prompts)} Ollama models: {', '.join(model_prompts.keys())}",
            f"📖 Built story with characters: {char_summary}",
            f"🎬 Created {len(plot_points_list)} plot points from spec",
            f"⏱️ Total processing: {total_processing_time:.1f}s",
            f"✨ Generated {len(alternative_prompts) + 1} asset prompt variations",
            "🎨 Ready for asset creation - Story and prompts extracted!"
        ]
        
        # Collect all model analyses for complete record
        complete_analysis = {
            "model_outputs": model_analyses,
            "model_prompts": model_prompts,
            "models_used": list(model_prompts.keys()),
            "synthesis_result": {
                "final_story": final_story,
                "primary_asset_prompt": primary_prompt,
                "alternative_prompts": alternative_prompts,
                "style_keywords": style_modifiers
            },
            "story_extraction": {
                "document_summaries": document_summaries,
                "stories": stories,
                "characters": characters_list,
                "settings": settings_list,
                "plot_points": plot_points_list,
                "extracted_from": "multi-model story building from spec documents"
            },
            "document_summary": {
                "total_documents": len(document_contents),
                "total_words": total_words,
                "file_types": list(set(doc['file_type'] for doc in document_contents)),
                "processed_files": processed_files
            }
        }
        
        # Save to database
        update_job_status(
            job_id=job_id,
            status="completed",
            progress=100,
            message=f"Multi-model document analysis completed with {len(model_prompts)} models",
            result={
                "job_type": "document_analysis_multi_model",
                "complete_analysis": complete_analysis,
                "recommendations": recommendations,
                "processed_files": processed_files,
                "document_stats": {
                    "total_documents": len(document_contents),
                    "total_words": total_words,
                    "file_types": list(set(doc['file_type'] for doc in document_contents))
                },
                "output_prompts": {
                    "primary_asset_prompt": primary_prompt,
                    "alternative_prompts": alternative_prompts,
                    "style_modifiers": style_modifiers
                },
                "story_insights": {
                    "final_story": final_story,
                    "document_summaries": document_summaries,
                    "stories": stories,
                    "characters": characters_list,
                    "settings": settings_list,
                    "plot_points": plot_points_list
                },
                "models_used": list(model_prompts.keys()),
                "processing_time": total_processing_time,
                "analysis_timestamp": time.time()
            }
        )
        
        print(f"💾 Multi-model document analysis saved to job: {job_id}")
        
        return {
            "success": True,
            "job_id": job_id,
            
            # PRIMARY OUTPUT - Asset creation prompt ready to use
            "prompt": primary_prompt,  # Main output for workflow nodes
            
            # Alternative outputs
            "primary_asset_prompt": primary_prompt,
            "alternative_prompts": alternative_prompts,
            "style_modifiers": style_modifiers,
            
            # Story-specific: Story built from spec documents
            "final_story": final_story,
            "document_summaries": document_summaries,
            "stories": stories,
            "characters": characters_list,
            "settings": settings_list,
            "plot_points": plot_points_list,
            
            # Multi-model analysis data
            "models_used": list(model_prompts.keys()),
            "model_count": len(model_prompts),
            "complete_analysis": complete_analysis,
            "recommendations": recommendations,
            
            # Document processing info
            "processed_files": processed_files,
            "document_stats": {
                "total_documents": len(document_contents),
                "total_words": total_words,
                "file_types": list(set(doc['file_type'] for doc in document_contents))
            },
            
            # Performance data
            "processing_time": total_processing_time,
            
            # Ready-to-use outputs for asset creation workflows
            "output_data": {
                "prompt": primary_prompt,  # Primary asset creation prompt
                "asset_prompt": primary_prompt,  # Explicit asset field
                "enhanced_prompt": primary_prompt,
                "text": primary_prompt,
                "design_prompt": primary_prompt,
                "style_keywords": style_modifiers,
                "alternative_prompts": alternative_prompts,
                # Story information built from spec
                "final_story": final_story,
                "document_summaries": document_summaries,
                "stories": stories,
                "characters": characters_list,
                "settings": settings_list,
                "plot_points": plot_points_list
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        error_msg = f"Document analysis error: {str(e)}"
        print(f"❌ {error_msg}")
        
        # Update job status with error if job_id exists
        if 'job_id' in locals():
            update_job_status(job_id, "failed", 0, error_msg, {
                "job_type": "document_analysis",
                "error": str(e)
            })
        
        raise HTTPException(
            status_code=500,
            detail=error_msg
        )

@app.post("/retexture")
async def retexture_model(
    request: RetextureRequest
):
    """Retexture a 3D model using Meshy API with optional job-specific folder storage."""
    
    try:
        # Validate the presigned URL
        if not request.presigned_url:
            raise HTTPException(status_code=400, detail="presigned_url is required")
        
        # Use provided prompt or default from environment
        prompt = request.prompt or RETEXTURE_PROMPT
        if not prompt:
            prompt = "preserve and enhance the existing textures, maintain original colors and patterns, high quality PBR materials"
        
        # Generate job_id if not provided
        job_id = request.job_id or str(uuid.uuid4())[:8]
        
        print(f"🎨 Starting retexturing process...")
        print(f"📄 Job ID: {job_id}")
        print(f"🔗 Presigned URL: {request.presigned_url[:50]}...")
        print(f"💬 Prompt: {prompt[:100]}...")
        
        s3 = boto3.client("s3", region_name="us-west-1")
        # url = s3.generate_presigned_url(
        #     "get_object",
        #     Params={"Bucket": "crimblame",
        #             "Key": request.presigned_url},
        #     ExpiresIn=21600
        # )

        # Call the retexture function
        retextured_url = retexture_with_meshy(
            model_url=request.presigned_url,
            prompt=prompt,
            job_id=job_id
        )
        
        if retextured_url and retextured_url != request.presigned_url:
            # Retexturing was successful
            return {
                "success": True,
                "job_id": job_id,
                "original_url": request.presigned_url,
                "retextured_url": retextured_url,
                "prompt_used": prompt,
                "message": "Retexturing completed successfully",
                "retexturing_folder": f"jobs/{job_id}/retexturing" if job_id else "retextured_models"
            }
        else:
            # Retexturing failed or returned original URL
            return {
                "success": False,
                "job_id": job_id,
                "original_url": request.presigned_url,
                "retextured_url": request.presigned_url,  # Fallback to original
                "prompt_used": prompt,
                "message": "Retexturing failed, using original model",
                "error": "Meshy API retexturing was unsuccessful"
            }
            
    except Exception as e:
        error_msg = f"Retexturing API error: {str(e)}"
        print(f"❌ {error_msg}")
        
        return {
            "success": False,
            "job_id": request.job_id or "unknown",
            "original_url": request.presigned_url,
            "retextured_url": request.presigned_url,  # Fallback to original
            "prompt_used": request.prompt or "unknown",
            "message": error_msg,
            "error": str(e)
        }

@app.get("/jobs")
async def list_jobs():
    """List all jobs from database with RAG information included."""
    try:
        # Try to get jobs from database first
        service = get_db_service()
        if service and service.ensure_connection():
            print("📊 Retrieving jobs from database...")
            db_jobs = service.list_jobs(limit=1000)  # Get more jobs
            
            # Convert to the expected format and enhance with RAG information
            jobs_list = []
            rag_stats = {
                "total_rag_jobs": 0,
                "rag_generated": 0,
                "rag_optimized": 0,
                "rag_with_3d_optimization": 0,
                "separate_rag_optimization": 0
            }
            
            for job in db_jobs:
                # Add job_id field if not present (for compatibility)
                if "job_id" not in job and "_id" in job:
                    job["job_id"] = job["_id"]
                
                # Enhance job with RAG metadata
                result = job.get("result", {})
                job_type = result.get("job_type", "")
                
                # Add RAG classification
                is_rag_job = job_type in ["rag_image_generation", "image_generation", "rag_image_optimization"]
                job["is_rag_job"] = is_rag_job
                
                if is_rag_job:
                    rag_stats["total_rag_jobs"] += 1
                    
                    # Add RAG-specific metadata
                    job["rag_metadata"] = {
                        "job_type": job_type,
                        "has_generated_image": bool(result.get("generated_image_s3_url") or result.get("generated_image_base64")),
                        "has_optimized_image": bool(result.get("optimized_3d_image_s3_url") or result.get("optimized_3d_image_base64")),
                        "reference_images_count": len(result.get("reference_images", [])),
                        "k_value": result.get("k", 0),
                        "prompt": result.get("prompt", "")[:100] + "..." if len(result.get("prompt", "")) > 100 else result.get("prompt", ""),
                        "model": result.get("model", "unknown"),
                        "3d_optimization_enabled": result.get("3d_optimization_enabled", False),
                        "3d_optimization_success": result.get("3d_optimization_success", False),
                        "separate_optimization_completed": result.get("separate_optimization_completed", False),
                        "storage_type": result.get("storage_type", "unknown")
                    }
                    
                    # Update statistics
                    if job["rag_metadata"]["has_generated_image"]:
                        rag_stats["rag_generated"] += 1
                    if job["rag_metadata"]["has_optimized_image"]:
                        rag_stats["rag_optimized"] += 1
                    if job["rag_metadata"]["3d_optimization_enabled"]:
                        rag_stats["rag_with_3d_optimization"] += 1
                    if job["rag_metadata"]["separate_optimization_completed"]:
                        rag_stats["separate_rag_optimization"] += 1
                    
                    # Add S3 URLs for easy access
                    if not job.get("images_s3"):
                        job["images_s3"] = {}
                    
                    # Add RAG generated image S3 URL
                    if result.get("generated_image_s3_url"):
                        job["images_s3"]["rag_generated"] = {
                            "s3_url": result["generated_image_s3_url"],
                            "filename": result.get("image_filename", "rag_generated.png"),
                            "prompt": result.get("prompt", ""),
                            "generation_timestamp": result.get("generation_timestamp"),
                            "model": result.get("model", "unknown"),
                            "reference_images": result.get("reference_images", [])
                        }
                    
                    # Add RAG optimized image S3 URL
                    optimized_s3_url = result.get("optimized_image_s3_url") or result.get("optimized_3d_image_s3_url") or result.get("rag_optimized_s3_url")
                    if optimized_s3_url:
                        job["images_s3"]["rag_optimized"] = {
                            "s3_url": optimized_s3_url,
                            "source_s3_url": result.get("generated_image_s3_url"),
                            "filename": result.get("image_filename", f"rag_optimized_{job['job_id']}.jpg"),
                            "optimization_prompt": result.get("optimization_prompt", ""),
                            "optimization_timestamp": result.get("optimization_timestamp"),
                            "separate_optimization": result.get("separate_optimization_completed", False)
                        }
                
                jobs_list.append(job)
            
            print(f"✅ Retrieved {len(jobs_list)} jobs from database ({rag_stats['total_rag_jobs']} RAG jobs)")
            return {
                "jobs": jobs_list,
                "total_count": len(jobs_list),
                "rag_statistics": rag_stats,
                "source": "database",
                "message": f"Retrieved {len(jobs_list)} artifacts from ForgeRealm database ({rag_stats['total_rag_jobs']} RAG jobs)"
            }
        else:
            # Fallback to in-memory storage
            print("💾 Database unavailable, using in-memory storage fallback...")
            memory_jobs = list(jobs.values()) if jobs else []
            
            # Enhance memory jobs with RAG information
            enhanced_jobs = []
            rag_stats = {"total_rag_jobs": 0, "rag_generated": 0, "rag_optimized": 0}
            
            for job in memory_jobs:
                result = job.get("result", {})
                job_type = result.get("job_type", "")
                is_rag_job = job_type in ["rag_image_generation", "image_generation", "rag_image_optimization"]
                job["is_rag_job"] = is_rag_job
                
                if is_rag_job:
                    rag_stats["total_rag_jobs"] += 1
                    if result.get("generated_image_s3_url"):
                        rag_stats["rag_generated"] += 1
                    if result.get("optimized_3d_image_s3_url"):
                        rag_stats["rag_optimized"] += 1
                
                enhanced_jobs.append(job)
            
            return {
                "jobs": enhanced_jobs,
                "total_count": len(enhanced_jobs),
                "rag_statistics": rag_stats,
                "source": "memory_fallback",
                "message": f"Retrieved {len(enhanced_jobs)} artifacts from memory ({rag_stats['total_rag_jobs']} RAG jobs, database unavailable)"
            }
            
    except Exception as e:
        print(f"❌ Error retrieving jobs: {e}")
        # Final fallback to in-memory storage
        memory_jobs = list(jobs.values()) if jobs else []
        
        # Enhance memory jobs with basic RAG information even in error case
        enhanced_jobs = []
        rag_stats = {"total_rag_jobs": 0, "rag_generated": 0, "rag_optimized": 0}
        
        for job in memory_jobs:
            result = job.get("result", {})
            job_type = result.get("job_type", "")
            is_rag_job = job_type in ["rag_image_generation", "image_generation", "rag_image_optimization"]
            job["is_rag_job"] = is_rag_job
            
            if is_rag_job:
                rag_stats["total_rag_jobs"] += 1
                if result.get("generated_image_s3_url"):
                    rag_stats["rag_generated"] += 1
                if result.get("optimized_3d_image_s3_url"):
                    rag_stats["rag_optimized"] += 1
            
            enhanced_jobs.append(job)
        
        return {
            "jobs": enhanced_jobs,
            "total_count": len(enhanced_jobs),
            "rag_statistics": rag_stats,
            "source": "error_fallback",
            "error": str(e),
            "message": f"Retrieved {len(enhanced_jobs)} artifacts from memory due to error ({rag_stats['total_rag_jobs']} RAG jobs)"
        }

@app.get("/jobs/{job_id}")
async def get_job_by_id(job_id: str):
    """Get a specific job by ID from database with fallback to memory."""
    try:
        # Try to get job from database first
        job_data = get_job_status(job_id)
        
        if not job_data:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
        # Enhance with local folder information
        folder_info = scan_job_folder(job_id)
        job_data["local_folder"] = folder_info
        
        # Get base64 encoded images for client display
        images_base64 = get_job_images_base64(job_id)
        job_data["images_base64"] = images_base64
        
        # Add S3 URLs for RAG-generated images if available
        result = job_data.get("result", {})
        if result.get("job_type") in ["rag_image_generation", "image_generation"]:
            s3_url = result.get("generated_image_s3_url")
            if s3_url:
                # Add S3 URL to images data for easy frontend access
                if not job_data.get("images_s3"):
                    job_data["images_s3"] = {}
                job_data["images_s3"]["rag_generated"] = {
                    "s3_url": s3_url,
                    "filename": result.get("image_filename", "rag_generated.png"),
                    "prompt": result.get("prompt", ""),
                    "generation_timestamp": result.get("generation_timestamp"),
                    "model": result.get("model", "unknown"),
                    "reference_images": result.get("reference_images", [])
                }
                print(f"🖼️ Added RAG S3 URL to job data: {s3_url[:50]}...")
        
        # Add S3 URLs for RAG-optimized images if available  
        elif result.get("job_type") == "rag_image_optimization":
            optimized_s3_url = result.get("optimized_image_s3_url")
            source_s3_url = result.get("source_image_s3_url")
            if optimized_s3_url:
                # Add optimized S3 URL to images data for easy frontend access
                if not job_data.get("images_s3"):
                    job_data["images_s3"] = {}
                job_data["images_s3"]["rag_optimized"] = {
                    "s3_url": optimized_s3_url,
                    "source_s3_url": source_s3_url,
                    "filename": result.get("image_filename", "rag_optimized.png"),
                    "prompt": result.get("prompt", ""),
                    "optimization_timestamp": result.get("optimization_timestamp"),
                    "model": result.get("model", "unknown"),
                    "reference_images": result.get("reference_images", [])
                }
                print(f"🎯 Added RAG optimization S3 URL to job data: {optimized_s3_url[:50]}...")
        
        # Add S3 URLs for separate RAG optimization if available
        elif result.get("separate_optimization_completed") and result.get("rag_optimized_s3_url"):
            rag_optimized_s3_url = result.get("rag_optimized_s3_url")
            if rag_optimized_s3_url:
                # Add separate optimization S3 URL to images data
                if not job_data.get("images_s3"):
                    job_data["images_s3"] = {}
                job_data["images_s3"]["rag_optimized"] = {
                    "s3_url": rag_optimized_s3_url,
                    "source_s3_url": result.get("generated_image_s3_url"),
                    "filename": f"rag_optimized_{job_id}.jpg",
                    "optimization_prompt": result.get("optimization_prompt", ""),
                    "optimization_timestamp": result.get("optimization_timestamp"),
                    "separate_optimization": True
                }
                print(f"🎯 Added separate RAG optimization S3 URL to job data: {rag_optimized_s3_url[:50]}...")
        
        # Add workflow information for animate jobs
        if job_data.get("result", {}).get("job_type") == "animate" or job_data.get("result", {}).get("animation_type") == "luma_ray_design":
            job_data["workflow"] = {
                "type": "luma_ray_animation",
                "description": "Image-to-Video Animation using Luma Ray 2 I2V technology",
                "steps": [
                    "Image upload and validation",
                    "Luma Ray 2 I2V API submission", 
                    "Video generation processing",
                    "Video download and local storage",
                    "Optional GIF conversion"
                ],
                "metadata_source": "job_metadata.json",
                "input_image_source": "input_image.jpg from job folder"
            }
        
        # Add summary information
        if folder_info["folder_exists"]:
            job_data["local_assets_summary"] = {
                "models_count": len(folder_info["models"]),
                "images_count": len(folder_info["images"]),
                "animations_count": len(folder_info["animations"]),
                "metadata_files_count": len(folder_info["metadata_files"]),
                "total_files": folder_info["total_files"],
                "total_size_mb": folder_info.get("total_size_mb", 0)
            }
            
            # Add latest model info if available
            if folder_info["models"]:
                latest_model = folder_info["models"][0]
                job_data["latest_model"] = {
                    "name": latest_model["name"],
                    "path": latest_model["path"],
                    "local_path": f"jobs/{job_id}/{latest_model['path']}"
                }
            
            # Add file system paths for easy access
            job_data["file_paths"] = {
                "job_folder": f"jobs/{job_id}",
                "models_folder": f"jobs/{job_id}/models",
                "images_folder": f"jobs/{job_id}/images", 
                "rigged_models_folder": f"jobs/{job_id}/rigged_models",
                "animations_folder": f"jobs/{job_id}/animations",
                "metadata_file": f"jobs/{job_id}/job_metadata.json"
            }
            
            # Add image availability info
            available_images = []
            for image_type, image_data in images_base64.items():
                if image_data is not None:
                    available_images.append(image_type)
            
            job_data["available_images"] = available_images
            job_data["images_loaded"] = len(available_images)
        
        return job_data
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error retrieving job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.post("/jobs")
async def create_job(
    job_type: str = "default",
    status: str = "queued",
    progress: int = 0,
    message: str = "Job created",
    result: dict = None
):
    """Create a new job in the database."""
    try:
        # Generate new job ID
        job_id = str(uuid.uuid4())
        
        # Create job data
        job_data = {
            "job_id": job_id,
            "job_type": job_type,
            "status": status,
            "progress": progress,
            "message": message,
            "result": result or {},
            "created_at": time.time(),
            "updated_at": time.time()
        }
        
        # Create job using database service
        update_job_status(
            job_id=job_id,
            status=status,
            progress=progress,
            message=message,
            result=result
        )
        
        # Create job directory
        job_dir = f"jobs/{job_id}"
        os.makedirs(job_dir, exist_ok=True)
        
        print(f"✅ Created new job: {job_id}")
        
        return {
            "success": True,
            "job_id": job_id,
            "job_data": job_data,
            "message": f"Job {job_id} created successfully"
        }
        
    except Exception as e:
        print(f"❌ Error creating job: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create job: {str(e)}")

@app.put("/jobs/{job_id}")
async def update_job(
    job_id: str,
    status: str = None,
    progress: int = None,
    message: str = None,
    result: dict = None
):
    """Update an existing job in the database."""
    try:
        # Check if job exists
        existing_job = get_job_status(job_id)
        if not existing_job:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
        # Prepare update data - only update provided fields
        update_data = {}
        
        if status is not None:
            update_data["status"] = status
        if progress is not None:
            update_data["progress"] = progress
        if message is not None:
            update_data["message"] = message
        if result is not None:
            # Merge with existing result
            existing_result = existing_job.get("result", {})
            merged_result = existing_result.copy()
            merged_result.update(result)
            update_data["result"] = merged_result
        
        # Update job using database service
        update_job_status(
            job_id=job_id,
            status=update_data.get("status", existing_job.get("status")),
            progress=update_data.get("progress", existing_job.get("progress", 0)),
            message=update_data.get("message", existing_job.get("message", "")),
            result=update_data.get("result", existing_job.get("result"))
        )
        
        # Get updated job data
        updated_job = get_job_status(job_id)
        
        print(f"✅ Updated job: {job_id}")
        
        return {
            "success": True,
            "job_id": job_id,
            "updated_data": update_data,
            "job_data": updated_job,
            "message": f"Job {job_id} updated successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error updating job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update job: {str(e)}")

@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Delete job from database and cleanup files."""
    try:
        # Try to delete from database first
        service = get_db_service()
        job_found = False
        
        if service and service.ensure_connection():
            # Check if job exists in database
            existing_job = service.get_job(job_id)
            if existing_job:
                # Delete from database
                if service.delete_job(job_id):
                    print(f"✅ Job {job_id} deleted from database")
                    job_found = True
                else:
                    print(f"⚠️ Failed to delete job {job_id} from database")
        
        # Also check memory fallback
        if job_id in jobs:
            del jobs[job_id]
            print(f"✅ Job {job_id} removed from memory")
            job_found = True
        
        if not job_found:
            raise HTTPException(status_code=404, detail="Job not found")
        
        # Cleanup job directory regardless of database status
        job_dir = f"jobs/{job_id}"
        directory_cleaned = False
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
            directory_cleaned = True
            print(f"🗂️ Cleaned up job directory: {job_dir}")
        
        return {
            "success": True,
            "message": f"ForgeRealm artifact {job_id} deleted successfully",
            "job_id": job_id,
            "database_deleted": service and service.ensure_connection(),
            "directory_cleaned": directory_cleaned
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error deleting job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete job: {str(e)}")

# Legacy endpoint for backward compatibility
@app.get("/status/{job_id}")
async def get_job_status_endpoint(job_id: str):
    """Legacy endpoint - redirects to /jobs/{job_id} for backward compatibility."""
    return await get_job_by_id(job_id)

@app.post("/test/pipeline")
async def test_pipeline(
    prompt: str = "A modern building with glass windows and brick facade",
    coordinates: str = "40.7589,-73.9851"  # Times Square coordinates
):
    """Test endpoint to validate the complete mapapi integration."""
    
    try:
        test_id = str(uuid.uuid4())[:8]
        print(f"🧪 Testing pipeline with ID: {test_id}")
        
        # Test 1: Prompt-based generation
        if prompt:
            print("📝 Testing prompt-based generation...")
            image_path = await generate_image_from_prompt(test_id, prompt)
            
            if image_path:
                print(f"✅ Prompt generation successful: {image_path}")
            else:
                print("❌ Prompt generation failed")
        
        # Test 2: Coordinate-based generation
        if coordinates and "," in coordinates:
            lat, lng = map(float, coordinates.split(","))
            print(f"🌍 Testing coordinate-based generation: {lat}, {lng}")
            
            model_path = await generate_from_coordinates(test_id, lat, lng)
            
            if model_path:
                print(f"✅ Coordinate generation successful: {model_path}")
                return {
                    "success": True,
                    "test_id": test_id,
                    "prompt_image": image_path if 'image_path' in locals() else None,
                    "coordinate_model": model_path,
                    "message": "Full pipeline test completed successfully"
                }
            else:
                print("❌ Coordinate generation failed")
        
        return {
            "success": False,
            "test_id": test_id,
            "message": "Pipeline test failed - check logs for details"
        }
        
    except Exception as e:
        print(f"❌ Pipeline test error: {e}")
        return {
            "success": False,
            "error": str(e),
            "message": "Pipeline test failed with exception"
        }

@app.post("/test/retexture")
async def test_retexture():
    """Test endpoint to verify retexturing API functionality."""
    
    # This is a mock test - in real usage you would have a valid presigned URL
    test_request = {
        "presigned_url": "https://example.com/test-model.glb",
        "prompt": "enhance textures with realistic materials",
        "job_id": "test_retex_" + str(int(time.time()))
    }
    
    return {
        "message": "Retexturing API test endpoint",
        "usage": {
            "endpoint": "POST /retexture",
            "required_fields": ["presigned_url"],
            "optional_fields": ["prompt", "job_id"],
            "example_request": test_request
        },
        "description": "Call POST /retexture with a valid presigned URL to retextured a 3D model",
        "note": "The retextured model will be saved in jobs/{job_id}/retexturing/ folder"
    }

@app.post("/jobs/{job_id}/confirm")
async def confirm_job_continuation(
    job_id: str,
    request: ConfirmationRequest,
    background_tasks: BackgroundTasks
):
    """Confirm continuation of job processing after waiting_for_confirmation status
    
    This endpoint handles user confirmation to proceed with expensive operations
    like GLB generation after image analysis is complete.
    
    Args:
        job_id: Job identifier from URL path
        request: ConfirmationRequest containing action ("continue" or "cancel") and optional message
        
    Returns:
        Job status and continuation result
    """
    
    try:
        print(f"🎯 Received confirmation request for job {job_id}")
        print(f"📝 Request action: {request.action}")
        print(f"💬 Request message: {request.message}")
        print(f"🗂️ Request job_id: {request.job_id}")
        
        # Validate that the URL job_id matches the request job_id (optional validation)
        if request.job_id != job_id:
            print(f"⚠️ Job ID mismatch: URL={job_id}, Request={request.job_id}")
            # We'll use the URL job_id as the authoritative one
        
        # Validate job exists using database service
        current_job = get_job_status(job_id)
        if not current_job:
            print(f"❌ Job {job_id} not found in database")
            # Get some available jobs for debugging
            all_jobs = get_all_jobs()
            available_jobs = list(all_jobs.keys()) if all_jobs else []
            print(f"📋 Available jobs: {available_jobs[:5]}")  # Show first 5 for debugging
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
        # Validate job is in waiting_for_confirmation status
        current_status = current_job.get("status")
        print(f"📊 Current job status: {current_status}")
        
        if current_status != "waiting_for_confirmation" and current_status != "failed":
            raise HTTPException(
                status_code=400, 
                detail=f"Job {job_id} is not waiting for confirmation. Current status: {current_status}"
            )
        
        # Validate request action
        if request.action not in ["continue", "cancel"]:
            raise HTTPException(status_code=400, detail="Action must be 'continue' or 'cancel'")
        
        print(f"✅ Validation passed for job {job_id}: {request.action}")
        if request.message:
            print(f"💬 Message: {request.message}")
        
        if request.action == "cancel":
            # User cancelled - mark job as cancelled
            update_job_status(job_id, "cancelled", 75, f"Job cancelled by user: {request.message or 'User decided not to proceed'}", {
                "confirmation_action": "cancel",
                "cancellation_reason": request.message or "User cancelled",
                "cancelled_at": time.time()
            })
            
            return {
                "success": True,
                "job_id": job_id,
                "action": "cancelled",
                "status": "cancelled",
                "message": "Job cancelled successfully. No charges incurred.",
                "confirmation_message": request.message
            }
        
        elif request.action == "continue":
            # User confirmed - continue with GLB generation
            print(f"✅ User confirmed GLB generation for job {job_id}")
            
            # Update status to indicate continuation
            update_job_status(job_id, "processing", 76, "User confirmed - continuing with GLB generation...", {
                "confirmation_action": "continue",
                "confirmation_message": request.message or "User approved GLB generation",
                "confirmed_at": time.time()
            })
            
            # Get the current job result data to continue processing
            current_result = current_job.get("result", {})
            
            # Continue with the 3D model generation in background
            background_tasks.add_task(
                continue_glb_generation_after_confirmation,
                job_id,
                current_result
            )
            
            return {
                "success": True,
                "job_id": job_id,
                "action": "continue",
                "status": "processing",
                "message": "GLB generation continued successfully. Processing in background.",
                "confirmation_message": request.message,
                "estimated_completion": "2-5 minutes"
            }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Confirmation endpoint error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error during confirmation: {str(e)}"
        )

@app.post("/design/edit")
async def design_edit_image(request: DesignEditRequest):
    """Design and edit images using Wavespeed AI Gemini 2.5 Flash Image model
    
    This endpoint provides AI-powered image generation and editing using Gemini 2.5 Flash Image
    through the Wavespeed AI API. Submit a text prompt to generate or edit images.
    
    Args:
        request: DesignEditRequest containing prompt and generation options
    
    Returns:
        Generated image URL and metadata, or base64 data if requested
    """
    
    try:
        # Get Wavespeed API key
        API_KEY = os.getenv("WAVESPEED_API_KEY")
        if not API_KEY:
            raise HTTPException(
                status_code=500, 
                detail="WAVESPEED_API_KEY not configured. Please set the environment variable."
            )
        
        print(f"🎨 Design/Edit request received")
        print(f"📝 Original prompt: {request.prompt}")
        print(f"🔧 Format: {request.output_format}, Sync: {request.enable_sync_mode}, Base64: {request.enable_base64_output}")
        
        # Use provided job_id or generate new one
        job_id = request.job_id or str(uuid.uuid4())
        
        # Create job directory if it doesn't exist
        job_dir = f"jobs/{job_id}"
        os.makedirs(job_dir, exist_ok=True)
        
        # Get existing image analysis to maintain character consistency
        character_description = ""
        if request.job_id and request.job_id in jobs:
            # Get analysis from job result
            job_result = jobs[request.job_id].get("result", {})
            objects_analysis = job_result.get("objects_analysis", "")
            
            if objects_analysis:
                character_description = f"Maintain the same character and style as described: {objects_analysis}. "
                print(f"🎭 Using existing character analysis for consistency")
            
            # Also check job metadata for additional analysis
            try:
                job_metadata = load_job_metadata(request.job_id)
                if job_metadata and not character_description:
                    metadata_analysis = job_metadata.get("result", {}).get("objects_analysis", "")
                    if metadata_analysis:
                        character_description = f"Maintain the same character and style as described: {metadata_analysis}. "
                        print(f"🎭 Using character analysis from job metadata")
            except Exception as e:
                print(f"⚠️ Could not load job metadata: {e}")
        
        # Enhance prompt with character consistency and background preservation
        enhanced_prompt = f"{character_description}{request.prompt}. If there is no background or the background is transparent/white, preserve it and do not change the background."
        print(f"📝 Enhanced prompt with character consistency: {enhanced_prompt}")
        
        
        # Submit request to Wavespeed AI
        url = "https://api.wavespeed.ai/api/v3/google/gemini-2.5-flash-image/text-to-image"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        }
        payload = {
            "enable_base64_output": request.enable_base64_output,
            "enable_sync_mode": request.enable_sync_mode,
            "output_format": request.output_format,
            "prompt": enhanced_prompt
        }

        begin = time.time()
        print(f"🚀 Submitting request to Wavespeed AI...")
        
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        if response.status_code != 200:
            print(f"❌ Wavespeed API error: {response.status_code}, {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Wavespeed API error: {response.text}"
            )

        result = response.json()["data"]
        request_id = result["id"]
        print(f"✅ Task submitted successfully. Request ID: {request_id}")

        # Poll for results
        poll_url = f"https://api.wavespeed.ai/api/v3/predictions/{request_id}/result"
        headers = {"Authorization": f"Bearer {API_KEY}"}
        
        print(f"⏳ Polling for results...")
        timeout_start = time.time()
        max_wait_time = request.timeout_seconds or 300  # 5 minutes default
        
        while True:
            # Check timeout
            if time.time() - timeout_start > max_wait_time:
                raise HTTPException(
                    status_code=408,
                    detail=f"Request timeout after {max_wait_time} seconds"
                )
            
            response = requests.get(poll_url, headers=headers)
            if response.status_code != 200:
                print(f"❌ Error polling results: {response.status_code}, {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Error polling results: {response.text}"
                )

            result = response.json()["data"]
            status = result["status"]

            if status == "completed":
                end = time.time()
                processing_time = end - begin
                print(f"✅ Task completed in {processing_time:.2f} seconds.")
                
                # Get the generated image URL or base64 data
                if request.enable_base64_output:
                    image_data = result.get("base64_image")
                    if not image_data:
                        raise HTTPException(
                            status_code=500,
                            detail="Base64 output requested but not provided by API"
                        )
                    
                    # Save base64 image to job folder if job_id provided
                    local_image_path = None
                    if request.job_id:
                        try:
                            # Decode and save base64 image
                            import base64
                            image_bytes = base64.b64decode(image_data)
                            local_image_path = os.path.join(job_dir, f"edited_image.{request.output_format}")
                            
                            with open(local_image_path, "wb") as f:
                                f.write(image_bytes)
                            
                            print(f"💾 Saved edited image to: {local_image_path}")
                            
                            # Update job with edited image info
                            if request.job_id in jobs:
                                current_result = jobs[request.job_id].get("result", {})
                                current_result["edited_image_local"] = local_image_path
                                current_result["edited_image_prompt"] = request.prompt
                                current_result["edited_image_enhanced_prompt"] = enhanced_prompt
                                current_result["edited_image_character_description"] = character_description.strip() if character_description else None
                                jobs[request.job_id]["result"] = current_result
                                jobs[request.job_id]["updated_at"] = time.time()
                                save_job_metadata(request.job_id)
                                print(f"🔄 Updated job {request.job_id} with edited image")
                        except Exception as e:
                            print(f"⚠️ Failed to save image to job folder: {e}")
                    
                    return {
                        "success": True,
                        "job_id": job_id,
                        "request_id": request_id,
                        "processing_time_seconds": processing_time,
                        "output_format": request.output_format,
                        "base64_image": image_data,
                        "prompt": request.prompt,
                        "enhanced_prompt": enhanced_prompt,
                        "character_description_used": character_description.strip() if character_description else None,
                        "local_image_path": local_image_path,
                        "generated_at": time.time()
                    }
                else:
                    image_url = result["outputs"][0] if result.get("outputs") else None
                    if not image_url:
                        raise HTTPException(
                            status_code=500,
                            detail="Image URL not provided by API"
                        )
                    
                    print(f"🎉 Task completed. Image URL: {image_url}")
                    
                    # Download and save image to job folder if job_id provided
                    local_image_path = None
                    if request.job_id:
                        try:
                            # Download image from URL
                            img_response = requests.get(image_url, timeout=30)
                            img_response.raise_for_status()
                            
                            local_image_path = os.path.join(job_dir, f"edited_image.{request.output_format}")
                            
                            with open(local_image_path, "wb") as f:
                                f.write(img_response.content)
                            
                            print(f"💾 Downloaded and saved edited image to: {local_image_path}")
                            
                            # Update job with edited image info
                            if request.job_id in jobs:
                                current_result = jobs[request.job_id].get("result", {})
                                current_result["edited_image_url"] = image_url
                                current_result["edited_image_local"] = local_image_path
                                current_result["edited_image_prompt"] = request.prompt
                                current_result["edited_image_enhanced_prompt"] = enhanced_prompt
                                current_result["edited_image_character_description"] = character_description.strip() if character_description else None
                                jobs[request.job_id]["result"] = current_result
                                jobs[request.job_id]["updated_at"] = time.time()
                                save_job_metadata(request.job_id)
                                print(f"🔄 Updated job {request.job_id} with edited image")
                        except Exception as e:
                            print(f"⚠️ Failed to download and save image: {e}")
                    
                    return {
                        "success": True,
                        "job_id": job_id,
                        "request_id": request_id,
                        "processing_time_seconds": processing_time,
                        "output_format": request.output_format,
                        "image_url": image_url,
                        "local_image_path": local_image_path,
                        "prompt": request.prompt,
                        "enhanced_prompt": enhanced_prompt,
                        "character_description_used": character_description.strip() if character_description else None,
                        "generated_at": time.time()
                    }
                    
            elif status == "failed":
                error_msg = result.get('error', 'Unknown error')
                print(f"❌ Task failed: {error_msg}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Wavespeed AI task failed: {error_msg}"
                )
            else:
                print(f"⏳ Task still processing. Status: {status}")
                # Wait before next poll
                await asyncio.sleep(2)  # Poll every 2 seconds
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Design/Edit endpoint error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )

@app.post("/design/poses")
async def design_poses_character(request: DesignPosesRequest):
    """Generate character poses using Segmind workflow
    
    This endpoint processes a character image through a Segmind workflow that generates
    multiple pose variations of the character.
    
    Args:
        request: DesignPosesRequest containing character image URL and options
    
    Returns:
        Generated poses image URL and metadata
    """
    
    try:
        # Get Segmind API key
        API_KEY = os.getenv("SEGMIND_API_KEY")
        if not API_KEY:
            raise HTTPException(
                status_code=500, 
                detail="SEGMIND_API_KEY not configured. Please set the environment variable."
            )
        
        print(f"🎭 Design/Poses request received")
        print(f"🖼️ Character image: {request.character_image}")
        
        # Use provided job_id or generate new one
        job_id = request.job_id or str(uuid.uuid4())
        
        # Create job directory if it doesn't exist
        job_dir = f"jobs/{job_id}"
        os.makedirs(job_dir, exist_ok=True)
        
        # Submit request to Segmind workflow
        url = "https://api.segmind.com/workflows/6839c278659263e69c7a5683-v3"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": API_KEY,
        }
        payload = {
            "Character_Image": request.character_image
        }

        begin = time.time()
        print(f"🚀 Submitting request to Segmind Poses workflow...")
        
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        if response.status_code != 200:
            print(f"❌ Segmind API error: {response.status_code}, {response.text}")
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Segmind API error: {response.text}"
            )

        result = response.json()
        request_id = result["request_id"]
        poll_url = result["poll_url"]
        status = result["status"]
        
        print(f"✅ Task submitted successfully. Request ID: {request_id}, Status: {status}")

        # Poll for results
        print(f"⏳ Polling for results at: {poll_url}")
        timeout_start = time.time()
        max_wait_time = request.timeout_seconds or 300  # 5 minutes default
        
        while True:
            # Check timeout
            if time.time() - timeout_start > max_wait_time:
                raise HTTPException(
                    status_code=408,
                    detail=f"Request timed out after {max_wait_time} seconds"
                )
            
            # Poll for results
            poll_response = requests.get(poll_url, headers={"x-api-key": API_KEY})
            if poll_response.status_code != 200:
                print(f"❌ Poll request failed: {poll_response.status_code}")
                raise HTTPException(
                    status_code=poll_response.status_code,
                    detail=f"Poll request failed: {poll_response.text}"
                )
            
            poll_result = poll_response.json()
            status = poll_result.get("status", "unknown")
            print(f"📊 Current status: {status}")
            
            if status in ["completed", "COMPLETED"]:
                processing_time = time.time() - begin
                print(f"⏱️ Processing completed in {processing_time:.2f} seconds")
                
                # Get the output image URL - try multiple possible field names
                image_output = (
                    poll_result.get("Image_Ouput") or  # API typo with missing 't'
                    poll_result.get("Image_Output") or  # Correct spelling
                    poll_result.get("output") or
                    poll_result.get("image_output") or
                    poll_result.get("result") or
                    poll_result.get("outputs", {}).get("image") if isinstance(poll_result.get("outputs"), dict) else None
                )
                
                print(f"🔍 Full API response: {json.dumps(poll_result, indent=2)}")
                
                if not image_output:
                    # If no image output found, try to find any URL in the response
                    response_str = json.dumps(poll_result)
                    url_pattern = r'https?://[^\s"\'<>]+\.(?:png|jpg|jpeg|gif|webp)'
                    urls = re.findall(url_pattern, response_str)
                    
                    if urls:
                        image_output = urls[0]
                        print(f"🔍 Found image URL in response: {image_output}")
                    else:
                        print(f"❌ No image output found in API response. Available keys: {list(poll_result.keys())}")
                        raise HTTPException(
                            status_code=500,
                            detail=f"Image output not provided by API. Response keys: {list(poll_result.keys())}"
                        )
                
                print(f"🎉 Task completed. Poses image URL: {image_output}")
                
                # Download and save image to job folder if job_id provided
                local_image_path = None
                poses_s3_url = None
                glb_model_path = None
                glb_s3_url = None
                
                if request.job_id:
                    try:
                        # Download image from URL
                        print(f"📥 Downloading poses image from: {image_output}")
                        img_response = requests.get(image_output, timeout=30)
                        img_response.raise_for_status()
                        
                        local_image_path = os.path.join(job_dir, f"character_poses.png")
                        
                        with open(local_image_path, "wb") as f:
                            f.write(img_response.content)
                        
                        print(f"💾 Downloaded and saved poses image to: {local_image_path}")
                        
                        # Upload poses image to S3 and get presigned URL
                        print(f"📤 Uploading poses image to S3...")
                        poses_s3_url = upload_poses_image_to_s3(request.job_id, local_image_path)
                        
                        # Generate 3D model from poses image
                        print(f"🎯 Generating 3D model from poses image...")
                        glb_model_path = None
                        glb_s3_url = None
                        
                        try:
                            # Generate 3D model using async function
                            glb_model_path = await generate_3d_model_from_image(local_image_path, request.job_id)
                            
                            if glb_model_path and os.path.exists(glb_model_path):
                                print(f"✅ 3D model generated: {glb_model_path}")
                                
                                # Upload GLB to S3
                                print(f"📤 Uploading 3D model GLB to S3...")
                                try:
                                    timestamp = int(time.time())
                                    glb_filename = os.path.basename(glb_model_path)
                                    s3_key = f"assets/{request.job_id}/models/{timestamp}_{glb_filename}"
                                    
                                    # Upload GLB to S3 using the imported function
                                    glb_s3_url = upload_glb_presigned(glb_model_path, S3_BUCKET, s3_key, PRESIGN_TTL)
                                    
                                    if glb_s3_url:
                                        print(f"✅ 3D model uploaded to S3: {glb_s3_url[:50]}...")
                                    else:
                                        print(f"⚠️ Failed to upload 3D model to S3")
                                        
                                except Exception as e:
                                    print(f"⚠️ Error uploading GLB to S3: {e}")
                                    glb_s3_url = None
                            else:
                                print(f"⚠️ 3D model generation failed or file not found")
                                
                        except Exception as e:
                            print(f"⚠️ Error generating 3D model from poses: {e}")
                            glb_model_path = None
                            glb_s3_url = None
                        
                        # Update job with all generated content
                        if poses_s3_url or glb_s3_url:
                            print(f"✅ Updating job metadata with generated content...")
                            
                            # Update job with poses image and 3D model info
                            if request.job_id in jobs:
                                current_result = jobs[request.job_id].get("result", {})
                                current_result["character_poses_url"] = image_output
                                current_result["character_poses_local"] = local_image_path
                                current_result["character_poses_source_image"] = request.character_image
                                
                                # Add poses image S3 URLs
                                if poses_s3_url:
                                    current_result["poses_s3_url"] = poses_s3_url.get("s3_direct_url")
                                    current_result["poses_s3_presigned_url"] = poses_s3_url.get("presigned_url")
                                    current_result["poses_s3_key"] = poses_s3_url.get("s3_key")
                                
                                # Add 3D model info
                                if glb_model_path:
                                    current_result["poses_3d_model_local"] = glb_model_path
                                if glb_s3_url:
                                    current_result["poses_3d_model_s3_url"] = glb_s3_url
                                    current_result["poses_3d_model_generated"] = True
                                else:
                                    current_result["poses_3d_model_generated"] = False
                                
                                jobs[request.job_id]["result"] = current_result
                                jobs[request.job_id]["updated_at"] = time.time()
                                save_job_metadata(request.job_id)
                                print(f"🔄 Updated job {request.job_id} with poses image and 3D model metadata")
                        else:
                            print(f"⚠️ Both S3 uploads failed for poses content")
                            poses_s3_url = None
                            
                    except Exception as e:
                        print(f"⚠️ Failed to download/upload poses image: {e}")
                        # Don't fail the whole request if download/upload fails
                        poses_s3_url = None
                else:
                    print(f"⚠️ No job_id provided, skipping S3 upload")
                
                # Prepare response data with safe access to S3 URLs
                response_data = {
                    "success": True,
                    "job_id": job_id,
                    "request_id": request_id,
                    "processing_time_seconds": processing_time,
                    "image_output": image_output,
                    "local_image_path": local_image_path,
                    "character_image": request.character_image,
                    "generated_at": time.time()
                }
                
                # Add poses image S3 URLs only if upload was successful
                if poses_s3_url:
                    response_data["poses_s3_url"] = poses_s3_url.get("s3_direct_url")
                    response_data["poses_s3_presigned_url"] = poses_s3_url.get("presigned_url")
                    response_data["poses_s3_key"] = poses_s3_url.get("s3_key")
                else:
                    response_data["poses_s3_url"] = None
                    response_data["poses_s3_presigned_url"] = None
                    response_data["poses_s3_key"] = None
                
                # Add 3D model information if generated
                if glb_model_path:
                    response_data["poses_3d_model_local"] = glb_model_path
                    response_data["poses_3d_model_generated"] = True
                    if glb_s3_url:
                        response_data["poses_3d_model_s3_url"] = glb_s3_url
                    else:
                        response_data["poses_3d_model_s3_url"] = None
                else:
                    response_data["poses_3d_model_local"] = None
                    response_data["poses_3d_model_s3_url"] = None
                    response_data["poses_3d_model_generated"] = False
                
                print(f"🎉 Returning successful response with status 200")
                return response_data
                    
            elif status in ["failed", "FAILED"]:
                error_msg = poll_result.get('error', 'Unknown error')
                print(f"❌ Task failed: {error_msg}")
                raise HTTPException(
                    status_code=500,
                    detail=f"Segmind poses workflow failed: {error_msg}"
                )
            elif status == "QUEUED":
                print(f"⏳ Task queued, waiting...")
                await asyncio.sleep(7)  # Wait 3 seconds before next poll
            else:
                print(f"⏳ Task still processing. Status: {status}")
                await asyncio.sleep(5)  # Poll every 2 seconds
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Design/Poses endpoint error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}"
        )

@app.post("/gif/to-spritesheet")
async def gif_to_spritesheet_endpoint(request: GifToSpriteSheetRequest):
    """Convert a GIF to a sprite sheet using database-only storage.
    
    This endpoint takes a GIF URL and converts it to a sprite sheet (horizontal or vertical layout).
    The resulting sprite sheet is uploaded to S3 and tracked in the database.
    """
    try:
        # Generate job ID for tracking
        job_id = request.job_id or str(uuid.uuid4())
        
        print(f"🎬 Starting GIF to sprite sheet conversion: {job_id}")
        print(f"📥 Source GIF: {request.gif_url}")
        print(f"⚙️ Settings: {request.sheet_type}, max {request.max_frames} frames, {request.background_color} background")
        
        # Update job status to processing
        update_job_status(
            job_id=job_id,
            status="processing",
            progress=10,
            message="Converting GIF to sprite sheet...",
            result={
                "job_type": "gif_to_spritesheet",
                "source_gif_url": request.gif_url,
                "sheet_type": request.sheet_type,
                "max_frames": request.max_frames,
                "background_color": request.background_color
            }
        )
        
        # Run conversion in background thread
        def convert_gif():
            try:
                print(f"🔄 Processing GIF conversion for job {job_id}")
                
                # Update progress
                update_job_status(
                    job_id=job_id,
                    status="processing",
                    progress=30,
                    message="Downloading and analyzing GIF..."
                )
                
                # Convert GIF to sprite sheet
                sprite_result = gif_to_spritesheet_database_only(
                    gif_url=request.gif_url,
                    sheet_type=request.sheet_type,
                    max_frames=request.max_frames,
                    background_color=request.background_color
                )
                
                if sprite_result:
                    # Success - update job with results
                    update_job_status(
                        job_id=job_id,
                        status="completed",
                        progress=100,
                        message="Sprite sheet conversion completed successfully!",
                        result={
                            "job_type": "gif_to_spritesheet",
                            "source_gif_url": request.gif_url,
                            "sprite_sheet_url": sprite_result["s3_direct_url"],
                            "sprite_sheet_presigned_url": sprite_result["presigned_url"],
                            "sprite_sheet_s3_key": sprite_result["s3_key"],
                            "sprite_metadata": sprite_result["sprite_metadata"],
                            "upload_timestamp": sprite_result["upload_timestamp"],
                            "conversion_settings": {
                                "sheet_type": request.sheet_type,
                                "max_frames": request.max_frames,
                                "background_color": request.background_color
                            }
                        }
                    )
                    print(f"✅ GIF to sprite sheet conversion completed for job {job_id}")
                else:
                    # Conversion failed
                    update_job_status(
                        job_id=job_id,
                        status="failed",
                        progress=0,
                        message="Failed to convert GIF to sprite sheet. Please check the GIF URL and try again.",
                        result={
                            "job_type": "gif_to_spritesheet",
                            "error": "conversion_failed",
                            "source_gif_url": request.gif_url
                        }
                    )
                    print(f"❌ GIF to sprite sheet conversion failed for job {job_id}")
                    
            except Exception as e:
                print(f"❌ Error in GIF conversion thread for job {job_id}: {e}")
                update_job_status(
                    job_id=job_id,
                    status="failed",
                    progress=0,
                    message=f"Conversion error: {str(e)}",
                    result={
                        "job_type": "gif_to_spritesheet",
                        "error": str(e),
                        "source_gif_url": request.gif_url
                    }
                )
        
        # Submit conversion to thread pool
        thread_pool.submit(convert_gif)
        
        # Return immediate response with job tracking
        return JSONResponse(content={
            "status": "processing",
            "job_id": job_id,
            "message": "GIF to sprite sheet conversion started",
            "progress": 10,
            "conversion_settings": {
                "source_gif_url": request.gif_url,
                "sheet_type": request.sheet_type,
                "max_frames": request.max_frames,
                "background_color": request.background_color
            },
            "polling_url": f"/status/{job_id}",
            "estimated_time": "30-120 seconds depending on GIF size and frame count"
        })
        
    except Exception as e:
        print(f"❌ GIF to sprite sheet endpoint error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start GIF conversion: {str(e)}"
        )

# ========= RAG IMAGE GENERATION ENDPOINT ================================

@app.post("/gen/gemini")
def gen_gemini_rag(
    prompt: str = Form(...),
    k: int = Form(3),
    job_id: str = Form(None),
    use_rag: bool = Form(False),
    reference_image_url: Optional[str] = Form(None),
    reference_image_source: Optional[str] = Form(None),
    use_reference_image: Optional[str] = Form(None)
):
    """
    Generate images using Gemini 2.5 Flash Image with optional RAG reference images.
    
    By default, generates images without RAG. Set use_rag=True to retrieve reference images.
    Requires: USE_GEMINI=1 and GEMINI_API_KEY set.
    
    Args:
        prompt: Text description for image generation
        k: Number of reference images to retrieve (default: 3, only used if use_rag=True)
        job_id: Optional job ID to associate with this generation
        use_rag: Whether to use RAG reference images (default: False)
    """
    # Reload environment variables to ensure fresh values from .env
    USE_GEMINI_CHECK = os.getenv("USE_GEMINI", "0") == "1"
    GEMINI_API_KEY_CHECK = os.getenv("GEMINI_API_KEY")
    
    if not USE_GEMINI_CHECK:
        raise HTTPException(400, "Gemini disabled; set USE_GEMINI=1 and GEMINI_API_KEY in .env file")
    
    if not GEMINI_API_KEY_CHECK:
        raise HTTPException(400, "GEMINI_API_KEY not configured in .env file")
    
    try:
        # Generate job ID if not provided
        if not job_id:
            job_id = str(uuid.uuid4())
        
        print(f"🎨 Starting image generation for job {job_id}")
        print(f"📝 Prompt: {prompt}")
        print(f"🔍 RAG enabled: {use_rag}")

        # Direct reference image from workflow/config
        inline_reference_images = []
        inline_reference_metadata = []
        use_reference_flag = False

        if use_reference_image:
            use_reference_flag = str(use_reference_image).lower() in {"1", "true", "yes"}

        if reference_image_url:
            print(f"🖼️ Direct reference image received (source: {reference_image_source or 'unknown'})")
            try:
                mime_type = "image/png"
                image_bytes = None

                ref_value = reference_image_url.strip()
                if ref_value.startswith("data:") and "base64," in ref_value:
                    header, b64data = ref_value.split(",", 1)
                    try:
                        mime_type = header.split(";")[0].split(":")[1]
                    except Exception:
                        mime_type = "image/png"
                    image_bytes = base64.b64decode(b64data)
                elif ref_value.lower().startswith("http://") or ref_value.lower().startswith("https://"):
                    ref_response = requests.get(ref_value, timeout=30)
                    ref_response.raise_for_status()
                    image_bytes = ref_response.content
                    mime_type = ref_response.headers.get("Content-Type", "image/png")
                else:
                    # Treat as raw base64 without data URI prefix
                    try:
                        image_bytes = base64.b64decode(ref_value)
                    except Exception as decode_err:
                        raise ValueError(f"Unsupported reference image format: {decode_err}")

                if image_bytes:
                    inline_reference_images.append({
                        "mime_type": mime_type or "image/png",
                        "data": image_bytes
                    })
                    inline_reference_metadata.append({
                        "source": reference_image_source or "inline_reference",
                        "size_bytes": len(image_bytes),
                        "mime_type": mime_type or "image/png"
                    })
                    use_reference_flag = True
                    print(f"✅ Loaded inline reference image ({len(image_bytes)} bytes, mime={mime_type})")
            except Exception as ref_error:
                print(f"❌ Failed to process reference image: {ref_error}")
        
        # Only use RAG if explicitly requested
        ref_paths = []
        ref_metadata = []
        
        if use_rag and RAG_DEPENDENCIES_AVAILABLE and FAISS_AVAILABLE:
            print(f"🔍 Attempting to retrieve {k} reference images using RAG...")
            # Load RAG index
            index, metas = load_img_store()
            if index and metas:
                # Embed the prompt and search for similar images
                query_embedding = embed_texts_clip([prompt])
                scores, ids = index.search(query_embedding, k)
                
                # Collect reference image paths
                for i, (score, img_id) in enumerate(zip(scores[0], ids[0])):
                    if img_id < 0 or img_id >= len(metas):
                        continue
                    meta = metas[img_id]
                    ref_paths.append(meta["path"])
                    ref_metadata.append({
                        "path": meta["path"],
                        "score": float(score),
                        "rank": i + 1,
                        "ocr": meta.get("ocr", ""),
                        "size": f"{meta.get('width', 0)}x{meta.get('height', 0)}"
                    })
                
                if ref_paths:
                    print(f"✅ Found {len(ref_paths)} reference images from RAG")
                    for meta in ref_metadata:
                        print(f"   📄 {os.path.basename(meta['path'])} (score: {meta['score']:.3f})")
                else:
                    print("⚠️ No reference images found, proceeding with text-only generation")
            else:
                print("⚠️ RAG index not available, proceeding with text-only generation")
        elif use_rag:
            print("⚠️ RAG requested but dependencies not available, proceeding with text-only generation")
        elif inline_reference_images:
            print("ℹ️  Using direct reference image provided in request")
        else:
            print("ℹ️  RAG not requested, proceeding with text-only generation")
        
        # Create job status
        update_job_status(
            job_id=job_id,
            status="processing", 
            progress=30,
            message="Generating image with Gemini...",
            result={
                "job_type": "reference_image_generation" if inline_reference_images else ("image_generation" if not ref_paths else "rag_image_generation"),
                "prompt": prompt,
                "reference_images": ref_metadata,
                "inline_reference": inline_reference_metadata,
                "k": k,
                "use_reference_image": use_reference_flag,
                "reference_image_source": reference_image_source
            }
        )
        
        # Build Gemini input: text + reference images as bytes
        model = genai.GenerativeModel(GEMINI_MODEL)
        
        # Enhanced prompt based on whether we have reference images
        if ref_paths or inline_reference_images:
            enhanced_prompt = f"""
Generate an image based on this description: {prompt}

Use the provided reference images as inspiration and style guidance. The generated image should:
1. Capture the essence and style of the reference images
2. Incorporate relevant visual elements, colors, and composition patterns
3. Create something new that fits the description while being influenced by the references
4. Maintain high quality and artistic coherence

Description: {prompt}
"""
            print(f"🤖 Calling Gemini with {len(ref_paths)} reference images...")
        else:
            enhanced_prompt = f"""
Generate a high-quality image based on this description: {prompt}

The generated image should:
1. Be visually appealing and well-composed
2. Match the description accurately
3. Have good lighting and artistic quality
4. Be suitable for use as a reference or inspiration

Description: {prompt}
"""
            print(f"🤖 Calling Gemini for text-only image generation...")
        
        parts = [enhanced_prompt]

        # Add reference images from RAG index (if available)
        for ref_path in ref_paths:
            if os.path.exists(ref_path):
                with open(ref_path, "rb") as f:
                    file_ext = os.path.splitext(ref_path)[1][1:].lower()
                    # Map file extensions to proper MIME types
                    if file_ext == 'jpg':
                        mime_type = "image/jpeg"
                    elif file_ext in ['png', 'jpeg', 'gif', 'webp']:
                        mime_type = f"image/{file_ext}"
                    else:
                        mime_type = "image/png"  # Default fallback
                    parts.append({
                        "mime_type": mime_type,
                        "data": f.read()
                    })

        # Add inline reference images coming directly from workflow/config
        for inline_ref in inline_reference_images:
            parts.append({
                "mime_type": inline_ref.get("mime_type", "image/png"),
                "data": inline_ref.get("data")
            })
        
        total_reference_count = len(parts) - 1
        print(f"🤖 Calling Gemini with {total_reference_count} reference image(s)...")
        
        # Generate image with Gemini
        resp = model.generate_content(parts)
        
        # Extract image from response
        image_bytes = None
        if hasattr(resp, 'candidates') and resp.candidates:
            for part in resp.candidates[0].content.parts:
                if hasattr(part, 'inline_data') and part.inline_data:
                    image_bytes = part.inline_data.data
                    break
        
        if not image_bytes:
            # Try alternative response format
            if hasattr(resp, '_result') and hasattr(resp._result, 'candidates'):
                for candidate in resp._result.candidates:
                    if hasattr(candidate, 'content'):
                        for part in candidate.content.parts:
                            if hasattr(part, 'inline_data') and part.inline_data:
                                image_bytes = part.inline_data.data
                                break
        
        if not image_bytes:
            print(f"❌ Gemini response format: {type(resp)}")
            print(f"❌ Available attributes: {dir(resp)}")
            raise HTTPException(500, "Gemini did not return an image")
        
        # Save generated image
        out_dir = "generated"
        os.makedirs(out_dir, exist_ok=True)
        timestamp = int(time.time())
        out_filename = f"gemini_rag_{job_id}_{timestamp}.png"
        out_path = os.path.join(out_dir, out_filename)
        
        with open(out_path, "wb") as f:
            f.write(image_bytes)
        
        print(f"✅ Generated image saved: {out_path}")
        
        # Update progress for 3D optimization step
        update_job_status(
            job_id=job_id,
            status="processing", 
            progress=60,
            message="Optimizing generated image for 3D model creation...",
            result={
                "job_type": "rag_image_generation",
                "prompt": prompt,
                "generated_image_path": out_path,
                "generated_image_base64": base64.b64encode(image_bytes).decode('utf-8'),
                "reference_images": ref_metadata,
                "k": k,
                "stage": "3d_optimization"
            }
        )
        
        # 3D Optimize the generated image using mapapi - DATABASE ONLY
        optimized_3d_path = None
        optimized_3d_s3_url = None
        optimized_3d_base64 = None
        objects_analysis = None
        
        try:
            if mapapi:
                print(f"🎯 Starting 3D optimization of RAG-generated image (database-only)...")
                
                # Create temporary file for analysis (will be deleted)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                    temp_file.write(image_bytes)
                    temp_input_path = temp_file.name
                
                try:
                    # Analyze the image to get objects description for optimization
                    objects_analysis = mapapi.analyze_image_with_llama(temp_input_path)
                    print(f"📋 Image analysis for 3D optimization: {objects_analysis[:100]}...")
                    
                    # Create temporary output file for optimization
                    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_output:
                        temp_optimized_path = temp_output.name
                    
                    # Submit to thread pool for 3D optimization
                    # Prepare RAG reference images (use top-k if available)
                    refs_to_use = []
                    if ref_paths:
                        use_k = min(int(k) if k else len(ref_paths), len(ref_paths))
                        refs_to_use = ref_paths[:use_k]
                    
                    if refs_to_use:
                        print(f"🔗 Using {len(refs_to_use)} RAG reference image(s) for 3D optimization")
                        reference_arg = refs_to_use[0] if len(refs_to_use) == 1 else refs_to_use
                    else:
                        reference_arg = temp_input_path  # Use the generated image as reference
                    
                    future = thread_pool.submit(
                        mapapi.generate_image_with_gemini,
                        objects_analysis,
                        reference_arg,
                        temp_optimized_path
                    )
                    optimized_result = future.result(timeout=180)  # 3 minute timeout
                    
                    if optimized_result and optimized_result[0] and os.path.exists(optimized_result[0]):
                        # Read optimized image and convert to base64
                        with open(optimized_result[0], "rb") as opt_file:
                            optimized_image_bytes = opt_file.read()
                            optimized_3d_base64 = base64.b64encode(optimized_image_bytes).decode('utf-8')
                        
                        print(f"✅ 3D optimization completed - stored in database")
                        
                        # Upload optimized image to S3
                        try:
                            optimized_filename = f"3d_optimized_{job_id}_{timestamp}.jpg"
                            optimized_3d_s3_url = upload_asset_to_s3(
                                optimized_result[0], 
                                "optimized_3d_image", 
                                job_id, 
                                optimized_filename
                            )
                            if optimized_3d_s3_url:
                                print(f"✅ 3D optimized image uploaded to S3")
                        except Exception as s3_error:
                            print(f"⚠️ Failed to upload 3D optimized image to S3: {s3_error}")
                        
                        # Clean up temporary files
                        try:
                            os.unlink(optimized_result[0])
                        except:
                            pass
                    else:
                        print(f"⚠️ 3D optimization did not produce a valid result")
                        
                finally:
                    # Clean up temporary files
                    try:
                        os.unlink(temp_input_path)
                        if 'temp_optimized_path' in locals() and os.path.exists(temp_optimized_path):
                            os.unlink(temp_optimized_path)
                    except:
                        pass
            else:
                print(f"⚠️ mapapi not available - skipping 3D optimization")
                
        except Exception as opt_error:
            print(f"❌ Error during 3D optimization: {opt_error}")
            # Continue without optimization rather than failing the entire request
        
        # Upload to S3 if available
        s3_url = None
        try:
            s3_result = upload_asset_to_s3(out_path, "generated_image", job_id, out_filename)
            if s3_result:
                s3_url = s3_result
                print(f"✅ Generated image uploaded to S3")
        except Exception as e:
            print(f"⚠️ Failed to upload to S3: {e}")
        
        # Update job status with completion
        result = {
            "job_type": "rag_image_generation",
            "prompt": prompt,
            "generated_image_path": out_path,
            "generated_image_s3_url": s3_url,
            "generated_image_base64": base64.b64encode(image_bytes).decode('utf-8'),
            "optimized_3d_image_path": None,  # Not using local paths for database-only
            "optimized_3d_image_s3_url": optimized_3d_s3_url,
            "optimized_3d_image_base64": optimized_3d_base64,
            "objects_analysis": objects_analysis,
            "reference_images": ref_metadata,
            "k": k,
            "generation_timestamp": timestamp,
            "model": GEMINI_MODEL,
            "3d_optimization_enabled": True,
            "3d_optimization_success": optimized_3d_base64 is not None,
            "storage_type": "database_only"
        }
        
        # Set completion message based on 3D optimization success
        completion_message = "RAG-based image generation completed successfully!"
        if optimized_3d_path:
            completion_message += " Image optimized for 3D model creation."
        else:
            completion_message += " 3D optimization was attempted but may not have completed."
        
        update_job_status(
            job_id=job_id,
            status="completed",
            progress=100,
            message=completion_message,
            result=result
        )
        
        return JSONResponse(content={
            "status": "completed",
            "job_id": job_id,
            "prompt": prompt,
            "generated_image_path": out_path,
            "generated_image_s3_url": s3_url,
            "optimized_3d_image_s3_url": optimized_3d_s3_url,
            "reference_images": ref_metadata,
            "image_filename": out_filename,
            "3d_optimization_enabled": True,
            "3d_optimization_success": optimized_3d_base64 is not None,
            "storage_type": "database_only",
            "message": f"Generated image using {len(ref_paths)} RAG references with 3D optimization (database-only storage)"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ RAG image generation error: {e}")
        if 'job_id' in locals():
            update_job_status(
                job_id=job_id,
                status="failed",
                progress=0,
                message=f"RAG generation failed: {str(e)}",
                result={
                    "job_type": "rag_image_generation",
                    "error": str(e),
                    "prompt": prompt
                }
            )
        raise HTTPException(500, f"RAG image generation failed: {str(e)}")

# ========= RAG OPTIMIZATION ENDPOINT ================================

@app.post("/gen/rag-optimize")
def rag_optimize_image(
    job_id: str = Form(...),
    optimization_prompt: str = Form("3D model ready optimization")
):
    """
    Optimize a RAG-generated image for 3D model creation.
    This endpoint takes a job_id from a previous RAG generation and performs 3D optimization.
    
    Args:
        job_id: Job ID from previous RAG generation
        optimization_prompt: Additional prompt for optimization guidance
    """
    try:
        # Get the existing job data
        if job_id not in jobs:
            raise HTTPException(404, f"Job {job_id} not found")
        
        job_data = jobs[job_id]
        
        # Validate job has generated image
        if job_data.get('status') != 'completed':
            raise HTTPException(400, f"Job {job_id} is not completed")
        
        result = job_data.get('result', {})
        if not result.get('generated_image_base64'):
            raise HTTPException(400, f"Job {job_id} does not have a generated image")
        
        # Check if already optimized
        if result.get('optimized_3d_image_base64'):
            print(f"⚠️ Job {job_id} already has 3D optimization, proceeding with re-optimization")
        
        print(f"🎯 Starting separate 3D optimization for job {job_id}")
        
        # Update job status for optimization
        update_job_status(
            job_id=job_id,
            status="processing", 
            progress=50,
            message="Performing 3D optimization...",
            result={
                **result,
                "stage": "separate_3d_optimization",
                "optimization_prompt": optimization_prompt
            }
        )
        
        # Get the generated image data
        generated_image_base64 = result['generated_image_base64']
        image_bytes = base64.b64decode(generated_image_base64)
        
        # 3D Optimize using database-only approach
        optimized_3d_s3_url = None
        optimized_3d_base64 = None
        objects_analysis = None
        
        if not mapapi:
            raise HTTPException(500, "mapapi not available for 3D optimization")
        
        print(f"🎯 Performing 3D optimization (database-only)...")
        
        # Create temporary file for analysis (will be deleted)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
            temp_file.write(image_bytes)
            temp_input_path = temp_file.name
        
        try:
            # Analyze the image to get objects description for optimization
            analysis_prompt = f"{optimization_prompt}. Describe objects and elements in this image suitable for 3D modeling."
            objects_analysis = mapapi.analyze_image_with_llama(temp_input_path)
            print(f"📋 Image analysis for separate 3D optimization: {objects_analysis[:100]}...")
            
            # Create temporary output file for optimization
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_output:
                temp_optimized_path = temp_output.name
            
            # Use reference images from original RAG generation if available
            ref_paths = []
            ref_metadata = result.get('reference_images', [])
            if ref_metadata:
                # Note: In database-only mode, we don't have direct access to ref paths
                # So we'll use the generated image as reference
                reference_arg = temp_input_path
                print(f"🔗 Using generated image as reference for optimization")
            else:
                reference_arg = temp_input_path
            
            # Submit to thread pool for 3D optimization
            future = thread_pool.submit(
                mapapi.generate_image_with_gemini,
                objects_analysis,
                reference_arg,
                temp_optimized_path
            )
            optimized_result = future.result(timeout=180)  # 3 minute timeout
            
            if optimized_result and optimized_result[0] and os.path.exists(optimized_result[0]):
                # Read optimized image and convert to base64
                with open(optimized_result[0], "rb") as opt_file:
                    optimized_image_bytes = opt_file.read()
                    optimized_3d_base64 = base64.b64encode(optimized_image_bytes).decode('utf-8')
                
                print(f"✅ Separate 3D optimization completed - stored in database")
                
                # Upload optimized image to S3
                try:
                    timestamp = int(time.time())
                    optimized_filename = f"rag_optimized_{job_id}_{timestamp}.jpg"
                    optimized_3d_s3_url = upload_asset_to_s3(
                        optimized_result[0], 
                        "optimized_3d_image", 
                        job_id, 
                        optimized_filename
                    )
                    if optimized_3d_s3_url:
                        print(f"✅ RAG optimized image uploaded to S3")
                except Exception as s3_error:
                    print(f"⚠️ Failed to upload RAG optimized image to S3: {s3_error}")
                
                # Clean up temporary files
                try:
                    os.unlink(optimized_result[0])
                except:
                    pass
            else:
                raise HTTPException(500, "3D optimization did not produce a valid result")
                
        finally:
            # Clean up temporary files
            try:
                os.unlink(temp_input_path)
                if 'temp_optimized_path' in locals() and os.path.exists(temp_optimized_path):
                    os.unlink(temp_optimized_path)
            except:
                pass
        
        # Update job result with optimization data
        updated_result = {
            **result,
            "optimized_3d_image_base64": optimized_3d_base64,
            "rag_optimized_s3_url": optimized_3d_s3_url,
            "objects_analysis": objects_analysis,
            "optimization_prompt": optimization_prompt,
            "separate_optimization_completed": True,
            "optimization_timestamp": int(time.time()),
            "storage_type": "database_only"
        }
        
        # Update job status with completion
        update_job_status(
            job_id=job_id,
            status="completed",
            progress=100,
            message="Separate 3D optimization completed successfully!",
            result=updated_result
        )
        
        return JSONResponse(content={
            "status": "completed",
            "job_id": job_id,
            "optimization_prompt": optimization_prompt,
            "rag_optimized_s3_url": optimized_3d_s3_url,
            "separate_optimization_success": True,
            "storage_type": "database_only",
            "message": "RAG image optimized for 3D model creation using database-only storage"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ RAG optimization error: {e}")
        if 'job_id' in locals():
            try:
                # Update job status to show optimization failed but keep original generation
                current_result = jobs.get(job_id, {}).get('result', {})
                update_job_status(
                    job_id=job_id,
                    status="completed",  # Keep as completed since generation worked
                    progress=100,
                    message=f"Original generation completed, but 3D optimization failed: {str(e)}",
                    result={
                        **current_result,
                        "optimization_error": str(e),
                        "separate_optimization_completed": False
                    }
                )
            except:
                pass
        raise HTTPException(500, f"RAG optimization failed: {str(e)}")

# ========= STANDALONE 3D OPTIMIZATION ENDPOINT ================================

@app.post("/optimize/3d")
async def optimize_for_3d_generation(
    file: Optional[UploadFile] = File(None, description="Image file to optimize (optional if image_url provided)"),
    image_url: Optional[str] = Form(None, description="URL of image to optimize (optional if file provided)"),
    prompt: Optional[str] = Form(None, description="Custom optimization prompt (optional, default: analyze image content)"),
    job_id: Optional[str] = Form(None, description="Optional job ID for tracking")
):
    """🎯 3D Optimization: Transform any image into a 3D-model-ready format
    
    This endpoint takes any image (Gemini-generated, uploaded, or from URL) and optimizes it
    specifically for 3D model generation. It:
    
    1. Analyzes the image content with AI vision
    2. Applies 3D-specific optimizations:
       - White/neutral background removal
       - Optimal camera angle for 3D reconstruction
       - PBR-ready textures and materials
       - Enhanced geometry clarity
       - Professional lighting setup
    3. Generates a new image optimized for 3D workflows
    4. Stores in database and uploads to S3
    
    **Use Cases:**
    - Prepare Gemini-generated images for 3D model creation
    - Optimize photos for photogrammetry workflows
    - Convert concept art into 3D-ready references
    - Clean up backgrounds and improve texture quality
    - Adjust lighting and angles for better 3D reconstruction
    
    Args:
        file: Image file to optimize (provide either file or image_url)
        image_url: URL of image to optimize (provide either file or image_url)
        prompt: Custom optimization instructions (optional - if not provided, uses image analysis)
        job_id: Optional job ID for tracking and organization
    
    Returns:
        Optimized image with S3 URLs and base64 data for database storage
    
    Example:
    ```bash
    # Optimize an uploaded image
    curl -X POST "http://localhost:8000/optimize/3d" \\
      -F "file=@character.jpg" \\
      -F "prompt=Optimize for 3D character modeling with clean background"
    
    # Optimize from URL
    curl -X POST "http://localhost:8000/optimize/3d" \\
      -F "image_url=https://example.com/image.jpg"
    ```
    """
    
    # Generate job ID if not provided
    if not job_id:
        job_id = str(uuid.uuid4())
    
    print(f"\n{'='*80}")
    print(f"🎯 3D OPTIMIZATION - Job ID: {job_id}")
    print(f"{'='*80}")
    
    try:
        # Get image data
        image_bytes = None
        image_filename = "image.jpg"
        
        if file:
            print(f"📁 Processing uploaded file: {file.filename}")
            image_bytes = await file.read()
            image_filename = file.filename
        elif image_url:
            print(f"🌐 Downloading image from URL: {image_url[:100]}...")
            try:
                response = requests.get(image_url, timeout=30)
                response.raise_for_status()
                image_bytes = response.content
                # Try to extract filename from URL
                try:
                    image_filename = image_url.split('/')[-1].split('?')[0]
                    if not image_filename or '.' not in image_filename:
                        image_filename = "image.jpg"
                except:
                    image_filename = "image.jpg"
            except Exception as e:
                raise HTTPException(400, f"Failed to download image from URL: {str(e)}")
        else:
            raise HTTPException(400, "Either file or image_url must be provided")
        
        if not image_bytes:
            raise HTTPException(400, "No image data received")
        
        # Initialize job status
        update_job_status(
            job_id=job_id,
            status="processing",
            progress=10,
            message="Starting 3D optimization...",
            result={
                "job_type": "3d_optimization",
                "source_image_filename": image_filename,
                "custom_prompt": prompt
            }
        )
        
        # Check if mapapi is available
        if not mapapi:
            raise HTTPException(500, "mapapi not available for 3D optimization")
        
        # Create temporary file for processing
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_input:
            temp_input.write(image_bytes)
            temp_input_path = temp_input.name
        
        try:
            # Update progress
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=30,
                message="Analyzing image content with AI vision...",
                result={
                    "job_type": "3d_optimization",
                    "stage": "analysis"
                }
            )
            
            # Analyze the image to understand its content
            print(f"🔍 Analyzing image content...")
            objects_analysis = mapapi.analyze_image_with_llama(temp_input_path)
            print(f"📋 Analysis result: {objects_analysis[:200]}...")
            
            # Build optimization prompt
            if prompt:
                # User provided custom prompt
                optimization_instructions = prompt
                print(f"📝 Using custom optimization prompt: {prompt}")
            else:
                # Auto-generate prompt based on image analysis
                optimization_instructions = (
                    f"Based on the image analysis: {objects_analysis[:500]}. "
                    f"Create a 3D-model-ready version with: "
                    f"1) Clean white or neutral background, "
                    f"2) Optimal viewing angle for 3D reconstruction (slight side angle), "
                    f"3) Enhanced PBR-ready textures and materials, "
                    f"4) Clear geometry and surface definition, "
                    f"5) Professional even lighting for texture baking."
                )
                print(f"🤖 Auto-generated optimization prompt based on image content")
            
            # Update progress
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=50,
                message="Generating 3D-optimized version with Gemini AI...",
                result={
                    "job_type": "3d_optimization",
                    "stage": "optimization",
                    "objects_analysis": objects_analysis
                }
            )
            
            # Create temporary output file
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_output:
                temp_output_path = temp_output.name
            
            # Run 3D optimization in thread pool
            print(f"🎨 Generating 3D-optimized image...")
            future = thread_pool.submit(
                mapapi.generate_image_with_gemini,
                optimization_instructions,
                temp_input_path,  # Use input image as reference
                temp_output_path
            )
            
            optimization_result = future.result(timeout=180)  # 3 minute timeout
            
            if not optimization_result or not optimization_result[0] or not os.path.exists(optimization_result[0]):
                raise HTTPException(500, "3D optimization did not produce a valid result")
            
            # Read optimized image
            with open(optimization_result[0], "rb") as opt_file:
                optimized_image_bytes = opt_file.read()
                optimized_3d_base64 = base64.b64encode(optimized_image_bytes).decode('utf-8')
            
            print(f"✅ 3D-optimized image generated successfully")
            
            # Update progress
            update_job_status(
                job_id=job_id,
                status="processing",
                progress=80,
                message="Uploading optimized image to S3...",
                result={
                    "job_type": "3d_optimization",
                    "stage": "upload"
                }
            )
            
            # Upload to S3
            optimized_3d_s3_url = None
            try:
                timestamp = int(time.time())
                optimized_filename = f"3d_optimized_{job_id}_{timestamp}.jpg"
                optimized_3d_s3_url = upload_asset_to_s3(
                    optimization_result[0],
                    "optimized_3d_image",
                    job_id,
                    optimized_filename
                )
                if optimized_3d_s3_url:
                    print(f"✅ 3D-optimized image uploaded to S3")
            except Exception as s3_error:
                print(f"⚠️ Failed to upload to S3: {s3_error}")
            
            # Also upload original image to S3 for reference
            original_s3_url = None
            try:
                original_filename = f"original_{job_id}_{timestamp}.jpg"
                original_s3_url = upload_asset_to_s3(
                    temp_input_path,
                    "original_image",
                    job_id,
                    original_filename
                )
                if original_s3_url:
                    print(f"✅ Original image uploaded to S3")
            except Exception as s3_error:
                print(f"⚠️ Failed to upload original to S3: {s3_error}")
            
            # Clean up temporary files
            try:
                os.unlink(optimization_result[0])
            except:
                pass
            
        finally:
            # Clean up input temporary file
            try:
                os.unlink(temp_input_path)
                if 'temp_output_path' in locals() and os.path.exists(temp_output_path):
                    os.unlink(temp_output_path)
            except:
                pass
        
        # Final update
        final_result = {
            "job_type": "3d_optimization",
            "original_image_s3_url": original_s3_url,
            "optimized_3d_image_s3_url": optimized_3d_s3_url,
            "optimized_3d_image_base64": optimized_3d_base64,
            "objects_analysis": objects_analysis,
            "optimization_instructions": optimization_instructions,
            "custom_prompt_used": prompt is not None,
            "timestamp": int(time.time()),
            "storage_type": "database_s3"
        }
        
        update_job_status(
            job_id=job_id,
            status="completed",
            progress=100,
            message="3D optimization completed successfully!",
            result=final_result
        )
        
        print(f"\n{'='*80}")
        print(f"✅ 3D OPTIMIZATION COMPLETED - Job ID: {job_id}")
        print(f"{'='*80}")
        
        return JSONResponse(content={
            "status": "completed",
            "job_id": job_id,
            "original_image_s3_url": original_s3_url,
            "optimized_3d_image_s3_url": optimized_3d_s3_url,
            "has_base64_data": True,
            "objects_analysis": objects_analysis[:200] + "..." if objects_analysis and len(objects_analysis) > 200 else objects_analysis,
            "message": "Image optimized for 3D model generation",
            "next_steps": [
                "Use the optimized image URL with /animate/extract endpoint",
                "Or use with /animate/image to generate a 3D model",
                "The optimized image has clean background and optimal angle for 3D reconstruction"
            ]
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ 3D optimization error: {e}")
        update_job_status(
            job_id=job_id,
            status="failed",
            progress=0,
            message=f"3D optimization failed: {str(e)}",
            result={
                "job_type": "3d_optimization",
                "error": str(e)
            }
        )
        raise HTTPException(500, f"3D optimization failed: {str(e)}")

@app.get("/system/status")
async def get_system_status():
    """Get detailed system status including thread pool and job information."""
    import sys
    
    # Calculate job statistics
    active_jobs = [job for job in jobs.values() if job['status'] == 'processing']
    completed_jobs = [job for job in jobs.values() if job['status'] == 'completed']
    failed_jobs = [job for job in jobs.values() if job['status'] == 'failed']
    queued_jobs = [job for job in jobs.values() if job['status'] == 'queued']
    
    # Thread pool info
    thread_info = {
        "max_workers": thread_pool._max_workers,
        "active_threads": thread_pool._threads and len(thread_pool._threads) or 0,
        "pending_tasks": thread_pool._work_queue.qsize(),
        "thread_names": [t.name for t in (thread_pool._threads or [])]
    }
    
    # System resource info
    try:
        # Basic system info without psutil
        cpu_percent = "N/A (psutil not available)"
        memory = {"available": "N/A", "percent": "N/A"}
        disk = {"free": "N/A", "percent": "N/A"}
    except Exception:
        cpu_percent = "N/A"
        memory = {"available": "N/A", "percent": "N/A"}
        disk = {"free": "N/A", "percent": "N/A"}
    
    return {
        "timestamp": time.time(),
        "uptime_seconds": time.time() - (jobs.get("_server_start", {}).get("updated_at", time.time())),
        "python_version": sys.version,
        "threading": thread_info,
        "jobs": {
            "total": len(jobs),
            "active": len(active_jobs),
            "completed": len(completed_jobs),
            "failed": len(failed_jobs),
            "queued": len(queued_jobs),
            "active_job_ids": [job["job_id"] for job in active_jobs[-5:]],  # Last 5 active jobs
        },
        "system_resources": {
            "cpu_percent": cpu_percent,
            "memory_percent": getattr(memory, 'percent', 'N/A'),
            "memory_available_gb": getattr(memory, 'available', 0) / (1024**3) if hasattr(memory, 'available') else 'N/A',
            "disk_free_gb": getattr(disk, 'free', 0) / (1024**3) if hasattr(disk, 'free') else 'N/A',
        },
        "api_status": "healthy" if len(active_jobs) < thread_pool._max_workers else "busy"
    }

@app.get("/jobs/{job_id}/{path:path}")
async def serve_job_files(job_id: str, path: str):
    """Serve files from job directories (GLB models, images, etc.)"""
    try:
        # Construct the full file path
        file_path = os.path.join("jobs", job_id, path)
        
        # Security check: ensure the path is within the jobs directory
        real_path = os.path.realpath(file_path)
        jobs_dir = os.path.realpath("jobs")
        
        if not real_path.startswith(jobs_dir):
            raise HTTPException(status_code=403, detail="Access denied")
        
        # Check if file exists
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="File not found")
        
        # Check if it's actually a file (not a directory)
        if not os.path.isfile(file_path):
            raise HTTPException(status_code=404, detail="Not a file")
        
        # Determine media type based on file extension
        import mimetypes
        media_type, _ = mimetypes.guess_type(file_path)
        
        # Special handling for GLB files
        if file_path.lower().endswith('.glb'):
            media_type = 'model/gltf-binary'
        elif file_path.lower().endswith('.gltf'):
            media_type = 'model/gltf+json'
        elif media_type is None:
            media_type = 'application/octet-stream'
        
        # Return the file
        return FileResponse(
            path=file_path,
            media_type=media_type,
            filename=os.path.basename(file_path)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error serving file {job_id}/{path}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    
# ========= HELPER FUNCTIONS =========

def get_vision_analysis_with_positions(image_path: str, image_width: int, image_height: int) -> str:
    """Get vision analysis with object position information for bounding box calculation.
    
    Args:
        image_path: Path to image file
        image_width: Width of image in pixels
        image_height: Height of image in pixels
        
    Returns:
        Analysis text with position indicators
    """
    try:
        import base64
        
        # Read and encode the image
        with open(image_path, "rb") as img_file:
            img_data = base64.b64encode(img_file.read()).decode('utf-8')
        
        # Enhanced prompt requesting position information
        payload = {
            "model": "llama3.2-vision:latest",
            "prompt": (
                "Analyze this image and identify ALL visible objects, characters, animals, and vehicles. "
                "For EACH object you identify, describe:\n"
                "1. What it is (be specific: 'red sports car', 'woman in blue dress', etc.)\n"
                "2. Its approximate position (left/center/right, top/middle/bottom)\n"
                "3. Its approximate size relative to the image (small/medium/large)\n\n"
                "Example format: 'There is a red sports car in the center-bottom area, taking up about 15% of the image width. "
                "A woman in blue dress is visible on the left side, middle height, occupying roughly 10% of the frame.'\n\n"
                "Be comprehensive and list ALL visible animate objects. Focus on: people, animals, vehicles (cars, planes, boats), "
                "moving objects, and any other elements that could be animated."
            ),
            "images": [img_data],
            "temperature": 0.3,
            "stream": False
        }
        
        print("🔍 Requesting vision analysis with position data...")
        response = requests.post("http://localhost:11434/api/generate", json=payload, timeout=90)
        response.raise_for_status()
        
        result = response.json()
        analysis = result.get("response", "")
        
        return analysis
        
    except Exception as e:
        print(f"⚠️ Enhanced vision analysis failed: {e}, falling back to standard analysis")
        # Fallback to standard analysis
        if mapapi:
            return mapapi.analyze_image_with_llama(image_path)
        return "Unable to analyze image"


def estimate_bounding_box(position_desc: str, size_desc: str, image_width: int, image_height: int) -> dict:
    """Estimate bounding box coordinates from position description.
    
    Args:
        position_desc: Position description (e.g., "center-bottom", "left side")
        size_desc: Size description (e.g., "small", "15% of image width")
        image_width: Image width in pixels
        image_height: Image height in pixels
        
    Returns:
        Dictionary with x, y, width, height in pixels
    """
    position_lower = position_desc.lower()
    size_lower = size_desc.lower()
    
    # Default values (center, medium size)
    x_ratio = 0.4  # Start X (left edge as ratio of width)
    y_ratio = 0.4  # Start Y (top edge as ratio of height)
    w_ratio = 0.2  # Width as ratio of image width
    h_ratio = 0.2  # Height as ratio of image height
    
    # Parse horizontal position
    if any(word in position_lower for word in ['left', 'leftmost', 'left-']):
        x_ratio = 0.05
    elif any(word in position_lower for word in ['right', 'rightmost', 'right-']):
        x_ratio = 0.75
    elif any(word in position_lower for word in ['center', 'middle', 'centre']):
        x_ratio = 0.4
    
    # Parse vertical position
    if any(word in position_lower for word in ['top', 'upper', 'top-', 'above']):
        y_ratio = 0.05
    elif any(word in position_lower for word in ['bottom', 'lower', 'bottom-', 'below']):
        y_ratio = 0.7
    elif any(word in position_lower for word in ['middle', 'center', 'centre']):
        y_ratio = 0.4
    
    # Parse size from percentage if mentioned
    import re
    percent_match = re.search(r'(\d+)%', size_lower)
    if percent_match:
        percent = int(percent_match.group(1))
        w_ratio = min(percent / 100.0, 0.8)
        h_ratio = w_ratio * 0.8  # Assume roughly proportional
    elif 'large' in size_lower or 'big' in size_lower:
        w_ratio = 0.4
        h_ratio = 0.4
    elif 'small' in size_lower or 'tiny' in size_lower:
        w_ratio = 0.1
        h_ratio = 0.1
    elif 'medium' in size_lower:
        w_ratio = 0.2
        h_ratio = 0.2
    
    # Calculate pixel coordinates
    x = int(x_ratio * image_width)
    y = int(y_ratio * image_height)
    width = int(w_ratio * image_width)
    height = int(h_ratio * image_height)
    
    # Ensure box stays within image bounds
    if x + width > image_width:
        width = image_width - x
    if y + height > image_height:
        height = image_height - y
    
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height
    }


def extract_position_from_text(text: str) -> str:
    """Extract position information from text description.
    
    Args:
        text: Description text containing position information
        
    Returns:
        Position description (e.g., "center-bottom", "left-middle")
    """
    text_lower = text.lower()
    
    # Horizontal position
    h_pos = "center"
    if any(word in text_lower for word in ['left', 'leftmost', 'left side', 'on the left']):
        h_pos = "left"
    elif any(word in text_lower for word in ['right', 'rightmost', 'right side', 'on the right']):
        h_pos = "right"
    elif any(word in text_lower for word in ['center', 'centre', 'middle', 'centered']):
        h_pos = "center"
    
    # Vertical position
    v_pos = "middle"
    if any(word in text_lower for word in ['top', 'upper', 'above', 'high', 'overhead']):
        v_pos = "top"
    elif any(word in text_lower for word in ['bottom', 'lower', 'below', 'ground', 'floor']):
        v_pos = "bottom"
    elif any(word in text_lower for word in ['middle', 'center', 'centre']):
        v_pos = "middle"
    
    # Combine
    return f"{h_pos}-{v_pos}"


def extract_size_from_text(text: str) -> str:
    """Extract size information from text description.
    
    Args:
        text: Description text containing size information
        
    Returns:
        Size description (e.g., "large", "15%", "small")
    """
    text_lower = text.lower()
    
    # Check for percentage
    import re
    percent_match = re.search(r'(\d+)%', text_lower)
    if percent_match:
        return f"{percent_match.group(1)}%"
    
    # Check for size descriptors
    if any(word in text_lower for word in ['large', 'big', 'huge', 'massive', 'giant']):
        return "large"
    elif any(word in text_lower for word in ['small', 'tiny', 'little', 'mini', 'compact']):
        return "small"
    elif any(word in text_lower for word in ['medium', 'moderate', 'average']):
        return "medium"
    
    # Default
    return "medium"


async def wan_animate_image(request: WanAnimateRequest) -> dict:
    """Animate an image using WAN 2.2 API from WaveSpeed.ai
    
    Args:
        request: WanAnimateRequest containing image URL, prompt, and animation parameters
        
    Returns:
        Dictionary with animation result URL and metadata
    """
    try:
        import requests
        import json
        import time
        
        # Get API key from environment
        WAVESPEED_API_KEY = os.environ.get("WAVESPEED_API_KEY")
        if not WAVESPEED_API_KEY:
            raise ValueError("WAVESPEED_API_KEY not found in environment variables")
        
        print(f"🎬 Starting WAN 2.2 animation for image: {request.image[:50]}...")
        print(f"📝 Animation prompt: {request.prompt}")
        
        # Submit animation task
        url = "https://api.wavespeed.ai/api/v3/wavespeed-ai/wan-2.2/animate"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {WAVESPEED_API_KEY}",
        }
        
        payload = {
            "mode": request.mode,
            "image": request.image,
            "prompt": request.prompt,
            "resolution": request.resolution,
            "seed": request.seed
        }
        
        # Only include video field if it has a value (API doesn't accept null)
        if request.video:
            payload["video"] = request.video
        
        print(f"📤 Submitting WAN 2.2 animation task...")
        begin = time.time()
        
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        if response.status_code != 200:
            raise Exception(f"WAN API error: {response.status_code}, {response.text}")
        
        result = response.json()["data"]
        request_id = result["id"]
        print(f"✅ Animation task submitted successfully. Request ID: {request_id}")
        
        # Poll for results
        poll_url = f"https://api.wavespeed.ai/api/v3/predictions/{request_id}/result"
        poll_headers = {"Authorization": f"Bearer {WAVESPEED_API_KEY}"}
        
        print(f"⏳ Polling for animation results...")
        max_retries = 60  # 3 minutes with 3-second intervals
        retry_count = 0
        
        while retry_count < max_retries:
            await asyncio.sleep(3)  # Use async sleep for non-blocking
            
            response = requests.get(poll_url, headers=poll_headers)
            if response.status_code != 200:
                print(f"⚠️ Polling error: {response.status_code}, {response.text}")
                retry_count += 1
                continue
            
            result = response.json()["data"]
            status = result["status"]
            
            if status == "completed":
                end = time.time()
                animation_url = result["outputs"][0]
                print(f"🎉 WAN 2.2 animation completed in {end - begin:.2f} seconds!")
                print(f"🎬 Animation URL: {animation_url}")
                
                return {
                    "success": True,
                    "animation_url": animation_url,
                    "request_id": request_id,
                    "processing_time": end - begin,
                    "status": "completed",
                    "prompt": request.prompt,
                    "resolution": request.resolution,
                    "seed": request.seed
                }
                
            elif status == "failed":
                error_msg = result.get('error', 'Unknown error')
                print(f"❌ WAN 2.2 animation failed: {error_msg}")
                return {
                    "success": False,
                    "error": error_msg,
                    "request_id": request_id,
                    "status": "failed",
                    "prompt": request.prompt
                }
            else:
                print(f"⏳ Animation still processing. Status: {status}")
                retry_count += 1
        
        # Timeout reached
        print(f"⏰ WAN 2.2 animation timed out after {max_retries * 3} seconds")
        return {
            "success": False,
            "error": "Animation processing timed out",
            "request_id": request_id,
            "status": "timeout",
            "prompt": request.prompt
        }
        
    except Exception as e:
        print(f"❌ WAN 2.2 animation error: {e}")
        return {
            "success": False,
            "error": str(e),
            "status": "error",
            "prompt": request.prompt if hasattr(request, 'prompt') else 'unknown'
        }

def check_ffmpeg() -> bool:
    """Check if ffmpeg is available for video processing."""
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

def download_file(url: str, dest: str) -> None:
    """Download a file from URL to destination path."""
    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r.raw, f)

async def luma_ray_animate_image(image_path: str, request: LumaRayRequest, job_id: str) -> dict:
    """Animate an image using Luma Ray 2 I2V API
    
    Args:
        image_path: Path to the input image file
        request: LumaRayRequest containing animation parameters
        job_id: Job ID for tracking and output organization
        
    Returns:
        Dictionary with animation result URL and metadata
    """
    try:
        # Get API key from environment
        WAVESPEED_API_KEY = os.environ.get("WAVESPEED_API_KEY")
        if not WAVESPEED_API_KEY:
            raise ValueError("WAVESPEED_API_KEY not found in environment variables")
        
        # Validate image path
        img_path = pathlib.Path(image_path)
        if not img_path.exists():
            raise FileNotFoundError(f"Image not found: {img_path.resolve()}")
        
        print(f"🎬 Starting Luma Ray 2 I2V animation for job {job_id}")
        print(f"📝 Animation prompt: {request.prompt}")
        print(f"📐 Video size: {request.size}")
        print(f"⏱️ Duration: {request.duration}s")
        
        # Setup job output directory
        job_dir = f"jobs/{job_id}"
        os.makedirs(job_dir, exist_ok=True)
        
        # Prepare API request
        headers = {"Authorization": f"Bearer {WAVESPEED_API_KEY}", "Content-Type": "application/json"}
        
        # Determine image source
        image_data = None
        if request.image:
            # Use provided image data (base64 or URL)
            if request.image.startswith("data:"):
                image_data = request.image
            else:
                # Assume it's base64 without data URI prefix
                image_data = f"data:image/png;base64,{request.image}"
        else:
            # Encode local image file as base64
            with open(img_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("ascii")
                image_data = f"data:image/png;base64,{img_b64}"
        
        payload = {
            "prompt": request.prompt,
            "size": request.size,
            "duration": str(request.duration),
            "image": image_data
        }
        
        # Add freeze and animate lists if provided
        if request.freeze:
            payload["freeze"] = request.freeze
            print(f"🔒 Freeze objects: {', '.join(request.freeze)}")
        
        if request.animate:
            payload["animate"] = request.animate
            print(f"🎬 Animate objects: {', '.join(request.animate)}")
        
        print(f"📤 Submitting Luma Ray 2 I2V task...")
        begin = time.time()
        
        # Submit animation task
        submit_url = "https://api.wavespeed.ai/api/v3/luma/ray-2-i2v"
        response = requests.post(submit_url, headers=headers, json=payload, timeout=300)
        
        if response.status_code != 200:
            raise Exception(f"Luma Ray API error: {response.status_code}, {response.text}")
        
        resp = response.json()
        job_id_api = resp.get("data", {}).get("id") or resp.get("id")
        
        if not job_id_api:
            raise Exception(f"Unexpected submit response: {json.dumps(resp, indent=2)}")
        
        print(f"✅ Animation task submitted successfully. API Job ID: {job_id_api}")
        
        # Poll for results
        result_url = f"https://api.wavespeed.ai/api/v3/predictions/{job_id_api}/result"
        poll_headers = {"Authorization": f"Bearer {WAVESPEED_API_KEY}"}
        
        print(f"⏳ Polling for animation results...")
        max_retries = 60  # 5 minutes with 5-second intervals
        retry_count = 0
        
        while retry_count < max_retries:
            await asyncio.sleep(5)  # Use async sleep for non-blocking
            
            r = requests.get(result_url, headers=poll_headers, timeout=60)
            if r.status_code != 200:
                print(f"⚠️ Polling error: {r.status_code}, {r.text}")
                retry_count += 1
                continue
            
            jr = r.json()
            status = jr.get("data", {}).get("status") or jr.get("status")
            error = jr.get("data", {}).get("error") or jr.get("error")
            outputs = jr.get("data", {}).get("outputs") or jr.get("outputs") or []
            
            if status in ("completed", "succeeded"):
                if not outputs:
                    raise Exception(f"Completed but no outputs: {json.dumps(jr, indent=2)}")
                
                video_url = outputs[0]
                end = time.time()
                
                # Download video to job directory
                out_video_path = os.path.join(job_dir, f"luma_ray_animation_{job_id}.mp4")
                print(f"⬇️ Downloading video: {video_url}")
                download_file(video_url, out_video_path)
                
                print(f"🎉 Luma Ray 2 I2V animation completed in {end - begin:.2f} seconds!")
                print(f"🎬 Video saved: {out_video_path}")
                
                result = {
                    "success": True,
                    "video_url": video_url,
                    "local_video_path": out_video_path,
                    "api_job_id": job_id_api,
                    "processing_time": end - begin,
                    "status": "completed",
                    "prompt": request.prompt,
                    "size": request.size,
                    "duration": request.duration
                }
                
                # Optional: Create GIF version
                if request.also_make_gif and check_ffmpeg():
                    try:
                        out_gif_path = os.path.join(job_dir, f"luma_ray_animation_{job_id}.gif")
                        print("🌀 Converting MP4 → GIF...")
                        
                        subprocess.run([
                            "ffmpeg", "-y", "-i", out_video_path,
                            "-vf", "fps=15,scale=640:-1:flags=lanczos", "-loop", "0",
                            out_gif_path
                        ], check=True)
                        
                        result["gif_path"] = out_gif_path
                        print(f"🖼️ GIF saved: {out_gif_path}")
                        
                        # Upload GIF to S3
                        print(f"📤 Uploading GIF to S3...")
                        signed_gif_url = upload_luma_gif_to_s3(job_id, out_gif_path)
                        if signed_gif_url:
                            result["signed_gif_url"] = signed_gif_url
                            print(f"✅ GIF uploaded to S3: {signed_gif_url.get('s3_direct_url', 'N/A')[:50]}...")
                        else:
                            print(f"❌ Failed to upload GIF to S3")
                        
                    except Exception as e:
                        print(f"⚠️ GIF conversion failed: {e}")
                        result["gif_error"] = str(e)
                elif request.also_make_gif:
                    result["gif_error"] = "ffmpeg not found"
                
                # Upload input image to S3
                print(f"📤 Uploading input image to S3...")
                signed_input_image = upload_input_image_to_s3(job_id, image_path)
                if signed_input_image:
                    result["signed_input_image"] = signed_input_image
                    print(f"✅ Input image uploaded to S3: {signed_input_image.get('s3_direct_url', 'N/A')[:50]}...")
                else:
                    print(f"❌ Failed to upload input image to S3")
                
                return result
                
            elif status in ("failed", "canceled"):
                error_msg = error or str(jr)
                print(f"❌ Luma Ray 2 I2V animation failed: {error_msg}")
                return {
                    "success": False,
                    "error": error_msg,
                    "api_job_id": job_id_api,
                    "status": "failed",
                    "prompt": request.prompt
                }
            else:
                print(f"⏳ Animation still processing. Status: {status}")
                retry_count += 1
        
        # Timeout reached
        print(f"⏰ Luma Ray 2 I2V animation timed out after {max_retries * 5} seconds")
        return {
            "success": False,
            "error": "Animation processing timed out",
            "api_job_id": job_id_api,
            "status": "timeout",
            "prompt": request.prompt
        }
        
    except Exception as e:
        print(f"❌ Luma Ray 2 I2V animation error: {e}")
        return {
            "success": False,
            "error": str(e),
            "status": "error",
            "prompt": request.prompt if hasattr(request, 'prompt') else 'unknown'
        }

def generate_image_from_prompt_sync(job_id: str, prompt: str) -> str:
    """Synchronous version for thread pool execution."""
    try:
        if not mapapi:
            raise RuntimeError("mapapi module not available")
            
        job_dir = f"jobs/{job_id}"
        os.makedirs(job_dir, exist_ok=True)
        
        print(f"🚀 [Thread {threading.current_thread().name}] Starting mapapi workflow for prompt: '{prompt[:100]}...'")
        
        # Set environment override for prompt
        original_prompt = os.environ.get("GEMINI_PROMPT_OVERRIDE")
        os.environ["GEMINI_PROMPT_OVERRIDE"] = prompt
        
        try:
            # Step 1: Generate initial image with Gemini 2.5 Flash Image
            print("🎨 Step 1: Generating image with Gemini 2.5...")
            gemini_output = os.path.join(job_dir, "gemini_generated.png")
            
            gemini_result = mapapi.generate_image_with_gemini(
                objects_description="",  # Will use prompt override
                reference_image_path="",  # Skip reference image when using override
                output_path=gemini_output
            )
            
            # Handle both tuple and single return formats
            if isinstance(gemini_result, tuple):
                gemini_image_path, content_type = gemini_result
            else:
                gemini_image_path = gemini_result
            
            if not gemini_image_path or not os.path.exists(gemini_image_path):
                raise RuntimeError("Failed to generate image with Gemini")
            
            print(f"✅ Gemini image generated: {gemini_image_path}")
            
            # Upload Gemini image to S3 immediately after generation
            print(f"📤 Uploading Gemini image to S3 for job {job_id}...")
            s3_gemini_url = upload_gemini_image_to_s3(job_id, gemini_image_path)
            
            if s3_gemini_url:
                # Update job with s3_gemini_url
                update_job_status(job_id, "processing", 15, "Gemini image generated and uploaded to S3", {
                    "s3_gemini_url": s3_gemini_url,
                    "gemini_image_local": gemini_image_path
                })
                print(f"✅ Gemini image uploaded to S3: {s3_gemini_url.get('s3_direct_url', 'N/A')[:50]}...")
            else:
                print(f"❌ Failed to upload Gemini image to S3 for job {job_id}")
                # Continue processing even if S3 upload fails
                update_job_status(job_id, "processing", 15, "Gemini image generated (S3 upload failed)", {
                    "s3_gemini_url": None,
                    "gemini_image_local": gemini_image_path
                })
            
            # Step 2: Analyze image with Llama 3.2 Vision
            print("🔍 Step 2: Analyzing image with Llama 3.2 Vision...")
            objects_analysis = mapapi.analyze_image_with_llama(gemini_image_path)
            print(f"📋 Objects identified: {objects_analysis}")
            
            # Save analysis and prompts for reference
            analysis_file = os.path.join(job_dir, "objects_analysis.txt")
            with open(analysis_file, "w") as f:
                f.write(f"Original Analysis: {objects_analysis}\n")
            
            # Step 4: Generate optimized image for 3D modeling using RETEXTURE_PROMPT
            print("🎨 Step 4: Generating 3D-optimized image using RETEXTURE_PROMPT...")
            optimized_image = os.path.join(job_dir, "3d_optimized.jpg")
            
            # Get RETEXTURE_PROMPT from environment
            retexture_prompt = os.getenv("RETEXTURE_PROMPT", 
                "preserve and enhance the existing textures, maintain original colors and patterns, high quality PBR materials")
            
            # Create a combined 3D-focused prompt using RETEXTURE_PROMPT + recognized objects
            combined_prompt = (
                f"Generate a high-fidelity 3D model of only the following detected objects: {objects_analysis}. "
                f"Use the objects_analysis as the sole source of content. Preserve original textures and materials with maximum fidelity, "
                f"produce clean, high-resolution PBR-ready textures, and prioritize photogrammetry-friendly geometry. "
                f"Frame the model from a side-angled/top-degree viewpoint (slightly elevated side view) that best reveals form for 3D reconstruction. "
                f"Render against a totally white background with even lighting, no background clutter, and neutral color balance. "
                f"Output suitable for 3D modeling, rigging, and animation with clear surface definition and minimal post-processing required."
            )
            
            print(f"🎯 Combined 3D prompt: {combined_prompt[:150]}...")
            
            # Update analysis file with RETEXTURE_PROMPT and combined prompt
            with open(analysis_file, "a") as f:
                f.write(f"RETEXTURE_PROMPT: {retexture_prompt}\n")
                f.write(f"Combined 3D Prompt: {combined_prompt}\n")
            
            # Temporarily override prompt for 3D optimization
            os.environ["GEMINI_PROMPT_OVERRIDE"] = combined_prompt
            
            optimized_result = mapapi.generate_image_with_gemini(
                objects_description=objects_analysis,
                reference_image_path=gemini_image_path,  # Use first generation as reference
                output_path=optimized_image
            )
            
            # Handle return format
            if isinstance(optimized_result, tuple):
                final_image_path, content_type = optimized_result
            else:
                final_image_path = optimized_result
            
            # Use the optimized image if successful, otherwise fall back to Gemini original
            if final_image_path and os.path.exists(final_image_path):
                print(f"✅ 3D-optimized image ready: {final_image_path}")
                
                # Upload optimized 3D image to S3 immediately after generation
                print(f"📤 Uploading optimized 3D image to S3 for job {job_id}...")
                s3_optimized_url = upload_optimized_3d_image_to_s3(job_id, final_image_path)
                
                if s3_optimized_url:
                    # Update job with s3_optimized_url
                    update_job_status(job_id, "processing", 35, "Optimized 3D image generated and uploaded to S3", {
                        "s3_optimized_url": s3_optimized_url,
                        "optimized_3d_image_local": final_image_path
                    })
                    print(f"✅ Optimized 3D image uploaded to S3: {s3_optimized_url.get('s3_direct_url', 'N/A')[:50]}...")
                else:
                    print(f"❌ Failed to upload optimized 3D image to S3 for job {job_id}")
                    # Continue processing even if S3 upload fails
                    update_job_status(job_id, "processing", 35, "Optimized 3D image generated (S3 upload failed)", {
                        "s3_optimized_url": None,
                        "optimized_3d_image_local": final_image_path
                    })
                
                return final_image_path
            else:
                print("⚠️ 3D optimization failed, using Gemini original")
                return gemini_image_path
                
        finally:
            # Restore original prompt
            if original_prompt:
                os.environ["GEMINI_PROMPT_OVERRIDE"] = original_prompt
            elif "GEMINI_PROMPT_OVERRIDE" in os.environ:
                del os.environ["GEMINI_PROMPT_OVERRIDE"]
        
    except Exception as e:
        print(f"❌ Error in mapapi workflow: {e}")
        import traceback
        traceback.print_exc()
        return None

async def generate_image_from_prompt(job_id: str, prompt: str) -> str:
    """Async wrapper for image generation that runs in thread pool."""
    loop = asyncio.get_event_loop()
    
    try:
        result = await loop.run_in_executor(
            thread_pool,
            generate_image_from_prompt_sync,
            job_id,
            prompt
        )
        return result
    except Exception as e:
        print(f"❌ Thread pool execution error for image generation: {e}")
        return None

async def generate_from_coordinates(job_id: str, lat: float, lon: float) -> str:
    """Generate 3D model from coordinates using full mapapi workflow."""
    try:
        if not mapapi:
            raise RuntimeError("mapapi module not available")
            
        job_dir = f"jobs/{job_id}"
        os.makedirs(job_dir, exist_ok=True)
        
        print(f"🌍 Processing coordinates: {lat}, {lon}")
        
        # Clean up any existing files to ensure fresh generation
        cleanup_existing_files()
        
        # Determine output path - use job-specific folder
        timestamp = int(time.time())
        job_models_dir = f"jobs/{job_id}/models"
        os.makedirs(job_models_dir, exist_ok=True)
        expected_model = os.path.join(job_models_dir, f"{job_id}_{timestamp}_streetview_3d_model.glb")
        print(f"📁 Job-specific output: {expected_model}")
        
        # Set coordinates in environment
        original_lat = os.environ.get("STREET_LAT")
        original_lng = os.environ.get("STREET_LNG")
        
        try:
            # Update coordinates
            os.environ["STREET_LAT"] = str(lat)
            os.environ["STREET_LNG"] = str(lon)
            print(f"📍 Set coordinates: STREET_LAT={lat}, STREET_LNG={lon}")
            
            # Step 1: Download Street View image
            print("📸 Step 1: Downloading Google Street View image...")
            streetview_image = os.path.join(job_dir, "streetview.jpg")
            
            # Build Street View URL
            google_maps_key = os.getenv("GOOGLE_MAPS_API_KEY")
            img_size = os.getenv("STREET_IMG_SIZE", "640x640")
            streetview_url = (
                f"https://maps.googleapis.com/maps/api/streetview"
                f"?size={img_size}&location={lat},{lon}&key={google_maps_key}"
            )
            
            # Download the image
            import requests
            print(f"🌐 Fetching from: {streetview_url}")
            response = requests.get(streetview_url, timeout=30)
            response.raise_for_status()
            
            with open(streetview_image, "wb") as f:
                f.write(response.content)
            print(f"✅ Street View image saved: {streetview_image}")
            
            # Step 2: Find nearest Street View for better quality
            print("🔍 Step 2: Finding nearest Street View panorama...")
            try:
                nearest = mapapi.find_nearest_streetview(
                    lat, lon, google_maps_key,
                    start_radius=50, step=50, max_radius=3000, source="outdoor"
                )
                
                if nearest:
                    print(f"📍 Found better panorama: {nearest}")
                    # Get higher quality image using pano ID
                    better_url = mapapi.build_streetview_image_url(
                        google_maps_key,
                        pano_id=nearest["pano_id"],
                        size=img_size,
                        source="outdoor",
                        heading=0,
                        pitch=0,
                        fov=80
                    )
                    
                    response = requests.get(better_url, timeout=30)
                    response.raise_for_status()
                    with open(streetview_image, "wb") as f:
                        f.write(response.content)
                    print("✅ Updated with higher quality panorama")
                
            except Exception as e:
                print(f"⚠️ Could not find better panorama, using original: {e}")
            
            # Step 3: Analyze image with Llama 3.2 Vision
            print("🔍 Step 3: Analyzing Street View with Llama 3.2...")
            objects_analysis = mapapi.analyze_image_with_llama(streetview_image)
            print(f"� Objects identified: {objects_analysis}")
            
            # Step 4: Extract 3D-worthy objects
            clean_objects = mapapi.extract_3d_objects(objects_analysis)
            print(f"🏗️ 3D objects for modeling: {clean_objects}")
            
            # Step 5: Generate enhanced image with Gemini 2.5 using RETEXTURE_PROMPT
            print("🎨 Step 5: Generating enhanced image with Gemini using RETEXTURE_PROMPT...")
            enhanced_image = os.path.join(job_dir, "gemini_enhanced.png")
            
            # Get RETEXTURE_PROMPT from environment
            retexture_prompt = os.getenv("RETEXTURE_PROMPT", 
                "preserve and enhance the existing textures, maintain original colors and patterns, high quality PBR materials")
            
            # Create enhanced prompt combining RETEXTURE_PROMPT with analysis
            enhanced_prompt = (
                f"Based on this Street View image, create a detailed 3D model of {clean_objects}. "
                f"{retexture_prompt}. "
                f"Focus on {clean_objects}, architectural accuracy, "
                f"optimal viewing angle for 3D reconstruction, "
                f"enhanced detail and textures while preserving the original structure"
            )
            
            print(f"🎯 Enhanced Street View prompt: {enhanced_prompt[:150]}...")
            
            # Save analysis with enhanced prompt
            analysis_file = os.path.join(job_dir, "streetview_analysis.txt")
            with open(analysis_file, "w") as f:
                f.write(f"Coordinates: {lat}, {lon}\n")
                f.write(f"Objects Analysis: {objects_analysis}\n")
                f.write(f"3D Objects: {clean_objects}\n")
                f.write(f"RETEXTURE_PROMPT: {retexture_prompt}\n")
                f.write(f"Enhanced Prompt: {enhanced_prompt}\n")
            
            # Temporarily set the enhanced prompt
            original_gemini_prompt = os.environ.get("GEMINI_PROMPT_OVERRIDE")
            os.environ["GEMINI_PROMPT_OVERRIDE"] = enhanced_prompt
            
            try:
                gemini_result = mapapi.generate_image_with_gemini(
                    objects_description=clean_objects,
                    reference_image_path=streetview_image,
                    output_path=enhanced_image
                )
            finally:
                # Restore original Gemini prompt
                if original_gemini_prompt:
                    os.environ["GEMINI_PROMPT_OVERRIDE"] = original_gemini_prompt
                elif "GEMINI_PROMPT_OVERRIDE" in os.environ:
                    del os.environ["GEMINI_PROMPT_OVERRIDE"]
            
            # Handle return format
            if isinstance(gemini_result, tuple):
                final_image_path, content_type = gemini_result
            else:
                final_image_path = gemini_result
            
            # Use enhanced image if available, otherwise use Street View
            modeling_image = final_image_path if (final_image_path and os.path.exists(final_image_path)) else streetview_image
            print(f"🎯 Using image for 3D modeling: {modeling_image}")
            
            # Step 6: Generate 3D model
            print("🚀 Step 6: Converting to 3D model...")
            
            # Copy to main directory for processing
            shutil.copy2(modeling_image, "streetview.jpg")
            
            # Use mapapi's 3D generation
            gen_params = {
                "seed": int(os.getenv("SEED", "42")),
                "steps": int(os.getenv("STEPS", "30")),
                "num_chunks": int(os.getenv("NUM_CHUNKS", "8000")),
                "max_facenum": int(os.getenv("MAX_FACENUM", "20000")),
                "guidance_scale": float(os.getenv("GUIDANCE_SCALE", "7.5")),
                "generate_texture": os.getenv("GENERATE_TEXTURE", "true").lower() == "true",
                "octree_resolution": int(os.getenv("OCTREE_RESOLUTION", "256")),
                "remove_background": os.getenv("REMOVE_BACKGROUND", "true").lower() == "true",
                "texture_resolution": int(os.getenv("TEXTURE_RESOLUTION", "4096")),
                "bake_normals": os.getenv("BAKE_NORMALS", "true").lower() == "true",
                "bake_ao": os.getenv("BAKE_AO", "true").lower() == "true",
            }
            
            headers = {
                "x-api-key": os.getenv("SEGMIND_API_KEY"),
                "Accept": "application/octet-stream"
            }
            
            segmind_endpoint = os.getenv("SEGMIND_ENDPOINT", "https://api.segmind.com/v1/hunyuan3d-2.1")
            
            # Generate 3D model
            import mimetypes
            mime = mimetypes.guess_type(modeling_image)[0] or "image/jpeg"
            
            with open(modeling_image, "rb") as imgf:
                # Validate image file before upload
                img_content = imgf.read()
                if len(img_content) == 0:
                    raise RuntimeError("Modeling image file is empty")
                
                print(f"📊 Image validation: {os.path.basename(modeling_image)}, size: {len(img_content):,} bytes")
                
                # Reset file pointer
                imgf.seek(0)
                files = {"image": (os.path.basename(modeling_image), imgf, mime)}
                data_form = {k: v for k, v in gen_params.items()}
                
                # Use retry wrapper for better reliability
                success, meta = mapapi.post_hunyuan_to_file_with_retry(
                    segmind_endpoint,
                    headers=headers,
                    data=data_form,
                    files=files,
                    out_path=expected_model,  # Generate directly in job folder
                    max_retries=3
                )
            
            if success and os.path.exists(expected_model):
                # Clean up temporary files from origin folder
                temp_files_to_clean = [
                    "streetview.jpg",
                    "gemini_generated.png", 
                    "streetview_3d_model.glb"  # Remove any accidentally created in origin
                ]
                
                for temp_file in temp_files_to_clean:
                    if os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                            print(f"🧹 Cleaned up: {temp_file}")
                        except Exception as e:
                            print(f"⚠️ Could not remove {temp_file}: {e}")
                
                # Save metadata
                metadata = {
                    "method": "coordinates_to_3d_pipeline",
                    "coordinates": {"lat": lat, "lng": lon},
                    "streetview_image": streetview_image,
                    "modeling_image": modeling_image,
                    "objects_analysis": objects_analysis,
                    "clean_objects": clean_objects,
                    "params": gen_params,
                    "file": expected_model,
                    "nearest_pano": nearest if 'nearest' in locals() else None
                }
                
                metadata_file = os.path.join(job_dir, "3d_model_output.json")
                with open(metadata_file, "w") as f:
                    json.dump(metadata, f, indent=2)
                
                print(f"✅ Generated new 3D model for coordinates: {lat}, {lon}")
                print(f"📁 Model saved in job folder: {expected_model}")
                return os.path.abspath(expected_model)
            else:
                raise RuntimeError(f"3D model generation failed: {meta}")
            
        finally:
            # Restore original coordinates
            if original_lat:
                os.environ["STREET_LAT"] = original_lat
            elif "STREET_LAT" in os.environ:
                del os.environ["STREET_LAT"]
                
            if original_lng:
                os.environ["STREET_LNG"] = original_lng
            elif "STREET_LNG" in os.environ:
                del os.environ["STREET_LNG"]
        
    except Exception as e:
        print(f"Error generating from coordinates: {e}")
        import traceback
        traceback.print_exc()
        return None

def cleanup_existing_files():
    """Clean up existing files to ensure fresh generation."""
    import glob
    
    # Static file list for known files
    files_to_remove = [
        "streetview_3d_model.glb",
        "streetview.jpg", 
        "gemini_generated.png",
        "gemini_generated.txt",
        "objects_analysis.txt",
        "objects_for_3d.txt", 
        "streetview_analysis.txt",
        "3d_model_output.json",
        "gemini_response.json",
        "gemini_enhanced.png",
        "3d_optimized.jpg"
    ]
    
    # Add pattern-based cleanup for timestamped files
    timestamp_patterns = [
        "streetview_*_3d_model.glb",
        "*_retextured_model.glb",
        "*_3d_model.glb"
    ]
    
    removed_files = []
    
    # Remove static files
    for file_path in files_to_remove:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                removed_files.append(file_path)
            except Exception as e:
                print(f"⚠️ Could not remove {file_path}: {e}")
    
    # Remove timestamped files using patterns
    for pattern in timestamp_patterns:
        for file_path in glob.glob(pattern):
            try:
                os.remove(file_path)
                removed_files.append(file_path)
            except Exception as e:
                print(f"⚠️ Could not remove {file_path}: {e}")
    
    if removed_files:
        print(f"🧹 Cleaned up existing files: {removed_files}")
    
    return len(removed_files)

def validate_image_for_3d_generation(image_path: str) -> bool:
    """Validate that an image is suitable for 3D generation."""
    try:
        if not os.path.exists(image_path):
            print(f"❌ Image file does not exist: {image_path}")
            return False
        
        file_size = os.path.getsize(image_path)
        if file_size == 0:
            print(f"❌ Image file is empty: {image_path}")
            return False
        
        if file_size < 1024:  # Less than 1KB
            print(f"⚠️ Image file seems very small: {file_size} bytes")
        
        # Try to validate with PIL if available
        try:
            from PIL import Image
            with Image.open(image_path) as img:
                width, height = img.size
                if width < 64 or height < 64:
                    print(f"❌ Image too small for 3D generation: {width}x{height}")
                    return False
                
                print(f"✅ Image validation passed: {width}x{height}, {img.mode}, {file_size:,} bytes")
                return True
                
        except ImportError:
            print("⚠️ PIL not available, using basic validation")
            return file_size > 1024  # At least 1KB
        except Exception as pil_error:
            print(f"❌ PIL validation failed: {pil_error}")
            return file_size > 1024  # Fallback to size check
        
    except Exception as e:
        print(f"❌ Image validation error: {e}")
        return False

# ========= DATABASE-ONLY HELPER FUNCTIONS =========

def generate_image_from_prompt_and_upload_to_s3(job_id: str, prompt: str) -> str:
    """Generate image from prompt and upload directly to S3 - Database-only storage."""
    try:
        if not mapapi:
            raise RuntimeError("mapapi module not available")
            
        print(f"🚀 [Thread {threading.current_thread().name}] Starting database-only image generation for prompt: '{prompt[:100]}...'")
        
        # Create temporary file for image generation (no job folder)
        import tempfile
        temp_dir = tempfile.mkdtemp()
        gemini_output = os.path.join(temp_dir, "gemini_generated.png")
        
        # Set environment override for prompt
        original_prompt = os.environ.get("GEMINI_PROMPT_OVERRIDE")
        os.environ["GEMINI_PROMPT_OVERRIDE"] = prompt
        
        try:
            # Step 1: Generate initial image with Gemini 2.5 Flash Image
            print("🎨 Step 1: Generating image with Gemini 2.5...")
            
            gemini_result = mapapi.generate_image_with_gemini(
                objects_description="",  # Will use prompt override
                reference_image_path="",  # Skip reference image when using override
                output_path=gemini_output
            )
            
            # Handle both tuple and single return formats
            if isinstance(gemini_result, tuple):
                gemini_image_path, content_type = gemini_result
            else:
                gemini_image_path = gemini_result
            
            if not gemini_image_path or not os.path.exists(gemini_image_path):
                raise RuntimeError("Failed to generate image with Gemini")
            
            print(f"✅ Gemini image generated: {gemini_image_path}")
            
            # Upload directly to S3 without saving locally
            print(f"📤 Uploading generated image directly to S3...")
            timestamp = int(time.time())
            s3_filename = f"{job_id}_{timestamp}_generated_image.png"
            s3_key = f"assets/{job_id}/generated/{s3_filename}"
            
            try:
                # Initialize S3 client and upload
                s3_client = boto3.client("s3", region_name="us-west-1")
                
                s3_client.upload_file(
                    gemini_image_path,
                    S3_BUCKET,
                    s3_key,
                    ExtraArgs={"ContentType": "image/png"}
                )
                
                # Generate S3 URL
                s3_image_url = f"https://{S3_BUCKET}.s3.us-west-1.amazonaws.com/{s3_key}"
                
                print(f"✅ Generated image uploaded to S3: {s3_image_url}")
                
                # Update job status with S3 URL
                update_job_status(job_id, "processing", 15, "Image generated and uploaded to S3", {
                    "generated_image_s3_url": s3_image_url,
                    "storage_type": "database_s3_only"
                })
                
                return s3_image_url
                
            except Exception as s3_error:
                raise Exception(f"Failed to upload generated image to S3: {s3_error}")
                
        finally:
            # Restore original prompt
            if original_prompt:
                os.environ["GEMINI_PROMPT_OVERRIDE"] = original_prompt
            elif "GEMINI_PROMPT_OVERRIDE" in os.environ:
                del os.environ["GEMINI_PROMPT_OVERRIDE"]
            
            # Clean up temporary files
            try:
                shutil.rmtree(temp_dir)
            except Exception as cleanup_error:
                print(f"⚠️ Failed to cleanup temp directory: {cleanup_error}")
        
    except Exception as e:
        print(f"❌ Error generating image from prompt: {e}")
        return None

async def luma_ray_animate_image_database_only(image_s3_url: str, request: LumaRayRequest, job_id: str) -> dict:
    """Animate an image using Luma Ray 2 I2V API - Database-only storage with S3 URLs
    
    Args:
        image_s3_url: S3 URL of the input image
        request: LumaRayRequest containing animation parameters
        job_id: Job ID for tracking and output organization
        
    Returns:
        Dictionary with animation S3 URLs and metadata
    """
    try:
        # Get API key from environment
        WAVESPEED_API_KEY = os.environ.get("WAVESPEED_API_KEY")
        if not WAVESPEED_API_KEY:
            raise ValueError("WAVESPEED_API_KEY not found in environment variables")
        
        print(f"🎬 Starting database-only Luma Ray 2 I2V animation for job {job_id}")
        print(f"📝 Animation prompt: {request.prompt}")
        print(f"📐 Video size: {request.size}")
        print(f"⏱️ Duration: {request.duration}s")
        print(f"🔗 Input image S3 URL: {image_s3_url[:50]}...")
        
        # Download image from S3 to temporary file for processing
        import tempfile
        temp_image_fd, temp_image_path = tempfile.mkstemp(suffix=".png")
        
        try:
            # Download image from S3
            response = requests.get(image_s3_url, timeout=30)
            response.raise_for_status()
            
            with os.fdopen(temp_image_fd, 'wb') as temp_file:
                temp_file.write(response.content)
            
            print(f"✅ Downloaded image from S3 to temp file: {temp_image_path}")
            
            # Prepare API request
            headers = {"Authorization": f"Bearer {WAVESPEED_API_KEY}", "Content-Type": "application/json"}
            
            # Encode image as base64
            with open(temp_image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("ascii")
                image_data = f"data:image/png;base64,{img_b64}"
            
            payload = {
                "prompt": request.prompt,
                "size": request.size,
                "duration": str(request.duration),
                "image": image_data
            }
            
            # Submit animation request
            submit_url = "https://api.wavespeed.ai/api/v3/luma/ray-2-i2v"
            start_time = time.time()
            
            print(f"📤 Submitting animation request to Luma Ray 2 I2V API...")
            update_job_status(job_id, "processing", 30, "Submitting to Luma Ray 2 API...")
            
            # Submit the request
            response = requests.post(submit_url, headers=headers, json=payload, timeout=300)
            if response.status_code != 200:
                raise Exception(f"Luma Ray API error: {response.status_code}, {response.text}")
            
            resp = response.json()
            api_job_id = resp.get("data", {}).get("id") or resp.get("id")
            
            if not api_job_id:
                raise Exception(f"Unexpected submit response: {json.dumps(resp, indent=2)}")
            
            print(f"✅ Animation job submitted: {api_job_id}")
            
            # Poll for completion
            update_job_status(job_id, "processing", 40, f"Polling animation job {api_job_id}...")
            
            result_url = f"https://api.wavespeed.ai/api/v3/predictions/{api_job_id}/result"
            poll_headers = {"Authorization": f"Bearer {WAVESPEED_API_KEY}"}
            max_attempts = 60  # 5 minutes with 5-second intervals
            attempt = 0
            
            while attempt < max_attempts:
                await asyncio.sleep(5)  # Poll every 5 seconds
                
                r = requests.get(result_url, headers=poll_headers, timeout=60)
                if r.status_code != 200:
                    print(f"⚠️ Polling error: {r.status_code}, {r.text}")
                    attempt += 1
                    continue
                
                jr = r.json()
                status = jr.get("data", {}).get("status") or jr.get("status")
                error = jr.get("data", {}).get("error") or jr.get("error")
                outputs = jr.get("data", {}).get("outputs") or jr.get("outputs") or []
                
                print(f"📊 Animation status: {status} (attempt {attempt + 1})")
                update_job_status(job_id, "processing", 50 + (attempt * 2), f"Animation in progress: {status}")
                
                if status in ("completed", "succeeded"):
                    if not outputs:
                        raise Exception(f"Completed but no outputs: {json.dumps(jr, indent=2)}")
                    
                    # Get video URL from outputs
                    video_url = outputs[0] if isinstance(outputs, list) else outputs
                    if not video_url:
                        raise Exception("No video URL in completed response")
                    
                    print(f"✅ Animation completed! Video URL: {video_url[:50]}...")
                    
                    # Download and upload video to S3
                    print(f"📥 Downloading video from Luma API...")
                    video_response = requests.get(video_url, timeout=300)  # 5 minutes for video download
                    if video_response.status_code != 200:
                        raise Exception(f"Failed to download video: {video_response.status_code}")
                    
                    video_content = video_response.content
                    
                    # Upload video to S3
                    print(f"📤 Uploading video to S3...")
                    timestamp = int(time.time())
                    video_filename = f"{job_id}_{timestamp}_animation.mp4"
                    video_s3_key = f"assets/{job_id}/animations/{video_filename}"
                    
                    # Create temporary video file for S3 upload
                    temp_video_fd, temp_video_path = tempfile.mkstemp(suffix=".mp4")
                    try:
                        with os.fdopen(temp_video_fd, 'wb') as temp_video_file:
                            temp_video_file.write(video_content)
                        
                        # Upload to S3
                        s3_client = boto3.client("s3", region_name="us-west-1")
                        s3_client.upload_file(
                            temp_video_path,
                            S3_BUCKET,
                            video_s3_key,
                            ExtraArgs={"ContentType": "video/mp4"}
                        )
                        
                        video_s3_url = f"https://{S3_BUCKET}.s3.us-west-1.amazonaws.com/{video_s3_key}"
                        print(f"✅ Video uploaded to S3: {video_s3_url}")
                        
                    except Exception as s3_error:
                        raise Exception(f"Failed to upload video to S3: {s3_error}")
                    finally:
                        try:
                            os.unlink(temp_video_path)
                        except:
                            pass
                    
                    # Handle GIF creation if requested
                    gif_s3_url = None
                    gif_error = None
                    
                    if request.also_make_gif:
                        try:
                            print(f"🎬 Creating GIF from video...")
                            
                            # Create temporary video file again for GIF conversion
                            temp_video_for_gif_fd, temp_video_for_gif_path = tempfile.mkstemp(suffix=".mp4")
                            temp_gif_fd, temp_gif_path = tempfile.mkstemp(suffix=".gif")
                            
                            try:
                                with os.fdopen(temp_video_for_gif_fd, 'wb') as temp_video_file:
                                    temp_video_file.write(video_content)
                                
                                # Close the gif file descriptor but keep the path
                                os.close(temp_gif_fd)
                                
                                # Convert to GIF using ffmpeg
                                ffmpeg_cmd = [
                                    "ffmpeg", "-i", temp_video_for_gif_path,
                                    "-vf", "fps=10,scale=640:-1:flags=lanczos",
                                    "-y", temp_gif_path
                                ]
                                
                                result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
                                
                                if result.returncode == 0 and os.path.exists(temp_gif_path) and os.path.getsize(temp_gif_path) > 0:
                                    # Upload GIF to S3
                                    gif_filename = f"{job_id}_{timestamp}_animation.gif"
                                    gif_s3_key = f"assets/{job_id}/animations/{gif_filename}"
                                    
                                    s3_client.upload_file(
                                        temp_gif_path,
                                        S3_BUCKET,
                                        gif_s3_key,
                                        ExtraArgs={"ContentType": "image/gif"}
                                    )
                                    
                                    gif_s3_url = f"https://{S3_BUCKET}.s3.us-west-1.amazonaws.com/{gif_s3_key}"
                                    print(f"✅ GIF uploaded to S3: {gif_s3_url}")
                                    
                                    # Update job status with GIF S3 URL
                                    update_job_status(job_id, "processing", 90, "GIF animation uploaded to S3", {
                                        "gif_s3_url": gif_s3_url,
                                        "gif_s3_key": gif_s3_key,
                                        "gif_upload_timestamp": timestamp,
                                        "storage_type": "database_s3_only"
                                    })
                                else:
                                    gif_error = f"FFmpeg conversion failed: {result.stderr}"
                            
                            except Exception as gif_exception:
                                gif_error = f"GIF creation error: {gif_exception}"
                            finally:
                                try:
                                    os.unlink(temp_video_for_gif_path)
                                    os.unlink(temp_gif_path)
                                except:
                                    pass
                        
                        except Exception as gif_outer_error:
                            gif_error = f"GIF workflow error: {gif_outer_error}"
                    
                    processing_time = time.time() - start_time
                    
                    return {
                        "success": True,
                        "video_s3_url": video_s3_url,
                        "gif_s3_url": gif_s3_url,
                        "gif_error": gif_error,
                        "api_job_id": api_job_id,
                        "processing_time": processing_time,
                        "prompt": request.prompt,
                        "size": request.size,
                        "duration": request.duration,
                        "storage_type": "database_s3_only"
                    }
                
                elif status in ("failed", "error"):
                    error_msg = error or "Animation failed"
                    raise Exception(f"Animation failed: {error_msg}")
                
                attempt += 1
            
            # Timeout
            raise Exception("Animation timed out after 5 minutes")
        
        except Exception as api_error:
            raise api_error
        finally:
            # Clean up temporary image file
            try:
                os.unlink(temp_image_path)
            except:
                pass
    
    except Exception as e:
        print(f"❌ Database-only Luma Ray animation error: {e}")
        return {
            "success": False,
            "error": str(e),
            "status": "error",
            "prompt": request.prompt if hasattr(request, 'prompt') else 'unknown',
            "storage_type": "database_s3_only"
        }

# ========= SERVER CONFIGURATION =========

if __name__ == "__main__":
    try:
        import uvicorn
        import os
        import sys
        
        print("🏗️ Starting ForgeRealm Digital Artifacts Engine...")
        print("🚀 Forge your digital artifacts at: http://localhost:8000")
        print("📚 API Documentation: http://localhost:8000/docs")
        print("⚡ Ready to transform your creative vision into stunning 3D artifacts!")
        
        # Ensure the server runs continuously
        print("🔄 Starting uvicorn server in continuous mode...")
        print(f"📍 App object: {app}")
        print(f"📍 App type: {type(app)}")
        
        # Start the server with proper error handling
        uvicorn.run(
            "animation_api:app",  # Use string reference for reload functionality
            host="0.0.0.0",
            port=8000,
            reload=True,
            log_level="info"
        )
        
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user (Ctrl+C)")
        sys.exit(0)
        
    except ImportError as e:
        print(f"❌ Import error: {e}")
        print("💡 Try: pip install uvicorn fastapi")
        sys.exit(1)
        
    except Exception as e:
        print(f"❌ Server startup error: {e}")
        print("🔧 Attempting fallback startup method...")
        
        try:
            # Fallback: Use string reference to app
            print("🔄 Trying string reference method...")
            uvicorn.run(
                "animation_api:app",
                host="0.0.0.0",
                port=8000,
                reload=True,
                log_level="info"
            )
            
        except Exception as fallback_error:
            print(f"❌ Fallback method failed: {fallback_error}")
            print("🆘 Manual command: uvicorn animation_api:app --host 0.0.0.0 --port 8000 --reload")
            sys.exit(1)


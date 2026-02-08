"""
Animation pipeline routes
"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, File, UploadFile, Form
from api.models import *
from api.job_manager import update_job_status, get_job_status
import uuid
import os
import sys

# Import the actual animation pipeline from the monolithic API
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    from animation_api import run_animation_pipeline
    ANIMATION_PIPELINE_AVAILABLE = True
    print("✅ Animation pipeline imported successfully")
except ImportError as e:
    print(f"⚠️ Warning: Could not import run_animation_pipeline: {e}")
    ANIMATION_PIPELINE_AVAILABLE = False

router = APIRouter(prefix="/animate", tags=["Animation"])

@router.post("/image")
async def animate_from_image(
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
    
    # Check if actual pipeline is available
    if not ANIMATION_PIPELINE_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="3D animation pipeline not available. Make sure animation_api.py dependencies are installed."
        )
    
    # Start the ACTUAL 3D generation pipeline in background
    # This will call the real run_animation_pipeline function from animation_api.py
    # 
    # MESHY API POLLING FLOW:
    # 1. Initial status: "queued" → job accepted by modular API
    # 2. Pipeline starts: "processing" (10%) → "Generating 3D model from image..."
    # 3. Meshy 3D generation: Polls Meshy API every 2 seconds
    #    - Status responses from Meshy: "PENDING" (0-90%), "SUCCEEDED" (100%), or "FAILED"
    #    - Example: "Rigging: PENDING (45%)" → "Rigging: PENDING (78%)" → "Rigging: SUCCEEDED (100%)"
    # 4. S3 upload: "processing" (25%) → "Uploading model to cloud storage..."
    # 5. Retexture (optional): "processing" (40%) → "Enhancing textures..."
    #    - Polls Meshy Retexture API: "PENDING" → "SUCCEEDED"
    # 6. Rigging: "processing" (45-60%) → "Creating character rig..."
    #    - Polls Meshy Rigging API: "Rigging: PENDING (X%)" → "Rigging: SUCCEEDED (100%)"
    # 7. Animations: "processing" (70-90%) → "Creating animation {action_id}..."
    #    - Polls Meshy Animation API for each animation
    # 8. Complete: "completed" (100%) → Real GLB URLs returned
    #
    # The workflow will see these real-time updates from Meshy's actual generation status!
    
    print(f"🎯 [JOB {job_id}] Starting 3D generation pipeline:")
    print(f"   → Input: {input_image_path}")
    print(f"   → Animations: {anim_ids}")
    print(f"   → Height: {height_meters}m, FPS: {fps}")
    print(f"   → Retexture: {use_retexture}, Skip confirmation: {skip_confirmation}")
    print(f"   → Will poll Meshy API for real-time status updates")
    
    background_tasks.add_task(
        run_animation_pipeline,
        job_id,
        input_image_path,
        anim_ids,
        height_meters,
        fps,
        use_retexture,
        skip_confirmation
    )
    
    return {
        "forge_id": job_id, 
        "job_id": job_id,
        "status": "queued", 
        "message": "🚀 3D generation started - will poll Meshy API for completion status",
        "note": "Check /status/{job_id} endpoint to see real-time Meshy API status updates"
    }


@router.post("/prompt")
async def animate_from_prompt(
    request: PromptRequest,
    background_tasks: BackgroundTasks
):
    """Forge artifact from text prompt."""
    job_id = str(uuid.uuid4())
    
    update_job_status(job_id, "queued", 0, "Prompt animation queued")
    
    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Vision forge started"
    }

@router.post("/coordinates")
async def animate_from_coordinates(
    request: CoordinatesRequest,
    background_tasks: BackgroundTasks
):
    """Create digital twin from GPS coordinates."""
    job_id = str(uuid.uuid4())
    
    update_job_status(job_id, "queued", 0, "Coordinates animation queued")
    
    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Coordinates animation started"
    }

    job_id = str(uuid.uuid4())
    
    update_job_status(job_id, "queued", 0, "Location forge queued")
    
    return {
        "job_id": job_id,
        "status": "queued",
        "message": "Digital twin creation started"
    }

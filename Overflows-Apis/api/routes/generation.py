"""
Image generation routes using Gemini and Replicate models
"""
import os
import time
import uuid
import base64
import tempfile
import requests
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, Form, HTTPException, File, UploadFile
from fastapi.responses import JSONResponse
import replicate

from api.job_manager import update_job_status
from api.s3_utils import upload_asset_to_s3

# Thread pool for long-running operations
thread_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="optimization")

# Import mapapi for 3D optimization
try:
    import mapapi
except ImportError:
    print("⚠️ Warning: mapapi not available. 3D optimization endpoint will not work.")
    mapapi = None

router = APIRouter()

# Environment configuration
USE_GEMINI = os.getenv("USE_GEMINI", "0") == "1"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY")

if REPLICATE_API_KEY:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_KEY


@router.post("/gen/gemini")
async def gen_gemini(
    prompt: str = Form(...),
    k: int = Form(3),
    job_id: str = Form(None),
    use_rag: bool = Form(False),
    reference_image_url: Optional[str] = Form(None),
):
    """
    Generate images using Gemini 2.5 Flash with optional RAG
    
    Parameters:
    - prompt: Text description of the image to generate
    - k: Number of reference images to retrieve (for RAG)
    - job_id: Optional job ID for tracking
    - use_rag: Whether to use RAG (Retrieval-Augmented Generation)
    - reference_image_url: Optional reference image URL
    """
    
    if not USE_GEMINI:
        raise HTTPException(400, "Gemini generation is disabled. Set USE_GEMINI=1 in .env file")
    
    if not GEMINI_API_KEY:
        raise HTTPException(400, "GEMINI_API_KEY not configured in .env file")
    
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
    except ImportError:
        raise HTTPException(500, "google-generativeai package not installed")
    
    # Generate job ID if not provided
    if not job_id:
        job_id = str(uuid.uuid4())
    
    print(f"🎨 Starting image generation for job {job_id}")
    print(f"📝 Prompt: {prompt}")
    
    # Create job status
    update_job_status(
        job_id=job_id,
        status="processing",
        progress=30,
        message="Generating image with Gemini...",
        result={
            "job_type": "image_generation",
            "prompt": prompt
        }
    )
    
    try:
        # Use Gemini model for image generation
        GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-image")
        model = genai.GenerativeModel(GEMINI_MODEL)
        
        enhanced_prompt = f"""
Generate a high-quality image based on this description: {prompt}

The generated image should:
1. Be visually appealing and well-composed
2. Match the description accurately
3. Have good lighting and artistic quality
4. Be suitable for use as a reference or inspiration

Description: {prompt}
"""
        
        print(f"🤖 Calling Gemini for image generation...")
        
        # Generate image
        response = model.generate_content(enhanced_prompt)
        
        # Extract image from response
        image_bytes = None
        if hasattr(response, 'candidates') and response.candidates:
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'inline_data') and part.inline_data:
                    image_bytes = part.inline_data.data
                    break
        
        if not image_bytes:
            raise HTTPException(500, "Gemini did not return an image")
        
        # Save generated image
        out_dir = "generated"
        os.makedirs(out_dir, exist_ok=True)
        timestamp = int(time.time())
        out_filename = f"gemini_{job_id}_{timestamp}.png"
        out_path = os.path.join(out_dir, out_filename)
        
        with open(out_path, "wb") as f:
            f.write(image_bytes)
        
        print(f"✅ Generated image saved: {out_path}")
        
        # Upload to S3
        s3_url = None
        try:
            print("☁️  Uploading to S3...")
            s3_url = upload_asset_to_s3(
                file_path=out_path,
                asset_type="images",
                job_id=job_id,
                asset_name=out_filename
            )
            print(f"✅ Uploaded to S3: {s3_url}")
        except Exception as s3_error:
            print(f"⚠️  S3 upload failed (continuing with local file): {s3_error}")
        
        # Determine the primary image URL (prefer S3, fallback to local)
        primary_image_url = s3_url if s3_url else f"/{out_path}"
        
        # Update job status
        update_job_status(
            job_id=job_id,
            status="completed",
            progress=100,
            message="Image generated successfully",
            result={
                "job_id": job_id,
                "job_type": "image_generation",
                "status": "completed",
                "prompt": prompt,
                "generated_image_path": out_path,
                "generated_image_s3_url": s3_url,
                "image_url": primary_image_url,
                "s3_url": s3_url,
                "local_path": out_path,
                "gemini_generated_url": primary_image_url
            }
        )
        
        return {
            "job_id": job_id,
            "status": "completed",
            "image_url": primary_image_url,
            "s3_url": s3_url,
            "local_path": out_path,
            "gemini_generated_url": primary_image_url,
            "generated_image_path": out_path,  # Expected by workflow API
            "generated_image_s3_url": s3_url,  # Expected by workflow API
            "prompt": prompt
        }
        
    except Exception as e:
        print(f"❌ Error generating image: {e}")
        update_job_status(
            job_id=job_id,
            status="failed",
            progress=0,
            message=f"Failed to generate image: {str(e)}",
            result={"error": str(e)}
        )
        raise HTTPException(500, f"Failed to generate image: {str(e)}")


@router.post("/gen/nano-banana-pro")
async def gen_nano_banana_pro(
    prompt: str = Form(...),
    resolution: str = Form("2K"),
    image_input: str = Form("[]"),  # JSON string of image URLs
    aspect_ratio: str = Form("4:3"),
    output_format: str = Form("png"),
    safety_filter_level: str = Form("block_only_high"),
    job_id: str = Form(None),
):
    """
    Generate images using Google's Nano Banana Pro via Replicate
    
    Parameters:
    - prompt: Text description of the image to generate
    - resolution: Image resolution - "2K", "4K", or "8K" (default: "2K")
    - image_input: JSON array of image URLs or base64 strings (default: [])
    - aspect_ratio: Output aspect ratio - "1:1", "4:3", "16:9", "9:16", "3:4" (default: "4:3")
    - output_format: Output format - "png", "jpg", or "webp" (default: "png")
    - safety_filter_level: Safety filter - "none", "block_only_high", "block_some" (default: "block_only_high")
    - job_id: Optional job ID for tracking
    
    Example:
    ```
    curl -X POST http://localhost:8000/gen/nano-banana-pro \\
        -F "prompt=dog for game assets in the style of clash royal with transparent background" \\
        -F "resolution=2K" \\
        -F "aspect_ratio=4:3" \\
        -F "output_format=png"
    ```
    """
    
    if not REPLICATE_API_KEY:
        raise HTTPException(400, "REPLICATE_API_KEY not configured in .env file")
    
    # Generate job ID if not provided
    if not job_id:
        job_id = str(uuid.uuid4())
    
    print(f"🍌 Starting Nano Banana Pro generation for job {job_id}")
    print(f"📝 Prompt: {prompt}")
    print(f"⚙️ Resolution: {resolution}, Aspect Ratio: {aspect_ratio}, Format: {output_format}")
    
    # Create job status
    update_job_status(
        job_id=job_id,
        status="processing",
        progress=30,
        message="Generating image with Nano Banana Pro...",
        result={
            "job_type": "nano_banana_pro",
            "prompt": prompt,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "output_format": output_format
        }
    )
    
    try:
        # Parse image_input JSON
        import json
        try:
            image_input_list = json.loads(image_input) if image_input else []
        except json.JSONDecodeError:
            image_input_list = []
        
        print(f"🖼️  Image inputs: {len(image_input_list)} images")
        
        # Run Nano Banana Pro model
        print("🚀 Calling Replicate API...")
        
        # Prepare input parameters
        model_input = {
            "prompt": prompt,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "output_format": output_format,
            "safety_filter_level": safety_filter_level
        }
        
        # Only add image_input if it's not empty
        if image_input_list:
            model_input["image_input"] = image_input_list
            
        print(f"📦 Model input: {json.dumps(model_input)}")
        
        # Use predictions.create for better error handling
        try:
            model = replicate.models.get("google/nano-banana-pro")
            version = model.latest_version
            
            prediction = replicate.predictions.create(
                version=version,
                input=model_input
            )
            
            print(f"⏳ Prediction created: {prediction.id}")
            prediction.wait()
            print(f"✅ Prediction status: {prediction.status}")
            
            if prediction.status == "failed":
                raise ValueError(f"Prediction failed: {prediction.error}")
                
            output = prediction.output
            print(f"📤 Raw output: {output}")
            
        except Exception as replicate_err:
            print(f"⚠️ Replicate API error: {replicate_err}")
            # Fallback to run if models.get fails (e.g. permission/auth issues)
            print("🔄 Falling back to replicate.run()...")
            output = replicate.run(
                "google/nano-banana-pro",
                input=model_input
            )
            print(f"📤 Raw output: {output}")
        
        # Handle different output types from Replicate
        output_url = None
        
        if output is None:
            raise ValueError("No output received from Replicate API (output is None)")

        if isinstance(output, str):
            output_url = output
        elif hasattr(output, 'url'):
            output_url = output.url() if callable(output.url) else output.url
        elif isinstance(output, list):
             if len(output) > 0:
                 first_item = output[0]
                 if isinstance(first_item, str):
                     output_url = first_item
                 elif hasattr(first_item, 'url'):
                     output_url = first_item.url() if callable(first_item.url) else first_item.url
                 elif first_item is not None:
                     output_url = str(first_item)
        elif hasattr(output, '__iter__'):
            # Iterator/Generator
            try:
                # Convert to list to see content
                output_list = list(output)
                if len(output_list) > 0:
                    output_url = str(output_list[0])
            except Exception as e:
                print(f"⚠️ Failed to consume iterator: {e}")
        else:
            output_url = str(output)
            
        if not output_url or output_url == "None":
             raise ValueError(f"No image content found in response. Raw output: {output}")
        
        print(f"📥 Downloading image from: {output_url}")
        
        # Download the generated image
        out_dir = "generated"
        os.makedirs(out_dir, exist_ok=True)
        timestamp = int(time.time())
        out_filename = f"nano_banana_{job_id}_{timestamp}.{output_format}"
        out_path = os.path.join(out_dir, out_filename)
        
        # Download from URL
        import requests
        response = requests.get(output_url, timeout=60)
        response.raise_for_status()
        
        # Write the file to disk
        with open(out_path, "wb") as file:
            file.write(response.content)
        
        print(f"✅ Generated image saved: {out_path}")
        
        # Upload to S3
        s3_url = None
        try:
            print("☁️  Uploading to S3...")
            s3_url = upload_asset_to_s3(
                file_path=out_path,
                asset_type="images",
                job_id=job_id,
                asset_name=out_filename
            )
            print(f"✅ Uploaded to S3: {s3_url}")
        except Exception as s3_error:
            print(f"⚠️  S3 upload failed (continuing with local file): {s3_error}")
        
        # Determine the primary image URL (prefer S3, fallback to local)
        primary_image_url = s3_url if s3_url else f"/{out_path}"
        
        # Update job status
        result_data = {
            "job_id": job_id,
            "job_type": "nano_banana_pro",
            "status": "completed",
            "prompt": prompt,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "output_format": output_format,
            "generated_image_path": out_path,
            "image_url": primary_image_url,
            "s3_url": s3_url,
            "local_path": out_path,
            "replicate_url": output_url
        }
        
        update_job_status(
            job_id=job_id,
            status="completed",
            progress=100,
            message="Image generated successfully with Nano Banana Pro",
            result=result_data
        )
        
        return {
            "job_id": job_id,
            "status": "completed",
            "image_url": primary_image_url,
            "s3_url": s3_url,
            "local_path": out_path,
            "generated_image_path": out_path,
            "replicate_url": output_url,
            "prompt": prompt,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
            "output_format": output_format
        }
        
    except Exception as e:
        print(f"❌ Error generating image with Nano Banana Pro: {e}")
        update_job_status(
            job_id=job_id,
            status="failed",
            progress=0,
            message=f"Failed to generate image: {str(e)}",
            result={"error": str(e)}
        )
        raise HTTPException(500, f"Failed to generate image with Nano Banana Pro: {str(e)}")


@router.post("/optimize/3d")
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


@router.post("/gen/time-travel-scene")
async def time_travel_scene(
    image_url: str = Form(None),
    file: UploadFile = File(None),
    target_date: str = Form(None, description="Target date in ISO format (YYYY-MM-DD)"),
    target_year: int = Form(None, description="Specific year to travel to"),
    time_of_day: str = Form("current", description="Time of day: dawn, morning, noon, afternoon, dusk, night, current"),
    season: str = Form("current", description="Season: spring, summer, fall, winter, current"),
    historical_accuracy: str = Form("moderate", description="Level of historical accuracy: low, moderate, high"),
):
    """
    🕰️ Time Travel Scene Transformation
    
    Transform a scene to a different time period while maintaining the location and composition.
    Uses Gemini 2.5 Flash with the original image as a reference for accurate scene transformation.
    
    Parameters:
    - image_url: URL of the source image (or upload file)
    - target_date: Date to travel to (YYYY-MM-DD format)
    - target_year: Optional specific year (overrides target_date year)
    - time_of_day: Specific time of day (dawn, morning, noon, afternoon, dusk, night, or current to keep same)
    - season: Season to show (spring, summer, fall, winter, or current to auto-detect)
    - historical_accuracy: How historically accurate (low=artistic liberty, moderate=balanced, high=documentary style)
    
    Returns:
    - Transformed scene image that looks like it was taken at the specified time
    """
    
    if not USE_GEMINI or not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Gemini API not configured. Set USE_GEMINI=1 and GEMINI_API_KEY")
    
    if not mapapi:
        raise HTTPException(status_code=503, detail="mapapi module not available for image generation")
    
    job_id = str(uuid.uuid4())
    
    try:
        print(f"\n{'='*80}")
        print(f"🕰️ TIME TRAVEL SCENE TRANSFORMATION - Job ID: {job_id}")
        print(f"{'='*80}")
        
        update_job_status(
            job_id=job_id,
            status="processing",
            progress=10,
            message="Preparing time travel transformation...",
            result={"job_type": "time_travel_scene"}
        )
        
        # Get source image
        if file:
            print(f"📁 Using uploaded file: {file.filename}")
            temp_dir = tempfile.mkdtemp()
            source_image_path = os.path.join(temp_dir, f"source_{file.filename}")
            
            with open(source_image_path, "wb") as f:
                content = await file.read()
                f.write(content)
                
            # Upload to S3
            source_s3_url = upload_asset_to_s3(
                file_path=source_image_path,
                asset_type="time_travel_source",
                job_id=job_id,
                asset_name=file.filename
            )
            
            # Base64 for database
            with open(source_image_path, "rb") as f:
                source_base64 = f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode()}"
                
        elif image_url:
            print(f"🌐 Using image URL: {image_url}")
            response = requests.get(image_url, timeout=30)
            response.raise_for_status()
            
            temp_dir = tempfile.mkdtemp()
            source_image_path = os.path.join(temp_dir, "source_image.jpg")
            
            with open(source_image_path, "wb") as f:
                f.write(response.content)
            
            source_s3_url = image_url
            source_base64 = f"data:image/jpeg;base64,{base64.b64encode(response.content).decode()}"
        else:
            raise HTTPException(status_code=400, detail="Either image_url or file must be provided")
        
        print(f"✅ Source image ready: {source_image_path}")
        
        # Parse date and determine era
        update_job_status(job_id, "processing", 20, "Analyzing target time period...")
        
        import datetime
        
        # If no target_date provided, default to a random historical year
        if not target_date:
            if target_year:
                # Use target_year if provided
                target_date = f"{target_year}-01-01"
            else:
                # Default to 1920 if nothing provided
                target_date = "1920-01-01"
                print(f"⚠️ No target_date provided, defaulting to {target_date}")
        
        try:
            target_dt = datetime.datetime.strptime(target_date, "%Y-%m-%d")
            if target_year:
                target_dt = target_dt.replace(year=target_year)
            
            year = target_dt.year
            month = target_dt.month
            day = target_dt.day
            
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Error parsing date: {str(e)}")
        
        # Determine era and historical context
        current_year = datetime.datetime.now().year
        
        if year < 1800:
            era = "pre-industrial"
            era_description = f"the year {year}, pre-industrial era"
        elif year < 1900:
            era = "19th_century"
            era_description = f"the 19th century ({year})"
        elif year < 1950:
            era = "early_20th_century"
            era_description = f"the early 20th century ({year})"
        elif year < 1980:
            era = "mid_20th_century"
            era_description = f"the mid-20th century ({year})"
        elif year < 2000:
            era = "late_20th_century"
            era_description = f"the late 20th century ({year})"
        elif year < current_year:
            era = "recent_past"
            era_description = f"the recent past ({year})"
        elif year == current_year:
            era = "present"
            era_description = f"present day ({year})"
        else:
            era = "near_future"
            era_description = f"the near future ({year})"
        
        # Auto-detect season from month if set to "current"
        if season == "current":
            season_map = {
                12: "winter", 1: "winter", 2: "winter",
                3: "spring", 4: "spring", 5: "spring",
                6: "summer", 7: "summer", 8: "summer",
                9: "fall", 10: "fall", 11: "fall"
            }
            season = season_map.get(month, "summer")
        
        print(f"📅 Target: {target_date} ({era_description})")
        print(f"🌤️ Season: {season}, Time: {time_of_day}")
        print(f"🎯 Historical accuracy: {historical_accuracy}")
        
        # Analyze source image to understand the scene
        update_job_status(job_id, "processing", 30, "Analyzing source scene...")
        
        print("🔍 Analyzing source image to understand scene composition...")
        analysis_prompt = """Analyze this image and describe:
1. The main location/setting (urban, rural, indoor, outdoor, etc.)
2. Key architectural or natural features
3. The general composition and perspective
4. Any notable objects or elements
5. Current apparent time period/era
6. Current lighting and atmosphere

Be specific but concise. Focus on elements that should be preserved or adapted for time travel."""
        
        scene_analysis = mapapi.analyze_image_with_llama(source_image_path)
        print(f"📋 Scene analysis: {scene_analysis[:300]}...")
        
        # Build comprehensive time travel prompt
        update_job_status(job_id, "processing", 50, f"Generating scene for {era_description}...")
        
        accuracy_instructions = {
            "low": "Take artistic liberties. Focus on creating a visually compelling scene that evokes the era's feeling.",
            "moderate": "Balance historical accuracy with visual appeal. Include era-appropriate details while maintaining artistic quality.",
            "high": "Maintain strict historical accuracy. Research-based details, authentic architecture, period-appropriate vehicles, clothing, and technology only."
        }
        
        time_of_day_lighting = {
            "dawn": "early morning golden hour, soft warm light, long shadows, peaceful atmosphere",
            "morning": "bright morning light, clear and fresh, energetic atmosphere",
            "noon": "midday sun, bright overhead lighting, short shadows, vibrant colors",
            "afternoon": "warm afternoon light, golden tones, relaxed atmosphere",
            "dusk": "golden hour sunset, warm orange and pink tones, dramatic shadows",
            "night": "nighttime scene, appropriate period lighting (gas lamps, electric lights, modern LED, etc.), atmospheric night ambiance",
            "current": "maintain the same lighting and time of day as the source image"
        }
        
        season_details = {
            "spring": "spring season with blooming flowers, fresh green leaves, mild weather, renewed growth",
            "summer": "summer season with lush full foliage, bright sunny conditions, vibrant colors",
            "fall": "autumn season with colorful changing leaves, golden tones, harvest atmosphere",
            "winter": "winter season with bare trees, possible snow, cold crisp atmosphere, winter light",
            "current": "maintain the same season as shown in the source image"
        }
        
        time_travel_prompt = f"""Transform this scene to {era_description}, specifically {month}/{day}/{year}.

REFERENCE IMAGE ANALYSIS:
{scene_analysis}

TIME TRAVEL REQUIREMENTS:
• Target Era: {era_description}
• Historical Accuracy: {accuracy_instructions[historical_accuracy]}
• Season: {season_details[season]}
• Time of Day: {time_of_day_lighting[time_of_day]}

TRANSFORMATION INSTRUCTIONS:
1. PRESERVE: The exact location, camera angle, and overall composition of the scene
2. ADAPT: All visual elements to match {year} authentically
   - Architecture style and condition for {year}
   - Vehicles, transportation methods of {year}
   - Clothing and fashion of {year}
   - Technology level of {year}
   - Street furniture, signs, infrastructure of {year}
   - Materials and construction techniques of {year}

3. ERA-SPECIFIC DETAILS:
   - Include period-appropriate details (vehicles, clothing, signs, technology)
   - Use authentic color palettes and photography styles of {year}
   - Apply period-appropriate wear, patina, and aging to structures
   - Include era-appropriate vegetation growth/landscaping
   - Show period-accurate urban planning and street layout

4. ATMOSPHERE:
   - {time_of_day_lighting[time_of_day]}
   - {season_details[season]}
   - Capture the authentic feeling and mood of {era_description}

5. PHOTOGRAPHIC STYLE:
   - Match the photographic technology and aesthetic of {year}
   - Use period-appropriate image quality, grain, color processing
   - Authentic camera perspective and lens characteristics for the era

Create a photorealistic scene that looks like it was actually photographed on {month}/{day}/{year}.
Maintain the same viewpoint and framing while transforming all elements to be historically accurate for that specific date."""

        print(f"\n📝 Time travel prompt ({len(time_travel_prompt)} chars):")
        print(f"{time_travel_prompt[:500]}...")
        
        # Generate transformed image using Gemini with reference image
        print(f"🎨 Generating time-traveled scene with Gemini...")
        
        # mapapi.generate_image_with_gemini expects:
        # - objects_description: the prompt text
        # - reference_image_path: single path (not list)
        # - output_path: destination file
        transformed_image_path, content_type = mapapi.generate_image_with_gemini(
            objects_description=time_travel_prompt,
            reference_image_path=source_image_path,  # Single path, not list
            output_path=os.path.join(temp_dir, f"time_travel_{job_id}.jpg")
        )
        
        if not transformed_image_path or not os.path.exists(transformed_image_path):
            raise RuntimeError("Failed to generate time-traveled scene")
        
        print(f"✅ Time-traveled scene generated: {transformed_image_path}")
        
        # Upload result to S3
        update_job_status(job_id, "processing", 80, "Uploading transformed scene...")
        
        transformed_s3_url = upload_asset_to_s3(
            file_path=transformed_image_path,
            asset_type="time_travel_result",
            job_id=job_id,
            asset_name=f"time_travel_{year}_{month}_{day}.jpg"
        )
        
        # Base64 for database
        with open(transformed_image_path, "rb") as f:
            transformed_base64 = f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode()}"
        
        print(f"☁️ Uploaded to S3: {transformed_s3_url}")
        
        # Prepare final result
        final_result = {
            "job_type": "time_travel_scene",
            "job_id": job_id,
            "source_image_s3_url": source_s3_url,
            "transformed_image_s3_url": transformed_s3_url,
            "transformed_image_base64": transformed_base64,
            "target_date": target_date,
            "target_year": year,
            "target_month": month,
            "target_day": day,
            "era": era,
            "era_description": era_description,
            "season": season,
            "time_of_day": time_of_day,
            "historical_accuracy": historical_accuracy,
            "scene_analysis": scene_analysis[:500] if scene_analysis else "",
            "prompt_preview": time_travel_prompt[:500],
            "timestamp": int(time.time()),
            "storage_type": "database_s3"
        }
        
        update_job_status(
            job_id=job_id,
            status="completed",
            progress=100,
            message=f"Time travel completed! Scene transformed to {era_description}",
            result=final_result
        )
        
        print(f"\n{'='*80}")
        print(f"✅ TIME TRAVEL COMPLETED - Job ID: {job_id}")
        print(f"{'='*80}")
        
        return JSONResponse(content={
            "status": "completed",
            "job_id": job_id,
            "transformed_image_s3_url": transformed_s3_url,
            "target_date": f"{year}-{month:02d}-{day:02d}",
            "era": era_description,
            "season": season,
            "time_of_day": time_of_day,
            "message": f"Scene successfully transformed to {era_description}",
            "preview": {
                "scene_analysis": scene_analysis[:200] + "..." if scene_analysis and len(scene_analysis) > 200 else scene_analysis,
                "transformation_type": f"{season} {time_of_day} in {year}"
            }
        })
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Time travel error: {e}")
        import traceback
        traceback.print_exc()
        
        update_job_status(
            job_id=job_id,
            status="failed",
            progress=0,
            message=f"Time travel failed: {str(e)}",
            result={
                "job_type": "time_travel_scene",
                "error": str(e)
            }
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/gen/playing-card")
async def generate_playing_card(
    background: str = Form("classic_wood", description="Card background: classic_wood, modern_gradient, gold_luxury, dark_elegant, marble, felt"),
    suit: str = Form("random", description="Card suit: hearts, diamonds, clubs, spades, random"),
    card_style: str = Form("realistic", description="Card style: realistic, fantasy, minimalist, neon, vintage"),
    job_id: str = Form(None),
):
    """
    🎴 Generate Playing Cards - Create randomized playing cards with custom backgrounds
    
    Parameters:
    - background: Card background style (classic_wood, modern_gradient, gold_luxury, dark_elegant, marble, felt)
    - suit: Card suit (hearts ♥, diamonds ♦, clubs ♣, spades ♠, or random)
    - card_style: Art style (realistic, fantasy, minimalist, neon, vintage)
    - job_id: Optional job ID for tracking
    
    Returns:
    - Generated playing card image (standard deck format)
    - Random face value (A, 2-10, J, Q, K)
    - S3 URL for card image
    
    Example:
    ```
    curl -X POST http://localhost:8000/gen/playing-card \\
        -F "background=gold_luxury" \\
        -F "suit=hearts" \\
        -F "card_style=realistic"
    ```
    """
    
    import random
    
    if not USE_GEMINI or not GEMINI_API_KEY:
        raise HTTPException(400, "Gemini API not configured. Set USE_GEMINI=1 and GEMINI_API_KEY in .env")
    
    if not mapapi:
        raise HTTPException(500, "mapapi not available for card generation")
    
    # Generate job ID if not provided
    if not job_id:
        job_id = str(uuid.uuid4())
    
    print(f"\n{'='*80}")
    print(f"🎴 PLAYING CARD GENERATION - Job ID: {job_id}")
    print(f"{'='*80}")
    print(f"Background: {background}")
    print(f"Suit: {suit}")
    print(f"Style: {card_style}")
    
    try:
        # Random card generation
        suits = ["hearts", "diamonds", "clubs", "spades"]
        face_values = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
        
        selected_suit = random.choice(suits) if suit == "random" else suit
        selected_value = random.choice(face_values)
        
        # Suit symbols
        suit_symbols = {
            "hearts": "♥",
            "diamonds": "♦",
            "clubs": "♣",
            "spades": "♠"
        }
        suit_symbol = suit_symbols.get(selected_suit, "♠")
        suit_colors = {
            "hearts": "red",
            "diamonds": "red",
            "clubs": "black",
            "spades": "black"
        }
        suit_color = suit_colors.get(selected_suit, "black")
        
        # Background descriptions
        background_descriptions = {
            "classic_wood": "classic wooden texture with subtle grain, warm brown tones, professional playing card background",
            "modern_gradient": "sleek modern gradient from deep blue to purple, contemporary gaming style",
            "gold_luxury": "luxurious gold filigree patterns on rich burgundy, premium casino style",
            "dark_elegant": "sophisticated dark charcoal with silver accents, elegant and minimalist",
            "marble": "white and gray marble texture, luxury and timeless design",
            "felt": "soft green felt texture like a poker table, authentic casino feel"
        }
        
        background_desc = background_descriptions.get(background, background_descriptions["classic_wood"])
        
        # Build comprehensive card generation prompt
        card_prompt = f"""Generate a professional playing card with the following specifications:

CARD DETAILS:
- Face Value: {selected_value}
- Suit: {selected_suit} ({suit_symbol})
- Suit Color: {suit_color}

DESIGN SPECIFICATIONS:
- Background: {background_desc}
- Art Style: {card_style}
- Format: Standard playing card dimensions (poker size: 3.5" × 2.5" aspect ratio)
- Quality: High resolution, casino-quality print ready

CARD LAYOUT:
1. Top-left corner: Large "{selected_value}{suit_symbol}" with suit color
2. Center: Ornate design featuring suit symbol, balanced and symmetrical
3. Bottom-right corner: "{selected_value}{suit_symbol}" upside down
4. Border: Professional framing with subtle shadows
5. Texture: Anti-glare finish, suitable for actual card stock

ARTISTIC REQUIREMENTS:
- Professional casino card aesthetic
- {suit_color} text and symbols
- {background_desc}
- {card_style} interpretation of the suit symbol
- Perfect symmetry for actual playing card production
- No text except card value and suit symbol
- High contrast for readability

TECHNICAL REQUIREMENTS:
- Resolution: 2400x1600 pixels (300 DPI equivalent)
- Color space: CMYK-ready
- Edge quality: Clean, professional borders
- Surface: Matte finish appearance

Generate this playing card with absolute precision. This is for actual card production quality."""

        print(f"\n🎨 Generated: {selected_value} of {selected_suit} {suit_symbol}")
        print(f"📝 Generating card with {card_style} style...")
        
        # Update job status
        update_job_status(
            job_id=job_id,
            status="processing",
            progress=30,
            message=f"Generating {selected_value}♦ of {selected_suit} with {background} background...",
            result={
                "job_type": "playing_card",
                "suit": selected_suit,
                "value": selected_value,
                "background": background,
                "style": card_style
            }
        )
        
        # Generate card using Gemini
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        
        GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-image")
        model = genai.GenerativeModel(GEMINI_MODEL)
        
        print(f"🤖 Calling Gemini ({GEMINI_MODEL})...")
        response = model.generate_content(card_prompt)
        
        # Extract image from response
        image_bytes = None
        if hasattr(response, 'candidates') and response.candidates:
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'inline_data') and part.inline_data:
                    image_bytes = part.inline_data.data
                    break
        
        if not image_bytes:
            raise HTTPException(500, "Gemini did not return a card image")
        
        # Save card image
        out_dir = "generated"
        os.makedirs(out_dir, exist_ok=True)
        timestamp = int(time.time())
        out_filename = f"card_{selected_value}_{selected_suit}_{job_id}_{timestamp}.png"
        out_path = os.path.join(out_dir, out_filename)
        
        with open(out_path, "wb") as f:
            f.write(image_bytes)
        
        print(f"✅ Card image saved: {out_path}")
        
        # Upload to S3
        s3_url = None
        try:
            print("☁️ Uploading card to S3...")
            s3_url = upload_asset_to_s3(
                file_path=out_path,
                asset_type="playing_cards",
                job_id=job_id,
                asset_name=out_filename
            )
            print(f"✅ Uploaded to S3: {s3_url}")
        except Exception as s3_error:
            print(f"⚠️ S3 upload failed: {s3_error}")
        
        # Determine primary URL
        primary_card_url = s3_url if s3_url else f"/{out_path}"
        
        # Final result
        result_data = {
            "job_id": job_id,
            "job_type": "playing_card",
            "status": "completed",
            "card_value": selected_value,
            "suit": selected_suit,
            "suit_symbol": suit_symbol,
            "suit_color": suit_color,
            "background": background,
            "style": card_style,
            "card_image_path": out_path,
            "card_image_s3_url": s3_url,
            "card_url": primary_card_url,
            "timestamp": timestamp
        }
        
        update_job_status(
            job_id=job_id,
            status="completed",
            progress=100,
            message=f"Playing card generated: {selected_value} of {selected_suit}",
            result=result_data
        )
        
        print(f"\n{'='*80}")
        print(f"✅ CARD GENERATED - Job ID: {job_id}")
        print(f"{'='*80}")
        print(f"Card: {selected_value}{suit_symbol} ({selected_suit})")
        print(f"Style: {card_style} on {background}")
        print(f"S3 URL: {s3_url}")
        
        return {
            "job_id": job_id,
            "status": "completed",
            "card_value": selected_value,
            "suit": selected_suit,
            "suit_symbol": suit_symbol,
            "card_image_url": primary_card_url,
            "s3_url": s3_url,
            "local_path": out_path,
            "background": background,
            "style": card_style,
            "message": f"Playing card generated: {selected_value}♦ of {selected_suit}"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Card generation error: {e}")
        import traceback
        traceback.print_exc()
        
        update_job_status(
            job_id=job_id,
            status="failed",
            progress=0,
            message=f"Card generation failed: {str(e)}",
            result={"job_type": "playing_card", "error": str(e)}
        )
        raise HTTPException(500, f"Card generation failed: {str(e)}")


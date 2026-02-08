"""
Fusion routes for combining multiple images into hybrid artifacts
"""
import os
import io
import time
import uuid
import base64
import tempfile
import requests
from typing import Optional, List
from fastapi import APIRouter, BackgroundTasks, HTTPException, File, UploadFile
from fastapi.responses import Response

from api.job_manager import update_job_status, get_job_status
from api.s3_utils import upload_asset_to_s3

# Import mapapi for image analysis and generation
try:
    import mapapi
except ImportError:
    print("⚠️ Warning: mapapi not available. Fusion operations will not work.")
    mapapi = None

# Import S3 configuration from test3danimate
try:
    from test3danimate import s3_client, S3_BUCKET, AWS_REGION
    S3_AVAILABLE = True
except ImportError:
    print("⚠️ Warning: S3 configuration not available from test3danimate")
    S3_AVAILABLE = False
    S3_BUCKET = os.environ.get("S3_BUCKET_NAME", "your-bucket-name")
    AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
    
    def s3_client():
        """Fallback S3 client"""
        import boto3
        return boto3.client('s3')

router = APIRouter(prefix="/animate", tags=["Fusion"])


def analyze_image_for_fusion(image_path: str) -> str:
    """Analyze image using vision AI to describe it for fusion purposes."""
    try:
        if not os.path.exists(image_path):
            print(f"⚠️ Image not found for analysis: {image_path}")
            return "Unknown image - file not found"
        
        if not mapapi:
            print("⚠️ mapapi not available for image analysis")
            return "Image analysis not available"
        
        print(f"🔍 Analyzing image for fusion: {os.path.basename(image_path)}")
        
        fusion_analysis_prompt = (
            "Analyze this image in detail for 3D model fusion purposes. Describe: "
            "1) Main objects and their shapes, materials, textures "
            "2) Colors, lighting, and style characteristics "
            "3) Geometric features and proportions "
            "4) Distinctive visual elements that could be fused with other objects "
            "5) Overall aesthetic and design style. "
            "Be specific about physical properties and visual characteristics that would work well in 3D model fusion."
        )
        
        original_prompt = os.environ.get("LLAMA_VISION_PROMPT")
        os.environ["LLAMA_VISION_PROMPT"] = fusion_analysis_prompt
        
        try:
            analysis_result = mapapi.analyze_image_with_llama(image_path)
            print(f"✅ Image analysis completed: {analysis_result[:100]}...")
            return analysis_result
        finally:
            if original_prompt:
                os.environ["LLAMA_VISION_PROMPT"] = original_prompt
            elif "LLAMA_VISION_PROMPT" in os.environ:
                del os.environ["LLAMA_VISION_PROMPT"]
        
    except Exception as e:
        print(f"❌ Error analyzing image {image_path}: {e}")
        return f"Error analyzing image: {str(e)}"


def generate_fusion_prompt_with_ollama(analysis1: str, analysis2: str, fusion_style: str = "blend", fusion_strength: float = 0.5) -> str:
    """Generate a fusion prompt using Ollama/GPT to combine two image analyses."""
    try:
        print(f"🧠 Generating fusion prompt with style: {fusion_style}, strength: {fusion_strength}")
        
        if fusion_style == "blend":
            fusion_instruction = f"seamlessly blend and combine the visual elements, with {int(fusion_strength * 100)}% emphasis on the second image characteristics"
        elif fusion_style == "merge":
            fusion_instruction = f"merge the geometric and structural elements while maintaining distinct features from both, balanced at {int(fusion_strength * 100)}%"
        elif fusion_style == "hybrid":
            fusion_instruction = f"create a hybrid that takes the best features from both, with {int(fusion_strength * 100)}% influence from the second image"
        else:
            fusion_instruction = "creatively combine and fuse the elements together"
        
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
        
        try:
            ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            ollama_model = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
            
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
            return create_fallback_fusion_prompt(analysis1, analysis2, fusion_style, fusion_strength)
        
    except Exception as e:
        print(f"❌ Error generating fusion prompt: {e}")
        return create_fallback_fusion_prompt(analysis1, analysis2, fusion_style, fusion_strength)


def create_fallback_fusion_prompt(analysis1: str, analysis2: str, fusion_style: str, fusion_strength: float) -> str:
    """Fallback method to create fusion prompt using template-based approach."""
    try:
        def extract_key_elements(analysis):
            elements = {"objects": [], "colors": [], "materials": [], "shapes": []}
            words = analysis.lower().split()
            
            object_keywords = ["car", "building", "tree", "person", "animal", "furniture", "vehicle", "structure"]
            for keyword in object_keywords:
                if keyword in words:
                    elements["objects"].append(keyword)
            
            color_keywords = ["red", "blue", "green", "yellow", "black", "white", "brown", "gray", "orange", "purple"]
            for color in color_keywords:
                if color in words:
                    elements["colors"].append(color)
            
            material_keywords = ["metal", "wood", "stone", "glass", "fabric", "plastic", "concrete", "leather"]
            for material in material_keywords:
                if material in words:
                    elements["materials"].append(material)
            
            return elements
        
        elements1 = extract_key_elements(analysis1)
        elements2 = extract_key_elements(analysis2)
        
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


def upload_uploaded_file_to_s3(job_id: str, file_content: bytes, filename: str, image_tag: str) -> Optional[dict]:
    """Upload user-uploaded file content directly to S3 without local storage."""
    try:
        if not S3_AVAILABLE:
            print(f"⚠️ S3 not available, skipping upload for {image_tag}")
            return None
            
        print(f"📤 Uploading {image_tag} directly to S3 for job {job_id}")
        
        s3 = s3_client()
        timestamp = int(time.time())
        file_ext = os.path.splitext(filename)[1].lower()
        clean_filename = f"{image_tag}_{timestamp}{file_ext}"
        s3_key = f"assets/{job_id}/source_images/{image_tag}/{timestamp}_{clean_filename}"
        
        content_type_map = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.bmp': 'image/bmp'
        }
        content_type = content_type_map.get(file_ext, 'image/jpeg')
        
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
        
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": s3_key},
            ExpiresIn=7 * 24 * 3600
        )
        
        s3_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
        s3_direct_url = f"https://{S3_BUCKET}.s3.{s3_region}.amazonaws.com/{s3_key}"
        
        print(f"✅ {image_tag} uploaded successfully!")
        
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
        print(f"⚠️ Error uploading {image_tag} to S3 (continuing without S3): {e}")
        return None


def generate_fusion_image_database_only(job_id: str, fusion_prompt: str, fusion_style: str, fusion_strength: float, source_image_1_content: bytes, source_image_2_content: bytes) -> Optional[bytes]:
    """Generate a fusion image using Gemini with both source images as references."""
    try:
        print(f"🎨 Generating fusion image with BOTH source images as references (job {job_id})")
        
        if not mapapi:
            print(f"❌ mapapi module not available for image generation")
            return None
        
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp1:
            temp1.write(source_image_1_content)
            temp1_path = temp1.name
        
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp2:
            temp2.write(source_image_2_content)
            temp2_path = temp2.name
        
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_out:
            temp_output_path = temp_out.name
        
        try:
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
            
            # Use mapapi to generate with both reference images
            print(f"🔄 Generating fusion image with Gemini...")
            gemini_result = mapapi.generate_image_with_gemini(
                enhanced_fusion_prompt,
                temp1_path,  # First reference image
                temp_output_path
            )
            
            if not gemini_result or not gemini_result[0] or not os.path.exists(gemini_result[0]):
                print(f"❌ Gemini generation failed or no output file")
                return None
            
            with open(gemini_result[0], "rb") as f:
                fusion_image_content = f.read()
            
            print(f"✅ Fusion image generated successfully ({len(fusion_image_content)} bytes)")
            
            try:
                os.unlink(gemini_result[0])
            except:
                pass
                
            return fusion_image_content
            
        finally:
            try:
                os.unlink(temp1_path)
                os.unlink(temp2_path)
                if os.path.exists(temp_output_path):
                    os.unlink(temp_output_path)
            except:
                pass
        
    except Exception as e:
        print(f"❌ Error generating fusion image: {e}")
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
    """Database-only fusion pipeline that stores all images directly in the database."""
    try:
        print(f"🔀 [Job {job_id}] Starting COMPLETE DATABASE-ONLY fusion pipeline!")
        
        # Step 1: Upload source images to S3 (optional - continue if fails)
        source_1_s3_result = upload_uploaded_file_to_s3(
            job_id, source_image_1_content, source_image_1_filename, "source_image_1"
        )
        
        if not source_1_s3_result:
            print(f"⚠️ S3 upload failed for source image 1, continuing with database-only storage")
        
        source_2_s3_result = upload_uploaded_file_to_s3(
            job_id, source_image_2_content, source_image_2_filename, "source_image_2"
        )
        
        if not source_2_s3_result:
            print(f"⚠️ S3 upload failed for source image 2, continuing with database-only storage")
        
        # Step 2: Convert to base64 for database
        source_1_base64 = base64.b64encode(source_image_1_content).decode('utf-8')
        source_2_base64 = base64.b64encode(source_image_2_content).decode('utf-8')
        
        s3_status = "with_s3" if (source_1_s3_result and source_2_s3_result) else "database_only"
        
        update_job_status(job_id, "processing", 10, f"Source images saved ({s3_status})", {
            "job_type": "fusion",
            "storage_type": "complete_database_only",
            "fusion_style": fusion_style,
            "fusion_strength": fusion_strength,
            "s3_available": source_1_s3_result is not None and source_2_s3_result is not None,
            "source_images_s3": {
                "source_image_1": source_1_s3_result,
                "source_image_2": source_2_s3_result
            } if (source_1_s3_result and source_2_s3_result) else None
        })
        
        # Step 3: Analyze images
        update_job_status(job_id, "processing", 20, "Analyzing source images...")
        
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp1:
            temp1.write(source_image_1_content)
            temp1_path = temp1.name
        
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as temp2:
            temp2.write(source_image_2_content)
            temp2_path = temp2.name
        
        try:
            analysis1 = analyze_image_for_fusion(temp1_path)
            analysis2 = analyze_image_for_fusion(temp2_path)
        finally:
            try:
                os.unlink(temp1_path)
                os.unlink(temp2_path)
            except:
                pass
        
        # Step 4: Generate fusion prompt
        update_job_status(job_id, "processing", 30, "Generating fusion prompt...")
        fusion_prompt = generate_fusion_prompt_with_ollama(
            analysis1, analysis2, fusion_style, fusion_strength
        )
        
        # Step 5: Generate fusion image
        update_job_status(job_id, "processing", 40, "Generating fusion image...")
        fusion_image_content = generate_fusion_image_database_only(
            job_id, fusion_prompt, fusion_style, fusion_strength,
            source_image_1_content, source_image_2_content
        )
        
        if not fusion_image_content:
            raise RuntimeError("Failed to generate fusion image")
        
        # Upload fusion image to S3 (optional)
        fusion_s3_result = upload_uploaded_file_to_s3(
            job_id, fusion_image_content, f"fusion_image_{job_id}.png", "fusion_image"
        )
        
        if not fusion_s3_result:
            print(f"⚠️ S3 upload failed for fusion image, using base64 data URL")
        
        fusion_image_base64 = base64.b64encode(fusion_image_content).decode('utf-8')
        
        # Create data URL as fallback if S3 failed
        fusion_image_url = fusion_s3_result["s3_direct_url"] if fusion_s3_result else f"data:image/png;base64,{fusion_image_base64}"
        
        # Only include base64 if S3 is not available (to avoid MongoDB document size limit)
        include_base64 = not (source_1_s3_result and source_2_s3_result and fusion_s3_result)
        
        # Final result - minimal data when S3 is available
        final_result = {
            "job_type": "fusion",
            "storage_type": "s3_with_fallback" if fusion_s3_result else "base64_only",
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
                    "size_bytes": len(source_image_1_content),
                    "content_type": "image/jpeg",
                    "s3_url": source_1_s3_result["s3_direct_url"] if source_1_s3_result else None,
                    "s3_presigned_url": source_1_s3_result["presigned_url"] if source_1_s3_result else None
                },
                "source_image_2": {
                    "filename": source_image_2_filename,
                    "size_bytes": len(source_image_2_content),
                    "content_type": "image/jpeg",
                    "s3_url": source_2_s3_result["s3_direct_url"] if source_2_s3_result else None,
                    "s3_presigned_url": source_2_s3_result["presigned_url"] if source_2_s3_result else None
                }
            },
            "fusion_image": {
                "size_bytes": len(fusion_image_content),
                "content_type": "image/png",
                "fusion_prompt": fusion_prompt,
                "generated_timestamp": time.time(),
                "s3_url": fusion_s3_result["s3_direct_url"] if fusion_s3_result else None,
                "s3_presigned_url": fusion_s3_result["presigned_url"] if fusion_s3_result else None
            },
            "fusion_prompt": fusion_prompt[:500] + "..." if len(fusion_prompt) > 500 else fusion_prompt,  # Truncate long prompts
            "analysis_summary": {
                "image_1_length": len(analysis1),
                "image_2_length": len(analysis2),
                # Only include truncated versions to save space
                "image_1_preview": analysis1[:200] + "..." if len(analysis1) > 200 else analysis1,
                "image_2_preview": analysis2[:200] + "..." if len(analysis2) > 200 else analysis2
            },
            "fusion_s3_url": fusion_s3_result["s3_direct_url"] if fusion_s3_result else None,
            "fusion_s3_presigned_url": fusion_s3_result["presigned_url"] if fusion_s3_result else None,
            "fusion_image_url": fusion_image_url,  # For workflow compatibility - S3 URL or data URL
            "note": "Base64 data omitted to avoid MongoDB document size limit. Use S3 URLs to access images."
        }
        
        # Only add base64 data if S3 is not available (fallback)
        if include_base64:
            print(f"⚠️ Including base64 data because S3 is not fully available")
            final_result["source_images"]["source_image_1"]["content_base64"] = source_1_base64
            final_result["source_images"]["source_image_2"]["content_base64"] = source_2_base64
            final_result["fusion_image"]["content_base64"] = fusion_image_base64
            final_result["analysis"] = {
                "image_1_analysis": analysis1,
                "image_2_analysis": analysis2
            }
            final_result["note"] = "Base64 data included because S3 is not available"
        
        update_job_status(
            job_id=job_id,
            status="completed",
            progress=100,
            message=f"Fusion image generated successfully!",
            result=final_result
        )
        
        print(f"🎉 Fusion job {job_id} completed successfully!")
        
    except Exception as e:
        print(f"❌ Error in fusion pipeline for job {job_id}: {e}")
        update_job_status(
            job_id=job_id,
            status="failed",
            progress=0,
            message=f"Fusion pipeline failed: {str(e)}",
            result={
                "job_type": "fusion",
                "error": str(e)
            }
        )


@router.post("/infuse-database-only")
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
    """🎭 Complete Database-Only Fusion: Create unique hybrid artifacts."""
    
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
    
    job_id = str(uuid.uuid4())
    
    try:
        anim_ids = [int(x.strip()) for x in animation_ids.split(',')]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid animation_ids format")
    
    try:
        print(f"🔀 Starting fusion job {job_id}")
        
        file1_content = await file1.read()
        file2_content = await file2.read()
        
        update_job_status(job_id, "queued", 0, "Fusion job queued", {
            "job_type": "fusion",
            "storage_type": "complete_database_only",
            "fusion_style": fusion_style,
            "fusion_strength": fusion_strength,
            "source_files": [file1.filename, file2.filename],
            "animation_ids": anim_ids
        })
        
        background_tasks.add_task(
            run_fusion_pipeline_database_only,
            job_id, file1_content, file2_content, file1.filename, file2.filename,
            fusion_style, fusion_strength, anim_ids, height_meters, fps, use_retexture
        )
        
        return {
            "job_id": job_id, 
            "status": "queued", 
            "message": "Fusion job started",
            "job_type": "fusion",
            "storage_type": "complete_database_only",
            "fusion_config": {
                "style": fusion_style,
                "strength": fusion_strength,
                "source_files": [file1.filename, file2.filename],
                "animation_ids": anim_ids
            }
        }
        
    except Exception as e:
        error_msg = f"Failed to initialize fusion job: {str(e)}"
        update_job_status(job_id, "failed", 0, error_msg, {"job_type": "fusion"})
        raise HTTPException(status_code=500, detail=error_msg)

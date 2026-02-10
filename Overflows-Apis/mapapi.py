import os
import sys
import time
import json
import mimetypes
import threading
from typing import Dict, Optional, Tuple
import requests
import base64
from dotenv import load_dotenv

# Gracefully handle missing google.generativeai
try:
    import google.generativeai as genai
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    GEMINI_AVAILABLE = True
except ImportError:
    print("⚠️ Warning: google.generativeai not installed. Gemini features will not work.")
    genai = None
    GEMINI_AVAILABLE = False

# Configure the client with your API key
# client = genai.Client()

# ====================== CONFIG & SETUP ======================
load_dotenv()  # reads .env in the current directory if present

SEGMIND_API_KEY = os.getenv("SEGMIND_API_KEY")
if not SEGMIND_API_KEY:
    raise SystemExit("❌ SEGMIND_API_KEY not set. Put it in your environment or .env")

# Gemini is optional for enhanced descriptions
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Google Maps API key for Street View
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY") or "REPLACE_ME"  # set in .env
LAT = float(os.getenv("STREET_LAT", "40.6889928"))
LNG = float(os.getenv("STREET_LNG", "-74.0440478"))
IMG_SIZE = os.getenv("STREET_IMG_SIZE", "640x640")  # 640x640 is fine for Street View Static

STREETVIEW_URL = (
    f"https://maps.googleapis.com/maps/api/streetview"
    f"?size={IMG_SIZE}&location={LAT},{LNG}&key={GOOGLE_MAPS_API_KEY}"
)

SEGMIND_ENDPOINT = os.getenv("SEGMIND_ENDPOINT", "https://api.segmind.com/v1/hunyuan3d-2.1")
OUT_GLBF = os.getenv("OUT_GLTF", "streetview_3d_model.glb")
OUT_JSON = os.getenv("OUT_META", "3d_model_output.json")

# Generation params—optimized for faster processing while maintaining quality
GEN_PARAMS = {
    "seed": int(os.getenv("SEED", "42")),
    "steps": int(os.getenv("STEPS", "25")),  # Reduced from 30 to 25 for faster processing
    "num_chunks": int(os.getenv("NUM_CHUNKS", "6000")),  # Reduced from 8000 to 6000
    "max_facenum": int(os.getenv("MAX_FACENUM", "15000")),  # Reduced from 20000 to 15000
    "guidance_scale": float(os.getenv("GUIDANCE_SCALE", "7.5")),
    "generate_texture": os.getenv("GENERATE_TEXTURE", "true").lower() == "true",
    "octree_resolution": int(os.getenv("OCTREE_RESOLUTION", "192")),  # Reduced from 256 to 192
    "remove_background": os.getenv("REMOVE_BACKGROUND", "true").lower() == "true",
    "texture_resolution": int(os.getenv("TEXTURE_RESOLUTION", "2048")),  # Reduced from 4096 to 2048
    "bake_normals": os.getenv("BAKE_NORMALS", "true").lower() == "true",
    "bake_ao": os.getenv("BAKE_AO", "true").lower() == "true",
}

HEADERS = {
    "x-api-key": SEGMIND_API_KEY,
    # Prefer binary stream; server may still choose JSON with a link.
    "Accept": "application/octet-stream"
}

def find_nearest_streetview(
    lat: float,
    lng: float,
    api_key: str,
    start_radius: int = 50,      # meters
    step: int = 50,              # how much to expand per attempt
    max_radius: int = 3000,      # hard cap
    source: str = "default",     # or "outdoor" to avoid indoor panos
    timeout: int = 10
) -> Optional[Dict]:
    """
    Returns a dict with {'pano_id', 'lat', 'lng', 'date'} of the nearest pano,
    or None if nothing was found up to max_radius.
    """
    radius = start_radius
    session = requests.Session()

    while radius <= max_radius:
        url = (
            "https://maps.googleapis.com/maps/api/streetview/metadata"
            f"?location={lat},{lng}&radius={radius}&source={source}&key={api_key}"
        )
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
        meta = r.json()

        if meta.get("status") == "OK":
            loc = meta.get("location", {})
            return {
                "pano_id": meta.get("pano_id"),
                "lat": loc.get("lat"),
                "lng": loc.get("lng"),
                "date": meta.get("date"),
                "radius_used": radius
            }

        # ZERO_RESULTS → increase search radius and try again
        if meta.get("status") == "ZERO_RESULTS":
            radius += step
            continue

        # Any other status: break early (e.g., REQUEST_DENIED, OVER_QUERY_LIMIT)
        raise RuntimeError(f"Street View metadata error: {meta}")

    return None


def build_streetview_image_url(
    api_key: str,
    pano_id: Optional[str] = None,
    latlng: Optional[Tuple[float, float]] = None,
    size: str = "640x640",
    heading: Optional[int] = None,
    pitch: Optional[int] = None,
    fov: Optional[int] = None,
    source: str = "default"
) -> str:
    """
    Returns a Street View Static API image URL targeting a specific pano (preferred) or lat/lng.
    """
    base = "https://maps.googleapis.com/maps/api/streetview"
    params = [f"size={size}", f"key={api_key}", f"source={source}"]

    if pano_id:
        params.append(f"pano={pano_id}")
    elif latlng:
        params.append(f"location={latlng[0]},{latlng[1]}")
    else:
        raise ValueError("Provide either pano_id or latlng")

    if heading is not None: params.append(f"heading={heading}")
    if pitch   is not None: params.append(f"pitch={pitch}")
    if fov     is not None: params.append(f"fov={fov}")

    return f"{base}?" + "&".join(params)

# ====================== HEARTBEAT ======================
def _heartbeat(label="Processing on server…"):
    start = time.time()
    while True:
        if getattr(_heartbeat, "stop", False):
            break
        elapsed = int(time.time() - start)
        mins, secs = divmod(elapsed, 60)
        sys.stdout.write(f"\r{label} {mins}m{secs:02d}s")
        sys.stdout.flush()
        time.sleep(1)
    # clear the line
    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()

# ====================== UTILITIES ======================
def download_streetview_image(local_path="streetview.jpg") -> str:
    """Download Google Street View image to local_path."""
    if "REPLACE_ME" in GOOGLE_MAPS_API_KEY:
        print("⚠️  GOOGLE_MAPS_API_KEY is a placeholder. Add your key in .env.")
    print(f"Downloading Street View image for ({LAT}, {LNG}) …")
    r = requests.get(STREETVIEW_URL, timeout=30)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(r.content)
    print(f"✓ Saved Street View to {local_path}")
    return local_path

def analyze_image_with_llama(image_path: str, ollama_url: str = "http://localhost:11434") -> str:
    """Analyze image with Llama 3.2 vision to identify objects."""
    try:
        # Read and encode the image
        with open(image_path, "rb") as img_file:
            img_data = base64.b64encode(img_file.read()).decode('utf-8')
        
        # Prepare the request for Ollama
        payload = {
            "model": "llama3.2-vision:latest",
            "prompt": (
            "You are a 3D character modeling assistant. Look at the provided image and produce a single, concise, highly-detailed description optimized for full 3D reconstruction. "
            "Include estimated gender/age, body type and proportions, precise clothing (layers, fabrics, dominant colors), hairstyle and facial features (shape, eyes, nose, facial hair if any), pose and orientation, facial expression, footwear, accessories, visible material and texture cues (e.g., worn leather, glossy metal, frayed cloth), any asymmetry or damage, and recommended camera views for capture (e.g., front, 3/4, side, top). "
            "Return only one plain English paragraph (no lists, no extra commentary), focused on details a 3D artist or generator would need. Keep it direct and under ~120 words."
            ),
            "images": [img_data],
            "temperature": 0.0,
            "max_new_tokens": 250,
            "stream": False
        }
        
        print("🔍 Analyzing image with Llama 3.2 vision for 3D objects...")
        response = requests.post(f"{ollama_url}/api/generate", json=payload, timeout=60)
        response.raise_for_status()
        
        result = response.json()
        analysis = result.get("response", "building structure")
        
        return analysis
        
    except Exception as e:
        print(f"❌ Error analyzing image with Llama: {e}")
        return "modern building structure"  # fallback

def extract_3d_objects(llama_response: str) -> str:
    """Extract and clean only the 3D-worthy objects from Llama's response."""
    # Convert to lowercase for processing
    response = llama_response.lower().strip()
    
    # Remove common non-3D elements
    exclude_terms = [
        'people', 'person', 'pedestrian', 'human', 'man', 'woman',
        'car', 'vehicle', 'truck', 'bus', 'motorcycle', 'bicycle',
        'traffic', 'sign', 'pole', 'street light', 'lamp post',
        'tree', 'grass', 'plants', 'vegetation', 'leaves',
        'sky', 'cloud', 'sun', 'shadow',
        'pavement', 'asphalt', 'concrete', 'sidewalk',
        'wire', 'cable', 'antenna'
    ]
    
    # Split into potential objects
    import re
    
    # Try to extract objects from various formats
    if ',' in response:
        # Comma-separated list
        objects = [obj.strip() for obj in response.split(',')]
    elif '\n' in response:
        # Line-separated list
        objects = [obj.strip() for obj in response.split('\n') if obj.strip()]
    else:
        # Try to extract from sentences
        objects = re.findall(r'\b(?:building|house|structure|facade|roof|wall|door|window|balcony|tower|apartment|office|shop|store|church|school|residential|commercial)\w*', response)
    
    # Filter out excluded terms and clean
    clean_objects = []
    for obj in objects:
        obj = obj.strip('.-,;:')
        # Skip if contains excluded terms
        if not any(exclude in obj for exclude in exclude_terms):
            # Keep architectural and structural elements
            if any(keep in obj for keep in ['building', 'house', 'structure', 'facade', 'roof', 'wall', 'door', 'window', 'balcony', 'tower', 'apartment', 'office', 'shop', 'store', 'church', 'school', 'residential', 'commercial', 'brick', 'stone', 'concrete', 'wooden', 'glass']):
                clean_objects.append(obj)
    
    # If we have clean objects, join them
    if clean_objects:
        result = ', '.join(clean_objects[:5])  # Limit to top 5 objects
    else:
        # Fallback: try to extract any building-related terms
        building_terms = re.findall(r'\b(?:building|house|structure|facade|roof|wall|door|window)\b', response)
        result = ', '.join(set(building_terms[:3])) if building_terms else "building structure"
    
    return result if result else "modern building"

def generate_image_with_segmind_sdxl(objects_description: str, output_path: str = "segmind_generated.jpg") -> str:
    """Generate an image using Segmind SDXL based on object analysis."""
    if not SEGMIND_API_KEY:
        print("⚠️  Skipping image generation - no SEGMIND API key")
        return None
    
    try:
        # Create a focused prompt for only the 3D objects
        prompt = f"isolated {objects_description}, architectural 3D model, high detail, realistic textures, side elevated view, professional rendering, white background, clean minimal composition"
        
        # Strong negative prompt to remove unwanted elements
        negative_prompt = "people, humans, persons, cars, vehicles, traffic, street signs, poles, trees, vegetation, plants, grass, sky, clouds, wires, cables, street furniture, clutter, background buildings, distant objects, crowds, animals, bicycles, motorcycles"
        
        # Segmind SDXL endpoint
        url = "https://api.segmind.com/v1/sdxl1.0-txt2img"
        
        headers = {'x-api-key': SEGMIND_API_KEY}
        
        data = {
            'prompt': prompt,
            'negative_prompt': negative_prompt,
            'style': 'base',
            'samples': 1,
            'scheduler': 'UniPC',
            'num_inference_steps': 30,  # Increased for better quality
            'guidance_scale': 8.0,      # Increased to follow prompt more strictly
            'strength': 1,
            'seed': 42,
            'img_width': 1024,
            'img_height': 1024,
            'refiner': True
        }
        
        print("🎨 Generating clean 3D-focused image with Segmind SDXL...")
        print(f"Objects to include: {objects_description}")
        print(f"Removing: people, cars, vegetation, street clutter")
        response = requests.post(url, json=data, headers=headers, timeout=120)
        response.raise_for_status()
        
        if response.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(response.content)
            print(f"✓ Clean 3D-focused image saved to {output_path}")
            return output_path
        else:
            print(f"❌ Image generation failed: {response.status_code}")
            print(f"Response: {response.text}")
            return None
        
    except Exception as e:
        print(f"❌ Error generating image: {e}")
        return None

def generate_image_with_gemini(objects_description: str,
                               reference_image_path: str = 'https://lh3.googleusercontent.com/gg/AAHar4cA-DqiQaDkkup6LcCqli7GRm8-8FWnG3qTve6zUmEzcvH-cGeCslT1hCC6IrgSdbCdYUngp-5ySx2AuPKy2yqSPS5_JYJ93nfuY7QsM7LkHT1DmBnpRKrXIrwCxQOPuDmMEmVc0j5Qwc9Pou-wghUmU7mjC1afzx8PlODUKauPT_x3Z_brxX5fSBiBj8EY-Y1s9jQG6lx3zNCCHyG9qNYrbsbhb_z73XUte2sUfdHzfLwCmXAXJ7UvttltlShP7q_zVj6iBCoq3blC4Pn4I6SonoBBI20rg40=d',
                               output_path: str = "gemini_generated.png"):
    """
    Generate an image with Gemini 2.5 Flash Image (preview) and optional reference image.
    Returns (image_path, content_type) or (None, None).
    """
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY not set")
        return None, None

    try:
        if not GEMINI_AVAILABLE or not genai:
            raise RuntimeError("Gemini API not available. Install google-generativeai package.")
        
        model_name = "gemini-2.5-flash-image-preview"
        image_model = genai.GenerativeModel(model_name)

        # Check for prompt override
        prompt_override = os.getenv("GEMINI_PROMPT_OVERRIDE", "").strip()
        
        if prompt_override:
            # Use the override prompt and skip image reference
            prompt_text = prompt_override
            print(f"🔄 Using custom prompt override: {prompt_text}")
        else:
            # Build default prompt text
            prompt_text = (
                f"use only the {objects_description} and generate a model of this analysis "
                f"with high fidelity, preserve the textures, and show from side degrees "
                f"of the top with a totally white background"
            )

        # ---- Build the request parts ----
        parts = [prompt_text]

        
        if reference_image_path and not prompt_override:
            try:
                # If it's a URL, fetch bytes from the web; otherwise read local file bytes
                if str(reference_image_path).lower().startswith(("http://", "https://")):
                    # Use a browser-like User-Agent and allow redirects; stream to be more robust
                    req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"}
                    resp = requests.get(reference_image_path, headers=req_headers, timeout=30, allow_redirects=True, stream=True)
                    resp.raise_for_status()
                    # read the response content (requests will handle streaming)
                    img_bytes = resp.content or b""
                    # Normalize MIME type (strip charset, etc.)
                    raw_ct = resp.headers.get("Content-Type") or ""
                    mime_t = raw_ct.split(";")[0].strip() or mimetypes.guess_type(reference_image_path)[0] or "image/png"
                    # If we got an HTML/text response, treat as failure and try to guess from URL
                    if mime_t.startswith("text/") or not img_bytes:
                        guessed = mimetypes.guess_type(reference_image_path)[0]
                        if guessed:
                            mime_t = guessed
                        else:
                            # Last-resort fallback
                            mime_t = "image/png"
                        if not img_bytes:
                            raise ValueError(f"No binary image data downloaded from URL: {reference_image_path}")
                else:
                    with open(reference_image_path, "rb") as f:
                        img_bytes = f.read()
                    mime_t = mimetypes.guess_type(reference_image_path)[0] or "image/png"

                parts.append({
                    "mime_type": mime_t,
                    "data": img_bytes
                })
            except Exception as e:
                print(f"⚠️ Could not load reference image '{reference_image_path}': {e}")
                # continue without attaching a reference image

        if prompt_override:
            print("🎨 Generating image with Gemini 2.5 Flash Image Preview (using custom prompt override)...")
        else:
            print("🎨 Generating image with Gemini 2.5 Flash Image Preview...")
        response = image_model.generate_content(parts)   # pass as list
        print("API request sent. Processing response...")
        
        # Debug: Print response structure
        print(f"Response type: {type(response)}")
        print(f"Response attributes: {dir(response)}")
        
        # Try different ways to access the generated image
        image_data = None
        mime_type = "image/png"
        
        # Method 1: Check if response has image directly
        if hasattr(response, 'image') and response.image:
            print("Found image in response.image")
            image_data = response.image
        
        # Method 2: Check candidates structure
        elif hasattr(response, 'candidates') and response.candidates:
            print(f"Found {len(response.candidates)} candidates")
            for i, candidate in enumerate(response.candidates):
                print(f"Candidate {i} type: {type(candidate)}")
                print(f"Candidate {i} attributes: {dir(candidate)}")
                
                if hasattr(candidate, 'content') and candidate.content:
                    if hasattr(candidate.content, 'parts') and candidate.content.parts:
                        for j, part in enumerate(candidate.content.parts):
                            print(f"Part {j} type: {type(part)}")
                            print(f"Part {j} attributes: {dir(part)}")
                            
                            # Check for inline_data
                            if hasattr(part, 'inline_data') and part.inline_data:
                                print("Found inline_data in part")
                                image_data = part.inline_data.data
                                mime_type = part.inline_data.mime_type or "image/png"
                                break
                            
                            # Check for image attribute
                            elif hasattr(part, 'image') and part.image:
                                print("Found image in part")
                                image_data = part.image
                                break
                            
                            # Check for blob
                            elif hasattr(part, 'blob') and part.blob:
                                print("Found blob in part")
                                image_data = part.blob.data
                                mime_type = part.blob.mime_type or "image/png"
                                break
                
                if image_data:
                    break
        
        # Method 3: Check parts directly
        elif hasattr(response, 'parts') and response.parts:
            print(f"Found {len(response.parts)} parts in response")
            for i, part in enumerate(response.parts):
                print(f"Part {i} type: {type(part)}")
                if hasattr(part, 'inline_data') and part.inline_data:
                    image_data = part.inline_data.data
                    mime_type = part.inline_data.mime_type or "image/png"
                    break
        
        if image_data:
            # Try to decode and save
            try:
                # If it's already bytes, use directly
                if isinstance(image_data, bytes):
                    decoded_data = image_data
                else:
                    # If it's base64 string, decode it
                    decoded_data = base64.b64decode(image_data)
                
                with open(output_path, "wb") as f:
                    f.write(decoded_data)
                
                print(f"✅ Saved Gemini-generated image to {output_path} ({mime_type})")
                print(f"Image size: {len(decoded_data)} bytes")
                return output_path, mime_type
                
            except Exception as decode_error:
                print(f"❌ Error decoding/saving image: {decode_error}")
                return None, None
        else:
            print("❌ No image data found in response")
            print("Response content:", str(response)[:500])
            return None, None
        
    except Exception as e:
        print(f"❌ Gemini image generation failed: {e}")
        import traceback
        traceback.print_exc()
        return None, None



def extract_most_prominent_object(objects_description: str) -> str:
    """Extract the most prominent/standout object from the description."""
    # Convert to lowercase for processing
    description = objects_description.lower().strip()
    
    # Priority order for object types (most important first)
    priority_objects = [
        # Buildings - highest priority
        'house', 'building', 'apartment', 'office', 'tower', 'structure',
        'residential', 'commercial', 'church', 'school', 'shop', 'store',
        
        # Architectural features
        'facade', 'roof', 'wall', 'balcony',
        
        # Other objects that might be prominent
        'car', 'vehicle', 'monument', 'fountain'
    ]
    
    # Look for the highest priority object in the description
    for obj in priority_objects:
        if obj in description:
            # If we find a match, try to get more context around it
            words = description.split()
            for i, word in enumerate(words):
                if obj in word:
                    # Try to get descriptive words before the object
                    context_start = max(0, i-2)
                    context_end = min(len(words), i+2)
                    context = ' '.join(words[context_start:context_end])
                    
                    # Clean up and return
                    context = context.strip('.,;:')
                    return context if len(context.split()) > 1 else obj
    
    # If no priority object found, take the first meaningful part
    words = description.split(',')
    if words:
        first_part = words[0].strip()
        return first_part if first_part else "building"
    
    return "building"  # ultimate fallback

def stream_get_to_file(url: str, *, headers: dict | None = None, out_path: str = OUT_GLBF):
    """Stream a GET response (no timeout) to file with progress."""
    with requests.get(url, headers=headers, stream=True, timeout=None, allow_redirects=True) as r:
        r.raise_for_status()
        total = r.headers.get("Content-Length")
        total = int(total) if total and total.isdigit() else None

        downloaded = 0
        chunk_size = 1024 * 64
        last_pct = -1

        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = int(downloaded * 100 / total)
                    if pct != last_pct:
                        sys.stdout.write(f"\rDownloading GLB: {pct}% ({downloaded}/{total} bytes)")
                        sys.stdout.flush()
                        last_pct = pct
                else:
                    if downloaded % (1024 * 1024) < chunk_size:
                        mb = downloaded / (1024 * 1024)
                        sys.stdout.write(f"\rDownloading GLB: ~{mb:.1f} MB")
                        sys.stdout.flush()

        if total:
            sys.stdout.write(f"\rDownloading GLB: 100% ({downloaded}/{total})\n")
        else:
            sys.stdout.write(f"\nDownload complete ({downloaded} bytes)\n")
        sys.stdout.flush()

    return True, {"status_code": 200, "file": out_path}

def post_hunyuan_to_file_with_retry(url: str, *, headers: dict, data: dict | None,
                         files: dict | None, out_path: str, max_retries: int = 3):
    """
    POST to Segmind with retry logic for server errors.
    Handles common API failures with exponential backoff and optimized parameters for timeout recovery.
    """
    import time
    
    # Store original parameters
    original_data = data.copy() if data else {}
    
    for attempt in range(max_retries):
        try:
            print(f"🚀 Attempt {attempt + 1}/{max_retries}: Posting to Segmind API...")
            
            # Optimize parameters for retry attempts after timeout
            current_data = original_data.copy() if original_data else {}
            if attempt > 0:
                # Use progressively faster parameters for retry attempts
                print(f"⚡ Using optimized parameters for retry attempt {attempt + 1}")
                optimized_params = optimize_params_for_retry(current_data, attempt)
                current_data.update(optimized_params)
                print(f"📊 Retry parameters: steps={current_data.get('steps', 'N/A')}, max_faces={current_data.get('max_facenum', 'N/A')}, tex_res={current_data.get('texture_resolution', 'N/A')}")
            
            # Validate image data before sending
            if files and "image" in files:
                image_file_tuple = files["image"]
                if len(image_file_tuple) >= 2:
                    filename, file_obj = image_file_tuple[0], image_file_tuple[1]
                    if hasattr(file_obj, 'read'):
                        # Reset file pointer if it's a file object
                        file_obj.seek(0)
                        file_size = len(file_obj.read())
                        file_obj.seek(0)  # Reset again for actual upload
                        print(f"📊 Image validation: {filename}, size: {file_size} bytes")
                        
                        if file_size == 0:
                            raise ValueError("Image file is empty")
                        if file_size < 1024:  # Less than 1KB is suspicious
                            print(f"⚠️ Warning: Image file seems very small ({file_size} bytes)")
            
            success, meta = post_hunyuan_to_file(url, headers=headers, data=current_data, files=files, out_path=out_path)
            
            if success:
                print(f"✅ Segmind API call successful on attempt {attempt + 1}")
                if attempt > 0:
                    print(f"💡 Success achieved with optimized parameters after timeout")
                return success, meta
            
            # Check for specific error types
            if "error_json" in meta:
                error_json = meta["error_json"]
                error_msg = error_json.get("error", str(error_json))
                
                print(f"❌ Segmind API error on attempt {attempt + 1}: {error_msg}")
                
                # Check for retryable errors
                retryable_errors = [
                    "Prediction failed",
                    "object has no attribute 'apply_filter'",
                    "NoneType",
                    "Internal server error",
                    "Server error",
                    "Timeout",
                    "Prediction timeout",  # Add specific timeout error
                    "Bad Gateway",
                    "Service Unavailable"
                ]
                
                is_retryable = any(err_pattern in error_msg for err_pattern in retryable_errors)
                
                # Special handling for timeout errors
                if "timeout" in error_msg.lower():
                    print(f"⏰ Timeout detected - will retry with faster parameters")
                    if attempt == 0:  # Show info on first timeout
                        print_timeout_recovery_info()
                    is_retryable = True
                elif not is_retryable and attempt == 0:
                    # For non-retryable errors on first attempt, still try once more with delay
                    print(f"⚠️ Non-retryable error detected, but trying once more...")
                    is_retryable = True
                elif not is_retryable:
                    print(f"❌ Non-retryable error, stopping attempts")
                    return success, meta
            
            # Adaptive backoff: shorter delays for timeout errors
            if attempt < max_retries - 1:
                if "timeout" in str(meta.get("error_json", {})).lower():
                    delay = 2  # Quick retry for timeouts
                    print(f"⏰ Quick retry in {delay}s for timeout...")
                else:
                    delay = 5 * (2 ** attempt)  # Exponential backoff for other errors
                    print(f"⏳ Waiting {delay}s before retry...")
                time.sleep(delay)
            
        except Exception as e:
            print(f"❌ Exception on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                delay = 5 * (2 ** attempt)
                print(f"⏳ Waiting {delay}s before retry...")
                time.sleep(delay)
            else:
                return False, {"error": str(e), "attempts": attempt + 1}
    
    print(f"❌ All {max_retries} attempts failed")
    return False, {"error": "Max retries exceeded", "attempts": max_retries}

def post_hunyuan_to_file(url: str, *, headers: dict, data: dict | None,
                         files: dict | None, out_path: str):
    """
    POST to Segmind. Handles:
      - heartbeat while computing (before first byte),
      - direct GLB stream (binary) with progress,
      - 200 JSON with download URL → auto GET and save,
      - non-200 JSON errors → returns parsed error_json.
    Uses reasonable timeout (10 minutes) to prevent extremely long waits.
    """
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/octet-stream")

    # Set reasonable timeout: 10 minutes total
    request_timeout = 600  # 10 minutes
    post_kwargs = {"headers": headers, "stream": True, "timeout": request_timeout}
    if files is None:
        post_kwargs["json"] = data
    else:
        post_kwargs["data"] = data
        post_kwargs["files"] = files

    # Start heartbeat immediately
    _heartbeat.stop = False
    hb = threading.Thread(target=_heartbeat, kwargs={"label": "Processing on server…"}, daemon=True)
    hb.start()

    first_chunk_seen = False
    try:
        with requests.post(url, **post_kwargs) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()

            # If JSON (even 200), parse and follow download URL automatically
            if "application/json" in ctype:
                try:
                    j = r.json()
                except Exception:
                    j = {"raw": r.text}

                # Stop heartbeat before switching to GET
                _heartbeat.stop = True
                hb.join(timeout=0.2)

                dl = (j.get("url") or
                      j.get("download_url") or
                      (j.get("output") or {}).get("url") or
                      (j.get("data") or {}).get("url"))

                if dl:
                    print("Server returned JSON with asset URL; fetching …")
                    return stream_get_to_file(dl, out_path=out_path)

                # JSON but no file URL -> return to caller for debugging
                return False, {"status_code": r.status_code, "error_json": j}

            # Otherwise assume binary stream (GLB)
            total = r.headers.get("Content-Length")
            total = int(total) if total and total.isdigit() else None

            r.raise_for_status()  # non-200 binaries -> raise

            downloaded = 0
            last_pct = -1
            chunk_size = 1024 * 64

            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        # keep heartbeat until first byte arrives
                        continue
                    if not first_chunk_seen:
                        first_chunk_seen = True
                        _heartbeat.stop = True
                        hb.join(timeout=0.2)
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / total)
                        if pct != last_pct:
                            sys.stdout.write(f"\rDownloading GLB: {pct}% ({downloaded}/{total} bytes)")
                            sys.stdout.flush()
                            last_pct = pct
                    else:
                        if downloaded % (1024 * 1024) < chunk_size:
                            mb = downloaded / (1024 * 1024)
                            sys.stdout.write(f"\rDownloading GLB: ~{mb:.1f} MB")
                            sys.stdout.flush()

            if total:
                sys.stdout.write(f"\rDownloading GLB: 100% ({downloaded}/{total})\n")
            else:
                sys.stdout.write(f"\nDownload complete ({downloaded} bytes)\n")
            sys.stdout.flush()

        return True, {"status_code": 200, "file": out_path}

    finally:
        # ensure heartbeat is stopped if we exit early
        _heartbeat.stop = True
        hb.join(timeout=0.2)

# ====================== MAIN FLOW ======================
def generate_3d_from_image(image_path: str, output_path: str = None) -> str:
    """
    Generate 3D model from a given image path with optional custom output path.
    
    Args:
        image_path: Path to input image
        output_path: Optional custom output path for GLB file
        
    Returns:
        Path to generated GLB file
    """
    if output_path is None:
        output_path = OUT_GLBF
    
    print(f"🎯 Generating 3D model from: {image_path}")
    print(f"📁 Output will be saved to: {output_path}")
    
    # Ensure output directory exists
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    
    # Analyze image with Llama 3.2 to identify objects
    objects_analysis = analyze_image_with_llama(image_path)
    print(f"\n📋 Objects identified: {objects_analysis}")
    
    # Save analysis for reference
    analysis_file = os.path.join(output_dir, "objects_analysis.txt") if output_dir else "objects_analysis.txt"
    with open(analysis_file, "w") as f:
        f.write(objects_analysis)
    print(f"✓ Saved object analysis to {analysis_file}")

    # Get enhanced image from Gemini 2.5 (optional)
    gemini_output = os.path.join(output_dir, "gemini_generated.png") if output_dir else "gemini_generated.png"
    gemini_image_path, content_type = generate_image_with_gemini(objects_analysis, image_path, gemini_output)
    if gemini_image_path and os.path.exists(gemini_image_path):
        generated_image = gemini_image_path
        print("🤖 Using Gemini-generated image for 3D modeling")
    else:
        generated_image = image_path
        print("📸 Using original image for 3D modeling")
    
    # Validate the image before attempting 3D generation
    print(f"🔍 Validating image for 3D generation: {generated_image}")
    if not validate_image_for_3d(generated_image):
        raise RuntimeError("Image validation failed. Cannot proceed with 3D generation.")
    
    mime = mimetypes.guess_type(generated_image)[0] or "image/jpeg"
    print(f"📤 Uploading {generated_image} ({mime}) to Segmind for 3D generation...")
    
    with open(generated_image, "rb") as imgf:
        # Validate image file
        img_content = imgf.read()
        if len(img_content) == 0:
            raise RuntimeError("Image file is empty!")
        
        print(f"📊 Image size: {len(img_content):,} bytes")
        
        # Reset file pointer and create file tuple
        imgf.seek(0)
        files = {"image": (os.path.basename(generated_image), imgf, mime)}
        data_form = {k: v for k, v in GEN_PARAMS.items()}  # same params, minus 'image'
        
        # Use retry wrapper for better reliability
        ok, meta = post_hunyuan_to_file_with_retry(
            SEGMIND_ENDPOINT,
            headers=HEADERS,
            data=data_form,
            files=files,
            out_path=output_path,
            max_retries=3
        )

    if not ok:
        error_msg = "3D generation failed."
        if "error_json" in meta:
            print("Response JSON:", json.dumps(meta["error_json"], ensure_ascii=False, indent=2))
            error_msg += f" API Error: {meta['error_json']}"
        
        # Save error metadata
        error_metadata = {
            "method": "generated_image_upload", 
            "meta": meta, 
            "source_image": generated_image,
            "objects_analysis": objects_analysis,
            "gemini_generated": gemini_image_path is not None,
            "error": True
        }
        
        error_json_file = os.path.join(output_dir, "3d_model_error.json") if output_dir else "3d_model_error.json"
        with open(error_json_file, "w") as f:
            json.dump(error_metadata, f, indent=2)
        
        raise RuntimeError(error_msg)

    print(f"\n🎉 3D GLB saved to: {meta['file']}")
    
    # Save success metadata
    success_metadata = {
        "method": "generated_image_upload", 
        "file": meta["file"], 
        "params": GEN_PARAMS,
        "source_image": generated_image,
        "objects_analysis": objects_analysis,
        "gemini_generated": gemini_image_path is not None
    }
    
    success_json_file = os.path.join(output_dir, "3d_model_output.json") if output_dir else OUT_JSON
    with open(success_json_file, "w") as f:
        json.dump(success_metadata, f, indent=2)
    
    return output_path

def main():
    # 1) Download the Street View image
    local_img = download_streetview_image("streetview.jpg")
    # os.environ["STREETVIEW_URL"] = STREETVIEW_URL
    
    # 2) Analyze image with Llama 3.2 to identify objects
    objects_analysis = analyze_image_with_llama(local_img)
    print(f"\n📋 Objects identified: {objects_analysis}")
    
    # Save analysis for reference
    with open("objects_analysis.txt", "w") as f:
        f.write(objects_analysis)
    print("✓ Saved object analysis to objects_analysis.txt")

    nearest = find_nearest_streetview(LAT, LNG, GOOGLE_MAPS_API_KEY, start_radius=50, step=50, max_radius=3000, source="outdoor")
    if not nearest:
        print("No Street View found within 3km.")
    else:
        print("Found pano:", nearest)
        # Prefer targeting by pano_id for an exact match:
        STREETVIEW_URL = build_streetview_image_url(
            GOOGLE_MAPS_API_KEY,
            pano_id=nearest["pano_id"],
            size="640x640",
            source="outdoor",
            heading=0,   # optional
            pitch=0,     # optional
            fov=80       # optional
        )
    
    # 3) Get enhanced image from Gemini 2.5 (optional)
    gemini_image_path, content_type = generate_image_with_gemini(objects_analysis, STREETVIEW_URL)
    if gemini_image_path and os.path.exists(gemini_image_path):
        generated_image = gemini_image_path
        print("🤖 Using Gemini-generated image for 3D modeling")
    else:
        generated_image = local_img
        print("📸 Using original Street View image for 3D modeling")
    
    # 4) Generate optimized image with Segmind SDXL
    # generated_image = generate_image_with_segmind_sdxl(description_for_image, "segmind_generated.jpg")
    
    # if not generated_image or not os.path.exists(generated_image):
    #     print("❌ Failed to generate optimized image. Cannot proceed with 3D generation.")
    #     print("The pipeline requires a generated image, not the original Street View.")
    #     return
    
    # print(f"✅ Using generated image for 3D modeling: {generated_image}")
    
    # 5) Generate 3D model using ONLY the generated image (no direct URL method)
    # print(f"\n=== Segmind | 3D Generation from Generated Image ===")
    # print(f"Source image: {generated_image}")
    
    # Ensure we have a valid image to upload: prefer Gemini-generated if available, otherwise use Street View
    if not generated_image or not os.path.exists(generated_image):
        # final fallback to the downloaded Street View image
        generated_image = local_img
    
    # Validate the image before attempting 3D generation
    print(f"🔍 Validating image for 3D generation: {generated_image}")
    if not validate_image_for_3d(generated_image):
        print("❌ Image validation failed. Cannot proceed with 3D generation.")
        return
    
    mime = mimetypes.guess_type(generated_image)[0] or "image/jpeg"
    print(f"📤 Uploading {generated_image} ({mime}) to Segmind for 3D generation...")
    
    with open(generated_image, "rb") as imgf:
        # Validate image file
        img_content = imgf.read()
        if len(img_content) == 0:
            print("❌ Image file is empty!")
            return
        
        print(f"📊 Image size: {len(img_content):,} bytes")
        
        # Reset file pointer and create file tuple
        imgf.seek(0)
        files = {"image": (os.path.basename(generated_image), imgf, mime)}
        data_form = {k: v for k, v in GEN_PARAMS.items()}  # same params, minus 'image'
        
        # Use retry wrapper for better reliability
        ok, meta = post_hunyuan_to_file_with_retry(
            SEGMIND_ENDPOINT,
            headers=HEADERS,
            data=data_form,
            files=files,
            out_path=OUT_GLBF,
            max_retries=3
        )

    if not ok:
        print("❌ 3D generation failed.")
        if "error_json" in meta:
            print("Response JSON:", json.dumps(meta["error_json"], ensure_ascii=False, indent=2))
        with open(OUT_JSON, "w") as f:
            json.dump({
                "method": "generated_image_upload", 
                "meta": meta, 
                "source_image": generated_image,
                "objects_analysis": objects_analysis,
                "gemini_generated": gemini_image_path is not None
            }, f, indent=2)
        return

    print(f"\n🎉 3D GLB saved to: {meta['file']}")
    with open(OUT_JSON, "w") as f:
        json.dump({
            "method": "generated_image_upload", 
            "file": meta["file"], 
            "params": GEN_PARAMS,
            "source_image": generated_image,
            "objects_analysis": objects_analysis,
            "gemini_generated": gemini_image_path is not None
        }, f, indent=2)

def validate_image_for_3d(image_path: str) -> bool:
    """Validate that an image is suitable for 3D generation."""
    try:
        import os
        from PIL import Image
        
        if not os.path.exists(image_path):
            print(f"❌ Image file does not exist: {image_path}")
            return False
        
        file_size = os.path.getsize(image_path)
        if file_size == 0:
            print(f"❌ Image file is empty: {image_path}")
            return False
        
        if file_size < 1024:  # Less than 1KB
            print(f"⚠️ Image file seems very small: {file_size} bytes")
        
        # Try to open with PIL to validate it's a real image
        try:
            with Image.open(image_path) as img:
                width, height = img.size
                if width < 64 or height < 64:
                    print(f"❌ Image too small for 3D generation: {width}x{height}")
                    return False
                
                if img.mode not in ['RGB', 'RGBA', 'L']:
                    print(f"⚠️ Unusual image mode: {img.mode}, converting to RGB")
                    # Convert to RGB and save
                    rgb_img = img.convert('RGB')
                    rgb_img.save(image_path, 'JPEG', quality=95)
                
                print(f"✅ Image validation passed: {width}x{height}, {img.mode}, {file_size:,} bytes")
                return True
                
        except Exception as pil_error:
            print(f"❌ PIL validation failed: {pil_error}")
            return False
        
    except ImportError:
        print("⚠️ PIL not available, skipping advanced image validation")
        # Basic validation without PIL
        return file_size > 0
    except Exception as e:
        print(f"❌ Image validation error: {e}")
        return False

def get_ultra_fast_params() -> dict:
    """
    Get ultra-fast generation parameters for timeout recovery.
    Prioritizes speed over quality to avoid timeouts.
    """
    return {
        "seed": 42,
        "steps": 15,           # Minimal steps for speed
        "num_chunks": 2000,    # Very low chunk count
        "max_facenum": 5000,   # Low face count for faster processing
        "guidance_scale": 7.5,
        "generate_texture": True,
        "octree_resolution": 64,    # Very low resolution for speed
        "remove_background": True,
        "texture_resolution": 1024,  # Low texture resolution
        "bake_normals": False,       # Disable to save time
        "bake_ao": False,           # Disable to save time
    }

def optimize_params_for_retry(original_params: dict, attempt: int) -> dict:
    """
    Progressively optimize parameters for retry attempts.
    Each retry uses faster parameters to avoid repeated timeouts.
    """
    if attempt == 1:
        # First retry: moderate optimization
        return {**original_params,
                "steps": max(15, int(original_params.get("steps", 25)) - 5),
                "max_facenum": max(5000, int(original_params.get("max_facenum", 15000)) // 2),
                "texture_resolution": max(1024, int(original_params.get("texture_resolution", 2048)) // 2),
                "octree_resolution": max(64, int(original_params.get("octree_resolution", 192)) // 2),
                "num_chunks": max(2000, int(original_params.get("num_chunks", 6000)) // 2),
        }
    elif attempt >= 2:
        # Second+ retry: use ultra-fast parameters
        print("🚀 Using ultra-fast parameters to avoid timeout")
        return get_ultra_fast_params()
    else:
        return original_params

def print_timeout_recovery_info():
    """Print helpful information about timeout recovery strategies."""
    print("\n" + "="*60)
    print("⏰ TIMEOUT RECOVERY STRATEGIES")
    print("="*60)
    print("""
🔧 CURRENT OPTIMIZATIONS:
   • Reduced default processing parameters for faster generation
   • 10-minute timeout limit instead of unlimited wait
   • Automatic parameter reduction on retry attempts
   • Ultra-fast fallback parameters for repeated timeouts

🎯 TO FURTHER REDUCE TIMEOUT RISK:
   1. Use smaller input images (≤ 640x640 pixels)
   2. Set STEPS=15 in your .env file
   3. Set TEXTURE_RESOLUTION=1024 in your .env file
   4. Set MAX_FACENUM=5000 for very fast processing

⚡ ULTRA-FAST MODE:
   Set these in your .env for minimal processing time:
   STEPS=10
   NUM_CHUNKS=1000
   MAX_FACENUM=3000
   TEXTURE_RESOLUTION=512
   OCTREE_RESOLUTION=32
   BAKE_NORMALS=false
   BAKE_AO=false
""")
    print("="*60)

if __name__ == "__main__":
    main()

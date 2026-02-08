"""
Image generation and vision analysis utilities
"""
import os
import tempfile
import requests
from typing import Optional
from PIL import Image
from io import BytesIO

def analyze_image_for_fusion(image_path: str) -> str:
    """Analyze image using vision AI to describe it for fusion purposes."""
    try:
        # Use Ollama llama3.2-vision for analysis
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        
        with open(image_path, 'rb') as f:
            image_data = f.read()
        
        # Encode image to base64
        import base64
        image_b64 = base64.b64encode(image_data).decode('utf-8')
        
        response = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": "llama3.2-vision",
                "prompt": "Describe this image in detail for use in AI image fusion. Focus on: objects, colors, style, composition, lighting, and mood.",
                "images": [image_b64],
                "stream": False
            },
            timeout=60
        )
        
        if response.status_code == 200:
            result = response.json()
            analysis = result.get("response", "").strip()
            if analysis:
                print(f"✅ Vision analysis complete: {analysis[:100]}...")
                return analysis
        
        # Fallback to basic description
        return f"Image from {os.path.basename(image_path)}"
        
    except Exception as e:
        print(f"⚠️ Vision analysis failed: {e}")
        return f"Image from {os.path.basename(image_path)}"

def generate_fusion_prompt_with_ollama(analysis1: str, analysis2: str, fusion_style: str = "blend", fusion_strength: float = 0.5) -> str:
    """Generate a fusion prompt using Ollama to combine two image analyses."""
    try:
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        
        weight1 = fusion_strength
        weight2 = 1.0 - fusion_strength
        
        system_prompt = f"""You are an expert at creating image generation prompts by fusing concepts.
Fusion style: {fusion_style}
Fusion strength: Image 1 ({weight1*100:.0f}%), Image 2 ({weight2*100:.0f}%)

Create a detailed, cohesive prompt that fuses these two images."""
        
        user_prompt = f"""Image 1 Analysis: {analysis1}

Image 2 Analysis: {analysis2}

Create a fusion prompt that combines elements from both images."""
        
        response = requests.post(
            f"{ollama_url}/api/generate",
            json={
                "model": "llama2",
                "prompt": f"{system_prompt}\n\n{user_prompt}",
                "stream": False
            },
            timeout=60
        )
        
        if response.status_code == 200:
            result = response.json()
            fusion_prompt = result.get("response", "").strip()
            if fusion_prompt:
                return fusion_prompt
        
        # Fallback to simple combination
        return f"{analysis1} combined with {analysis2}"
        
    except Exception as e:
        print(f"⚠️ Fusion prompt generation failed: {e}")
        return f"{analysis1} combined with {analysis2}"

def download_and_process_image(image_url: str, output_path: str = None) -> Optional[str]:
    """Download image from URL and optionally save to path."""
    try:
        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
        
        img = Image.open(BytesIO(response.content))
        
        if output_path:
            img.save(output_path)
            return output_path
        else:
            temp_file = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            img.save(temp_file.name)
            return temp_file.name
            
    except Exception as e:
        print(f"❌ Error downloading image: {e}")
        return None

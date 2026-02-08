"""
Pydantic models for request/response validation
"""
from pydantic import BaseModel, Field
from typing import Optional, List

class PromptRequest(BaseModel):
    prompt: str
    animation_ids: List[int] = Field(default=[1, 2, 3])
    height_meters: float = 1.8
    fps: int = 30

class CoordinatesRequest(BaseModel):
    latitude: float
    longitude: float
    animation_ids: List[int] = Field(default=[1, 2, 3])
    height_meters: float = 1.8
    fps: int = 30

class ImageRequest(BaseModel):
    image_url: Optional[str] = None
    image_path: Optional[str] = None

class RetextureRequest(BaseModel):
    texture_prompt: str
    job_id: str

class ConfirmationRequest(BaseModel):
    job_id: str
    confirmed: bool

class DesignEditRequest(BaseModel):
    job_id: str
    edit_prompt: str
    animation_ids: List[int] = Field(default=[1, 2, 3])
    height_meters: float = 1.8
    fps: int = 30

class DesignPosesRequest(BaseModel):
    job_id: str
    pose_count: int = 8

class InfuseRequest(BaseModel):
    fusion_style: str = "blend"
    fusion_strength: float = 0.5
    animation_ids: List[int] = Field(default=[1, 2, 3])
    height_meters: float = 1.8
    fps: int = 30
    use_retexture: bool = False

class WanAnimateRequest(BaseModel):
    prompt: str
    image_url: Optional[str] = None
    image_path: Optional[str] = None
    duration: int = 5
    aspect_ratio: str = "16:9"

class Wan22FastRequest(BaseModel):
    image_url: Optional[str] = None
    prompt: str = ""
    duration: int = 5
    aspect_ratio: str = "16:9"
    model: str = "i2v"
    seed: int = -1
    flow_shift: float = 7.0
    embedded_guidance_scale: float = 6.0
    motion_bucket_id: int = 127
    fps: int = 25
    augmentation_level: float = 0.0
    num_inference_steps: int = 50

class Wan22AnimateRequest(BaseModel):
    image_url: Optional[str] = None
    prompt: str = ""
    duration: int = 5
    aspect_ratio: str = "16:9"
    model: str = "i2v"
    seed: int = -1
    flow_shift: float = 7.0
    embedded_guidance_scale: float = 6.0
    motion_bucket_id: int = 127
    fps: int = 25
    augmentation_level: float = 0.0
    num_inference_steps: int = 50

class IdeogramV3TurboRequest(BaseModel):
    prompt: str
    aspect_ratio: str = "ASPECT_1_1"
    magic_prompt_option: str = "AUTO"
    seed: Optional[int] = None
    style_type: str = "AUTO"
    negative_prompt: str = ""
    model: str = "V_2_TURBO"
    save_to_s3: bool = True

class LumaRayRequest(BaseModel):
    prompt: str
    aspect_ratio: str = "16:9"
    loop: bool = False
    keyframes: Optional[dict] = None
    

class LumaLabsRequest(BaseModel):
    prompt: str
    loop: bool = False
    aspect_ratio: str = "16:9"
    keyframes: Optional[dict] = None

class GifToSpriteSheetRequest(BaseModel):
    gif_url: str
    sheet_type: str = "horizontal"
    max_frames: int = 50
    background_color: str = "transparent"

class RagImageGenRequest(BaseModel):
    prompt: str
    reference_image: Optional[str] = None

class SmartAnimateRequest(BaseModel):
    prompt: str
    duration: int = 5
    aspect_ratio: str = "16:9"
    auto_enhance: bool = True
    vision_model: str = "gemini"

class S3Assets(BaseModel):
    glb_url: Optional[str] = None
    fbx_url: Optional[str] = None
    animation_urls: List[str] = Field(default_factory=list)
    preview_gif_url: Optional[str] = None
    thumbnail_url: Optional[str] = None

class AnimationResult(BaseModel):
    job_id: str
    status: str
    message: str
    glb_file: Optional[str] = None
    fbx_file: Optional[str] = None
    animation_files: List[str] = Field(default_factory=list)
    s3_assets: Optional[S3Assets] = None
    preview_gif: Optional[str] = None
    thumbnail: Optional[str] = None
    processing_time: Optional[float] = None

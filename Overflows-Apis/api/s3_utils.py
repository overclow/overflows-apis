"""
S3/MinIO upload utilities for various asset types
"""
import os
import time
from typing import Optional
import boto3
from botocore.config import Config

# Initialize S3/MinIO client
def get_s3_client():
    """Get S3 client configured for MinIO or AWS."""
    
    # Check for MinIO first
    minio_endpoint = os.getenv("MINIO_ENDPOINT")
    minio_access_key = os.getenv("MINIO_ACCESS_KEY")
    minio_secret_key = os.getenv("MINIO_SECRET_KEY")
    
    if minio_endpoint and minio_access_key and minio_secret_key:
        print(f"✅ Using MinIO at {minio_endpoint}")
        minio_secure = os.getenv("MINIO_SECURE", "false").lower() == "true"
        return boto3.client(
            's3',
            endpoint_url=minio_endpoint,
            aws_access_key_id=minio_access_key,
            aws_secret_access_key=minio_secret_key,
            region_name='us-east-1',
            use_ssl=minio_secure,
            config=Config(signature_version='s3v4', s3={'addressing_style': 'path'})
        ), 'minio'
    
    # Fall back to AWS S3
    aws_region = os.getenv("AWS_REGION", "us-east-1")
    print(f"✅ Using AWS S3 in region {aws_region}")
    return boto3.client("s3", region_name=aws_region), 'aws'

# Get bucket and region
S3_BUCKET = os.getenv("MINIO_BUCKET") or os.getenv("S3_BUCKET", "workflows-assets")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
s3_client_obj, STORAGE_TYPE = get_s3_client()

def s3_client():
    """Return the configured S3 client."""
    return s3_client_obj

def upload_asset_to_s3(file_path: str, asset_type: str, job_id: str, asset_name: str = None) -> Optional[str]:
    """Upload any file asset to S3 and return presigned URL."""
    try:
        if not os.path.exists(file_path):
            print(f"❌ File not found: {file_path}")
            return None
            
        s3 = s3_client()
        timestamp = int(time.time())
        filename = asset_name or os.path.basename(file_path)
        key = f"assets/{job_id}/{asset_type}/{timestamp}_{filename}"
        
        # Determine content type
        content_type_map = {
            '.glb': 'model/gltf-binary',
            '.gltf': 'model/gltf+json',
            '.fbx': 'application/octet-stream',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.mp4': 'video/mp4',
            '.webp': 'image/webp'
        }
        
        file_ext = os.path.splitext(file_path)[1].lower()
        content_type = content_type_map.get(file_ext, 'application/octet-stream')
        
        print(f"📤 Uploading {asset_type}: {filename} -> s3://{S3_BUCKET}/{key}")
        
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
            ExpiresIn=7 * 24 * 3600
        )
        
        print(f"✅ Uploaded successfully: {key}")
        return presigned_url
        
    except Exception as e:
        print(f"❌ Error uploading {file_path}: {e}")
        return None

def upload_image_to_s3(job_id: str, image_path: str, image_tag: str) -> Optional[dict]:
    """Upload image to S3 with specific tagging."""
    try:
        if not os.path.exists(image_path):
            return None
            
        s3 = s3_client()
        timestamp = int(time.time())
        filename = os.path.basename(image_path)
        key = f"assets/{job_id}/images/{image_tag}/{timestamp}_{filename}"
        
        file_ext = os.path.splitext(image_path)[1].lower()
        content_type_map = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp'
        }
        content_type = content_type_map.get(file_ext, 'image/jpeg')
        
        with open(image_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {
                        "job_id": job_id,
                        "asset_type": "image",
                        "image_tag": image_tag,
                        "upload_time": str(timestamp)
                    }
                }
            )
        
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=7 * 24 * 3600
        )
        
        s3_direct_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{key}"
        
        return {
            "presigned_url": presigned_url,
            "s3_direct_url": s3_direct_url,
            "s3_key": key,
            "image_tag": image_tag,
            "upload_timestamp": timestamp
        }
        
    except Exception as e:
        print(f"❌ Error uploading image: {e}")
        return None

def upload_glb_to_s3_with_urls(file_path: str, filename: str) -> dict:
    """Upload GLB to S3 and return both presigned URL and direct S3 URL."""
    try:
        s3 = s3_client()
        timestamp = int(time.time())
        key = f"models/{timestamp}_{filename}"
        
        with open(file_path, 'rb') as f:
            s3.upload_fileobj(
                f,
                S3_BUCKET,
                key,
                ExtraArgs={
                    "ContentType": "model/gltf-binary",
                    "Metadata": {
                        "upload_time": str(timestamp)
                    }
                }
            )
        
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=7 * 24 * 3600
        )
        
        s3_direct_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{key}"
        
        return {
            "presigned_url": presigned_url,
            "s3_direct_url": s3_direct_url,
            "s3_key": key
        }
        
    except Exception as e:
        print(f"❌ Error uploading GLB: {e}")
        return {}

def create_presigned_download_url(file_path: str, filename: str) -> str:
    """Create presigned URL for downloading result files."""
    try:
        s3 = s3_client()
        key = f"downloads/{filename}"
        
        with open(file_path, 'rb') as f:
            s3.upload_fileobj(f, S3_BUCKET, key)
        
        presigned_url = s3.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": S3_BUCKET, "Key": key},
            ExpiresIn=3600  # 1 hour
        )
        
        return presigned_url
        
    except Exception as e:
        print(f"❌ Error creating presigned URL: {e}")
        return ""

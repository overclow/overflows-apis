"""
Configuration and environment variables for workflow API
"""

import os
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(override=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# AWS S3 configuration
AWS_REGION = os.getenv("AWS_REGION", "us-west-1")
S3_BUCKET = os.getenv("S3_BUCKET", "mapapi")

# Replicate API configuration
REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY")

# Animation API base URL configuration
ANIMATION_API_URL = os.getenv("ANIMATION_API_URL", "http://localhost:8000")

# MongoDB configuration
MONGODB_URL = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "studio")

# CORS configuration
ALLOWED_ORIGINS = [
    "http://localhost:5173",  # Vite default port
    "http://localhost:5174",  # Vite alternate port
    "http://localhost:3000",  # React default port
    "http://localhost:3001",  # Client port
    "http://127.0.0.1:3000",  # React with 127.0.0.1
    "http://127.0.0.1:3001",  # Client with 127.0.0.1 (NEW)
    "http://localhost:8080",  # Common dev port
    "http://localhost:4200",  # Angular default port
]

# Debug: Print API key status
def print_config_status():
    print(f"🔧 Workflow API Environment check:")
    print(f"   REPLICATE_API_KEY: {'SET (length: ' + str(len(REPLICATE_API_KEY)) + ')' if REPLICATE_API_KEY else 'NOT SET'}")
    print(f"   AWS_REGION: {AWS_REGION}")
    print(f"   S3_BUCKET: {S3_BUCKET}")
    print(f"   ANIMATION_API_URL: {ANIMATION_API_URL}")
    print(f"   MONGODB_URL: {MONGODB_URL}")
    print(f"   MONGODB_DATABASE: {MONGODB_DATABASE}")

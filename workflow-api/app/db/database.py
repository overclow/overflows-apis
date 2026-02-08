"""
MongoDB database operations for workflow storage
"""

import time
import logging
import ssl
import certifi
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from pymongo import MongoClient, IndexModel
from pymongo.errors import DuplicateKeyError, ConnectionFailure

from app.core.config import MONGODB_URL, MONGODB_DATABASE

logger = logging.getLogger(__name__)

# MongoDB connection
mongo_client = None
mongo_db = None
use_mongodb = False

# In-memory workflow storage (fallback when MongoDB unavailable)
workflows = {}
workflow_executions = {}


def init_mongodb():
    """Initialize MongoDB connection for workflow storage."""
    global mongo_client, mongo_db, use_mongodb
    
    try:
        logger.info(f"🔌 Connecting to MongoDB at {MONGODB_URL}")
        
        # Check if it's a local MongoDB connection (no SSL needed)
        is_local = any(host in MONGODB_URL for host in ['localhost', '127.0.0.1', '0.0.0.0'])
        is_atlas = 'mongodb.net' in MONGODB_URL or 'mongodb+srv' in MONGODB_URL
        
        if is_local:
            # Local MongoDB without SSL
            mongo_client = MongoClient(
                MONGODB_URL,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                socketTimeoutMS=10000,
                maxPoolSize=10,
                retryWrites=True
            )
        else:
            # MongoDB Atlas or remote with SSL
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED
            ssl_context.load_verify_locations(cafile=certifi.where())
            
            mongo_client = MongoClient(
                MONGODB_URL,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                socketTimeoutMS=10000,
                maxPoolSize=10,
                retryWrites=True,
                tlsCAFile=certifi.where(),
                tls=True,
                tlsAllowInvalidCertificates=False,
                tlsAllowInvalidHostnames=False
            )
        
        # Test connection
        mongo_client.admin.command('ping')
        mongo_db = mongo_client[MONGODB_DATABASE]
        use_mongodb = True
        
        # Setup workflow collections with indexes
        setup_workflow_collections()
        
        logger.info(f"✅ Connected to MongoDB database '{MONGODB_DATABASE}' for workflows")
        
    except Exception as e:
        logger.warning(f"⚠️ MongoDB not available, using in-memory storage: {e}")
        use_mongodb = False


def setup_workflow_collections():
    """Setup workflow collections with proper indexes."""
    try:
        # Workflows collection
        workflows_collection = mongo_db.workflows
        workflow_indexes = [
            IndexModel([("workflow_id", 1)], unique=True),
            IndexModel([("created_at", -1)]),
            IndexModel([("name", 1)])
        ]
        workflows_collection.create_indexes(workflow_indexes)
        
        # Workflow executions collection
        executions_collection = mongo_db.workflow_executions
        execution_indexes = [
            IndexModel([("execution_id", 1)], unique=True),
            IndexModel([("workflow_id", 1)]),
            IndexModel([("status", 1)]),
            IndexModel([("started_at", -1)]),
            IndexModel([("created_at", -1)])
        ]
        executions_collection.create_indexes(execution_indexes)
        
        logger.info("✅ Workflow collections and indexes setup completed")
        
    except Exception as e:
        logger.error(f"❌ Error setting up workflow collections: {e}")


def save_workflow_to_db(workflow_data: dict) -> bool:
    """Save workflow definition to MongoDB."""
    if not use_mongodb:
        workflows[workflow_data["workflow_id"]] = workflow_data
        return True
    
    try:
        workflow_data["created_at"] = time.time()
        workflow_data["created_datetime"] = datetime.fromtimestamp(workflow_data["created_at"], tz=timezone.utc)
        mongo_db.workflows.insert_one(workflow_data.copy())
        logger.info(f"💾 Saved workflow {workflow_data['workflow_id']} to MongoDB")
        return True
    except DuplicateKeyError:
        logger.warning(f"⚠️ Workflow {workflow_data['workflow_id']} already exists")
        return False
    except Exception as e:
        logger.error(f"❌ Error saving workflow: {e}")
        workflows[workflow_data["workflow_id"]] = workflow_data  # Fallback to in-memory
        return False


def get_workflow_from_db(workflow_id: str) -> Optional[dict]:
    """Retrieve workflow definition from MongoDB."""
    if not use_mongodb:
        return workflows.get(workflow_id)
    
    try:
        workflow = mongo_db.workflows.find_one({"workflow_id": workflow_id}, {"_id": 0})
        return workflow
    except Exception as e:
        logger.error(f"❌ Error retrieving workflow: {e}")
        return workflows.get(workflow_id)


def list_workflows_from_db() -> List[dict]:
    """List all workflows from MongoDB."""
    if not use_mongodb:
        return list(workflows.values())
    
    try:
        workflow_list = list(mongo_db.workflows.find({}, {"_id": 0}).sort("created_at", -1))
        return workflow_list
    except Exception as e:
        logger.error(f"❌ Error listing workflows: {e}")
        return list(workflows.values())


def save_execution_to_db(execution_data: dict) -> bool:
    """Save workflow execution to MongoDB."""
    if not use_mongodb:
        workflow_executions[execution_data["execution_id"]] = execution_data
        return True
    
    try:
        execution_data["created_at"] = time.time()
        execution_data["created_datetime"] = datetime.fromtimestamp(execution_data["created_at"], tz=timezone.utc)
        execution_data["updated_at"] = execution_data["created_at"]
        execution_data["updated_datetime"] = execution_data["created_datetime"]
        
        mongo_db.workflow_executions.insert_one(execution_data.copy())
        logger.info(f"💾 Saved workflow execution {execution_data['execution_id']} to MongoDB")
        return True
    except DuplicateKeyError:
        return update_execution_in_db(execution_data["execution_id"], execution_data)
    except Exception as e:
        logger.error(f"❌ Error saving execution: {e}")
        workflow_executions[execution_data["execution_id"]] = execution_data
        return False


def update_execution_in_db(execution_id: str, updates: dict) -> bool:
    """Update workflow execution in MongoDB."""
    if not use_mongodb:
        if execution_id in workflow_executions:
            workflow_executions[execution_id].update(updates)
        return True
    
    try:
        updates["updated_at"] = time.time()
        updates["updated_datetime"] = datetime.fromtimestamp(updates["updated_at"], tz=timezone.utc)
        
        result = mongo_db.workflow_executions.update_one(
            {"execution_id": execution_id},
            {"$set": updates}
        )
        
        # Also update in-memory cache
        if execution_id in workflow_executions:
            workflow_executions[execution_id].update(updates)
        
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"❌ Error updating execution: {e}")
        if execution_id in workflow_executions:
            workflow_executions[execution_id].update(updates)
        return False


def get_execution_from_db(execution_id: str) -> Optional[dict]:
    """Retrieve workflow execution from MongoDB."""
    if not use_mongodb:
        return workflow_executions.get(execution_id)
    
    try:
        execution = mongo_db.workflow_executions.find_one({"execution_id": execution_id}, {"_id": 0})
        return execution
    except Exception as e:
        logger.error(f"❌ Error retrieving execution: {e}")
        return workflow_executions.get(execution_id)


def list_executions_from_db(workflow_id: Optional[str] = None) -> List[dict]:
    """List workflow executions from MongoDB."""
    if not use_mongodb:
        execs = list(workflow_executions.values())
        if workflow_id:
            execs = [e for e in execs if e.get("workflow_id") == workflow_id]
        return execs
    
    try:
        query = {"workflow_id": workflow_id} if workflow_id else {}
        execution_list = list(mongo_db.workflow_executions.find(query, {"_id": 0}).sort("started_at", -1))
        return execution_list
    except Exception as e:
        logger.error(f"❌ Error listing executions: {e}")
        execs = list(workflow_executions.values())
        if workflow_id:
            execs = [e for e in execs if e.get("workflow_id") == workflow_id]
        return execs


def extract_assets_from_output(output: dict, node_type: str) -> dict:
    """Extract asset URLs from node output."""
    assets = {}
    
    # Image assets - check all possible fields
    if "image_url" in output and output["image_url"]:
        assets["image_url"] = output["image_url"]
    if "s3_url" in output and output["s3_url"]:
        assets["s3_url"] = output["s3_url"]
    if "gemini_generated_url" in output and output["gemini_generated_url"]:
        assets["gemini_generated_url"] = output["gemini_generated_url"]
    
    # 3D model assets
    if "model_url" in output and output["model_url"]:
        assets["model_url"] = output["model_url"]
    if "glb_url" in output and output["glb_url"]:
        assets["glb_url"] = output["glb_url"]
    
    # Video/animation assets
    if "video_url" in output and output["video_url"]:
        assets["video_url"] = output["video_url"]
    if "animation_url" in output and output["animation_url"]:
        assets["animation_url"] = output["animation_url"]
    
    # Multiple animation URLs
    if "animation_urls" in output and isinstance(output["animation_urls"], list):
        for idx, url in enumerate(output["animation_urls"]):
            if url:  # Only add non-empty URLs
                assets[f"animation_url_{idx}"] = url
    
    # Job IDs for reference
    if "job_id" in output and output["job_id"]:
        assets["job_id"] = output["job_id"]
    
    # Any other URL-like fields
    for key, value in output.items():
        if isinstance(value, str) and ("http://" in value or "https://" in value):
            # Check if it's a URL and not already captured
            if key not in assets and any(ext in value.lower() for ext in ['.jpg', '.png', '.gif', '.mp4', '.glb', '.obj', 's3.amazonaws', 'luma.ai', 'cdn']):
                assets[key] = value
    
    return assets

"""
Workflow API - Orchestrate complex animation pipelines
Execute nodes in order based on defined edges and dependencies
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
import uuid
import time
import os
import subprocess
import json
import base64
import tempfile
import ssl
import certifi
import plistlib
from datetime import datetime, timezone
import logging
from pymongo import MongoClient, IndexModel
from pymongo.errors import DuplicateKeyError, ConnectionFailure
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(override=True)

# Import the main animation API functions
import requests
from io import BytesIO
import boto3
from PIL import Image
import replicate

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Workflow Orchestration API")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite default port
        "http://localhost:5174",  # Vite alternate port
        "http://localhost:3000",  # React default port
        "http://localhost:8080",  # Common dev port
        "http://localhost:4200",  # Angular default port
    ],
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods (GET, POST, PUT, DELETE, etc.)
    allow_headers=["*"],  # Allow all headers
)

# MongoDB connection
mongo_client = None
mongo_db = None
use_mongodb = False

# AWS S3 configuration
AWS_REGION = os.getenv("AWS_REGION", "us-west-1")
S3_BUCKET = os.getenv("S3_BUCKET", "mapapi")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

# Replicate API configuration
REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY")
if REPLICATE_API_KEY:
    os.environ["REPLICATE_API_TOKEN"] = REPLICATE_API_KEY

# Suno API configuration
SUNO_API_KEY = os.getenv("SUNO_API_KEY")

# Animation API base URL configuration
ANIMATION_API_URL = os.getenv("ANIMATION_API_URL", "http://localhost:8000")

# Debug: Print API key status
print(f"🔧 Workflow API Environment check:")
print(f"   REPLICATE_API_KEY: {'SET (length: ' + str(len(REPLICATE_API_KEY)) + ')' if REPLICATE_API_KEY else 'NOT SET'}")
print(f"   SUNO_API_KEY: {'SET (length: ' + str(len(SUNO_API_KEY)) + ')' if SUNO_API_KEY else 'NOT SET'}")
print(f"   AWS_REGION: {AWS_REGION}")
print(f"   S3_BUCKET: {S3_BUCKET}")
print(f"   ANIMATION_API_URL: {ANIMATION_API_URL}")

# In-memory workflow storage (fallback when MongoDB unavailable)
workflows = {}
workflow_executions = {}
# Initialize MongoDB connection
def init_mongodb():
    """Initialize MongoDB connection for workflow storage."""
    global mongo_client, mongo_db, use_mongodb
    
    try:
        mongo_url = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
        db_name = os.getenv("MONGODB_DATABASE", "studio")
        
        logger.info(f"🔌 Connecting to MongoDB at {mongo_url}")
        
        # Check if it's a local MongoDB connection (no SSL needed)
        is_local = any(host in mongo_url for host in ['localhost', '127.0.0.1', '0.0.0.0'])
        is_atlas = 'mongodb.net' in mongo_url or 'mongodb+srv' in mongo_url
        
        if is_local:
            # Local MongoDB without SSL
            mongo_client = MongoClient(
                mongo_url,
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
                mongo_url,
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
        mongo_db = mongo_client[db_name]
        use_mongodb = True
        
        # Setup workflow collection with indexes
        setup_workflow_collections()
        
        logger.info(f"✅ Connected to MongoDB database '{db_name}' for workflows")
        
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

# Initialize MongoDB on startup
init_mongodb()


# ========= MONGODB HELPER FUNCTIONS =========

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


# ========= MODELS =========

class WorkflowNode(BaseModel):
    id: str
    type: str
    config: Dict[str, Any] = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    source: str
    target: str


class WorkflowDefinition(BaseModel):
    nodes: List[WorkflowNode]
    edges: List[WorkflowEdge]
    name: Optional[str] = None
    description: Optional[str] = None


class WorkflowExecuteRequest(BaseModel):
    workflow_id: Optional[str] = None
    nodes: Optional[List[WorkflowNode]] = None
    edges: Optional[List[WorkflowEdge]] = None


class NodeExecutionResult(BaseModel):
    node_id: str
    node_type: str
    status: str  # "success", "failed", "skipped"
    output: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    execution_time: float = 0.0
    timestamp: str
    assets: Dict[str, str] = Field(default_factory=dict)  # Store all asset URLs


class WorkflowExecutionStatus(BaseModel):
    execution_id: str
    workflow_id: Optional[str] = None
    status: str  # "queued", "running", "completed", "failed"
    progress: int = 0  # 0-100
    current_node: Optional[str] = None
    completed_nodes: List[str] = Field(default_factory=list)
    node_results: List[NodeExecutionResult] = Field(default_factory=list)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    total_execution_time: float = 0.0
    final_output: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    # Asset tracking by step
    assets_by_step: List[Dict[str, Any]] = Field(default_factory=list)
    all_assets: Dict[str, List[str]] = Field(default_factory=dict)  # {"images": [...], "videos": [...], "models": [...]}


# ========= WORKFLOW ENGINE =========

class WorkflowEngine:
    """Core workflow execution engine"""
    
    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url
        
    def build_execution_order(self, nodes: List[WorkflowNode], edges: List[WorkflowEdge]) -> List[str]:
        """Build execution order using topological sort"""
        
        # Build adjacency list
        graph = {node.id: [] for node in nodes}
        in_degree = {node.id: 0 for node in nodes}
        
        for edge in edges:
            if edge.source in graph:
                # Only add edge if target also exists in the graph
                if edge.target in in_degree:
                    graph[edge.source].append(edge.target)
                    in_degree[edge.target] += 1
        
        # Find nodes with no dependencies
        queue = [node_id for node_id, degree in in_degree.items() if degree == 0]
        execution_order = []
        
        while queue:
            current = queue.pop(0)
            execution_order.append(current)
            
            for neighbor in graph[current]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
        
        # Check for cycles
        if len(execution_order) != len(nodes):
            raise ValueError("Workflow contains circular dependencies")
        
        return execution_order
    
    def get_node_inputs(self, node_id: str, edges: List[WorkflowEdge], 
                        executed_results: Dict[str, NodeExecutionResult]) -> Dict[str, Any]:
        """Get inputs for a node from previous node outputs"""
        
        inputs = {}
        
        # Find all edges that target this node
        for edge in edges:
            if edge.target == node_id and edge.source in executed_results:
                source_result = executed_results[edge.source]
                if source_result.status == "success":
                    inputs[edge.source] = source_result.output
        
        return inputs
    
    async def execute_node(self, node: WorkflowNode, inputs: Dict[str, Any]) -> NodeExecutionResult:
        """Execute a single workflow node"""
        
        start_time = time.time()
        result = NodeExecutionResult(
            node_id=node.id,
            node_type=node.type,
            status="success",
            timestamp=datetime.now().isoformat()
        )
        
        try:
            # Route to appropriate handler based on node type
            if node.type == "text_prompt":
                result.output = await self.execute_text_prompt(node, inputs)
                
            elif node.type == "enhance_prompt":
                result.output = await self.execute_enhance_prompt(node, inputs)
                
            elif node.type == "enhance_prompt_ollama":
                result.output = await self.execute_enhance_prompt_ollama(node, inputs)
                
            elif node.type == "enhance_prompt_gnokit":
                result.output = await self.execute_enhance_prompt_gnokit(node, inputs)
                
            elif node.type == "enhance_prompt_fusion":
                result.output = await self.execute_enhance_prompt_fusion(node, inputs)
                
            elif node.type == "analyze_image_ollama":
                result.output = await self.execute_analyze_image_ollama(node, inputs)
                
            elif node.type == "analyze_image":
                result.output = await self.execute_analyze_vision(node, inputs)
                
            elif node.type == "analyze_documents":
                result.output = await self.execute_analyze_documents(node, inputs)
                
            elif node.type == "generate_image":
                result.output = await self.execute_generate_image(node, inputs)
                
            elif node.type == "generate_rag":
                result.output = await self.execute_generate_rag(node, inputs)
                
            elif node.type == "generate_nano_banana" or node.type == "generate_nano_banana_pro":
                result.output = await self.execute_generate_nano_banana(node, inputs)
                
            elif node.type == "generate_3d":
                result.output = await self.execute_generate_3d(node, inputs)
                
            elif node.type == "animate_luma":
                result.output = await self.execute_animate_luma(node, inputs)
                
            elif node.type == "animate_easy":
                result.output = await self.execute_animate_easy(node, inputs)
                
            elif node.type == "animate_wan":
                result.output = await self.execute_animate_wan(node, inputs)
                
            elif node.type == "animate_wan_fast":
                result.output = await self.execute_animate_wan_fast(node, inputs)
                
            elif node.type == "animate_wan_animate":
                result.output = await self.execute_animate_wan_animate(node, inputs)
                
            elif node.type == "generate_ideogram_turbo":
                result.output = await self.execute_generate_ideogram_turbo(node, inputs)
                
            elif node.type == "optimize_3d":
                result.output = await self.execute_optimize_3d(node, inputs)
                
            elif node.type == "time_travel_scene":
                result.output = await self.execute_time_travel_scene(node, inputs)
                
            elif node.type == "save_result":
                result.output = await self.execute_save_result(node, inputs)
                
            elif node.type == "fusion":
                result.output = await self.execute_fusion(node, inputs)
                
            elif node.type == "veo_3_fast":
                result.output = await self.execute_veo_3_fast(node, inputs)
                
            elif node.type == "sketch_board":
                result.output = await self.execute_sketch_board(node, inputs)
                
            elif node.type == "create_logo":
                result.output = await self.execute_create_logo(node, inputs)
                
            elif node.type == "remove_background":
                result.output = await self.execute_remove_background(node, inputs)
                
            elif node.type == "add_sound_to_image":
                result.output = await self.execute_add_sound_to_image(node, inputs)
                
            elif node.type == "generate_cover_music":
                result.output = await self.execute_generate_cover_music(node, inputs)
                
            elif node.type == "generate_music_suno":
                result.output = await self.execute_generate_music_suno(node, inputs)
                
            elif node.type == "replace_music_section":
                result.output = await self.execute_replace_music_section(node, inputs)
                
            elif node.type == "load_suno_from_taskid":
                result.output = await self.execute_load_suno_from_taskid(node, inputs)
                
            elif node.type == "separate_vocals":
                result.output = await self.execute_separate_vocals(node, inputs)
                
            elif node.type == "create_character":
                result.output = await self.execute_create_character(node, inputs)
                
            elif node.type == "upload_image":
                result.output = await self.execute_upload_image(node, inputs)
                
            elif node.type == "dating_app_enhancement":
                result.output = await self.execute_dating_app_enhancement(node, inputs)
                
            elif node.type == "qwen_image_edit":
                result.output = await self.execute_qwen_image_edit(node, inputs)
            
            elif node.type == "extract_dna":
                result.output = await self.execute_extract_dna(node, inputs)
                
            # Game Engine Export Nodes
            elif node.type == "export_cocos2d":
                result.output = await self.execute_export_cocos2d(node, inputs)
                
            elif node.type == "export_unity":
                result.output = await self.execute_export_unity(node, inputs)
                
            elif node.type == "export_unreal":
                result.output = await self.execute_export_unreal(node, inputs)
                
            elif node.type == "export_godot":
                result.output = await self.execute_export_godot(node, inputs)
                
            elif node.type == "create_spritesheet":
                result.output = await self.execute_create_spritesheet(node, inputs)
                
            elif node.type == "export_game_scene":
                result.output = await self.execute_export_game_scene(node, inputs)
                
            else:
                raise ValueError(f"Unknown node type: {node.type}")
            
            result.status = "success"
            
        except Exception as e:
            result.status = "failed"
            result.error = str(e)
            print(f"❌ Node {node.id} ({node.type}) failed: {e}")
        
        result.execution_time = time.time() - start_time
        return result
    
    # ========= NODE EXECUTORS =========
    
    async def execute_text_prompt(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute text_prompt node - simply passes through the prompt"""
        prompt = node.config.get("prompt", "")
        
        # Allow dynamic prompt from previous node
        if "prompt" in inputs and inputs["prompt"]:
            prompt = inputs["prompt"]
        
        return {
            "prompt": prompt,
            "type": "text"
        }
    
    async def execute_enhance_prompt(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute enhance_prompt node - calls Segmind Bria API"""
        
        # Get prompt from previous node or config
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            # Try to extract prompt from previous node
            for input_data in inputs.values():
                if isinstance(input_data, dict) and "prompt" in input_data:
                    prompt = input_data["prompt"]
                    break
                elif isinstance(input_data, dict) and "enhanced_prompt" in input_data:
                    prompt = input_data["enhanced_prompt"]
                    break
        
        if not prompt:
            raise ValueError("No prompt provided for enhancement")
        
        preserve_original = node.config.get("preserve_original", "false").lower() == "true"
        
        # Call the enhance endpoint
        response = requests.post(
            f"{self.base_url}/enhance/prompt",
            data={
                "prompt": prompt,
                "preserve_original": preserve_original
            },
            timeout=60
        )
        
        response.raise_for_status()
        result = response.json()
        
        return {
            "enhanced_prompt": result.get("enhanced_prompt", prompt),
            "original_prompt": prompt if preserve_original else None,
            "processing_time": result.get("processing_time", 0)
        }
    
    async def execute_enhance_prompt_ollama(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute enhance_prompt_ollama node - calls local Ollama"""
        
        # Get prompt from previous node or config
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict) and "prompt" in input_data:
                    prompt = input_data["prompt"]
                    break
        
        if not prompt:
            raise ValueError("No prompt provided for enhancement")
        
        model = node.config.get("model", "mistral")
        temperature = float(node.config.get("temperature", "0.7"))
        preserve_original = node.config.get("preserve_original", "false").lower() == "true"
        
        response = requests.post(
            f"{self.base_url}/enhance/prompt/ollama",
            data={
                "prompt": prompt,
                "model": model,
                "temperature": temperature,
                "preserve_original": preserve_original
            },
            timeout=120
        )
        
        response.raise_for_status()
        result = response.json()
        
        return {
            "enhanced_prompt": result.get("enhanced_prompt", prompt),
            "original_prompt": prompt if preserve_original else None,
            "model": model,
            "processing_time": result.get("processing_time", 0)
        }
    
    async def execute_enhance_prompt_gnokit(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute enhance_prompt_gnokit node - uses gnokit/improve-prompt Ollama model for advanced prompt enhancement"""
        
        # Get prompt from previous node or config
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict) and "prompt" in input_data:
                    prompt = input_data["prompt"]
                    break
        
        if not prompt:
            raise ValueError("No prompt provided for enhancement")
        
        temperature = float(node.config.get("temperature", "0.7"))
        preserve_original = node.config.get("preserve_original", "false").lower() == "true"
        ollama_url = node.config.get("ollama_url", "http://localhost:11434")
        
        print(f"🎯 [WORKFLOW] Enhancing prompt with gnokit/improve-prompt model")
        print(f"📝 Original prompt: {prompt[:100]}...")
        
        try:
            # Call Ollama API directly with gnokit/improve-prompt model
            response = requests.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": "gnokit/improve-prompt",
                    "prompt": prompt,
                    "options": {
                        "temperature": temperature
                    },
                    "stream": False
                },
                timeout=120
            )
            
            response.raise_for_status()
            result = response.json()
            
            # Extract the enhanced prompt from Ollama response
            enhanced_prompt = result.get("response", "").strip()
            
            if not enhanced_prompt:
                print(f"⚠️ Empty response from gnokit model, using original prompt")
                enhanced_prompt = prompt
            
            print(f"✅ Prompt enhanced successfully")
            print(f"📝 Enhanced prompt: {enhanced_prompt[:100]}...")
            
            return {
                "enhanced_prompt": enhanced_prompt,
                "original_prompt": prompt if preserve_original else None,
                "model": "gnokit/improve-prompt",
                "temperature": temperature,
                "processing_time": result.get("total_duration", 0) / 1e9 if "total_duration" in result else 0
            }
            
        except requests.exceptions.ConnectionError:
            raise ValueError(
                f"Cannot connect to Ollama at {ollama_url}. "
                f"Please ensure Ollama is running:\n"
                f"  1. Install Ollama: https://ollama.ai\n"
                f"  2. Pull the model: ollama pull gnokit/improve-prompt\n"
                f"  3. Verify it's running: ollama list"
            )
        except Exception as e:
            error_msg = str(e)
            if "404" in error_msg or "not found" in error_msg.lower():
                raise ValueError(
                    f"Model 'gnokit/improve-prompt' not found. "
                    f"Please pull it first:\n"
                    f"  ollama pull gnokit/improve-prompt"
                )
            raise ValueError(f"Failed to enhance prompt with gnokit model: {error_msg}")
    
    async def execute_enhance_prompt_fusion(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute enhance_prompt_fusion node - uses all 3 enhancement methods and combines them with the best local Ollama model
        
        This node type:
        1. Runs the prompt through all 3 enhancement methods (Segmind, Ollama basic, gnokit)
        2. Uses the most suitable local Ollama model to analyze all variants
        3. Creates a comprehensive JSON prompt optimized for image generation
        """
        
        # Get prompt from previous node or config
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "prompt" in input_data:
                        prompt = input_data["prompt"]
                        break
                    elif "enhanced_prompt" in input_data:
                        prompt = input_data["enhanced_prompt"]
                        break
        
        if not prompt:
            raise ValueError("No prompt provided for fusion enhancement")
        
        # Configuration
        ollama_url = node.config.get("ollama_url", "http://localhost:11434")
        fusion_model = node.config.get("fusion_model", "llama3.2:3b")  # Best local model for analysis
        temperature = float(node.config.get("temperature", "0.3"))  # Lower temp for better analysis
        preserve_original = node.config.get("preserve_original", "true").lower() == "true"
        output_format = node.config.get("output_format", "json")  # json or text
        
        print(f"🔥 [WORKFLOW] Starting fusion prompt enhancement with all 3 methods")
        print(f"📝 Original prompt: {prompt[:100]}...")
        print(f"🤖 Using fusion model: {fusion_model}")
        
        enhancement_results = {}
        enhancement_errors = {}
        
        # Method 1: Segmind Bria API Enhancement
        try:
            print(f"🚀 Method 1: Segmind Bria API enhancement...")
            response = requests.post(
                f"{self.base_url}/enhance/prompt",
                data={
                    "prompt": prompt,
                    "preserve_original": False
                },
                timeout=60
            )
            
            if response.status_code == 200:
                result = response.json()
                enhancement_results["segmind"] = {
                    "enhanced_prompt": result.get("enhanced_prompt", prompt),
                    "processing_time": result.get("processing_time", 0),
                    "method": "Segmind Bria API"
                }
                print(f"✅ Segmind enhancement: {enhancement_results['segmind']['enhanced_prompt'][:80]}...")
            else:
                enhancement_errors["segmind"] = f"HTTP {response.status_code}: {response.text[:100]}"
                print(f"❌ Segmind failed: {enhancement_errors['segmind']}")
                
        except Exception as e:
            enhancement_errors["segmind"] = str(e)
            print(f"❌ Segmind error: {e}")
        
        # Method 2: Ollama Basic Enhancement (using mistral or similar)
        try:
            print(f"🧠 Method 2: Ollama basic enhancement...")
            basic_model = node.config.get("basic_model", "mistral")
            
            response = requests.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": basic_model,
                    "prompt": f"Enhance this prompt for image generation, make it more detailed and visually descriptive: {prompt}",
                    "options": {
                        "temperature": 0.7
                    },
                    "stream": False
                },
                timeout=120
            )
            
            if response.status_code == 200:
                result = response.json()
                enhanced = result.get("response", "").strip()
                if enhanced:
                    enhancement_results["ollama_basic"] = {
                        "enhanced_prompt": enhanced,
                        "processing_time": result.get("total_duration", 0) / 1e9 if "total_duration" in result else 0,
                        "method": f"Ollama {basic_model}",
                        "model": basic_model
                    }
                    print(f"✅ Ollama basic enhancement: {enhanced[:80]}...")
                else:
                    enhancement_errors["ollama_basic"] = "Empty response"
            else:
                enhancement_errors["ollama_basic"] = f"HTTP {response.status_code}"
                
        except Exception as e:
            enhancement_errors["ollama_basic"] = str(e)
            print(f"❌ Ollama basic error: {e}")
        
        # Method 3: gnokit/improve-prompt Enhancement
        try:
            print(f"🎯 Method 3: gnokit/improve-prompt enhancement...")
            
            response = requests.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": "gnokit/improve-prompt",
                    "prompt": prompt,
                    "options": {
                        "temperature": 0.7
                    },
                    "stream": False
                },
                timeout=120
            )
            
            if response.status_code == 200:
                result = response.json()
                enhanced = result.get("response", "").strip()
                if enhanced:
                    enhancement_results["gnokit"] = {
                        "enhanced_prompt": enhanced,
                        "processing_time": result.get("total_duration", 0) / 1e9 if "total_duration" in result else 0,
                        "method": "gnokit/improve-prompt",
                        "model": "gnokit/improve-prompt"
                    }
                    print(f"✅ gnokit enhancement: {enhanced[:80]}...")
                else:
                    enhancement_errors["gnokit"] = "Empty response"
            else:
                enhancement_errors["gnokit"] = f"HTTP {response.status_code}"
                
        except Exception as e:
            enhancement_errors["gnokit"] = str(e)
            print(f"❌ gnokit error: {e}")
        
        # Ensure we have at least one successful enhancement
        if not enhancement_results:
            error_summary = "; ".join([f"{k}: {v}" for k, v in enhancement_errors.items()])
            raise ValueError(f"All enhancement methods failed: {error_summary}")
        
        print(f"📊 Successfully enhanced with {len(enhancement_results)}/{3} methods")
        
        # Step 4: Use fusion model to analyze and combine all variants
        try:
            print(f"🔀 Step 4: Analyzing and fusing all variants with {fusion_model}...")
            
            # Build analysis prompt
            analysis_prompt = f"""You are an expert prompt engineer for AI image generation. Analyze the following prompt variants and create the best possible fusion prompt.

ORIGINAL PROMPT:
{prompt}

ENHANCED VARIANTS:
"""
            
            for method, data in enhancement_results.items():
                analysis_prompt += f"\n{method.upper()} ({data['method']}):\n{data['enhanced_prompt']}\n"
            
            if enhancement_errors:
                analysis_prompt += f"\nFAILED METHODS: {', '.join(enhancement_errors.keys())}\n"
            
            if output_format == "json":
                analysis_prompt += f"""
Create a comprehensive JSON response with the following structure:
{{
    "fused_prompt": "The best fusion of all variants, optimized for image generation",
    "style_tags": ["tag1", "tag2", "tag3"],
    "technical_aspects": ["aspect1", "aspect2"],
    "mood_lighting": "description of mood and lighting",
    "composition": "description of composition and framing", 
    "quality_modifiers": ["4K", "detailed", "etc"],
    "analysis": "Brief explanation of why this fusion is optimal",
    "confidence_score": 0.95,
    "source_methods": ["list", "of", "successful", "methods"]
}}

Respond ONLY with valid JSON, no other text.
"""
            else:
                analysis_prompt += f"""
Based on all the variants above, create the BEST possible prompt for image generation that:
1. Combines the strongest elements from each variant
2. Is optimized for visual clarity and detail
3. Includes appropriate style and quality modifiers
4. Is concise but comprehensive

Respond with only the optimized prompt, no explanation.
"""
            
            # Call fusion model
            response = requests.post(
                f"{ollama_url}/api/generate",
                json={
                    "model": fusion_model,
                    "prompt": analysis_prompt,
                    "options": {
                        "temperature": temperature
                    },
                    "stream": False
                },
                timeout=180
            )
            
            response.raise_for_status()
            fusion_result = response.json()
            fusion_response = fusion_result.get("response", "").strip()
            
            if not fusion_response:
                raise ValueError("Empty response from fusion model")
            
            print(f"✅ Fusion analysis completed")
            print(f"📝 Fusion result: {fusion_response[:100]}...")
            
            # Parse JSON if requested
            fusion_data = None
            if output_format == "json":
                try:
                    fusion_data = json.loads(fusion_response)
                    print(f"✅ Successfully parsed JSON fusion result")
                except json.JSONDecodeError as e:
                    print(f"⚠️ Failed to parse JSON, falling back to text: {e}")
                    fusion_data = {
                        "fused_prompt": fusion_response,
                        "analysis": "JSON parsing failed, raw response provided",
                        "confidence_score": 0.8,
                        "source_methods": list(enhancement_results.keys())
                    }
            
            # Build comprehensive result
            result = {
                "fused_prompt": fusion_data.get("fused_prompt") if fusion_data else fusion_response,
                "original_prompt": prompt if preserve_original else None,
                "enhancement_results": enhancement_results,
                "enhancement_errors": enhancement_errors if enhancement_errors else None,
                "fusion_model": fusion_model,
                "fusion_temperature": temperature,
                "output_format": output_format,
                "successful_methods": len(enhancement_results),
                "total_methods": 3,
                "processing_summary": f"Enhanced with {len(enhancement_results)}/3 methods, fused with {fusion_model}"
            }
            
            # Add JSON-specific fields if available
            if fusion_data:
                result.update({
                    "style_tags": fusion_data.get("style_tags", []),
                    "technical_aspects": fusion_data.get("technical_aspects", []),
                    "mood_lighting": fusion_data.get("mood_lighting", ""),
                    "composition": fusion_data.get("composition", ""),
                    "quality_modifiers": fusion_data.get("quality_modifiers", []),
                    "confidence_score": fusion_data.get("confidence_score", 0.8),
                    "fusion_analysis": fusion_data.get("analysis", "")
                })
            
            print(f"🔥 Fusion enhancement completed successfully!")
            print(f"📊 Final prompt: {result['fused_prompt'][:100]}...")
            
            return result
            
        except Exception as e:
            print(f"❌ Fusion analysis failed: {e}")
            # Fallback: return best available enhancement
            if enhancement_results:
                best_method = max(enhancement_results.keys(), 
                                key=lambda k: len(enhancement_results[k]['enhanced_prompt']))
                best_result = enhancement_results[best_method]
                
                print(f"🔄 Falling back to best single method: {best_method}")
                
                return {
                    "fused_prompt": best_result["enhanced_prompt"],
                    "original_prompt": prompt if preserve_original else None,
                    "enhancement_results": enhancement_results,
                    "enhancement_errors": enhancement_errors,
                    "fallback_method": best_method,
                    "fusion_model": fusion_model,
                    "fusion_error": str(e),
                    "successful_methods": len(enhancement_results),
                    "total_methods": 3,
                    "processing_summary": f"Fusion failed, using best single method: {best_method}"
                }
            else:
                raise ValueError(f"Fusion analysis failed and no fallback available: {e}")
    
    async def execute_analyze_image_ollama(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute analyze_image_ollama node - analyzes image with Llama 3.2 Vision"""
        
        # Get image from previous node or config
        image_url = node.config.get("image_url")
        image_path = node.config.get("image_path")

        # Determine if we should skip vision analysis for sketches
        sketch_keywords = ["sketch", "drawing", "canvas"]
        sketch_source_detected = False

        for source_id, input_data in inputs.items():
            if isinstance(source_id, str) and any(keyword in source_id.lower() for keyword in sketch_keywords):
                sketch_source_detected = True
            if isinstance(input_data, dict):
                node_type = str(input_data.get("node_type", ""))
                data_type = str(input_data.get("type", ""))
                reference_source = str(input_data.get("reference_source", ""))
                if any(keyword in node_type.lower() for keyword in sketch_keywords):
                    sketch_source_detected = True
                if any(keyword in data_type.lower() for keyword in sketch_keywords):
                    sketch_source_detected = True
                if any(keyword in reference_source.lower() for keyword in sketch_keywords):
                    sketch_source_detected = True
            if sketch_source_detected:
                break

        if not sketch_source_detected:
            # Also check node config metadata for explicit sketch flags
            config_source = str(node.config.get("image_source", ""))
            if any(keyword in config_source.lower() for keyword in sketch_keywords):
                sketch_source_detected = True

        if sketch_source_detected:
            # Ensure we pass along a usable image reference for downstream nodes
            if not image_url and inputs:
                for input_data in inputs.values():
                    if isinstance(input_data, dict):
                        for field in ["image_url", "s3_url", "sketch_url", "drawing_url", "gemini_generated_url"]:
                            if field in input_data and input_data[field]:
                                image_url = input_data[field]
                                break
                        if image_url:
                            break

            print("🎨 [WORKFLOW] Skipping Llama 3.2 Vision analysis for sketch reference input")
            return {
                "status": "skipped",
                "reason": "sketch_reference",
                "analysis": "",
                "model": node.config.get("model", "llama3.2-vision"),
                "processing_time": 0,
                "image_url": image_url or image_path
            }
        
        # Try to extract image from previous node
        if not image_url and not image_path and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
        
        if not image_url and not image_path:
            raise ValueError("No image provided for analysis")
        
        model = node.config.get("model", "llama3.2-vision")
        custom_prompt = node.config.get("custom_prompt", 
            "Describe this image in detail, focusing on what would make an interesting animation.")
        
        # Download image if URL provided
        if image_url:
            img_response = requests.get(image_url, timeout=30)
            img_response.raise_for_status()
            image_content = img_response.content
            filename = "image.jpg"
        else:
            with open(image_path, 'rb') as f:
                image_content = f.read()
            filename = os.path.basename(image_path)
        
        # Call analyze endpoint
        files = {'image': (filename, BytesIO(image_content), 'image/jpeg')}
        data = {
            'model': model,
            'custom_prompt': custom_prompt
        }
        
        response = requests.post(
            f"{self.base_url}/analyze/image/ollama",
            files=files,
            data=data,
            timeout=180
        )
        
        response.raise_for_status()
        result = response.json()
        
        return {
            "analysis": result.get("analysis", ""),
            "model": model,
            "processing_time": result.get("processing_time", 0),
            "image_url": image_url
        }
    
    async def execute_analyze_vision(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute analyze_vision node - uses Dual AI (YOLOv8 + Llama 3.2 Vision) for comprehensive object detection"""
        
        # Get image from previous node or config
        image_url = node.config.get("image_url")
        image_path = node.config.get("image_path")
        # Support base64 image uploads (data URI or raw base64) via config or inputs
        image_upload = node.config.get("image_upload") or node.config.get("image_base64")
        # Also allow common alternative keys
        if not image_upload and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_upload" in input_data:
                        image_upload = input_data["image_upload"]
                        break
                    if "image_base64" in input_data:
                        image_upload = input_data["image_base64"]
                        break
                    # sometimes upstream returns full data URI under other keys
                    for k in ("image_data", "data_uri", "data_url"):
                        if k in input_data and isinstance(input_data[k], str) and input_data[k].startswith("data:"):
                            image_upload = input_data[k]
                            break
                    if image_upload:
                        break
                # also accept plain base64 string directly as an input value
                elif isinstance(input_data, str) and input_data.strip().startswith("data:"):
                    image_upload = input_data
                    break

        # If we received a base64 upload, decode and write to a temp file to use as image_path
        if image_upload and isinstance(image_upload, str):
            try:
                # If it's a data URI like "data:image/png;base64,AAAA..."
                if image_upload.startswith("data:") and "base64," in image_upload:
                    header, b64data = image_upload.split("base64,", 1)
                    # try to infer extension from header
                    try:
                        mime = header.split(";")[0].split(":")[1]
                        ext = mime.split("/")[-1]
                        # normalize common variations
                        if ext == "jpeg":
                            ext = "jpg"
                    except Exception:
                        ext = "png"
                else:
                    # raw base64, attempt to guess png by default
                    b64data = image_upload
                    ext = "png"

                # Decode base64
                image_bytes = base64.b64decode(b64data)

                # Write to a temporary file (persisted for downstream calls)
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
                tmp.write(image_bytes)
                tmp.flush()
                tmp.close()
                image_path = tmp.name

                print(f"🗂️  Decoded base64 upload to temp file: {image_path}")

            except Exception as e:
                raise ValueError(f"Failed to decode base64 image upload: {e}")
        # Try to extract image from previous node
        if not image_url and not image_path and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
                    elif "gemini_generated_url" in input_data:
                        image_url = input_data["gemini_generated_url"]
                        break
        
        if not image_url and not image_path:
            raise ValueError("No image provided for vision analysis")
        
        # Extract prompt context for context-aware confidence scoring
        user_prompt = None
        enhanced_prompt = None
        context_prompts = []
        
        # Look for prompts in node config
        user_prompt = node.config.get("user_prompt") or node.config.get("prompt")
        enhanced_prompt = node.config.get("enhanced_prompt") or node.config.get("fused_prompt")
        context_prompts = node.config.get("context_prompts", [])
        
        # Look for prompts in input data from previous nodes
        if inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    # Check for prompt data
                    if not user_prompt and "prompt" in input_data:
                        user_prompt = input_data["prompt"]
                    if not user_prompt and "user_prompt" in input_data:
                        user_prompt = input_data["user_prompt"]
                    if not enhanced_prompt and "enhanced_prompt" in input_data:
                        enhanced_prompt = input_data["enhanced_prompt"]
                    if not enhanced_prompt and "fused_prompt" in input_data:
                        enhanced_prompt = input_data["fused_prompt"]
                    if not context_prompts and "context_prompts" in input_data:
                        context_prompts = input_data["context_prompts"]
                    
                    # Check text prompt output format
                    if not user_prompt and "enhanced_text" in input_data:
                        user_prompt = input_data["enhanced_text"]
                    if not user_prompt and "text" in input_data:
                        user_prompt = input_data["text"]
        
        print(f"🔍 [WORKFLOW] Analyzing image with Context-Aware Dual AI Vision (YOLOv8 + Llama 3.2)")
        print(f"🖼️  Image URL: {image_url[:100] if image_url else 'local file'}...")
        if user_prompt:
            print(f"🎯 User prompt: {user_prompt[:100]}...")
        if enhanced_prompt:
            print(f"✨ Enhanced prompt: {enhanced_prompt[:100]}...")
        if context_prompts:
            print(f"🏷️  Context prompts: {context_prompts}")
        
        # Prepare request data with context-aware parameters
        request_data = {}
        if user_prompt:
            request_data["user_prompt"] = user_prompt
        if enhanced_prompt:
            request_data["enhanced_prompt"] = enhanced_prompt
        if context_prompts:
            request_data["context_prompts"] = json.dumps(context_prompts) if isinstance(context_prompts, list) else context_prompts
        
        # Prepare request based on whether we have URL or local file
        try:
            if image_url:
                # Use image_url parameter with context-aware prompts
                request_data["image_url"] = image_url
                response = requests.post(
                    f"{self.base_url}/animate/vision",
                    data=request_data,
                    timeout=180  # Increased timeout for dual AI processing
                )
            else:
                # Upload local file with context-aware prompts
                with open(image_path, 'rb') as f:
                    files = {'file': (os.path.basename(image_path), f, 'image/jpeg')}
                    response = requests.post(
                        f"{self.base_url}/animate/vision",
                        files=files,
                        data=request_data,
                        timeout=180  # Increased timeout for dual AI processing
                    )
            
            response.raise_for_status()
            result = response.json()
            
            # Extract enhanced dual AI results
            analysis_metadata = result.get("analysis_metadata", {})
            detected_objects = result.get("detected_objects", [])
            confidence_stats = result.get("confidence_statistics", {})
            scene_analysis = result.get("scene_analysis", "")
            
            print(f"✅ Dual AI Vision analysis completed")
            print(f"🤖 Models used: {analysis_metadata.get('models_used', ['YOLOv8', 'Llama 3.2 Vision'])}")
            print(f"📦 Detected {len(detected_objects)} objects with avg confidence: {confidence_stats.get('average_confidence', 0):.2f}")
            
            # Handle processing_time which can be either a float or a dict
            processing_time_value = analysis_metadata.get('processing_time', 0)
            if isinstance(processing_time_value, dict):
                processing_time_display = processing_time_value.get('total', 0)
            else:
                processing_time_display = processing_time_value
            
            print(f"⏱️ Processing time: {processing_time_display:.2f}s")
            
            # Create summary of high-confidence objects (using converged confidence)
            high_confidence_objects = [
                obj for obj in detected_objects 
                if obj.get("converged_confidence", obj.get("confidence_score", 0)) >= 0.7
            ]
            
            # Create summary of high animation potential objects
            high_potential_objects = [
                obj for obj in detected_objects 
                if obj.get("animation_potential") == "high"
            ]
            
            # Enhanced output with all dual AI features
            return {
                "status": "completed",
                "analysis_type": "context_aware_dual_ai_vision",
                
                # Core detection results
                "detected_objects": detected_objects,
                "object_count": len(detected_objects),
                
                # Context-aware confidence analysis
                "confidence_statistics": confidence_stats,
                "general_confidence": result.get("general_confidence", 0.0),
                "general_confidence_level": result.get("general_confidence_level", "none"),
                "prompt_relevance_confidence": result.get("prompt_relevance_confidence", 0.0),
                "high_confidence_objects": high_confidence_objects,
                "high_confidence_count": len(high_confidence_objects),
                
                # Context-aware features
                "prompt_context": result.get("prompt_context", {
                    "user_prompt": user_prompt or "none",
                    "enhanced_prompt": enhanced_prompt or "none",
                    "context_prompts": context_prompts,
                    "relevance_analysis": {}
                }),
                
                # Animation analysis
                "high_potential_objects": high_potential_objects,
                "high_potential_count": len(high_potential_objects),
                "animation_recommendations": result.get("animation_recommendations", []),
                
                # Scene understanding
                "scene_analysis": scene_analysis,
                "scene_context": scene_analysis,  # Alternative key for backwards compatibility
                
                # Technical metadata
                "analysis_metadata": analysis_metadata,
                "image_dimensions": result.get("image_dimensions", {}),
                "processing_details": result.get("processing_details", {}),
                
                # Image references
                "image_url": image_url or image_path,
                "s3_url": result.get("rag_generated_s3_url"),
                "job_id": result.get("job_id"),
                
                # Legacy compatibility fields
                "animatable_objects": detected_objects,  # For backwards compatibility
                "recommendations": result.get("animation_recommendations", []),
                "full_analysis": scene_analysis,
                
                # Advanced features data
                "model_convergence": {
                    "yolo_detections": analysis_metadata.get("yolo_detections", 0),
                    "llama_detections": analysis_metadata.get("llama_detections", 0),
                    "converged_count": analysis_metadata.get("converged_objects", 0)
                },
                
                # Confidence breakdown
                "confidence_breakdown": {
                    "high": confidence_stats.get("high_confidence_count", 0),
                    "medium": confidence_stats.get("medium_confidence_count", 0),
                    "low": confidence_stats.get("low_confidence_count", 0)
                },
                
                # General confidence summary for easy access
                "confidence_summary": {
                    "score": result.get("general_confidence", 0.0),
                    "level": result.get("general_confidence_level", "none"),
                    "individual_average": confidence_stats.get("average_confidence", 0.0),
                    "weighted_average": confidence_stats.get("weighted_average", 0.0)
                }
            }
            
        except requests.exceptions.RequestException as e:
            # Provide more helpful error message
            error_msg = str(e)
            
            # Check if it's a connection error (API not running)
            if "Connection" in error_msg or "connect" in error_msg.lower():
                raise ValueError(
                    f"Cannot connect to animation API at {self.base_url}. "
                    f"Please ensure the animation API is running:\n"
                    f"  Terminal 1: cd /Users/avihairing/mmmp && python3 animation_api.py\n"
                    f"Original error: {error_msg}"
                )
            elif "503" in error_msg or "Service Unavailable" in error_msg:
                raise ValueError(
                    f"Vision analysis endpoint returned 503 Service Unavailable. "
                    f"This usually means YOLOv8 is not installed or not loading properly.\n"
                    f"To fix, run: pip install ultralytics\n"
                    f"Then restart the animation API.\n"
                    f"Original error: {error_msg}"
                )
            elif "404" in error_msg:
                raise ValueError(
                    f"Vision analysis endpoint not found at {self.base_url}/animate/vision. "
                    f"The endpoint may not be implemented in your animation_api.py version.\n"
                    f"Alternative: Use 'analyze_image_ollama' node type instead.\n"
                    f"Original error: {error_msg}"
                )
            else:
                raise ValueError(f"Vision analysis request failed: {error_msg}")
    
    async def execute_analyze_documents(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute analyze_documents node - analyzes multiple text documents and generates design prompts"""
        
        # Get documents from node config or inputs
        documents_data = node.config.get("documents", [])
        design_context = node.config.get("design_context", "")
        output_type = node.config.get("output_type", "image_generation")
        style_preferences = node.config.get("style_preferences", "")
        
        # Look for documents in input data from previous nodes
        if not documents_data and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "documents" in input_data:
                        documents_data = input_data["documents"]
                        break
                    elif "files" in input_data:
                        documents_data = input_data["files"]
                        break
                    elif "document_content" in input_data:
                        # Handle single document case
                        documents_data = [input_data]
                        break
        
        if not documents_data:
            raise ValueError("No documents provided for analysis. Please upload text documents (PDF, TXT, DOC, etc.)")
        
        print(f"📄 [WORKFLOW] Analyzing {len(documents_data) if isinstance(documents_data, list) else 1} documents for design prompt generation")
        print(f"🎯 Design context: {design_context or 'Not specified'}")
        print(f"🎨 Output type: {output_type}")
        print(f"✨ Style preferences: {style_preferences or 'Not specified'}")
        
        # Prepare form data for the API call
        try:
            # For workflow execution, we'll pass document content directly if available
            # Or prepare file-like objects if we have file paths
            
            form_data = {
                "design_context": design_context,
                "output_type": output_type,
                "style_preferences": style_preferences
            }
            
            # Handle different input formats
            if isinstance(documents_data, list):
                # Multiple documents case
                if all(isinstance(doc, dict) and "content" in doc for doc in documents_data):
                    # We have document content directly
                    combined_content = "\n\n=== DOCUMENT SEPARATOR ===\n\n".join([
                        f"DOCUMENT: {doc.get('filename', f'document_{i+1}')}\n{doc['content']}"
                        for i, doc in enumerate(documents_data)
                    ])
                    
                    # Call the API with the combined content directly
                    response = requests.post(
                        f"{self.base_url}/analyze/documents",
                        data={
                            **form_data,
                            "combined_document_content": combined_content,
                            "document_count": len(documents_data)
                        },
                        timeout=300  # Longer timeout for document analysis
                    )
                else:
                    # We might have file paths or need to handle differently
                    raise ValueError("Document data format not supported. Expected documents with 'content' field.")
            else:
                # Single document case
                if isinstance(documents_data, dict) and "content" in documents_data:
                    response = requests.post(
                        f"{self.base_url}/analyze/documents",
                        data={
                            **form_data,
                            "combined_document_content": documents_data["content"],
                            "document_count": 1
                        },
                        timeout=300
                    )
                else:
                    raise ValueError("Single document data format not supported. Expected document with 'content' field.")
            
            response.raise_for_status()
            result = response.json()
            
            # Extract key outputs
            primary_prompt = result.get("primary_prompt", "")
            alternative_prompts = result.get("alternative_prompts", [])
            style_modifiers = result.get("style_modifiers", [])
            analysis = result.get("analysis", {})
            
            print(f"✅ Document analysis completed")
            print(f"📝 Generated primary prompt: {primary_prompt[:100] if primary_prompt else 'None'}...")
            print(f"🔄 Generated {len(alternative_prompts)} alternative prompts")
            print(f"🎨 Found {len(style_modifiers)} style modifiers")
            
            # Create workflow-friendly output
            return {
                "status": "completed",
                "analysis_type": "document_analysis",
                
                # Primary outputs for next nodes
                "prompt": primary_prompt,
                "enhanced_prompt": primary_prompt,  # For compatibility with enhance nodes
                "primary_prompt": primary_prompt,
                "alternative_prompts": alternative_prompts,
                "style_modifiers": style_modifiers,
                
                # Complete analysis data
                "analysis": analysis,
                "document_summary": analysis.get("document_summary", {}),
                "design_analysis": analysis.get("design_analysis", {}),
                "prompt_recommendations": analysis.get("prompt_recommendations", {}),
                
                # Enhanced output data for workflows
                "output_data": result.get("output_data", {}),
                
                # Processing information
                "document_stats": result.get("document_stats", {}),
                "processing_time": result.get("processing_time", 0),
                "recommendations": result.get("recommendations", []),
                
                # Workflow metadata
                "job_id": result.get("job_id"),
                "processed_files": result.get("processed_files", []),
                
                # Additional context for downstream nodes
                "design_context": design_context,
                "style_preferences": style_preferences,
                "output_type": output_type,
                
                # Ready-to-use outputs
                "text": primary_prompt,  # For text prompt nodes
                "enhanced_text": primary_prompt,  # For enhanced prompt nodes
                "design_prompt": primary_prompt,  # Semantic alias
                "generated_prompt": primary_prompt  # Alternative alias
            }
            
        except requests.exceptions.RequestException as e:
            # Provide helpful error message
            error_msg = str(e)
            
            if "Connection" in error_msg or "connect" in error_msg.lower():
                raise ValueError(
                    f"Cannot connect to animation API at {self.base_url}. "
                    f"Please ensure the animation API is running with document analysis support.\n"
                    f"Original error: {error_msg}"
                )
            elif "503" in error_msg or "Service Unavailable" in error_msg:
                raise ValueError(
                    f"Document analysis endpoint returned 503 Service Unavailable. "
                    f"This usually means Ollama is not running or not accessible.\n"
                    f"Please ensure Ollama is running on localhost:11434 with llama3.2 model.\n"
                    f"Original error: {error_msg}"
                )
            else:
                raise ValueError(f"Document analysis failed: {error_msg}")
        
        except Exception as e:
            raise ValueError(f"Document analysis error: {str(e)}")

    async def execute_generate_image(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute generate_image node - generates image only (no 3D model)

        Supports img2img generation when connected after nodes that provide reference images (sketches, drawings, etc.)
        """

        # Get prompt from previous node or config
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    # Priority 1: Check for fused_prompt from fusion enhancement
                    if "fused_prompt" in input_data:
                        prompt = input_data["fused_prompt"]
                        print(f"🔥 [WORKFLOW] Using fused prompt from upstream fusion node: {prompt[:100]}...")
                        break
                    # Priority 2: Check for enhanced_prompt from other enhancement methods
                    elif "enhanced_prompt" in input_data:
                        prompt = input_data["enhanced_prompt"]
                        break
                    # Priority 3: Check for basic prompt
                    elif "prompt" in input_data:
                        prompt = input_data["prompt"]
                        break

        if not prompt:
            raise ValueError("No prompt provided for image generation")

        # Check for reference/sketch image from config or previous nodes
        reference_image_url = node.config.get("reference_image_url") or node.config.get("image_url")
        reference_image_source = None
        reference_from_config = False

        if isinstance(reference_image_url, str) and reference_image_url.strip():
            reference_image_url = reference_image_url.strip()
            reference_from_config = True
            reference_image_source = node.config.get("reference_image_source") or "config_reference"

        # Explicitly check for sketch_url support: sketch node may output 'sketch_url'
        if (not reference_image_url) and inputs:
            # Look for image from previous nodes (sketch, drawing, or any image)
            for node_id, input_data in inputs.items():
                if isinstance(input_data, dict):
                    # Check for various image fields including sketch_url
                    for field in [
                        "sketch_url",
                        "sketch_path",
                        "image_url",
                        "s3_url",
                        "gemini_generated_url",
                        "drawing_url",
                        "optimized_3d_image_s3_url",
                        "fusion_image_url"
                    ]:
                        if field in input_data and input_data[field]:
                            reference_image_url = input_data[field]
                            reference_image_source = node_id
                            print(f"🖼️  Found reference image from node '{reference_image_source}' (field: {field})")
                            break
                    if reference_image_url:
                        break

        # If inputs indicate sketch type, mark source accordingly
        if not reference_image_source and inputs:
            for node_id, input_data in inputs.items():
                if isinstance(input_data, dict):
                    input_type = str(input_data.get("type", ""))
                    if input_type and any(keyword in input_type.lower() for keyword in ["sketch", "draw", "canvas"]):
                        reference_image_source = node_id or input_type
                        print(f"🖼️  Marking reference source as sketch from node '{reference_image_source}' (type: {input_type})")
                        break

        # Create a unique job ID for tracking
        job_id = str(uuid.uuid4())

        if reference_image_url:
            print(f"🎨 [WORKFLOW] Generating image with REFERENCE/SKETCH support")
            print(f"📝 Prompt: {prompt[:100]}...")
            preview_reference = reference_image_url[:80] + ("..." if len(reference_image_url) > 80 else "")
            print(f"🖼️  Reference image: {preview_reference}")
            print(f"🔄 Using img2img mode - will base generation on reference image")

            if isinstance(reference_image_url, str) and reference_image_url.startswith("data:"):
                print("📦 Reference image provided as base64 data URI")
            elif isinstance(reference_image_url, str) and not reference_image_url.lower().startswith("http"):
                print("⚠️ Reference image is not an HTTP URL - will pass raw value to generation API")

            # Check for strict identity flag (used for character consistency/aging)
            strict_identity = node.config.get("strict_identity", False)

            if strict_identity:
                print(f"🔒 [WORKFLOW] Strict Identity Preservation Mode ENABLED")
                enhanced_prompt = f"""
STRICT IDENTITY PRESERVATION REQUIRED.
Based on the provided reference image, create a new image with the following description:

{prompt}

CRITICAL INSTRUCTIONS:
- You MUST preserve the exact facial identity, features, and likeness of the person in the reference image.
- This is the SAME PERSON, just transformed according to the prompt (e.g. age, height).
- Do not change the person's identity or generate a generic face.
- Maintain the same clothing, pose, and background unless explicitly told otherwise.
- The reference image is the GROUND TRUTH for the character's appearance.

Reference-based generation for: {prompt}
"""
            else:
                # Standard reference-based generation prompt
                enhanced_prompt = f"""
Based on the provided reference image/sketch, create a new image with the following description:

{prompt}

Important:
- Use the reference image as the structural foundation
- Preserve the composition, layout, and key elements from the reference
- Apply the style, details, and enhancements described in the prompt
- If the reference is a sketch, add realistic details, colors, and textures
- Maintain the overall structure while improving quality and adding details

Reference-based generation for: {prompt}
"""

            # Call generation with reference context
            # Check if reference is from sketch node - disable RAG for sketches
            sketch_keywords = ['sketch', 'draw', 'canvas']
            is_sketch_reference = False

            if reference_image_source and any(keyword in str(reference_image_source).lower() for keyword in sketch_keywords):
                is_sketch_reference = True

            if not is_sketch_reference:
                for input_data in inputs.values():
                    if isinstance(input_data, dict):
                        input_type = str(input_data.get("type", ""))
                        if input_type and any(keyword in input_type.lower() for keyword in sketch_keywords):
                            is_sketch_reference = True
                            break
            if not is_sketch_reference and reference_from_config:
                config_source = str(node.config.get("reference_image_source", ""))
                if config_source and any(keyword in config_source.lower() for keyword in sketch_keywords):
                    is_sketch_reference = True
            
            if is_sketch_reference:
                print(f"🎨 Detected sketch reference - RAG disabled for direct sketch-to-image conversion")
                use_rag = False
                k_value = 0  # No RAG retrieval
            else:
                print(f"🖼️  Using reference image without RAG retrieval")
                use_rag = False  # Disable RAG when we have any reference image
                k_value = 0  # Avoid RAG to honor direct reference exclusively
            
            try:
                request_data = {
                    "prompt": enhanced_prompt,
                    "k": k_value,
                    "job_id": job_id,
                    "use_rag": use_rag
                }
                
                # Only add reference_image_url if the backend supports it
                if reference_image_url:
                    request_data["reference_image_url"] = reference_image_url
                if reference_image_source:
                    request_data["reference_image_source"] = reference_image_source
                if reference_from_config:
                    request_data["use_reference_image"] = "true"
                
                response = requests.post(
                    f"{self.base_url}/gen/gemini",
                    data=request_data,
                    timeout=180
                )

                response.raise_for_status()
                result = response.json()

                print(f"✅ Image generated with reference image guidance")

                # Extract image URLs from result
                image_url = result.get("generated_image_s3_url") or result.get("generated_image_path")

                return {
                    "job_id": job_id,
                    "image_url": image_url,
                    "s3_url": result.get("generated_image_s3_url"),
                    "gemini_generated_url": image_url,
                    "local_path": result.get("generated_image_path"),
                    "reference_image_url": reference_image_url,
                    "reference_source": reference_image_source,
                    "reference_from_config": reference_from_config,
                    "generation_mode": "img2img",
                    "status": "completed",
                    "prompt": prompt
                }

            except requests.exceptions.ConnectionError:
                error_msg = f"Cannot connect to Animation API at {self.base_url}. Please ensure animation_api.py is running."
                print(f"❌ {error_msg}")
                raise Exception(error_msg)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    error_msg = f"Endpoint /gen/gemini not found at {self.base_url}. Please ensure animation_api.py is running with the correct version."
                    print(f"❌ {error_msg}")
                    raise Exception(error_msg)
                else:
                    raise
            except Exception as e:
                print(f"❌ Error calling /gen/gemini with reference: {e}")
                raise Exception(f"Image generation with reference failed: {str(e)}")

        else:
            # Standard text-to-image generation without reference
            print(f"🎨 [WORKFLOW] Generating image with /gen/gemini endpoint (text-to-image)")
            print(f"📝 Prompt: {prompt[:100]}...")

            # Use /gen/gemini endpoint which generates images without triggering full animation pipeline
            try:
                response = requests.post(
                    f"{self.base_url}/gen/gemini",
                    data={
                        "prompt": prompt,
                        "k": 3,  # Number of reference images for RAG (not used when use_rag=False)
                        "job_id": job_id,
                        "use_rag": False  # Don't use RAG for generate_image node
                    },
                    timeout=180
                )

                response.raise_for_status()
                result = response.json()

                print(f"✅ Image generated successfully via /gen/gemini")

                # Extract image URLs from result
                image_url = result.get("generated_image_s3_url") or result.get("generated_image_path")

                return {
                    "job_id": job_id,
                    "image_url": image_url,
                    "s3_url": result.get("generated_image_s3_url"),
                    "gemini_generated_url": image_url,
                    "local_path": result.get("generated_image_path"),
                    "generation_mode": "text2img",
                    "status": "completed",
                    "prompt": prompt
                }

            except requests.exceptions.ConnectionError:
                error_msg = f"Cannot connect to Animation API at {self.base_url}. Please ensure animation_api.py is running."
                print(f"❌ {error_msg}")
                raise Exception(error_msg)
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    error_msg = f"Endpoint /gen/gemini not found at {self.base_url}. Please ensure animation_api.py is running with the correct version."
                    print(f"❌ {error_msg}")
                    raise Exception(error_msg)
                else:
                    raise
            except Exception as e:
                print(f"❌ Error calling /gen/gemini: {e}")
                raise Exception(f"Image generation failed: {str(e)}")
    
    async def execute_generate_rag(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute generate_rag node - generates image using RAG (Retrieval-Augmented Generation) with reference images"""
        
        # Get prompt from previous node or config
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "enhanced_prompt" in input_data:
                        prompt = input_data["enhanced_prompt"]
                        break
                    elif "prompt" in input_data:
                        prompt = input_data["prompt"]
                        break
                    elif "analysis" in input_data:
                        # Can use image analysis as prompt
                        prompt = input_data["analysis"]
                        break
        
        if not prompt:
            raise ValueError("No prompt provided for RAG image generation")
        
        # Get RAG configuration from node config
        k = int(node.config.get("k", "3"))  # Number of reference images to retrieve
        job_id = node.config.get("job_id") or str(uuid.uuid4())
        
        print(f"🔍 [WORKFLOW] Generating image with RAG using /gen/gemini endpoint")
        print(f"📝 Prompt: {prompt[:100]}...")
        print(f"📚 Retrieving {k} reference images from RAG index")
        
        # Use /gen/gemini endpoint with RAG parameters
        try:
            response = requests.post(
                f"{self.base_url}/gen/gemini",
                data={
                    "prompt": prompt,
                    "k": k,
                    "job_id": job_id,
                    "use_rag": True
                },
                timeout=180
            )
            
            response.raise_for_status()
            result = response.json()
            
            print(f"✅ RAG image generated successfully")
            
            # Extract image URLs and reference information from result
            image_url = result.get("generated_image_s3_url") or result.get("generated_image_path")
            reference_images = result.get("reference_images", [])
            
            if reference_images:
                print(f"📚 Used {len(reference_images)} reference images:")
                for ref in reference_images[:3]:  # Show top 3
                    print(f"   - {ref.get('path', 'unknown')} (score: {ref.get('score', 0):.3f})")
            else:
                print(f"⚠️ No reference images were used (text-only generation)")
            
            return {
                "job_id": job_id,
                "image_url": image_url,
                "s3_url": result.get("generated_image_s3_url"),
                "gemini_generated_url": image_url,
                "local_path": result.get("generated_image_path"),
                "reference_images": reference_images,
                "reference_count": len(reference_images),
                "k": k,
                "job_type": "rag_image_generation",
                "status": "completed",
                "prompt": prompt
            }
            
        except Exception as e:
            print(f"❌ Error calling /gen/gemini for RAG: {e}")
            raise Exception(f"RAG image generation failed: {str(e)}")
    
    async def execute_generate_nano_banana(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute generate_nano_banana node - generates images using Google's Nano Banana Pro
        
        High-quality image generation with transparent backgrounds, perfect for game assets.
        Pricing: Varies by resolution (2K/4K/8K)
        """
        
        # Get prompt from previous node or config
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "enhanced_prompt" in input_data:
                        prompt = input_data["enhanced_prompt"]
                        break
                    elif "fused_prompt" in input_data:
                        prompt = input_data["fused_prompt"]
                        break
                    elif "prompt" in input_data:
                        prompt = input_data["prompt"]
                        break
        
        if not prompt:
            raise ValueError("No prompt provided for Nano Banana Pro generation")
        
        # Get configuration from node
        resolution = node.config.get("resolution", "2K")  # 2K, 4K, or 8K
        aspect_ratio = node.config.get("aspect_ratio", "4:3")  # 1:1, 4:3, 16:9, 9:16, 3:4
        output_format = node.config.get("output_format", "png")  # png, jpg, webp
        safety_filter_level = node.config.get("safety_filter_level", "block_only_high")
        
        # Get optional image inputs (reference images)
        image_input = node.config.get("image_input") or node.config.get("image_url")
        
        # Parse image_input if it's a JSON string first (before any list operations)
        if image_input and isinstance(image_input, str):
            # Check if it's a JSON array string
            if image_input.strip().startswith('['):
                try:
                    image_input = json.loads(image_input)
                    print(f"📦 Parsed JSON array with {len(image_input)} image(s) from config")
                except json.JSONDecodeError as e:
                    print(f"⚠️ Failed to parse image_input as JSON: {e}")
                    # Treat as a single URL string
                    image_input = [image_input]
            else:
                # Single URL string - will be converted to list below
                pass
        
        # Handle base64 image - upload to S3 and get presigned URL
        if image_input and isinstance(image_input, str):
            if image_input.startswith("data:") or (not image_input.startswith("http")):
                print(f"🔄 [WORKFLOW] Detected base64 image_input, uploading to S3...")
                try:
                    # Decode base64
                    if image_input.startswith("data:"):
                        # Data URI format
                        header, b64data = image_input.split(",", 1)
                        try:
                            mime = header.split(";")[0].split(":")[1]
                            ext = mime.split("/")[-1]
                            if ext == "jpeg":
                                ext = "jpg"
                        except Exception:
                            ext = "png"
                    else:
                        # Raw base64
                        b64data = image_input
                        ext = "png"
                    
                    image_bytes = base64.b64decode(b64data)
                    
                    # Upload to S3
                    s3 = boto3.client('s3', region_name=AWS_REGION)
                    image_filename = f"nano_banana_input/{uuid.uuid4()}.{ext}"
                    
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=image_filename,
                        Body=image_bytes,
                        ContentType=f'image/{ext}'
                    )
                    
                    # Generate presigned URL (expires in 1 hour)
                    image_input = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': image_filename},
                        ExpiresIn=3600
                    )
                    
                    print(f"✅ [WORKFLOW] Image uploaded to S3: {image_filename}")
                    print(f"🔗 Presigned URL: {image_input[:80]}...")
                
                except Exception as e:
                    print(f"❌ Failed to process base64 image: {e}")
                    raise ValueError(f"Failed to upload image_input to S3: {str(e)}")
        
        # If no image_input in config, look for reference images from previous nodes
        if not image_input and inputs:
            temp_images = []
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    # Check all possible image URL fields (matching client-side logic)
                    # Priority order based on client-side implementation
                    image_url = (
                        input_data.get("sketch_data") or
                        input_data.get("image_url") or
                        input_data.get("s3_url") or
                        input_data.get("gemini_generated_url") or
                        input_data.get("optimized_3d_image_s3_url") or
                        input_data.get("transformed_image_s3_url") or
                        input_data.get("fusion_image_url") or
                        input_data.get("replicate_url") or
                        input_data.get("local_path")
                    )
                    
                    if image_url:
                        temp_images.append(image_url)
                        print(f"📎 Found reference image from previous node: {image_url[:80]}...")
            
            if temp_images:
                image_input = temp_images
        
        # Ensure image_input is a list (after JSON parsing and collection)
        if not image_input:
            image_input = []
        elif not isinstance(image_input, list):
            # Single URL string - convert to list
            image_input = [image_input]
        
        # Process base64 images in the list - upload to S3
        processed_image_list = []
        for idx, img in enumerate(image_input):
            if not img:
                continue
            
            img_str = str(img).strip()
            
            # Check if it's a base64 image
            if img_str.startswith("data:") or (not img_str.startswith("http")):
                print(f"🔄 [WORKFLOW] Detected base64 image #{idx+1} in list, uploading to S3...")
                try:
                    # Decode base64
                    if img_str.startswith("data:"):
                        # Data URI format
                        header, b64data = img_str.split(",", 1)
                        try:
                            mime = header.split(";")[0].split(":")[1]
                            ext = mime.split("/")[-1]
                            if ext == "jpeg":
                                ext = "jpg"
                        except Exception:
                            ext = "png"
                    else:
                        # Raw base64
                        b64data = img_str
                        ext = "png"
                    
                    image_bytes = base64.b64decode(b64data)
                    
                    # Upload to S3
                    s3 = boto3.client('s3', region_name=AWS_REGION)
                    image_filename = f"nano_banana_input/{uuid.uuid4()}.{ext}"
                    
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=image_filename,
                        Body=image_bytes,
                        ContentType=f'image/{ext}'
                    )
                    
                    # Generate presigned URL (expires in 1 hour)
                    presigned_url = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': image_filename},
                        ExpiresIn=3600
                    )
                    
                    processed_image_list.append(presigned_url)
                    print(f"✅ [WORKFLOW] Image #{idx+1} uploaded to S3: {image_filename}")
                    print(f"🔗 Presigned URL: {presigned_url[:80]}...")
                
                except Exception as e:
                    print(f"⚠️ Failed to process base64 image #{idx+1}: {e}")
                    # Skip this image but continue with others
                    continue
            else:
                # Already a URL, keep it
                processed_image_list.append(img_str)
        
        # Replace image_input with processed list
        image_input = processed_image_list
        
        # Generate job ID
        job_id = str(uuid.uuid4())
        
        print(f"🍌 [WORKFLOW] Generating image with Nano Banana Pro")
        print(f"📝 Prompt: {prompt[:100]}...")
        print(f"📐 Resolution: {resolution}, Aspect Ratio: {aspect_ratio}")
        print(f"🖼️  Format: {output_format}, Reference images: {len(image_input)}")
        
        try:
            # Prepare image_input as JSON array of URLs for Replicate
            # Filter and validate image URLs to ensure they're valid URIs
            valid_image_urls = []
            
            if image_input and len(image_input) > 0:
                for img_url in image_input:
                    if not img_url:
                        continue
                    
                    # Convert to string if needed
                    img_url_str = str(img_url).strip()
                    
                    # Validate that it's a proper HTTP/HTTPS URL
                    if img_url_str.startswith(('http://', 'https://')):
                        # Basic validation - must have valid URL structure
                        try:
                            from urllib.parse import urlparse
                            parsed = urlparse(img_url_str)
                            if parsed.scheme and parsed.netloc:
                                valid_image_urls.append(img_url_str)
                                print(f"✅ Valid reference image: {img_url_str[:80]}...")
                            else:
                                print(f"⚠️ Skipping invalid URL (no scheme/netloc): {img_url_str[:80]}...")
                        except Exception as e:
                            print(f"⚠️ Skipping malformed URL: {img_url_str[:80]}... - Error: {e}")
                    elif img_url_str.startswith('data:'):
                        # Base64 data URIs are not supported by Replicate for this model
                        print(f"⚠️ Skipping base64 data URI (not supported by Replicate): {img_url_str[:80]}...")
                    else:
                        print(f"⚠️ Skipping non-HTTP URL: {img_url_str[:80]}...")
            
            # Prepare image_input value
            if valid_image_urls:
                image_input_value = json.dumps(valid_image_urls)
                print(f"📎 Using {len(valid_image_urls)} valid reference image(s)")
            else:
                # No valid reference images - empty array
                image_input_value = "[]"
                if image_input and len(image_input) > 0:
                    print(f"⚠️ No valid HTTP/HTTPS URLs found in {len(image_input)} reference(s)")
                print(f"📝 Generating from prompt only (no reference images)")

            # Prepare form data (no files parameter - Replicate uses URLs)
            form_data = {
                "prompt": prompt,
                "resolution": resolution,
                "image_input": image_input_value,  # JSON string array of valid HTTP/HTTPS URLs
                "aspect_ratio": aspect_ratio,
                "output_format": output_format,
                "safety_filter_level": safety_filter_level,
                "job_id": job_id
            }
            
            # Send as form data only (no files upload for Replicate)
            response = requests.post(
                f"{self.base_url}/gen/nano-banana-pro",
                data=form_data,
                timeout=300  # 5 minutes for high-quality generation
            )
            
            response.raise_for_status()
            result = response.json()
            
            print(f"✅ Nano Banana Pro image generated successfully")
            
            # Extract image URLs from result
            image_url = result.get("image_url")
            local_path = result.get("local_path") or result.get("generated_image_path")
            replicate_url = result.get("replicate_url")
            
            return {
                "job_id": job_id,
                "image_url": image_url,
                "local_path": local_path,
                "generated_image_path": local_path,
                "replicate_url": replicate_url,
                "prompt": prompt,
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
                "output_format": output_format,
                "generation_mode": "nano_banana_pro",
                "status": "completed",
                "model": "google/nano-banana-pro"
            }
            
        except requests.exceptions.ConnectionError:
            error_msg = f"Cannot connect to Animation API at {self.base_url}. Please ensure animation_api.py is running."
            print(f"❌ {error_msg}")
            raise Exception(error_msg)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                error_msg = f"Endpoint /gen/nano-banana-pro not found at {self.base_url}. Please ensure animation_api.py is running with Nano Banana Pro support."
                print(f"❌ {error_msg}")
                raise Exception(error_msg)
            else:
                raise
        except Exception as e:
            print(f"❌ Error calling /gen/nano-banana-pro: {e}")
            raise Exception(f"Nano Banana Pro generation failed: {str(e)}")
    
    async def execute_generate_3d(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute generate_3d node - generates 3D model from image"""
        
        # Get image from previous node or config
        image_url = node.config.get("image_url")
        if not image_url and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    # Check all possible image URL fields
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
                    elif "gemini_generated_url" in input_data:
                        image_url = input_data["gemini_generated_url"]
                        break
                    elif "optimized_3d_image_s3_url" in input_data:
                        image_url = input_data["optimized_3d_image_s3_url"]
                        break
        
        if not image_url:
            # Debug: print what we received
            print(f"DEBUG: No image URL found. Inputs received: {inputs}")
            raise ValueError("No image provided for 3D generation")
        
        # WORKFLOW POLICY: Always skip rigging in workflow execution
        # Rigging is disabled by default in workflows to avoid pipeline failures
        skip_rigging = True
        print(f"⚡ [WORKFLOW] Rigging is DISABLED by default in workflow execution")
        
        # Allow manual override if explicitly requested
        if node.config.get("enable_rigging", "false").lower() == "true":
            skip_rigging = False
            print(f"🤖 [WORKFLOW] Rigging manually ENABLED via config (enable_rigging=true)")
        
        animation_ids = node.config.get("animation_ids", '' if skip_rigging else "106,30,55")
        height_meters = float(node.config.get("height_meters", "1.75"))
        fps = int(node.config.get("fps", "60"))
        use_retexture = node.config.get("use_retexture", "false").lower() == "true"
        
        # Final confirmation log
        print(f"🔧 [WORKFLOW] 3D Generation settings:")
        print(f"   • Rigging: {'DISABLED ⚠️' if skip_rigging else 'ENABLED'}")
        print(f"   • animation_ids: '{animation_ids}' {'(empty = skip rigging)' if not animation_ids else ''}")
        print(f"   • height_meters: {height_meters}")
        print(f"   • fps: {fps}")
        
        # Download image
        img_response = requests.get(image_url, timeout=30)
        img_response.raise_for_status()
        
        # Upload to animate/image endpoint
        files = {'file': ('image.jpg', BytesIO(img_response.content), 'image/jpeg')}
        data = {
            'animation_ids': animation_ids,  # Empty string skips rigging
            'height_meters': str(height_meters),
            'fps': str(fps),
            'use_retexture': str(use_retexture).lower(),
            'skip_confirmation': 'true'  # Workflows always skip confirmation (string for form data)
        }
        
        print(f"⚡ [WORKFLOW] Setting skip_confirmation=true for automated workflow execution")
        
        response = requests.post(
            f"{self.base_url}/animate/image",
            files=files,
            data=data,
            timeout=600
        )
        
        response.raise_for_status()
        result = response.json()
        
        job_id = result.get("forge_id") or result.get("job_id")
        
        # Poll for completion
        max_retries = 120
        for i in range(max_retries):
            time.sleep(10)
            
            status_response = requests.get(f"{self.base_url}/status/{job_id}")
            status_response.raise_for_status()
            status_data = status_response.json()
            
            if status_data.get("status") == "completed":
                result_data = status_data.get("result", {})
                
                # When rigging is skipped, URLs might be in different fields
                model_url = (
                    result_data.get("model_url") or 
                    result_data.get("glb_url") or
                    result_data.get("s3_direct_url") or
                    ""
                )
                
                print(f"✅ [WORKFLOW] 3D model generation completed!")
                print(f"   • Model URL: {model_url[:80] if model_url else 'None'}...")
                print(f"   • Rigging status: {result_data.get('rigging_status', 'unknown')}")
                print(f"   • Animations: {len(result_data.get('animation_urls', []))}")
                
                return {
                    "job_id": job_id,
                    "model_url": model_url,
                    "glb_url": model_url,  # Alias
                    "s3_direct_url": result_data.get("s3_direct_url"),
                    "animation_urls": result_data.get("animation_urls", []),
                    "rigging_status": result_data.get("rigging_status", "unknown"),
                    "processing_method": result_data.get("processing_method", "unknown"),
                    "status": "completed"
                }
            elif status_data.get("status") == "failed":
                error_message = status_data.get("message", "Unknown error")
                print(f"❌ [WORKFLOW] 3D generation failed: {error_message}")
                raise Exception(f"3D generation failed: {error_message}")
            
            # Log progress every 30 seconds
            if i % 3 == 0:
                progress = status_data.get("progress", 0)
                message = status_data.get("message", "Processing...")
                print(f"⏳ [WORKFLOW] Job {job_id} progress: {progress}% - {message}")
        
        raise Exception("3D generation timed out after 20 minutes")
    
    async def execute_animate_luma(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute animate_luma node - animates with Luma Ray 2"""
        
        # Get image from previous node or config
        image_url = node.config.get("image_url")
        if not image_url and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
                    elif "glb_url" in input_data:
                        image_url = input_data["glb_url"]
                        break
                    elif "model_url" in input_data:
                        image_url = input_data["model_url"]
                        break
        
        # Get prompt
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict) and "enhanced_prompt" in input_data:
                    prompt = input_data
        if not prompt:
            raise ValueError("No prompt provided for animation")
        
        resolution = node.config.get("resolution", "720p")
        duration = node.config.get("duration", "5s")
        concepts = node.config.get("concepts")
        
        # Validate duration for ray-2 model
        model = node.config.get("model", "ray-2")
        if model == "ray-2" and duration not in ["5s", "9s"]:
            # ray-2 only supports 5s and 9s durations
            print(f"⚠️  Warning: ray-2 model only supports 5s or 9s duration. Converting {duration} to 9s")
            duration = "9s"
        
        data = {
            'prompt': prompt,
            'resolution': resolution,
            'duration': duration
        }
        
        if image_url:
            data['image_url'] = image_url
        if concepts:
            data['concepts'] = concepts
        
        response = requests.post(
            f"{self.base_url}/animate/luma",
            data=data,
            timeout=600
        )
        
        response.raise_for_status()
        result = response.json()
        
        return {
            "job_id": result.get("job_id"),
            "video_url": result.get("video_url"),
            "animation_url": result.get("animation_url"),
            "status": "completed"
        }
    
    async def execute_animate_easy(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute animate_easy node - animates with Segmind Easy Animate"""
        
        # Get image from previous node
        image_url = node.config.get("image_url")
        if not image_url and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
        
        if not image_url:
            raise ValueError("No image provided for animation")
        
        # Get prompt
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict) and "enhanced_prompt" in input_data:
                    prompt = input_data["enhanced_prompt"]
                    break
        
        if not prompt:
            raise ValueError("No prompt provided for animation")
        
        data = {
            'image_url': image_url,
            'prompt': prompt,
            'video_length': int(node.config.get("video_length", "72")),
            'frame_rate': int(node.config.get("frame_rate", "24")),
            'steps': int(node.config.get("steps", "25")),
            'cfg': float(node.config.get("cfg", "7.0"))
        }
        
        response = requests.post(
            f"{self.base_url}/animate/easy",
            data=data,
            timeout=600
        )
        
        response.raise_for_status()
        result = response.json()
        
        return {
            "job_id": result.get("job_id"),
            "video_url": result.get("video_url"),
            "status": "completed"
        }
    
    async def execute_animate_wan(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute animate_wan node - animates with WAN 2.2"""
        
        # Get image from previous node
        image_url = node.config.get("image")
        if not image_url and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
        
        if not image_url:
            raise ValueError("No image provided for WAN animation")
        
        prompt = node.config.get("prompt", "")
        resolution = node.config.get("resolution", "480p")
        seed = int(node.config.get("seed", "42"))
        
        response = requests.post(
            f"{self.base_url}/animate/wan",
            json={
                "image": image_url,
                "prompt": prompt,
                "resolution": resolution,
                "seed": seed
            },
            timeout=600
        )
        
        response.raise_for_status()
        result = response.json()
        
        return {
            "job_id": result.get("job_id"),
            "video_url": result.get("video_url"),
            "status": "completed"
        }
    
    async def execute_animate_wan_fast(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute animate_wan_fast node - WAN 2.2 i2v-fast via Replicate
        
        Fast image-to-video animation with pricing:
        - 480p: $0.05 per video
        - 720p: $0.11 per video
        """
        
        # Get image from previous node or config
        image_url = node.config.get("image")
        if not image_url and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
                    elif "gemini_generated_url" in input_data:
                        image_url = input_data["gemini_generated_url"]
                        break
        
        if not image_url:
            raise ValueError("No image provided for WAN 2.2 i2v-fast animation")
        
        # Get animation prompt - user MUST provide this explicitly
        # Check for animation-specific prompt field first
        prompt = node.config.get("animation_prompt", "").strip()
        
        # If no animation_prompt, check regular prompt but IGNORE if it matches previous node's prompt
        if not prompt:
            prompt = node.config.get("prompt", "").strip()
            
            # Check if this prompt came from previous node (would be in inputs too)
            if prompt and inputs:
                for input_data in inputs.values():
                    if isinstance(input_data, dict):
                        # If the prompt matches what came from previous node, ignore it
                        if input_data.get("prompt") == prompt or input_data.get("enhanced_prompt") == prompt:
                            print(f"⚠️  Ignoring prompt '{prompt[:50]}...' - appears to be from previous node")
                            prompt = ""
                            break
        
        if not prompt:
            raise ValueError(
                "No animation prompt provided. Please specify how to animate the image. "
                "Example: 'slowly pan across the scene', 'zoom in dramatically', 'gentle floating motion'"
            )
        
        # Get configuration
        resolution = node.config.get("resolution", "480p")
        
        # Validate and map resolution - WAN 2.2 i2v-fast only supports 480p and 720p
        resolution_mapping = {
            "480p": "480p",
            "720p": "720p",
            "1080p": "720p",  # Map to highest supported
            "2K": "720p",
            "4K": "720p",
            "8K": "720p"
        }
        
        if resolution not in resolution_mapping:
            print(f"⚠️  Unknown resolution '{resolution}', defaulting to 480p")
            resolution = "480p"
        else:
            original_resolution = resolution
            resolution = resolution_mapping[resolution]
            if original_resolution != resolution:
                print(f"⚠️  Resolution '{original_resolution}' not supported, using '{resolution}' instead")
        
        go_fast = node.config.get("go_fast", "true").lower() == "true"
        num_frames = int(node.config.get("num_frames", "81"))
        sample_shift = int(node.config.get("sample_shift", "12"))
        frames_per_second = int(node.config.get("frames_per_second", "16"))
        interpolate_output = node.config.get("interpolate_output", "true").lower() == "true"
        
        print(f"🎬 [WORKFLOW] WAN 2.2 i2v-fast animation (Direct Replicate)")
        print(f"🖼️  Image URL: {image_url}")
        print(f"📝 Animation Prompt: {prompt}")
        print(f"🎯 Resolution: {resolution} ({'$0.05' if resolution == '480p' else '$0.11'})")
        
        if not REPLICATE_API_KEY:
            raise ValueError("REPLICATE_API_KEY not found in environment variables")
        
        import replicate
        client = replicate.Client(api_token=REPLICATE_API_KEY)
        
        try:
            # Call Replicate directly
            print(f"🔄 Calling Replicate WAN 2.2 i2v-fast model...")
            
            input_params = {
                "image": image_url,
                "prompt": prompt,
                "go_fast": go_fast,
                "num_frames": num_frames,
                "sample_shift": sample_shift,
                "frames_per_second": frames_per_second,
                "interpolate_output": interpolate_output,
                "resolution": resolution,  # Keep "480p" format
                "lora_scale_transformer": 1,
                "lora_scale_transformer_2": 1
            }
            
            # Debug: Log what we're sending to Replicate
            print(f"📤 Replicate Input Parameters:")
            print(f"   - image: {input_params['image']}")
            print(f"   - prompt: {input_params['prompt']}")
            print(f"   - resolution: {input_params['resolution']}")
            print(f"   - go_fast: {input_params['go_fast']}")
            print(f"   - num_frames: {input_params['num_frames']}")
            
            output = replicate.run(
                "wan-video/wan-2.2-i2v-fast",  # Correct WAN 2.2 i2v-fast model
                input=input_params
            )
            
            # Output is a FileOutput object with .url() method
            video_url = None
            if hasattr(output, 'url'):
                # Call the url() method if it's callable
                video_url = output.url() if callable(output.url) else output.url
            elif isinstance(output, str):
                video_url = output
            elif isinstance(output, list) and len(output) > 0:
                first_output = output[0]
                if hasattr(first_output, 'url'):
                    video_url = first_output.url() if callable(first_output.url) else first_output.url
                elif isinstance(first_output, str):
                    video_url = first_output
            
            if not video_url:
                raise ValueError("No video URL returned from Replicate")
            
            print(f"✅ Replicate returned video URL: {video_url[:100]}...")
            
            # Upload to S3 for persistent storage
            try:
                print(f"📥 Downloading video from Replicate...")
                video_response = requests.get(video_url, timeout=300)
                video_response.raise_for_status()
                
                s3 = boto3.client('s3', region_name=AWS_REGION)
                video_filename = f"animations/wan_fast_{uuid.uuid4()}.mp4"
                
                print(f"☁️  Uploading to S3...")
                s3.put_object(
                    Bucket=S3_BUCKET,
                    Key=video_filename,
                    Body=video_response.content,
                    ContentType='video/mp4'
                )
                
                s3_url = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': S3_BUCKET, 'Key': video_filename},
                    ExpiresIn=86400
                )
                
                print(f"✅ Video uploaded to S3: {video_filename}")
                video_url = s3_url  # Use S3 URL as primary
                
            except Exception as e:
                print(f"⚠️  Failed to upload to S3: {e}")
                print(f"📎 Using Replicate URL instead")
            
            print(f"✅ WAN 2.2 i2v-fast animation completed!")
            print(f"🎥 Video: {video_url[:80]}...")
            print(f"💰 Cost: {'$0.05' if resolution == '480p' else '$0.11'}")
            
            return {
                "job_id": str(uuid.uuid4()),
                "video_url": video_url,
                "video_s3_url": video_url if 's3' in video_url else None,
                "replicate_url": output.url if hasattr(output, 'url') else str(output),
                "image": image_url,
                "prompt": prompt,
                "resolution": resolution,
                "settings": {
                    "go_fast": go_fast,
                    "num_frames": num_frames,
                    "sample_shift": sample_shift,
                    "frames_per_second": frames_per_second,
                    "interpolate_output": interpolate_output
                },
                "status": "completed"
            }
            
        except Exception as e:
            print(f"❌ WAN 2.2 i2v-fast failed: {e}")
            raise Exception(f"WAN 2.2 i2v-fast animation failed: {str(e)}")
    
    async def execute_fusion(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute fusion node - fuses two images together to create a hybrid"""
        
        # Get images from previous nodes or config
        image1_url = node.config.get("image1_url") or node.config.get("image_url") or node.config.get("image")
        image2_url = node.config.get("image2_url")
        
        # Handle base64 for image1_url - upload to S3 and get presigned URL
        if image1_url and isinstance(image1_url, str):
            if image1_url.startswith("data:") or (not image1_url.startswith("http")):
                print(f"🔄 [WORKFLOW] Image 1 is base64, uploading to S3...")
                try:
                    # Parse data URI or raw base64
                    if image1_url.startswith("data:"):
                        # Extract MIME type and base64 data
                        header, b64data = image1_url.split(",", 1)
                        mime_match = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
                        ext = mime_match.split("/")[1] if "/" in mime_match else "png"
                    else:
                        # Raw base64 without data URI
                        b64data = image1_url
                        ext = "png"
                    
                    # Decode base64
                    image_bytes = base64.b64decode(b64data)
                    
                    # Upload to S3
                    s3 = boto3.client('s3', region_name=AWS_REGION)
                    image_filename = f"fusion_input1/{uuid.uuid4()}.{ext}"
                    
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=image_filename,
                        Body=image_bytes,
                        ContentType=f'image/{ext}'
                    )
                    
                    # Generate presigned URL (1 hour expiry)
                    image1_url = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': image_filename},
                        ExpiresIn=3600
                    )
                    
                    print(f"✅ Image 1 uploaded to S3: {image_filename}")
                    
                except Exception as e:
                    print(f"❌ Failed to process base64 image 1: {e}")
                    raise ValueError(f"Failed to process base64 image 1: {e}")
        
        # Handle base64 for image2_url - upload to S3 and get presigned URL
        if image2_url and isinstance(image2_url, str):
            if image2_url.startswith("data:") or (not image2_url.startswith("http")):
                print(f"🔄 [WORKFLOW] Image 2 is base64, uploading to S3...")
                try:
                    # Parse data URI or raw base64
                    if image2_url.startswith("data:"):
                        # Extract MIME type and base64 data
                        header, b64data = image2_url.split(",", 1)
                        mime_match = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
                        ext = mime_match.split("/")[1] if "/" in mime_match else "png"
                    else:
                        # Raw base64 without data URI
                        b64data = image2_url
                        ext = "png"
                    
                    # Decode base64
                    image_bytes = base64.b64decode(b64data)
                    
                    # Upload to S3
                    s3 = boto3.client('s3', region_name=AWS_REGION)
                    image_filename = f"fusion_input2/{uuid.uuid4()}.{ext}"
                    
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=image_filename,
                        Body=image_bytes,
                        ContentType=f'image/{ext}'
                    )
                    
                    # Generate presigned URL (1 hour expiry)
                    image2_url = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': image_filename},
                        ExpiresIn=3600
                    )
                    
                    print(f"✅ Image 2 uploaded to S3: {image_filename}")
                    
                except Exception as e:
                    print(f"❌ Failed to process base64 image 2: {e}")
                    raise ValueError(f"Failed to process base64 image 2: {e}")
        
        # Try to extract images from previous nodes if not provided in config
        if not image1_url or not image2_url:
            image_data = []  # List of (node_id, url) tuples
            
            for node_id, input_data in inputs.items():
                if isinstance(input_data, dict):
                    # Check all possible image URL fields
                    for field in ["image_url", "s3_url", "gemini_generated_url", "optimized_3d_image_s3_url", "fusion_image_url"]:
                        if field in input_data and input_data[field]:
                            url = input_data[field]
                            image_data.append((node_id, url))
                            print(f"📸 Found image from node '{node_id}': {url[:80]}...")
                            break  # Only take first image from each node
                
                if len(image_data) >= 2:
                    break
            
            # Assign found URLs
            if not image1_url and len(image_data) > 0:
                image1_url = image_data[0][1]
                print(f"✅ Image 1 from node: {image_data[0][0]}")
            
            if not image2_url and len(image_data) > 1:
                image2_url = image_data[1][1]
                print(f"✅ Image 2 from node: {image_data[1][0]}")
        
        if not image1_url or not image2_url:
            available_nodes = list(inputs.keys()) if inputs else []
            raise ValueError(
                f"Two images required for fusion. Found: image1={'Yes' if image1_url else 'No'}, image2={'Yes' if image2_url else 'No'}. "
                f"Connected nodes: {available_nodes}"
            )
        
        # Get fusion configuration
        fusion_style = node.config.get("fusion_style", "blend")
        fusion_strength = float(node.config.get("fusion_strength", "0.5"))
        realistic_mode = node.config.get("realistic_mode", "false").lower() == "true"
        animation_ids = node.config.get("animation_ids", "")
        height_meters = float(node.config.get("height_meters", "1.75"))
        fps = int(node.config.get("fps", "60"))
        use_retexture = node.config.get("use_retexture", "false").lower() == "true"
        
        print(f"🔀 [WORKFLOW] Fusing two images")
        print(f"🖼️  Image 1: {image1_url[:80]}...")
        print(f"🖼️  Image 2: {image2_url[:80]}...")
        print(f"🎨 Fusion style: {fusion_style}")
        print(f"⚖️  Fusion strength: {fusion_strength}")
        print(f"📸 Realistic mode: {'Ultra Realistic Photo' if realistic_mode else 'Artistic/Drawing Style'}")
        
        try:
            # Process images (download or decode base64)
            print(f"📥 Processing images...")
            
            # Process image 1
            if image1_url.startswith("data:") or (not image1_url.startswith("http")):
                if image1_url.startswith("data:"):
                    header, b64data = image1_url.split(",", 1)
                else:
                    b64data = image1_url
                img1_bytes = base64.b64decode(b64data)
            else:
                img1_response = requests.get(image1_url, timeout=30)
                img1_response.raise_for_status()
                img1_bytes = img1_response.content
            
            # Process image 2
            if image2_url.startswith("data:") or (not image2_url.startswith("http")):
                if image2_url.startswith("data:"):
                    header, b64data = image2_url.split(",", 1)
                else:
                    b64data = image2_url
                img2_bytes = base64.b64decode(b64data)
            else:
                img2_response = requests.get(image2_url, timeout=30)
                img2_response.raise_for_status()
                img2_bytes = img2_response.content
            
            # Call the fusion endpoint
            files = {
                'file1': ('image1.jpg', BytesIO(img1_bytes), 'image/jpeg'),
                'file2': ('image2.jpg', BytesIO(img2_bytes), 'image/jpeg')
            }
            
            data = {
                'animation_ids': animation_ids,
                'height_meters': str(height_meters),
                'fps': str(fps),
                'use_retexture': str(use_retexture).lower(),
                'fusion_style': fusion_style,
                'fusion_strength': str(fusion_strength),
                'realistic_mode': str(realistic_mode).lower()
            }
            
            print(f"🚀 Calling fusion endpoint...")
            if realistic_mode:
                print(f"📸 Ultra Realistic Mode enabled - output will be photo-realistic")
            else:
                print(f"🎨 Artistic Mode - preserving drawing/artistic style")
            
            response = requests.post(
                f"{self.base_url}/animate/infuse-database-only",
                files=files,
                data=data,
                timeout=600
            )
            
            response.raise_for_status()
            result = response.json()
            
            job_id = result.get("forge_id") or result.get("job_id")
            print(f"⏳ Fusion job started: {job_id}")
            
            # Poll for completion
            max_retries = 120
            for i in range(max_retries):
                time.sleep(10)
                
                status_response = requests.get(f"{self.base_url}/status/{job_id}")
                status_response.raise_for_status()
                status_data = status_response.json()
                
                if status_data.get("status") == "completed":
                    result_data = status_data.get("result", {})
                    
                    print(f"✅ [WORKFLOW] Fusion completed!")
                    
                    # Extract fusion results
                    fusion_result = {
                        "job_id": job_id,
                        "job_type": "fusion",
                        "fusion_style": fusion_style,
                        "fusion_strength": fusion_strength,
                        "realistic_mode": realistic_mode,
                        "rendering_style": "photo-realistic" if realistic_mode else "artistic",
                        "status": "completed"
                    }
                    
                    # Add fusion image if available
                    if "fusion_image" in result_data:
                        fusion_img = result_data["fusion_image"]
                        fusion_result["fusion_image_url"] = fusion_img.get("s3_url") or fusion_img.get("s3_presigned_url")
                        fusion_result["image_url"] = fusion_result["fusion_image_url"]
                        fusion_result["s3_url"] = fusion_result["fusion_image_url"]
                    
                    # Add source images info
                    if "source_images" in result_data:
                        fusion_result["source_images"] = result_data["source_images"]
                    
                    # Add 3D model if available
                    if "model_url" in result_data or "glb_url" in result_data:
                        fusion_result["model_url"] = result_data.get("model_url") or result_data.get("glb_url")
                        fusion_result["glb_url"] = fusion_result["model_url"]
                    
                    # Add animations if available
                    if "animation_urls" in result_data:
                        fusion_result["animation_urls"] = result_data["animation_urls"]
                    
                    # Add fusion analysis
                    if "analysis" in result_data:
                        fusion_result["analysis"] = result_data["analysis"]
                    
                    if "fusion_prompt" in result_data:
                        fusion_result["fusion_prompt"] = result_data["fusion_prompt"]
                    
                    print(f"   • Fusion image: {fusion_result.get('fusion_image_url', 'N/A')[:80]}...")
                    print(f"   • Model: {fusion_result.get('model_url', 'N/A')[:80] if fusion_result.get('model_url') else 'Not generated'}...")
                    
                    return fusion_result
                    
                elif status_data.get("status") == "failed":
                    error_message = status_data.get("message", "Unknown error")
                    print(f"❌ [WORKFLOW] Fusion failed: {error_message}")
                    raise Exception(f"Fusion failed: {error_message}")
                
                # Log progress every 30 seconds
                if i % 3 == 0:
                    progress = status_data.get("progress", 0)
                    message = status_data.get("message", "Processing...")
                    print(f"⏳ [WORKFLOW] Fusion job {job_id} progress: {progress}% - {message}")
            
            raise Exception("Fusion timed out after 20 minutes")
            
        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            
            if "Connection" in error_msg or "connect" in error_msg.lower():
                raise ValueError(
                    f"Cannot connect to animation API at {self.base_url}. "
                    f"Please ensure the animation API is running."
                )
            else:
                raise ValueError(f"Fusion request failed: {error_msg}")
    
    async def execute_veo_3_fast(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute veo_3_fast node - Google Veo 3 Fast image/text to video
        
        Ultra-fast video generation optimized for:
        - Game character presentations
        - Game commercials
        - Action scenes
        - Cinematic cutscenes
        
        Pricing: ~$0.12 per video
        """
        
        # Get input type (image or text)
        input_type = node.config.get("input_type", "image")
        
        # Check if auto-chaining is enabled (need to handle base64 differently)
        target_duration = node.config.get("target_duration")
        enable_auto_chain = target_duration and target_duration != "single"
        
        # Get image from previous node or config (for I2V mode)
        image_url = None
        if input_type == "image":
            image_url = node.config.get("image") or node.config.get("image_url")
            
            print(f"🔍 Looking for input image...")
            print(f"   From config 'image': {node.config.get('image')[:100] if node.config.get('image') else 'None'}")
            print(f"   From config 'image_url': {node.config.get('image_url')[:100] if node.config.get('image_url') else 'None'}")
            
            if not image_url and inputs:
                print(f"   Checking {len(inputs)} input nodes...")
                for node_id, input_data in inputs.items():
                    print(f"   Input from {node_id}: {type(input_data)}")
                    if isinstance(input_data, dict):
                        # For auto-chain mode, prioritize last_frame_url and image_url
                        # Also check input_image field which may contain the continuation image
                        for field in ["last_frame_url", "image_url", "input_image", "s3_url", "gemini_generated_url", "fusion_image_url"]:
                            if field in input_data and input_data[field]:
                                print(f"   Found '{field}': {input_data[field][:100] if isinstance(input_data[field], str) else input_data[field]}...")
                                image_url = input_data[field]
                                break
                    if image_url:
                        break
            
            if not image_url:
                raise ValueError("No image provided for Veo 3 Fast I2V generation. Connect an image node or switch to text-to-video mode.")
            
            # Validate that we have an image, not a video
            if image_url.lower().endswith(('.mp4', '.mov', '.avi', '.webm', '.mkv')):
                raise ValueError(
                    f"Invalid input: Received video URL instead of image URL. "
                    f"Veo 3 Fast I2V mode requires a single image frame, not a video file. "
                    f"URL: {image_url[:100]}... "
                    f"This usually means last frame extraction failed in the previous node."
                )
            
            # If image is base64, upload to S3 first (required for Replicate API)
            if image_url and isinstance(image_url, str) and image_url.startswith("data:"):
                print(f"� Converting base64 input image to S3 URL (required by Veo 3 Fast)...")
                try:
                    # Parse data URI
                    header, encoded = image_url.split(",", 1)
                    image_data = base64.b64decode(encoded)
                    
                    # Determine format from data URI
                    image_format = "png"
                    if "jpeg" in header or "jpg" in header:
                        image_format = "jpeg"
                    elif "png" in header:
                        image_format = "png"
                    elif "webp" in header:
                        image_format = "webp"
                    
                    # Upload to S3
                    s3 = boto3.client('s3', region_name=AWS_REGION)
                    input_image_filename = f"veo3_frames/input_frame_{uuid.uuid4()}.{image_format}"
                    
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=input_image_filename,
                        Body=image_data,
                        ContentType=f'image/{image_format}'
                    )
                    
                    # Use presigned URL with 7 days expiry for reliability
                    image_url = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': input_image_filename},
                        ExpiresIn=604800  # 7 days
                    )
                    
                    print(f"✅ Input image uploaded to S3: {input_image_filename}")
                    print(f"🔗 S3 URL: {image_url[:80]}...")
                    
                except Exception as e:
                    print(f"⚠️ Failed to upload input image to S3: {e}")
                    raise ValueError(f"Failed to process base64 image: {str(e)}")
        
        # Get prompt
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "enhanced_prompt" in input_data:
                        prompt = input_data["enhanced_prompt"]
                        break
                    elif "prompt" in input_data:
                        prompt = input_data["prompt"]
                        break
        
        if not prompt:
            raise ValueError("No prompt provided for Veo 3 Fast video generation")
        
        # Get video purpose and enhance prompt accordingly
        video_purpose = node.config.get("video_purpose", "general")
        
        # Enhance prompt based on purpose
        purpose_enhancements = {
            "character_presentation": "professional game character showcase, smooth rotation, detailed presentation, game engine quality, ",
            "game_commercial": "cinematic game commercial, dramatic lighting, professional advertisement quality, engaging presentation, ",
            "action_scene": "dynamic game action sequence, intense motion, combat scene, high energy gameplay footage, ",
            "cinematic": "cinematic cutscene quality, movie-like production, dramatic storytelling, high-end rendering, ",
            "general": ""
        }
        
        enhanced_prompt = purpose_enhancements.get(video_purpose, "") + prompt
        
        # Get configuration
        duration = int(node.config.get("duration", "8"))
        resolution = node.config.get("resolution", "720p")
        aspect_ratio = node.config.get("aspect_ratio", "16:9")
        generate_audio = node.config.get("generate_audio", "false").lower() == "true"
        
        # Validate aspect_ratio - Veo 3 Fast only supports 16:9 and 9:16
        valid_aspect_ratios = ["16:9", "9:16"]
        if aspect_ratio not in valid_aspect_ratios:
            original_aspect = aspect_ratio
            # Map common aspect ratios to closest valid option
            if "9:16" in aspect_ratio or "vertical" in aspect_ratio.lower() or "portrait" in aspect_ratio.lower():
                aspect_ratio = "9:16"
            elif "16:9" in aspect_ratio or "horizontal" in aspect_ratio.lower() or "landscape" in aspect_ratio.lower():
                aspect_ratio = "16:9"
            else:
                # Try to parse ratio and determine orientation
                try:
                    parts = aspect_ratio.split(":")
                    if len(parts) == 2:
                        w, h = float(parts[0]), float(parts[1])
                        aspect_ratio = "9:16" if h > w else "16:9"
                    else:
                        aspect_ratio = "16:9"  # Default to landscape
                except:
                    aspect_ratio = "16:9"  # Default to landscape
            print(f"⚠️  Veo 3 Fast only supports 16:9 or 9:16 aspect ratios. Adjusted '{original_aspect}' → '{aspect_ratio}'")
        
        # Validate resolution - Veo 3 Fast only supports 720p and 1080p
        valid_resolutions = ["720p", "1080p"]
        if resolution not in valid_resolutions:
            original_resolution = resolution
            # Map common resolutions to closest valid option
            resolution_lower = resolution.lower()
            if "1080" in resolution_lower or "2k" in resolution_lower or "4k" in resolution_lower or "8k" in resolution_lower:
                resolution = "1080p"
            elif "720" in resolution_lower or "480" in resolution_lower or "360" in resolution_lower or "sd" in resolution_lower:
                resolution = "720p"
            else:
                # Try to parse numeric resolution
                try:
                    # Extract numbers from resolution string
                    import re
                    numbers = re.findall(r'\d+', resolution)
                    if numbers:
                        res_value = int(numbers[0])
                        resolution = "1080p" if res_value >= 900 else "720p"
                    else:
                        resolution = "720p"  # Default to 720p
                except:
                    resolution = "720p"  # Default to 720p
            print(f"⚠️  Veo 3 Fast only supports 720p or 1080p resolutions. Adjusted '{original_resolution}' → '{resolution}'")
        
        # Advanced options
        camera_motion = node.config.get("camera_motion", "auto")
        motion_intensity = node.config.get("motion_intensity", "medium")
        style_preset = node.config.get("style_preset", "auto")
        loop_video = node.config.get("loop_video", "false").lower() == "true"
        seed = node.config.get("seed")
        
        # Add camera motion and style to prompt if specified
        if camera_motion != "auto":
            camera_instructions = {
                "static": "static camera, no camera movement",
                "pan_left": "camera pans left smoothly",
                "pan_right": "camera pans right smoothly",
                "zoom_in": "camera zooms in dramatically",
                "zoom_out": "camera zooms out revealing scene",
                "dolly_forward": "camera dollies forward",
                "dolly_backward": "camera dollies backward",
                "orbit_left": "camera orbits around subject to the left",
                "orbit_right": "camera orbits around subject to the right"
            }
            enhanced_prompt += f", {camera_instructions.get(camera_motion, '')}"
        
        # Add motion intensity
        intensity_instructions = {
            "low": ", subtle gentle movements, minimal action",
            "medium": ", balanced motion and action",
            "high": ", dynamic high-energy action, fast-paced movement"
        }
        enhanced_prompt += intensity_instructions.get(motion_intensity, "")
        
        # Add style preset
        if style_preset != "auto":
            style_instructions = {
                "cinematic": ", cinematic movie-like style, film quality",
                "photorealistic": ", photorealistic ultra-detailed style",
                "anime": ", anime art style, animated aesthetic",
                "cartoon": ", cartoon style animation",
                "game_render": ", game engine rendering style, real-time graphics quality"
            }
            enhanced_prompt += style_instructions.get(style_preset, "")
        
        print(f"🎬 [WORKFLOW] Google Veo 3 Fast video generation")
        print(f"📹 Mode: {'Image-to-Video (I2V)' if input_type == 'image' else 'Text-to-Video (T2V)'}")
        if image_url:
            print(f"🖼️  Image: {image_url[:80]}...")
        print(f"📝 Prompt: {enhanced_prompt[:150]}...")
        print(f"🎯 Purpose: {video_purpose}")
        print(f"⏱️  Duration: {duration}s, Resolution: {resolution}, Aspect: {aspect_ratio}")
        print(f"🎥 Camera: {camera_motion}, Motion: {motion_intensity}, Style: {style_preset}")
        print(f"🔊 Audio: {'Yes' if generate_audio else 'No'}, Loop: {'Yes' if loop_video else 'No'}")
        print(f"💰 Cost: ~$0.12")
        
        if not REPLICATE_API_KEY:
            raise ValueError("REPLICATE_API_KEY not found in environment variables")
        
        import replicate
        client = replicate.Client(api_token=REPLICATE_API_KEY)
        
        try:
            # Build input parameters
            input_params = {
                "prompt": enhanced_prompt,
                "duration": duration,
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
            }
            
            # Only add generate_audio if it's True (Veo 3 Fast might not accept false)
            if generate_audio:
                input_params["generate_audio"] = True
            
            # Add image for I2V mode
            if input_type == "image" and image_url:
                # Validate image URL - should not be base64 at this point
                if image_url.startswith("data:"):
                    raise ValueError(f"Image URL is still base64 data URI. This should have been converted to S3 URL. Image starts with: {image_url[:100]}")
                
                # Validate image URL format
                if not (image_url.startswith("http://") or image_url.startswith("https://")):
                    raise ValueError(f"Invalid image URL format. Must be http:// or https://. Got: {image_url[:100]}")
                
                # Check for common URL issues
                if " " in image_url:
                    raise ValueError(f"Image URL contains spaces. URL: {image_url[:100]}")
                
                # Try to verify the URL is accessible
                try:
                    print(f"🔍 Verifying image URL is accessible...")
                    test_response = requests.head(image_url, timeout=10, allow_redirects=True)
                    print(f"✅ Image URL accessible: Status {test_response.status_code}")
                    if test_response.status_code >= 400:
                        print(f"⚠️  Warning: Image URL returned status {test_response.status_code}")
                except Exception as e:
                    print(f"⚠️  Warning: Could not verify image URL accessibility: {e}")
                
                input_params["image"] = image_url
                print(f"🖼️  Using image URL: {image_url[:100]}...")
            
            # Add optional parameters
            if seed:
                input_params["seed"] = int(seed)
            
            # Note: Veo 3 may not support all these parameters directly
            # The prompt enhancement above handles camera_motion, style, etc.
            
            print(f"🚀 Calling Replicate Veo 3 Fast model...")
            print(f"📤 Input parameters: duration={duration}s, resolution={resolution}, aspect={aspect_ratio}")
            print(f"📤 Full input params keys: {list(input_params.keys())}")
            
            # Debug: Print all input params (truncate long values)
            print(f"📤 DEBUG - Full input parameters:")
            for key, value in input_params.items():
                if isinstance(value, str) and len(value) > 100:
                    print(f"   {key}: {value[:100]}... (length: {len(value)})")
                else:
                    print(f"   {key}: {value}")
            
            try:
                output = replicate.run(
                    "google/veo-3-fast",
                    input=input_params
                )
            except replicate.exceptions.ReplicateError as e:
                print(f"❌ Replicate API error: {e}")
                print(f"📋 Error type: {type(e).__name__}")
                if hasattr(e, 'detail'):
                    print(f"📋 Error detail: {e.detail}")
                if hasattr(e, 'status'):
                    print(f"📋 Status code: {e.status}")
                raise Exception(f"Replicate API rejected the input: {str(e)}")
            except Exception as e:
                print(f"❌ Unexpected error calling Replicate: {e}")
                print(f"📋 Error type: {type(e).__name__}")
                raise
            
            # Output is a FileOutput object with .url() method
            video_url = None
            if hasattr(output, 'url'):
                video_url = output.url() if callable(output.url) else output.url
            elif isinstance(output, str):
                video_url = output
            elif isinstance(output, list) and len(output) > 0:
                first_output = output[0]
                if hasattr(first_output, 'url'):
                    video_url = first_output.url() if callable(first_output.url) else first_output.url
                elif isinstance(first_output, str):
                    video_url = first_output
            
            if not video_url:
                raise ValueError("No video URL returned from Replicate")
            
            print(f"✅ Replicate returned video URL: {video_url[:100]}...")
            
            # Upload to S3 for persistent storage
            last_frame_url = None
            try:
                print(f"📥 Downloading video from Replicate...")
                video_response = requests.get(video_url, timeout=300)
                video_response.raise_for_status()
                
                video_content = video_response.content
                
                s3 = boto3.client('s3', region_name=AWS_REGION)
                video_filename = f"animations/veo3_fast_{uuid.uuid4()}.mp4"
                
                print(f"☁️  Uploading video to S3...")
                s3.put_object(
                    Bucket=S3_BUCKET,
                    Key=video_filename,
                    Body=video_content,
                    ContentType='video/mp4'
                )
                
                s3_url = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': S3_BUCKET, 'Key': video_filename},
                    ExpiresIn=86400  # 24 hours
                )
                
                print(f"✅ Video uploaded to S3: {video_filename}")
                video_url = s3_url  # Use S3 URL as primary
                
                # Extract last frame from video
                try:
                    print(f"🎞️  Extracting last frame from video...")
                    import cv2
                    import numpy as np
                    
                    # Write video to temporary file
                    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as temp_video:
                        temp_video.write(video_content)
                        temp_video_path = temp_video.name
                    
                    print(f"📁 Temp video saved to: {temp_video_path}")
                    
                    try:
                        # Open video with OpenCV
                        cap = cv2.VideoCapture(temp_video_path)
                        
                        if not cap.isOpened():
                            raise Exception("Failed to open video with OpenCV")
                        
                        # Get total number of frames
                        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                        fps = cap.get(cv2.CAP_PROP_FPS)
                        
                        print(f"📊 Video info: {total_frames} frames, {fps} FPS")
                        
                        if total_frames > 0:
                            # Set position to last frame
                            cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 1)
                            ret, frame = cap.read()
                            
                            if ret and frame is not None:
                                print(f"✅ Last frame captured: {frame.shape}")
                                
                                # Convert BGR to RGB
                                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                                
                                # Convert to PIL Image
                                from PIL import Image
                                pil_image = Image.fromarray(frame_rgb)
                                
                                # Save to bytes
                                img_byte_arr = BytesIO()
                                pil_image.save(img_byte_arr, format='PNG')
                                img_byte_arr = img_byte_arr.getvalue()
                                
                                print(f"📦 Frame size: {len(img_byte_arr)} bytes")
                                
                                # Upload last frame to S3
                                frame_filename = f"veo3_frames/last_frame_{uuid.uuid4()}.png"
                                s3.put_object(
                                    Bucket=S3_BUCKET,
                                    Key=frame_filename,
                                    Body=img_byte_arr,
                                    ContentType='image/png'
                                )
                                
                                # Use presigned URL with 7 days expiry
                                last_frame_url = s3.generate_presigned_url(
                                    'get_object',
                                    Params={'Bucket': S3_BUCKET, 'Key': frame_filename},
                                    ExpiresIn=604800  # 7 days
                                )
                                
                                print(f"✅ Last frame extracted and uploaded: {frame_filename}")
                                print(f"🖼️  Frame URL: {last_frame_url[:80]}...")
                            else:
                                print(f"⚠️  Failed to read last frame (ret={ret}, frame={'None' if frame is None else 'exists'})")
                        else:
                            print(f"⚠️  No frames found in video (total_frames={total_frames})")
                        
                        cap.release()
                    
                    finally:
                        # Clean up temp video file
                        try:
                            os.unlink(temp_video_path)
                            print(f"🗑️  Cleaned up temp file")
                        except Exception as cleanup_error:
                            print(f"⚠️  Failed to cleanup temp file: {cleanup_error}")
                
                except ImportError as import_error:
                    print(f"❌ OpenCV not available: {import_error}")
                    print(f"💡 Install opencv-python to enable last frame extraction")
                    print(f"⚠️  Auto-chaining will NOT work without last frame extraction!")
                except Exception as e:
                    print(f"❌ Failed to extract last frame: {e}")
                    import traceback
                    print(f"📜 Traceback: {traceback.format_exc()}")
                    print(f"⚠️  Auto-chaining will NOT work without last frame extraction!")
                
            except Exception as e:
                print(f"⚠️  Failed to upload to S3: {e}")
                print(f"📎 Using Replicate URL instead")
            
            # Get chain metadata from node config
            chain_data = node.config.get("chainData", {})
            segment_index = chain_data.get("segmentIndex", 0)
            accumulated_duration = chain_data.get("accumulatedDuration", 0)
            
            print(f"✅ Veo 3 Fast video generation completed!")
            print(f"🎥 Video: {video_url[:80]}...")
            if last_frame_url:
                print(f"🖼️  Last frame available as image_url for next nodes")
            else:
                print(f"⚠️  No last frame available!")
                if enable_auto_chain:
                    print(f"❌ WARNING: Auto-chain is enabled but last frame extraction failed!")
                    print(f"❌ Continuation nodes will FAIL without a last frame image!")
            
            if enable_auto_chain:
                print(f"🔗 Auto-chain enabled: Segment {segment_index + 1}, Target: {target_duration}s")
            print(f"💰 Cost: ~$0.12")
            
            # For auto-chain mode, validate that we have a last frame
            if enable_auto_chain and not last_frame_url:
                raise Exception(
                    "Auto-chaining enabled but last frame extraction failed. "
                    "Cannot create continuation nodes without a last frame. "
                    "Please ensure OpenCV is installed and video can be processed."
                )
            
            # For auto-chain mode, ensure input image is S3 URL, not base64
            output_input_image = None
            if input_type == "image":
                if enable_auto_chain:
                    # Only include S3/HTTP URLs, not base64
                    if image_url and not image_url.startswith("data:"):
                        output_input_image = image_url
                else:
                    # Normal mode, include any image URL
                    output_input_image = image_url
            
            result = {
                "job_id": str(uuid.uuid4()),
                "video_url": video_url,
                "video_s3_url": video_url if 's3' in video_url else None,
                "replicate_url": output.url if hasattr(output, 'url') else str(output),
                "image_url": last_frame_url,  # Last frame for next nodes
                "last_frame_url": last_frame_url,
                "s3_url": last_frame_url,  # Alias for compatibility
                "image": output_input_image,
                "input_image": output_input_image,
                "prompt": enhanced_prompt,
                "original_prompt": prompt,
                "input_type": input_type,
                "video_purpose": video_purpose,
                "duration": duration,
                "resolution": resolution,
                "aspect_ratio": aspect_ratio,
                "settings": {
                    "generate_audio": generate_audio,
                    "camera_motion": camera_motion,
                    "motion_intensity": motion_intensity,
                    "style_preset": style_preset,
                    "loop_video": loop_video,
                    "seed": seed
                },
                "status": "completed",
                "model": "google/veo-3-fast",
                "cost": 0.12
            }
            
            # Add chain metadata if auto-chaining is enabled
            if enable_auto_chain:
                result["chain_enabled"] = True
                result["target_duration"] = target_duration
                result["segment_duration"] = duration
                result["segment_index"] = segment_index
                result["accumulated_duration"] = accumulated_duration + duration
                result["needs_continuation"] = (accumulated_duration + duration) < int(target_duration)
                
                if result["needs_continuation"]:
                    print(f"🔗 Continuation needed: {accumulated_duration + duration}s / {target_duration}s")
                else:
                    print(f"✅ Target duration reached: {accumulated_duration + duration}s / {target_duration}s")
            
            return result
            
        except Exception as e:
            print(f"❌ Veo 3 Fast failed: {e}")
            raise Exception(f"Veo 3 Fast video generation failed: {str(e)}")
    
    async def execute_sketch_board(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute sketch_board node - outputs a sketch/drawing image that can be used as reference
        
        Supports:
        - Base64 encoded images (data URI or raw base64) - uploads to S3
        - Image URLs - returns as-is
        - Local file paths - uploads to S3
        - Sketch data from previous nodes
        """
        
        print(f"✏️  [WORKFLOW] Sketch board node starting...")
        print(f"📋 Node config keys: {list(node.config.keys())}")
        print(f"📦 Inputs: {list(inputs.keys()) if inputs else 'None'}")
        
        # Debug: Print full config
        for key, value in node.config.items():
            if isinstance(value, str) and len(value) > 100:
                print(f"   Config[{key}]: {value[:100]}... (length: {len(value)})")
            else:
                print(f"   Config[{key}]: {value}")
        
        # Get sketch image from config or previous node
        sketch_url = node.config.get("sketch_url") or node.config.get("image_url")
        sketch_path = node.config.get("sketch_path") or node.config.get("image_path")
        sketch_base64 = node.config.get("sketch_data") or node.config.get("image_upload") or node.config.get("sketch_base64")
        
        # IMPORTANT: Check for 'image' field which is commonly used in canvas/sketch nodes
        if not sketch_base64 and not sketch_url and not sketch_path:
            # Try common field names
            for field_name in ["image", "canvas_data", "drawing", "sketch", "canvas_image"]:
                if field_name in node.config:
                    sketch_base64 = node.config[field_name]
                    print(f"✅ Found sketch in config field: {field_name}")
                    break
        
        print(f"🔍 After config check:")
        print(f"   sketch_url: {sketch_url[:80] if sketch_url else 'None'}")
        print(f"   sketch_path: {sketch_path}")
        print(f"   sketch_base64: {'Found (' + str(len(sketch_base64)) + ' chars)' if sketch_base64 else 'None'}")
        
        # Try to extract sketch from previous node
        if not sketch_url and not sketch_path and not sketch_base64 and inputs:
            print(f"🔍 Checking inputs from previous nodes...")
            for node_id, input_data in inputs.items():
                print(f"   Node: {node_id}, Type: {type(input_data)}")
                if isinstance(input_data, dict):
                    print(f"   Keys: {list(input_data.keys())}")
                    # Check for various sketch/image fields
                    for field in ["sketch_url", "drawing_url", "image_url", "s3_url", "sketch_base64", "image_base64", "image_upload", "image", "canvas_data"]:
                        if field in input_data and input_data[field]:
                            if field.endswith("_base64") or field in ["image_upload", "image", "canvas_data"]:
                                sketch_base64 = input_data[field]
                            elif field.endswith("_path"):
                                sketch_path = input_data[field]
                            else:
                                sketch_url = input_data[field]
                            print(f"✅ Found sketch from node '{node_id}' in field '{field}'")
                            break
                    if sketch_url or sketch_path or sketch_base64:
                        break
        
        # Process base64 sketch if provided
        if sketch_base64:
            try:
                print(f"🎨 [WORKFLOW] Processing base64 sketch data")
                
                # Handle data URI format
                if sketch_base64.startswith("data:") and "base64," in sketch_base64:
                    header, b64data = sketch_base64.split("base64,", 1)
                    # Try to infer extension from header
                    try:
                        mime = header.split(";")[0].split(":")[1]
                        ext = mime.split("/")[-1]
                        if ext == "jpeg":
                            ext = "jpg"
                    except Exception:
                        ext = "png"
                else:
                    # Raw base64
                    b64data = sketch_base64
                    ext = "png"
                
                # Decode base64
                image_bytes = base64.b64decode(b64data)
                
                # Write to temporary file
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", prefix="sketch_")
                tmp.write(image_bytes)
                tmp.flush()
                tmp.close()
                sketch_path = tmp.name
                
                print(f"✅ Decoded base64 sketch to: {sketch_path}")
                
            except Exception as e:
                raise ValueError(f"Failed to decode base64 sketch data: {e}")
        
        # Validate we have a sketch
        if not sketch_url and not sketch_path:
            raise ValueError(
                "No sketch provided for sketch_board node. "
                "Please provide sketch_url, sketch_path, or sketch_base64 in config, "
                "or connect a node that outputs an image."
            )
        
        print(f"✏️  [WORKFLOW] Sketch board node processing")
        
        # Upload to S3 if we have a local file or base64 data
        s3_url = None
        if sketch_path and not sketch_url:
            try:
                print(f"📤 [WORKFLOW] Uploading sketch to S3...")
                
                # Use S3 directly via boto3
                if S3_BUCKET and AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
                    import boto3
                    
                    s3 = boto3.client(
                        's3',
                        aws_access_key_id=AWS_ACCESS_KEY_ID,
                        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                        region_name=AWS_REGION
                    )
                    
                    # Generate unique filename
                    file_ext = os.path.splitext(sketch_path)[1] or '.png'
                    filename = f"sketches/sketch_{uuid.uuid4()}{file_ext}"
                    
                    # Upload to S3
                    with open(sketch_path, 'rb') as f:
                        s3.upload_fileobj(f, S3_BUCKET, filename)
                    
                    # Generate presigned URL (24 hour expiry for sketches)
                    s3_url = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': filename},
                        ExpiresIn=86400
                    )
                    
                    print(f"✅ Sketch uploaded to S3: {s3_url[:80]}...")
                else:
                    print(f"⚠️  S3 not configured, using local path")
                        
            except Exception as e:
                print(f"⚠️  S3 upload failed: {e}, using local path")
        
        # Build result
        final_url = s3_url or sketch_url or sketch_path
        
        result = {
            "sketch_url": final_url,
            "sketch_path": sketch_path,
            "image_url": final_url,  # Alias for downstream nodes
            "s3_url": s3_url or (sketch_url if sketch_url and "s3" in sketch_url else None),
            "drawing_url": final_url,  # Alternative alias
            "node_type": "sketch_board",
            "status": "completed",
            "message": "Sketch uploaded and ready for use as reference" if s3_url else "Sketch ready for use as reference"
        }
        
        print(f"✅ Sketch board ready: {result['image_url'][:80] if result['image_url'] else 'local file'}...")
        
        return result
    
    async def execute_animate_wan_animate(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute animate_wan_animate node - WAN 2.2 Animate-Animation via Replicate
        
        Transfer motion from reference video to character image.
        Pricing: ~$0.003 per second (333 seconds for $1)
        """
        
        # Get video from previous node or config
        video_url = node.config.get("video")
        if not video_url and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "video_url" in input_data:
                        video_url = input_data["video_url"]
                        break
                    elif "video_s3_url" in input_data:
                        video_url = input_data["video_s3_url"]
                        break
                    elif "animation_url" in input_data:
                        video_url = input_data["animation_url"]
                        break
        
        if not video_url:
            raise ValueError("No video provided for WAN 2.2 Animate-Animation. This node requires a reference video showing the desired movement.")
        
        # Get character image from previous node or config
        character_image = node.config.get("character_image")
        if not character_image and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        character_image = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        character_image = input_data["s3_url"]
                        break
                    elif "gemini_generated_url" in input_data:
                        character_image = input_data["gemini_generated_url"]
                        break
        
        if not character_image:
            raise ValueError("No character image provided for WAN 2.2 Animate-Animation")
        
        # Get configuration
        resolution = node.config.get("resolution", "720")
        go_fast = node.config.get("go_fast", "true").lower() == "true"
        refert_num = int(node.config.get("refert_num", "1"))
        frames_per_second = int(node.config.get("frames_per_second", "24"))
        merge_audio = node.config.get("merge_audio", "true").lower() == "true"
        
        print(f"🎬 [WORKFLOW] WAN 2.2 Animate-Animation")
        print(f"🎥 Reference video: {video_url[:80]}...")
        print(f"🖼️  Character image: {character_image[:80]}...")
        print(f"🎯 Resolution: {resolution}")
        print(f"💰 Cost: ~$0.003/second")
        
        # Call the endpoint
        response = requests.post(
            f"{self.base_url}/animate/wan-animate",
            json={
                "video": video_url,
                "character_image": character_image,
                "resolution": resolution,
                "go_fast": go_fast,
                "refert_num": refert_num,
                "frames_per_second": frames_per_second,
                "merge_audio": merge_audio
            },
            timeout=30  # Initial response is immediate
        )
        
        response.raise_for_status()
        result = response.json()
        
        job_id = result.get("job_id")
        
        print(f"⏳ Job started: {job_id}")
        print(f"💰 Cost per second: {result.get('cost_per_second')}")
        
        # Poll for completion
        max_retries = 120  # 10 minutes max (120 * 5 seconds)
        for i in range(max_retries):
            time.sleep(5)
            
            status_response = requests.get(f"{self.base_url}/status/{job_id}")
            status_response.raise_for_status()
            status_data = status_response.json()
            
            if status_data.get("status") == "completed":
                result_data = status_data.get("result", {})
                video_url_output = result_data.get("video_url") or result_data.get("video_s3_url")
                
                print(f"✅ WAN 2.2 Animate-Animation completed!")
                print(f"⏱️  Processing time: {result_data.get('processing_time_seconds', 0):.1f}s")
                print(f"💰 Actual cost: ${result_data.get('actual_cost', 0):.4f}")
                
                return {
                    "job_id": job_id,
                    "video_url": video_url_output,
                    "video_s3_url": result_data.get("video_s3_url"),
                    "local_path": result_data.get("local_path"),
                    "reference_video": video_url,
                    "character_image": character_image,
                    "resolution": resolution,
                    "processing_time_seconds": result_data.get("processing_time_seconds"),
                    "cost_per_second": result_data.get("cost_per_second"),
                    "actual_cost": result_data.get("actual_cost"),
                    "settings": result_data.get("settings", {}),
                    "status": "completed"
                }
            elif status_data.get("status") == "failed":
                error_msg = status_data.get("message", "Unknown error")
                raise Exception(f"WAN 2.2 Animate-Animation failed: {error_msg}")
            
            # Log progress
            if i % 6 == 0:  # Every 30 seconds
                progress = status_data.get("progress", 0)
                message = status_data.get("message", "")
                print(f"⏳ Progress: {progress}% - {message}")
        
        raise Exception("WAN 2.2 Animate-Animation timed out after 10 minutes")
    
    async def execute_generate_ideogram_turbo(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute generate_ideogram_turbo node - Ideogram V3 Turbo text-on-image generation
        
        Add professional text rendering on images with perfect typography.
        Pricing: $0.03 per image
        """
        
        # Get prompt from previous node or config
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "enhanced_prompt" in input_data:
                        prompt = input_data["enhanced_prompt"]
                        break
                    elif "prompt" in input_data:
                        prompt = input_data["prompt"]
                        break
        
        if not prompt:
            raise ValueError("No prompt provided for Ideogram V3 Turbo generation")
        
        # Get optional input image from previous node
        input_image = node.config.get("image") or node.config.get("image_url")
        if not input_image and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        input_image = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        input_image = input_data["s3_url"]
                        break
                    elif "gemini_generated_url" in input_data:
                        input_image = input_data["gemini_generated_url"]
                        break
                    elif "image_s3_url" in input_data:
                        input_image = input_data["image_s3_url"]
                        break
        
        # Get optional mask image from config or previous node
        mask_image = node.config.get("mask_url") or node.config.get("mask")
        if not mask_image and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "mask_url" in input_data:
                        mask_image = input_data["mask_url"]
                        break
                    elif "mask" in input_data:
                        mask_image = input_data["mask"]
                        break
        
        # Handle base64 mask - upload to S3 and get presigned URL
        if mask_image and isinstance(mask_image, str):
            if mask_image.startswith("data:") or (not mask_image.startswith("http")):
                print(f"🔄 [WORKFLOW] Detected base64 mask, uploading to S3...")
                try:
                    # Decode base64
                    if mask_image.startswith("data:"):
                        # Data URI format
                        header, b64data = mask_image.split(",", 1)
                    else:
                        # Raw base64
                        b64data = mask_image
                    
                    mask_bytes = base64.b64decode(b64data)

                    # If we have an input image, resize mask to match its dimensions
                    if input_image:
                        print(f"📏 [WORKFLOW] Resizing mask to match input image dimensions...")
                        
                        # Download input image
                        img_response = requests.get(input_image, timeout=30)
                        img_response.raise_for_status()
                        input_img = Image.open(BytesIO(img_response.content))
                        input_width, input_height = input_img.size
                        
                        print(f"   Input image size: {input_width}x{input_height}")
                        
                        # Load and resize mask
                        mask_img = Image.open(BytesIO(mask_bytes))
                        original_mask_size = mask_img.size
                        print(f"   Original mask size: {original_mask_size[0]}x{original_mask_size[1]}")
                        
                        # Resize mask to match input image exactly
                        if mask_img.size != (input_width, input_height):
                            mask_img = mask_img.resize((input_width, input_height), Image.Resampling.LANCZOS)
                            print(f"   ✅ Resized mask to: {input_width}x{input_height}")
                            
                            # Convert back to bytes
                            mask_buffer = BytesIO()
                            mask_img.save(mask_buffer, format='PNG')
                            mask_bytes = mask_buffer.getvalue()
                        else:
                            print(f"   ✅ Mask already matches input dimensions")
                    
                    # Upload to S3
                    s3 = boto3.client('s3', region_name=AWS_REGION)
                    mask_filename = f"masks/mask_{uuid.uuid4()}.png"
                    
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=mask_filename,
                        Body=mask_bytes,
                        ContentType='image/png'
                    )
                    
                    # Generate presigned URL (expires in 1 hour)
                    mask_image = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': mask_filename},
                        ExpiresIn=3600
                    )
                    
                    print(f"✅ [WORKFLOW] Mask uploaded to S3: {mask_filename}")
                    print(f"🔗 Presigned URL: {mask_image[:80]}...")
                    
                except Exception as e:
                    print(f"❌ Failed to process base64 mask: {e}")
                    raise ValueError(f"Failed to upload mask to S3: {str(e)}")
        
        # Get configuration
        aspect_ratio = node.config.get("aspect_ratio", "3:2")
        resolution = node.config.get("resolution", "1024x1024")
        magic_prompt_option = node.config.get("magic_prompt_option", "Auto")
        style_type = node.config.get("style_type")
        
        print(f"🎨 [WORKFLOW] Ideogram V3 Turbo generation")
        print(f"📝 Prompt: {prompt[:100]}...")
        
        # Determine generation mode based on available inputs
        if input_image and mask_image:
            print(f"🖼️  Input image: {input_image[:80]}...")
            print(f"🎭 Mask image: {mask_image[:80]}...")
            print(f"✅ INPAINTING MODE: Both image and mask provided")
            generation_mode = "inpainting"
        elif input_image and not mask_image:
            print(f"🖼️  Input image detected: {input_image[:80]}...")
            print(f"⚠️  No mask provided - text-only generation will be used")
            print(f"� Tip: Add a mask image to enable inpainting mode")
            generation_mode = "text-to-image"
        elif mask_image and not input_image:
            print(f"🎭 Mask detected: {mask_image[:80]}...")
            print(f"⚠️  No input image provided - mask will be ignored")
            print(f"💡 Tip: Mask requires an input image for inpainting")
            generation_mode = "text-to-image"
        else:
            print(f"✅ Text-only generation mode")
            generation_mode = "text-to-image"
        
        print(f"📐 Aspect ratio: {aspect_ratio}")
        print(f"💰 Cost: $0.03")
        
        # Build request payload
        payload = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
            "magic_prompt_option": magic_prompt_option
        }
        
        # Add image and mask if both are provided (inpainting mode)
        if input_image and mask_image:
            payload["image"] = input_image
            payload["mask"] = mask_image
            print(f"🎨 Payload includes: image + mask (inpainting)")
        else:
            print(f"🎨 Payload mode: text-only generation")
        
        # Only add optional fields if they have values
        if style_type:
            payload["style_type"] = style_type
        
        # Call the endpoint
        response = requests.post(
            f"{self.base_url}/generate/ideogram-turbo",
            json=payload,
            timeout=30  # Initial response is immediate
        )
        
        response.raise_for_status()
        result = response.json()
        
        job_id = result.get("job_id")
        
        print(f"⏳ Job started: {job_id}")
        print(f"💰 Cost: $0.03 per image")
        
        # Poll for completion
        max_retries = 30  # 2.5 minutes max (30 * 5 seconds)
        for i in range(max_retries):
            time.sleep(5)
            
            status_response = requests.get(f"{self.base_url}/status/{job_id}")
            status_response.raise_for_status()
            status_data = status_response.json()
            
            if status_data.get("status") == "completed":
                result_data = status_data.get("result", {})
                image_url = result_data.get("image_url") or result_data.get("image_s3_url")
                
                print(f"✅ Ideogram V3 Turbo generation completed!")
                print(f"💰 Cost: $0.03")
                
                return {
                    "job_id": job_id,
                    "image_url": image_url,
                    "image_s3_url": result_data.get("image_s3_url"),
                    "s3_url": result_data.get("image_s3_url"),  # Alias for chaining
                    "local_path": result_data.get("local_path"),
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "input_image": input_image,
                    "mask_image": mask_image,
                    "generation_mode": generation_mode,
                    "processing_time_seconds": result_data.get("processing_time_seconds"),
                    "cost_per_image": 0.03,
                    "actual_cost": 0.03,
                    "settings": result_data.get("settings", {}),
                    "status": "completed"
                }
            elif status_data.get("status") == "failed":
                error_msg = status_data.get("message", "Unknown error")
                raise Exception(f"Ideogram V3 Turbo generation failed: {error_msg}")
            
            # Log progress
            if i % 3 == 0:  # Every 15 seconds
                progress = status_data.get("progress", 0)
                print(f"⏳ Progress: {progress}%")
        
        raise Exception("Ideogram V3 Turbo generation timed out after 2.5 minutes")
    
    async def execute_optimize_3d(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute optimize_3d node - optimizes image for 3D model generation"""
        
        # Get image from previous node or config
        image_url = node.config.get("image_url")
        if not image_url and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    # Check all possible image URL fields
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
                    elif "gemini_generated_url" in input_data:
                        image_url = input_data["gemini_generated_url"]
                        break
                    elif "optimized_3d_image_s3_url" in input_data:
                        image_url = input_data["optimized_3d_image_s3_url"]
                        break
        
        if not image_url:
            raise ValueError("No image provided for 3D optimization")
        
        # Get optional custom prompt from config
        prompt = node.config.get("prompt") or node.config.get("optimization_prompt")
        
        print(f"🎯 [WORKFLOW] Optimizing image for 3D generation with /optimize/3d endpoint")
        print(f"🖼️  Image URL: {image_url[:100]}...")
        if prompt:
            print(f"📝 Custom prompt: {prompt}")
        
        # Prepare request data
        data = {
            "image_url": image_url,
            "prompt": prompt
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/optimize/3d",
                data=data,
                timeout=300
            )
            
            # Try to get error details from response
            if response.status_code != 200:
                try:
                    error_detail = response.json().get("detail", response.text)
                    print(f"❌ Optimize API error ({response.status_code}): {error_detail}")
                    raise ValueError(f"Optimize API error: {error_detail}")
                except:
                    print(f"❌ Optimize API error ({response.status_code}): {response.text[:500]}")
                    response.raise_for_status()
            
            result = response.json()
            
            print(f"✅ 3D optimization completed")
            print(f"📦 Job ID: {result.get('job_id')}")
            
            # Extract URLs and return structured data
            return {
                "job_id": result.get("job_id"),
                "original_image_s3_url": result.get("original_image_s3_url"),
                "optimized_3d_image_s3_url": result.get("optimized_3d_image_s3_url"),
                "image_url": result.get("optimized_3d_image_s3_url"),  # Alias for consistency
                "s3_url": result.get("optimized_3d_image_s3_url"),  # Alias for next node
                "objects_analysis": result.get("objects_analysis"),
                "has_base64_data": result.get("has_base64_data", False),
                "status": "completed"
            }
            
        except requests.exceptions.RequestException as e:
            error_msg = str(e)
            
            if "Connection" in error_msg or "connect" in error_msg.lower():
                raise ValueError(
                    f"Cannot connect to animation API at {self.base_url}. "
                    f"Please ensure the animation API is running."
                )
            elif "500" in error_msg:
                raise ValueError(
                    f"3D optimization failed on server. Check if mapapi is configured correctly."
                )
            else:
                raise ValueError(f"3D optimization request failed: {error_msg}")
    
    async def execute_time_travel_scene(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute time_travel_scene node - transforms scene to different time period"""
        
        # Get image from previous node or config
        image_url = node.config.get("image_url") or node.config.get("image_file")
        
        # Handle base64 image - upload to S3 and get presigned URL
        if image_url and isinstance(image_url, str):
            if image_url.startswith("data:") or (not image_url.startswith("http")):
                print(f"🔄 [WORKFLOW] Detected base64 image, uploading to S3...")
                try:
                    # Decode base64
                    if image_url.startswith("data:"):
                        # Data URI format
                        header, b64data = image_url.split(",", 1)
                        try:
                            mime = header.split(";")[0].split(":")[1]
                            ext = mime.split("/")[-1]
                            if ext == "jpeg":
                                ext = "jpg"
                        except Exception:
                            ext = "png"
                    else:
                        # Raw base64
                        b64data = image_url
                        ext = "png"
                
                    image_bytes = base64.b64decode(b64data)
                    
                    # Upload to S3
                    s3 = boto3.client('s3', region_name=AWS_REGION)
                    image_filename = f"time_travel/input_{uuid.uuid4()}.{ext}"
                    
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=image_filename,
                        Body=image_bytes,
                        ContentType=f'image/{ext}'
                    )
                    
                    # Generate presigned URL (expires in 1 hour)
                    image_url = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': image_filename},
                        ExpiresIn=3600
                    )
                    
                    print(f"✅ [WORKFLOW] Image uploaded to S3: {image_filename}")
                    print(f"🔗 Presigned URL: {image_url[:80]}...")
                
                except Exception as e:
                    print(f"❌ Failed to process base64 image: {e}")
                    raise ValueError(f"Failed to upload image to S3: {str(e)}")
        
        if not image_url and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    # Check all possible image URL fields
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "image_file" in input_data:
                        image_url = input_data["image_file"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
                    elif "output_url" in input_data:
                        image_url = input_data["output_url"]
                        break
                    elif "url" in input_data:
                        image_url = input_data["url"]
                        break
                    elif "transformed_image_s3_url" in input_data:
                        image_url = input_data["transformed_image_s3_url"]
                        break
                    elif "optimized_3d_image_s3_url" in input_data:
                        image_url = input_data["optimized_3d_image_s3_url"]
                        break
        
        if not image_url:
            raise ValueError("No image provided for time travel transformation")
        
        print(f"⏳ [WORKFLOW] Time Travel Scene transformation")
        print(f"🖼️  Input image: {image_url[:80]}...")
        
        # Get time travel parameters from config
        target_date = node.config.get("target_date")
        target_year = node.config.get("target_year")
        time_of_day = node.config.get("time_of_day") or node.config.get("time_of_day_mood", "current")
        season = node.config.get("season", "current")
        historical_accuracy = node.config.get("historical_accuracy", "moderate")
        era_style = node.config.get("era_style")  # e.g., "renaissance", "medieval", "victorian"
        transformation_strength = node.config.get("transformation_strength", "moderate")  # "subtle", "moderate", "strong"
        
        # Build time travel context for the prompt
        time_context_parts = []
        
        # Add era/style information
        if era_style:
            print(f"🎭 Era style: {era_style}")
            time_context_parts.append(f"{era_style} era style")
        elif target_date or target_year:
            # Extract year from target_date or use target_year
            year = target_year
            if target_date and not year:
                try:
                    year = target_date.split("-")[0]
                except:
                    pass
            
            if year:
                print(f"📅 Target year: {year}")
                # Map year to historical period
                year_int = int(year)
                if year_int < 500:
                    time_context_parts.append("ancient times")
                elif year_int < 1000:
                    time_context_parts.append("early medieval period")
                elif year_int < 1500:
                    time_context_parts.append("medieval period")
                elif year_int < 1700:
                    time_context_parts.append("renaissance period")
                elif year_int < 1800:
                    time_context_parts.append("baroque period")
                elif year_int < 1900:
                    time_context_parts.append("victorian era")
                elif year_int < 1950:
                    time_context_parts.append("early 20th century")
                elif year_int < 2000:
                    time_context_parts.append("mid-to-late 20th century")
                else:
                    time_context_parts.append("modern era")
        
        # Add time of day
        if time_of_day and time_of_day != "current" and time_of_day != "auto":
            print(f"🌅 Time of day: {time_of_day}")
            time_context_parts.append(f"{time_of_day} lighting")
        
        # Add season
        if season and season != "current":
            print(f"🍂 Season: {season}")
            time_context_parts.append(f"{season} season")
        
        # Add transformation strength descriptor
        strength_descriptors = {
            "subtle": "subtle historical influence",
            "moderate": "clear historical atmosphere",
            "strong": "dramatic historical transformation"
        }
        if transformation_strength in strength_descriptors:
            time_context_parts.append(strength_descriptors[transformation_strength])
        
        # Build the time travel prompt addition
        time_travel_prompt = ", ".join(time_context_parts) if time_context_parts else "historical scene"
        
        print(f"🎨 Time context: {time_travel_prompt}")
        
        # Check for Character Time Travel parameters (Age/Height)
        target_age = node.config.get("target_age")
        target_height = node.config.get("target_height")
        
        # Also check inputs for dynamic age/height from previous nodes
        original_prompt = ""
        if inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if not target_age and "target_age" in input_data:
                        target_age = str(input_data["target_age"])
                    if not target_height and "target_height" in input_data:
                        target_height = str(input_data["target_height"])
                    
                    # Look for prompt from previous nodes to maintain character consistency
                    if not original_prompt:
                         if "prompt" in input_data:
                             original_prompt = input_data["prompt"]
                         elif "enhanced_prompt" in input_data:
                             original_prompt = input_data["enhanced_prompt"]
                         elif "fused_prompt" in input_data:
                             original_prompt = input_data["fused_prompt"]
        
        # Define defaults for character generation if time travel involves aging
        prompt = original_prompt or node.config.get("prompt", "")
        age = node.config.get("age", "")
        height = node.config.get("height", "")
        enhancer_type = node.config.get("enhancer_type", "enhance_prompt_ollama")
        generator_type = node.config.get("generator_type", "generate_nano_banana_pro")
        remove_bg = str(node.config.get("remove_background", "false")).lower() == "true"

        if target_age:
            print(f"⏳ [WORKFLOW] Character Time Travel detected: Target Age {target_age}")
            # Override age with target_age for the prompt
            age = target_age
            if target_height:
                height = target_height
                print(f"📏 Target Height: {target_height}")
        
        # 1. Construct Base Prompt with Time Travel Context
        character_details = []
        if age:
            character_details.append(f"Age: {age}")
        if height:
            character_details.append(f"Height: {height}")
        
        # Build prompt with time travel context
        if not prompt:
            # If no base prompt, create one describing the scene
            if image_url:
                prompt = "Transform this image into a scene with"
            else:
                prompt = "A scene with"
        
        # Integrate time travel context into the prompt
        if time_travel_prompt:
            base_prompt = f"{prompt} {time_travel_prompt}"
        else:
            base_prompt = prompt
        
        if character_details:
            base_prompt = f"{base_prompt}. Character details: {', '.join(character_details)}."
        
        # Add historical accuracy guidance
        if historical_accuracy == "high":
            base_prompt = f"{base_prompt} Historically accurate, authentic period details."
        elif historical_accuracy == "low":
            base_prompt = f"{base_prompt} Artistic interpretation, creative freedom."
            
        print(f"📝 Base Prompt: {base_prompt}")
        
        # Skip enhancement if base prompt is too short
        if len(base_prompt.strip()) < 10:
            raise ValueError(f"Base prompt is too short or empty: '{base_prompt}'. Please provide more context.")
        
        # 2. Enhance Prompt
        enhanced_prompt = base_prompt
        if enhancer_type and enhancer_type != "none":
            print(f"✨ Enhancing prompt using {enhancer_type}...")
            enhancer_node = WorkflowNode(
                id=f"{node.id}_enhancer",
                type=enhancer_type,
                position={"x": 0, "y": 0},
                config={
                    "prompt": base_prompt,
                    # Pass through other potential config options if needed
                }
            )
            
            enhancer_inputs = {} 
            
            enhancer_result = None
            if enhancer_type == "enhance_prompt":
                enhancer_result = await self.execute_enhance_prompt(enhancer_node, enhancer_inputs)
            elif enhancer_type == "enhance_prompt_ollama":
                enhancer_result = await self.execute_enhance_prompt_ollama(enhancer_node, enhancer_inputs)
            elif enhancer_type == "enhance_prompt_gnokit":
                enhancer_result = await self.execute_enhance_prompt_gnokit(enhancer_node, enhancer_inputs)
            elif enhancer_type == "enhance_prompt_fusion":
                enhancer_result = await self.execute_enhance_prompt_fusion(enhancer_node, enhancer_inputs)
            
            if enhancer_result:
                enhanced_prompt = enhancer_result.get("enhanced_prompt", base_prompt)
                print(f"✨ Enhanced Prompt: {enhanced_prompt[:100]}...")
            else:
                print(f"⚠️ Enhancer {enhancer_type} returned no result, using base prompt.")

        # 3. Generate Image
        print(f"🎨 Generating image using {generator_type}...")
        
        # Prepare generator configuration
        generator_config = {
            "prompt": enhanced_prompt,
            "negative_prompt": node.config.get("negative_prompt", ""),
            "num_outputs": 1,
            "output_format": node.config.get("output_format", "png"),
            "image_url": image_url  # Pass input image
        }
        
        # Add generator-specific defaults and options
        if generator_type in ["generate_nano_banana", "generate_nano_banana_pro"]:
            # Nano Banana Pro specific options
            # Valid aspect ratios: "match_input_image", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"
            aspect_ratio = node.config.get("aspect_ratio", "4:3")
            if aspect_ratio == "1024x1024": # Handle common default from other nodes
                aspect_ratio = "1:1"
                
            generator_config.update({
                "aspect_ratio": aspect_ratio,
                "resolution": node.config.get("resolution", "2K"),
                "safety_filter_level": node.config.get("safety_filter_level", "block_only_high"),
                "image_url": image_url
            })
        else:
            # Default for other generators (like Ideogram, Gemini)
            generator_config["aspect_ratio"] = node.config.get("aspect_ratio", "1024x1024")

        generator_node = WorkflowNode(
            id=f"{node.id}_generator",
            type=generator_type,
            position={"x": 0, "y": 0},
            config=generator_config
        )
        
        generator_inputs = {} 
        
        generator_result = None
        if generator_type in ["generate_nano_banana", "generate_nano_banana_pro"]:
             generator_result = await self.execute_generate_nano_banana(generator_node, generator_inputs)
        elif generator_type == "generate_image": # Gemini or others
             generator_result = await self.execute_generate_image(generator_node, generator_inputs)
        elif generator_type == "generate_ideogram_turbo":
             generator_result = await self.execute_generate_ideogram_turbo(generator_node, generator_inputs)
             
        if not generator_result:
            raise ValueError(f"Generator {generator_type} failed to produce a result")
            
        image_url = generator_result.get("image_url") or generator_result.get("output_url") or generator_result.get("url")
        if not image_url:
             raise ValueError(f"Generator {generator_type} did not return an image URL")
             
        print(f"🖼️ Generated Image URL: {image_url[:100]}...")

        # 4. Remove Background
        final_image_url = image_url
        if remove_bg:
            print(f"✂️ Removing background...")
            rembg_node = WorkflowNode(
                id=f"{node.id}_rembg",
                type="remove_background",
                position={"x": 0, "y": 0},
                config={
                    "image_url": image_url
                }
            )
            
            rembg_result = await self.execute_remove_background(rembg_node, {})
            
            if rembg_result and "image_url" in rembg_result:
                final_image_url = rembg_result["image_url"]
                print(f"✅ Background removed: {final_image_url[:100]}...")
            else:
                print(f"⚠️ Background removal failed or returned no URL, using original image.")

        # 5. Return Result
        return {
            "image_url": final_image_url,
            "original_image_url": image_url,
            "prompt": base_prompt,
            "enhanced_prompt": enhanced_prompt,
            "age": age,
            "height": height,
            "status": "completed",
            "node_type": "create_character"
        }
    
    async def execute_upload_image(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute upload_image node - handles image uploads and makes them available to workflow
        Always uploads to S3 and returns S3 URL as primary output"""
        
        # Get image from config or previous node
        image_url = node.config.get("image_url")
        image_path = node.config.get("image_path")
        image_upload = node.config.get("image_upload") or node.config.get("image_base64") or node.config.get("image_file")
        
        # Try to extract from inputs if not in config
        if not image_url and not image_path and not image_upload and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                    elif "image_upload" in input_data:
                        image_upload = input_data["image_upload"]
                    elif "image_base64" in input_data:
                        image_upload = input_data["image_base64"]
        
        # Process base64 upload if provided
        if image_upload and isinstance(image_upload, str):
            try:
                print(f"🎨 [WORKFLOW] Processing base64 upload data")
                
                # Handle data URI format
                if image_upload.startswith("data:") and "base64," in image_upload:
                    header, b64data = image_upload.split("base64,", 1)
                    try:
                        mime = header.split(";")[0].split(":")[1]
                        ext = mime.split("/")[-1]
                        if ext == "jpeg":
                            ext = "jpg"
                    except Exception:
                        ext = "png"
                else:
                    b64data = image_upload
                    ext = "png"
                
                # Decode base64
                image_bytes = base64.b64decode(b64data)
                
                # Write to temporary file
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", prefix="upload_")
                tmp.write(image_bytes)
                tmp.flush()
                tmp.close()
                image_path = tmp.name
                
                print(f"✅ Decoded base64 upload to: {image_path}")
                
            except Exception as e:
                raise ValueError(f"Failed to decode base64 image upload: {e}")
        
        # Download remote URL to local file if needed
        if image_url and not image_path:
            try:
                print(f"📥 [WORKFLOW] Downloading image from URL...")
                import httpx
                
                async with httpx.AsyncClient() as client:
                    response = await client.get(image_url, timeout=30.0)
                    response.raise_for_status()
                    
                    # Determine extension from content-type or URL
                    content_type = response.headers.get("content-type", "")
                    if "png" in content_type:
                        ext = ".png"
                    elif "webp" in content_type:
                        ext = ".webp"
                    elif "gif" in content_type:
                        ext = ".gif"
                    else:
                        ext = ".jpg"
                    
                    # Write to temporary file
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="download_")
                    tmp.write(response.content)
                    tmp.flush()
                    tmp.close()
                    image_path = tmp.name
                    
                    print(f"✅ Downloaded image to: {image_path}")
                    
            except Exception as e:
                print(f"⚠️  Download failed: {e}, will use original URL")
        
        # Upload to S3 (always do this to ensure we have a stable S3 URL)
        s3_url = None
        s3_direct_url = None
        if image_path:
            try:
                print(f"📤 [WORKFLOW] Uploading image to S3...")
                
                # Upload to S3 directly using boto3
                s3 = boto3.client('s3', region_name=AWS_REGION)
                file_ext = os.path.splitext(image_path)[1] or ".jpg"
                if file_ext == ".": file_ext = ".jpg"
                
                s3_filename = f"uploads/upload_{uuid.uuid4()}{file_ext}"
                
                # Determine content type
                content_type = "image/jpeg"
                if file_ext.lower() == ".png":
                    content_type = "image/png"
                elif file_ext.lower() == ".webp":
                    content_type = "image/webp"
                elif file_ext.lower() == ".gif":
                    content_type = "image/gif"
                
                with open(image_path, "rb") as f:
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=s3_filename,
                        Body=f,
                        ContentType=content_type
                    )
                
                # Generate presigned URL (expires in 24 hours)
                s3_url = s3.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': S3_BUCKET, 'Key': s3_filename},
                    ExpiresIn=86400
                )
                
                # Also generate direct S3 URL (no expiration, but requires bucket to be public)
                s3_direct_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_filename}"
                
                print(f"✅ Image uploaded to S3: {s3_filename}")
                print(f"🔗 Presigned URL: {s3_url[:80]}...")
                        
            except Exception as e:
                print(f"❌ S3 upload failed: {e}")
                raise ValueError(f"Failed to upload image to S3: {e}")
        
        # Prefer S3 URL as primary output
        final_url = s3_url or image_url or image_path
        
        if not final_url:
             raise ValueError("No image provided for upload_image node")

        return {
            "image_url": final_url,
            "s3_url": s3_url,
            "s3_direct_url": s3_direct_url,
            "url": final_url,
            "output_url": final_url,
            "local_path": image_path,
            "original_url": image_url,
            "status": "completed",
            "node_type": "upload_image",
            "message": "Image uploaded to S3 successfully" if s3_url else "Image processed"
        }

    async def execute_generate_cover_music(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute generate_cover_music node - Generate cover music using Suno API
        
        This node:
        1. Takes a music task ID or audio URL from previous nodes
        2. Calls Suno API to generate a cover version
        3. Polls for completion
        4. Downloads the generated audio
        5. Uploads to S3 and returns audio URL
        
        Args:
            node: Workflow node with config containing:
                - task_id (str, optional): Suno task ID for the original music
                - audio_url (str, optional): Direct audio URL to cover
                - callback_url (str, optional): Callback URL for async notification
                - style (str, optional): Music style/genre for the cover
                - wait_for_completion (bool): Whether to poll for completion (default: True)
            inputs: Previous node outputs containing task_id or audio_url
        
        Returns:
            Dict with audio URL, S3 URL, and task information
        """
        import httpx
        
        print(f"\n🎼 Generating cover music with Suno API...")
        
        if not SUNO_API_KEY:
            raise ValueError("SUNO_API_KEY not found in environment variables")
        
        # Get task ID from config or previous node
        task_id = node.config.get("task_id") or node.config.get("taskId")
        
        if not task_id and inputs:
            print(f"🔍 Looking for task_id in inputs...")
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    task_id = input_data.get("task_id") or input_data.get("taskId") or input_data.get("suno_task_id")
                    if task_id:
                        print(f"✅ Found task_id: {task_id}")
                        break
        
        if not task_id:
            raise ValueError("No task_id provided for Suno cover generation. Please provide task_id in config or from a previous node.")
        
        # Get optional parameters
        callback_url = node.config.get("callback_url") or node.config.get("callBackUrl")
        wait_for_completion = node.config.get("wait_for_completion", True)
        
        # Suno API requires callBackUrl - use from env, config, or placeholder
        if not callback_url:
            # Check environment variable first
            callback_url = os.getenv("SUNO_CALLBACK_URL")
            if callback_url:
                print(f"✓ Using callback URL from environment: {callback_url}")
            else:
                callback_url = "https://api.example.com/callback"
                print(f"⚠️  No callback URL provided, using placeholder")
        
        print(f"🎵 Task ID: {task_id}")
        print(f"🔔 Callback URL: {callback_url}")
        
        # Call Suno API to generate cover
        try:
            url = "https://api.sunoapi.org/api/v1/suno/cover/generate"
            
            payload = {
                "taskId": task_id,
                "callBackUrl": callback_url  # Always include callback URL
            }
            
            headers = {
                "Authorization": f"Bearer {SUNO_API_KEY}",
                "Content-Type": "application/json"
            }
            
            print(f"📤 Calling Suno API...")
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            
            print(f"📥 Suno API response: {result}")
            
            if result.get("code") != 200:
                raise ValueError(f"Suno API error: {result.get('msg', 'Unknown error')}")
            
            cover_task_id = result.get("data", {}).get("taskId")
            
            if not cover_task_id:
                raise ValueError("No task ID returned from Suno API")
            
            print(f"✅ Cover generation started: {cover_task_id}")
            
            # If not waiting for completion, return task ID immediately
            if not wait_for_completion:
                return {
                    "task_id": cover_task_id,
                    "cover_task_id": cover_task_id,
                    "original_task_id": task_id,
                    "status": "processing",
                    "message": "Cover generation started. Use the task ID to check status.",
                    "node_type": "generate_cover_music"
                }
            
            # Poll for completion
            print(f"⏳ Polling for cover generation completion...")
            status_url = f"https://api.sunoapi.org/api/v1/generate/record-info?taskId={cover_task_id}"
            print(f"🔗 Status URL: {status_url}")
            
            max_retries = 60  # 5 minutes max (60 * 5 seconds)
            audio_url = None
            
            for i in range(max_retries):
                time.sleep(5)
                
                try:
                    status_response = requests.get(
                        status_url,
                        headers={"Authorization": f"Bearer {SUNO_API_KEY}"},
                        timeout=30
                    )
                    status_response.raise_for_status()
                    status_data = status_response.json()
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code == 404:
                        print(f"⚠️  Task not found yet, will retry... ({i+1}/{max_retries})")
                        continue
                    raise
                
                if status_data.get("code") != 200:
                    print(f"⚠️  Status check warning: {status_data.get('msg')}")
                    continue
                
                # Response structure: data.response.sunoData contains the audio clips
                data_wrapper = status_data.get("data")
                if not data_wrapper or not isinstance(data_wrapper, dict):
                    print(f"⚠️  Invalid or missing data in response ({i+1}/{max_retries})")
                    print(f"📥 Response structure: {status_data}")
                    continue
                
                response_data = data_wrapper.get("response", {})
                status = data_wrapper.get("status")
                suno_clips = response_data.get("sunoData", []) if isinstance(response_data, dict) else []
                
                print(f"📊 Status check {i+1}/{max_retries}: {status}")
                
                if status == "SUCCESS" and suno_clips:
                    first_clip = suno_clips[0]
                    audio_url = first_clip.get("audioUrl") or first_clip.get("sourceAudioUrl")
                    print(f"✅ Cover generation completed!")
                    print(f"🎵 Audio URL: {audio_url}")
                    break
                elif status == "FAILED" or status == "ERROR":
                    error_msg = data_wrapper.get("errorMessage") or "Unknown error"
                    raise ValueError(f"Cover generation failed: {error_msg}")
                elif not status or status == "PROCESSING" or status == "PENDING":
                    if i % 6 == 0:  # Log every 30 seconds
                        print(f"⏳ Still processing... ({i*5}s elapsed)")
                    continue
            
            if not audio_url:
                raise TimeoutError("Cover generation timed out after 5 minutes")
            
            # Download and upload to S3
            print(f"⬇️  Downloading cover audio...")
            
            async with httpx.AsyncClient(timeout=300.0) as http_client:
                audio_response = await http_client.get(audio_url, follow_redirects=True)
                audio_response.raise_for_status()
                audio_data = audio_response.content
            
            # Save to temp file
            audio_filename = f"suno_cover_{uuid.uuid4()}.mp3"
            temp_audio_path = os.path.join("/tmp", audio_filename)
            
            with open(temp_audio_path, 'wb') as f:
                f.write(audio_data)
            
            print(f"💾 Saved to: {temp_audio_path}")
            
            # Upload to S3
            s3_audio_key = f"suno_covers/{node.id}/{audio_filename}"
            s3 = boto3.client('s3', region_name=AWS_REGION)
            
            s3.upload_file(
                temp_audio_path,
                S3_BUCKET,
                s3_audio_key,
                ExtraArgs={'ContentType': 'audio/mpeg'}
            )
            
            # Use region-agnostic S3 URL
            s3_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{s3_audio_key}"
            
            print(f"☁️  Uploaded to S3: {s3_audio_key}")
            
            # Clean up temp file
            try:
                os.remove(temp_audio_path)
            except:
                pass
            
            # Build result
            result = {
                "audio_url": s3_url,  # PRIMARY OUTPUT: S3 URL
                "s3_url": s3_url,
                "output_url": s3_url,
                "url": s3_url,
                "original_audio_url": audio_url,  # Original Suno URL
                "task_id": cover_task_id,
                "cover_task_id": cover_task_id,
                "original_task_id": task_id,
                "local_path": temp_audio_path,
                "node_type": "generate_cover_music",
                "status": "completed",
                "message": "Cover music generated and uploaded to S3 successfully",
                "outputs": {
                    "audio": s3_url,
                    "audio_s3": s3_url,
                    "task_id": cover_task_id
                }
            }
            
            print(f"✅ Cover music generation completed")
            
            return result
            
        except requests.exceptions.RequestException as e:
            error_msg = f"Suno API request failed: {str(e)}"
            print(f"❌ {error_msg}")
            raise Exception(error_msg)
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ Cover generation error: {e}")
            print(f"📋 Full error trace:\n{error_details}")
            raise Exception(f"Cover music generation failed: {str(e)}")

    async def execute_generate_music_suno(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute generate_music_suno node - Generate original music using Suno API
        
        This node:
        1. Takes music generation parameters (prompt, style, etc.)
        2. Calls Suno API to generate original music
        3. Polls for completion
        4. Downloads the generated audio
        5. Uploads to S3 and returns audio URL
        
        Args:
            node: Workflow node with config containing:
                - prompt (str): Description of the desired music
                - style (str, optional): Music style/genre
                - title (str, optional): Track title
                - instrumental (bool, optional): Generate instrumental only (default: False)
                - custom_mode (bool, optional): Use custom mode (default: True)
                - model (str, optional): Suno model version (default: "V4_5ALL")
                - vocal_gender (str, optional): "m" or "f" for male/female vocals
                - style_weight (float, optional): Style adherence weight 0-1 (default: 0.65)
                - weirdness_constraint (float, optional): Creativity level 0-1 (default: 0.65)
                - audio_weight (float, optional): Audio quality weight 0-1 (default: 0.65)
                - negative_tags (str, optional): Styles to avoid
                - persona_id (str, optional): Persona ID for consistency
                - callback_url (str, optional): Callback URL for async notification
                - wait_for_completion (bool): Whether to poll for completion (default: True)
            inputs: Previous node outputs containing prompt or other parameters
        
        Returns:
            Dict with audio URL, S3 URL, and task information
        """
        import httpx
        
        print(f"\n🎼 Generating music with Suno API...")
        
        if not SUNO_API_KEY:
            raise ValueError("SUNO_API_KEY not found in environment variables")
        
        # Get prompt from config or previous node
        prompt = node.config.get("prompt", "")
        
        if not prompt and inputs:
            print(f"🔍 Looking for prompt in inputs...")
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    prompt = input_data.get("prompt") or input_data.get("enhanced_prompt") or input_data.get("sound_prompt")
                    if prompt:
                        print(f"✅ Found prompt: {prompt[:100]}...")
                        break
        
        if not prompt:
            raise ValueError("No prompt provided for Suno music generation. Please provide a prompt describing the desired music.")
        
        # Get configuration parameters
        custom_mode = node.config.get("custom_mode", node.config.get("customMode", True))
        instrumental = node.config.get("instrumental", False)
        model = node.config.get("model", "V4_5ALL")
        style = node.config.get("style", "")
        title = node.config.get("title", "")
        vocal_gender = node.config.get("vocal_gender", node.config.get("vocalGender", ""))
        style_weight = float(node.config.get("style_weight", node.config.get("styleWeight", 0.65)))
        weirdness_constraint = float(node.config.get("weirdness_constraint", node.config.get("weirdnessConstraint", 0.65)))
        audio_weight = float(node.config.get("audio_weight", node.config.get("audioWeight", 0.65)))
        negative_tags = node.config.get("negative_tags", node.config.get("negativeTags", ""))
        persona_id = node.config.get("persona_id", node.config.get("personaId", ""))
        callback_url = node.config.get("callback_url", node.config.get("callBackUrl", ""))
        wait_for_completion = node.config.get("wait_for_completion", True)
        
        # Suno API requires callBackUrl - use from env, config, or placeholder
        if not callback_url:
            # Check environment variable first
            callback_url = os.getenv("SUNO_CALLBACK_URL")
            if callback_url:
                print(f"✓ Using callback URL from environment: {callback_url}")
            else:
                callback_url = "https://api.example.com/callback"
                print(f"⚠️  No callback URL provided, using placeholder")
        
        print(f"🎵 Prompt: {prompt[:100]}...")
        print(f"🎨 Style: {style or 'Auto'}")
        print(f"🎤 Instrumental: {instrumental}")
        print(f"🤖 Model: {model}")
        if title:
            print(f"📝 Title: {title}")
        print(f"🔔 Callback URL: {callback_url}")
        
        # Build payload
        payload = {
            "customMode": custom_mode,
            "instrumental": instrumental,
            "model": model,
            "prompt": prompt,
            "callBackUrl": callback_url  # Always include callback URL
        }
        
        # Add optional parameters
        if style:
            payload["style"] = style
        if title:
            payload["title"] = title
        if vocal_gender:
            payload["vocalGender"] = vocal_gender
        if negative_tags:
            payload["negativeTags"] = negative_tags
        if persona_id:
            payload["personaId"] = persona_id
        
        # Add weight parameters
        payload["styleWeight"] = style_weight
        payload["weirdnessConstraint"] = weirdness_constraint
        payload["audioWeight"] = audio_weight
        
        # Call Suno API to generate music
        try:
            url = "https://api.sunoapi.org/api/v1/generate"
            
            headers = {
                "Authorization": f"Bearer {SUNO_API_KEY}",
                "Content-Type": "application/json"
            }
            
            print(f"📤 Calling Suno API...")
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            
            print(f"📥 Suno API response: {result}")
            
            if result.get("code") != 200:
                raise ValueError(f"Suno API error: {result.get('msg', 'Unknown error')}")
            
            task_id = result.get("data", {}).get("taskId")
            
            if not task_id:
                raise ValueError("No task ID returned from Suno API")
            
            print(f"✅ Music generation started: {task_id}")
            
            # If not waiting for completion, return task ID immediately
            if not wait_for_completion:
                return {
                    "task_id": task_id,
                    "suno_task_id": task_id,
                    "status": "processing",
                    "prompt": prompt,
                    "style": style,
                    "title": title,
                    "message": "Music generation started. Use the task ID to check status.",
                    "node_type": "generate_music_suno"
                }
            
            # Poll for completion
            print(f"⏳ Polling for music generation completion...")
            print(f"📍 Task ID: {task_id}")
            status_url = f"https://api.sunoapi.org/api/v1/generate/record-info?taskId={task_id}"
            print(f"🔗 Will check status at: {status_url}")
            
            max_retries = 120  # 10 minutes max (120 * 5 seconds) - music generation can take longer
            audio_url = None
            consecutive_404s = 0
            
            for i in range(max_retries):
                time.sleep(5)
                
                try:
                    status_response = requests.get(
                        status_url,
                        headers={"Authorization": f"Bearer {SUNO_API_KEY}"},
                        timeout=30
                    )
                    status_response.raise_for_status()
                    status_data = status_response.json()
                    consecutive_404s = 0  # Reset counter on success
                    
                    print(f"📥 Response: {status_data}")
                    
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code == 404:
                        consecutive_404s += 1
                        print(f"⚠️  Task not found (404) - attempt {consecutive_404s} ({i+1}/{max_retries})")
                        
                        # If we get too many consecutive 404s, the task might not exist
                        if consecutive_404s > 20:  # ~100 seconds of 404s
                            print(f"❌ Task not found after {consecutive_404s} attempts. Task may have failed to create.")
                            return {
                                "task_id": task_id,
                                "suno_task_id": task_id,
                                "status": "failed",
                                "error": "Task not found in Suno API. The task may have failed to create or the task ID is invalid.",
                                "message": "Use task_id to manually check status at https://api.sunoapi.org",
                                "node_type": "generate_music_suno"
                            }
                        continue
                    else:
                        print(f"❌ HTTP Error {e.response.status_code}: {e.response.text}")
                        raise
                
                if status_data.get("code") != 200:
                    print(f"⚠️  Status check warning: {status_data.get('msg')}")
                    continue
                
                # Response structure: data.response.sunoData contains the audio clips
                data_wrapper = status_data.get("data")
                if not data_wrapper or not isinstance(data_wrapper, dict):
                    consecutive_404s += 1
                    print(f"⚠️  Invalid or missing data in response - attempt {consecutive_404s} ({i+1}/{max_retries})")
                    print(f"📥 Response structure: {status_data}")
                    if consecutive_404s > 20:
                        return {
                            "task_id": task_id,
                            "suno_task_id": task_id,
                            "status": "failed",
                            "error": "Invalid response structure from Suno API",
                            "node_type": "generate_music_suno"
                        }
                    continue
                
                consecutive_404s = 0  # Reset counter on valid response
                response_data = data_wrapper.get("response", {})
                status = data_wrapper.get("status")
                suno_clips = response_data.get("sunoData", []) if isinstance(response_data, dict) else []
                
                print(f"📊 Status check {i+1}/{max_retries}: {status}")
                print(f"🎵 Found {len(suno_clips)} audio clips")
                
                if status == "SUCCESS" and suno_clips:
                    # Get the first audio clip
                    first_clip = suno_clips[0]
                    audio_url = first_clip.get("audioUrl") or first_clip.get("sourceAudioUrl")
                    print(f"✅ Music generation completed!")
                    print(f"🎵 Audio URL: {audio_url}")
                    
                    # Store all clips data
                    task_data = {
                        "status": "completed",
                        "audio_url": audio_url,
                        "clips": suno_clips,
                        "task_id": task_id
                    }
                    break
                elif status == "FAILED" or status == "ERROR":
                    error_msg = data_wrapper.get("errorMessage") or "Unknown error"
                    raise ValueError(f"Music generation failed: {error_msg}")
                elif not status or status == "PROCESSING" or status == "PENDING":
                    if i % 6 == 0:  # Log every 30 seconds
                        print(f"⏳ Still processing... ({i*5}s elapsed)")
                    continue
                else:
                    consecutive_404s += 1
                    print(f"⚠️  Unexpected status: {status} - attempt {consecutive_404s}")
                    if consecutive_404s > 20:
                        return {
                            "task_id": task_id,
                            "suno_task_id": task_id,
                            "status": "failed",
                            "error": f"Unexpected status: {status}",
                            "node_type": "generate_music_suno"
                        }
                    continue
            
            if not audio_url:
                print(f"⏰ Timeout reached after {max_retries * 5} seconds")
                print(f"📋 Task ID for manual check: {task_id}")
                return {
                    "task_id": task_id,
                    "suno_task_id": task_id,
                    "status": "timeout",
                    "message": f"Music generation timed out after 10 minutes. Task ID: {task_id}. Check status manually at https://api.sunoapi.org",
                    "node_type": "generate_music_suno"
                }
            
            # Download and upload to S3
            print(f"⬇️  Downloading generated music...")
            
            async with httpx.AsyncClient(timeout=300.0) as http_client:
                audio_response = await http_client.get(audio_url, follow_redirects=True)
                audio_response.raise_for_status()
                audio_data = audio_response.content
            
            # Save to temp file
            audio_filename = f"suno_music_{uuid.uuid4()}.mp3"
            temp_audio_path = os.path.join("/tmp", audio_filename)
            
            with open(temp_audio_path, 'wb') as f:
                f.write(audio_data)
            
            print(f"💾 Saved to: {temp_audio_path}")
            
            # Upload to S3
            s3_audio_key = f"suno_music/{node.id}/{audio_filename}"
            s3 = boto3.client('s3', region_name=AWS_REGION)
            
            s3.upload_file(
                temp_audio_path,
                S3_BUCKET,
                s3_audio_key,
                ExtraArgs={'ContentType': 'audio/mpeg'}
            )
            
            # Use region-agnostic S3 URL
            s3_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{s3_audio_key}"
            
            print(f"☁️  Uploaded to S3: {s3_audio_key}")
            
            # Clean up temp file
            try:
                os.remove(temp_audio_path)
            except:
                pass
            
            # Build result with all clip information
            result = {
                "audio_url": s3_url,  # PRIMARY OUTPUT: S3 URL
                "s3_url": s3_url,
                "output_url": s3_url,
                "url": s3_url,
                "original_audio_url": audio_url,  # Original Suno URL
                "task_id": task_id,
                "suno_task_id": task_id,
                "local_path": temp_audio_path,
                "prompt": prompt,
                "style": style,
                "title": title,
                "instrumental": instrumental,
                "model": model,
                "node_type": "generate_music_suno",
                "status": "completed",
                "message": "Music generated and uploaded to S3 successfully",
                # Add all clips info if available
                "clips": task_data.get("clips", []) if 'task_data' in locals() else [],
                "clip_count": len(suno_clips) if 'suno_clips' in locals() else 1,
                "outputs": {
                    "audio": s3_url,
                    "audio_s3": s3_url,
                    "task_id": task_id
                }
            }
            
            print(f"✅ Music generation completed")
            
            return result
            
        except requests.exceptions.RequestException as e:
            error_msg = f"Suno API request failed: {str(e)}"
            print(f"❌ {error_msg}")
            raise Exception(error_msg)
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ Cover generation error: {e}")
            print(f"📋 Full error trace:\n{error_details}")
            raise Exception(f"Cover music generation failed: {str(e)}")


    async def execute_replace_music_section(self, node, inputs):
        """Replace a section of an existing Suno music track
        
        This node:
        1. Takes an existing Suno task/audio ID
        2. Replaces a specific time range with new content
        3. Downloads and uploads the modified audio to S3
        
        Config:
        - task_id: Original Suno task ID
        - audio_id: Specific audio clip ID to modify
        - prompt: Description of the replacement section
        - tags/style: Music style for the replacement
        - title: Title for the modified track
        - infill_start_s: Start time in seconds (e.g., 10.5)
        - infill_end_s: End time in seconds (e.g., 20.75)
        - negative_tags: Styles to avoid
        - callback_url: Optional webhook URL
        """
        import requests
        import httpx
        import boto3
        import uuid
        import os
        
        try:
            print(f"\n{'='*60}")
            print(f"✂️  REPLACE MUSIC SECTION")
            print(f"{'='*60}")
            
            # Get task ID and audio ID from config or previous node
            task_id = node.config.get("task_id") or node.config.get("taskId")
            # Get task ID and audio ID from config or previous node
            task_id = node.config.get("task_id") or node.config.get("taskId")
            audio_id = node.config.get("audio_id") or node.config.get("audioId")
            
            # If no audio_id but we have clips in config, get from first clip
            if not audio_id and "clips" in node.config:
                clips = node.config.get("clips", [])
                if clips and len(clips) > 0:
                    audio_id = clips[0].get("id") or clips[0].get("clip_id") or clips[0].get("audioId")
            
            print(f"🔍 Looking for task_id and audio_id...")
            print(f"   Config keys: {list(node.config.keys())}")
            print(f"   Found task_id: {task_id}")
            print(f"   Found audio_id: {audio_id}")
            if "clips" in node.config:
                print(f"   Clips available: {len(node.config.get('clips', []))}")
            
            # Try to get from previous node output if not in config
            if (not task_id or not audio_id) and inputs:
                for input_data in inputs.values():
                    if isinstance(input_data, dict):
                        if not task_id:
                            task_id = (input_data.get("task_id") or 
                                     input_data.get("suno_task_id") or 
                                     input_data.get("taskId"))
                        
                        if not audio_id:
                            audio_id = (input_data.get("audio_id") or 
                                      input_data.get("clip_id") or
                                      input_data.get("audioId"))
                            
                            # If we have clips array, get the first clip's ID
                            if not audio_id and "clips" in input_data:
                                clips = input_data.get("clips", [])
                                if clips and len(clips) > 0:
                                    audio_id = clips[0].get("id") or clips[0].get("clip_id")
                        
                        if task_id and audio_id:
                            break
            
            if not task_id:
                raise ValueError("No task_id provided. Need original Suno task ID.")
            
            if not audio_id:
                raise ValueError("No audio_id provided. Need specific audio clip ID to modify.")
            
            # Get replacement parameters
            prompt = node.config.get("prompt", "")
            if not prompt and inputs:
                for input_data in inputs.values():
                    if isinstance(input_data, dict) and "prompt" in input_data:
                        prompt = input_data["prompt"]
                        break
            
            if not prompt:
                raise ValueError("No prompt provided for replacement section")
            
            tags = node.config.get("tags") or node.config.get("style", "")
            title = node.config.get("title", "Modified Track")
            infill_start_s = float(node.config.get("infill_start_s", node.config.get("infillStartS", 0)))
            infill_end_s = float(node.config.get("infill_end_s", node.config.get("infillEndS", 10)))
            negative_tags = node.config.get("negative_tags", node.config.get("negativeTags", ""))
            
            # Get lyrics (required by API, can be empty string or actual lyrics)
            full_lyrics = node.config.get("full_lyrics") or node.config.get("fullLyrics") or ""
            if not full_lyrics:
                # Generate default lyrics based on prompt if not provided
                full_lyrics = f"[Verse]\n{prompt}\n\n[Chorus]\n{prompt}"
            
            # Get callback URL
            callback_url = node.config.get("callback_url", node.config.get("callBackUrl", ""))
            if not callback_url:
                callback_url = os.getenv("SUNO_CALLBACK_URL")
                if callback_url:
                    print(f"✓ Using callback URL from environment: {callback_url}")
                else:
                    callback_url = "https://api.example.com/callback"
                    print(f"⚠️  No callback URL provided, using placeholder")
            
            print(f"📍 Original Task ID: {task_id}")
            print(f"🎵 Audio ID to modify: {audio_id}")
            print(f"📝 Replacement prompt: {prompt}")
            print(f"🎨 Style/Tags: {tags}")
            print(f"📜 Lyrics: {full_lyrics[:50]}..." if len(full_lyrics) > 50 else f"📜 Lyrics: {full_lyrics}")
            print(f"✂️  Replace section: {infill_start_s}s - {infill_end_s}s ({infill_end_s - infill_start_s}s duration)")
            print(f"🔔 Callback URL: {callback_url}")
            
            # Prepare API payload
            payload = {
                "taskId": task_id,
                "audioId": audio_id,
                "prompt": prompt,
                "fullLyrics": full_lyrics,  # Required field
                "infillStartS": infill_start_s,
                "infillEndS": infill_end_s,
                "callBackUrl": callback_url
            }
            
            # Add optional fields
            if tags:
                payload["tags"] = tags
            if title:
                payload["title"] = title
            if negative_tags:
                payload["negativeTags"] = negative_tags
            
            print(f"\n🚀 Calling Suno API to replace section...")
            
            # Call Suno API
            response = requests.post(
                "https://api.sunoapi.org/api/v1/generate/replace-section",
                json=payload,
                headers={
                    "Authorization": f"Bearer {SUNO_API_KEY}",
                    "Content-Type": "application/json"
                },
                timeout=30
            )
            
            response.raise_for_status()
            response_data = response.json()
            
            print(f"📥 API Response: {response_data}")
            
            if response_data.get("code") != 200:
                raise ValueError(f"Suno API error: {response_data.get('msg')}")
            
            # Extract new task ID
            data = response_data.get("data", {})
            new_task_id = data.get("taskId") or data.get("task_id")
            
            if not new_task_id:
                raise ValueError("No task ID returned from Suno API")
            
            print(f"✅ Section replacement initiated")
            print(f"🆔 New Task ID: {new_task_id}")
            
            # Return immediately if not waiting for completion
            wait_for_completion = node.config.get("wait_for_completion", True)
            if not wait_for_completion:
                return {
                    "task_id": new_task_id,
                    "suno_task_id": new_task_id,
                    "original_task_id": task_id,
                    "audio_id": audio_id,
                    "status": "processing",
                    "infill_start_s": infill_start_s,
                    "infill_end_s": infill_end_s,
                    "message": "Section replacement started. Use load_suno_from_taskid to retrieve results.",
                    "node_type": "replace_music_section"
                }
            
            # Poll for completion
            print(f"\n⏳ Polling for section replacement completion...")
            status_url = f"https://api.sunoapi.org/api/v1/generate/record-info?taskId={new_task_id}"
            print(f"🔗 Status URL: {status_url}")
            
            max_retries = 120  # 10 minutes
            audio_url = None
            consecutive_404s = 0
            
            for i in range(max_retries):
                time.sleep(5)
                
                try:
                    status_response = requests.get(
                        status_url,
                        headers={"Authorization": f"Bearer {SUNO_API_KEY}"},
                        timeout=30
                    )
                    status_response.raise_for_status()
                    status_data = status_response.json()
                except requests.exceptions.HTTPError as e:
                    if e.response.status_code == 404:
                        consecutive_404s += 1
                        print(f"⚠️  Task not found (404) - attempt {consecutive_404s} ({i+1}/{max_retries})")
                        if consecutive_404s > 20:
                            print(f"❌ Task not found after {consecutive_404s} attempts.")
                            return {
                                "task_id": new_task_id,
                                "suno_task_id": new_task_id,
                                "original_task_id": task_id,
                                "status": "failed",
                                "error": "Task not found in Suno API.",
                                "node_type": "replace_music_section"
                            }
                        continue
                    else:
                        print(f"❌ HTTP Error {e.response.status_code}: {e.response.text}")
                        raise
                
                if status_data.get("code") != 200:
                    print(f"⚠️  Status check warning: {status_data.get('msg')}")
                    continue
                
                # Parse response
                data_wrapper = status_data.get("data")
                if not data_wrapper or not isinstance(data_wrapper, dict):
                    consecutive_404s += 1
                    print(f"⚠️  Invalid or missing data in response ({i+1}/{max_retries})")
                    if consecutive_404s > 20:
                        return {
                            "task_id": new_task_id,
                            "status": "failed",
                            "error": "Invalid response from Suno API",
                            "node_type": "replace_music_section"
                        }
                    continue
                
                consecutive_404s = 0
                response_data_inner = data_wrapper.get("response", {})
                status = data_wrapper.get("status")
                suno_clips = response_data_inner.get("sunoData", []) if isinstance(response_data_inner, dict) else []
                
                print(f"📊 Status check {i+1}/{max_retries}: {status}")
                
                if status == "SUCCESS" and suno_clips:
                    first_clip = suno_clips[0]
                    audio_url = first_clip.get("audioUrl") or first_clip.get("sourceAudioUrl")
                    print(f"✅ Section replacement completed!")
                    print(f"🎵 Audio URL: {audio_url}")
                    break
                elif status == "FAILED" or status == "ERROR":
                    error_msg = data_wrapper.get("errorMessage") or "Unknown error"
                    raise ValueError(f"Section replacement failed: {error_msg}")
                elif not status or status == "PROCESSING" or status == "PENDING":
                    if i % 6 == 0:
                        print(f"⏳ Still processing... ({i*5}s elapsed)")
                    continue
            
            if not audio_url:
                print(f"⏰ Timeout reached after {max_retries * 5} seconds")
                return {
                    "task_id": new_task_id,
                    "suno_task_id": new_task_id,
                    "original_task_id": task_id,
                    "status": "timeout",
                    "message": f"Section replacement timed out. Task ID: {new_task_id}",
                    "node_type": "replace_music_section"
                }
            
            # Download and upload to S3
            print(f"\n⬇️  Downloading modified audio...")
            
            async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as http_client:
                audio_response = await http_client.get(audio_url)
                audio_response.raise_for_status()
                audio_data = audio_response.content
            
            print(f"Downloaded {len(audio_data)} bytes")
            
            # Save to temp file
            audio_filename = f"suno_replaced_{new_task_id}_{uuid.uuid4()}.mp3"
            temp_audio_path = os.path.join("/tmp", audio_filename)
            
            with open(temp_audio_path, 'wb') as f:
                f.write(audio_data)
            
            # Upload to S3
            s3_audio_key = f"suno_replaced/{new_task_id}/{audio_filename}"
            s3 = boto3.client('s3', region_name=AWS_REGION)
            
            s3.upload_file(
                temp_audio_path,
                S3_BUCKET,
                s3_audio_key,
                ExtraArgs={'ContentType': 'audio/mpeg'}
            )
            
            s3_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{s3_audio_key}"
            
            print(f"☁️  Uploaded to S3: {s3_url}")
            
            # Clean up
            try:
                os.remove(temp_audio_path)
            except:
                pass
            
            # Build result
            result = {
                "audio_url": s3_url,  # PRIMARY OUTPUT
                "s3_url": s3_url,
                "output_url": s3_url,
                "url": s3_url,
                "task_id": new_task_id,
                "suno_task_id": new_task_id,
                "original_task_id": task_id,
                "original_audio_id": audio_id,
                "infill_start_s": infill_start_s,
                "infill_end_s": infill_end_s,
                "replaced_duration": infill_end_s - infill_start_s,
                "prompt": prompt,
                "tags": tags,
                "title": title,
                "clips": suno_clips,
                "clip_count": len(suno_clips),
                "node_type": "replace_music_section",
                "status": "completed",
                "message": f"Successfully replaced section {infill_start_s}s-{infill_end_s}s"
            }
            
            print(f"\n✅ Section replacement completed successfully!")
            print(f"🎵 Modified audio: {s3_url}")
            
            return result
            
        except requests.exceptions.RequestException as e:
            error_msg = f"Suno API request failed: {str(e)}"
            print(f"❌ {error_msg}")
            raise Exception(error_msg)
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ Section replacement error: {e}")
            print(f"📋 Full error trace:\n{error_details}")
            raise Exception(f"Music section replacement failed: {str(e)}")


    async def execute_load_suno_from_taskid(self, node, inputs):
        """Load completed Suno music from task ID
        
        This node:
        1. Takes a Suno task ID from config or previous node
        2. Fetches the completed music data from Suno API
        3. Downloads the audio file(s)
        4. Uploads to S3
        5. Returns audio URLs and metadata
        
        Config:
        - task_id: Suno task ID to load
        - clip_index: Which clip to use (0 or 1, default: 0) - Suno generates 2 clips per task
        - download_all: If true, downloads all clips (default: false)
        """
        import requests
        import httpx
        import boto3
        import uuid
        import os
        
        try:
            print(f"\n{'='*60}")
            print(f"🎵 LOAD SUNO FROM TASK ID")
            print(f"{'='*60}")
            
            # Get task ID from config or previous node
            task_id = node.config.get("task_id") or node.config.get("taskId")
            
            # Try to get from previous node output
            if not task_id and inputs:
                for input_data in inputs.values():
                    if isinstance(input_data, dict):
                        task_id = (input_data.get("task_id") or 
                                 input_data.get("suno_task_id") or 
                                 input_data.get("taskId"))
                        if task_id:
                            break
            
            if not task_id:
                raise ValueError("No task_id provided in config or previous node output")
            
            clip_index = int(node.config.get("clip_index", 0))
            download_all = node.config.get("download_all", False)
            
            print(f"📍 Task ID: {task_id}")
            print(f"🎯 Clip Index: {clip_index}")
            print(f"📥 Download All: {download_all}")
            
            # Fetch task data from Suno API
            print(f"\n⏳ Fetching task data from Suno API...")
            status_url = f"https://api.sunoapi.org/api/v1/generate/record-info?taskId={task_id}"
            
            response = requests.get(
                status_url,
                headers={"Authorization": f"Bearer {SUNO_API_KEY}"},
                timeout=30
            )
            response.raise_for_status()
            status_data = response.json()
            
            if status_data.get("code") != 200:
                raise ValueError(f"Suno API error: {status_data.get('msg')}")
            
            # Parse response
            data_wrapper = status_data.get("data", {})
            response_data = data_wrapper.get("response", {})
            status = data_wrapper.get("status")
            suno_clips = response_data.get("sunoData", [])
            
            print(f"📊 Status: {status}")
            print(f"🎵 Found {len(suno_clips)} audio clips")
            
            if status != "SUCCESS":
                return {
                    "task_id": task_id,
                    "status": status.lower(),
                    "message": f"Task status is {status}, not SUCCESS",
                    "error": f"Task not completed. Current status: {status}",
                    "node_type": "load_suno_from_taskid"
                }
            
            if not suno_clips:
                raise ValueError("No audio clips found in task response")
            
            # Determine which clips to download
            clips_to_download = []
            if download_all:
                clips_to_download = suno_clips
                print(f"📦 Downloading all {len(suno_clips)} clips")
            else:
                if clip_index >= len(suno_clips):
                    clip_index = 0
                    print(f"⚠️  Requested clip index out of range, using clip 0")
                clips_to_download = [suno_clips[clip_index]]
                print(f"📦 Downloading clip {clip_index}")
            
            # Download and upload clips
            uploaded_clips = []
            
            for idx, clip in enumerate(clips_to_download):
                clip_id = clip.get("id")
                audio_url = clip.get("audioUrl") or clip.get("sourceAudioUrl")
                title = clip.get("title", "untitled")
                duration = clip.get("duration", 0)
                
                print(f"\n⬇️  Downloading clip {idx+1}/{len(clips_to_download)}...")
                print(f"   ID: {clip_id}")
                print(f"   Title: {title}")
                print(f"   Duration: {duration}s")
                print(f"   URL: {audio_url[:60]}...")
                
                # Download audio
                async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as http_client:
                    audio_response = await http_client.get(audio_url)
                    audio_response.raise_for_status()
                    audio_data = audio_response.content
                
                print(f"   Downloaded {len(audio_data)} bytes")
                
                # Save to temp file
                audio_filename = f"suno_{task_id}_{clip_id}.mp3"
                temp_audio_path = os.path.join("/tmp", audio_filename)
                
                with open(temp_audio_path, 'wb') as f:
                    f.write(audio_data)
                
                # Upload to S3
                s3_audio_key = f"suno_loaded/{task_id}/{audio_filename}"
                s3 = boto3.client('s3', region_name=AWS_REGION)
                
                s3.upload_file(
                    temp_audio_path,
                    S3_BUCKET,
                    s3_audio_key,
                    ExtraArgs={'ContentType': 'audio/mpeg'}
                )
                
                # Use region-agnostic S3 URL
                s3_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{s3_audio_key}"
                
                print(f"   ☁️  Uploaded to S3: {s3_url}")
                
                # Clean up temp file
                try:
                    os.remove(temp_audio_path)
                except:
                    pass
                
                uploaded_clips.append({
                    "clip_id": clip_id,
                    "audio_url": s3_url,
                    "s3_url": s3_url,
                    "original_audio_url": audio_url,
                    "title": title,
                    "duration": duration,
                    "prompt": clip.get("prompt", ""),
                    "tags": clip.get("tags", ""),
                    "model": clip.get("modelName", ""),
                    "image_url": clip.get("imageUrl", ""),
                    "create_time": clip.get("createTime", 0)
                })
            
            # Build result
            primary_clip = uploaded_clips[0]
            
            result = {
                "audio_url": primary_clip["audio_url"],  # PRIMARY OUTPUT
                "s3_url": primary_clip["s3_url"],
                "output_url": primary_clip["audio_url"],
                "url": primary_clip["audio_url"],
                "task_id": task_id,
                "suno_task_id": task_id,
                "title": primary_clip["title"],
                "duration": primary_clip["duration"],
                "prompt": primary_clip["prompt"],
                "tags": primary_clip["tags"],
                "model": primary_clip["model"],
                "image_url": primary_clip["image_url"],
                "clips": uploaded_clips,
                "clip_count": len(uploaded_clips),
                "total_clips_available": len(suno_clips),
                "node_type": "load_suno_from_taskid",
                "status": "completed",
                "message": f"Loaded {len(uploaded_clips)} clip(s) from Suno task {task_id}"
            }
            
            print(f"\n✅ Successfully loaded Suno music from task {task_id}")
            print(f"🎵 Primary audio URL: {primary_clip['audio_url']}")
            print(f"📊 Loaded {len(uploaded_clips)} of {len(suno_clips)} available clips")
            
            return result
            
        except requests.exceptions.RequestException as e:
            error_msg = f"Suno API request failed: {str(e)}"
            print(f"❌ {error_msg}")
            raise Exception(error_msg)
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ Load Suno error: {e}")
            print(f"📋 Full error trace:\n{error_details}")
            raise Exception(f"Failed to load Suno music: {str(e)}")

    async def execute_separate_vocals(self, node, inputs):
        """
        Separate vocals and instruments from a Suno music track.
        
        API: POST https://api.sunoapi.org/api/v1/vocal-removal/generate
        
        Separation types:
        - separate_vocal: Generates vocal track and instrumental track (default)
        - split_stem: Generates individual instrument tracks (vocals, backing vocals, 
                      drums, bass, guitar, keyboard, strings, brass, woodwinds, 
                      percussion, synthesizer, effects, etc.)
        
        Required params:
        - task_id: The task ID of the music generation
        - audio_id: The ID of the specific audio track to separate
        - type: "separate_vocal" or "split_stem" (default: separate_vocal)
        - callback_url: URL to receive results (or poll with status endpoint)
        
        Optional params:
        - wait_for_completion: Whether to poll for completion (default: true)
        - poll_interval: Seconds between status checks (default: 5)
        - max_poll_attempts: Maximum polling attempts (default: 120)
        """
        import httpx
        import asyncio
        from datetime import datetime
        
        print("\n" + "="*80)
        print("🎵 SUNO VOCAL & INSTRUMENT STEM SEPARATION")
        print("="*80)
        
        try:
            # Get parameters from config or previous node outputs
            task_id = node.config.get("task_id") or node.config.get("taskId")
            audio_id = node.config.get("audio_id") or node.config.get("audioId")
            
            # If no audio_id but we have clips in config, get from first clip
            if not audio_id and "clips" in node.config:
                clips = node.config.get("clips", [])
                if clips and len(clips) > 0:
                    audio_id = clips[0].get("id") or clips[0].get("clip_id") or clips[0].get("audioId")
                    print(f"🔍 Extracted audio_id from clips array: {audio_id}")
            
            # Check inputs if we're missing either parameter
            if (not task_id or not audio_id) and inputs:
                print(f"🔍 Looking in previous node outputs for missing parameters...")
                for key, value in inputs.items():
                    if isinstance(value, dict):
                        if not task_id:
                            task_id = value.get("task_id") or value.get("taskId") or value.get("suno_task_id")
                        if not audio_id:
                            audio_id = value.get("audio_id") or value.get("audioId")
                            # Also check clips array in inputs
                            if not audio_id and "clips" in value:
                                clips = value.get("clips", [])
                                if clips and len(clips) > 0:
                                    audio_id = clips[0].get("id") or clips[0].get("clip_id") or clips[0].get("audioId")
            
            if not task_id or not audio_id:
                raise Exception(f"Missing required parameters. task_id: {task_id}, audio_id: {audio_id}")
            
            # Get separation type
            separation_type = node.config.get("type") or node.config.get("separation_type") or "separate_vocal"
            if separation_type not in ["separate_vocal", "split_stem"]:
                separation_type = "separate_vocal"
            
            # Get callback URL - Suno API requires a valid callback URL
            ngrok_tunnel = os.environ.get("NGROK_TUNNEL", "")
            callback_url = node.config.get("callback_url") or node.config.get("callBackUrl")
            if not callback_url:
                if ngrok_tunnel:
                    callback_url = f"{ngrok_tunnel}/api/webhooks/suno-vocal-separation"
                else:
                    # Use a placeholder URL if no ngrok tunnel is configured
                    # The API requires this field even if we're polling instead of using callbacks
                    callback_url = "https://example.com/callback"
            
            # Get polling parameters
            wait_for_completion = node.config.get("wait_for_completion", True)
            poll_interval = node.config.get("poll_interval", 5)
            max_poll_attempts = node.config.get("max_poll_attempts", 120)
            
            print(f"📋 Parameters:")
            print(f"   Task ID: {task_id}")
            print(f"   Audio ID: {audio_id}")
            print(f"   Separation Type: {separation_type}")
            print(f"   Callback URL: {callback_url}")
            print(f"   Wait for completion: {wait_for_completion}")
            
            # Get Suno API credentials
            suno_api_key = os.environ.get("SUNO_API_KEY")
            if not suno_api_key:
                raise Exception("SUNO_API_KEY not found in environment variables")
            
            # Prepare API request
            api_url = "https://api.sunoapi.org/api/v1/vocal-removal/generate"
            headers = {
                "Authorization": f"Bearer {suno_api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "taskId": task_id,
                "audioId": audio_id,
                "type": separation_type,
                "callBackUrl": callback_url
            }
            
            print(f"\n🚀 Initiating vocal separation...")
            print(f"🔗 API Endpoint: {api_url}")
            
            # Make API request
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(api_url, json=payload, headers=headers)
                
                print(f"📡 Response Status: {response.status_code}")
                
                if response.status_code != 200:
                    error_text = response.text
                    print(f"❌ API Error Response: {error_text}")
                    raise Exception(f"Suno API error ({response.status_code}): {error_text}")
                
                response_data = response.json()
                print(f"✅ API Response: {response_data}")
                
                # Check if API accepted the request (code 200 in response)
                api_code = response_data.get("code")
                api_msg = response_data.get("msg", "")
                
                if api_code != 200:
                    # API returned error code
                    raise Exception(f"Suno API returned code {api_code}: {api_msg}")
                
                print(f"✅ Vocal separation request accepted!")
                
                # Extract separation task ID - this is the NEW task ID for the separation job
                data_obj = response_data.get("data") or {}
                separation_task_id = data_obj.get("taskId") or data_obj.get("task_id") or task_id
                
                print(f"🎵 Separation task ID: {separation_task_id}")
                
            result = {
                "separation_task_id": separation_task_id,
                "original_task_id": task_id,
                "audio_id": audio_id,
                "separation_type": separation_type,
                "callback_url": callback_url,
                "status": "processing",
                "node_type": "separate_vocals"
            }
            
            # If wait_for_completion is False, return immediately
            if not wait_for_completion:
                result["message"] = f"Vocal separation initiated for task {separation_task_id}. Use callback or poll for results."
                print(f"\n✅ Vocal separation task created: {separation_task_id}")
                print(f"⏳ Processing asynchronously - check callback or poll for results")
                return result
            
            # Poll for completion
            print(f"\n⏳ Polling for vocal separation completion...")
            print(f"   Max attempts: {max_poll_attempts}")
            print(f"   Poll interval: {poll_interval}s")
            
            status_url = f"https://api.sunoapi.org/api/v1/generate/record-info?taskId={separation_task_id}"
            attempts = 0
            consecutive_404s = 0
            max_consecutive_404s = 5
            
            while attempts < max_poll_attempts:
                attempts += 1
                await asyncio.sleep(poll_interval)
                
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        status_response = await client.get(status_url, headers=headers)
                        
                        if status_response.status_code == 404:
                            consecutive_404s += 1
                            print(f"⏳ Attempt {attempts}/{max_poll_attempts}: Task not found yet (404 count: {consecutive_404s})")
                            
                            if consecutive_404s >= max_consecutive_404s:
                                print(f"⚠️  Received {consecutive_404s} consecutive 404s, but continuing to poll...")
                            continue
                        
                        consecutive_404s = 0
                        
                        if status_response.status_code != 200:
                            print(f"⚠️  Attempt {attempts}/{max_poll_attempts}: Status check returned {status_response.status_code}")
                            continue
                        
                        status_data = status_response.json()
                        
                        # Debug: Print the full response to understand structure
                        if attempts <= 2:  # Only print first few attempts
                            print(f"🔍 Debug - Full response: {status_data}")
                        
                        # Handle different response structures - check for None explicitly
                        if not status_data:
                            print(f"⚠️  Attempt {attempts}/{max_poll_attempts}: Empty response")
                            continue
                        
                        # Suno API returns: {"code": 200, "msg": "...", "data": {...}}
                        # Check the top-level code first
                        api_code = status_data.get("code")
                        api_msg = status_data.get("msg", "")
                        
                        # Handle error codes (400, 451, 500)
                        if api_code and api_code != 200:
                            print(f"\n❌ Vocal separation failed!")
                            print(f"   Error code: {api_code}")
                            print(f"   Message: {api_msg}")
                            
                            # Provide specific error messages
                            if api_code == 400:
                                error_detail = "Parameter error or unsupported audio format"
                            elif api_code == 451:
                                error_detail = "Source audio file download failed"
                            elif api_code == 500:
                                error_detail = "Server internal error"
                            else:
                                error_detail = api_msg or "Unknown error"
                            
                            result["status"] = "failed"
                            result["error"] = error_detail
                            result["error_code"] = api_code
                            return result
                            
                        data_obj = status_data.get("data")
                        # If data is None or empty, task is still processing - continue polling
                        if not data_obj:
                            if attempts <= 3:  # Only log for first few attempts
                                print(f"⏳ Attempt {attempts}/{max_poll_attempts}: Task still processing (data is None)")
                            continue
                            
                        if not isinstance(data_obj, dict):
                            print(f"⚠️  Attempt {attempts}/{max_poll_attempts}: Invalid data type")
                            print(f"🔍 data_obj value: {data_obj}, type: {type(data_obj)}")
                            continue
                        
                        # Check if vocal_removal_info exists - this means separation is complete
                        # code=200 AND vocal_removal_info present = Success!
                        vocal_removal_info = data_obj.get("vocal_removal_info") or data_obj.get("vocalRemovalInfo")
                        
                        if vocal_removal_info:
                            # Task completed successfully!
                            print(f"📊 Attempt {attempts}/{max_poll_attempts}: Separation complete!")
                            print(f"\n✅ Vocal separation completed!")
                            
                            print(f"🔍 Debug - vocal_removal_info keys: {list(vocal_removal_info.keys()) if vocal_removal_info else 'None'}")
                            
                            print(f"\n📦 Processing separated stems...")
                            
                            # Download and upload stems to S3
                            s3_stems = {}
                            stem_types = []
                            
                            if separation_type == "separate_vocal":
                                stem_types = ["vocal_url", "instrumental_url"]
                            else:  # split_stem
                                stem_types = [
                                    "vocal_url", "backing_vocals_url", "drums_url", "bass_url",
                                    "guitar_url", "keyboard_url", "strings_url", "brass_url",
                                    "woodwinds_url", "percussion_url", "synth_url", "fx_url"
                                ]
                            
                            for stem_type in stem_types:
                                stem_url = vocal_removal_info.get(stem_type)
                                if stem_url:
                                    stem_name = stem_type.replace("_url", "")
                                    print(f"   📥 Downloading {stem_name}...")
                                    
                                    try:
                                        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                                            audio_response = await client.get(stem_url)
                                            if audio_response.status_code == 200:
                                                audio_data = audio_response.content
                                                
                                                # Upload to S3
                                                s3_filename = f"{stem_name}.mp3"
                                                s3_key = f"suno_separated/{separation_task_id}/{s3_filename}"
                                                
                                                self.s3_client.put_object(
                                                    Bucket=self.s3_bucket_name,
                                                    Key=s3_key,
                                                    Body=audio_data,
                                                    ContentType="audio/mpeg"
                                                )
                                                
                                                s3_url = f"https://{self.s3_bucket_name}.s3.{self.s3_region}.amazonaws.com/{s3_key}"
                                                s3_stems[stem_name] = {
                                                    "url": s3_url,
                                                    "s3_key": s3_key,
                                                    "original_url": stem_url
                                                }
                                                print(f"   ✅ Uploaded {stem_name} to S3: {s3_url}")
                                            else:
                                                print(f"   ⚠️  Failed to download {stem_name}: {audio_response.status_code}")
                                    except Exception as e:
                                        print(f"   ❌ Error processing {stem_name}: {e}")
                            
                            result["status"] = "completed"
                            result["stems"] = s3_stems
                            result["stem_count"] = len(s3_stems)
                            result["separation_type"] = separation_type
                            result["message"] = f"Successfully separated {len(s3_stems)} stem(s)"
                            
                            # Add convenient direct access to common stems
                            if "vocal" in s3_stems:
                                result["vocal_url"] = s3_stems["vocal"]["url"]
                            if "instrumental" in s3_stems:
                                result["instrumental_url"] = s3_stems["instrumental"]["url"]
                            
                            print(f"\n✅ Successfully separated {len(s3_stems)} stem(s)")
                            return result
                        
                        # If we have data but no vocal_removal_info, task is still processing
                        else:
                            if attempts <= 3:
                                print(f"⏳ Attempt {attempts}/{max_poll_attempts}: Task processing (waiting for vocal_removal_info)")
                                print(f"🔍 data_obj keys: {list(data_obj.keys())}")
                            continue
                        
                except Exception as e:
                    print(f"⚠️  Attempt {attempts}/{max_poll_attempts}: Polling error - {e}")
                    continue
            
            # Timeout reached
            print(f"\n⏰ Polling timeout reached after {attempts} attempts")
            result["status"] = "timeout"
            result["message"] = f"Vocal separation did not complete within {max_poll_attempts * poll_interval} seconds"
            return result
            
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ Vocal separation error: {e}")
            print(f"📋 Full error trace:\n{error_details}")
            raise Exception(f"Failed to separate vocals: {str(e)}")


    async def execute_add_sound_to_image(self, node, inputs):
        """Execute add sound to image using vision recognition + prompt enhancement + MMAudio
        
        This node:
        1. Analyzes the image using Llama 3.2 Vision or LLaVA (reuses existing method)
        2. Enhances the description using prompt enhancement (reuses existing method)
        3. Generates sound using Replicate MMAudio model
        4. Returns video file with sound synced to image
        
        Args:
            node: Workflow node with config containing:
                - vision_model (str): "llama3.2" or "llava" (default: "llama3.2")
                - image_url (str, optional): Direct image URL
                - custom_vision_prompt (str, optional): Custom prompt for vision analysis
                - seed (int, optional): Random seed for sound generation (default: -1)
                - write_files (bool, optional): Whether to save to S3/local
            inputs: Previous node outputs containing image URLs
        
        Returns:
            Dict with video URL containing image with generated sound
        """
        import replicate
        import httpx
        from io import BytesIO
        
        print(f"\n🎵 Adding sound to image...")
        
        # Extract image URL from config or inputs with comprehensive field checking
        image_url = node.config.get("image_url") or ""
        
        if not image_url and inputs:
            print(f"🔍 Looking for image URL in inputs...")
            print(f"📦 Available inputs: {list(inputs.keys())}")
            
            # Look for image URL in previous node outputs
            for input_key, input_data in inputs.items():
                print(f"  Checking input '{input_key}': {type(input_data)}")
                
                if isinstance(input_data, dict):
                    print(f"    Available fields: {list(input_data.keys())}")
                    
                    # Try various possible field names (prioritize S3 URLs)
                    priority_fields = [
                        "s3_url", "s3_direct_url",  # S3 URLs first
                        "output_url", "image_url", "url",  # Then other URLs
                        "logo_url", "sketch_url", "drawing_url",
                        "presigned_url", "gemini_generated_url"
                    ]
                    
                    for field in priority_fields:
                        if field in input_data and input_data[field]:
                            value = input_data[field]
                            if isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
                                image_url = value
                                print(f"    ✅ Found image URL in field '{field}': {image_url[:100]}...")
                                break
                    
                    if image_url:
                        break
                        
                elif isinstance(input_data, str) and (input_data.startswith("http://") or input_data.startswith("https://")):
                    image_url = input_data
                    print(f"    ✅ Found image URL as string: {image_url[:100]}...")
                    break
        
        if not image_url:
            raise ValueError("Sound generation requires an image URL from a previous node or config")
        
        # Handle base64 images - upload to S3 first
        if image_url.startswith("data:image"):
            print(f"🖼️  Detected base64 image, uploading to S3...")
            try:
                # Parse the data URI
                if "base64," in image_url:
                    header, b64data = image_url.split("base64,", 1)
                    # Extract image format from header (e.g., "data:image/png;base64")
                    img_format = header.split("data:image/")[1].split(";")[0]
                    ext = img_format if img_format in ["png", "jpg", "jpeg", "webp", "gif"] else "png"
                else:
                    raise ValueError("Invalid base64 image format")
                
                # Decode base64
                import base64
                image_bytes = base64.b64decode(b64data)
                
                # Upload to S3
                s3_image_key = f"sound_generation/input_images/{uuid.uuid4()}.{ext}"
                
                # Determine content type
                content_type_map = {
                    "png": "image/png",
                    "jpg": "image/jpeg",
                    "jpeg": "image/jpeg",
                    "webp": "image/webp",
                    "gif": "image/gif"
                }
                content_type = content_type_map.get(ext, "image/png")
                
                # Write to temp file first
                temp_img_file = f"/tmp/sound_input_{uuid.uuid4()}.{ext}"
                with open(temp_img_file, 'wb') as f:
                    f.write(image_bytes)
                
                # Upload to S3 (without ACL as bucket doesn't support it)
                s3 = boto3.client('s3', region_name=AWS_REGION)
                s3.upload_file(
                    temp_img_file,
                    S3_BUCKET,
                    s3_image_key,
                    ExtraArgs={
                        'ContentType': content_type
                    }
                )
                
                # Use region-agnostic S3 URL (automatically redirects to correct region)
                image_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{s3_image_key}"
                
                print(f"✅ Base64 image uploaded to S3: {s3_image_key}")
                print(f"🔗 Image URL: {image_url}")
                
                # Clean up temp file
                try:
                    os.remove(temp_img_file)
                except:
                    pass
                    
            except Exception as e:
                raise ValueError(f"Failed to upload base64 image to S3: {e}")
        
        # Step 1: Analyze image with vision model
        vision_model = node.config.get("vision_model", "llama3.2").lower()
        custom_vision_prompt = node.config.get("custom_vision_prompt", 
            "Describe this image in detail, focusing on the sounds, atmosphere, and audio elements that would be appropriate. What sounds would you hear in this scene?")
        
        print(f"👁️  Analyzing image with {vision_model}...")
        
        image_description = ""
        
        try:
            # Create a temporary node config for vision analysis
            vision_node = type('obj', (object,), {
                'config': {
                    'image_url': image_url,
                    'model': f"{vision_model}-vision",
                    'custom_prompt': custom_vision_prompt
                }
            })()
            
            # Call the existing vision analysis method
            if vision_model == "llama3.2" or vision_model == "llama":
                vision_result = await self.execute_analyze_image_ollama(vision_node, {})
            else:
                # Use the general vision method which supports multiple models
                vision_result = await self.execute_analyze_vision(vision_node, {})
            
            image_description = vision_result.get("analysis", "") or vision_result.get("description", "")
            
            if not image_description:
                print(f"⚠️  Vision analysis returned empty description, using fallback")
                image_description = "ambient scene sounds, atmospheric audio"
            else:
                print(f"✅ Vision analysis complete")
                print(f"📝 Description: {image_description[:150]}...")
            
        except Exception as e:
            print(f"⚠️  Vision analysis failed: {e}")
            print(f"🔄 Using fallback description for sound generation")
            image_description = "ambient scene sounds, atmospheric audio, environmental soundscape"
        
        # Step 2: Enhance the description for sound generation
        print(f"🎨 Enhancing prompt for sound generation...")
        
        sound_prompt = image_description
        
        try:
            # Create a temporary node config for prompt enhancement
            enhance_node = type('obj', (object,), {
                'config': {
                    'prompt': f"Create a detailed audio description for sound generation based on this scene: {image_description}. Focus on specific sounds, their characteristics, and the overall audio atmosphere.",
                    'temperature': '0.7',
                    'preserve_original': 'false'
                }
            })()
            
            # Try to use gnokit enhancement (best quality)
            try:
                enhance_result = await self.execute_enhance_prompt_gnokit(enhance_node, {})
                enhanced = enhance_result.get("enhanced_prompt", "")
                if enhanced and len(enhanced) > 10:
                    sound_prompt = enhanced
                    print(f"✅ Prompt enhancement complete")
                    print(f"🎯 Sound prompt: {sound_prompt[:150]}...")
                else:
                    print(f"⚠️  Enhancement returned short result, using original description")
            except Exception as e:
                print(f"⚠️  Gnokit enhancement failed: {e}")
                print(f"🔄 Using vision description directly")
            
        except Exception as e:
            print(f"⚠️  Prompt enhancement failed: {e}")
            print(f"🔄 Using vision description for sound generation")
        
        # Step 3: Generate sound using Replicate MMAudio
        print(f"🔊 Generating sound with MMAudio...")
        
        # Initialize Replicate client
        if not REPLICATE_API_KEY:
            raise ValueError("REPLICATE_API_KEY not found in environment variables")
        
        import replicate
        client = replicate.Client(api_token=REPLICATE_API_KEY)
        
        # Get seed configuration
        seed = int(node.config.get("seed", -1))
        
        # Ensure we have a valid prompt
        if not sound_prompt or len(sound_prompt.strip()) < 3:
            sound_prompt = "ambient environmental sounds"
            print(f"⚠️  Using default sound prompt: {sound_prompt}")
        
        try:
            # MMAudio requires a video file, not an image
            # We need to convert the static image to a video first
            print(f"🎬 Converting image to video for MMAudio...")
            print(f"   Image: {image_url[:80]}...")
            
            # Download the image first (follow redirects for S3 URL changes)
            async with httpx.AsyncClient(timeout=60.0) as http_client:
                img_response = await http_client.get(image_url, follow_redirects=True)
                img_response.raise_for_status()
                img_data = img_response.content
            
            # Save image to temp file
            temp_img_path = f"/tmp/sound_img_{uuid.uuid4()}.jpg"
            with open(temp_img_path, 'wb') as f:
                f.write(img_data)
            
            print(f"💾 Image saved to: {temp_img_path}")
            
            # Convert image to 8-second video using ffmpeg
            # Create a static video from the image (8 seconds at 24 fps)
            temp_video_path = f"/tmp/sound_video_{uuid.uuid4()}.mp4"
            
            print(f"🎥 Creating 8-second video from image using ffmpeg...")
            
            # Run ffmpeg to create video from image
            ffmpeg_cmd = [
                "ffmpeg",
                "-loop", "1",  # Loop the input image
                "-i", temp_img_path,  # Input image
                "-c:v", "libx264",  # Video codec
                "-t", "8",  # Duration: 8 seconds
                "-pix_fmt", "yuv420p",  # Pixel format
                "-vf", "scale=1280:720",  # Scale to 720p
                "-r", "24",  # Frame rate: 24 fps
                "-y",  # Overwrite output file
                temp_video_path
            ]
            
            import subprocess
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                print(f"❌ ffmpeg error: {result.stderr}")
                raise Exception(f"Failed to convert image to video: {result.stderr}")
            
            print(f"✅ Video created: {temp_video_path}")
            
            # Upload video to S3 to get a URL that MMAudio can access
            s3_video_key = f"sound_generation/input_videos/{uuid.uuid4()}.mp4"
            s3 = boto3.client('s3', region_name=AWS_REGION)
            s3.upload_file(
                temp_video_path,
                S3_BUCKET,
                s3_video_key,
                ExtraArgs={
                    'ContentType': 'video/mp4'
                }
            )
            
            # Use region-agnostic S3 URL (automatically redirects to correct region)
            video_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{s3_video_key}"
            
            print(f"☁️ Video uploaded to S3: {s3_video_key}")
            print(f"🔗 Video URL: {video_url}")
            
            # Now call MMAudio with the video URL
            print(f"📤 Calling Replicate MMAudio model...")
            print(f"   Video: {video_url}")
            print(f"   Prompt: {sound_prompt[:100]}...")
            print(f"   Seed: {seed}")
            
            output = client.run(
                "zsxkib/mmaudio:62871fb59889b2d7c13777f08deb3b36bdff88f7e1d53a50ad7694548a41b484",
                input={
                    "video": video_url,  # Use video URL
                    "prompt": sound_prompt,
                    "seed": seed if seed >= 0 else -1
                }
            )
            
            # Clean up temporary files
            try:
                os.remove(temp_img_path)
                os.remove(temp_video_path)
            except:
                pass
            
            print(f"✅ MMAudio API call completed, processing output...")
            
            # Get the output URL with better error handling
            output_url = None
            if output is None:
                raise ValueError("MMAudio returned None output")
            elif hasattr(output, 'url'):
                output_url = output.url() if callable(output.url) else output.url
            elif isinstance(output, str):
                output_url = output
            elif hasattr(output, '__str__'):
                output_url = str(output)
            else:
                raise ValueError(f"Unexpected output type from MMAudio: {type(output)}")
            
            if not output_url:
                raise ValueError("Failed to extract output URL from MMAudio response")
            
            print(f"✅ Sound generation completed")
            print(f"🎬 Output video: {output_url[:80] if len(output_url) > 80 else output_url}...")
            
            # Optional: Download and upload to S3
            s3_url = None
            audio_url = None
            audio_s3_url = None
            local_path = None
            local_audio_path = None
            write_files = node.config.get("write_files", True)
            
            if write_files:
                try:
                    print(f"⬇️  Downloading output video...")
                    
                    # Download the video file
                    async with httpx.AsyncClient(timeout=300.0) as http_client:
                        response = await http_client.get(output_url)
                        response.raise_for_status()
                        video_data = response.content
                    
                    # Save to temp file
                    temp_filename = f"sound_added_{uuid.uuid4()}.mp4"
                    temp_path = os.path.join("/tmp", temp_filename)
                    
                    with open(temp_path, 'wb') as f:
                        f.write(video_data)
                    
                    local_path = temp_path
                    print(f"💾 Saved video to: {temp_path}")
                    
                    # Extract audio from video using ffmpeg
                    print(f"🎵 Extracting audio from video...")
                    audio_filename = f"sound_only_{uuid.uuid4()}.mp3"
                    temp_audio_path = os.path.join("/tmp", audio_filename)
                    
                    # Run ffmpeg to extract audio
                    ffmpeg_audio_cmd = [
                        "ffmpeg",
                        "-i", temp_path,  # Input video
                        "-vn",  # No video
                        "-acodec", "libmp3lame",  # MP3 codec
                        "-q:a", "2",  # High quality (0-9, lower is better)
                        "-y",  # Overwrite output file
                        temp_audio_path
                    ]
                    
                    audio_result = subprocess.run(
                        ffmpeg_audio_cmd,
                        capture_output=True,
                        text=True
                    )
                    
                    if audio_result.returncode != 0:
                        print(f"⚠️  ffmpeg audio extraction warning: {audio_result.stderr}")
                        # Try alternative format (AAC) if MP3 fails
                        audio_filename = f"sound_only_{uuid.uuid4()}.aac"
                        temp_audio_path = os.path.join("/tmp", audio_filename)
                        ffmpeg_audio_cmd = [
                            "ffmpeg",
                            "-i", temp_path,
                            "-vn",
                            "-acodec", "aac",
                            "-y",
                            temp_audio_path
                        ]
                        audio_result = subprocess.run(ffmpeg_audio_cmd, capture_output=True, text=True)
                    
                    if audio_result.returncode == 0 and os.path.exists(temp_audio_path):
                        local_audio_path = temp_audio_path
                        print(f"✅ Audio extracted to: {temp_audio_path}")
                        
                        # Upload audio to S3
                        try:
                            audio_s3_key = f"sound_generation/{node.id}/{audio_filename}"
                            s3 = boto3.client('s3', region_name=AWS_REGION)
                            
                            # Determine content type based on file extension
                            audio_content_type = "audio/mpeg" if audio_filename.endswith('.mp3') else "audio/aac"
                            
                            s3.upload_file(
                                temp_audio_path,
                                S3_BUCKET,
                                audio_s3_key,
                                ExtraArgs={'ContentType': audio_content_type}
                            )
                            # Use region-agnostic S3 URL
                            audio_s3_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{audio_s3_key}"
                            audio_url = audio_s3_url
                            print(f"☁️  Audio uploaded to S3: {audio_s3_key}")
                            
                        except Exception as e:
                            print(f"⚠️  Audio S3 upload failed: {e}")
                    else:
                        print(f"⚠️  Audio extraction failed")
                    
                    # Upload video to S3
                    try:
                        s3_key = f"sound_generation/{node.id}/{temp_filename}"
                        s3 = boto3.client('s3', region_name=AWS_REGION)
                        s3.upload_file(
                            temp_path,
                            S3_BUCKET,
                            s3_key,
                            ExtraArgs={'ContentType': 'video/mp4'}
                        )
                        # Use region-agnostic S3 URL
                        s3_url = f"https://{S3_BUCKET}.s3.amazonaws.com/{s3_key}"
                        print(f"☁️  Video uploaded to S3: {s3_key}")
                        
                        # Use S3 URL as the final output
                        output_url = s3_url
                        
                    except Exception as e:
                        print(f"⚠️  Video S3 upload failed: {e}")
                    
                except Exception as e:
                    print(f"⚠️  File download/upload failed: {e}")
            
            # Build result - prioritize audio as the main output
            result = {
                "audio_url": audio_url,  # PRIMARY OUTPUT: Extracted audio file
                "audio_s3_url": audio_s3_url,  # S3 URL for audio
                "output_url": audio_url or output_url,  # Audio first, video fallback
                "url": audio_url or output_url,  # Audio first, video fallback
                "video_url": output_url,  # Video with sound
                "video_s3_url": s3_url,  # S3 URL for video
                "s3_url": audio_s3_url or s3_url,  # Audio S3 first, video fallback
                "local_path": local_path,
                "local_audio_path": local_audio_path,
                "original_image_url": image_url,
                "vision_model": vision_model,
                "image_description": image_description,
                "sound_prompt": sound_prompt,
                "seed": seed,
                "node_type": "add_sound_to_image",
                "status": "completed",
                "message": "Sound extracted and uploaded to S3 successfully",
                "outputs": {
                    "audio": audio_url,  # PRIMARY: Extracted audio
                    "audio_s3": audio_s3_url,
                    "video": output_url,  # SECONDARY: Video with sound
                    "video_s3": s3_url
                }
            }
            
            print(f"✅ Sound-to-image processing completed")
            
            return result
            
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            print(f"❌ Sound generation error: {e}")
            print(f"📋 Full error trace:\n{error_details}")
            
            # Try to provide more helpful error messages
            error_msg = str(e)
            if "division" in error_msg.lower() and "zero" in error_msg.lower():
                error_msg = f"Sound generation failed due to invalid parameters. Please check the image URL and try again. Details: {error_msg}"
            elif "timeout" in error_msg.lower():
                error_msg = f"Sound generation timed out. The image might be too large or the service is busy. Details: {error_msg}"
            elif "replicate" in error_msg.lower():
                error_msg = f"Replicate API error. Please check your API key and quota. Details: {error_msg}"
            else:
                error_msg = f"Sound generation failed: {error_msg}"
            
            raise Exception(error_msg)



    async def execute_create_character(self, node, inputs):
        """
        Execute character creation workflow:
        1. Check for reference image (base64) - upload to S3 if present
        2. Enhance prompt (optional)
        3. Generate image (Nano Banana Pro, Gemini, etc.) using reference if available
        4. Remove background (optional)
        """
        print(f"👤 Starting Character Creation Node...")
        
        # Extract configuration
        prompt = node.config.get("prompt", "")
        if not prompt:
            prompt = "A detailed character portrait"
        
        # Check if input is from DNA extractor (should use text only, not image reference)
        use_text_only = False
        if inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    # Check if this is DNA extractor output
                    if input_data.get("node_type") == "extract_dna" or input_data.get("use_text_only"):
                        use_text_only = True
                        print(f"🧬 DNA Extractor detected - using text-based DNA profile only")
                        print(f"⚠️  Skipping image reference to avoid network issues")
                        break
        
        # Check for reference image (base64) - but skip if DNA extractor
        reference_image = None
        reference_s3_url = None
        
        if not use_text_only:
            reference_image = node.config.get("reference_image") or node.config.get("image_url")
        
        # Handle base64 reference image - upload to S3
        if reference_image and isinstance(reference_image, str) and reference_image.startswith("data:image"):
            print(f"🖼️  Found base64 reference image, uploading to S3...")
            try:
                # Parse data URI
                header, b64data = reference_image.split("base64,", 1)
                mime = header.split(";")[0].split(":")[1]
                ext = mime.split("/")[-1]
                if ext == "jpeg":
                    ext = "jpg"
                
                # Decode base64
                image_bytes = base64.b64decode(b64data)
                
                # Upload to S3
                if S3_BUCKET and AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
                    import boto3
                    
                    s3 = boto3.client(
                        's3',
                        aws_access_key_id=AWS_ACCESS_KEY_ID,
                        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                        region_name=AWS_REGION
                    )
                    
                    # Generate unique filename
                    filename = f"character_references/reference_{uuid.uuid4()}.{ext}"
                    
                    # Upload to S3
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=filename,
                        Body=image_bytes,
                        ContentType=mime
                    )
                    
                    # Generate presigned URL (24 hour expiry)
                    reference_s3_url = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': filename},
                        ExpiresIn=86400
                    )
                    
                    print(f"✅ Reference image uploaded to S3: {reference_s3_url[:80]}...")
                else:
                    print(f"⚠️  S3 not configured, cannot upload reference image")
                    
            except Exception as e:
                print(f"❌ Failed to upload reference image to S3: {e}")
        elif reference_image and isinstance(reference_image, str):
            # Already a URL
            reference_s3_url = reference_image
            print(f"🖼️  Using existing reference image URL: {reference_s3_url[:80]}...")
            
        age = node.config.get("age", "")
        height = node.config.get("height", "")
        enhancer_type = node.config.get("enhancer_type", "enhance_prompt_ollama")
        generator_type = node.config.get("generator_type", "generate_nano_banana_pro")
        remove_bg = str(node.config.get("remove_background", "true")).lower() == "true"
        
        # Check for Time Travel / Aging parameters
        target_age = node.config.get("target_age")
        target_height = node.config.get("target_height")
        
        if target_age:
            print(f"⏳ Character Time Travel detected: Target Age {target_age}")
            # Override age with target_age for the prompt
            age = target_age
            if target_height:
                height = target_height
                print(f"📏 Target Height: {target_height}")
        
        # 1. Construct Base Prompt
        character_details = []
        if age:
            character_details.append(f"Age: {age}")
        if height:
            character_details.append(f"Height: {height}")
        
        # Add reference context to prompt if present
        if reference_s3_url:
            character_details.append("Based on reference image")
            
        base_prompt = prompt
        if character_details:
            base_prompt = f"{prompt}. Character details: {', '.join(character_details)}."
            
        print(f"📝 Base Prompt: {base_prompt}")
        
        # 2. Enhance Prompt
        enhanced_prompt = base_prompt
        if enhancer_type and enhancer_type != "none":
            print(f"✨ Enhancing prompt using {enhancer_type}...")
            enhancer_node = WorkflowNode(
                id=f"{node.id}_enhancer",
                type=enhancer_type,
                position={"x": 0, "y": 0},
                config={
                    "prompt": base_prompt,
                }
            )
            
            enhancer_inputs = {} 
            
            enhancer_result = None
            if enhancer_type == "enhance_prompt":
                enhancer_result = await self.execute_enhance_prompt(enhancer_node, enhancer_inputs)
            elif enhancer_type == "enhance_prompt_ollama":
                enhancer_result = await self.execute_enhance_prompt_ollama(enhancer_node, enhancer_inputs)
            elif enhancer_type == "enhance_prompt_gnokit":
                enhancer_result = await self.execute_enhance_prompt_gnokit(enhancer_node, enhancer_inputs)
            elif enhancer_type == "enhance_prompt_fusion":
                enhancer_result = await self.execute_enhance_prompt_fusion(enhancer_node, enhancer_inputs)
            
            if enhancer_result:
                enhanced_prompt = enhancer_result.get("enhanced_prompt", base_prompt)
                print(f"✨ Enhanced Prompt: {enhanced_prompt[:100]}...")
            else:
                print(f"⚠️ Enhancer {enhancer_type} returned no result, using base prompt.")

        # 3. Generate Image
        print(f"🎨 Generating image using {generator_type}...")
        
        # Prepare generator configuration
        generator_config = {
            "prompt": enhanced_prompt,
            "negative_prompt": node.config.get("negative_prompt", ""),
            "num_outputs": 1,
            "output_format": node.config.get("output_format", "png")
        }
        
        # Add reference image if available
        if reference_s3_url:
            generator_config["reference_image_url"] = reference_s3_url
            generator_config["image_url"] = reference_s3_url
            generator_config["image_input"] = [reference_s3_url]  # For Nano Banana Pro
            print(f"🎯 Using reference image for generation")
        
        # Add generator-specific defaults and options
        if generator_type in ["generate_nano_banana", "generate_nano_banana_pro"]:
            # Nano Banana Pro specific options
            # Valid aspect ratios: "match_input_image", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"
            aspect_ratio = node.config.get("aspect_ratio", "match_input_image" if reference_s3_url else "4:3")
            if aspect_ratio == "1024x1024": # Handle common default from other nodes
                aspect_ratio = "1:1"
                
            generator_config.update({
                "aspect_ratio": aspect_ratio,
                "resolution": node.config.get("resolution", "2K"),
                "safety_filter_level": node.config.get("safety_filter_level", "block_only_high")
            })
        else:
            # Default for other generators (like Ideogram, Gemini)
            generator_config["aspect_ratio"] = node.config.get("aspect_ratio", "1024x1024")

        generator_node = WorkflowNode(
            id=f"{node.id}_generator",
            type=generator_type,
            position={"x": 0, "y": 0},
            config=generator_config
        )
        
        generator_inputs = {} 
        
        generator_result = None
        if generator_type in ["generate_nano_banana", "generate_nano_banana_pro"]:
             generator_result = await self.execute_generate_nano_banana(generator_node, generator_inputs)
        elif generator_type == "generate_image": # Gemini or others
             generator_result = await self.execute_generate_image(generator_node, generator_inputs)
        elif generator_type == "generate_ideogram_turbo":
             generator_result = await self.execute_generate_ideogram_turbo(generator_node, generator_inputs)
             
        if not generator_result:
            raise ValueError(f"Generator {generator_type} failed to produce a result")
            
        image_url = generator_result.get("image_url") or generator_result.get("output_url") or generator_result.get("url")
        if not image_url:
             raise ValueError(f"Generator {generator_type} did not return an image URL")
             
        print(f"🖼️ Generated Image URL: {image_url[:100]}...")

        # 4. Remove Background
        final_image_url = image_url
        if remove_bg:
            print(f"✂️ Removing background...")
            rembg_node = WorkflowNode(
                id=f"{node.id}_rembg",
                type="remove_background",
                position={"x": 0, "y": 0},
                config={
                    "image_url": image_url
                }
            )
            
            rembg_result = await self.execute_remove_background(rembg_node, {})
            
            if rembg_result and "image_url" in rembg_result:
                final_image_url = rembg_result["image_url"]
                print(f"✅ Background removed: {final_image_url[:100]}...")
            else:
                print(f"⚠️ Background removal failed or returned no URL, using original image.")

        # 5. Return Result
        result = {
            "image_url": final_image_url,
            "s3_url": final_image_url,
            "character_image": final_image_url,
            "original_image_url": image_url,
            "prompt": base_prompt,
            "enhanced_prompt": enhanced_prompt,
            "age": age,
            "height": height,
            "status": "completed",
            "node_type": "create_character"
        }
        
        # Add reference info if used
        if reference_s3_url:
            result["reference_image_url"] = reference_s3_url
            result["used_reference"] = True
            print(f"✅ Character created using reference image")
        else:
            result["used_reference"] = False
            print(f"✅ Character created without reference")
        
        return result


    async def execute_create_logo(self, node, inputs):
        """Execute logo generation using Replicate LogoAI model"""
        import replicate
        import httpx
        from PIL import Image
        from io import BytesIO
        
        print(f"\n🎨 Creating logo with LogoAI...")
        
        # Extract prompt from config or inputs
        prompt = node.config.get("prompt") or ""
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "prompt" in input_data:
                        prompt = input_data["prompt"]
                        break
                    elif "text" in input_data:
                        prompt = input_data["text"]
                        break
        
        if not prompt:
            raise ValueError("Logo generation requires a prompt")
        
        print(f"📝 Logo prompt: {prompt[:100]}...")
        
        # Get optional parameters - ensure correct types
        width = int(node.config.get("width", 512))
        height = int(node.config.get("height", 512))
        num_outputs = int(node.config.get("num_outputs", 1))
        scheduler = node.config.get("scheduler", "K_EULER")
        num_inference_steps = int(node.config.get("num_inference_steps", 50))
        guidance_scale = float(node.config.get("guidance_scale", 7.5))
        
        # Initialize Replicate client with API key from environment
        if not REPLICATE_API_KEY:
            raise ValueError("REPLICATE_API_KEY not found in environment variables")
        
        client = replicate.Client(api_token=REPLICATE_API_KEY)
        
        # Call Replicate API
        print(f"🔄 Calling Replicate LogoAI model...")
        
        input_params = {
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_outputs": num_outputs,
            "scheduler": scheduler,
            "num_inference_steps": num_inference_steps,
            "guidance_scale": guidance_scale
        }
        
        try:
            output = client.run(
                "mejiabrayan/logoai:67ed00e8999fecd32035074fa0f2e9a31ee03b57a8415e6a5e2f93a242ddd8d2",
                input=input_params
            )
            
            # Output is an array of file objects with .url() method
            print(f"✅ LogoAI returned {len(output) if isinstance(output, list) else 1} logo(s)")
            
            # Collect all logo URLs
            logo_urls = []
            s3_urls = []
            local_paths = []
            
            # Process each output
            outputs = output if isinstance(output, list) else [output]
            
            for idx, logo_output in enumerate(outputs):
                # Get the URL from the file object
                if hasattr(logo_output, 'url'):
                    logo_url = logo_output.url
                elif isinstance(logo_output, str):
                    logo_url = logo_output
                else:
                    logo_url = str(logo_output)
                
                logo_urls.append(logo_url)
                print(f"🖼️  Logo {idx+1}: {logo_url[:80]}...")
                
                # Optional: Download and save to S3/local
                write_files = node.config.get("write_files", False)
                if write_files:
                    try:
                        # Download the image
                        async with httpx.AsyncClient() as client:
                            response = await client.get(logo_url)
                            response.raise_for_status()
                            image_data = response.content
                        
                        # Save to temp file
                        temp_filename = f"logo_{uuid.uuid4()}_{idx}.png"
                        temp_path = os.path.join("/tmp", temp_filename)
                        
                        with open(temp_path, "wb") as f:
                            f.write(image_data)
                        
                        local_paths.append(temp_path)
                        
                        # Upload to S3
                        s3_url = None
                        try:
                            s3_key = f"logos/{node.node_id}/{temp_filename}"
                            self.s3_client.upload_file(
                                temp_path,
                                self.bucket_name,
                                s3_key,
                                ExtraArgs={'ContentType': 'image/png'}
                            )
                            s3_url = self.s3_client.generate_presigned_url(
                                'get_object',
                                Params={'Bucket': self.bucket_name, 'Key': s3_key},
                                ExpiresIn=3600
                            )
                            s3_urls.append(s3_url)
                            print(f"☁️  Uploaded to S3: {s3_key}")
                        except Exception as e:
                            print(f"⚠️  S3 upload failed: {e}")
                            s3_urls.append(None)
                        
                    except Exception as e:
                        print(f"⚠️  File processing failed for logo {idx+1}: {e}")
                        local_paths.append(None)
                        s3_urls.append(None)
            
            # Build result
            result = {
                "logo_urls": logo_urls,
                "logo_url": logo_urls[0] if logo_urls else None,  # Primary logo
                "image_url": logo_urls[0] if logo_urls else None,  # Alias for downstream nodes
                "s3_urls": s3_urls if s3_urls else None,
                "s3_url": s3_urls[0] if s3_urls else None,
                "local_paths": local_paths if local_paths else None,
                "local_path": local_paths[0] if local_paths else None,
                "count": len(logo_urls),
                "prompt": prompt,
                "node_type": "create_logo",
                "status": "completed",
                "message": f"Generated {len(logo_urls)} logo(s) successfully"
            }
            
            print(f"✅ Logo generation completed: {len(logo_urls)} logo(s) created")
            
            return result
            
        except Exception as e:
            print(f"❌ LogoAI error: {e}")
            raise Exception(f"Logo generation failed: {str(e)}")

    async def execute_remove_background(self, node, inputs):
        """Execute background removal/replacement using Replicate MODNet model
        
        Args:
            node: Workflow node with config containing:
                - remove_background (bool): If True, removes background (transparent). If False, replaces with white.
                - image_url (str, optional): Direct image URL
                - write_files (bool, optional): Whether to save to S3/local
            inputs: Previous node outputs containing image URLs
        
        Returns:
            Dict with processed image URL and metadata
        """
        import replicate
        import httpx
        from PIL import Image
        from io import BytesIO
        
        print(f"\n🎭 Processing background...")
        
        # Extract image URL from config or inputs with comprehensive field checking
        image_url = node.config.get("image_url") or ""
        
        if not image_url and inputs:
            print(f"🔍 Looking for image URL in inputs...")
            print(f"📦 Available inputs: {list(inputs.keys())}")
            
            # Look for image URL in previous node outputs
            for input_key, input_data in inputs.items():
                print(f"  Checking input '{input_key}': {type(input_data)}")
                
                if isinstance(input_data, dict):
                    print(f"    Available fields: {list(input_data.keys())}")
                    
                    # Try various possible field names (prioritize S3 URLs)
                    priority_fields = [
                        "s3_url", "s3_direct_url",  # S3 URLs first
                        "output_url", "image_url", "url",  # Then other URLs
                        "logo_url", "sketch_url", "drawing_url",
                        "presigned_url", "gemini_generated_url"
                    ]
                    
                    for field in priority_fields:
                        if field in input_data and input_data[field]:
                            value = input_data[field]
                            if isinstance(value, str) and (value.startswith("http://") or value.startswith("https://")):
                                image_url = value
                                print(f"    ✅ Found image URL in field '{field}': {image_url[:100]}...")
                                break
                    
                    if image_url:
                        break
                        
                elif isinstance(input_data, str) and (input_data.startswith("http://") or input_data.startswith("https://")):
                    image_url = input_data
                    print(f"    ✅ Found image URL as string: {image_url[:100]}...")
                    break
        
        if not image_url:
            raise ValueError("Background processing requires an image URL from a previous node or config")
        
        # Get configuration
        remove_background = node.config.get("remove_background", True)  # Default: remove (transparent)
        action = "removal" if remove_background else "replacement"
        
        print(f"🖼️  Input image: {image_url[:80]}...")
        print(f"🎨 Action: Background {action}")
        
        # Initialize Replicate client with API key from environment
        if not REPLICATE_API_KEY:
            raise ValueError("REPLICATE_API_KEY not found in environment variables")
        
        client = replicate.Client(api_token=REPLICATE_API_KEY)
        
        # Call Replicate MODNet API
        print(f"🔄 Calling Replicate MODNet model...")
        
        try:
            output = client.run(
                "pollinations/modnet:da7d45f3b836795f945f221fc0b01a6d3ab7f5e163f13208948ad436001e2255",
                input={"image": image_url}
            )
            
            # Get the output URL
            if hasattr(output, 'url'):
                output_url = output.url
            elif isinstance(output, str):
                output_url = output
            else:
                output_url = str(output)
            
            print(f"✅ Background {action} completed")
            print(f"🖼️  Output: {output_url[:80]}...")
            
            # Optional: Download and process further (e.g., replace with white background)
            s3_url = None
            local_path = None
            
            if not remove_background:
                # Need to replace transparent background with white
                print(f"🎨 Replacing transparent background with white...")
                try:
                    # Download the image with transparent background
                    async with httpx.AsyncClient() as http_client:
                        response = await http_client.get(output_url)
                        response.raise_for_status()
                        image_data = response.content
                    
                    # Open with PIL and replace transparent with white
                    img = Image.open(BytesIO(image_data)).convert("RGBA")
                    
                    # Create white background
                    white_bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
                    
                    # Composite the image over white background
                    result_img = Image.alpha_composite(white_bg, img)
                    
                    # Convert to RGB (remove alpha channel)
                    result_img = result_img.convert("RGB")
                    
                    # Save to temp file
                    temp_filename = f"bg_replaced_{uuid.uuid4()}.png"
                    temp_path = os.path.join("/tmp", temp_filename)
                    
                    result_img.save(temp_path, "PNG")
                    local_path = temp_path
                    
                    # Upload to S3
                    write_files = node.config.get("write_files", True)
                    if write_files:
                        try:
                            s3_key = f"background_processing/{node.node_id}/{temp_filename}"
                            self.s3_client.upload_file(
                                temp_path,
                                self.bucket_name,
                                s3_key,
                                ExtraArgs={'ContentType': 'image/png'}
                            )
                            s3_url = f"https://{self.bucket_name}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
                            print(f"☁️  Uploaded to S3: {s3_key}")
                            
                            # Use S3 URL as the final output
                            output_url = s3_url
                            
                        except Exception as e:
                            print(f"⚠️  S3 upload failed: {e}")
                    
                    print(f"✅ White background replacement completed")
                    
                except Exception as e:
                    print(f"⚠️  Background replacement failed: {e}")
                    # Fall back to transparent output
            
            # Build result
            result = {
                "image_url": output_url,
                "output_url": output_url,
                "url": output_url,
                "s3_url": s3_url,
                "local_path": local_path,
                "original_image_url": image_url,
                "background_removed": remove_background,
                "background_color": "white" if not remove_background else "transparent",
                "node_type": "remove_background",
                "status": "completed",
                "message": f"Background {action} completed successfully"
            }
            
            print(f"✅ Background processing completed")
            
            return result
            
        except Exception as e:
            print(f"❌ Background processing error: {e}")
            raise Exception(f"Background processing failed: {str(e)}")

    async def execute_dating_app_enhancement(self, node, inputs):
        """
        Execute dating app enhancement node:
        Enhances photos to make them pop for dating apps while strictly preserving facial identity.
        Features:
        - Remove unwanted background objects (trash, clutter, photobombers)
        - Make person stand out (bokeh, vignette, depth, contrast, glow)
        - Overall photo enhancement (lighting, color, quality)
        - Strict identity preservation for dating app authenticity
        """
        import replicate
        import time
        
        start_time = time.time()
        print(f"💘 Starting Dating App Enhancement...")
        
        # 1. Get Image URL
        image_url = node.config.get("image_url")
        if not image_url and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    # Check all possible image URL fields
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
                    elif "output_url" in input_data:
                        image_url = input_data["output_url"]
                        break
        
        if not image_url:
            raise ValueError("Dating App Enhancement requires an image URL")
            
        print(f"🖼️ Input Image: {image_url[:100]}...")
        
        # 2. Get Configuration
        user_prompt = node.config.get("prompt", "")
        style = node.config.get("enhancement_style", "natural")
        strength_val = float(node.config.get("enhancement_strength", 5))
        
        # NEW: Object removal options
        remove_objects = node.config.get("remove_objects", False)
        objects_to_remove = node.config.get("objects_to_remove", "")
        
        # NEW: Make person pop options
        make_person_pop = node.config.get("make_person_pop", False)
        pop_style = node.config.get("pop_style", "bokeh")
        
        # Authenticity mode
        preserve_authenticity = node.config.get("preserve_authenticity", True)
        
        # Clamp strength between 0 and 10
        strength_val = max(0, min(10, strength_val))
        
        # Map 0-10 to prompt_strength
        # For dating apps, we want more subtle changes to preserve authenticity
        # 0 -> 0.10 (Ultra subtle)
        # 5 -> 0.25 (Balanced - good for dating apps)
        # 10 -> 0.45 (More dramatic, but still preserving identity)
        if preserve_authenticity:
            # More conservative range for authentic dating profiles
            prompt_strength = 0.10 + (strength_val / 10.0) * 0.35
        else:
            # Slightly more dramatic range if authenticity is not strict
            prompt_strength = 0.15 + (strength_val / 10.0) * 0.40
        
        print(f"🎚️ Enhancement Strength: {strength_val}/10 (Denoising: {prompt_strength:.2f})")
        print(f"🎨 Style: {style}")
        print(f"🗑️ Remove Objects: {remove_objects}")
        print(f"✨ Make Person Pop: {make_person_pop} ({pop_style if make_person_pop else 'N/A'})")
        print(f"🔒 Strict Authenticity: {preserve_authenticity}")
        
        # 3. Construct Prompt - Multi-part system
        prompt_parts = []
        
        # Base enhancement style
        style_prompts = {
            "natural": "natural photography, subtle improvements, authentic look, soft lighting, real life photo",
            "balanced": "professional photography, balanced lighting, sharp focus, high quality, natural enhancement",
            "lighting": "perfect lighting, improved exposure, natural brightness, clear and well-lit, professional quality",
            "color": "vibrant colors, natural color grading, lively atmosphere, rich tones, color enhancement",
            "portrait": "portrait photography, natural bokeh, soft background blur, subject in focus, natural depth",
            "golden_hour": "golden hour lighting, warm natural tones, sunset glow, soft romantic lighting",
            "studio": "studio quality lighting, professional look, clean aesthetic, sharp details, high-end photography"
        }
        
        style_instruction = style_prompts.get(style, style_prompts["natural"])
        prompt_parts.append(style_instruction)
        
        # NEW: Object removal instructions
        if remove_objects:
            removal_text = "remove background distractions"
            if objects_to_remove:
                # Parse specific objects to remove
                removal_text = f"remove {objects_to_remove} from background"
            removal_text += ", clean background, remove clutter, no unwanted items"
            prompt_parts.append(removal_text)
            print(f"🗑️ Object Removal: {removal_text}")
        
        # NEW: Make person pop instructions
        if make_person_pop:
            pop_styles = {
                "bokeh": "natural bokeh effect, soft background blur, subject in sharp focus, beautiful depth of field, f/1.8 aperture effect",
                "vignette": "natural vignette effect, slightly darker edges, draw attention to center, subtle edge darkening",
                "depth": "enhanced depth separation, clear subject-background distinction, 3D depth effect, subject stands out naturally",
                "contrast": "enhanced subject contrast, vibrant subject colors, person pops from background, natural color pop",
                "glow": "subtle natural glow around subject, soft radiance, ethereal quality, gentle luminosity"
            }
            pop_instruction = pop_styles.get(pop_style, pop_styles["bokeh"])
            prompt_parts.append(pop_instruction)
            print(f"✨ Pop Style: {pop_instruction}")
        
        # User's additional instructions
        if user_prompt:
            prompt_parts.append(user_prompt)
        
        # Identity preservation - CRITICAL for dating apps
        if preserve_authenticity:
            identity_protection = "CRITICAL: preserve exact facial identity, keep original face unchanged, same person, no face alterations, maintain body proportions, authentic representation, dating profile appropriate, honest photo"
        else:
            identity_protection = "preserve facial identity, keep original face features, maintain authenticity"
        
        prompt_parts.append(identity_protection)
        
        # Quality instructions
        prompt_parts.append("high quality, professional photo, 8k, highly detailed")
        
        # Combine all parts
        final_prompt = ", ".join(prompt_parts)
        
        # Negative prompt - what to avoid
        negative_parts = [
            "distorted face", "changed face", "different person", "altered identity",
            "plastic surgery look", "fake appearance", "unrealistic",
            "bad anatomy", "deformed", "blurry", "low quality", "pixelated"
        ]
        
        if preserve_authenticity:
            # Extra strict negative prompts for dating app authenticity
            negative_parts.extend([
                "face swap", "face morph", "body modification", "misleading edits",
                "catfish", "heavily filtered", "instagram filter", "beauty filter",
                "body changes", "proportion changes", "weight changes"
            ])
        
        if remove_objects:
            # Don't remove the person!
            negative_parts.extend([
                "remove person", "delete subject", "empty photo", "no people"
            ])
        
        negative_prompt = ", ".join(negative_parts)
        
        print(f"📝 Final Prompt: {final_prompt[:200]}...")
        print(f"🚫 Negative Prompt: {negative_prompt[:150]}...")
        
        # 4. Call Replicate (SDXL for img2img)
        if not REPLICATE_API_KEY:
            raise ValueError("REPLICATE_API_KEY not found in environment variables")
            
        client = replicate.Client(api_token=REPLICATE_API_KEY)
        
        try:
            print(f"🔄 Calling Replicate (SDXL Refiner)...")
            
            # Memory optimization: Check and resize image if needed
            try:
                from PIL import Image
                from io import BytesIO
                
                print(f"📐 Checking image dimensions for memory optimization...")
                img_response = requests.get(image_url, timeout=30)
                img_response.raise_for_status()
                img = Image.open(BytesIO(img_response.content))
                original_width, original_height = img.size
                
                print(f"📐 Original image: {original_width}x{original_height}")
                
                # Limit to 2K resolution to prevent CUDA OOM
                max_dimension = 2048
                if original_width > max_dimension or original_height > max_dimension:
                    print(f"⚠️  Image too large, resizing to prevent CUDA out of memory...")
                    
                    # Calculate new dimensions maintaining aspect ratio
                    if original_width > original_height:
                        new_width = max_dimension
                        new_height = int((max_dimension / original_width) * original_height)
                    else:
                        new_height = max_dimension
                        new_width = int((max_dimension / original_height) * original_width)
                    
                    img = img.resize((new_width, new_height), Image.LANCZOS)
                    print(f"✅ Resized to {new_width}x{new_height}")
                    
                    # Upload resized image to S3
                    buffer = BytesIO()
                    img.save(buffer, format='PNG', quality=95)
                    buffer.seek(0)
                    
                    s3 = boto3.client('s3', region_name=AWS_REGION)
                    s3_key = f"dating_app_resized/{uuid.uuid4()}.png"
                    
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=s3_key,
                        Body=buffer,
                        ContentType='image/png'
                    )
                    
                    # Update image_url to resized version
                    image_url = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': s3_key},
                        ExpiresIn=3600
                    )
                    print(f"✅ Using resized image from S3: {s3_key}")
                else:
                    print(f"✅ Image size is optimal for processing")
                    
            except Exception as e:
                print(f"⚠️  Could not check/resize image: {e}")
                print(f"⚠️  Proceeding with original image - may cause memory issues on large images")
            
            # Using SDXL for image-to-image with dating app optimizations
            # Reduced inference steps for memory efficiency
            # Try with standard settings first, with CUDA OOM retry logic
            max_retries = 2
            retry_count = 0
            output_url = None
            
            while retry_count <= max_retries and not output_url:
                try:
                    # Adjust settings based on retry attempt
                    if retry_count == 0:
                        # First attempt: standard settings
                        inference_steps = 30
                        guidance = 7.5
                        print(f"🎨 Running enhancement (attempt {retry_count + 1}/{max_retries + 1})")
                    elif retry_count == 1:
                        # Second attempt: reduced settings
                        inference_steps = 25
                        guidance = 7.0
                        print(f"⚠️ Retry with reduced settings (steps={inference_steps}, guidance={guidance})")
                    else:
                        # Final attempt: minimal settings
                        inference_steps = 20
                        guidance = 6.5
                        print(f"⚠️ Final retry with minimal settings (steps={inference_steps}, guidance={guidance})")
                    
                    output = client.run(
                        "stability-ai/sdxl:39ed52f2a78e934b3ba6e2a89f5b1c712de7dfea535525255b1aa35c5565e08b",
                        input={
                            "prompt": final_prompt,
                            "negative_prompt": negative_prompt,
                            "image": image_url,
                            "prompt_strength": prompt_strength,
                            "num_inference_steps": inference_steps,
                            "guidance_scale": guidance,
                            "refine": "expert_ensemble_refiner",
                            "high_noise_frac": 0.8
                        }
                    )
                    
                    # Output is usually a list of URLs
                    if isinstance(output, list) and len(output) > 0:
                        output_url = output[0]
                    elif output:
                        output_url = output
                        
                    # Handle FileOutput object if returned by newer replicate clients
                    if hasattr(output_url, 'url'):
                         output_url = output_url.url
                         
                    # Ensure it's a string
                    output_url = str(output_url)
                    
                    print(f"✅ Enhancement completed: {output_url[:100]}...")
                    break
                    
                except Exception as retry_error:
                    error_msg = str(retry_error).lower()
                    
                    # Check if it's a CUDA out of memory error
                    if "cuda" in error_msg and ("out of memory" in error_msg or "oom" in error_msg):
                        print(f"❌ CUDA out of memory error on attempt {retry_count + 1}")
                        retry_count += 1
                        
                        if retry_count > max_retries:
                            raise Exception(
                                f"Enhancement failed after {max_retries + 1} attempts due to GPU memory constraints. "
                                f"Image size: {img.size}. Try with a smaller image or contact support."
                            )
                        
                        # Wait a bit before retry to let GPU memory clear
                        import asyncio
                        await asyncio.sleep(2)
                    else:
                        # Not a memory error, raise immediately
                        raise
            
            if not output_url:
                raise Exception("Enhancement failed: No output generated")
            
            processing_time = time.time() - start_time
            
            return {
                "image_url": output_url,
                "original_image_url": image_url,
                "enhancement_style": style,
                "enhancement_strength": strength_val,
                "prompt_strength": prompt_strength,
                "remove_objects": remove_objects,
                "objects_removed": objects_to_remove if remove_objects else None,
                "make_person_pop": make_person_pop,
                "pop_style": pop_style if make_person_pop else None,
                "preserve_authenticity": preserve_authenticity,
                "processing_time": processing_time,
                "status": "completed",
                "node_type": "dating_app_enhancement",
                "dating_app_ready": True,
                "authenticity_preserved": preserve_authenticity,
                "retries_needed": retry_count
            }
            
        except Exception as e:
            error_msg = str(e)
            print(f"❌ Enhancement failed: {error_msg}")
            
            # Provide more specific error message for CUDA OOM
            if "cuda" in error_msg.lower() and ("out of memory" in error_msg.lower() or "oom" in error_msg.lower()):
                raise Exception(
                    f"GPU memory error: Image too large for processing. "
                    f"The image was resized to {img.size} but still requires too much GPU memory. "
                    f"Please try with a smaller image or use a different enhancement option."
                )
            
            raise Exception(f"Dating app enhancement failed: {error_msg}")
    
    async def execute_qwen_image_edit(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute qwen_image_edit node - Edit images using Qwen Image Edit Plus LoRA"""
        
        # Get image from previous node or config
        image_url = node.config.get("image_url")
        if not image_url and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        image_url = input_data["image_url"]
                        break
                    elif "s3_url" in input_data:
                        image_url = input_data["s3_url"]
                        break
                    elif "output_url" in input_data:
                        image_url = input_data["output_url"]
                        break
        
        if not image_url:
            raise ValueError("No image provided for Qwen Image Edit")

        # Get prompt
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "prompt" in input_data:
                        prompt = input_data["prompt"]
                        break
                    elif "enhanced_prompt" in input_data:
                        prompt = input_data["enhanced_prompt"]
                        break
        
        if not prompt:
            raise ValueError("No prompt provided for Qwen Image Edit")

        # Get other parameters
        go_fast = node.config.get("go_fast", "true").lower() == "true"
        lora_scale = float(node.config.get("lora_scale", 1.25))
        aspect_ratio = node.config.get("aspect_ratio", "match_input_image")
        lora_weights = node.config.get("lora_weights", "dx8152/qwen-edit-2509-multiple-angles")
        output_format = node.config.get("output_format", "webp")
        output_quality = int(node.config.get("output_quality", 95))
        seed = node.config.get("seed")

        print(f"🎨 [WORKFLOW] Qwen Image Edit")
        print(f"🖼️  Image: {image_url[:80]}...")
        print(f"📝 Prompt: {prompt[:100]}...")

        if not REPLICATE_API_KEY:
            raise ValueError("REPLICATE_API_KEY not found in environment variables")
            
        client = replicate.Client(api_token=REPLICATE_API_KEY)
        
        try:
            # Qwen expects image as an array/list, not a string
            input_params = {
                "image": [image_url] if isinstance(image_url, str) else image_url,
                "prompt": prompt,
                "go_fast": go_fast,
                "lora_scale": lora_scale,
                "aspect_ratio": aspect_ratio,
                "lora_weights": lora_weights,
                "output_format": output_format,
                "output_quality": output_quality
            }
            
            if seed:
                input_params["seed"] = int(seed)

            print(f"🔧 Input params: image={type(input_params['image'])}, prompt={prompt[:50]}...")
            
            output = client.run(
                "qwen/qwen-image-edit-plus-lora",
                input=input_params
            )
            
            # Output is usually a list of URLs or a single URL object
            output_url = None
            if isinstance(output, list) and len(output) > 0:
                output_url = output[0]
            elif output:
                output_url = output
                
            # Handle FileOutput object if returned by newer replicate clients
            if hasattr(output_url, 'url'):
                 output_url = output_url.url

            print(f"✅ Qwen Image Edit completed: {str(output_url)[:100]}...")
            
            return {
                "image_url": str(output_url),
                "original_image_url": image_url,
                "prompt": prompt,
                "status": "completed",
                "node_type": "qwen_image_edit"
            }
            
        except Exception as e:
            print(f"❌ Qwen Image Edit failed: {e}")
            raise Exception(f"Qwen Image Edit failed: {str(e)}")

    async def execute_extract_dna(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute extract_dna node - Extract physical DNA characteristics from image
        
        This node:
        1. Takes an image input (from connection, upload, or URL)
        2. Optionally removes background to focus on subject
        3. Analyzes physical features using Llama 3.2 Vision
        4. Extracts DNA profile (facial features, body type, etc.)
        5. Passes image + DNA profile forward for family member generation
        6. Ensures background removal flag is set for downstream nodes
        """
        
        print(f"🧬 [DNA EXTRACTOR] Starting DNA extraction...")
        
        # Get image source configuration
        image_source = node.config.get("image_source", "connection")
        image_url = node.config.get("image_url")
        image_upload = None
        
        # 1. Get image from appropriate source
        if image_source == "connection" or not image_source:
            # Get from connected node
            if inputs:
                for input_data in inputs.values():
                    if isinstance(input_data, dict):
                        # Check for various image fields
                        for field in ["image_url", "s3_url", "character_image", "final_image_url", "output_url"]:
                            if field in input_data and input_data[field]:
                                image_url = input_data[field]
                                print(f"✅ Found image from connected node: {field}")
                                break
                        if image_url:
                            break
        elif image_source == "upload":
            # Get from upload
            image_upload = node.config.get("image_upload")
        elif image_source == "url":
            # Get from URL
            image_url = node.config.get("image_url")
        
        # Handle base64 upload - upload to S3
        if image_upload and isinstance(image_upload, str) and image_upload.startswith("data:image"):
            print(f"📤 Uploading base64 image to S3...")
            try:
                # Parse data URI
                header, b64data = image_upload.split("base64,", 1)
                mime = header.split(";")[0].split(":")[1]
                ext = mime.split("/")[-1]
                if ext == "jpeg":
                    ext = "jpg"
                
                # Decode base64
                image_bytes = base64.b64decode(b64data)
                
                # Upload to S3
                if S3_BUCKET and AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
                    import boto3
                    
                    s3 = boto3.client(
                        's3',
                        aws_access_key_id=AWS_ACCESS_KEY_ID,
                        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                        region_name=AWS_REGION
                    )
                    
                    # Generate unique filename
                    filename = f"dna_extraction/source_{uuid.uuid4()}.{ext}"
                    
                    # Upload to S3
                    s3.put_object(
                        Bucket=S3_BUCKET,
                        Key=filename,
                        Body=image_bytes,
                        ContentType=mime
                    )
                    
                    # Generate presigned URL (24 hour expiry)
                    image_url = s3.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': S3_BUCKET, 'Key': filename},
                        ExpiresIn=86400
                    )
                    
                    print(f"✅ Image uploaded to S3: {image_url[:80]}...")
                    
            except Exception as e:
                print(f"❌ Failed to upload image: {e}")
                raise ValueError(f"Failed to process uploaded image: {e}")
        
        if not image_url:
            raise ValueError("No image provided for DNA extraction. Please connect an image node or upload an image.")
        
        print(f"🖼️  Source image: {image_url[:80]}...")
        
        # 2. Get DNA extraction configuration
        preserve_background = node.config.get("preserve_background", "remove")
        feature_focus = node.config.get("feature_focus", "full")
        variation_strength = float(node.config.get("variation_strength", 0.3))
        
        # Get original prompt from inputs if available
        original_prompt = ""
        if inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "prompt" in input_data:
                        original_prompt = input_data["prompt"]
                        break
                    elif "enhanced_prompt" in input_data:
                        original_prompt = input_data["enhanced_prompt"]
                        break
        
        print(f"🎯 Feature focus: {feature_focus}")
        print(f"🔄 Variation strength: {variation_strength}")
        print(f"🖼️  Background: {preserve_background}")
        
        # 3. Remove background if requested
        processed_image_url = image_url
        if preserve_background == "remove":
            print(f"✂️ Removing background to focus on subject...")
            try:
                rembg_node = WorkflowNode(
                    id=f"{node.id}_rembg",
                    type="remove_background",
                    position={"x": 0, "y": 0},
                    config={
                        "image_url": image_url,
                        "remove_background": True
                    }
                )
                
                rembg_result = await self.execute_remove_background(rembg_node, {})
                
                if rembg_result and "image_url" in rembg_result:
                    processed_image_url = rembg_result["image_url"]
                    print(f"✅ Background removed: {processed_image_url[:80]}...")
                else:
                    print(f"⚠️ Background removal failed, using original image")
                    
            except Exception as e:
                print(f"⚠️ Background removal error: {e}, using original image")
        
        # 4. Analyze image with Llama Vision to extract DNA features
        print(f"🧬 Analyzing physical characteristics with Llama 3.2 Vision...")
        
        # Build DNA extraction prompt based on feature focus
        if feature_focus == "facial":
            analysis_prompt = """Analyze this person's facial features in extreme detail for DNA extraction:

FACIAL STRUCTURE:
- Face shape (oval, round, square, heart, diamond)
- Jawline definition and prominence
- Cheekbone height and prominence
- Forehead size and shape

EYE CHARACTERISTICS:
- Eye shape (almond, round, hooded, upturned, downturned)
- Eye color and intensity
- Eye size relative to face
- Eyebrow shape, thickness, and arch
- Distance between eyes

NOSE FEATURES:
- Nose shape (straight, aquiline, button, roman)
- Nose width and length
- Nostril size and shape
- Bridge height and definition

MOUTH & SMILE:
- Lip fullness (upper and lower)
- Mouth width relative to face
- Smile characteristics
- Teeth visibility

SKIN & COMPLEXION:
- Skin tone and undertones
- Skin texture
- Notable features (freckles, dimples, beauty marks)

Provide a detailed DNA profile that can be used to generate family members with consistent features."""

        elif feature_focus == "body":
            analysis_prompt = """Analyze this person's body type and physical proportions for DNA extraction:

BODY TYPE:
- Overall build (ectomorph, mesomorph, endomorph)
- Body frame (small, medium, large)
- Height proportions relative to surroundings

PROPORTIONS:
- Shoulder width relative to hips
- Torso length relative to legs
- Limb proportions
- Overall body symmetry

PHYSICAL CHARACTERISTICS:
- Posture and stance
- Muscle definition level
- Body composition indicators

DISTINCTIVE FEATURES:
- Notable physical traits
- Unique proportional characteristics

Provide a detailed DNA profile focusing on body type that can be used to generate family members with consistent physical proportions."""

        else:  # full DNA profile
            analysis_prompt = """Perform comprehensive DNA extraction of all physical characteristics:

COMPLETE FACIAL ANALYSIS:
- Face shape, jawline, cheekbones, forehead
- Eye shape, color, size, eyebrows, spacing
- Nose shape, width, length, bridge
- Lip fullness, mouth width, smile
- Skin tone, texture, notable features
- Ear shape and placement
- Hair color, texture, style

FULL BODY ANALYSIS:
- Body type and build
- Height proportions
- Shoulder-to-hip ratio
- Torso and limb proportions
- Posture and stance
- Muscle definition

GENETIC MARKERS:
- Dominant physical traits
- Hereditary features likely to pass to family
- Unique distinguishing characteristics
- Ethnic/genetic heritage indicators

STYLE & PRESENTATION:
- Current appearance style
- Grooming choices
- Clothing fit indicators

Provide an extremely detailed DNA profile covering ALL physical aspects that can be used to generate consistent family members with proper genetic variation."""

        # Call Llama Vision analysis
        try:
            # Use the analyze_image_ollama method with custom prompt
            vision_node = WorkflowNode(
                id=f"{node.id}_vision",
                type="analyze_image_ollama",
                position={"x": 0, "y": 0},
                config={
                    "image_url": processed_image_url,
                    "model": "llama3.2-vision",
                    "custom_prompt": analysis_prompt
                }
            )
            
            vision_result = await self.execute_analyze_image_ollama(vision_node, {})
            
            dna_analysis = vision_result.get("analysis", "")
            
            if not dna_analysis:
                raise ValueError("Vision analysis returned empty DNA profile")
            
            print(f"✅ DNA profile extracted: {len(dna_analysis)} characters")
            print(f"📋 DNA Profile preview: {dna_analysis[:200]}...")
            
        except Exception as e:
            print(f"❌ DNA extraction failed: {e}")
            raise ValueError(f"Failed to extract DNA profile: {e}")
        
        # 5. Build DNA-enhanced prompt for downstream nodes
        dna_prompt_addition = f"""

DNA PROFILE - MAINTAIN THESE CHARACTERISTICS:
{dna_analysis}

FAMILY VARIATION INSTRUCTIONS:
- Variation strength: {variation_strength} (0=identical, 1=diverse)
- Keep core genetic features consistent
- Allow natural family variation in expression
- Maintain {feature_focus} feature focus
- Preserve genetic markers and hereditary traits
"""

        # Combine with original prompt if available
        if original_prompt:
            enhanced_prompt = original_prompt + dna_prompt_addition
        else:
            enhanced_prompt = f"Generate family member with consistent DNA traits.{dna_prompt_addition}"
        
        # 6. Return comprehensive DNA package
        # NOTE: We output the analyzed image but don't want it used as reference for generation
        # The DNA profile in the prompt is sufficient for maintaining genetic consistency
        result = {
            "analyzed_image_url": processed_image_url,  # Use different key to avoid confusion
            "source_image_url": image_url,  # Original unprocessed image
            "dna_reference_image": processed_image_url,  # For display/reference only
            "s3_url": processed_image_url,
            "dna_profile": dna_analysis,
            "dna_prompt": enhanced_prompt,
            "prompt": enhanced_prompt,
            "enhanced_prompt": enhanced_prompt,
            "feature_focus": feature_focus,
            "variation_strength": variation_strength,
            "background_removed": preserve_background == "remove",
            "remove_background": True,  # Signal to downstream nodes
            "preserve_authenticity": True,
            "use_text_only": True,  # Signal: Don't use image as generation input, use DNA text
            "status": "completed",
            "node_type": "extract_dna",
            "message": f"DNA profile extracted with {feature_focus} focus - use text DNA profile for generation"
        }
        
        print(f"✅ DNA Extractor completed successfully")
        print(f"📦 Output: DNA profile in text format (no image reference needed)")
        print(f"🔗 Ready for text-based family member generation")
        print(f"⚠️  Note: Image is for analysis only, not for img2img generation")
        
        return result

    # ========= GAME ENGINE EXPORT NODES =========
    
    async def execute_export_cocos2d(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Export assets and scene data to Cocos2d format"""
        
        print(f"🎮 [COCOS2D] Starting Cocos2d scene export")
        
        # Get configuration
        scene_name = node.config.get("scene_name", "GameScene")
        export_format = node.config.get("export_format", "cocos_creator")
        include_assets = node.config.get("include_assets", ["sprites", "spritesheets"])
        sprite_resolution = node.config.get("sprite_resolution", "hd")
        generate_atlas = node.config.get("generate_atlas", True)
        atlas_format = node.config.get("atlas_format", "plist")
        layer_structure = node.config.get("layer_structure", True)
        coordinate_system = node.config.get("coordinate_system", "bottom_left")
        physics_engine = node.config.get("physics_engine", "none")
        export_code = node.config.get("export_code", False)
        compression = node.config.get("compression", "none")
        
        # Collect assets from previous nodes
        collected_assets = {
            "sprites": [],
            "animations": [],
            "particles": [],
            "sounds": []
        }
        
        if inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    # Collect image assets
                    if "image_url" in input_data:
                        collected_assets["sprites"].append({
                            "url": input_data["image_url"],
                            "name": input_data.get("sprite_name", f"sprite_{len(collected_assets['sprites'])}")
                        })
                    # Collect video/animation assets
                    if "video_url" in input_data:
                        collected_assets["animations"].append({
                            "url": input_data["video_url"],
                            "name": f"animation_{len(collected_assets['animations'])}"
                        })
        
        print(f"📦 Collected {len(collected_assets['sprites'])} sprites")
        
        # Resolution multipliers
        resolution_scale = {"sd": 1, "hd": 2, "full_hd": 3, "ultra_hd": 4}.get(sprite_resolution, 2)
        
        # Create Cocos2d scene structure
        cocos_scene = {
            "__type__": "cc.Scene",
            "_name": scene_name,
            "_objFlags": 0,
            "_parent": None,
            "_children": [],
            "_active": True,
            "_level": 0,
            "_components": [],
            "_prefab": None,
            "_opacity": 255,
            "_color": {"__type__": "cc.Color", "r": 255, "g": 255, "b": 255, "a": 255},
            "_contentSize": {"__type__": "cc.Size", "width": 960 * resolution_scale, "height": 640 * resolution_scale},
            "_anchorPoint": {"__type__": "cc.Vec2", "x": 0.5, "y": 0.5},
            "_position": {"__type__": "cc.Vec3", "x": 0, "y": 0, "z": 0},
            "_scale": {"__type__": "cc.Vec3", "x": 1, "y": 1, "z": 1},
            "_rotationX": 0,
            "_rotationY": 0,
            "_id": str(uuid.uuid4()),
            "_autoReleaseAssets": False
        }
        
        # Add Canvas component for Cocos Creator
        if export_format == "cocos_creator":
            canvas_component = {
                "__type__": "cc.Canvas",
                "node": {"__id__": 1},
                "_enabled": True,
                "_designResolution": {
                    "__type__": "cc.Size",
                    "width": 960 * resolution_scale,
                    "height": 640 * resolution_scale
                },
                "_fitWidth": False,
                "_fitHeight": True
            }
            cocos_scene["_components"].append(canvas_component)
        
        # Add sprites to scene
        sprite_nodes = []
        for idx, sprite_asset in enumerate(collected_assets["sprites"]):
            # Calculate position based on coordinate system
            if coordinate_system == "bottom_left":
                pos_x = 100 + (idx * 150)
                pos_y = 100
            elif coordinate_system == "top_left":
                pos_x = 100 + (idx * 150)
                pos_y = cocos_scene["_contentSize"]["height"] - 100
            else:  # center
                pos_x = (cocos_scene["_contentSize"]["width"] / 2) + (idx * 150) - (len(collected_assets["sprites"]) * 75)
                pos_y = cocos_scene["_contentSize"]["height"] / 2
            
            sprite_node = {
                "__type__": "cc.Sprite",
                "_name": sprite_asset["name"],
                "_objFlags": 0,
                "_parent": {"__id__": 1},
                "_children": [],
                "_active": True,
                "_components": [],
                "_prefab": None,
                "_opacity": 255,
                "_color": {"__type__": "cc.Color", "r": 255, "g": 255, "b": 255, "a": 255},
                "_contentSize": {"__type__": "cc.Size", "width": 200, "height": 200},
                "_anchorPoint": {"__type__": "cc.Vec2", "x": 0.5, "y": 0.5},
                "_position": {"__type__": "cc.Vec3", "x": pos_x, "y": pos_y, "z": 0},
                "_scale": {"__type__": "cc.Vec3", "x": 1, "y": 1, "z": 1},
                "_rotationX": 0,
                "_rotationY": 0,
                "_id": str(uuid.uuid4()),
                "_spriteFrame": {
                    "__type__": "cc.SpriteFrame",
                    "_name": sprite_asset["name"],
                    "_texture": sprite_asset["url"],
                    "_rect": {"__type__": "cc.Rect", "x": 0, "y": 0, "width": 512, "height": 512}
                },
                "_type": 0,
                "_sizeMode": 1,
                "_fillType": 0,
                "_fillCenter": {"__type__": "cc.Vec2", "x": 0, "y": 0},
                "_fillStart": 0,
                "_fillRange": 0,
                "_isTrimmedMode": True,
                "_atlas": None
            }
            
            # Add physics body if requested
            if physics_engine != "none":
                physics_body = {
                    "__type__": f"cc.{'BoxCollider' if physics_engine == 'box2d' else 'PhysicsCollider'}",
                    "node": {"__id__": len(sprite_nodes) + 2},
                    "_enabled": True,
                    "_density": 1.0,
                    "_sensor": False,
                    "_restitution": 0.0,
                    "_friction": 0.2
                }
                sprite_node["_components"].append(physics_body)
            
            sprite_nodes.append(sprite_node)
            cocos_scene["_children"].append({"__id__": len(sprite_nodes) + 1})
        
        # Create sprite sheet metadata if requested
        sprite_sheet_data = None
        if generate_atlas and len(collected_assets["sprites"]) > 0:
            print(f"🖼️  Generating sprite sheet with {atlas_format} format")
            
            if atlas_format == "plist":
                # Cocos2d Plist format
                sprite_sheet_data = {
                    "metadata": {
                        "format": 3,
                        "pixelFormat": "RGBA8888",
                        "premultiplyAlpha": False,
                        "textureFileName": f"{scene_name}_atlas.png",
                        "size": f"{{2048,2048}}"
                    },
                    "frames": {}
                }
                
                for idx, sprite in enumerate(collected_assets["sprites"]):
                    sprite_sheet_data["frames"][sprite["name"]] = {
                        "frame": f"{{0,{idx*256},512,512}}",
                        "offset": "{0,0}",
                        "rotated": False,
                        "sourceColorRect": "{{0,0},{512,512}}",
                        "sourceSize": "{512,512}"
                    }
            
            elif atlas_format == "json":
                # JSON format (universal)
                sprite_sheet_data = {
                    "frames": {},
                    "meta": {
                        "app": "Workflow API - Cocos2d Exporter",
                        "version": "1.0",
                        "image": f"{scene_name}_atlas.png",
                        "format": "RGBA8888",
                        "size": {"w": 2048, "h": 2048},
                        "scale": resolution_scale
                    }
                }
                
                for idx, sprite in enumerate(collected_assets["sprites"]):
                    sprite_sheet_data["frames"][sprite["name"]] = {
                        "frame": {"x": 0, "y": idx * 256, "w": 512, "h": 512},
                        "rotated": False,
                        "trimmed": False,
                        "spriteSourceSize": {"x": 0, "y": 0, "w": 512, "h": 512},
                        "sourceSize": {"w": 512, "h": 512}
                    }
        
        # Generate scene loading code if requested
        scene_code = None
        if export_code:
            if export_format == "cocos_creator":
                # JavaScript/TypeScript code for Cocos Creator
                scene_code = f"""// Auto-generated scene loading code for {scene_name}
import {{ _decorator, Component, Node, Sprite, SpriteFrame, resources }} from 'cc';
const {{ ccclass, property }} = _decorator;

@ccclass('{scene_name}')
export class {scene_name} extends Component {{
    
    onLoad() {{
        this.loadSceneAssets();
    }}
    
    loadSceneAssets() {{
        // Load sprites
        {chr(10).join([f'        this.loadSprite("{sprite["name"]}", "{sprite["url"]}");' for sprite in collected_assets["sprites"]])}
    }}
    
    loadSprite(name: string, url: string) {{
        resources.load(url, SpriteFrame, (err, spriteFrame) => {{
            if (!err) {{
                const node = new Node(name);
                const sprite = node.addComponent(Sprite);
                sprite.spriteFrame = spriteFrame;
                this.node.addChild(node);
            }}
        }});
    }}
}}
"""
            else:
                # C++ code for Cocos2d-x
                scene_code = f"""// Auto-generated scene loading code for {scene_name}
#include "{scene_name}.h"

USING_NS_CC;

Scene* {scene_name}::createScene()
{{
    return {scene_name}::create();
}}

bool {scene_name}::init()
{{
    if (!Scene::init())
    {{
        return false;
    }}
    
    // Load sprites
    {chr(10).join([f'    this->loadSprite("{sprite["name"]}", "{sprite["url"]}");' for sprite in collected_assets["sprites"]])}
    
    return true;
}}

void {scene_name}::loadSprite(const std::string& name, const std::string& url)
{{
    auto sprite = Sprite::create(url);
    if (sprite)
    {{
        this->addChild(sprite);
    }}
}}
"""
        
        # Save scene to S3
        scene_json = json.dumps({
            "scene": cocos_scene,
            "nodes": sprite_nodes,
            "metadata": {
                "format": export_format,
                "version": "1.0",
                "resolution": sprite_resolution,
                "coordinate_system": coordinate_system,
                "physics_engine": physics_engine
            }
        }, indent=2)
        
        s3 = boto3.client('s3', region_name=AWS_REGION)
        scene_filename = f"game-exports/cocos2d/{scene_name}_{uuid.uuid4()}.json"
        
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=scene_filename,
            Body=scene_json.encode('utf-8'),
            ContentType='application/json'
        )
        
        scene_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': scene_filename},
            ExpiresIn=86400
        )
        
        # Save sprite sheet data if generated
        sprite_sheet_url = None
        if sprite_sheet_data:
            sheet_ext = "plist" if atlas_format == "plist" else "json"
            sheet_filename = f"game-exports/cocos2d/{scene_name}_atlas_{uuid.uuid4()}.{sheet_ext}"
            
            if atlas_format == "plist":
                # Convert to XML plist format
                import plistlib
                sheet_content = plistlib.dumps(sprite_sheet_data, fmt=plistlib.FMT_XML)
            else:
                sheet_content = json.dumps(sprite_sheet_data, indent=2).encode('utf-8')
            
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=sheet_filename,
                Body=sheet_content,
                ContentType='application/xml' if atlas_format == 'plist' else 'application/json'
            )
            
            sprite_sheet_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': sheet_filename},
                ExpiresIn=86400
            )
        
        # Save code if generated
        code_url = None
        if scene_code:
            code_ext = "ts" if export_format == "cocos_creator" else "cpp"
            code_filename = f"game-exports/cocos2d/{scene_name}_{uuid.uuid4()}.{code_ext}"
            
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=code_filename,
                Body=scene_code.encode('utf-8'),
                ContentType='text/plain'
            )
            
            code_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': code_filename},
                ExpiresIn=86400
            )
        
        print(f"✅ Cocos2d scene exported successfully")
        print(f"📦 Scene: {scene_url[:80]}...")
        if sprite_sheet_url:
            print(f"🖼️  Atlas: {sprite_sheet_url[:80]}...")
        if code_url:
            print(f"💻 Code: {code_url[:80]}...")
        
        result = {
            "scene_url": scene_url,
            "scene_name": scene_name,
            "export_format": export_format,
            "sprite_count": len(collected_assets["sprites"]),
            "resolution": sprite_resolution,
            "coordinate_system": coordinate_system,
            "physics_engine": physics_engine,
            "status": "completed"
        }
        
        if sprite_sheet_url:
            result["sprite_sheet_url"] = sprite_sheet_url
            result["atlas_format"] = atlas_format
        
        if code_url:
            result["code_url"] = code_url
        
        return result

    async def execute_export_unity(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Export assets to Unity-compatible format"""
        
        print(f"🔷 [UNITY] Starting Unity asset export")
        
        # Get configuration
        unity_version = node.config.get("unity_version", "2022")
        asset_type = node.config.get("asset_type", "prefab")
        render_pipeline = node.config.get("render_pipeline", "urp")
        material_type = node.config.get("material_type", "standard")
        include_colliders = node.config.get("include_colliders", True)
        lod_levels = int(node.config.get("lod_levels", 3))
        texture_format = node.config.get("texture_format", "auto")
        generate_package = node.config.get("generate_package", True)
        
        # Collect assets from previous nodes
        collected_assets = []
        
        if inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        collected_assets.append({
                            "url": input_data["image_url"],
                            "type": "texture",
                            "name": input_data.get("name", f"Texture_{len(collected_assets)}")
                        })
                    if "model_url" in input_data or "glb_url" in input_data:
                        collected_assets.append({
                            "url": input_data.get("model_url") or input_data.get("glb_url"),
                            "type": "model",
                            "name": input_data.get("name", f"Model_{len(collected_assets)}")
                        })
        
        print(f"📦 Collected {len(collected_assets)} assets")
        
        # Create Unity meta files
        unity_assets = []
        
        for asset in collected_assets:
            if asset["type"] == "texture":
                # Unity texture import settings
                texture_meta = {
                    "fileFormatVersion": 2,
                    "guid": str(uuid.uuid4()).replace('-', ''),
                    "TextureImporter": {
                        "internalIDToNameTable": [],
                        "externalObjects": {},
                        "serializedVersion": 11,
                        "mipmaps": {
                            "mipMapMode": 0,
                            "enableMipMap": 1,
                            "sRGBTexture": 1,
                            "linearTexture": 0,
                            "fadeOut": 0
                        },
                        "bumpmap": {
                            "convertToNormalMap": 0,
                            "externalNormalMap": 0,
                            "heightScale": 0.25,
                            "normalMapFilter": 0
                        },
                        "isReadable": 0,
                        "streamingMipmaps": 0,
                        "streamingMipmapsPriority": 0,
                        "grayScaleToAlpha": 0,
                        "generateCubemap": 6,
                        "cubemapConvolution": 0,
                        "seamlessCubemap": 0,
                        "textureFormat": 1,
                        "maxTextureSize": 2048,
                        "textureSettings": {
                            "serializedVersion": 2,
                            "filterMode": 1,
                            "aniso": 1,
                            "mipBias": 0,
                            "wrapU": 0,
                            "wrapV": 0,
                            "wrapW": 0
                        },
                        "nPOTScale": 1,
                        "lightmap": 0,
                        "compressionQuality": 50,
                        "spriteMode": 0,
                        "spriteExtrude": 1,
                        "spriteMeshType": 1,
                        "alignment": 0,
                        "spritePivot": {"x": 0.5, "y": 0.5},
                        "spritePixelsToUnits": 100,
                        "spriteBorder": {"x": 0, "y": 0, "z": 0, "w": 0},
                        "spriteGenerateFallbackPhysicsShape": 1,
                        "alphaUsage": 1,
                        "alphaIsTransparency": 0,
                        "spriteTessellationDetail": -1,
                        "textureType": 0,
                        "textureShape": 1,
                        "singleChannelComponent": 0,
                        "maxTextureSizeSet": 0,
                        "compressionQualitySet": 0,
                        "textureFormatSet": 0,
                        "platformSettings": []
                    }
                }
                
                # Create material
                material_data = {
                    "fileFormatVersion": 2,
                    "guid": str(uuid.uuid4()).replace('-', ''),
                    "Material": {
                        "serializedVersion": 6,
                        "m_ObjectHideFlags": 0,
                        "m_CorrespondingSourceObject": {"fileID": 0},
                        "m_PrefabInstance": {"fileID": 0},
                        "m_PrefabAsset": {"fileID": 0},
                        "m_Name": asset["name"] + "_Material",
                        "m_Shader": {
                            "fileID": 46 if render_pipeline == "built_in" else 4800000
                        },
                        "m_ShaderKeywords": "",
                        "m_LightmapFlags": 4,
                        "m_EnableInstancingVariants": 0,
                        "m_DoubleSidedGI": 0,
                        "m_CustomRenderQueue": -1,
                        "m_SavedProperties": {
                            "m_TexEnvs": [
                                {
                                    "_MainTex": {
                                        "m_Texture": {"fileID": 0},
                                        "m_Scale": {"x": 1, "y": 1},
                                        "m_Offset": {"x": 0, "y": 0}
                                    }
                                }
                            ],
                            "m_Floats": [
                                {"_Metallic": 0},
                                {"_Glossiness": 0.5}
                            ],
                            "m_Colors": [
                                {"_Color": {"r": 1, "g": 1, "b": 1, "a": 1}}
                            ]
                        }
                    }
                }
                
                unity_assets.append({
                    "name": asset["name"],
                    "url": asset["url"],
                    "meta": texture_meta,
                    "material": material_data
                })
            
            elif asset["type"] == "model":
                # Unity model import settings
                model_meta = {
                    "fileFormatVersion": 2,
                    "guid": str(uuid.uuid4()).replace('-', ''),
                    "ModelImporter": {
                        "serializedVersion": 20,
                        "internalIDToNameTable": [],
                        "externalObjects": {},
                        "materials": {
                            "materialImportMode": 1,
                            "materialName": 0,
                            "materialSearch": 1,
                            "materialLocation": 1
                        },
                        "animations": {
                            "legacyGenerateAnimations": 4,
                            "bakeSimulation": 0,
                            "resampleCurves": 1,
                            "optimizeGameObjects": 0,
                            "motionNodeName": "",
                            "rigImportErrors": "",
                            "rigImportWarnings": "",
                            "animationImportErrors": "",
                            "animationImportWarnings": "",
                            "animationRetargetingWarnings": "",
                            "animationDoRetargetingWarnings": 0,
                            "importAnimatedCustomProperties": 0,
                            "importConstraints": 0,
                            "animationCompression": 1,
                            "animationRotationError": 0.5,
                            "animationPositionError": 0.5,
                            "animationScaleError": 0.5,
                            "animationWrapMode": 0,
                            "extraExposedTransformPaths": [],
                            "extraUserProperties": [],
                            "clipAnimations": []
                        },
                        "meshes": {
                            "lODScreenPercentages": [],
                            "globalScale": 1,
                            "meshCompression": 0,
                            "addColliders": include_colliders,
                            "useSRGBMaterialColor": 1,
                            "sortHierarchyByName": 1,
                            "importVisibility": 1,
                            "importBlendShapes": 1,
                            "importCameras": 1,
                            "importLights": 1,
                            "fileIdsGeneration": 2,
                            "swapUVChannels": 0,
                            "generateSecondaryUV": 0,
                            "useFileUnits": 1,
                            "keepQuads": 0,
                            "weldVertices": 1,
                            "preserveHierarchy": 0,
                            "skinWeightsMode": 0,
                            "maxBonesPerVertex": 4,
                            "minBoneWeight": 0.001,
                            "meshOptimizationFlags": -1,
                            "indexFormat": 0,
                            "secondaryUVAngleDistortion": 8,
                            "secondaryUVAreaDistortion": 15.000001,
                            "secondaryUVHardAngle": 88,
                            "secondaryUVMarginMethod": 1,
                            "secondaryUVMinLightmapResolution": 40,
                            "secondaryUVMinObjectScale": 1,
                            "secondaryUVPackMargin": 4,
                            "useFileScale": 1
                        }
                    }
                }
                
                unity_assets.append({
                    "name": asset["name"],
                    "url": asset["url"],
                    "meta": model_meta
                })
        
        # Create Unity package manifest if requested
        package_manifest = None
        if generate_package:
            package_manifest = {
                "name": f"com.workflow.{asset_type}",
                "version": "1.0.0",
                "displayName": f"Workflow Generated {asset_type.title()}",
                "description": f"Auto-generated Unity {asset_type} from Workflow API",
                "unity": f"{unity_version}.0",
                "dependencies": {},
                "keywords": ["workflow", "generated", asset_type],
                "author": {
                    "name": "Workflow API",
                    "email": "support@workflow.com",
                    "url": "https://workflow.com"
                }
            }
        
        # Save to S3
        s3 = boto3.client('s3', region_name=AWS_REGION)
        export_data = {
            "unity_version": unity_version,
            "asset_type": asset_type,
            "render_pipeline": render_pipeline,
            "assets": unity_assets,
            "package_manifest": package_manifest
        }
        
        export_filename = f"game-exports/unity/unity_export_{uuid.uuid4()}.json"
        
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=export_filename,
            Body=json.dumps(export_data, indent=2).encode('utf-8'),
            ContentType='application/json'
        )
        
        export_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': export_filename},
            ExpiresIn=86400
        )
        
        print(f"✅ Unity export completed")
        print(f"📦 Export: {export_url[:80]}...")
        
        return {
            "export_url": export_url,
            "unity_version": unity_version,
            "asset_type": asset_type,
            "render_pipeline": render_pipeline,
            "asset_count": len(unity_assets),
            "package_generated": generate_package,
            "status": "completed"
        }

    async def execute_export_unreal(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Export assets to Unreal Engine format"""
        
        print(f"🔶 [UNREAL] Starting Unreal Engine export")
        
        # Get configuration
        unreal_version = node.config.get("unreal_version", "ue5_4")
        nanite_enabled = node.config.get("nanite_enabled", False)
        lumen_enabled = node.config.get("lumen_enabled", False)
        material_workflow = node.config.get("material_workflow", "pbr")
        collision_complexity = node.config.get("collision_complexity", "simple")
        texture_resolution = node.config.get("texture_resolution", "2048")
        generate_blueprint = node.config.get("generate_blueprint", False)
        
        # Collect assets
        collected_assets = []
        
        if inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        collected_assets.append({
                            "url": input_data["image_url"],
                            "type": "texture"
                        })
                    if "model_url" in input_data or "glb_url" in input_data:
                        collected_assets.append({
                            "url": input_data.get("model_url") or input_data.get("glb_url"),
                            "type": "static_mesh"
                        })
        
        print(f"📦 Collected {len(collected_assets)} assets for Unreal")
        
        # Create Unreal asset metadata
        unreal_export = {
            "version": unreal_version,
            "nanite_enabled": nanite_enabled,
            "lumen_enabled": lumen_enabled,
            "material_workflow": material_workflow,
            "assets": collected_assets,
            "import_settings": {
                "texture": {
                    "compression": "TC_Default",
                    "mip_gen_settings": "TMGS_FromTextureGroup",
                    "srgb": True,
                    "max_texture_size": int(texture_resolution)
                },
                "static_mesh": {
                    "build_nanite": nanite_enabled,
                    "build_reversed_index_buffer": True,
                    "generate_lightmap_uvs": True,
                    "collision_complexity": collision_complexity,
                    "min_lightmap_resolution": 64,
                    "src_lightmap_index": 0,
                    "dst_lightmap_index": 1
                }
            }
        }
        
        # Generate Blueprint if requested
        if generate_blueprint:
            blueprint_class = f"""// Auto-generated Blueprint Class
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "WorkflowGeneratedActor.generated.h"

UCLASS()
class GAME_API AWorkflowGeneratedActor : public AActor
{{
    GENERATED_BODY()
    
public:
    AWorkflowGeneratedActor();
    
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Components")
    UStaticMeshComponent* MeshComponent;
    
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Materials")
    UMaterialInstanceDynamic* DynamicMaterial;
    
protected:
    virtual void BeginPlay() override;
    
public:
    virtual void Tick(float DeltaTime) override;
    
    UFUNCTION(BlueprintCallable, Category = "Workflow")
    void LoadGeneratedAssets();
}};
"""
            unreal_export["blueprint_class"] = blueprint_class
        
        # Save to S3
        s3 = boto3.client('s3', region_name=AWS_REGION)
        export_filename = f"game-exports/unreal/unreal_export_{uuid.uuid4()}.json"
        
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=export_filename,
            Body=json.dumps(unreal_export, indent=2).encode('utf-8'),
            ContentType='application/json'
        )
        
        export_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': export_filename},
            ExpiresIn=86400
        )
        
        print(f"✅ Unreal Engine export completed")
        print(f"📦 Export: {export_url[:80]}...")
        
        return {
            "export_url": export_url,
            "unreal_version": unreal_version,
            "nanite_enabled": nanite_enabled,
            "lumen_enabled": lumen_enabled,
            "asset_count": len(collected_assets),
            "blueprint_generated": generate_blueprint,
            "status": "completed"
        }

    async def execute_export_godot(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Export assets to Godot Engine format"""
        
        print(f"🎯 [GODOT] Starting Godot Engine export")
        
        # Get configuration
        godot_version = node.config.get("godot_version", "4")
        scene_format = node.config.get("scene_format", "tscn")
        node_structure = node.config.get("node_structure", "node2d")
        physics_layer = int(node.config.get("physics_layer", 1))
        export_scripts = node.config.get("export_scripts", False)
        compress_textures = node.config.get("compress_textures", "lossless")
        
        # Collect assets
        collected_assets = []
        
        if inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        collected_assets.append({
                            "url": input_data["image_url"],
                            "type": "texture",
                            "name": input_data.get("name", f"Sprite_{len(collected_assets)}")
                        })
        
        print(f"📦 Collected {len(collected_assets)} assets for Godot")
        
        # Create Godot scene (.tscn format)
        godot_scene = f"""[gd_scene load_steps={len(collected_assets) + 1} format=3 uid="uid://{uuid.uuid4().hex[:16]}"]

[node name="{node_structure.upper()}" type="{node_structure.title()}"]
"""
        
        # Add sprites/assets to scene
        for idx, asset in enumerate(collected_assets):
            godot_scene += f"""
[node name="{asset['name']}" type="Sprite2D" parent="."]
position = Vector2({100 + idx * 150}, 300)
texture = preload("res://assets/{asset['name']}.png")
"""
            
            if physics_layer > 0:
                godot_scene += f"""
[node name="CollisionShape2D" type="CollisionShape2D" parent="{asset['name']}"]
shape = preload("res://shapes/{asset['name']}_shape.tres")
collision_layer = {physics_layer}
collision_mask = {physics_layer}
"""
        
        # Generate GDScript if requested
        gdscript_code = None
        if export_scripts:
            gdscript_code = f"""extends {node_structure.title()}

# Auto-generated Godot script

func _ready():
\tprint("Scene loaded: Workflow generated")
\tload_assets()

func _process(delta):
\tpass

func load_assets():
\t# Load generated assets
\tfor child in get_children():
\t\tif child is Sprite2D:
\t\t\tprint("Loaded sprite: ", child.name)
"""
        
        # Save to S3
        s3 = boto3.client('s3', region_name=AWS_REGION)
        
        # Save scene file
        scene_filename = f"game-exports/godot/scene_{uuid.uuid4()}.{scene_format}"
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=scene_filename,
            Body=godot_scene.encode('utf-8'),
            ContentType='text/plain'
        )
        
        scene_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': scene_filename},
            ExpiresIn=86400
        )
        
        # Save script if generated
        script_url = None
        if gdscript_code:
            script_filename = f"game-exports/godot/script_{uuid.uuid4()}.gd"
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=script_filename,
                Body=gdscript_code.encode('utf-8'),
                ContentType='text/plain'
            )
            
            script_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': script_filename},
                ExpiresIn=86400
            )
        
        print(f"✅ Godot Engine export completed")
        print(f"📦 Scene: {scene_url[:80]}...")
        
        result = {
            "scene_url": scene_url,
            "godot_version": godot_version,
            "scene_format": scene_format,
            "node_structure": node_structure,
            "asset_count": len(collected_assets),
            "physics_layer": physics_layer,
            "status": "completed"
        }
        
        if script_url:
            result["script_url"] = script_url
            result["script_generated"] = True
        
        return result

    async def execute_create_spritesheet(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Generate optimized sprite sheet from multiple images"""
        
        print(f"🖼️  [SPRITESHEET] Creating sprite sheet")
        
        # Get configuration
        layout_algorithm = node.config.get("layout_algorithm", "maxrects")
        atlas_size = int(node.config.get("atlas_size", 2048))
        padding = int(node.config.get("padding", 2))
        trim_transparent = node.config.get("trim_transparent", True)
        output_format = node.config.get("output_format", "json")
        generate_multiple_scales = node.config.get("generate_multiple_scales", False)
        
        # Collect images from previous nodes
        image_urls = []
        
        if inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        image_urls.append(input_data["image_url"])
                    elif "images" in input_data and isinstance(input_data["images"], list):
                        image_urls.extend(input_data["images"])
        
        if not image_urls:
            raise ValueError("No images provided for sprite sheet generation")
        
        print(f"📦 Creating sprite sheet from {len(image_urls)} images")
        print(f"📐 Atlas size: {atlas_size}x{atlas_size}, Padding: {padding}px")
        
        # Download and process images
        from PIL import Image
        sprites = []
        
        for idx, url in enumerate(image_urls):
            try:
                response = requests.get(url, timeout=30)
                response.raise_for_status()
                img = Image.open(BytesIO(response.content))
                
                # Convert to RGBA if needed
                if img.mode != 'RGBA':
                    img = img.convert('RGBA')
                
                # Trim transparent borders if requested
                if trim_transparent:
                    bbox = img.getbbox()
                    if bbox:
                        img = img.crop(bbox)
                
                sprites.append({
                    "name": f"sprite_{idx}",
                    "image": img,
                    "width": img.width,
                    "height": img.height
                })
                
            except Exception as e:
                print(f"⚠️  Failed to load image {idx}: {e}")
        
        print(f"✅ Loaded {len(sprites)} sprites")
        
        # Simple packing algorithm (shelf packing)
        atlas = Image.new('RGBA', (atlas_size, atlas_size), (0, 0, 0, 0))
        sprite_positions = {}
        
        current_x = padding
        current_y = padding
        row_height = 0
        
        for sprite in sprites:
            # Check if sprite fits in current row
            if current_x + sprite["width"] + padding > atlas_size:
                # Move to next row
                current_x = padding
                current_y += row_height + padding
                row_height = 0
            
            # Check if sprite fits in atlas
            if current_y + sprite["height"] + padding > atlas_size:
                print(f"⚠️  Atlas full, couldn't fit sprite {sprite['name']}")
                break
            
            # Paste sprite onto atlas
            atlas.paste(sprite["image"], (current_x, current_y))
            
            # Store sprite position
            sprite_positions[sprite["name"]] = {
                "x": current_x,
                "y": current_y,
                "width": sprite["width"],
                "height": sprite["height"]
            }
            
            # Update position
            current_x += sprite["width"] + padding
            row_height = max(row_height, sprite["height"])
        
        # Save atlas image to S3
        atlas_buffer = BytesIO()
        atlas.save(atlas_buffer, format='PNG')
        atlas_buffer.seek(0)
        
        s3 = boto3.client('s3', region_name=AWS_REGION)
        atlas_filename = f"game-exports/spritesheets/atlas_{uuid.uuid4()}.png"
        
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=atlas_filename,
            Body=atlas_buffer.getvalue(),
            ContentType='image/png'
        )
        
        atlas_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': atlas_filename},
            ExpiresIn=86400
        )
        
        # Create sprite sheet metadata
        if output_format == "json":
            metadata = {
                "frames": {},
                "meta": {
                    "app": "Workflow API Sprite Sheet Generator",
                    "version": "1.0",
                    "image": atlas_filename,
                    "format": "RGBA8888",
                    "size": {"w": atlas_size, "h": atlas_size},
                    "scale": "1"
                }
            }
            
            for name, pos in sprite_positions.items():
                metadata["frames"][name] = {
                    "frame": {
                        "x": pos["x"],
                        "y": pos["y"],
                        "w": pos["width"],
                        "h": pos["height"]
                    },
                    "rotated": False,
                    "trimmed": trim_transparent,
                    "spriteSourceSize": {
                        "x": 0,
                        "y": 0,
                        "w": pos["width"],
                        "h": pos["height"]
                    },
                    "sourceSize": {
                        "w": pos["width"],
                        "h": pos["height"]
                    }
                }
            
            metadata_content = json.dumps(metadata, indent=2).encode('utf-8')
            
        elif output_format == "plist":
            # Cocos2d plist format
            import plistlib
            metadata = {
                "frames": {},
                "metadata": {
                    "format": 3,
                    "pixelFormat": "RGBA8888",
                    "premultiplyAlpha": False,
                    "textureFileName": atlas_filename,
                    "size": f"{{{atlas_size},{atlas_size}}}"
                }
            }
            
            for name, pos in sprite_positions.items():
                metadata["frames"][name] = {
                    "frame": f"{{{pos['x']},{pos['y']},{pos['width']},{pos['height']}}}",
                    "offset": "{0,0}",
                    "rotated": False,
                    "sourceColorRect": f"{{{0},{0},{pos['width']},{pos['height']}}}",
                    "sourceSize": f"{{{pos['width']},{pos['height']}}}"
                }
            
            metadata_content = plistlib.dumps(metadata, fmt=plistlib.FMT_XML)
        
        else:  # xml
            metadata_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<TextureAtlas imagePath="{atlas_filename}" width="{atlas_size}" height="{atlas_size}">
"""
            for name, pos in sprite_positions.items():
                metadata_content += f'    <sprite n="{name}" x="{pos["x"]}" y="{pos["y"]}" w="{pos["width"]}" h="{pos["height"]}"/>\n'
            
            metadata_content += "</TextureAtlas>"
            metadata_content = metadata_content.encode('utf-8')
        
        # Save metadata
        meta_ext = {"json": "json", "plist": "plist", "xml": "xml"}.get(output_format, "json")
        meta_filename = f"game-exports/spritesheets/atlas_{uuid.uuid4()}.{meta_ext}"
        
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=meta_filename,
            Body=metadata_content,
            ContentType='application/json' if output_format == 'json' else 'application/xml'
        )
        
        meta_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': meta_filename},
            ExpiresIn=86400
        )
        
        print(f"✅ Sprite sheet created successfully")
        print(f"🖼️  Atlas: {atlas_url[:80]}...")
        print(f"📄 Metadata: {meta_url[:80]}...")
        
        return {
            "atlas_url": atlas_url,
            "metadata_url": meta_url,
            "output_format": output_format,
            "atlas_size": atlas_size,
            "sprite_count": len(sprite_positions),
            "sprites": list(sprite_positions.keys()),
            "status": "completed"
        }

    async def execute_export_game_scene(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Export complete game scene with all assets and metadata"""
        
        print(f"🗺️  [SCENE] Exporting complete game scene")
        
        # Get configuration
        scene_format = node.config.get("scene_format", "json")
        include_metadata = node.config.get("include_metadata", ["transforms", "materials"])
        asset_bundling = node.config.get("asset_bundling", "separate")
        coordinate_system = node.config.get("coordinate_system", "right_handed_y_up")
        optimize_assets = node.config.get("optimize_assets", True)
        generate_manifest = node.config.get("generate_manifest", True)
        
        # Collect all assets from previous nodes
        scene_assets = {
            "textures": [],
            "models": [],
            "animations": [],
            "sounds": []
        }
        
        if inputs:
            for node_id, input_data in inputs.items():
                if isinstance(input_data, dict):
                    if "image_url" in input_data:
                        scene_assets["textures"].append({
                            "url": input_data["image_url"],
                            "name": input_data.get("name", f"texture_{len(scene_assets['textures'])}"),
                            "source_node": node_id
                        })
                    if "model_url" in input_data or "glb_url" in input_data:
                        scene_assets["models"].append({
                            "url": input_data.get("model_url") or input_data.get("glb_url"),
                            "name": input_data.get("name", f"model_{len(scene_assets['models'])}"),
                            "source_node": node_id
                        })
                    if "video_url" in input_data:
                        scene_assets["animations"].append({
                            "url": input_data["video_url"],
                            "name": input_data.get("name", f"animation_{len(scene_assets['animations'])}"),
                            "source_node": node_id
                        })
        
        total_assets = sum(len(assets) for assets in scene_assets.values())
        print(f"📦 Collected {total_assets} assets for scene export")
        
        # Create scene structure
        scene_data = {
            "version": "1.0",
            "format": scene_format,
            "coordinate_system": coordinate_system,
            "scene": {
                "name": "WorkflowGeneratedScene",
                "root": {
                    "name": "Root",
                    "type": "Node",
                    "transform": {
                        "position": [0, 0, 0],
                        "rotation": [0, 0, 0],
                        "scale": [1, 1, 1]
                    },
                    "children": []
                }
            },
            "assets": scene_assets
        }
        
        # Add transforms metadata if requested
        if "transforms" in include_metadata:
            for idx, texture in enumerate(scene_assets["textures"]):
                sprite_node = {
                    "name": texture["name"],
                    "type": "Sprite",
                    "asset_ref": texture["url"],
                    "transform": {
                        "position": [idx * 150, 0, 0],
                        "rotation": [0, 0, 0],
                        "scale": [1, 1, 1]
                    }
                }
                scene_data["scene"]["root"]["children"].append(sprite_node)
            
            for idx, model in enumerate(scene_assets["models"]):
                model_node = {
                    "name": model["name"],
                    "type": "Mesh",
                    "asset_ref": model["url"],
                    "transform": {
                        "position": [0, 0, idx * 200],
                        "rotation": [0, 0, 0],
                        "scale": [1, 1, 1]
                    }
                }
                scene_data["scene"]["root"]["children"].append(model_node)
        
        # Add materials metadata if requested
        if "materials" in include_metadata:
            scene_data["materials"] = []
            for texture in scene_assets["textures"]:
                material = {
                    "name": f"{texture['name']}_Material",
                    "shader": "Standard",
                    "properties": {
                        "albedo": texture["url"],
                        "metallic": 0.0,
                        "roughness": 0.5
                    }
                }
                scene_data["materials"].append(material)
        
        # Add lighting metadata if requested
        if "lighting" in include_metadata:
            scene_data["lighting"] = {
                "ambient": {"r": 0.2, "g": 0.2, "b": 0.2},
                "directional_lights": [
                    {
                        "name": "MainLight",
                        "direction": [0.5, -1, 0.5],
                        "color": {"r": 1, "g": 1, "b": 1},
                        "intensity": 1.0
                    }
                ]
            }
        
        # Add camera metadata if requested
        if "cameras" in include_metadata:
            scene_data["cameras"] = [
                {
                    "name": "MainCamera",
                    "type": "perspective",
                    "fov": 60,
                    "near": 0.1,
                    "far": 1000,
                    "transform": {
                        "position": [0, 5, 10],
                        "rotation": [-15, 0, 0],
                        "scale": [1, 1, 1]
                    }
                }
            ]
        
        # Save scene to S3
        s3 = boto3.client('s3', region_name=AWS_REGION)
        scene_filename = f"game-exports/scenes/scene_{uuid.uuid4()}.json"
        
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=scene_filename,
            Body=json.dumps(scene_data, indent=2).encode('utf-8'),
            ContentType='application/json'
        )
        
        scene_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': scene_filename},
            ExpiresIn=86400
        )
        
        # Generate manifest if requested
        manifest_url = None
        if generate_manifest:
            manifest = {
                "scene_file": scene_filename,
                "total_assets": total_assets,
                "asset_breakdown": {
                    "textures": len(scene_assets["textures"]),
                    "models": len(scene_assets["models"]),
                    "animations": len(scene_assets["animations"]),
                    "sounds": len(scene_assets["sounds"])
                },
                "metadata_included": include_metadata,
                "coordinate_system": coordinate_system,
                "optimized": optimize_assets
            }
            
            manifest_filename = f"game-exports/scenes/manifest_{uuid.uuid4()}.json"
            
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=manifest_filename,
                Body=json.dumps(manifest, indent=2).encode('utf-8'),
                ContentType='application/json'
            )
            
            manifest_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': S3_BUCKET, 'Key': manifest_filename},
                ExpiresIn=86400
            )
        
        print(f"✅ Game scene exported successfully")
        print(f"🗺️  Scene: {scene_url[:80]}...")
        if manifest_url:
            print(f"📋 Manifest: {manifest_url[:80]}...")
        
        result = {
            "scene_url": scene_url,
            "scene_format": scene_format,
            "coordinate_system": coordinate_system,
            "total_assets": total_assets,
            "textures": len(scene_assets["textures"]),
            "models": len(scene_assets["models"]),
            "animations": len(scene_assets["animations"]),
            "metadata_included": include_metadata,
            "status": "completed"
        }
        
        if manifest_url:
            result["manifest_url"] = manifest_url
        
        return result


async def run_workflow_execution(execution_id: str, nodes: List[WorkflowNode], edges: List[WorkflowEdge]):
    """Background task to execute workflow"""
    
    execution = get_execution_from_db(execution_id)
    if not execution:
        logger.error(f"❌ Execution {execution_id} not found")
        return
    
    execution["status"] = "running"
    update_execution_in_db(execution_id, {"status": "running"})
    
    start_time = time.time()
    engine = WorkflowEngine(base_url=ANIMATION_API_URL)
    
    # Initialize asset tracking
    all_assets = {
        "images": [],
        "videos": [],
        "models": [],
        "prompts": []
    }
    
    try:
        # Build execution order
        execution_order = engine.build_execution_order(nodes, edges)
        total_nodes = len(execution_order)
        
        print(f"\n{'='*80}")
        print(f"🚀 WORKFLOW EXECUTION: {execution_id}")
        print(f"{'='*80}")
        print(f"📋 Execution order: {' → '.join(execution_order)}")
        print(f"📊 Total nodes: {total_nodes}")
        
        # Execute nodes in order
        node_map = {node.id: node for node in nodes}
        executed_results = {}
        
        for idx, node_id in enumerate(execution_order):
            node = node_map[node_id]
            execution["current_node"] = node_id
            execution["progress"] = int((idx / total_nodes) * 100)
            
            # Update progress in database
            update_execution_in_db(execution_id, {
                "current_node": node_id,
                "progress": execution["progress"]
            })
            
            print(f"\n{'─'*80}")
            print(f"▶️  Executing node {idx + 1}/{total_nodes}: {node_id} ({node.type})")
            print(f"{'─'*80}")
            
            # Get inputs from previous nodes
            node_inputs = engine.get_node_inputs(node_id, edges, executed_results)
            
            # Execute node
            result = await engine.execute_node(node, node_inputs)
            executed_results[node_id] = result
            
            # Extract assets from result
            node_assets = extract_assets_from_output(result.output, node.type)
            result.assets = node_assets
            
            # Categorize assets
            for key, url in node_assets.items():
                if "image" in key.lower() or "gemini" in key.lower():
                    if url not in all_assets["images"]:
                        all_assets["images"].append(url)
                elif "video" in key.lower() or "animation" in key.lower():
                    if url not in all_assets["videos"]:
                        all_assets["videos"].append(url)
                elif "model" in key.lower() or "glb" in key.lower():
                    if url not in all_assets["models"]:
                        all_assets["models"].append(url)
            
            # Track prompts
            if "prompt" in result.output or "enhanced_prompt" in result.output:
                prompt_text = result.output.get("enhanced_prompt") or result.output.get("prompt")
                if prompt_text:
                    all_assets["prompts"].append({
                        "step": idx + 1,
                        "node_id": node_id,
                        "prompt": prompt_text
                    })
            
            # Create step record
            step_record = {
                "step": idx + 1,
                "node_id": node_id,
                "node_type": node.type,
                "status": result.status,
                "timestamp": result.timestamp,
                "execution_time": result.execution_time,
                "assets": node_assets,
                "output": result.output
            }
            
            # Update execution status
            execution["completed_nodes"].append(node_id)
            execution["node_results"].append(result.model_dump())
            execution["assets_by_step"].append(step_record)
            execution["all_assets"] = all_assets
            
            # Save step to database
            update_execution_in_db(execution_id, {
                "completed_nodes": execution["completed_nodes"],
                "node_results": execution["node_results"],
                "assets_by_step": execution["assets_by_step"],
                "all_assets": execution["all_assets"]
            })
            
            if result.status == "success":
                print(f"✅ Node completed in {result.execution_time:.2f}s")
                if node_assets:
                    print(f"📦 Assets generated: {list(node_assets.keys())}")
            else:
                print(f"❌ Node failed: {result.error}")
                # Continue execution even if node fails (optional: could stop here)
        
        # Mark as completed
        execution["status"] = "completed"
        execution["progress"] = 100
        execution["completed_at"] = datetime.now(tz=timezone.utc).isoformat()
        execution["total_execution_time"] = time.time() - start_time
        
        # Set final output from last node
        if execution_order:
            last_node_id = execution_order[-1]
            if last_node_id in executed_results:
                execution["final_output"] = executed_results[last_node_id].output
        
        # Final database update
        update_execution_in_db(execution_id, {
            "status": "completed",
            "progress": 100,
            "completed_at": execution["completed_at"],
            "total_execution_time": execution["total_execution_time"],
            "final_output": execution["final_output"],
            "all_assets": execution["all_assets"]
        })
        
        print(f"\n{'='*80}")
        print(f"✅ WORKFLOW COMPLETED")
        print(f"{'='*80}")
        print(f"⏱️  Total time: {execution['total_execution_time']:.2f}s")
        print(f"✅ Completed nodes: {len(execution['completed_nodes'])}/{total_nodes}")
        print(f"📦 Total assets generated:")
        print(f"   - Images: {len(all_assets['images'])}")
        print(f"   - Videos: {len(all_assets['videos'])}")
        print(f"   - 3D Models: {len(all_assets['models'])}")
        
    except Exception as e:
        execution["status"] = "failed"
        execution["error"] = str(e)
        execution["completed_at"] = datetime.now(tz=timezone.utc).isoformat()
        execution["total_execution_time"] = time.time() - start_time
        
        update_execution_in_db(execution_id, {
            "status": "failed",
            "error": execution["error"],
            "completed_at": execution["completed_at"],
            "total_execution_time": execution["total_execution_time"]
        })
        
        print(f"\n{'='*80}")
        print(f"❌ WORKFLOW FAILED: {e}")
        print(f"{'='*80}")

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
import json
import base64
import tempfile
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

# Replicate API configuration
REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY")

# Animation API base URL configuration
ANIMATION_API_URL = os.getenv("ANIMATION_API_URL", "http://localhost:8000")

# Debug: Print API key status
print(f"🔧 Workflow API Environment check:")
print(f"   REPLICATE_API_KEY: {'SET (length: ' + str(len(REPLICATE_API_KEY)) + ')' if REPLICATE_API_KEY else 'NOT SET'}")
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
        mongo_client = MongoClient(
            mongo_url,
            serverSelectionTimeoutMS=2000,
            connectTimeoutMS=3000,
            socketTimeoutMS=5000,
            maxPoolSize=10,
            retryWrites=True
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
                graph[edge.source].append(edge.target)
                if edge.target in in_degree:
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
                
            elif node.type == "save_result":
                result.output = await self.execute_save_result(node, inputs)
                
            elif node.type == "fusion":
                result.output = await self.execute_fusion(node, inputs)
                
            elif node.type == "sketch_board":
                result.output = await self.execute_sketch_board(node, inputs)
                
            elif node.type == "create_logo":
                result.output = await self.execute_create_logo(node, inputs)
                
            elif node.type == "remove_background":
                result.output = await self.execute_remove_background(node, inputs)
                
            elif node.type == "add_sound_to_image":
                result.output = await self.execute_add_sound_to_image(node, inputs)
                
            elif node.type == "dating_app_enhancement":
                result.output = await self.execute_dating_app_enhancement(node, inputs)
                
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
            print(f"� Detected {len(detected_objects)} objects with avg confidence: {confidence_stats.get('average_confidence', 0):.2f}")
            print(f"⏱️ Processing time: {analysis_metadata.get('processing_time', {}).get('total', 0):.2f}s")
            
            # Create summary of high-confidence objects (using converged confidence)
            high_confidence_objects = [
                obj for obj in detected_objects 
                if obj.get("converged_confidence", 0) >= 0.7
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

            # Enhanced prompt to indicate we want to use the reference
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
                    prompt = input_data["enhanced_prompt"]
                    break
        
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
        
        # Get prompt from previous node or config
        prompt = node.config.get("prompt", "")
        if not prompt and inputs:
            for input_data in inputs.values():
                if isinstance(input_data, dict) and "enhanced_prompt" in input_data:
                    prompt = input_data["enhanced_prompt"]
                    break
                elif isinstance(input_data, dict) and "prompt" in input_data:
                    prompt = input_data["prompt"]
                    break
        
        if not prompt:
            raise ValueError("No prompt provided for WAN 2.2 i2v-fast animation")
        
        # Get configuration
        resolution = node.config.get("resolution", "480p")
        go_fast = node.config.get("go_fast", "true").lower() == "true"
        num_frames = int(node.config.get("num_frames", "81"))
        sample_shift = int(node.config.get("sample_shift", "12"))
        frames_per_second = int(node.config.get("frames_per_second", "16"))
        interpolate_output = node.config.get("interpolate_output", "true").lower() == "true"
        
        print(f"🎬 [WORKFLOW] WAN 2.2 i2v-fast animation")
        print(f"🖼️  Image: {image_url[:80]}...")
        print(f"📝 Prompt: {prompt[:100]}...")
        print(f"🎯 Resolution: {resolution} ({'$0.05' if resolution == '480p' else '$0.11'})")
        
        # Call the endpoint
        response = requests.post(
            f"{self.base_url}/animate/wan-fast",
            json={
                "image": image_url,
                "prompt": prompt,
                "resolution": resolution,
                "go_fast": go_fast,
                "num_frames": num_frames,
                "sample_shift": sample_shift,
                "frames_per_second": frames_per_second,
                "interpolate_output": interpolate_output
            },
            timeout=30  # Initial response is immediate
        )
        
        response.raise_for_status()
        result = response.json()
        
        job_id = result.get("job_id")
        
        print(f"⏳ Job started: {job_id}")
        print(f"💰 Estimated cost: {result.get('estimated_cost')}")
        
        # Poll for completion
        max_retries = 60  # 5 minutes max (60 * 5 seconds)
        for i in range(max_retries):
            time.sleep(5)
            
            status_response = requests.get(f"{self.base_url}/status/{job_id}")
            status_response.raise_for_status()
            status_data = status_response.json()
            
            if status_data.get("status") == "completed":
                result_data = status_data.get("result", {})
                video_url = result_data.get("video_url") or result_data.get("video_s3_url")
                
                print(f"✅ WAN 2.2 i2v-fast animation completed!")
                print(f"💰 Actual cost: ${result_data.get('actual_cost', 0):.4f}")
                
                return {
                    "job_id": job_id,
                    "video_url": video_url,
                    "video_s3_url": result_data.get("video_s3_url"),
                    "local_path": result_data.get("local_path"),
                    "image": image_url,
                    "prompt": prompt,
                    "resolution": resolution,
                    "actual_cost": result_data.get("actual_cost"),
                    "settings": result_data.get("settings", {}),
                    "status": "completed"
                }
            elif status_data.get("status") == "failed":
                error_msg = status_data.get("message", "Unknown error")
                raise Exception(f"WAN 2.2 i2v-fast animation failed: {error_msg}")
            
            # Log progress
            if i % 6 == 0:  # Every 30 seconds
                progress = status_data.get("progress", 0)
                print(f"⏳ Progress: {progress}%")
        
        raise Exception("WAN 2.2 i2v-fast animation timed out after 5 minutes")
    
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
            # Debug: print what we received
            print(f"DEBUG: No image URL found. Inputs received: {inputs}")
            raise ValueError("No image provided for 3D optimization")
        
        # Get optional custom prompt from config
        prompt = node.config.get("prompt") or node.config.get("optimization_prompt")
        
        print(f"🎯 [WORKFLOW] Optimizing image for 3D generation with /optimize/3d endpoint")
        print(f"🖼️  Image URL: {image_url[:100]}...")
        if prompt:
            print(f"📝 Custom prompt: {prompt}")
        
        # Prepare request data
        data = {"image_url": image_url}
        if prompt:
            data["prompt"] = prompt
        
        try:
            response = requests.post(
                f"{self.base_url}/optimize/3d",
                data=data,
                timeout=300
            )
            
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
                "status": "completed",
                "message": result.get("message"),
                "next_steps": result.get("next_steps", [])
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
    
    async def execute_save_result(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute save_result node - saves all workflow results to database with job tagging"""
        
        # Get configuration
        result_name = node.config.get("result_name", "workflow_result")
        description = node.config.get("description", "")
        tags = node.config.get("tags", [])
        
        # Generate a unique result ID
        result_id = str(uuid.uuid4())
        
        print(f"💾 [WORKFLOW] Saving workflow results to database")
        print(f"🏷️  Result ID: {result_id}")
        print(f"📝 Result name: {result_name}")
        
        # Collect all data from previous nodes
        collected_data = {
            "result_id": result_id,
            "result_name": result_name,
            "description": description,
            "tags": tags if isinstance(tags, list) else [tags] if tags else [],
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "created_at": time.time(),
            "inputs": {}
        }
        
        # Collect all assets from inputs
        all_assets = {
            "images": [],
            "videos": [],
            "models": [],
            "animations": [],
            "prompts": [],
            "analyses": []
        }
        
        # Process each input from previous nodes
        for node_id, input_data in inputs.items():
            if isinstance(input_data, dict):
                collected_data["inputs"][node_id] = input_data
                
                # Extract and categorize assets
                for key, value in input_data.items():
                    if isinstance(value, str):
                        # Image assets
                        if any(k in key.lower() for k in ["image_url", "s3_url", "gemini", "optimized"]):
                            if value and value not in all_assets["images"]:
                                all_assets["images"].append({
                                    "url": value,
                                    "source_node": node_id,
                                    "field": key
                                })
                        # Video/animation assets
                        elif any(k in key.lower() for k in ["video", "animation", "gif"]):
                            if value and value not in [a["url"] for a in all_assets["videos"]]:
                                all_assets["videos"].append({
                                    "url": value,
                                    "source_node": node_id,
                                    "field": key
                                })
                        # 3D model assets
                        elif any(k in key.lower() for k in ["model", "glb", "fbx", "obj"]):
                            if value and value not in [a["url"] for a in all_assets["models"]]:
                                all_assets["models"].append({
                                    "url": value,
                                    "source_node": node_id,
                                    "field": key
                                })
                    
                    # Extract prompts
                    if key in ["prompt", "enhanced_prompt"]:
                        if value:
                            all_assets["prompts"].append({
                                "text": value,
                                "source_node": node_id,
                                "field": key
                            })
                    
                    # Extract analyses
                    if key in ["analysis", "objects_analysis", "full_analysis"]:
                        if value:
                            all_assets["analyses"].append({
                                "text": value,
                                "source_node": node_id,
                                "field": key
                            })
                    
                    # Handle animation_urls arrays
                    if key == "animation_urls" and isinstance(value, list):
                        for idx, url in enumerate(value):
                            if url and url not in [a["url"] for a in all_assets["animations"]]:
                                all_assets["animations"].append({
                                    "url": url,
                                    "source_node": node_id,
                                    "field": f"{key}[{idx}]"
                                })
                    
                    # Handle reference_images arrays (from RAG)
                    if key == "reference_images" and isinstance(value, list):
                        for ref in value:
                            if isinstance(ref, dict) and "path" in ref:
                                all_assets["images"].append({
                                    "url": ref["path"],
                                    "source_node": node_id,
                                    "field": "reference_image",
                                    "score": ref.get("score"),
                                    "rank": ref.get("rank")
                                })
        
        # Add collected assets to result
        collected_data["assets"] = all_assets
        
        # Generate summary statistics
        collected_data["summary"] = {
            "total_images": len(all_assets["images"]),
            "total_videos": len(all_assets["videos"]),
            "total_models": len(all_assets["models"]),
            "total_animations": len(all_assets["animations"]),
            "total_prompts": len(all_assets["prompts"]),
            "total_analyses": len(all_assets["analyses"]),
            "source_node_count": len(inputs)
        }
        
        # Save to MongoDB results collection
        try:
            if use_mongodb:
                results_collection = mongo_db.results
                
                # Create indexes if not exists
                try:
                    results_collection.create_index([("result_id", 1)], unique=True)
                    results_collection.create_index([("created_at", -1)])
                    results_collection.create_index([("result_name", 1)])
                    results_collection.create_index([("tags", 1)])
                except Exception as idx_err:
                    logger.debug(f"Indexes may already exist: {idx_err}")
                
                # Insert result
                results_collection.insert_one(collected_data.copy())
                
                print(f"✅ Results saved to MongoDB 'results' collection")
                print(f"📊 Summary: {collected_data['summary']['total_images']} images, "
                      f"{collected_data['summary']['total_videos']} videos, "
                      f"{collected_data['summary']['total_models']} models")
            else:
                print(f"⚠️ MongoDB not available, results saved in memory only")
        
        except Exception as e:
            logger.error(f"❌ Error saving results to database: {e}")
            print(f"⚠️ Failed to save to database: {e}")
        
        # Return the complete result for the workflow
        return {
            "result_id": result_id,
            "result_name": result_name,
            "description": description,
            "tags": collected_data["tags"],
            "timestamp": collected_data["timestamp"],
            "summary": collected_data["summary"],
            "assets": all_assets,
            "status": "saved",
            "message": f"Saved {collected_data['summary']['source_node_count']} node results with {collected_data['summary']['total_images'] + collected_data['summary']['total_videos'] + collected_data['summary']['total_models']} total assets"
        }
    
    async def execute_fusion(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute fusion node - fuses two images together to create a hybrid"""
        
        # Get images from previous nodes or config
        image1_url = node.config.get("image1_url")
        image2_url = node.config.get("image2_url")
        
        # Try to extract images from previous nodes
        # inputs is a dict where keys are node_ids and values are their outputs
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
            
            # Assign found URLs to image1 and image2 based on node order
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
                f"Connected nodes: {available_nodes}. Make sure two nodes providing images are connected to this fusion node."
            )
        
        # Get fusion configuration
        fusion_style = node.config.get("fusion_style", "blend")  # blend, merge, hybrid
        fusion_strength = float(node.config.get("fusion_strength", "0.5"))  # 0.0 to 1.0
        animation_ids = node.config.get("animation_ids", "")

        # Validate animation IDs if provided
        if animation_ids and not isinstance(animation_ids, str):
            raise ValueError("Invalid animation_ids format. Must be a comma-separated string of IDs.")
        
        height_meters = float(node.config.get("height_meters", "1.75"))
        fps = int(node.config.get("fps", "60"))
        use_retexture = node.config.get("use_retexture", "false").lower() == "true"
        
        print(f"🔀 [WORKFLOW] Fusing two images")
        print(f"🖼️  Image 1: {image1_url[:80]}...")
        print(f"🖼️  Image 2: {image2_url[:80]}...")
        print(f"🎨 Fusion style: {fusion_style}")
        print(f"⚖️  Fusion strength: {fusion_strength}")
        
        try:
            # Handle base64 images or download from URLs
            print(f"📥 Processing images...")
            
            # Process image 1
            if image1_url.startswith("data:") or (not image1_url.startswith("http")):
                # Base64 image
                print(f"🔄 Image 1 is base64, decoding...")
                if image1_url.startswith("data:"):
                    header, b64data = image1_url.split(",", 1)
                else:
                    b64data = image1_url
                img1_bytes = base64.b64decode(b64data)
            else:
                # URL image
                print(f"📥 Downloading image 1...")
                img1_response = requests.get(image1_url, timeout=30)
                img1_response.raise_for_status()
                img1_bytes = img1_response.content
            
            # Process image 2
            if image2_url.startswith("data:") or (not image2_url.startswith("http")):
                # Base64 image
                print(f"🔄 Image 2 is base64, decoding...")
                if image2_url.startswith("data:"):
                    header, b64data = image2_url.split(",", 1)
                else:
                    b64data = image2_url
                img2_bytes = base64.b64decode(b64data)
            else:
                # URL image
                print(f"📥 Downloading image 2...")
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
                'fusion_strength': str(fusion_strength)
            }
            
            print(f"🚀 Calling fusion endpoint...")
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
                        "status": "completed"
                    }
                    
                    # Add fusion image if available
                    if "fusion_image" in result_data:
                        fusion_img = result_data["fusion_image"]
                        fusion_result["fusion_image_url"] = fusion_img.get("s3_url") or fusion_img.get("s3_presigned_url")
                        fusion_result["image_url"] = fusion_result["fusion_image_url"]  # Alias for next nodes
                        fusion_result["s3_url"] = fusion_result["fusion_image_url"]  # Alias for next nodes
                    
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
    
    async def execute_sketch_board(self, node: WorkflowNode, inputs: Dict[str, Any]) -> Dict[str, Any]:
        """Execute sketch_board node - outputs a sketch/drawing image that can be used as reference
        
        Supports:
        - Base64 encoded images (data URI or raw base64) - uploads to S3
        - Image URLs - returns as-is
        - Local file paths - uploads to S3
        - Sketch data from previous nodes
        """
        
        # Get sketch image from config or previous node
        sketch_url = node.config.get("sketch_url") or node.config.get("image_url")
        sketch_path = node.config.get("sketch_path") or node.config.get("image_path")
        sketch_base64 = node.config.get("sketch_data") or node.config.get("sketch_data") or node.config.get("image_upload")
        
        # Try to extract sketch from previous node
        if not sketch_url and not sketch_path and not sketch_base64 and inputs:
            for node_id, input_data in inputs.items():
                if isinstance(input_data, dict):
                    # Check for various sketch/image fields
                    for field in ["sketch_url", "drawing_url", "image_url", "s3_url", "sketch_base64", "image_base64", "image_upload"]:
                        if field in input_data and input_data[field]:
                            if field.endswith("_base64") or field == "image_upload":
                                sketch_base64 = input_data[field]
                            elif field.endswith("_path"):
                                sketch_path = input_data[field]
                            else:
                                sketch_url = input_data[field]
                            print(f"✏️  Found sketch from node '{node_id}' in field '{field}'")
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
                
                # Upload the file to S3 via animation API
                with open(sketch_path, 'rb') as f:
                    files = {'file': (os.path.basename(sketch_path), f, 'image/jpeg')}
                    
                    # Call a simple upload endpoint or use the optimize/3d endpoint
                    # For now, we'll use a direct approach by calling the animation API
                    response = requests.post(
                        f"{self.base_url}/optimize/3d",
                        files={'file': (os.path.basename(sketch_path), open(sketch_path, 'rb'), 'image/jpeg')},
                        data={"prompt": "sketch upload"},
                        timeout=60
                    )
                    
                    if response.status_code == 200:
                        result_data = response.json()
                        s3_url = result_data.get("original_image_s3_url") or result_data.get("optimized_3d_image_s3_url")
                        print(f"✅ Sketch uploaded to S3: {s3_url[:80]}...")
                    else:
                        print(f"⚠️  Failed to upload to S3, using local path")
                        
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
        
        client = replicate.Client(api_token=REPLICATE_API_KEY)
        
        # Get seed configuration
        seed = int(node.config.get("seed", -1))
        
        # Ensure we have a valid prompt
        if not sound_prompt or len(sound_prompt.strip()) < 3:
            sound_prompt = "ambient environmental sounds"
            print(f"⚠️  Using default sound prompt: {sound_prompt}")
        
        try:
            # Call MMAudio API
            output = client.run(
                "hkchengrex/MMAudio:4b9f9c6cb8bb33b891078b8273839ffc1ba4cb3eb3e0b81d1b6e93d80fc61c88",
                input={
                    "image": image_url,
                    "prompt": sound_prompt,
                    "seed": seed
                }
            )
            
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
            local_path = None
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
                    print(f"💾 Saved to: {temp_path}")
                    
                    # Upload to S3
                    try:
                        s3_key = f"sound_generation/{node.node_id}/{temp_filename}"
                        self.s3_client.upload_file(
                            temp_path,
                            self.bucket_name,
                            s3_key,
                            ExtraArgs={'ContentType': 'video/mp4'}
                        )
                        s3_url = f"https://{self.bucket_name}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
                        print(f"☁️  Uploaded to S3: {s3_key}")
                        
                        # Use S3 URL as the final output
                        output_url = s3_url
                        
                    except Exception as e:
                        print(f"⚠️  S3 upload failed: {e}")
                    
                except Exception as e:
                    print(f"⚠️  File download/upload failed: {e}")
            
            # Build result
            result = {
                "video_url": output_url,
                "output_url": output_url,
                "url": output_url,
                "s3_url": s3_url,
                "local_path": local_path,
                "original_image_url": image_url,
                "vision_model": vision_model,
                "image_description": image_description,
                "sound_prompt": sound_prompt,
                "seed": seed,
                "node_type": "add_sound_to_image",
                "status": "completed",
                "message": "Sound added to image successfully"
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


# ========= API ENDPOINTS =========

@app.post("/workflow/create")
async def create_workflow(workflow: WorkflowDefinition):
    """Create and save a workflow definition"""
    
    workflow_id = str(uuid.uuid4())
    
    workflow_data = {
        "workflow_id": workflow_id,
        "name": workflow.name or f"Workflow {workflow_id[:8]}",
        "description": workflow.description,
        "nodes": [node.model_dump() for node in workflow.nodes],
        "edges": [edge.model_dump() for edge in workflow.edges],
    }
    
    save_workflow_to_db(workflow_data)
    
    return {
        "success": True,
        "workflow_id": workflow_id,
        "name": workflow_data["name"],
        "node_count": len(workflow.nodes),
        "edge_count": len(workflow.edges),
        "storage": "mongodb" if use_mongodb else "memory"
    }


@app.get("/workflow/{workflow_id}")
async def get_workflow(workflow_id: str):
    """Get workflow definition"""
    
    workflow = get_workflow_from_db(workflow_id)
    
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    return workflow


@app.get("/workflows")
async def list_workflows():
    """List all workflows"""
    
    workflow_list = list_workflows_from_db()
    
    return {
        "workflows": workflow_list,
        "count": len(workflow_list),
        "storage": "mongodb" if use_mongodb else "memory"
    }


@app.post("/workflow/execute")
async def execute_workflow(request: WorkflowExecuteRequest, background_tasks: BackgroundTasks):
    """Execute a workflow (either saved or ad-hoc)"""
    
    # Get workflow definition
    if request.workflow_id:
        workflow_def = get_workflow_from_db(request.workflow_id)
        if not workflow_def:
            raise HTTPException(status_code=404, detail="Workflow not found")
        nodes = [WorkflowNode(**n) for n in workflow_def["nodes"]]
        edges = [WorkflowEdge(**e) for e in workflow_def["edges"]]
    elif request.nodes and request.edges:
        nodes = request.nodes
        edges = request.edges
    else:
        raise HTTPException(
            status_code=400, 
            detail="Either workflow_id or (nodes + edges) must be provided"
        )
    
    # Create execution record
    execution_id = str(uuid.uuid4())
    
    execution_status = WorkflowExecutionStatus(
        execution_id=execution_id,
        workflow_id=request.workflow_id,
        status="queued",
        started_at=datetime.now(tz=timezone.utc).isoformat()
    )
    
    execution_data = execution_status.model_dump()
    save_execution_to_db(execution_data)
    
    # Start background execution
    background_tasks.add_task(run_workflow_execution, execution_id, nodes, edges)
    
    return {
        "success": True,
        "execution_id": execution_id,
        "status": "queued",
        "message": "Workflow execution started",
        "storage": "mongodb" if use_mongodb else "memory"
    }


@app.get("/workflow/execution/{execution_id}")
async def get_execution_status(execution_id: str):
    """Get workflow execution status"""
    
    execution = get_execution_from_db(execution_id)
    
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    
    return execution


@app.get("/workflow/executions")
async def list_executions(workflow_id: Optional[str] = None):
    """List all workflow executions"""
    
    execution_list = list_executions_from_db(workflow_id)
    
    return {
        "executions": execution_list,
        "count": len(execution_list),
        "storage": "mongodb" if use_mongodb else "memory"
    }


@app.get("/workflow/result/{result_id}")
async def get_saved_result(result_id: str):
    """Get a saved workflow result by result_id"""
    
    if not use_mongodb:
        raise HTTPException(status_code=503, detail="Database not available")
    
    try:
        result = mongo_db.results.find_one({"result_id": result_id}, {"_id": 0})
        
        if not result:
            raise HTTPException(status_code=404, detail="Result not found")
        
        return result
        
    except Exception as e:
        logger.error(f"Error retrieving result: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve result: {str(e)}")


@app.get("/workflow/results")
async def list_saved_results(
    tags: Optional[str] = None,
    result_name: Optional[str] = None,
    limit: int = 50,
    skip: int = 0
):
    """List saved workflow results with optional filtering
    
    Args:
        tags: Comma-separated tags to filter by (OR logic)
        result_name: Filter by result name (partial match)
        limit: Maximum number of results to return (default: 50)
        skip: Number of results to skip for pagination (default: 0)
    """
    
    if not use_mongodb:
        raise HTTPException(status_code=503, detail="Database not available")
    
    try:
        query = {}
        
        # Filter by tags
        if tags:
            tag_list = [t.strip() for t in tags.split(",")]
            query["tags"] = {"$in": tag_list}
        
        # Filter by result name (case-insensitive partial match)
        if result_name:
            query["result_name"] = {"$regex": result_name, "$options": "i"}
        
        # Query with pagination
        results = list(
            mongo_db.results
            .find(query, {"_id": 0})
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        
        # Get total count
        total_count = mongo_db.results.count_documents(query)
        
        return {
            "results": results,
            "count": len(results),
            "total_count": total_count,
            "limit": limit,
            "skip": skip,
            "has_more": (skip + len(results)) < total_count,
            "filters": {
                "tags": tag_list if tags else None,
                "result_name": result_name
            }
        }
        
    except Exception as e:
        logger.error(f"Error listing results: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list results: {str(e)}")


@app.delete("/workflow/result/{result_id}")
async def delete_saved_result(result_id: str):
    """Delete a saved workflow result"""
    
    if not use_mongodb:
        raise HTTPException(status_code=503, detail="Database not available")
    
    try:
        result = mongo_db.results.delete_one({"result_id": result_id})
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Result not found")
        
        return {
            "success": True,
            "result_id": result_id,
            "message": "Result deleted successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting result: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete result: {str(e)}")


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


@app.get("/")
async def root():
    return {
        "service": "Workflow Orchestration API",
        "version": "1.0.0",
        "endpoints": {
            "create_workflow": "POST /workflow/create",
            "execute_workflow": "POST /workflow/execute",
            "get_execution": "GET /workflow/execution/{execution_id}",
            "list_workflows": "GET /workflows",
            "list_executions": "GET /workflow/executions",
            "get_saved_result": "GET /workflow/result/{result_id}",
            "list_saved_results": "GET /workflow/results?tags=tag1,tag2&result_name=name&limit=50&skip=0",
            "delete_saved_result": "DELETE /workflow/result/{result_id}"
        },
        "supported_node_types": {
            "text_prompt": "Simple text input node",
            "enhance_prompt": "Enhance prompt with Segmind Bria API",
            "enhance_prompt_ollama": "Enhance prompt with local Ollama",
            "enhance_prompt_gnokit": "🚀 NEW: Enhance prompt with gnokit/improve-prompt Ollama model (advanced)",
            "analyze_image_ollama": "Analyze image with Llama 3.2 Vision",
            "analyze_image": "Analyze image with YOLOv8 vision (detects animatable objects)",
            "generate_image": "Generate image with Gemini (text-only, no RAG references)",
            "generate_rag": "🔍 Generate image with RAG (Retrieval-Augmented Generation using reference images)",
            "optimize_3d": "🎯 Optimize any image for 3D model generation",
            "generate_3d": "Generate full 3D model from image",
            "animate_luma": "Animate with Luma Ray 2",
            "animate_easy": "Animate with Segmind Easy Animate",
            "animate_wan": "Animate with WAN 2.2",
            "save_result": "💾 Save all workflow results to database with tagging",
            "fusion": "🔀 Fuse two images together to create a hybrid (blend, merge, or hybrid styles)",
            "dating_app_enhancement": "❤️ Enhance dating profile photos (preserve identity, improve style)"
        },
        "new_features": {
            "enhance_prompt_gnokit_node": {
                "description": "🚀 Advanced prompt enhancement using gnokit/improve-prompt Ollama model",
                "features": [
                    "Uses specialized gnokit/improve-prompt model trained for prompt improvement",
                    "Direct integration with local Ollama API",
                    "No external API dependencies - completely local processing",
                    "Configurable temperature for creativity control",
                    "Preserves original prompt option for comparison",
                    "Optimized for image generation prompts",
                    "Better contextual understanding than generic models"
                ],
                "config_options": {
                    "prompt": "Text prompt to enhance (can be from previous node)",
                    "temperature": "Creativity level (0.0-1.0, default: 0.7)",
                    "preserve_original": "Keep original prompt in output (true/false, default: false)",
                    "ollama_url": "Ollama API URL (default: http://localhost:11434)"
                },
                "requirements": {
                    "ollama": "Ollama must be installed and running",
                    "model": "gnokit/improve-prompt model must be pulled",
                    "installation_steps": [
                        "1. Install Ollama from https://ollama.ai",
                        "2. Pull the model: ollama pull gnokit/improve-prompt",
                        "3. Verify: ollama list (should show gnokit/improve-prompt)"
                    ]
                },
                "example_usage": {
                    "basic": {
                        "description": "Simple prompt enhancement",
                        "workflow": [
                            {"type": "text_prompt", "config": {"prompt": "ancient library, warm light, dust motes"}},
                            {"type": "enhance_prompt_gnokit", "config": {"temperature": "0.7"}}
                        ]
                    },
                    "with_rag": {
                        "description": "Enhanced prompt for RAG image generation",
                        "workflow": [
                            {"type": "text_prompt", "config": {"prompt": "cyberpunk street"}},
                            {"type": "enhance_prompt_gnokit", "config": {"temperature": "0.8"}},
                            {"type": "generate_rag", "config": {"k": 5}}
                        ]
                    },
                    "comparison": {
                        "description": "Compare original and enhanced prompts",
                        "workflow": [
                            {"type": "text_prompt", "config": {"prompt": "sunset over mountains"}},
                            {"type": "enhance_prompt_gnokit", "config": {
                                "temperature": "0.7",
                                "preserve_original": "true"
                            }}
                        ],
                        "note": "Output includes both original_prompt and enhanced_prompt fields"
                    }
                },
                "output": {
                    "enhanced_prompt": "The improved, more detailed prompt",
                    "original_prompt": "Original prompt (if preserve_original=true)",
                    "model": "Always 'gnokit/improve-prompt'",
                    "temperature": "Temperature value used",
                    "processing_time": "Time taken in seconds"
                },
                "comparison_with_other_enhancers": {
                    "enhance_prompt": {
                        "description": "Uses Segmind Bria API (cloud-based)",
                        "pros": ["No local setup", "Fast"],
                        "cons": ["Requires API key", "External dependency", "May have rate limits"]
                    },
                    "enhance_prompt_ollama": {
                        "description": "Uses configurable Ollama models (mistral, etc.)",
                        "pros": ["Local processing", "Multiple model choices"],
                        "cons": ["Generic models not specialized for prompts"]
                    },
                    "enhance_prompt_gnokit": {
                        "description": "Uses gnokit/improve-prompt (specialized)",
                        "pros": ["Local processing", "Specialized for prompt improvement", "Better results for image generation"],
                        "cons": ["Requires model download (~4GB)"]
                    }
                },
                "typical_improvements": {
                    "input": "ancient library, warm light, dust motes",
                    "output_example": "A magnificent ancient library bathed in warm, golden light streaming through tall arched windows, illuminating countless leather-bound books on towering wooden shelves. Countless dust motes dance gracefully in the sunbeams, creating an ethereal atmosphere of timeless knowledge and scholarly tranquility.",
                    "enhancements": [
                        "Adds descriptive adjectives",
                        "Expands scene details",
                        "Improves composition guidance",
                        "Enhances atmospheric elements",
                        "Maintains original intent"
                    ]
                },
                "error_handling": {
                    "ollama_not_running": {
                        "error": "Cannot connect to Ollama",
                        "solution": "Start Ollama service: ollama serve"
                    },
                    "model_not_found": {
                        "error": "Model 'gnokit/improve-prompt' not found",
                        "solution": "Pull the model: ollama pull gnokit/improve-prompt"
                    },
                    "empty_response": {
                        "behavior": "Falls back to original prompt",
                        "warning": "Check model is properly loaded"
                    }
                },
                "api_details": {
                    "endpoint": "POST {ollama_url}/api/generate",
                    "method": "Direct Ollama API call (not through animation API)",
                    "request_format": {
                        "model": "gnokit/improve-prompt",
                        "prompt": "user's prompt",
                        "options": {"temperature": 0.7},
                        "stream": False
                    },
                    "response_format": {
                        "response": "enhanced prompt text",
                        "total_duration": "processing time in nanoseconds"
                    }
                }
            },
            "save_result_node": {
                "description": "💾 Save all workflow results to database with comprehensive tagging and asset organization",
                "features": [
                    "Collects all outputs from previous workflow nodes",
                    "Organizes assets by type (images, videos, models, animations)",
                    "Tags results for easy retrieval and filtering",
                    "Generates summary statistics of collected assets",
                    "Stores in MongoDB 'results' collection with indexes",
                    "Preserves source node information for each asset",
                    "Supports custom result names and descriptions",
                    "Enables batch querying by tags or result name"
                ],
                "config_options": {
                    "result_name": "Name for this result set (default: 'workflow_result')",
                    "description": "Optional description of the workflow results",
                    "tags": "Array of tags for categorization (e.g., ['production', 'character', 'v1.0'])"
                },
                "collected_data": {
                    "inputs": "All outputs from previous nodes, keyed by node_id",
                    "assets": {
                        "images": "All image URLs with source node and field information",
                        "videos": "All video/animation URLs with metadata",
                        "models": "All 3D model URLs (GLB, FBX, OBJ)",
                        "animations": "Animation URLs from animation nodes",
                        "prompts": "All prompts used throughout workflow",
                        "analyses": "All AI analyses performed"
                    },
                    "summary": "Statistics including total counts of each asset type"
                },
                "example_usage": {
                    "basic": {
                        "description": "Save results at end of workflow",
                        "workflow": [
                            {"type": "text_prompt", "config": {"prompt": "A robot"}},
                            {"type": "generate_rag", "config": {"k": 3}},
                            {"type": "generate_3d", "config": {}},
                            {"type": "save_result", "config": {
                                "result_name": "Robot Model v1",
                                "description": "3D robot generated from RAG image",
                                "tags": ["robot", "3d-model", "production"]
                            }}
                        ]
                    },
                    "multi_stage": {
                        "description": "Save intermediate results at multiple stages",
                        "workflow": [
                            {"type": "text_prompt", "config": {"prompt": "Character concept"}},
                            {"type": "generate_rag", "config": {"k": 5}},
                            {"type": "save_result", "config": {
                                "result_name": "Concept Art",
                                "tags": ["concept", "stage1"]
                            }},
                            {"type": "optimize_3d", "config": {}},
                            {"type": "generate_3d", "config": {}},
                            {"type": "save_result", "config": {
                                "result_name": "Final 3D Model",
                                "tags": ["final", "stage2", "3d"]
                            }}
                        ],
                        "note": "Multiple save_result nodes allow tracking workflow stages"
                    }
                },
                "output": {
                    "result_id": "Unique UUID for this saved result",
                    "result_name": "Name provided in config",
                    "description": "Description provided in config",
                    "tags": "Array of tags for filtering",
                    "timestamp": "ISO 8601 timestamp of save",
                    "summary": {
                        "total_images": "Count of image assets",
                        "total_videos": "Count of video assets",
                        "total_models": "Count of 3D model assets",
                        "total_animations": "Count of animation assets",
                        "total_prompts": "Count of prompts",
                        "total_analyses": "Count of analyses",
                        "source_node_count": "Number of nodes that contributed data"
                    },
                    "assets": "Complete asset catalog with metadata",
                    "status": "Always 'saved' on success",
                    "message": "Summary of saved data"
                },
                "api_endpoints": {
                    "retrieve_by_id": {
                        "endpoint": "GET /workflow/result/{result_id}",
                        "description": "Get complete saved result by result_id",
                        "returns": "Full result object with all assets and metadata"
                    },
                    "list_results": {
                        "endpoint": "GET /workflow/results",
                        "description": "List saved results with filtering and pagination",
                        "parameters": {
                            "tags": "Comma-separated tags (OR logic): ?tags=production,character",
                            "result_name": "Partial name match: ?result_name=robot",
                            "limit": "Max results per page (default: 50)",
                            "skip": "Pagination offset (default: 0)"
                        },
                        "returns": {
                            "results": "Array of matching results",
                            "count": "Number of results in this page",
                            "total_count": "Total matching results",
                            "has_more": "Boolean indicating more results available"
                        }
                    },
                    "delete_result": {
                        "endpoint": "DELETE /workflow/result/{result_id}",
                        "description": "Delete a saved result",
                        "returns": "Success confirmation"
                    }
                },
                "use_cases": {
                    "asset_library": "Build searchable library of generated assets",
                    "version_control": "Track different versions with tags (v1.0, v2.0, etc.)",
                    "project_organization": "Tag by project, client, or asset type",
                    "batch_operations": "Retrieve all results for a specific tag for batch processing",
                    "audit_trail": "Maintain complete history of workflow outputs",
                    "client_delivery": "Package results for client with all metadata"
                },
                "database_structure": {
                    "collection": "results",
                    "indexes": [
                        "result_id (unique)",
                        "created_at (descending)",
                        "result_name",
                        "tags (array index)"
                    ],
                    "note": "Requires MongoDB connection (USE_MONGODB=true)"
                }
            },
            "fusion_node": {
                "description": "🔀 Fuse two images together to create a hybrid image with blend, merge, or hybrid styles",
                "features": [
                    "Combines two images into a seamless hybrid",
                    "Three fusion styles: blend (smooth), merge (distinct features), hybrid (AI-guided)",
                    "Adjustable fusion strength (0.0 to 1.0)",
                    "Preserves important features from both source images",
                    "Optional 3D model generation from fused result",
                    "Optional animation rigging with configurable animations",
                    "Returns detailed fusion analysis and metadata"
                ],
                "config_options": {
                    "image1_url": "URL of first image (can be from previous node)",
                    "image2_url": "URL of second image (can be from previous node)",
                    "fusion_style": "Fusion approach: 'blend' (default), 'merge', or 'hybrid'",
                    "fusion_strength": "Balance between images (0.0-1.0, default: 0.5)",
                    "animation_ids": "Comma-separated animation IDs for rigging (default: empty/none)",
                    "height_meters": "Character height for rigging (default: 1.75)",
                    "fps": "Animation frames per second (default: 60)",
                    "use_retexture": "Apply retexturing to result (default: false)"
                },
                "input_sources": {
                    "images_from_config": "Specify image1_url and image2_url directly in node config",
                    "images_from_nodes": "Automatically extracts first two image URLs from previous nodes",
                    "supported_fields": "Checks: image_url, s3_url, gemini_generated_url, optimized_3d_image_s3_url"
                },
                "fusion_styles": {
                    "blend": {
                        "description": "Smooth transition between images with weighted averaging",
                        "use_case": "When you want a gentle combination of both images",
                        "example": "Blend a human face with an animal face for creature design"
                    },
                    "merge": {
                        "description": "Preserves distinct features from both images",
                        "use_case": "When you want recognizable elements from both sources",
                        "example": "Merge architectural styles from two buildings"
                    },
                    "hybrid": {
                        "description": "AI-guided fusion that intelligently combines features",
                        "use_case": "When you want the AI to determine best combination",
                        "example": "Create fantasy creature from two different animals"
                    }
                },
                "example_usage": {
                    "basic_fusion": {
                        "description": "Fuse two generated images with default blend style",
                        "workflow": [
                            {"type": "text_prompt", "config": {"prompt": "phoenix bird"}},
                            {"type": "generate_image", "config": {}},
                            {"type": "text_prompt", "config": {"prompt": "dragon"}},
                            {"type": "generate_image", "config": {}},
                            {"type": "fusion", "config": {"fusion_style": "blend", "fusion_strength": "0.5"}}
                        ]
                    },
                    "custom_fusion": {
                        "description": "Fuse with specific images and hybrid style",
                        "workflow": [
                            {"type": "fusion", "config": {
                                "image1_url": "s3://bucket/image1.jpg",
                                "image2_url": "s3://bucket/image2.jpg",
                                "fusion_style": "hybrid",
                                "fusion_strength": "0.7"
                            }}
                        ]
                    },
                    "fusion_with_animation": {
                        "description": "Fuse two images and create animated 3D model",
                        "workflow": [
                            {"type": "text_prompt", "config": {"prompt": "warrior"}},
                            {"type": "generate_rag", "config": {"k": 3}},
                            {"type": "text_prompt", "config": {"prompt": "wizard"}},
                            {"type": "generate_rag", "config": {"k": 3}},
                            {"type": "fusion", "config": {
                                "fusion_style": "merge",
                                "fusion_strength": "0.6",
                                "animation_ids": "106,30,55",
                                "use_retexture": "true"
                            }}
                        ],
                        "note": "Creates hybrid character with animations"
                    },
                    "fusion_and_save": {
                        "description": "Fuse images and save results with tags",
                        "workflow": [
                            {"type": "generate_image", "config": {"prompt": "robot"}},
                            {"type": "generate_image", "config": {"prompt": "vehicle"}},
                            {"type": "fusion", "config": {"fusion_style": "hybrid"}},
                            {"type": "save_result", "config": {
                                "result_name": "robot_vehicle_hybrid",
                                "tags": ["fusion", "robot", "vehicle", "concept"]
                            }}
                        ]
                    }
                },
                "output": {
                    "job_id": "Unique tracking ID for fusion job",
                    "job_type": "Always 'fusion'",
                    "fusion_image_url": "Primary URL to fused image",
                    "image_url": "Alias for fusion_image_url (for next nodes)",
                    "s3_url": "Alias for fusion_image_url (for next nodes)",
                    "fusion_style": "Style used for fusion",
                    "fusion_strength": "Strength value used",
                    "source_images": "Metadata about both source images",
                    "model_url": "3D model URL (if generated)",
                    "glb_url": "Alias for model_url",
                    "animation_urls": "Array of animation URLs (if rigging enabled)",
                    "analysis": "AI analysis of fusion result",
                    "fusion_prompt": "Generated description of fusion",
                    "status": "Processing status (completed/failed)"
                },
                "fusion_strength_guide": {
                    "0.0_to_0.3": "Heavily favors image 1 with subtle influence from image 2",
                    "0.4_to_0.6": "Balanced fusion with equal contribution from both images",
                    "0.7_to_1.0": "Heavily favors image 2 with subtle influence from image 1"
                },
                "processing_time": {
                    "typical": "5-10 minutes for fusion without rigging",
                    "with_animation": "10-20 minutes when animation_ids specified",
                    "factors": "Depends on image complexity, fusion style, and server load"
                },
                "use_cases": {
                    "character_design": "Combine different character traits or species",
                    "concept_art": "Explore hybrid design concepts by fusing existing assets",
                    "style_transfer": "Merge content from one image with style from another",
                    "asset_variation": "Create variations by fusing original asset with modifications",
                    "creature_creation": "Design fantasy creatures from real animal combinations",
                    "architecture": "Blend different architectural styles or building elements"
                },
                "requirements": {
                    "animation_api": "Animation API must be running at http://localhost:8000",
                    "image_access": "Both images must be accessible via HTTP/HTTPS URLs",
                    "format": "Supports common image formats (JPG, PNG, WebP)",
                    "timeout": "Fusion jobs timeout after 20 minutes"
                },
                "tips": {
                    "style_selection": "Use 'blend' for organic subjects, 'merge' for architectural/mechanical, 'hybrid' for complex AI decision",
                    "strength_tuning": "Start with 0.5 and adjust based on results",
                    "source_quality": "Higher quality source images produce better fusions",
                    "similar_subjects": "Fusing similar subjects (two faces, two buildings) typically yields better results",
                    "pre_optimization": "Consider using optimize_3d node on source images before fusion for better 3D model generation"
                }
            },
            "generate_rag_node": {
                "description": "🔍 Generate images using RAG (Retrieval-Augmented Generation) with reference images",
                "features": [
                    "Retrieves similar reference images from RAG index using CLIP embeddings",
                    "Passes reference images to Gemini 2.5 Flash for style-aware generation",
                    "Creates images that match both the text prompt and reference styles",
                    "Supports configurable number of reference images (k parameter)",
                    "Falls back to text-only generation if RAG index unavailable",
                    "Returns reference image metadata with similarity scores",
                    "Enables consistent style across workflow generations"
                ],
                "config_options": {
                    "prompt": "Text description for image generation (can be from previous node)",
                    "k": "Number of reference images to retrieve from RAG index (default: 3)",
                    "job_id": "Optional job ID for tracking (auto-generated if not provided)"
                },
                "input_sources": {
                    "prompt_from_node": "Accepts 'enhanced_prompt', 'prompt', or 'analysis' from previous nodes",
                    "prompt_from_config": "Can also specify prompt directly in node config"
                },
                "example_usage": {
                    "basic": {
                        "description": "Simple RAG-based image generation",
                        "workflow": [
                            {"type": "text_prompt", "config": {"prompt": "A cyberpunk cityscape"}},
                            {"type": "generate_rag", "config": {"k": 5}}
                        ]
                    },
                    "with_enhancement": {
                        "description": "Enhance prompt then generate with RAG",
                        "workflow": [
                            {"type": "text_prompt", "config": {"prompt": "fantasy castle"}},
                            {"type": "enhance_prompt_ollama", "config": {"model": "mistral"}},
                            {"type": "generate_rag", "config": {"k": 3}}
                        ]
                    },
                    "with_analysis": {
                        "description": "Analyze uploaded image, then generate similar styled image",
                        "workflow": [
                            {"type": "analyze_image_ollama", "config": {"image_url": "https://..."}},
                            {"type": "generate_rag", "config": {"k": 4}}
                        ],
                        "note": "Uses image analysis as prompt for RAG generation"
                    }
                },
                "output": {
                    "job_id": "Tracking ID for the generation job",
                    "image_url": "Primary URL to generated image",
                    "s3_url": "S3 URL of generated image",
                    "gemini_generated_url": "Alias for generated image URL",
                    "local_path": "Local file path (if available)",
                    "reference_images": "Array of reference images used with metadata",
                    "reference_count": "Number of reference images used",
                    "k": "Requested number of references",
                    "job_type": "Always 'rag_image_generation'",
                    "status": "Generation status (completed/failed)",
                    "prompt": "Original prompt used"
                },
                "reference_metadata": {
                    "structure": "Each reference includes: path, score, rank, ocr, size",
                    "example": {
                        "path": "/path/to/reference.jpg",
                        "score": 0.845,
                        "rank": 1,
                        "ocr": "extracted text",
                        "size": "1024x768"
                    }
                },
                "comparison_with_generate_image": {
                    "generate_image": "Text-only generation without reference images",
                    "generate_rag": "Uses similar reference images to guide style and composition",
                    "use_case_image": "When you need a specific concept without style constraints",
                    "use_case_rag": "When you want to maintain consistent style across generations or match existing assets"
                },
                "requirements": {
                    "environment": "USE_GEMINI=1 and GEMINI_API_KEY must be set",
                    "rag_index": "Optional: FAISS index with CLIP embeddings for reference retrieval",
                    "fallback": "If RAG index unavailable, performs text-only generation automatically"
                }
            },
            "optimize_3d_node": {
                "description": "Transform any image into 3D-model-ready format",
                "features": [
                    "Analyzes image content with AI vision",
                    "Removes/cleans background",
                    "Optimizes camera angle for 3D reconstruction",
                    "Enhances textures for PBR workflows",
                    "Supports custom optimization prompts",
                    "Works with Gemini-generated images or uploaded files",
                    "Automatically skips rigging when used with generate_3d"
                ],
                "config_options": {
                    "image_url": "URL of image to optimize (auto-extracted from previous node)",
                    "prompt": "Optional custom optimization instructions",
                    "optimization_prompt": "Alternative field name for prompt"
                },
                "example_usage": {
                    "description": "Generate image → Optimize for 3D → Create 3D model (no rigging)",
                    "workflow": [
                        {"type": "generate_image", "config": {"prompt": "A fantasy character"}},
                        {"type": "optimize_3d", "config": {"prompt": "Prepare for 3D character modeling"}},
                        {"type": "generate_3d", "config": {}}
                    ],
                    "note": "When generate_3d follows optimize_3d, rigging is automatically skipped for faster processing"
                },
                "output": {
                    "job_id": "Tracking ID for the optimization job",
                    "original_image_s3_url": "S3 URL of original image",
                    "optimized_3d_image_s3_url": "S3 URL of optimized image (ready for 3D)",
                    "image_url": "Alias for optimized URL (for next nodes)",
                    "s3_url": "Alias for optimized URL (for next nodes)",
                    "objects_analysis": "AI analysis of image content",
                    "status": "completed"
                }
            },
            "generate_3d_smart_rigging": {
                "description": "Smart rigging control in generate_3d node",
                "features": [
                    "Automatically detects if previous node was optimize_3d",
                    "Skips rigging when optimize_3d is upstream (faster processing)",
                    "Manual control via skip_rigging config option",
                    "Reduces processing time by ~60% when rigging not needed"
                ],
                "config_options": {
                    "skip_rigging": "Set to 'true' to manually skip rigging and animations",
                    "animation_ids": "Comma-separated animation IDs (leave empty to skip rigging)",
                    "height_meters": "Character height in meters (default: 1.75)",
                    "fps": "Frames per second for animations (default: 60)",
                    "use_retexture": "Enable texture enhancement (default: false)"
                },
                "example_usage": {
                    "auto_skip": "optimize_3d → generate_3d (rigging auto-skipped)",
                    "manual_skip": {"type": "generate_3d", "config": {"skip_rigging": "true"}},
                    "with_rigging": {"type": "generate_3d", "config": {"animation_ids": "106,30,55"}}
                }
            }
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)

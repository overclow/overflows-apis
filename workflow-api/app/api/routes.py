"""
FastAPI route handlers for workflow API endpoints
"""

import uuid
from typing import Optional
from fastapi import HTTPException, BackgroundTasks
from datetime import datetime, timezone

from app.models.models import (
    WorkflowDefinition,
    WorkflowExecuteRequest,
    WorkflowNode,
    WorkflowEdge,
    WorkflowExecutionStatus
)
from app.db.database import (
    use_mongodb,
    mongo_db,
    save_workflow_to_db,
    get_workflow_from_db,
    list_workflows_from_db,
    save_execution_to_db,
    get_execution_from_db,
    list_executions_from_db
)
import logging

logger = logging.getLogger(__name__)


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


async def get_workflow(workflow_id: str):
    """Get workflow definition"""
    
    workflow = get_workflow_from_db(workflow_id)
    
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    
    return workflow


async def list_workflows():
    """List all workflows"""
    
    workflow_list = list_workflows_from_db()
    
    return {
        "workflows": workflow_list,
        "count": len(workflow_list),
        "storage": "mongodb" if use_mongodb else "memory"
    }


async def execute_workflow(request: WorkflowExecuteRequest, background_tasks: BackgroundTasks, run_workflow_execution_func):
    """Execute a workflow (either saved or ad-hoc)"""
    
    # Get workflow definition
    if request.workflow_id:
        workflow_def = get_workflow_from_db(request.workflow_id)
        if not workflow_def:
            raise HTTPException(status_code=404, detail="Workflow not found")
        nodes = [WorkflowNode(**n) for n in workflow_def["nodes"]]
        edges = [WorkflowEdge(**e) for e in workflow_def["edges"]]
    elif request.nodes and request.edges:
        # Preprocess nodes to fix malformed upload nodes
        processed_nodes = []
        for i, node in enumerate(request.nodes):
            # Check if this is a malformed upload node (has image data but missing id/type)
            node_dict = node.model_dump() if hasattr(node, 'model_dump') else node
            
            # Detect malformed upload node: has image fields but missing id/type
            if isinstance(node_dict, dict):
                has_image_data = any(key in node_dict for key in ['image', 'image_file', 'image_url', 'image_upload'])
                missing_required = 'id' not in node_dict or 'type' not in node_dict
                
                if has_image_data and missing_required:
                    # This is a malformed upload node - fix it
                    logger.warning(f"Detected malformed upload node at index {i}, auto-fixing...")
                    
                    # Extract image data
                    image_data = (node_dict.get('image_upload') or 
                                node_dict.get('image_file') or 
                                node_dict.get('image_url') or 
                                node_dict.get('image'))
                    
                    # Create proper upload node
                    fixed_node = WorkflowNode(
                        id=node_dict.get('id', f"upload_image-auto-{i}-{uuid.uuid4().hex[:8]}"),
                        type='upload_image',
                        config={
                            'image_upload': image_data
                        }
                    )
                    processed_nodes.append(fixed_node)
                    logger.info(f"✅ Fixed malformed node at index {i} -> {fixed_node.id}")
                else:
                    # Normal node - keep as is
                    processed_nodes.append(node if isinstance(node, WorkflowNode) else WorkflowNode(**node_dict))
            else:
                processed_nodes.append(node)
        
        nodes = processed_nodes
        
        # Process edges to ensure they're WorkflowEdge objects
        processed_edges = []
        for edge in request.edges:
            if isinstance(edge, WorkflowEdge):
                processed_edges.append(edge)
            elif isinstance(edge, dict):
                processed_edges.append(WorkflowEdge(**edge))
            else:
                processed_edges.append(edge)
        
        edges = processed_edges
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
    background_tasks.add_task(run_workflow_execution_func, execution_id, nodes, edges)
    
    return {
        "success": True,
        "execution_id": execution_id,
        "status": "queued",
        "message": "Workflow execution started",
        "storage": "mongodb" if use_mongodb else "memory"
    }


async def get_execution_status(execution_id: str):
    """Get workflow execution status"""
    
    execution = get_execution_from_db(execution_id)
    
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    
    return execution


async def list_executions(workflow_id: Optional[str] = None):
    """List all workflow executions"""
    
    execution_list = list_executions_from_db(workflow_id)
    
    return {
        "executions": execution_list,
        "count": len(execution_list),
        "storage": "mongodb" if use_mongodb else "memory"
    }


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


async def list_saved_results(
    tags: Optional[str] = None,
    result_name: Optional[str] = None,
    limit: int = 50,
    skip: int = 0
):
    """List saved workflow results with optional filtering"""
    
    if not use_mongodb:
        raise HTTPException(status_code=503, detail="Database not available")
    
    try:
        query = {}
        
        # Filter by tags
        if tags:
            tag_list = [t.strip() for t in tags.split(",")]
            query["tags"] = {"$in": tag_list}
        
        # Filter by result name
        if result_name:
            query["result_name"] = {"$regex": result_name, "$options": "i"}
        
        # Get total count
        total_count = mongo_db.results.count_documents(query)
        
        # Get results with pagination
        results = list(
            mongo_db.results.find(query, {"_id": 0})
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        
        return {
            "results": results,
            "count": len(results),
            "total_count": total_count,
            "has_more": (skip + len(results)) < total_count,
            "limit": limit,
            "skip": skip
        }
        
    except Exception as e:
        logger.error(f"Error listing results: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list results: {str(e)}")


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


async def root():
    """API documentation endpoint"""
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
            "enhance_prompt_gnokit": "Enhance prompt with gnokit/improve-prompt Ollama model",
            "enhance_prompt_fusion": "Fuse all enhancement methods with best results",
            "analyze_image_ollama": "Analyze image with Llama 3.2 Vision",
            "analyze_image": "Analyze image with YOLOv8 + Llama 3.2 Dual AI",
            "analyze_documents": "Analyze multiple documents and generate design prompts",
            "generate_image": "Generate image with Gemini (supports img2img with reference)",
            "generate_rag": "Generate image with RAG references",
            "generate_ideogram_turbo": "Generate image with Ideogram V3 Turbo (text-on-image)",
            "optimize_3d": "Optimize image for 3D model generation",
            "generate_3d": "Generate 3D model from image",
            "animate_luma": "Animate with Luma Ray 2",
            "animate_easy": "Animate with Segmind Easy Animate",
            "animate_wan": "Animate with WAN 2.2",
            "animate_wan_fast": "Fast animation with WAN 2.2 i2v-fast",
            "animate_wan_animate": "Transfer motion with WAN 2.2 Animate-Animation",
            "veo_3_fast": "Google Veo 3 Fast - Ultra-fast I2V/T2V for game videos",
            "fusion": "Fuse two images together",
            "sketch_board": "Create sketch from canvas drawing",
            "create_logo": "Generate logo design",
            "remove_background": "Remove background from image",
            "add_sound_to_image": "Add generated sound to image",
            "generate_music_suno": "Generate original music with Suno AI",
            "generate_cover_music": "Generate cover music with Suno AI",
            "replace_music_section": "Replace a time section in existing Suno music",
            "load_suno_from_taskid": "Load completed Suno music from task ID",
            "separate_vocals": "Separate vocals and instruments from Suno music into stems",
            "create_character": "Create consistent character reference sheet",
            "upload_image": "Upload image to S3 and get presigned URL",
            "dating_app_enhancement": "Enhance photos for dating apps (preserve identity)",
            "qwen_image_edit": "Edit images with Qwen Image Edit Plus LoRA",
            "extract_dna": "Extract physical DNA characteristics for family generation",
            "save_result": "Save all workflow results to database"
        }
    }


async def suno_webhook(request: dict):
    """
    Handle webhook callbacks from Suno API
    Receives task completion notifications with audio URLs
    """
    import httpx
    import boto3
    import os
    import tempfile
    from urllib.parse import urlparse
    
    logger.info("=== SUNO WEBHOOK RECEIVED ===")
    logger.info(f"Full payload: {request}")
    
    try:
        # Extract data from Suno callback payload
        # Typical Suno callback structure (may vary):
        # {
        #   "task_id": "...",
        #   "status": "success",
        #   "audio_url": "https://...",
        #   "result": {...}
        # }
        
        task_id = request.get("task_id") or request.get("id")
        status = request.get("status")
        audio_url = request.get("audio_url")
        
        # Check if result is nested
        if not audio_url and "result" in request:
            result = request.get("result")
            if isinstance(result, dict):
                audio_url = result.get("audio_url")
        
        # Also check for clips array (Suno returns clips with audio_url)
        if not audio_url and "clips" in request:
            clips = request.get("clips")
            if isinstance(clips, list) and len(clips) > 0:
                audio_url = clips[0].get("audio_url")
        
        logger.info(f"Extracted - Task ID: {task_id}, Status: {status}, Audio URL: {audio_url}")
        
        if not task_id:
            logger.error("No task_id found in webhook payload")
            return {
                "success": False,
                "error": "Missing task_id in payload",
                "received_payload": request
            }
        
        # If no audio URL, still log the callback
        if not audio_url:
            logger.warning(f"No audio_url found in webhook for task {task_id}")
            return {
                "success": True,
                "received": task_id,
                "status": status,
                "note": "No audio URL to process"
            }
        
        # Download the audio file
        logger.info(f"Downloading audio from: {audio_url}")
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            response = await client.get(audio_url)
            response.raise_for_status()
            audio_content = response.content
        
        logger.info(f"Downloaded {len(audio_content)} bytes of audio data")
        
        # Upload to S3
        s3_client = boto3.client(
            's3',
            region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        bucket_name = os.getenv("S3_BUCKET", "crimblame")
        
        # Create S3 key
        filename = os.path.basename(urlparse(audio_url).path) or f"{task_id}.mp3"
        if not filename.endswith('.mp3'):
            filename += '.mp3'
        
        s3_key = f"suno_webhook/{task_id}/{filename}"
        
        logger.info(f"Uploading to S3: s3://{bucket_name}/{s3_key}")
        
        s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_key,
            Body=audio_content,
            ContentType='audio/mpeg'
        )
        
        # Generate S3 URL
        s3_url = f"https://{bucket_name}.s3.amazonaws.com/{s3_key}"
        
        logger.info(f"✓ Audio uploaded successfully to: {s3_url}")
        
        # TODO: Update workflow execution with results
        # This would require storing task_id -> execution_id mapping
        # For now, just log and return success
        
        return {
            "success": True,
            "received": task_id,
            "status": status,
            "s3_url": s3_url,
            "original_audio_url": audio_url,
            "message": "Audio processed and uploaded to S3"
        }
        
    except Exception as e:
        logger.error(f"Error processing Suno webhook: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "received_payload": request
        }

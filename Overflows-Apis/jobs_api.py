#!/usr/bin/env python3
"""
Jobs API - Clean REST API for job management
Provides CRUD operations for jobs with database-first architecture
"""

import os
import time
import uuid
import shutil
import json
import base64
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

# Import database service directly
from db_service import DatabaseService

# Initialize database service
db_service = DatabaseService()

# Create router for jobs API
router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

# ========= REQUEST/RESPONSE MODELS =========

class JobCreateRequest(BaseModel):
    """Request model for creating a new job"""
    title: Optional[str] = Field(None, description="Job title/name")
    description: Optional[str] = Field(None, description="Job description")
    job_type: Optional[str] = Field("general", description="Type of job")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")

class JobUpdateRequest(BaseModel):
    """Request model for updating an existing job"""
    title: Optional[str] = Field(None, description="Updated job title")
    description: Optional[str] = Field(None, description="Updated job description")
    status: Optional[str] = Field(None, description="Updated job status")
    progress: Optional[int] = Field(None, description="Updated progress (0-100)")
    message: Optional[str] = Field(None, description="Updated status message")
    result: Optional[Dict[str, Any]] = Field(None, description="Updated result data")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Updated metadata")

class JobResponse(BaseModel):
    """Response model for job data"""
    job_id: str = Field(description="Unique job identifier")
    title: Optional[str] = Field(None, description="Job title")
    description: Optional[str] = Field(None, description="Job description")
    job_type: str = Field("general", description="Type of job")
    status: str = Field("pending", description="Current status")
    progress: int = Field(0, description="Progress percentage (0-100)")
    message: str = Field("", description="Status message")
    result: Optional[Dict[str, Any]] = Field(None, description="Job result data")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")
    created_at: float = Field(description="Creation timestamp")
    updated_at: float = Field(description="Last update timestamp")
    source: str = Field("database", description="Data source (database/memory)")

class JobListResponse(BaseModel):
    """Response model for job list with pagination"""
    jobs: List[JobResponse] = Field(description="List of jobs")
    total_count: int = Field(description="Total number of jobs")
    page: int = Field(description="Current page number")
    page_size: int = Field(description="Number of jobs per page")
    source: str = Field("database", description="Data source")

# ========= UTILITY FUNCTIONS =========

def scan_job_folder(job_id: str) -> dict:
    """Scan job folder for files and assets"""
    try:
        job_folder = f"jobs/{job_id}"
        if not os.path.exists(job_folder):
            return {"exists": False, "files": [], "total_files": 0}
        
        files = []
        for root, dirs, filenames in os.walk(job_folder):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                rel_path = os.path.relpath(file_path, job_folder)
                files.append({
                    "name": filename,
                    "path": rel_path,
                    "size": os.path.getsize(file_path) if os.path.exists(file_path) else 0
                })
        
        return {
            "exists": True,
            "files": files,
            "total_files": len(files)
        }
    except Exception as e:
        print(f"❌ Error scanning job folder {job_id}: {e}")
        return {"exists": False, "files": [], "total_files": 0, "error": str(e)}

def get_job_images_base64(job_id: str) -> dict:
    """Get base64 encoded images from job folder"""
    try:
        job_folder = f"jobs/{job_id}"
        images = {}
        
        if not os.path.exists(job_folder):
            return images
        
        image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
        
        for root, dirs, files in os.walk(job_folder):
            for file in files:
                file_path = os.path.join(root, file)
                file_ext = os.path.splitext(file)[1].lower()
                
                if file_ext in image_extensions:
                    try:
                        with open(file_path, 'rb') as f:
                            image_data = f.read()
                            base64_data = base64.b64encode(image_data).decode('utf-8')
                            
                            # Determine MIME type
                            mime_type = f"image/{file_ext[1:]}"
                            if file_ext in ['.jpg', '.jpeg']:
                                mime_type = "image/jpeg"
                            
                            images[file] = {
                                "data": base64_data,
                                "mime_type": mime_type,
                                "size": len(image_data)
                            }
                    except Exception as e:
                        print(f"⚠️ Error encoding image {file}: {e}")
        
        return images
    except Exception as e:
        print(f"❌ Error getting job images for {job_id}: {e}")
        return {}

# ========= API ENDPOINTS =========

@router.get("/", response_model=JobListResponse)
async def get_all_jobs(
    page: int = 1,
    page_size: int = 100, 
    status: Optional[str] = None,
    job_type: Optional[str] = None
):
    """
    Get all jobs with pagination and filtering
    
    Returns a paginated list of jobs from the database with optional filtering.
    """
    try:
        # Ensure database connection
        await db_service.ensure_connection()
        print(f"📊 Retrieving jobs from database (page {page}, size {page_size})...")
        
        # Calculate skip and limit
        skip = (page - 1) * page_size
        
        # Get jobs with filters - note: simplified call since we don't know the exact API
        db_jobs = db_service.list_jobs(limit=page_size)
        
        # Convert to response format
        jobs_list = []
        for job in db_jobs.values() if isinstance(db_jobs, dict) else db_jobs:
            # Ensure job_id field is present
            if "job_id" not in job and "_id" in job:
                job["job_id"] = job["_id"]
            
            # Apply filters manually if needed
            if status and job.get("status") != status:
                continue
            if job_type and job.get("job_type") != job_type:
                continue
            
            job_response = JobResponse(
                job_id=job.get("job_id", job.get("_id", str(uuid.uuid4()))),
                title=job.get("title"),
                description=job.get("description"),
                job_type=job.get("job_type", "unknown"),
                status=job.get("status", "unknown"),
                progress=job.get("progress", 0),
                message=job.get("message", ""),
                result=job.get("result"),
                metadata=job.get("metadata"),
                created_at=job.get("created_at", time.time()),
                updated_at=job.get("updated_at", time.time()),
                source="database"
            )
            jobs_list.append(job_response)
        
        # Apply pagination
        start_idx = skip
        end_idx = start_idx + page_size
        paginated_jobs = jobs_list[start_idx:end_idx]
        
        print(f"✅ Retrieved {len(paginated_jobs)} jobs from database")
        return JobListResponse(
            jobs=paginated_jobs,
            total_count=len(jobs_list),
            page=page,
            page_size=page_size,
            source="database"
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve jobs: {str(e)}"
        )

@router.get("/{job_id}", response_model=JobResponse)
async def get_job_by_id(job_id: str):
    """
    Get job by ID with comprehensive data including folder scan and images
    
    Returns detailed job information including file structure and base64 images.
    """
    try:
        # Get job from database
        await db_service.ensure_connection()
        job_data = await db_service.get_job(job_id)
        
        if not job_data:
            raise HTTPException(
                status_code=404, 
                detail=f"Job {job_id} not found"
            )
        
        # Scan job folder for additional info
        folder_info = scan_job_folder(job_id)
        
        # Get base64 images
        images_base64 = get_job_images_base64(job_id)
        
        # Enhance result data with folder and image info
        enhanced_result = job_data.get("result", {}).copy() if job_data.get("result") else {}
        enhanced_result.update({
            "folder_info": folder_info,
            "images_base64": images_base64,
            "total_files": folder_info.get("total_files", 0),
            "has_images": len(images_base64) > 0
        })
        
        return JobResponse(
            job_id=job_data.get("job_id", job_id),
            title=job_data.get("title"),
            description=job_data.get("description"),
            job_type=job_data.get("job_type", "unknown"),
            status=job_data.get("status", "unknown"),
            progress=job_data.get("progress", 0),
            message=job_data.get("message", ""),
            result=enhanced_result,
            metadata=job_data.get("metadata"),
            created_at=job_data.get("created_at", time.time()),
            updated_at=job_data.get("updated_at", time.time()),
            source="database"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get job {job_id}: {str(e)}"
        )

@router.post("/", response_model=JobResponse)
async def create_job(job_request: JobCreateRequest, background_tasks: BackgroundTasks):
    """
    Create a new job
    
    Creates a new job in the database with the provided information.
    """
    try:
        # Generate unique job ID
        job_id = str(uuid.uuid4())
        current_time = time.time()
        
        # Create job data
        job_data = {
            "job_id": job_id,
            "title": job_request.title,
            "description": job_request.description,
            "job_type": job_request.job_type,
            "status": "pending",
            "progress": 0,
            "message": "Job created successfully",
            "result": None,
            "metadata": job_request.metadata,
            "created_at": current_time,
            "updated_at": current_time
        }
        
        # Save to database
        await db_service.ensure_connection()
        success = await db_service.create_job(job_id, job_data)
        
        if not success:
            raise HTTPException(
                status_code=500,
                detail="Failed to create job in database"
            )
        
        # Create job folder
        job_folder = f"jobs/{job_id}"
        os.makedirs(job_folder, exist_ok=True)
        
        # Save metadata to job folder
        metadata_path = os.path.join(job_folder, "job_metadata.json")
        with open(metadata_path, "w") as f:
            json.dump(job_data, f, indent=2)
        
        print(f"✅ Created new job: {job_id}")
        
        return JobResponse(**job_data, source="database")
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create job: {str(e)}"
        )

@router.put("/{job_id}", response_model=JobResponse)
async def update_job(job_id: str, job_request: JobUpdateRequest):
    """
    Update an existing job
    
    Updates job fields with the provided data. Only non-null fields are updated.
    """
    try:
        # Get existing job
        await db_service.ensure_connection()
        existing_job = await db_service.get_job(job_id)
        
        if not existing_job:
            raise HTTPException(
                status_code=404,
                detail=f"Job {job_id} not found"
            )
        
        # Prepare update data (only update non-null fields)
        update_data = existing_job.copy()
        update_data["updated_at"] = time.time()
        
        if job_request.title is not None:
            update_data["title"] = job_request.title
        if job_request.description is not None:
            update_data["description"] = job_request.description
        if job_request.status is not None:
            update_data["status"] = job_request.status
        if job_request.progress is not None:
            update_data["progress"] = job_request.progress
        if job_request.message is not None:
            update_data["message"] = job_request.message
        if job_request.result is not None:
            # Merge with existing result
            existing_result = existing_job.get("result", {})
            if isinstance(existing_result, dict) and isinstance(job_request.result, dict):
                update_data["result"] = {**existing_result, **job_request.result}
            else:
                update_data["result"] = job_request.result
        if job_request.metadata is not None:
            # Merge with existing metadata
            existing_metadata = existing_job.get("metadata", {})
            if isinstance(existing_metadata, dict) and isinstance(job_request.metadata, dict):
                update_data["metadata"] = {**existing_metadata, **job_request.metadata}
            else:
                update_data["metadata"] = job_request.metadata
        
        # Update in database
        success = await db_service.update_job(job_id, update_data)
        
        if not success:
            raise HTTPException(
                status_code=500,
                detail="Failed to update job in database"
            )
        
        # Get updated job
        updated_job = await db_service.get_job(job_id)
        
        print(f"✅ Updated job: {job_id}")
        
        return JobResponse(
            job_id=updated_job.get("job_id", job_id),
            title=updated_job.get("title"),
            description=updated_job.get("description"),
            job_type=updated_job.get("job_type", "unknown"),
            status=updated_job.get("status", "unknown"),
            progress=updated_job.get("progress", 0),
            message=updated_job.get("message", ""),
            result=updated_job.get("result"),
            metadata=updated_job.get("metadata"),
            created_at=updated_job.get("created_at", time.time()),
            updated_at=updated_job.get("updated_at", time.time()),
            source="database"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to update job {job_id}: {str(e)}"
        )

@router.delete("/{job_id}")
async def delete_job(job_id: str, remove_files: bool = False):
    """
    Delete a job
    
    Removes the job from the database and optionally deletes associated files.
    """
    try:
        # Check if job exists
        await db_service.ensure_connection()
        existing_job = await db_service.get_job(job_id)
        
        if not existing_job:
            raise HTTPException(
                status_code=404,
                detail=f"Job {job_id} not found"
            )
        
        # Delete from database
        success = await db_service.delete_job(job_id)
        
        if not success:
            raise HTTPException(
                status_code=500,
                detail="Failed to delete job from database"
            )
        
        # Optionally remove job folder and files
        if remove_files:
            job_folder = f"jobs/{job_id}"
            if os.path.exists(job_folder):
                try:
                    shutil.rmtree(job_folder)
                    print(f"🗑️ Deleted job folder: {job_folder}")
                except Exception as e:
                    print(f"⚠️ Failed to delete job folder {job_folder}: {e}")
        
        print(f"✅ Deleted job: {job_id}")
        
        return {
            "message": f"Job {job_id} deleted successfully",
            "job_id": job_id,
            "files_removed": remove_files
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete job {job_id}: {str(e)}"
        )

@router.post("/bulk-delete")
async def bulk_delete_jobs(
    job_ids: List[str],
    remove_files: bool = False,
    background_tasks: BackgroundTasks = None
):
    """
    Delete multiple jobs in bulk
    
    Efficiently removes multiple jobs from the database with optional file cleanup.
    """
    try:
        await db_service.ensure_connection()
        
        deleted_jobs = []
        failed_jobs = []
        
        for job_id in job_ids:
            try:
                # Check if job exists and delete
                existing_job = await db_service.get_job(job_id)
                if existing_job:
                    success = await db_service.delete_job(job_id)
                    if success:
                        deleted_jobs.append(job_id)
                        
                        # Schedule file cleanup in background if requested
                        if remove_files and background_tasks:
                            background_tasks.add_task(cleanup_job_files, job_id)
                    else:
                        failed_jobs.append({"job_id": job_id, "reason": "Database deletion failed"})
                else:
                    failed_jobs.append({"job_id": job_id, "reason": "Job not found"})
            except Exception as e:
                failed_jobs.append({"job_id": job_id, "reason": str(e)})
        
        print(f"✅ Bulk delete completed: {len(deleted_jobs)} deleted, {len(failed_jobs)} failed")
        
        return {
            "message": f"Bulk delete completed",
            "deleted_count": len(deleted_jobs),
            "failed_count": len(failed_jobs), 
            "deleted_jobs": deleted_jobs,
            "failed_jobs": failed_jobs,
            "files_cleanup_scheduled": remove_files
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Bulk delete operation failed: {str(e)}"
        )

def cleanup_job_files(job_id: str):
    """Background task to clean up job files"""
    try:
        job_folder = f"jobs/{job_id}"
        if os.path.exists(job_folder):
            shutil.rmtree(job_folder)
            print(f"🗑️ Cleaned up job folder: {job_folder}")
    except Exception as e:
        print(f"⚠️ Failed to cleanup job folder {job_folder}: {e}")

@router.get("/stats/summary")
async def get_jobs_stats():
    """
    Get jobs statistics and summary
    
    Returns overview statistics about jobs in the system.
    """
    try:
        await db_service.ensure_connection()
        
        # Get all jobs for statistics (this could be optimized with database aggregation)
        all_jobs = db_service.list_jobs(limit=10000)  # High limit to get most jobs
        
        # Calculate statistics
        stats = {
            "total_jobs": 0,
            "status_breakdown": {},
            "job_type_breakdown": {},
            "recent_activity": [],
            "average_progress": 0,
            "completion_rate": 0
        }
        
        if not all_jobs:
            return stats
        
        jobs_list = list(all_jobs.values()) if isinstance(all_jobs, dict) else all_jobs
        stats["total_jobs"] = len(jobs_list)
        
        total_progress = 0
        completed_jobs = 0
        
        # Sort jobs by updated_at for recent activity
        sorted_jobs = sorted(jobs_list, key=lambda x: x.get("updated_at", 0), reverse=True)
        
        for job in jobs_list:
            # Status breakdown
            status = job.get("status", "unknown")
            stats["status_breakdown"][status] = stats["status_breakdown"].get(status, 0) + 1
            
            # Job type breakdown
            job_type = job.get("job_type", "unknown")
            stats["job_type_breakdown"][job_type] = stats["job_type_breakdown"].get(job_type, 0) + 1
            
            # Progress calculation
            progress = job.get("progress", 0)
            total_progress += progress
            
            if status == "completed" or progress >= 100:
                completed_jobs += 1
        
        # Calculate averages
        stats["average_progress"] = total_progress / len(jobs_list) if jobs_list else 0
        stats["completion_rate"] = (completed_jobs / len(jobs_list) * 100) if jobs_list else 0
        
        # Recent activity (last 10 jobs)
        stats["recent_activity"] = [
            {
                "job_id": job.get("job_id", job.get("_id")),
                "title": job.get("title"),
                "status": job.get("status"),
                "updated_at": job.get("updated_at")
            }
            for job in sorted_jobs[:10]
        ]
        
        return stats
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get job statistics: {str(e)}"
        )

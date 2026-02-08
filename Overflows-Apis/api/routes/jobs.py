"""
Job status and management routes
"""
from fastapi import APIRouter, HTTPException
from api.job_manager import get_job_status, get_all_jobs, delete_job_status

router = APIRouter(tags=["Jobs"])

@router.get("/status/{job_id}")
async def get_job_status_endpoint(job_id: str):
    """Get status of a specific job."""
    job = get_job_status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job

@router.get("/jobs")
async def list_all_jobs():
    """List all jobs."""
    return get_all_jobs()

@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Delete a job."""
    success = delete_job_status(job_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return {"success": True, "job_id": job_id}

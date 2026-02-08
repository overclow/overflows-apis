"""
Job management utilities - status tracking, metadata, and storage
"""
import os
import json
import time
import threading
from typing import Optional, Dict, Any
from db_service import update_job_status_db, get_job_db, get_all_jobs_db, delete_job_db

# Global job storage
jobs = {}

# Try to use database by default - will be set by calling code
use_database = True  # Changed from False to True - use database by default

def update_job_status(job_id: str, status: str, progress: int = 0, message: str = "", result: dict = None):
    """Update job status using database service or fallback to memory."""
    global use_database, jobs
    
    if use_database:
        try:
            update_job_status_db(job_id, status, progress, message, result)
            return
        except Exception as e:
            print(f"⚠️ Database update failed: {e}")
            print("📌 Falling back to in-memory storage")
            use_database = False  # Disable database for this session
    
    # Fallback to in-memory storage
    with threading.Lock():
        if job_id not in jobs:
            jobs[job_id] = {}
        
        jobs[job_id].update({
            "status": status,
            "progress": progress,
            "message": message,
            "last_updated": time.time()
        })
        
        if result:
            jobs[job_id]["result"] = result

def get_job_status(job_id: str) -> Optional[dict]:
    """Get job status from database or memory fallback."""
    global use_database, jobs
    
    if use_database:
        try:
            return get_job_db(job_id)
        except Exception as e:
            print(f"⚠️ Database query failed: {e}")
            use_database = False  # Disable database for this session
    
    return jobs.get(job_id)

def get_all_jobs() -> dict:
    """Get all jobs from database or memory fallback."""
    global use_database, jobs
    
    if use_database:
        try:
            return get_all_jobs_db()
        except Exception as e:
            print(f"⚠️ Database query failed: {e}")
    
    return jobs.copy()

def delete_job_status(job_id: str) -> bool:
    """Delete job from database or memory fallback."""
    global use_database, jobs
    
    if use_database:
        try:
            return delete_job_db(job_id)
        except Exception as e:
            print(f"⚠️ Database delete failed: {e}")
    
    if job_id in jobs:
        del jobs[job_id]
        return True
    return False

def job_exists(job_id: str) -> bool:
    """Check if job exists in database or memory fallback."""
    global use_database, jobs
    
    if use_database:
        try:
            job = get_job_db(job_id)
            return job is not None
        except Exception:
            pass
    
    return job_id in jobs

def save_job_metadata(job_id: str):
    """Save job metadata to disk for persistence."""
    try:
        job_folder = f"jobs/{job_id}"
        if not os.path.exists(job_folder):
            os.makedirs(job_folder, exist_ok=True)
        
        metadata_file = os.path.join(job_folder, "job_metadata.json")
        job_data = get_job_status(job_id)
        
        if job_data:
            with open(metadata_file, 'w') as f:
                json.dump(job_data, f, indent=2)
            print(f"💾 Job metadata saved to {metadata_file}")
        else:
            print(f"⚠️ No job data found for {job_id}")
            
    except Exception as e:
        print(f"❌ Error saving job metadata: {e}")

def load_job_metadata(job_id: str) -> dict:
    """Load job metadata from disk."""
    try:
        metadata_file = f"jobs/{job_id}/job_metadata.json"
        
        if os.path.exists(metadata_file):
            with open(metadata_file, 'r') as f:
                return json.load(f)
        else:
            print(f"⚠️ Metadata file not found for job {job_id}")
            return {}
            
    except Exception as e:
        print(f"❌ Error loading job metadata: {e}")
        return {}

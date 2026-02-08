#!/usr/bin/env python3
"""
MongoDB Database Service for 3D Animation API
Handles all database operations for job management, status tracking, and metadata storage.
"""

import os
import time
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from pymongo import MongoClient, IndexModel
from pymongo.errors import DuplicateKeyError, OperationFailure, ConnectionFailure
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DatabaseService:
    """MongoDB service for job management with automatic schema management and indexing."""
    
    def __init__(self, mongo_url: str = "mongodb://localhost:27017", db_name: str = "studio"):
        """Initialize database connection and setup indexes."""
        self.mongo_url = mongo_url
        self.db_name = db_name
        self.client = None
        self.db = None
        self._lock = threading.Lock()
        self._connected = False
        
        # Connect to database
        self.connect()
        
        # Setup collections and indexes
        if self._connected:
            self.setup_collections()
    
    def connect(self) -> bool:
        """Establish connection to MongoDB."""
        try:
            logger.info(f"🔌 Connecting to MongoDB at {self.mongo_url}")
            self.client = MongoClient(
                self.mongo_url,
                serverSelectionTimeoutMS=2000,  # 2 second timeout (reduced)
                connectTimeoutMS=3000,          # 3 second timeout (reduced)
                socketTimeoutMS=5000,           # 5 second timeout (reduced)
                maxPoolSize=10,                 # Reduced pool size
                retryWrites=True
            )
            
            # Test connection
            self.client.admin.command('ping')
            self.db = self.client[self.db_name]
            self._connected = True
            
            logger.info(f"✅ Connected to MongoDB database '{self.db_name}'")
            return True
            
        except ConnectionFailure as e:
            logger.error(f"❌ Failed to connect to MongoDB: {e}")
            self._connected = False
            return False
        except Exception as e:
            logger.error(f"❌ Unexpected error connecting to MongoDB: {e}")
            self._connected = False
            return False
    
    def setup_collections(self):
        """Setup collections with proper indexes and schema validation."""
        try:
            # Jobs collection with indexes
            jobs_collection = self.db.jobs
            
            # Create indexes for efficient querying
            indexes = [
                IndexModel([("job_id", 1)], unique=True),  # Unique job_id index
                IndexModel([("status", 1)]),               # Status index for filtering
                IndexModel([("created_at", -1)]),          # Creation time index (descending)
                IndexModel([("updated_at", -1)]),          # Update time index (descending)
                IndexModel([("isInfused", 1)]),            # Infusion job index
                IndexModel([("result.job_type", 1)]),      # Job type index
                IndexModel([("result.animation_type", 1)]) # Animation type index
            ]
            
            # Create indexes (this will skip existing ones)
            jobs_collection.create_indexes(indexes)
            
            logger.info("✅ Database collections and indexes setup completed")
            
        except Exception as e:
            logger.error(f"❌ Error setting up collections: {e}")
    
    def ensure_connection(self) -> bool:
        """Ensure database connection is active, reconnect if needed."""
        if not self._connected:
            return self.connect()
        
        try:
            # Test connection with ping
            self.client.admin.command('ping')
            return True
        except:
            logger.warning("🔄 Connection lost, attempting to reconnect...")
            return self.connect()
    
    # ========= JOB MANAGEMENT OPERATIONS =========
    
    def create_job(self, job_id: str, status: str = "queued", progress: int = 0, 
                   message: str = "", result: dict = None) -> dict:
        """Create a new job in the database."""
        with self._lock:
            if not self.ensure_connection():
                raise Exception("Database connection failed")
            
            try:
                current_time = time.time()
                
                # Determine if this is an infused job
                is_infused = False
                if result:
                    if (result.get("job_type") == "infusion" or 
                        "fusion_style" in result or 
                        "fusion_data" in result or
                        "fused_image_path" in result):
                        is_infused = True
                
                job_doc = {
                    "job_id": job_id,
                    "status": status,
                    "progress": progress,
                    "message": message,
                    "result": result or {},
                    "isInfused": is_infused,
                    "created_at": current_time,
                    "updated_at": current_time,
                    "created_datetime": datetime.fromtimestamp(current_time, tz=timezone.utc),
                    "updated_datetime": datetime.fromtimestamp(current_time, tz=timezone.utc)
                }
                
                # Insert into database
                self.db.jobs.insert_one(job_doc)
                
                # Remove MongoDB _id from returned object for compatibility
                job_doc.pop('_id', None)
                
                logger.info(f"📝 Created job {job_id} with status '{status}'")
                return job_doc
                
            except DuplicateKeyError:
                logger.warning(f"⚠️ Job {job_id} already exists")
                return self.get_job(job_id)
            except Exception as e:
                logger.error(f"❌ Error creating job {job_id}: {e}")
                raise
    
    def get_job(self, job_id: str) -> Optional[dict]:
        """Retrieve a job by ID."""
        if not self.ensure_connection():
            return None
        
        try:
            job_doc = self.db.jobs.find_one({"job_id": job_id}, {"_id": 0})
            return job_doc
        except Exception as e:
            logger.error(f"❌ Error retrieving job {job_id}: {e}")
            return None
    
    def update_job(self, job_id: str, status: str = None, progress: int = None, 
                   message: str = None, result: dict = None, merge_result: bool = True) -> bool:
        """Update an existing job."""
        with self._lock:
            if not self.ensure_connection():
                return False
            
            try:
                # Build update document
                update_doc = {
                    "updated_at": time.time(),
                    "updated_datetime": datetime.now(tz=timezone.utc)
                }
                
                if status is not None:
                    update_doc["status"] = status
                if progress is not None:
                    update_doc["progress"] = progress
                if message is not None:
                    update_doc["message"] = message
                
                # Handle result merging
                if result is not None:
                    if merge_result:
                        # Get existing job to merge results
                        existing_job = self.get_job(job_id)
                        if existing_job:
                            existing_result = existing_job.get("result", {})
                            merged_result = existing_result.copy()
                            merged_result.update(result)
                            update_doc["result"] = merged_result
                            
                            # Update infusion status if needed
                            if (merged_result.get("job_type") == "infusion" or 
                                "fusion_style" in merged_result or 
                                "fusion_data" in merged_result or
                                "fused_image_path" in merged_result):
                                update_doc["isInfused"] = True
                        else:
                            update_doc["result"] = result
                    else:
                        update_doc["result"] = result
                
                # Update in database
                result = self.db.jobs.update_one(
                    {"job_id": job_id},
                    {"$set": update_doc}
                )
                
                if result.modified_count > 0:
                    logger.debug(f"📝 Updated job {job_id}")
                    return True
                else:
                    logger.warning(f"⚠️ No changes made to job {job_id}")
                    return False
                    
            except Exception as e:
                logger.error(f"❌ Error updating job {job_id}: {e}")
                return False
    
    def delete_job(self, job_id: str) -> bool:
        """Delete a job from the database."""
        with self._lock:
            if not self.ensure_connection():
                return False
            
            try:
                result = self.db.jobs.delete_one({"job_id": job_id})
                if result.deleted_count > 0:
                    logger.info(f"🗑️ Deleted job {job_id}")
                    return True
                else:
                    logger.warning(f"⚠️ Job {job_id} not found for deletion")
                    return False
            except Exception as e:
                logger.error(f"❌ Error deleting job {job_id}: {e}")
                return False
    
    def list_jobs(self, status: str = None, limit: int = 100, skip: int = 0, 
                  sort_by: str = "updated_at", sort_order: int = -1) -> List[dict]:
        """List jobs with optional filtering and pagination."""
        if not self.ensure_connection():
            return []
        
        try:
            # Build query filter
            query = {}
            if status:
                query["status"] = status
            
            # Execute query with sorting and pagination
            cursor = self.db.jobs.find(
                query, 
                {"_id": 0}  # Exclude MongoDB _id field
            ).sort(sort_by, sort_order).skip(skip).limit(limit)
            
            jobs = list(cursor)
            return jobs
            
        except Exception as e:
            logger.error(f"❌ Error listing jobs: {e}")
            return []
    
    def count_jobs(self, status: str = None) -> int:
        """Count jobs with optional status filter."""
        if not self.ensure_connection():
            return 0
        
        try:
            query = {}
            if status:
                query["status"] = status
            
            return self.db.jobs.count_documents(query)
        except Exception as e:
            logger.error(f"❌ Error counting jobs: {e}")
            return 0
    
    def get_job_statistics(self) -> dict:
        """Get comprehensive job statistics."""
        if not self.ensure_connection():
            return {}
        
        try:
            # Aggregate statistics
            pipeline = [
                {
                    "$group": {
                        "_id": "$status",
                        "count": {"$sum": 1}
                    }
                }
            ]
            
            status_counts = {}
            for doc in self.db.jobs.aggregate(pipeline):
                status_counts[doc["_id"]] = doc["count"]
            
            # Get infusion job count
            infused_count = self.db.jobs.count_documents({"isInfused": True})
            
            # Get total count
            total_count = self.db.jobs.count_documents({})
            
            # Get recent activity (last 24 hours)
            twenty_four_hours_ago = time.time() - (24 * 60 * 60)
            recent_count = self.db.jobs.count_documents({
                "created_at": {"$gte": twenty_four_hours_ago}
            })
            
            return {
                "total": total_count,
                "by_status": status_counts,
                "infused_jobs": infused_count,
                "recent_24h": recent_count,
                "active": status_counts.get("processing", 0),
                "completed": status_counts.get("completed", 0),
                "failed": status_counts.get("failed", 0),
                "queued": status_counts.get("queued", 0)
            }
            
        except Exception as e:
            logger.error(f"❌ Error getting job statistics: {e}")
            return {}
    
    # ========= LEGACY COMPATIBILITY METHODS =========
    
    def get_all_jobs_dict(self) -> dict:
        """Get all jobs as a dictionary for legacy compatibility."""
        jobs_dict = {}
        try:
            jobs_list = self.list_jobs(limit=10000)  # Get all jobs
            for job in jobs_list:
                jobs_dict[job["job_id"]] = job
            return jobs_dict
        except Exception as e:
            logger.error(f"❌ Error getting all jobs dict: {e}")
            return {}
    
    def job_exists(self, job_id: str) -> bool:
        """Check if a job exists."""
        if not self.ensure_connection():
            return False
        
        try:
            return self.db.jobs.count_documents({"job_id": job_id}) > 0
        except Exception as e:
            logger.error(f"❌ Error checking job existence {job_id}: {e}")
            return False
    
    # ========= UTILITY METHODS =========
    
    def health_check(self) -> dict:
        """Perform database health check."""
        try:
            if not self.ensure_connection():
                return {
                    "status": "unhealthy",
                    "connected": False,
                    "error": "Failed to connect to database"
                }
            
            # Test basic operations
            ping_result = self.client.admin.command('ping')
            stats = self.db.command("dbstats")
            
            return {
                "status": "healthy",
                "connected": True,
                "database": self.db_name,
                "collections": self.db.list_collection_names(),
                "storage_size_mb": round(stats.get("storageSize", 0) / (1024 * 1024), 2),
                "data_size_mb": round(stats.get("dataSize", 0) / (1024 * 1024), 2),
                "indexes": stats.get("indexes", 0),
                "objects": stats.get("objects", 0)
            }
            
        except Exception as e:
            return {
                "status": "unhealthy",
                "connected": False,
                "error": str(e)
            }
    
    def close(self):
        """Close database connection."""
        if self.client:
            self.client.close()
            self._connected = False
            logger.info("🔌 Database connection closed")

# ========= GLOBAL DATABASE INSTANCE =========

# Global database service instance
db_service = None

def get_db_service() -> DatabaseService:
    """Get or create global database service instance."""
    global db_service
    if db_service is None:
        # Get MongoDB URL from environment or use default
        mongo_url = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
        db_name = os.getenv("MONGODB_DATABASE", "studio")
        
        db_service = DatabaseService(mongo_url, db_name)
    
    return db_service

def initialize_database() -> bool:
    """Initialize database service and test connection with timeout."""
    try:
        import threading
        import queue
        
        # Use threading to implement timeout
        result_queue = queue.Queue()
        
        def init_worker():
            try:
                db = get_db_service()
                health = db.health_check()
                result_queue.put(("success", health))
            except Exception as e:
                result_queue.put(("error", str(e)))
        
        # Start initialization in a separate thread
        init_thread = threading.Thread(target=init_worker)
        init_thread.daemon = True
        init_thread.start()
        
        # Wait for result with timeout
        try:
            result_type, result_data = result_queue.get(timeout=5)  # 5 second timeout
            
            if result_type == "success":
                if result_data["status"] == "healthy":
                    logger.info("✅ Database service initialized successfully")
                    return True
                else:
                    logger.error(f"❌ Database health check failed: {result_data}")
                    return False
            else:
                logger.error(f"❌ Database initialization error: {result_data}")
                return False
                
        except queue.Empty:
            logger.error("❌ Database initialization timed out after 5 seconds")
            return False
            
    except Exception as e:
        logger.error(f"❌ Failed to initialize database service: {e}")
        return False

# ========= LEGACY COMPATIBILITY WRAPPER FUNCTIONS =========

def update_job_status_db(job_id: str, status: str, progress: int = 0, message: str = "", result: dict = None):
    """Legacy compatibility wrapper for update_job_status."""
    db = get_db_service()
    
    # Check if job exists, create if not
    if not db.job_exists(job_id):
        db.create_job(job_id, status, progress, message, result)
    else:
        db.update_job(job_id, status, progress, message, result, merge_result=True)

def get_job_db(job_id: str) -> Optional[dict]:
    """Legacy compatibility wrapper for getting a job."""
    db = get_db_service()
    return db.get_job(job_id)

def get_all_jobs_db() -> dict:
    """Legacy compatibility wrapper for getting all jobs as dict."""
    db = get_db_service()
    return db.get_all_jobs_dict()

def delete_job_db(job_id: str) -> bool:
    """Legacy compatibility wrapper for deleting a job."""
    db = get_db_service()
    return db.delete_job(job_id)

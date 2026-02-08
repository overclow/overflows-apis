"""
Workflow API - Orchestrate complex animation pipelines
Execute nodes in order based on defined edges and dependencies

This is the main entry point that initializes the FastAPI app and sets up routes.
The actual implementation is organized into separate modules for maintainability.
"""

from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional

# Import configuration
from app.core.config import ALLOWED_ORIGINS, print_config_status

# Import models
from app.models.models import WorkflowDefinition, WorkflowExecuteRequest

# Import database initialization
from app.db.database import init_mongodb

# Import route handlers
from app.api import routes

# Import workflow engine (contains WorkflowEngine class with all node executors)
from app.core.workflow_engine_full import run_workflow_execution

# Print configuration on startup
print_config_status()

# Initialize FastAPI app
app = FastAPI(title="Workflow Orchestration API")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize MongoDB on startup
init_mongodb()


# ========= API ENDPOINTS =========

@app.get("/")
async def root():
    return await routes.root()


@app.post("/workflow/create")
async def create_workflow_endpoint(workflow: WorkflowDefinition):
    return await routes.create_workflow(workflow)


@app.get("/workflow/{workflow_id}")
async def get_workflow_endpoint(workflow_id: str):
    return await routes.get_workflow(workflow_id)


@app.get("/workflows")
async def list_workflows_endpoint():
    return await routes.list_workflows()


@app.post("/workflow/execute")
async def execute_workflow_endpoint(request: WorkflowExecuteRequest, background_tasks: BackgroundTasks):
    return await routes.execute_workflow(request, background_tasks, run_workflow_execution)


@app.get("/workflow/execution/{execution_id}")
async def get_execution_status_endpoint(execution_id: str):
    return await routes.get_execution_status(execution_id)


@app.get("/workflow/status/{execution_id}")
async def get_execution_status_alias_endpoint(execution_id: str):
    """Alias for /workflow/execution/{execution_id} for backward compatibility"""
    return await routes.get_execution_status(execution_id)


@app.get("/workflow/executions")
async def list_executions_endpoint(workflow_id: Optional[str] = None):
    return await routes.list_executions(workflow_id)


@app.get("/workflow/result/{result_id}")
async def get_saved_result_endpoint(result_id: str):
    return await routes.get_saved_result(result_id)


@app.get("/workflow/results")
async def list_saved_results_endpoint(
    tags: Optional[str] = None,
    result_name: Optional[str] = None,
    limit: int = 50,
    skip: int = 0
):
    return await routes.list_saved_results(tags, result_name, limit, skip)


@app.delete("/workflow/result/{result_id}")
async def delete_saved_result_endpoint(result_id: str):
    return await routes.delete_saved_result(result_id)


@app.post("/webhook/suno")
async def suno_webhook_endpoint(request: dict):
    """Webhook endpoint for Suno API callbacks"""
    return await routes.suno_webhook(request)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)

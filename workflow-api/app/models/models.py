"""
Pydantic models for workflow API
"""

from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional


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
    nodes: Optional[List[Any]] = None  # Changed from List[WorkflowNode] to accept raw dicts for auto-fixing
    edges: Optional[List[Any]] = None  # Changed from List[WorkflowEdge] to accept raw dicts


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

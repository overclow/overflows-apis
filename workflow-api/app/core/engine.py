"""
Workflow Engine - Core execution logic and node executors

This module contains the WorkflowEngine class and the run_workflow_execution function.
Note: This is imported from the original workflow_engine_full.py with proper imports updated.
"""

# Re-export the engine from the full implementation
# After proper import path updates, this becomes the clean interface

import sys
import os

# Ensure parent directory is in path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# Import from the full engine file (temporary solution)
# TODO: Further refactor workflow_engine_full.py to use proper imports
from app.core.workflow_engine_full import WorkflowEngine, run_workflow_execution

__all__ = ["WorkflowEngine", "run_workflow_execution"]

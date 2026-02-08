"""
Prompt enhancement and text processing node executors
"""

import requests
import json
from typing import Dict, Any

from models import WorkflowNode


async def execute_text_prompt(node: WorkflowNode, inputs: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    """Execute text_prompt node - simply passes through the prompt"""
    prompt = node.config.get("prompt", "")
    
    # Allow dynamic prompt from previous node
    if "prompt" in inputs and inputs["prompt"]:
        prompt = inputs["prompt"]
    
    return {
        "prompt": prompt,
        "type": "text"
    }


async def execute_enhance_prompt(node: WorkflowNode, inputs: Dict[str, Any], base_url: str) -> Dict[str, Any]:
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
        f"{base_url}/enhance/prompt",
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


async def execute_enhance_prompt_ollama(node: WorkflowNode, inputs: Dict[str, Any], base_url: str) -> Dict[str, Any]:
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
        f"{base_url}/enhance/prompt/ollama",
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

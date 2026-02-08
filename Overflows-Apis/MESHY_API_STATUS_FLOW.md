# Meshy API Status Polling Flow

## Overview
The `/animate/image` endpoint now uses the REAL 3D generation pipeline that polls Meshy AI's API for actual status updates.

## How It Works

### 1. Job Submission
```json
POST /animate/image
Response: {
  "forge_id": "abc-123-xyz",
  "job_id": "abc-123-xyz", 
  "status": "queued",
  "message": "🚀 3D generation started - will poll Meshy API for completion status"
}
```

### 2. Status Polling (Check with `/status/{job_id}`)

The pipeline polls Meshy API every **2 seconds** and updates job status in real-time:

#### Phase 1: 3D Model Generation (10-25%)
```
Status: "processing" (10%)
Message: "Generating 3D model from image..."

→ Calls Segmind Hunyuan3D API (image-to-3D)
→ Validates image (size, format, content)
→ Submits to Meshy for 3D mesh generation
→ Waits for GLB file to be ready
```

**Meshy API Response During Generation:**
```json
{
  "status": "PENDING",
  "progress": 0,
  "task_id": "mesh-task-123"
}
```

```json
{
  "status": "PENDING", 
  "progress": 45,
  "task_id": "mesh-task-123"
}
```

```json
{
  "status": "SUCCEEDED",
  "progress": 100,
  "result": {
    "model_url": "https://assets.meshy.ai/google-oauth2-xxx/tasks/xxx/output/model.glb"
  }
}
```

#### Phase 2: S3 Upload (25-30%)
```
Status: "processing" (25%)
Message: "Uploading model to cloud storage..."

→ Uploads GLB to AWS S3
→ Generates presigned URL (24 hour expiry)
→ Gets permanent S3 direct URL
```

**Job Status Update:**
```json
{
  "status": "processing",
  "progress": 30,
  "message": "Model uploaded to S3",
  "result": {
    "s3_upload": {
      "presigned_url": "https://s3.amazonaws.com/...",
      "s3_direct_url": "https://s3.amazonaws.com/bucket/key",
      "s3_key": "meshy_tmp/12345/model.glb"
    }
  }
}
```

#### Phase 3: Retexturing (40%) - Optional if `use_retexture=true`
```
Status: "processing" (40%)
Message: "Enhancing textures..."

→ Calls Meshy Retexture API
→ Preserves/enhances existing textures
→ Prevents texture loss during rigging
```

**Meshy Retexture API Polling:**
```
Console Output:
[abc-123] 🎨 RETEXTURING WITH MESHY API TO PRESERVE TEXTURES...
[abc-123] → Retexture task ID: retex-task-456
Retexturing: PENDING (0%)
Retexturing: PENDING (30%)
Retexturing: PENDING (65%)
Retexturing: SUCCEEDED (100%)
[abc-123] ✅ Retextured model ready: https://assets.meshy.ai/.../retextured.glb
```

**Meshy API Response:**
```json
{
  "status": "SUCCEEDED",
  "progress": 100,
  "result": {
    "model_url": "https://assets.meshy.ai/.../retextured.glb",
    "texture_maps": {
      "diffuse": "...",
      "normal": "...",
      "roughness": "..."
    }
  }
}
```

#### Phase 4: Character Rigging (45-60%)
```
Status: "processing" (45%)
Message: "Creating character rig..."

→ Calls Meshy Rigging API
→ Creates bone structure
→ Sets up character skeleton
```

**Meshy Rigging API Polling (Every 2 seconds):**
```
Console Output:
[abc-123] 🤖 Starting async rigging process...
[abc-123] 🎯 Rigging task submitted: rig-task-789
[abc-123] Rigging: PENDING (0%)
[abc-123] Rigging: PENDING (15%)
[abc-123] Rigging: PENDING (34%)
[abc-123] Rigging: PENDING (58%)
[abc-123] Rigging: PENDING (76%)
[abc-123] Rigging: PENDING (89%)
[abc-123] Rigging: SUCCEEDED (100%)
[abc-123] ✅ Rigging completed successfully
```

**Meshy API Response Structure:**
```json
// While processing:
{
  "status": "PENDING",
  "progress": 58,
  "task_id": "rig-task-789"
}

// On completion:
{
  "status": "SUCCEEDED",
  "progress": 100,
  "result": {
    "rigged_character_fbx_url": "https://assets.meshy.ai/.../rigged.fbx",
    "rigged_character_glb_url": "https://assets.meshy.ai/.../rigged.glb"
  }
}

// On failure:
{
  "status": "FAILED",
  "task_error": {
    "message": "Pose estimation failed",
    "code": "RIGGING_ERROR"
  }
}
```

**Job Status After Rigging:**
```json
{
  "status": "processing",
  "progress": 60,
  "message": "Rigging: SUCCEEDED (100%)",
  "result": {
    "rigged_character_glb_url": "https://assets.meshy.ai/.../rigged.glb",
    "rigged_character_fbx_url": "https://assets.meshy.ai/.../rigged.fbx"
  }
}
```

#### Phase 5: Animation Generation (70-90%)
```
Status: "processing" (70-90%)
Message: "Creating animation {action_id}..."

→ Applies animation to rigged model
→ One API call per animation ID
→ Adjusts FPS if specified
```

**Meshy Animation API Polling (For each animation):**
```
Console Output:
[abc-123] 🎬 Starting async animation process...
[abc-123] 🎯 Animation 106 task submitted: anim-task-111
[abc-123] Animation 106: PENDING (0%)
[abc-123] Animation 106: PENDING (42%)
[abc-123] Animation 106: PENDING (78%)
[abc-123] Animation 106: SUCCEEDED (100%)
[abc-123] ✅ Animation 106 completed and stored in database

[abc-123] 🎯 Animation 30 task submitted: anim-task-222
[abc-123] Animation 30: PENDING (0%)
[abc-123] Animation 30: PENDING (51%)
[abc-123] Animation 30: SUCCEEDED (100%)
[abc-123] ✅ Animation 30 completed and stored in database
```

**Meshy API Response:**
```json
{
  "status": "SUCCEEDED",
  "progress": 100,
  "result": {
    "animation_glb_url": "https://assets.meshy.ai/.../anim_106.glb",
    "animation_fbx_url": "https://assets.meshy.ai/.../anim_106.fbx"
  }
}
```

#### Phase 6: Completion (100%)
```
Status: "completed" (100%)
Message: "Job completed successfully"
```

**Final Job Result:**
```json
{
  "status": "completed",
  "progress": 100,
  "message": "Job completed successfully",
  "result": {
    "model_url": "https://s3.amazonaws.com/.../presigned?...",
    "glb_url": "https://s3.amazonaws.com/.../presigned?...",
    "s3_direct_url": "https://s3.amazonaws.com/bucket/key.glb",
    "animation_urls": [
      {
        "action_id": 106,
        "raw_anim_glb": "https://assets.meshy.ai/.../anim_106.glb",
        "raw_anim_fbx": "https://assets.meshy.ai/.../anim_106.fbx",
        "fps": 60
      },
      {
        "action_id": 30,
        "raw_anim_glb": "https://assets.meshy.ai/.../anim_30.glb",
        "raw_anim_fbx": "https://assets.meshy.ai/.../anim_30.fbx",
        "fps": 60
      }
    ],
    "total_animations": 2,
    "rigging_status": "completed",
    "processing_method": "async_with_rigging"
  }
}
```

## Key Polling Functions

### `poll_task()` - Synchronous Polling (test3danimate.py)
```python
def poll_task(task_url: str, label: str, interval: float = 2.0):
    while True:
        data = get_json(task_url)
        status = data.get("status")
        progress = data.get("progress", 0)
        print(f"{label}: {status} ({progress}%)")
        if status in ("SUCCEEDED", "FAILED"):
            return data
        time.sleep(interval)
```

### `async_poll_task()` - Async Polling (animation_api.py)
```python
async def async_poll_task(task_url: str, label: str, interval: float = 2.0, job_id: str = None):
    while True:
        data = await async_get_json(task_url)
        status = data.get("status")
        progress = data.get("progress", 0)
        print(f"[{job_id}] {label}: {status} ({progress}%)")
        
        # Update job status if job_id provided
        if job_id:
            update_job_status(job_id, "processing", min(progress, 99), f"{label}: {status} ({progress}%)")
        
        if status in ("SUCCEEDED", "FAILED"):
            return data
        
        await asyncio.sleep(interval)
```

## Status Codes from Meshy API

| Status | Meaning | Action |
|--------|---------|--------|
| `PENDING` | Task is processing | Continue polling |
| `SUCCEEDED` | Task completed successfully | Extract result URLs |
| `FAILED` | Task failed | Handle error, check task_error field |

## Error Handling

### Rigging Failures
If rigging fails (e.g., "Pose estimation failed"), the pipeline continues without animations:
```json
{
  "status": "completed",
  "progress": 100,
  "message": "Job completed successfully (rigging failed, no animations created)",
  "result": {
    "model_url": "https://...",
    "rigging_status": "failed",
    "rigging_error": "Pose estimation failed - model may not be suitable for rigging"
  }
}
```

### Animation Failures
If individual animations fail, the pipeline continues with remaining animations.

## Workflow Integration

Your workflow polls `/status/{job_id}` every 10 seconds and will see:
1. Real progress updates from Meshy API (0% → 100%)
2. Detailed status messages showing which phase is running
3. Final completion with actual GLB file URLs

## Example Timeline

```
00:00 - Job submitted: "queued" (0%)
00:02 - 3D generation starts: "processing" (10%) - "Generating 3D model..."
00:05 - Meshy mesh generation: "processing" (10%) - "3D Generation: PENDING (23%)"
00:08 - Meshy mesh generation: "processing" (10%) - "3D Generation: PENDING (67%)"
00:10 - Mesh complete: "processing" (25%) - "Uploading to S3..."
00:12 - S3 upload done: "processing" (30%) - "Model uploaded to S3"
00:14 - Retexturing starts: "processing" (40%) - "Enhancing textures..."
00:18 - Retexturing done: "processing" (45%) - "Creating character rig..."
00:20 - Rigging in progress: "processing" (50%) - "Rigging: PENDING (34%)"
00:25 - Rigging in progress: "processing" (55%) - "Rigging: PENDING (76%)"
00:28 - Rigging complete: "processing" (60%) - "Rigging: SUCCEEDED (100%)"
00:30 - Animation 106 starts: "processing" (70%) - "Creating animation 106..."
00:35 - Animation 106 done: "processing" (80%) - "Animation 106 completed"
00:37 - Animation 30 starts: "processing" (85%) - "Creating animation 30..."
00:42 - Animation 30 done: "processing" (90%) - "Animation 30 completed"
00:44 - Job complete: "completed" (100%) - "Job completed successfully"
```

## Testing the Flow

1. Start the modular API:
   ```bash
   cd /Users/avihairing/workflows-apis/Overflows-Apis
   python animation_api_modular.py
   ```

2. Submit a job:
   ```bash
   curl -X POST http://localhost:8000/animate/image \
     -F "file=@your_image.jpg" \
     -F "animation_ids=106,30" \
     -F "skip_confirmation=true"
   ```

3. Poll for status:
   ```bash
   watch -n 2 curl http://localhost:8000/status/{job_id}
   ```

4. Watch console logs to see Meshy API responses in real-time!

## Summary

✅ The endpoint **DOES** check Meshy API for actual generation status  
✅ Polls every **2 seconds** until task succeeds or fails  
✅ Returns **real progress updates** (0-100%)  
✅ Shows **actual Meshy API status codes** (PENDING, SUCCEEDED, FAILED)  
✅ Updates job status with **real-time messages** from Meshy  
✅ Returns **actual GLB file URLs** when complete  

Your workflow will see genuine Meshy AI generation progress, not simulated placeholders! 🎉

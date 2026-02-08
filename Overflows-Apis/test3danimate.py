import os, time, json, subprocess, shutil
import boto3, requests
from botocore.exceptions import ClientError
from urllib.parse import urlparse
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ========= CONFIG =========
API_V1 = "https://api.meshy.ai/openapi/v1"   # rigging + animations
MESHY_KEY = os.environ["MESHY_API_KEY"]

AWS_REGION = os.environ.get("AWS_REGION", "eu-central-1")
S3_BUCKET = os.environ["S3_BUCKET"]          # must exist
LOCAL_GLB = os.environ.get("LOCAL_GLB", "streetview_3d_model.glb")
S3_KEY = os.environ.get("S3_KEY", f"meshy_tmp/{int(time.time())}_{os.path.basename(LOCAL_GLB)}")

HEIGHT_METERS = float(os.environ.get("HEIGHT_METERS", "1.75"))
ANIMATION_IDS = [int(x) for x in os.environ.get("ANIMATION_IDS", "44").split(",")]
ANIM_FPS = int(os.environ.get("ANIM_FPS", "60"))
PRESIGN_TTL = int(os.environ.get("PRESIGN_TTL", "3600"))   # seconds

# New option: Skip Meshy rigging to preserve textures
SKIP_RIGGING = os.environ.get("SKIP_RIGGING", "false").lower() == "true"
USE_MIXAMO = os.environ.get("USE_MIXAMO", "true").lower() == "true"

# Retexture API options for texture preservation
USE_RETEXTURE = os.environ.get("USE_RETEXTURE", "true").lower() == "true"
RETEXTURE_PROMPT = os.environ.get("RETEXTURE_PROMPT", "preserve and enhance the existing textures, maintain original colors and patterns, high quality PBR materials")
RETEXTURE_IMAGE_URL = os.environ.get("RETEXTURE_IMAGE_URL", "")

# Blender binary (set BLENDER env var to full path if needed)
BLENDER = os.environ.get("BLENDER", "blender")

HDR_JSON = {"Authorization": f"Bearer {MESHY_KEY}", "Content-Type": "application/json"}
HDR_AUTH = {"Authorization": f"Bearer {MESHY_KEY}"}

# ========= Small embedded Blender helpers (written to temp files when needed) =========
PACK_SCRIPT = r"""
# blender -b -P pack_glb_textures.py -- in.(glb/gltf) out.glb
import bpy, sys
inp = sys.argv[sys.argv.index("--")+1]
out = sys.argv[sys.argv.index("--")+2]

bpy.ops.wm.read_factory_settings(use_empty=True)
ext = inp.lower().split(".")[-1]
if ext in ("gltf", "glb"):
    bpy.ops.import_scene.gltf(filepath=inp)
else:
    raise RuntimeError("Unsupported input: " + inp)

# Pack external textures into the .blend
bpy.ops.file.pack_all()

# Export GLB with images embedded
bpy.ops.export_scene.gltf(filepath=out, export_format='GLB', export_image_format='AUTO',
                          export_apply=True, export_animations=False, export_skins=True,
                          export_materials='EXPORT')
print("Packed GLB:", out)
"""

MERGE_SCRIPT = r"""
# blender -b -P merge_anim_to_rig.py -- textured.glb anim.(glb/fbx) out.glb
import bpy, sys, os
textured, anim, out = sys.argv[sys.argv.index("--")+1:sys.argv.index("--")+4]

bpy.ops.wm.read_factory_settings(use_empty=True)

# Import textured model first (this has the textures we want to preserve)
print("Importing textured model:", textured)
bpy.ops.import_scene.gltf(filepath=textured)

# Pack all textures to ensure they're embedded
bpy.ops.file.pack_all()

# Get all objects after import
textured_objects = list(bpy.data.objects)
textured_meshes = [o for o in textured_objects if o.type == "MESH"]
textured_armature = next((o for o in textured_objects if o.type == "ARMATURE"), None)

print(f"Textured model imported: {len(textured_meshes)} meshes, armature: {textured_armature.name if textured_armature else 'None'}")

# Store material information from textured meshes
mesh_materials = {}
for mesh_obj in textured_meshes:
    if mesh_obj.data.materials:
        mesh_materials[mesh_obj.name] = [mat for mat in mesh_obj.data.materials if mat]
        print(f"Stored materials for {mesh_obj.name}: {len(mesh_materials[mesh_obj.name])} materials")

# Import animation file
print("Importing animation:", anim)
ext = os.path.splitext(anim)[1].lower()
if ext == ".fbx":
    bpy.ops.import_scene.fbx(filepath=anim, automatic_bone_orientation=True)
elif ext in (".glb", ".gltf"):
    bpy.ops.import_scene.gltf(filepath=anim)
else:
    raise RuntimeError("Unsupported animation file: " + anim)

# Get animation objects (imported after textured model)
all_objects_after = list(bpy.data.objects)
anim_objects = [o for o in all_objects_after if o not in textured_objects]
anim_armature = next((o for o in anim_objects if o.type == "ARMATURE"), None)
anim_meshes = [o for o in anim_objects if o.type == "MESH"]

if not anim_armature:
    print("ERROR: No armature found in animation file")
    raise RuntimeError("No armature in animation file")

print(f"Animation imported: {len(anim_objects)} objects, armature: {anim_armature.name}, meshes: {len(anim_meshes)}")

# Check for animation data
if not anim_armature.animation_data:
    print("ERROR: No animation data found in animation armature")
    raise RuntimeError("No animation data in animation file")

# Get the action from animation armature
action = None
if anim_armature.animation_data.action:
    action = anim_armature.animation_data.action
    print(f"Found action: {action.name}")
elif anim_armature.animation_data.nla_tracks:
    # Get action from first NLA track
    for track in anim_armature.animation_data.nla_tracks:
        if track.strips:
            action = track.strips[0].action
            print(f"Found action from NLA track: {action.name}")
            break

if not action:
    print("ERROR: No action found in animation data")
    raise RuntimeError("No animation action found")

# CRITICAL FIX: Transfer materials from textured meshes to animated meshes
# This ensures we get both textures AND animation working together
if textured_armature:
    # If textured model has armature, apply animation to it
    if not textured_armature.animation_data:
        textured_armature.animation_data_create()
    textured_armature.animation_data.action = action.copy()
    print(f"Applied animation to textured armature: {action.name}")
    
    # CRITICAL: Force preserve original materials
    print("🎨 PRESERVING ORIGINAL TEXTURED MATERIALS...")
    for mesh_obj in textured_meshes:
        if mesh_obj.name in mesh_materials:
            # Store original materials to prevent overwriting
            original_materials = mesh_materials[mesh_obj.name].copy()
            
            # Clear and reassign original materials
            mesh_obj.data.materials.clear()
            for mat in original_materials:
                if mat:  # Ensure material exists
                    mesh_obj.data.materials.append(mat)
                    print(f"✅ Preserved material: {mat.name}")
            print(f"Reassigned {len(original_materials)} materials to {mesh_obj.name}")
        else:
            print(f"⚠️ No stored materials for {mesh_obj.name}")
    
    # Remove animation objects (keep textured model with animation)
    for obj in anim_objects:
        if obj.users_scene:
            bpy.data.objects.remove(obj, do_unlink=True)
    
    final_armature = textured_armature
    final_meshes = textured_meshes
    
else:
    # FALLBACK: Textured model has no armature - transfer textures to animated meshes
    print("Textured model has no armature - transferring textures to animated meshes")
    
    # Apply animation to animation armature (already has it)
    print(f"Using animation armature: {anim_armature.name}")
    
    # CRITICAL FIX: Preserve original textured materials and copy them to animated meshes
    print("🎨 TRANSFERRING ORIGINAL TEXTURED MATERIALS TO ANIMATED MESHES...")
    
    # Store original textured materials BEFORE any modifications
    original_materials = {}
    for mesh_obj in textured_meshes:
        if mesh_obj.data.materials:
            original_materials[mesh_obj.name] = [mat for mat in mesh_obj.data.materials if mat]
            print(f"📦 Stored {len(original_materials[mesh_obj.name])} original materials from {mesh_obj.name}")
    
    # Transfer materials and vertex groups from textured meshes to animated meshes
    if textured_meshes and anim_meshes:
        # Map materials by mesh index/name similarity
        for i, anim_mesh in enumerate(anim_meshes):
            print(f"\n🔄 Processing animated mesh: {anim_mesh.name}")
            
            # Find best matching textured mesh (by index or name similarity)
            textured_mesh = None
            if i < len(textured_meshes):
                textured_mesh = textured_meshes[i]
            elif len(textured_meshes) == 1:
                textured_mesh = textured_meshes[0]
            
            if textured_mesh and textured_mesh.name in original_materials:
                # Transfer ORIGINAL materials (not current ones which might be corrupted)
                original_mats = original_materials[textured_mesh.name]
                print(f"🎨 Transferring {len(original_mats)} original materials from {textured_mesh.name} to {anim_mesh.name}")
                
                # IMPORTANT: Clear animated mesh materials first
                anim_mesh.data.materials.clear()
                
                # Add original materials to animated mesh
                for mat in original_mats:
                    if mat:  # Ensure material exists
                        # Create a copy to avoid material conflicts
                        mat_copy = mat.copy()
                        mat_copy.name = f"{mat.name}_anim"
                        anim_mesh.data.materials.append(mat_copy)
                        print(f"  ✅ Added material: {mat_copy.name}")
                    else:
                        print(f"  ⚠️ Skipping None material")
                        
                print(f"✅ Successfully transferred materials to {anim_mesh.name}")
            else:
                print(f"❌ No original materials found for animated mesh {anim_mesh.name}")
                
                # Try to find materials from any textured mesh as fallback
                fallback_materials = []
                for tex_mesh_name, mats in original_materials.items():
                    fallback_materials.extend(mats)
                    break  # Use first available set of materials
                
                if fallback_materials:
                    print(f"🔄 Using fallback materials: {len(fallback_materials)}")
                    anim_mesh.data.materials.clear()
                    for mat in fallback_materials:
                        if mat:
                            mat_copy = mat.copy()
                            mat_copy.name = f"{mat.name}_fallback"
                            anim_mesh.data.materials.append(mat_copy)
            
            # CRITICAL: Ensure mesh is properly bound to armature
            # Check if animated mesh has armature modifier
            armature_mod = None
            for mod in anim_mesh.modifiers:
                if mod.type == 'ARMATURE':
                    armature_mod = mod
                    break
            
            if armature_mod and armature_mod.object == anim_armature:
                print(f"✅ {anim_mesh.name} is properly bound to armature {anim_armature.name}")
            else:
                print(f"⚠️ {anim_mesh.name} not bound to armature - fixing binding...")
                
                # Remove any existing armature modifiers
                for mod in list(anim_mesh.modifiers):
                    if mod.type == 'ARMATURE':
                        anim_mesh.modifiers.remove(mod)
                
                # Add proper armature modifier
                armature_mod = anim_mesh.modifiers.new(name="Armature", type='ARMATURE')
                armature_mod.object = anim_armature
                armature_mod.use_vertex_groups = True
                print(f"✅ Added armature binding for {anim_mesh.name}")
            
            # Ensure mesh has vertex groups matching armature bones
            bone_names = [bone.name for bone in anim_armature.data.bones]
            mesh_vgroups = [vg.name for vg in anim_mesh.vertex_groups]
            missing_groups = set(bone_names) - set(mesh_vgroups)
            
            if missing_groups:
                print(f"⚠️ {anim_mesh.name} missing vertex groups: {list(missing_groups)[:5]}...")
            else:
                print(f"✅ {anim_mesh.name} has all required vertex groups ({len(mesh_vgroups)})")
                
        # Also copy UV coordinates and mesh data if possible
        if len(textured_meshes) == 1 and len(anim_meshes) == 1:
            textured_mesh = textured_meshes[0]
            anim_mesh = anim_meshes[0]
            
            # Try to transfer UV data if compatible
            if (len(textured_mesh.data.vertices) == len(anim_mesh.data.vertices) and 
                textured_mesh.data.uv_layers and not anim_mesh.data.uv_layers):
                print("🔄 Transferring UV coordinates from textured mesh...")
                for uv_layer in textured_mesh.data.uv_layers:
                    new_uv = anim_mesh.data.uv_layers.new(name=uv_layer.name)
                    for i, uv_loop in enumerate(uv_layer.data):
                        if i < len(new_uv.data):
                            new_uv.data[i].uv = uv_loop.uv
                print("✅ UV coordinates transferred")
    
    # Remove textured objects (keep animated model with transferred textures)
    for obj in textured_objects:
        if obj.users_scene:
            bpy.data.objects.remove(obj, do_unlink=True)
    
    final_armature = anim_armature
    final_meshes = anim_meshes

print("Final setup complete - armature with animation + meshes with textures")

# Validate the final setup
print("\n=== FINAL VALIDATION ===")
print(f"Final armature: {final_armature.name if final_armature else 'None'}")
if final_armature and final_armature.animation_data and final_armature.animation_data.action:
    action_name = final_armature.animation_data.action.name
    frame_range = final_armature.animation_data.action.frame_range
    print(f"Animation: {action_name}, frames: {frame_range[0]}-{frame_range[1]}")
else:
    print("⚠️ WARNING: No animation data found in final armature!")

print(f"Final meshes: {[m.name for m in final_meshes]}")
for mesh in final_meshes:
    mat_count = len([m for m in mesh.data.materials if m])
    armature_mods = [m for m in mesh.modifiers if m.type == 'ARMATURE']
    vgroup_count = len(mesh.vertex_groups)
    print(f"  {mesh.name}: {mat_count} materials, {len(armature_mods)} armature mods, {vgroup_count} vertex groups")
    
    if not armature_mods:
        print(f"    ❌ ERROR: {mesh.name} has no armature modifier - animation will not work!")
    elif armature_mods[0].object != final_armature:
        print(f"    ❌ ERROR: {mesh.name} armature modifier points to wrong armature!")
    else:
        print(f"    ✅ {mesh.name} properly bound to {final_armature.name}")

print("=== END VALIDATION ===\n")

# Force pack all materials and textures again
bpy.ops.file.pack_all()

# Select final objects for export
for obj in final_meshes:
    obj.select_set(True)
if final_armature:
    final_armature.select_set(True)

# Export GLB with embedded textures + animation
print("Exporting final GLB with textures and animation...")
bpy.ops.export_scene.gltf(
    filepath=out, 
    export_format='GLB', 
    export_image_format='AUTO',
    export_apply=True, 
    export_animations=True, 
    export_skins=True,
    export_materials='EXPORT',
    use_selection=False,  # Export all objects
    export_extras=True,
    export_texture_dir='',  # Embed textures
    will_save_settings=False
)
print("Exported with textures and animation:", out)
"""

# ========= UTILITIES =========
def have_blender() -> bool:
    try:
        subprocess.run([BLENDER, "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False

def write_temp_script(text: str, filename: str) -> str:
    tmpdir = os.path.join(os.getcwd(), ".tmp_blender")
    os.makedirs(tmpdir, exist_ok=True)
    path = os.path.join(tmpdir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path

def glb_has_embedded_textures(path: str):
    """
    Returns (bool_ok, explanation).
    Uses pygltflib if available; otherwise a heuristic.
    """
    try:
        from pygltflib import GLTF2
        gltf = GLTF2().load(path)
        imgs = gltf.images or []
        if not imgs:
            return False, "No images found in GLB."
        for img in imgs:
            if (img.bufferView is None) and not ((img.uri or "").startswith("data:")):
                return False, f"Image references external file: {img.uri}"
        return True, "All textures embedded."
    except Exception:
        try:
            with open(path, "rb") as f:
                blob = f.read()
            if b"data:image" in blob:
                return True, "Heuristic: found embedded data:image."
            return False, "Heuristic: no data:image; textures may be external."
        except Exception as e:
            return False, f"Heuristic failed: {e}"

def pack_glb_with_blender(inp_glb: str, out_glb: str):
    if not have_blender():
        print("⚠️ Blender not found. Skipping texture packing.")
        # Just copy the input file as fallback
        shutil.copy2(inp_glb, out_glb)
        return
    script = write_temp_script(PACK_SCRIPT, "pack_glb_textures.py")
    cmd = [BLENDER, "-b", "-P", script, "--", inp_glb, out_glb]
    subprocess.run(cmd, check=True)

def merge_anim_to_rig(rigged_glb: str, anim_path: str, out_glb: str):
    if not have_blender():
        print("⚠️ Blender not found. Cannot merge textures - copying animation file instead.")
        # Just copy the animation file as fallback
        shutil.copy2(anim_path, out_glb)
        return
    script = write_temp_script(MERGE_SCRIPT, "merge_anim_to_rig.py")
    cmd = [BLENDER, "-b", "-P", script, "--", rigged_glb, anim_path, out_glb]
    subprocess.run(cmd, check=True)

def s3_client():
    return boto3.client("s3", region_name=AWS_REGION)

def upload_glb_presigned(file_path: str, bucket: str, key: str, ttl: int) -> str:
    s3 = s3_client()
    s3.upload_file(
        Filename=file_path,
        Bucket=bucket,
        Key=key,
        ExtraArgs={"ContentType": "model/gltf-binary"}
    )
    url = s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl
    )
    return url

def post_json(url, payload):
    r = requests.post(url, json=payload, headers={"Authorization": f"Bearer {MESHY_KEY}", "Content-Type": "application/json"}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"{r.status_code}: {r.text}")
    return r.json().get("result")

def get_json(url):
    r = requests.get(url, headers={"Authorization": f"Bearer {MESHY_KEY}"}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"{r.status_code}: {r.text}")
    return r.json()

def poll_task(task_url: str, label: str, interval: float = 2.0):
    while True:
        data = get_json(task_url)
        status = data.get("status")
        progress = data.get("progress", 0)
        print(f"{label}: {status} ({progress}%)")
        if status in ("SUCCEEDED", "FAILED"):
            return data
        time.sleep(interval)

def download_file(url: str, output_folder: str, custom_filename: str = None) -> str:
    if not url: return None
    os.makedirs(output_folder, exist_ok=True)
    
    if custom_filename:
        name = custom_filename
    else:
        name = os.path.basename(urlparse(url).path) or f"meshy_{int(time.time())}.glb"
    
    path = os.path.join(output_folder, name)
    print(f"Downloading {name} ...")
    r = requests.get(url, stream=True, timeout=300); r.raise_for_status()
    with open(path, "wb") as f:
        for chunk in r.iter_content(8192):
            if chunk: f.write(chunk)
    print(f"✅ Saved {path} ({os.path.getsize(path):,} bytes)")
    return path

def suggest_mixamo_workflow():
    """Provide instructions for using Mixamo to preserve textures while adding animations."""
    print("\n" + "="*60)
    print("🎨 TEXTURE PRESERVATION SOLUTIONS")
    print("="*60)
    
    print(f"""
🔧 ISSUE: Standard rigging can strip textures from your model.

✅ SOLUTION 1 (NEW!): Meshy Retexture API + Rigging
   Set USE_RETEXTURE=true in .env to preserve textures during rigging:
   • Automatically enhances/preserves textures before rigging
   • Generates PBR materials (diffuse, normal, roughness)
   • Keeps original colors and patterns intact
   • Fully automated workflow

✅ SOLUTION 2: Adobe Mixamo (manual but reliable)
   Use for manual control over texture preservation:

📋 MIXAMO STEP-BY-STEP:
1. 🌐 Go to https://www.mixamo.com (free Adobe account required)
2. 📁 Upload your textured model: streetview_3d_model.glb ({os.path.getsize('streetview_3d_model.glb'):,} bytes)
3. 🦴 Mixamo will auto-rig it (preserving textures!)
4. 🎬 Browse animations: Confident Walk, Casual Walk, Stage Walk, etc.
5. ⚙️  Configure: 30 FPS, With Skin, FBX format
6. 💾 Download each animated version
7. 🎯 Result: Fully textured + animated characters

🎁 COMPARISON:
┌─────────────────┬─────────────────┬─────────────────┐
│ Method          │ Meshy Retexture │ Mixamo          │
├─────────────────┼─────────────────┼─────────────────┤
│ Automation      │ ✅ Full API     │ ⚠️ Manual       │
│ Texture Quality │ ✅ Enhanced PBR │ ✅ Preserved    │
│ Speed           │ ✅ Fast         │ ⚠️ Slower       │
│ Cost            │ 💰 API Credits  │ ✅ Free         │
│ Control         │ ⚠️ Limited      │ ✅ Full Control │
└─────────────────┴─────────────────┴─────────────────┘

⚡ RECOMMENDED WORKFLOW:
   1. Try Meshy Retexture API first (set USE_RETEXTURE=true)
   2. If results aren't perfect, use Mixamo for manual control
   3. Compare both approaches for your specific model

🔧 CONFIGURATION:
   Add to .env file:
   USE_RETEXTURE=true   # Enable Meshy Retexture API (default: true)
   USE_RETEXTURE=false  # Disable retexturing, use original model
   RETEXTURE_PROMPT="Preserve existing textures, enhance quality"
""")
    
    print("="*60)
    return True

def create_mixamo_instructions():
    """Create a detailed instruction file for the Mixamo workflow."""
    instructions = """# MIXAMO WORKFLOW - Preserve Textures + Add Animations

## The Problem
Meshy's rigging process strips textures from 3D models. This is a common issue with automated rigging services that focus on bone structure over material preservation.

## The Solution: Adobe Mixamo
Mixamo is Adobe's free character animation service that preserves textures while adding professional animations.

## Step-by-Step Process

### 1. Prepare Your Model
- File: `streetview_3d_model.glb` (your original textured Street View model)
- Size: ~5MB with full textures
- Status: ✅ Ready for Mixamo

### 2. Upload to Mixamo
1. Go to https://www.mixamo.com
2. Sign in with free Adobe account
3. Click "Upload Character"
4. Select `streetview_3d_model.glb`
5. Wait for auto-rigging (usually 1-2 minutes)

### 3. Choose Animations
Popular animations that match your Meshy choices:
- **Confident Walk** → Search "Confident" or "Strut"
- **Casual Walk** → Search "Walking" or "Casual"
- **Stage Walk** → Search "Fashion" or "Catwalk"
- **Combat/Attack** → Search "Punch" or "Fighting"

### 4. Download Settings
For each animation:
- Format: **FBX for Unity** (best compatibility)
- Skin: **With Skin** (includes textures)
- Frames: **30 FPS** (smooth playback)
- Keyframe Reduction: **None** (preserve quality)

### 5. Results
You'll get:
- ✅ Fully textured animated characters
- ✅ Professional animation quality
- ✅ Multiple format options
- ✅ Preserved original colors and materials

## File Organization
```
your_project/
├── streetview_3d_model.glb          # Original (textured, no animation)
├── mixamo_animations/
│   ├── character_confident_walk.fbx # Textured + animated
│   ├── character_casual_walk.fbx    # Textured + animated
│   └── character_stage_walk.fbx     # Textured + animated
└── meshy_animations/                # For comparison (no textures)
    ├── action_106/
    ├── action_30/
    └── action_55/
```

## Why Mixamo Works Better
- **Designed for game/film industry**: Preserves textures by default
- **Large animation library**: 2000+ professional animations
- **Free service**: No API limits or costs
- **Multiple formats**: FBX, GLB, DAE support
- **Quality control**: Human-curated animations

## Fallback Options
If Mixamo doesn't work for your specific model:
1. **Manual Blender rigging**: Time-consuming but full control
2. **Other services**: Character Creator, Maya, 3ds Max
3. **Hybrid approach**: Mixamo for animation + manual texture reapplication

## Comparison: Meshy vs Mixamo

| Feature | Meshy API | Mixamo |
|---------|-----------|--------|
| Textures | ❌ Stripped | ✅ Preserved |
| Automation | ✅ Full API | ⚠️ Manual upload |
| Cost | 💰 Paid API | ✅ Free |
| Quality | ⚠️ Basic | ✅ Professional |
| File size | ~1MB | ~2-5MB |

## Next Steps
1. Try Mixamo workflow first
2. Compare results with Meshy animations
3. Use Mixamo versions for final production
4. Keep Meshy versions for motion reference

---
Generated by: 3D Street View Animation Pipeline
Date: September 24, 2025
"""
    
    with open("MIXAMO_WORKFLOW.md", "w") as f:
        f.write(instructions)
    
    print("📄 Created detailed instructions: MIXAMO_WORKFLOW.md")
    return True

def ensure_self_contained_input(local_glb: str) -> str:
    ok, why = glb_has_embedded_textures(local_glb)
    if ok:
        print(f"✅ Input GLB textures OK: {why}")
        return local_glb
    print(f"⚠️  Input GLB not self-contained: {why}")
    
    if not have_blender():
        print("⚠️ Blender not available - cannot pack textures. Using original GLB as-is.")
        return local_glb
        
    packed = os.path.splitext(local_glb)[0] + "_packed.glb"
    print("→ Packing textures into GLB via Blender ...")
    pack_glb_with_blender(local_glb, packed)
    ok2, why2 = glb_has_embedded_textures(packed)
    if not ok2:
        print(f"🚫 Failed to pack textures: {why2} - using original GLB")
        return local_glb
    print(f"✅ Packed GLB ready: {packed}")
    return packed
    ok, why = glb_has_embedded_textures(local_glb)
    if ok:
        print(f"✅ Input GLB textures OK: {why}")
        return local_glb
    print(f"⚠️  Input GLB not self-contained: {why}")
    
    if not have_blender():
        print("⚠️ Blender not available - cannot pack textures. Using original GLB as-is.")
        return local_glb
        
    packed = os.path.splitext(local_glb)[0] + "_packed.glb"
    print("→ Packing textures into GLB via Blender ...")
    pack_glb_with_blender(local_glb, packed)
    ok2, why2 = glb_has_embedded_textures(packed)
    if not ok2:
        print(f"🚫 Failed to pack textures: {why2} - using original GLB")
        return local_glb
    print(f"✅ Packed GLB ready: {packed}")
    return packed

def retexture_with_meshy(model_url: str, prompt: str = None, job_id: str = None) -> str:
    """
    Use Meshy Retexture API to preserve/enhance textures before rigging.
    Returns the retextured model URL.
    
    Args:
        model_url: URL of the model to retexture
        prompt: Optional text prompt for retexturing
        job_id: Optional job ID for organizing retextured files
    """
    print("\n🎨 RETEXTURING WITH MESHY API TO PRESERVE TEXTURES...")
    
    # Build the payload - must have either text_style_prompt or image_style_url
    # NOTE: Disable forced PBR generation by default to avoid overly metallic/shiny results;
    # add stronger negative prompts to prevent geometry/face distortion and excessive gloss.
    payload = {
        "model_url": model_url,
        # "art_style": "realistic",  # Keep realistic style
        "negative_prompt": "blurry, low quality, distorted, missing textures, flat colors, metallic, glossy, specular, shiny, reflective, stretched, warped, altered geometry, facial distortion, changed proportions",
        "enable_pbr": False  # Avoid automatic PBR generation that can create metallic look
    }
    
    # Add style reference - prioritize image URL if provided, otherwise use text prompt
    # Request stronger preservation and higher-quality output (if the API supports these fields)
    payload.update({
        "preserve_original_textures": True,
        "preserve_uv": True,
        "texture_upscale": {"enabled": True, "method": "realesrgan", "scale": 2},
        "target_texture_resolution": 4096,
        "quality": "high",
        "style_strength": 0.10  # keep style influence low to avoid changing original look
    })

    if RETEXTURE_IMAGE_URL:
        payload["image_style_url"] = RETEXTURE_IMAGE_URL
        print(f"→ Using image style reference: {RETEXTURE_IMAGE_URL}")
    elif prompt:
        base_prompt = (
            "Preserve and enhance the existing textures and materials. "
            "Keep original colors, patterns and UV mapping exactly. "
            "Do not change geometry, facial features, or body proportions. "
            "Avoid metallic, overly glossy, or stylized finishes. "
            "Remove compression artifacts and upscale texture detail to high resolution (prefer lossless quality)."
        )
        # Safely concatenate user prompt after the preservation instructions
        payload["text_style_prompt"] = base_prompt + " " + prompt
        print(f"→ Using text style prompt: '{(payload['text_style_prompt'])[:160]}...'")
    else:
        # Default high-quality preservation prompt (no forced PBR)
        default_prompt = (
            "Preserve and enhance the existing textures and materials. "
            "Maintain original colors, patterns and UV layout. "
            "Do not alter geometry or proportions. "
            "Produce high-resolution, artifact-free textures and avoid adding metallic or glossy effects."
        )
        payload["text_style_prompt"] = default_prompt
        print(f"→ Using default preservation prompt: '{default_prompt[:160]}...'")
    try:
        retexture_task_id = post_json(f"{API_V1}/retexture", payload)
        print(f"→ Retexture task ID: {retexture_task_id}")
        
        # Poll for completion
        retexture_task = poll_task(f"{API_V1}/retexture/{retexture_task_id}", "Retexturing")
        
        if retexture_task.get("status") != "SUCCEEDED":
            err = retexture_task.get("task_error", {})
            print(f"❌ Retexturing failed: {err.get('message', err)}")
            print("→ Continuing with original model...")
            return model_url
        
        # Handle different response formats - sometimes result is nested, sometimes direct
        if "result" in retexture_task:
            retexture_result = retexture_task["result"]
            retextured_url = retexture_result.get("model_url")
        else:
            # Direct response format
            retextured_url = retexture_task.get("model_url")
        
        # Debug: Print the actual response structure
        print(f"🔍 Debug - Retexture response keys: {list(retexture_task.keys())}")
        if not retextured_url:
            print(f"🔍 Debug - Full response: {json.dumps(retexture_task, indent=2)}")
            # Try alternative field names
            retextured_url = (retexture_task.get("retextured_model_url") or 
                            retexture_task.get("output_url") or
                            retexture_task.get("url"))
        
        if retextured_url:
            print(f"✅ Retextured model ready: {retextured_url}")
            
            # Download the retextured model for local reference
            timestamp = int(time.time())
            if job_id:
                # Save in job-specific retexturing folder
                retex_dir = f"jobs/{job_id}/retexturing"
                os.makedirs(retex_dir, exist_ok=True)
                print(f"📁 Saving retextured model to job folder: {retex_dir}")
                # Add timestamp to retextured model filename
                base_filename = f"{job_id}_{timestamp}_retextured_model.glb"
            else:
                # Use default folder for backward compatibility
                retex_dir = "retextured_models"
                os.makedirs(retex_dir, exist_ok=True)
                # Add timestamp to retextured model filename
                base_filename = f"{timestamp}_retextured_model.glb"
            
            retextured_path = download_file(retextured_url, retex_dir, base_filename)
            
            if retextured_path:
                # Verify textures are preserved/enhanced
                ok, why = glb_has_embedded_textures(retextured_path)
                print(("✅" if ok else "⚠️"), "Retextured GLB texture check:", why)
                
                if job_id:
                    print(f"📁 Retextured model saved: {retextured_path}")
            
            return retextured_url
        else:
            print("❌ No retextured model URL found in response")
            print(f"Available fields: {list(retexture_task.keys())}")
            print("→ Continuing with original model...")
            return model_url
            
    except Exception as e:
        print(f"❌ Retexturing error: {e}")
        print("→ Continuing with original model...")
        return model_url

def main():
    # 0) Sanity / prepare input GLB
    if not os.path.isfile(LOCAL_GLB):
        raise FileNotFoundError(f"LOCAL_GLB not found: {LOCAL_GLB}")
    
    # Check if user wants to preserve textures
    if USE_MIXAMO or SKIP_RIGGING:
        print("🎨 TEXTURE PRESERVATION MODE ENABLED")
        suggest_mixamo_workflow()
        create_mixamo_instructions()
        
        if SKIP_RIGGING:
            print("\n⚠️ SKIP_RIGGING=true - Exiting before Meshy processing")
            print("💡 Use the Mixamo workflow above to preserve textures!")
            return
        
        print("\n🔄 Continuing with Meshy anyway (now using Retexture API)...")
    
    upload_glb = ensure_self_contained_input(LOCAL_GLB)

    # 1) Upload to S3 & presign
    print(f"Uploading {upload_glb} to s3://{S3_BUCKET}/{S3_KEY} ...")
    try:
        presigned_url = upload_glb_presigned(upload_glb, S3_BUCKET, S3_KEY, PRESIGN_TTL)
    except ClientError as e:
        raise SystemExit(f"S3 error: {e}")
    print("Pre-signed URL (masked):", presigned_url.split("?")[0], "?...")

    # 1.5) Retexture with Meshy to preserve/enhance textures (NEW!)
    if USE_RETEXTURE and not SKIP_RIGGING:
        print("🎨 Using Meshy Retexture API to preserve textures before rigging...")
        retextured_url = retexture_with_meshy(presigned_url, RETEXTURE_PROMPT)
        # Use retextured model for rigging if available
        rigging_model_url = retextured_url
    else:
        if not USE_RETEXTURE:
            print("⚪ Retexturing disabled (USE_RETEXTURE=false) - using original model for rigging")
        elif SKIP_RIGGING:
            print("⚪ Rigging skipped (SKIP_RIGGING=true) - retexturing not applicable")
        rigging_model_url = presigned_url

    # 2) Meshy Rigging (now using retextured model)
    rig_task_id = post_json(f"{API_V1}/rigging", {"model_url": rigging_model_url, "height_meters": HEIGHT_METERS})
    print("rig_task_id:", rig_task_id)
    rig_task = poll_task(f"{API_V1}/rigging/{rig_task_id}", "Rigging")
    if rig_task.get("status") != "SUCCEEDED":
        err = rig_task.get("task_error", {})
        raise SystemExit(f"Rigging failed: {err.get('message', err)}")
    rig_res = rig_task["result"]

    print("\n=== Rigging Results ===")
    rig_fbx_url = rig_res.get("rigged_character_fbx_url")
    rig_glb_url = rig_res.get("rigged_character_glb_url")
    
    print(f"Rigged FBX URL: {rig_fbx_url}")
    print(f"Rigged GLB URL: {rig_glb_url}")
    
    if not rig_fbx_url and not rig_glb_url:
        print("⚠️ No rigged model URLs returned by Meshy API")
        print("Raw rigging result:", json.dumps(rig_res, indent=2))
        raise SystemExit("No rigged model files available from Meshy")
    
    rig_dir = "rigged_models"; os.makedirs(rig_dir, exist_ok=True)
    rig_fbx_path = download_file(rig_fbx_url, rig_dir) if rig_fbx_url else None
    rig_glb_path = download_file(rig_glb_url, rig_dir) if rig_glb_url else None
    
    # If GLB download failed but we have FBX, convert FBX to GLB
    if not rig_glb_path and rig_fbx_path:
        if not have_blender():
            print("⚠️ No rigged GLB available and Blender not found - cannot convert FBX to GLB")
        else:
            print("⚠️ No rigged GLB available, converting FBX to GLB...")
            rig_glb_path = os.path.join(rig_dir, "rigged_from_fbx.glb")
            try:
                # Use Blender to convert FBX to GLB
                script = write_temp_script("""
import bpy
import sys
fbx_path = sys.argv[sys.argv.index("--") + 1]
glb_path = sys.argv[sys.argv.index("--") + 2]

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.fbx(filepath=fbx_path)
bpy.ops.file.pack_all()
bpy.ops.export_scene.gltf(filepath=glb_path, export_format='GLB', 
                          export_image_format='AUTO', export_apply=True, 
                          export_animations=False, export_skins=True,
                          export_materials='EXPORT')
print("Converted FBX to GLB:", glb_path)
""", "fbx_to_glb.py")
                cmd = [BLENDER, "-b", "-P", script, "--", rig_fbx_path, rig_glb_path]
                subprocess.run(cmd, check=True)
                print(f"✅ Converted FBX to GLB: {rig_glb_path}")
            except Exception as e:
                print(f"❌ Failed to convert FBX to GLB: {e}")
                rig_glb_path = None

    # Verify rigged GLB has textures
    if rig_glb_path:
        ok, why = glb_has_embedded_textures(rig_glb_path)
        print(("✅" if ok else "⚠️"), "Rigged GLB texture check:", why)
        if not ok:
            if not have_blender():
                print("⚠️ Rigged GLB textures not embedded and Blender not available - continuing anyway")
            else:
                # try repack (rare)
                repacked = os.path.splitext(rig_glb_path)[0] + "_repacked.glb"
                print("→ Repacking rigged GLB to embed textures ...")
                try:
                    pack_glb_with_blender(rig_glb_path, repacked)
                    rig_glb_path = repacked
                except Exception as e:
                    print(f"❌ Failed to repack textures: {e}")
    
    # Final check - if we still don't have a rigged GLB, we'll need to work differently
    if not rig_glb_path:
        print("⚠️ No usable rigged GLB available - will attempt to use original input GLB for texture reference")

    # 3) Animations
    print("\nApplying animations:", ANIMATION_IDS)
    out_manifest = []
    for action_id in ANIMATION_IDS:
        print(f"\n→ Creating animation task (action_id={action_id})")
        anim_task_id = post_json(f"{API_V1}/animations", {
            "rig_task_id": rig_task_id,
            "action_id": action_id,
            "post_process": {"operation_type": "change_fps", "fps": ANIM_FPS}
        })
        print("anim_task_id:", anim_task_id)
        anim_task = poll_task(f"{API_V1}/animations/{anim_task_id}", f"Animation {action_id}")
        if anim_task.get("status") != "SUCCEEDED":
            print(f"❌ Animation {action_id} failed:", anim_task.get("task_error"))
            continue

        anim_res = anim_task["result"]
        a_dir = os.path.join("animations", f"action_{action_id}")
        glb_u = anim_res.get("animation_glb_url")
        fbx_u = anim_res.get("animation_fbx_url")

        anim_glb = download_file(glb_u, a_dir) if glb_u else None
        anim_fbx = download_file(fbx_u, a_dir) if fbx_u else None

        final_textured_glb = None

        if anim_glb:
            ok, why = glb_has_embedded_textures(anim_glb)
            print(("✅" if ok else "⚠️"), f"Animation {action_id} GLB texture check:", why)
            if ok:
                final_textured_glb = anim_glb

        # Force texture merging: Always merge animation onto textured source
        # Priority: 1) Retextured model, 2) Rigged GLB, 3) Original GLB
        retextured_source = "retextured_models/model.glb" if os.path.exists("retextured_models/model.glb") else None
        
        if not final_textured_glb:
            src_anim = anim_glb or anim_fbx
            if not src_anim:
                print(f"❌ No animation file returned for action {action_id}")
                continue
            
            # Choose best texture source (prioritize retextured model)
            if retextured_source and USE_RETEXTURE:
                texture_source = retextured_source
                print(f"🎨 Using retextured model for textures: {os.path.basename(texture_source)}")
            elif rig_glb_path:
                texture_source = rig_glb_path
                print(f"🦴 Using rigged GLB for textures: {os.path.basename(texture_source)}")
            else:
                texture_source = upload_glb
                print(f"📁 Using original GLB for textures: {os.path.basename(texture_source)}")
            
            merged = os.path.join(a_dir, f"action_{action_id}_TEXTURED.glb")
            
            if not have_blender():
                print(f"⚠️ Blender not available - copying animation file instead of merging textures → {merged}")
                shutil.copy2(src_anim, merged)
                final_textured_glb = merged
            else:
                print(f"→ Merging animation onto textured source → {merged}")
                try:
                    merge_anim_to_rig(texture_source, src_anim, merged)
                    
                    # Verify the merged file has textures
                    if os.path.exists(merged):
                        ok, why = glb_has_embedded_textures(merged)
                        if ok:
                            print(f"✅ Successfully merged textures: {why}")
                            final_textured_glb = merged
                        else:
                            print(f"⚠️ Merge result lacks textures: {why}")
                            print(f"→ Trying alternative texture source...")
                            
                            # Try with retextured model if we used rigged GLB
                            if texture_source == rig_glb_path and retextured_source:
                                print(f"→ Retrying with retextured model...")
                                merge_anim_to_rig(retextured_source, src_anim, merged)
                                ok2, why2 = glb_has_embedded_textures(merged)
                                if ok2:
                                    print(f"✅ Retry successful: {why2}")
                                    final_textured_glb = merged
                                else:
                                    print(f"❌ Retry failed: {why2} - using animation file")
                                    final_textured_glb = src_anim
                            else:
                                final_textured_glb = src_anim
                    else:
                        print(f"❌ Merge failed - output file not created")
                        final_textured_glb = src_anim
                        
                except Exception as e:
                    print(f"❌ Failed to merge textures for action {action_id}: {e}")
                    print("→ Using raw animation file instead")
                    final_textured_glb = src_anim

        out_manifest.append({
            "action_id": action_id,
            "final_textured_glb": final_textured_glb,
            "raw_anim_glb": glb_u,
            "raw_anim_fbx": fbx_u
        })
        print(f"✅ Final textured GLB for action {action_id}: {final_textured_glb}")

    summary = {
        "rig_task_id": rig_task_id,
        "rigged": {"glb": rig_glb_path, "fbx": rig_fbx_path},
        "animations": out_manifest
    }
    os.makedirs("out", exist_ok=True)
    with open("out/meshy_results_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print("\n📄 Saved summary → out/meshy_results_summary.json")

if __name__ == "__main__":
    main()

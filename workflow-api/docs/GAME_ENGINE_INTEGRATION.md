# 🎮 Game Engine Integration Guide

## Overview

The Workflow API now supports direct integration with major game engines, allowing you to generate assets via AI workflows and export them in engine-specific formats ready for import.

## Supported Game Engines

### 1. 🎮 Cocos2d (Cocos2d-x & Cocos Creator)
- Scene JSON export with sprite hierarchy
- Texture atlas generation (Plist/JSON)
- Physics body configuration (Box2D/Chipmunk)
- Multi-resolution support (@1x, @2x, @3x)
- Code generation (C++/JS/TS)

### 2. 🔷 Unity
- Prefab & scene export
- URP/HDRP material support
- LOD levels & mesh optimization
- Collider generation
- Unity Package creation

### 3. 🔶 Unreal Engine
- Static mesh & texture import settings
- Nanite & Lumen support (UE5+)
- PBR material workflows
- Blueprint class generation
- Multiple texture compression formats

### 4. 🎯 Godot Engine
- TSCN scene format export
- Sprite2D/Node3D hierarchy
- GDScript skeleton generation
- Physics layers & collision shapes
- Godot 3.x & 4.x support

### 5. 🖼️ Universal Sprite Sheets
- MaxRects/Guillotine/Shelf packing algorithms
- Multi-resolution atlas generation
- JSON/Plist/XML metadata formats
- Automatic transparency trimming
- Compatible with any 2D engine

### 6. 🗺️ Engine-Agnostic Scene Export
- Universal JSON/glTF format
- Transform hierarchy preservation
- Material & lighting data
- Camera configurations
- Asset manifest generation

## Example Workflows

### Example 1: Character to Cocos2d Scene

```json
{
  "nodes": [
    {
      "id": "prompt",
      "type": "text_prompt",
      "config": {
        "text": "pixel art game character, warrior with sword, transparent background, 8-bit style"
      }
    },
    {
      "id": "generate",
      "type": "generate_nano_banana_pro",
      "config": {
        "output_format": "png",
        "resolution": "2K"
      }
    },
    {
      "id": "export",
      "type": "export_cocos2d",
      "config": {
        "scene_name": "PlayerCharacterScene",
        "export_format": "cocos_creator",
        "sprite_resolution": "hd",
        "generate_atlas": true,
        "atlas_format": "plist",
        "coordinate_system": "bottom_left",
        "physics_engine": "box2d",
        "export_code": true
      }
    }
  ],
  "edges": [
    { "source": "prompt", "target": "generate" },
    { "source": "generate", "target": "export" }
  ]
}
```

**Output:**
- Scene JSON with sprite hierarchy
- Texture atlas (Plist + PNG)
- TypeScript loading code
- Physics colliders configured

---

### Example 2: Multiple Assets to Unity

```json
{
  "nodes": [
    {
      "id": "character-prompt",
      "type": "text_prompt",
      "config": {
        "text": "3D low-poly game character, knight, stylized"
      }
    },
    {
      "id": "environment-prompt",
      "type": "text_prompt",
      "config": {
        "text": "game environment texture, stone wall, seamless, PBR"
      }
    },
    {
      "id": "generate-character",
      "type": "generate_3d",
      "config": {}
    },
    {
      "id": "generate-texture",
      "type": "generate_nano_banana_pro",
      "config": {
        "output_format": "png"
      }
    },
    {
      "id": "export-unity",
      "type": "export_unity",
      "config": {
        "unity_version": "2022",
        "asset_type": "prefab",
        "render_pipeline": "urp",
        "material_type": "standard",
        "include_colliders": true,
        "lod_levels": 3,
        "generate_package": true
      }
    }
  ],
  "edges": [
    { "source": "character-prompt", "target": "generate-character" },
    { "source": "environment-prompt", "target": "generate-texture" },
    { "source": "generate-character", "target": "export-unity" },
    { "source": "generate-texture", "target": "export-unity" }
  ]
}
```

**Output:**
- Unity prefab with LODs
- PBR materials configured
- Mesh colliders added
- .unitypackage ready for import

---

### Example 3: Sprite Sheet for Any 2D Engine

```json
{
  "nodes": [
    {
      "id": "frame1",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "game sprite frame 1, character walking, pixel art",
        "output_format": "png"
      }
    },
    {
      "id": "frame2",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "game sprite frame 2, character walking, pixel art",
        "output_format": "png"
      }
    },
    {
      "id": "frame3",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "game sprite frame 3, character walking, pixel art",
        "output_format": "png"
      }
    },
    {
      "id": "frame4",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "game sprite frame 4, character walking, pixel art",
        "output_format": "png"
      }
    },
    {
      "id": "spritesheet",
      "type": "create_spritesheet",
      "config": {
        "layout_algorithm": "maxrects",
        "atlas_size": "2048",
        "padding": 2,
        "trim_transparent": true,
        "output_format": "json",
        "generate_multiple_scales": true
      }
    }
  ],
  "edges": [
    { "source": "frame1", "target": "spritesheet" },
    { "source": "frame2", "target": "spritesheet" },
    { "source": "frame3", "target": "spritesheet" },
    { "source": "frame4", "target": "spritesheet" }
  ]
}
```

**Output:**
- Optimized texture atlas (PNG)
- Sprite metadata (JSON)
- @1x, @2x, @3x versions
- Ready for any 2D engine

---

### Example 4: Complete Game Scene Export

```json
{
  "nodes": [
    {
      "id": "background",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "game background, fantasy forest, vibrant colors"
      }
    },
    {
      "id": "character",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "game character, elf archer, transparent background"
      }
    },
    {
      "id": "enemy",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "game enemy, goblin warrior, transparent background"
      }
    },
    {
      "id": "scene",
      "type": "export_game_scene",
      "config": {
        "scene_format": "json",
        "include_metadata": ["transforms", "materials", "lighting", "cameras"],
        "asset_bundling": "separate",
        "coordinate_system": "right_handed_y_up",
        "optimize_assets": true,
        "generate_manifest": true
      }
    }
  ],
  "edges": [
    { "source": "background", "target": "scene" },
    { "source": "character", "target": "scene" },
    { "source": "enemy", "target": "scene" }
  ]
}
```

**Output:**
- Complete scene JSON
- All assets referenced
- Transform hierarchy
- Material definitions
- Lighting & camera setup
- Asset manifest

---

### Example 5: Unreal Engine with Nanite

```json
{
  "nodes": [
    {
      "id": "model",
      "type": "generate_3d",
      "config": {
        "prompt": "high-detail fantasy castle model"
      }
    },
    {
      "id": "export",
      "type": "export_unreal",
      "config": {
        "unreal_version": "ue5_4",
        "nanite_enabled": true,
        "lumen_enabled": true,
        "material_workflow": "pbr",
        "collision_complexity": "complex",
        "texture_resolution": "4096",
        "generate_blueprint": true
      }
    }
  ],
  "edges": [
    { "source": "model", "target": "export" }
  ]
}
```

**Output:**
- Nanite-enabled static mesh
- Lumen-compatible materials
- Complex collision mesh
- 4K PBR textures
- Blueprint actor class

---

### Example 6: Godot Scene with Physics

```json
{
  "nodes": [
    {
      "id": "player",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "2D game player character sprite, top-down view"
      }
    },
    {
      "id": "obstacle",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "2D game obstacle, rock, top-down view"
      }
    },
    {
      "id": "export",
      "type": "export_godot",
      "config": {
        "godot_version": "4",
        "scene_format": "tscn",
        "node_structure": "node2d",
        "physics_layer": 1,
        "export_scripts": true,
        "compress_textures": "lossless"
      }
    }
  ],
  "edges": [
    { "source": "player", "target": "export" },
    { "source": "obstacle", "target": "export" }
  ]
}
```

**Output:**
- Godot .tscn scene file
- Sprite2D nodes configured
- CollisionShape2D added
- GDScript skeleton code
- Physics layers set

---

## API Endpoints

All game engine export nodes are accessible through the workflow execution endpoint:

```bash
POST http://localhost:8001/workflow/execute
```

### Request Body
```json
{
  "nodes": [...],
  "edges": [...]
}
```

### Response
```json
{
  "success": true,
  "execution_id": "exec_abc123",
  "status": "queued",
  "message": "Workflow execution started"
}
```

### Check Status
```bash
GET http://localhost:8001/workflow/execution/{execution_id}
```

---

## Configuration Reference

### Cocos2d Export Node

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `scene_name` | string | "GameScene" | Name of the exported scene |
| `export_format` | select | "cocos_creator" | cocos2d_x, cocos_creator, cocos2d_js |
| `include_assets` | multi-select | ["sprites", "spritesheets"] | Asset types to include |
| `sprite_resolution` | select | "hd" | sd, hd, full_hd, ultra_hd |
| `generate_atlas` | boolean | true | Pack sprites into texture atlas |
| `atlas_format` | select | "plist" | plist, json, tp |
| `layer_structure` | boolean | true | Preserve layer hierarchy |
| `coordinate_system` | select | "bottom_left" | bottom_left, top_left, center |
| `physics_engine` | select | "none" | none, box2d, chipmunk |
| `export_code` | boolean | false | Generate scene loading code |

### Unity Export Node

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `unity_version` | select | "2022" | 2021, 2022, 2023, 6 |
| `asset_type` | select | "prefab" | prefab, scene, scriptable_object, material |
| `render_pipeline` | select | "urp" | built_in, urp, hdrp |
| `material_type` | select | "standard" | standard, unlit, toon, sprite |
| `include_colliders` | boolean | true | Add collision shapes |
| `lod_levels` | select | "3" | 0, 3, 4 |
| `texture_format` | select | "auto" | auto, rgba32, dxt5, astc |
| `generate_package` | boolean | true | Create .unitypackage |

### Sprite Sheet Node

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `layout_algorithm` | select | "maxrects" | maxrects, guillotine, shelf |
| `atlas_size` | select | "2048" | 512, 1024, 2048, 4096 |
| `padding` | number | 2 | Padding between sprites (px) |
| `trim_transparent` | boolean | true | Remove transparent borders |
| `output_format` | select | "json" | json, plist, xml, spine |
| `generate_multiple_scales` | boolean | false | Generate @1x, @2x, @3x |

---

## Integration Tips

### 1. **Asset Naming Convention**
Use consistent naming in your workflow for better organization:
```json
{
  "config": {
    "sprite_name": "player_character_idle"
  }
}
```

### 2. **Resolution Planning**
Choose appropriate resolutions based on target platform:
- Mobile: 1K-2K textures
- Desktop: 2K-4K textures
- Console: 4K-8K textures

### 3. **Batch Processing**
Connect multiple generation nodes to a single export node for batch processing:
```
[Gen1] ──┐
[Gen2] ──┼──> [Export]
[Gen3] ──┘
```

### 4. **Coordinate System Conversion**
Be aware of different engine coordinate systems:
- **Cocos2d**: Bottom-left origin
- **Unity**: Center origin, Y-up
- **Unreal**: Left-handed, Z-up
- **Godot**: Top-left (2D), Y-up (3D)

### 5. **Physics Setup**
Add physics metadata early in the workflow for automatic collider generation:
```json
{
  "config": {
    "physics_engine": "box2d",
    "collision_complexity": "simple"
  }
}
```

---

## Real-World Use Cases

### 1. **Rapid Prototyping**
Generate placeholder assets for game prototypes:
```
Text Prompt → Generate Assets → Export to Engine → Instant Prototype
```

### 2. **Asset Variations**
Create multiple variations of assets quickly:
```
Base Prompt → [Variation 1, 2, 3, ...] → Sprite Sheet → Game Ready
```

### 3. **Dynamic Content**
Generate game content at runtime or based on player actions:
```
Player Action → API Call → Generate Asset → Load into Game
```

### 4. **Cross-Platform Export**
Export the same assets to multiple engines:
```
Generate Asset → [Cocos2d Export, Unity Export, Godot Export]
```

### 5. **Animation Workflows**
Create frame-by-frame animations:
```
[Frame 1, 2, 3, ...] → Sprite Sheet → Animation Data
```

---

## Troubleshooting

### Assets Not Appearing in Engine
- **Check asset URLs**: Ensure S3 URLs are accessible
- **Verify format**: Confirm engine supports the export format
- **Check coordinates**: Adjust coordinate system in export config

### Sprite Sheet Packing Issues
- **Increase atlas size**: Try larger atlas (2048 → 4096)
- **Reduce padding**: Lower padding value if space is tight
- **Trim transparency**: Enable trim to save space

### Physics Not Working
- **Verify collision layers**: Check layer configuration
- **Test collider shapes**: Ensure proper collider type (box/circle/polygon)
- **Check engine settings**: Confirm physics engine is enabled

### Material Issues
- **Check texture formats**: Ensure textures are in supported format
- **Verify material type**: Match material to render pipeline
- **Test shader compatibility**: Confirm shader is available in target engine

---

## Next Steps

1. **Test with Simple Workflow**: Start with a single asset export
2. **Explore Batch Processing**: Connect multiple nodes to one export
3. **Customize Export Settings**: Adjust configs for your specific needs
4. **Integrate with Game Engine**: Import exported assets and test in-engine
5. **Automate Pipeline**: Build automated workflows for production

---

## Support

For issues or questions:
- Check workflow execution logs
- Verify node configurations
- Review asset URLs and formats
- Test with minimal workflow first

Happy game development! 🎮✨

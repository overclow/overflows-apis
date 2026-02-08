# 🖼️ Base64 Image Support in Nano Banana Pro

## Overview

The Nano Banana Pro node now automatically handles base64-encoded images by uploading them to S3 and converting them to presigned URLs. This ensures compatibility with Replicate's API which requires HTTP/HTTPS URLs.

## How It Works

### Before
```json
{
  "image_input": "data:image/png;base64,iVBORw0KGgoAAAANS..."
}
```
❌ **Error**: Replicate doesn't accept base64 data URIs

### After (Automatic)
```json
{
  "image_input": "data:image/png;base64,iVBORw0KGgoAAAANS..."
}
```
✅ **Automatically**:
1. Detects base64 format
2. Uploads to S3: `nano_banana_input/{uuid}.png`
3. Generates presigned URL (1 hour expiry)
4. Uses URL with Replicate API

## Supported Formats

### Single Base64 Image
```json
{
  "nodes": [
    {
      "id": "generate",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "enhance this image",
        "image_input": "data:image/png;base64,iVBORw0KGg..."
      }
    }
  ]
}
```

### Multiple Base64 Images in Array
```json
{
  "nodes": [
    {
      "id": "generate",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "blend these images",
        "image_input": [
          "data:image/png;base64,iVBORw0KGg...",
          "data:image/jpeg;base64,/9j/4AAQSkZ...",
          "https://example.com/image3.png"
        ]
      }
    }
  ]
}
```
The node will:
- Upload first two base64 images to S3
- Keep the third URL as-is
- Pass all as URLs to Replicate

### JSON String Array
```json
{
  "nodes": [
    {
      "id": "generate",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "process images",
        "image_input": "[\"data:image/png;base64,iVBORw0...\", \"https://example.com/img.png\"]"
      }
    }
  ]
}
```

### Raw Base64 (no data URI prefix)
```json
{
  "nodes": [
    {
      "id": "generate",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "enhance",
        "image_input": "iVBORw0KGgoAAAANS..."
      }
    }
  ]
}
```
Automatically detected and uploaded as PNG.

## Detection Logic

The node checks for base64 in this order:

1. **Data URI format**: `data:image/png;base64,...`
   - Extracts MIME type for correct file extension
   - Supports: png, jpg/jpeg, webp, gif

2. **Raw base64**: No `http://` or `https://` prefix
   - Assumes PNG format
   - Uploads with `.png` extension

3. **HTTP/HTTPS URLs**: Pass through unchanged
   - Already in correct format for Replicate

## Time Travel Node Support

The same base64 handling is also available in the `time_travel_scene` node:

```json
{
  "nodes": [
    {
      "id": "time-travel",
      "type": "time_travel_scene",
      "config": {
        "image_url": "data:image/png;base64,iVBORw0KGg...",
        "target_year": "1920",
        "era_style": "victorian"
      }
    }
  ]
}
```

## Logs

When base64 images are detected, you'll see:

```
🔄 [WORKFLOW] Detected base64 image_input, uploading to S3...
✅ [WORKFLOW] Image uploaded to S3: nano_banana_input/abc123.png
🔗 Presigned URL: https://s3.us-west-1.amazonaws.com/mapapi/nano...
```

For multiple images:
```
🔄 [WORKFLOW] Detected base64 image #1 in list, uploading to S3...
✅ [WORKFLOW] Image #1 uploaded to S3: nano_banana_input/abc123.png
🔗 Presigned URL: https://s3.us-west-1.amazonaws.com/mapapi/nano...
🔄 [WORKFLOW] Detected base64 image #2 in list, uploading to S3...
✅ [WORKFLOW] Image #2 uploaded to S3: nano_banana_input/def456.jpg
🔗 Presigned URL: https://s3.us-west-1.amazonaws.com/mapapi/nano...
📎 Using 2 valid reference image(s)
```

## Error Handling

If base64 processing fails:

```
⚠️ Failed to process base64 image #2: Invalid base64 string
```

The node will:
- Skip the invalid image
- Continue processing other images
- Show warning in logs
- Proceed with remaining valid images

## S3 Storage

**Location**: `s3://mapapi/nano_banana_input/`

**Filename Format**: `{uuid}.{ext}`
- Example: `abc123-def456-789.png`

**URL Expiry**: 1 hour (3600 seconds)
- After 1 hour, the presigned URL expires
- Original file remains in S3
- Can regenerate presigned URL if needed

**Cleanup**: 
- Consider setting S3 lifecycle policy
- Auto-delete files older than 24 hours
- Or use S3 bucket versioning

## Best Practices

### 1. **Prefer S3 URLs When Possible**
If you already have images in S3, use direct URLs instead of base64 to avoid re-uploading:
```json
{
  "image_input": "https://s3.us-west-1.amazonaws.com/mapapi/my-image.png"
}
```

### 2. **Use Upload Node for Persistent Storage**
For images you'll use multiple times, use the `upload_image` node first:
```json
{
  "nodes": [
    {
      "id": "upload",
      "type": "upload_image",
      "config": {
        "image_upload": "data:image/png;base64,..."
      }
    },
    {
      "id": "generate1",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "variation 1"
      }
    },
    {
      "id": "generate2",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "variation 2"
      }
    }
  ],
  "edges": [
    {"source": "upload", "target": "generate1"},
    {"source": "upload", "target": "generate2"}
  ]
}
```

### 3. **Batch Upload Multiple Images**
For multiple base64 images, pass them all at once:
```json
{
  "image_input": [
    "data:image/png;base64,...",
    "data:image/png;base64,...",
    "data:image/png;base64,..."
  ]
}
```

### 4. **Monitor S3 Usage**
- Check S3 bucket size regularly
- Set up lifecycle rules
- Consider cost optimization

## Example Workflows

### 1. Sketch to Rendered Image
```json
{
  "nodes": [
    {
      "id": "sketch",
      "type": "sketch_board",
      "config": {}
    },
    {
      "id": "generate",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "professional render of this sketch",
        "resolution": "4K"
      }
    }
  ],
  "edges": [
    {"source": "sketch", "target": "generate"}
  ]
}
```
The sketch board outputs base64, which is automatically uploaded.

### 2. Image Variation with Multiple References
```json
{
  "nodes": [
    {
      "id": "gen",
      "type": "generate_nano_banana_pro",
      "config": {
        "prompt": "blend these styles",
        "image_input": [
          "data:image/png;base64,iVBORw0...",  // Style 1
          "data:image/png;base64,/9j/4AAQ...", // Style 2
          "https://example.com/style3.png"     // Style 3
        ]
      }
    }
  ]
}
```

### 3. Time Travel with Base64 Input
```json
{
  "nodes": [
    {
      "id": "time-travel",
      "type": "time_travel_scene",
      "config": {
        "image_url": "data:image/png;base64,iVBORw0...",
        "target_year": "1850",
        "era_style": "victorian",
        "time_of_day": "sunset"
      }
    }
  ]
}
```

## Technical Details

### MIME Type Detection
```javascript
"data:image/png;base64,..."     → .png
"data:image/jpeg;base64,..."    → .jpg
"data:image/jpg;base64,..."     → .jpg
"data:image/webp;base64,..."    → .webp
"data:image/gif;base64,..."     → .gif
"iVBORw0KGgoAAAANS..."         → .png (default)
```

### URL Validation
After S3 upload, URLs are validated:
```python
✅ Valid: https://s3.us-west-1.amazonaws.com/...
✅ Valid: https://example.com/image.png
❌ Invalid: data:image/png;base64,... (should be uploaded)
❌ Invalid: /local/path/image.png (local paths)
❌ Invalid: ftp://example.com/image.png (non-HTTP)
```

### Processing Flow
```
Base64 Input
    ↓
Detect format (data URI or raw)
    ↓
Extract MIME type & decode
    ↓
Upload to S3
    ↓
Generate presigned URL (1hr expiry)
    ↓
Add to valid_image_urls array
    ↓
Send to Replicate API
```

## Troubleshooting

### Issue: "Failed to upload image_input to S3"
**Cause**: Invalid base64 string or S3 credentials issue

**Solution**:
1. Verify base64 string is valid
2. Check AWS credentials in `.env`
3. Verify S3 bucket permissions

### Issue: "Skipping base64 data URI (not supported by Replicate)"
**Cause**: Base64 not being uploaded to S3

**Solution**: This shouldn't happen now with automatic upload. If you see this, the upload step failed silently.

### Issue: Presigned URL expired
**Cause**: URL expires after 1 hour

**Solution**: Re-run the workflow or use permanent S3 URLs

## API Cost Considerations

**S3 Costs**:
- PUT request: ~$0.005 per 1,000 requests
- Storage: ~$0.023 per GB/month
- GET request (presigned): ~$0.004 per 10,000 requests

**Example**:
- 1,000 base64 uploads per day
- Average 2MB per image
- Monthly cost: ~$3-5

**Optimization**:
- Use lifecycle rules to delete old files
- Compress images before encoding to base64
- Use upload_image node for reusable images

---

## Summary

✅ **Automatic**: No code changes needed
✅ **Seamless**: Works with existing workflows  
✅ **Compatible**: Converts base64 → S3 URLs automatically
✅ **Flexible**: Handles single images, arrays, JSON strings
✅ **Robust**: Continues on error, validates URLs

Base64 images now "just work" in Nano Banana Pro! 🎉

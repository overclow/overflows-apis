# Jenkins CI/CD Pipeline Setup - Workflow API

This document describes the automated CI/CD pipeline for the **Workflow Orchestration API** (FastAPI application).

## Overview

The Jenkins pipeline automates the entire deployment lifecycle:

1. **Checkout** - Pull latest code from repository
2. **Setup Python Environment** - Create Python 3.12 virtual environment
3. **Install Dependencies** - Install from requirements.txt
4. **Lint & Quality** - Run code quality checks
5. **Test** - Run unit tests
6. **Build Verification** - Verify application structure
7. **Stop Previous Instance** - Clean up old processes
8. **Deploy & Start API** - Start Workflow API with persistence
9. **Health Check** - Verify API is responding
10. **Verify Running Process** - Confirm persistence
11. **Report** - Generate build summary

## Pipeline Files

### Jenkinsfile
Main pipeline definition. Located at `/Users/overclaw/projects/overflows-apis/Jenkinsfile`

- 14 stages covering build, test, and deployment
- 30-minute timeout
- Keeps last 10 builds
- Full logging and error handling

### deploy-api.sh
Standalone deployment script for manual or automated API startup.

**Usage:**
```bash
./deploy-api.sh [port] [host] [python_path]

# Examples:
./deploy-api.sh                        # Default: 8001, 127.0.0.1
./deploy-api.sh 8001 127.0.0.1        # Custom port/host
./deploy-api.sh 8001 0.0.0.0 python3.12  # Custom Python
```

**Features:**
- Automatic cleanup of previous instances
- Health check with automatic retry
- Color-coded output
- Process persistence (disown)
- Detailed logging
- PID file for monitoring

## Configuration

### Environment Variables (in Jenkinsfile)

```groovy
PYTHON_VERSION = '3.12'
API_PORT = '8001'
API_HOST = '127.0.0.1'
VENV_DIR = "${WORKSPACE}/venv"
API_LOG_FILE = "${WORKSPACE}/api_deployment.log"
```

### Modify Settings

Edit `Jenkinsfile` to change:
- Python version
- API port (default: 8001)
- API host (default: 127.0.0.1)
- Virtual environment path
- Log file location

## API Details

**Entry Point:** `workflow_api.py`

**Framework:** FastAPI with Uvicorn

**Available Endpoints:**
- `GET /` - Root endpoint, returns status
- `POST /workflow/create` - Create new workflow
- `GET /workflows` - List all workflows
- `POST /workflow/execute` - Execute a workflow
- `GET /workflow/executions` - List executions
- `GET /workflow/execution/{execution_id}` - Get execution status
- `GET /workflow/result/{result_id}` - Get result by ID
- `GET /workflow/results` - List results
- `DELETE /workflow/result/{result_id}` - Delete result
- `POST /webhook/suno` - Suno API webhook

**Default Port:** 8001

**Default Host:** 127.0.0.1

## Setting Up Jenkins Job

### Manual Setup (if not auto-discovered)

1. **New Job** → Select "Pipeline"
2. **Name:** `workflow-api` or similar
3. **Pipeline** section:
   - Definition: "Pipeline script from SCM"
   - SCM: Git
   - Repository URL: `https://github.com/overclow/overflows-apis.git`
   - Branch: `*/main` or `*/master`
   - Script Path: `Jenkinsfile`
4. **Build Triggers:**
   - Poll SCM: `H/30 * * * *` (every 30 minutes)
   - Or webhook from GitHub
5. **Save**

### Auto-Discovery (if not working)

Jenkins can auto-discover Jenkinsfile from repository:
1. Jenkins → Manage Jenkins → Configure System
2. Look for "GitHub" section
3. Configure webhook in GitHub:
   - Go to repository settings
   - Webhooks → Add webhook
   - Payload URL: `http://jenkins-url/github-webhook/`
   - Content type: `application/json`
   - Events: Push events

## Manual Deployment

To deploy API without Jenkins:

```bash
cd /Users/overclaw/projects/overflows-apis

# Make script executable
chmod +x deploy-api.sh

# Run deployment
./deploy-api.sh

# Or with custom settings
./deploy-api.sh 8001 127.0.0.1 python3.12

# Monitor logs
tail -f api.log

# Stop API
kill $(cat api.pid)
```

## Deployment Process

### Automatic (via Jenkins)

1. Push code to GitHub
2. Jenkins detects changes
3. Pipeline triggers automatically
4. Runs all 14 stages
5. API starts and persists
6. Health checks confirm success

### Manual (via Script)

```bash
# Stop current instance
./deploy-api.sh  # Will kill existing process on port 8001

# Or manually
kill -9 $(lsof -ti:8001)

# Deploy new version
./deploy-api.sh 8001 127.0.0.1
```

## Process Persistence

The pipeline uses several techniques to ensure the API survives Jenkins job completion:

1. **nohup** - Ignores SIGHUP (terminal hangup)
2. **disown** - Removes job from shell's job table
3. **Background Process** - `&` puts process in background
4. **Process Verification** - Confirms process remains after script ends

This ensures the API continues running even after Jenkins pipeline completes.

## Troubleshooting

### API Won't Start

**Check logs:**
```bash
tail -f /Users/overclaw/projects/overflows-apis/api.log
```

**Common issues:**
- Port 8001 already in use: `lsof -i :8001`
- Python not installed: `python3.12 --version`
- Missing dependencies: `pip install -r workflow-api/requirements.txt`

### Port Conflicts

**Find and kill process on port 8001:**
```bash
lsof -ti:8001 | xargs kill -9

# Then restart
./deploy-api.sh
```

### API Crashes on Startup

1. Check dependencies:
   ```bash
   python3.12 -m pip list | grep -E fastapi|uvicorn|pymongo
   ```

2. Test import:
   ```bash
   cd workflow-api
   python3.12 -c "from workflow_api import app; print('✅ Import OK')"
   ```

3. Verify MongoDB connection:
   - Check `.env` file for connection string
   - Ensure MongoDB is running (or use mock)

### Health Check Fails

**Manual health check:**
```bash
curl -v http://127.0.0.1:8001/

# With timeout
timeout 5 curl -v http://127.0.0.1:8001/ || echo "API not responding"
```

**Check if process is running:**
```bash
ps aux | grep uvicorn
lsof -i :8001
```

## Monitoring

### View Logs

```bash
# Real-time
tail -f /Users/overclaw/projects/overflows-apis/api.log

# Last 50 lines
tail -50 /Users/overclaw/projects/overflows-apis/api.log

# Search for errors
grep ERROR /Users/overclaw/projects/overflows-apis/api.log
```

### Check Process Status

```bash
# Is process running?
ps aux | grep uvicorn

# What's on port 8001?
lsof -i :8001

# Get PID
cat /Users/overclaw/projects/overflows-apis/api.pid
```

### Test API Endpoints

```bash
# Root endpoint
curl http://127.0.0.1:8001/

# List workflows
curl http://127.0.0.1:8001/workflows

# List executions
curl http://127.0.0.1:8001/workflow/executions

# With JSON pretty-print
curl http://127.0.0.1:8001/workflows | jq .
```

## Integration with overflow-client

The Workflow API runs on **port 8001** while overflow-client runs on **port 3001**.

**From the frontend**, API calls should use:
```javascript
const API_URL = 'http://127.0.0.1:8001';

// Example
fetch(`${API_URL}/workflows`)
  .then(r => r.json())
  .then(data => console.log(data));
```

**CORS Configuration:**
- API has CORS enabled for `ALLOWED_ORIGINS` (see `.env`)
- Frontend must be in allowed origins list

## Performance Tuning

### Workers

Default: 2 workers per settings in `deploy-api.sh`

**Modify in deploy-api.sh:**
```bash
--workers 2  # Change this number
```

**Recommendation:**
- Local development: 1 worker
- Production: number of CPU cores

### Log Level

Default: `info`

**Change log level in deploy-api.sh:**
```bash
--log-level info  # Options: critical, error, warning, info, debug
```

## Scaling

### Multiple Instances

To run multiple API instances on different ports:

```bash
# Instance 1 on 8001
./deploy-api.sh 8001 127.0.0.1

# Instance 2 on 8002
./deploy-api.sh 8002 127.0.0.1

# Instance 3 on 8003
./deploy-api.sh 8003 127.0.0.1
```

Then use a load balancer (nginx, HAProxy) to distribute requests.

### Docker Deployment (Future)

For production, consider Docker:

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY workflow-api/ .
RUN pip install -r requirements.txt

EXPOSE 8001
CMD ["python", "-m", "uvicorn", "workflow_api:app", "--host", "0.0.0.0", "--port", "8001"]
```

Build and run:
```bash
docker build -t workflow-api .
docker run -p 8001:8001 workflow-api
```

## Next Steps

1. **Push to GitHub:** Commit Jenkinsfile and deploy-api.sh
2. **Configure Jenkins:** Set up job or webhook
3. **Test Pipeline:** Push a small change to trigger build
4. **Monitor:** Watch logs and verify API is running
5. **Integrate Frontend:** Update overflow-client to use API endpoints

## Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com)
- [Uvicorn Documentation](https://www.uvicorn.org)
- [Jenkins Pipeline Documentation](https://www.jenkins.io/doc/book/pipeline/)
- [GitHub Webhook Configuration](https://docs.github.com/en/developers/webhooks-and-events/webhooks/creating-webhooks)

---

**Created:** 2026-02-10  
**Repository:** https://github.com/overclow/overflows-apis  
**Workspace:** `/Users/overclaw/projects/overflows-apis`

# Jenkins Manual Setup - Workflow API Pipeline

This guide explains how to manually access Jenkins and trigger the workflow-api pipeline build.

## ⚠️ Current Status

- ✅ **Job Configuration File Created:** `~/.jenkins/jobs/workflow-api/config.xml`
- ✅ **Pipeline Code Pushed to GitHub:** Jenkinsfile in repository
- ✅ **Pipeline Tested Locally:** All 11 stages passing (see `run-pipeline-locally.sh`)
- ✅ **API Running on Port 8001:** Ready for integration
- 🔄 **Jenkins UI Access:** Requires authentication (needs browser tab with Chrome extension relay)

## How to Access Jenkins Web Interface

### Option 1: Using Chrome Browser Extension (Recommended)

1. **Open Chrome browser**
   - You should see the OpenClaw extension icon in the toolbar

2. **Click the OpenClaw extension icon**
   - Status badge should show "ON" (green)

3. **Navigate to Jenkins:**
   ```
   http://127.0.0.1:8080/
   ```

4. **First-Time Setup** (if prompted):
   - Jenkins will show initial setup wizard
   - Follow the setup instructions
   - Create an admin account if needed

5. **View Workflow API Job:**
   ```
   http://127.0.0.1:8080/job/workflow-api/
   ```

### Option 2: Direct URL (if browser is available)

```
http://127.0.0.1:8080/job/workflow-api/
```

**Note:** Jenkins requires authentication. You'll need to log in first.

## Default Jenkins Credentials

If you set up Jenkins with default credentials:
- **Username:** admin
- **Password:** Check Jenkins unlock file: `/Users/overclaw/.jenkins/secrets/initialAdminPassword`

To view the password:
```bash
cat /Users/overclaw/.jenkins/secrets/initialAdminPassword
```

## Creating/Configuring the Job in Jenkins UI

Once you access Jenkins:

1. **Go to Jenkins Home**
   - `http://127.0.0.1:8080/`

2. **Look for "workflow-api" Job**
   - If not visible, go to "New Item" and create it

3. **Configure as Pipeline Job:**
   - Job Name: `workflow-api`
   - Type: **Pipeline**
   - Repository URL: `https://github.com/overclow/overflows-apis.git`
   - Branch: `*/main`
   - Script Path: `Jenkinsfile`

4. **Enable Build Triggers:**
   - Check: "Poll SCM"
   - Schedule: `H/30 * * * *` (every 30 minutes)

5. **Save** and **Build Now**

## Manual Pipeline Execution (Without Jenkins)

If you can't access Jenkins UI, you can run the pipeline locally:

```bash
cd /Users/overclaw/projects/overflows-apis

# Run the local pipeline simulator (all 11 stages)
./run-pipeline-locally.sh
```

**This will:**
- ✅ Create Python 3.12 virtual environment
- ✅ Install dependencies from requirements.txt
- ✅ Run linting and syntax checks
- ✅ Test the application
- ✅ Deploy the API to port 8001
- ✅ Verify the API is running
- ✅ Generate a detailed report

**Output:** All API endpoints ready to use on `http://127.0.0.1:8001/`

## Automatic Builds (Once Jenkins is Configured)

Once the job is properly configured in Jenkins:

### Trigger 1: Automatic Polling
- Jenkins will check GitHub every 30 minutes
- If changes detected, pipeline automatically runs
- API redeploys with new code

### Trigger 2: Manual Trigger
```
http://127.0.0.1:8080/job/workflow-api/build
```
(Requires authentication)

### Trigger 3: GitHub Webhook (Optional)
1. In GitHub repository settings
2. Add Webhook:
   - Payload URL: `http://your-jenkins-url:8080/github-webhook/`
   - Content type: `application/json`
   - Events: Push events
3. Jenkins will build immediately on push

## Jenkins Job Configuration File

The job configuration is located at:
```
~/.jenkins/jobs/workflow-api/config.xml
```

**Contents:**
- Job name: workflow-api
- Description: Workflow API CI/CD Pipeline - FastAPI with Python 3.12
- Repository: https://github.com/overclow/overflows-apis.git
- Branch: main
- Jenkinsfile: Jenkinsfile (in repository root)
- Polling: Every 30 minutes

## Workflow API Deployment

### Current Deployment Status

**✅ Running on Port 8001:**
```bash
# Check if API is running
lsof -i :8001

# Check API status
curl http://127.0.0.1:8001/

# View API logs
tail -f /Users/overclaw/projects/overflows-apis/api_deployment.log
```

### Stop API
```bash
# Kill the process
pkill -f "uvicorn.*8001"

# Or use the stored PID
kill $(cat /Users/overclaw/projects/overflows-apis/api.pid)
```

### Restart API
```bash
# Using the deployment script
cd /Users/overclaw/projects/overflows-apis
./deploy-api.sh 8001 127.0.0.1 python3

# Or using the local pipeline
./run-pipeline-locally.sh
```

## Jenkins Configuration Files

### Main Pipeline: Jenkinsfile
```
/Users/overclaw/projects/overflows-apis/Jenkinsfile
```

**Stages:**
1. Checkout
2. Setup Python Environment
3. Install Dependencies
4. Lint & Quality
5. Test
6. Build Verification
7. Stop Previous Instance
8. Deploy & Start API
9. Health Check
10. Verify Running Process
11. Report

### Standalone Deployment Script: deploy-api.sh
```
/Users/overclaw/projects/overflows-apis/deploy-api.sh
```

**Usage:**
```bash
./deploy-api.sh [port] [host] [python_path]

# Examples
./deploy-api.sh                         # Default: 8001, 127.0.0.1
./deploy-api.sh 8001 127.0.0.1        # Custom port/host
./deploy-api.sh 8001 0.0.0.0 python3.12  # All interfaces
```

### Local Pipeline Simulator: run-pipeline-locally.sh
```
/Users/overclaw/projects/overflows-apis/run-pipeline-locally.sh
```

**Usage:**
```bash
./run-pipeline-locally.sh
```

Simulates all 11 Jenkins pipeline stages locally without requiring Jenkins.

## Testing the API

### Quick Test
```bash
curl http://127.0.0.1:8001/
```

**Expected Response:**
```json
{
  "service": "Workflow Orchestration API",
  "version": "1.0.0",
  "endpoints": { ... },
  "supported_node_types": { ... }
}
```

### Test All Endpoints

```bash
# List workflows
curl http://127.0.0.1:8001/workflows

# List executions
curl http://127.0.0.1:8001/workflow/executions

# Get service info
curl http://127.0.0.1:8001/ | jq .service

# Create a workflow (example)
curl -X POST http://127.0.0.1:8001/workflow/create \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Workflow",
    "description": "Test",
    "nodes": [],
    "edges": []
  }'
```

## Troubleshooting

### Jenkins Won't Start
```bash
# Check status
brew services list | grep jenkins

# Start Jenkins
brew services start jenkins-lts

# Restart Jenkins
brew services restart jenkins-lts
```

### Can't Access Jenkins UI
1. Verify Jenkins is running: `ps aux | grep jenkins.war`
2. Check port 8080 is open: `lsof -i :8080`
3. Try accessing: `http://127.0.0.1:8080/`

### Job Not Showing in Jenkins
1. Verify config file exists: `ls ~/.jenkins/jobs/workflow-api/config.xml`
2. Restart Jenkins: `brew services restart jenkins-lts`
3. Jenkins needs 30 seconds to load the job from disk

### API Won't Start
1. Check logs: `tail -f /Users/overclaw/projects/overflows-apis/api_deployment.log`
2. Verify port 8001 is free: `lsof -i :8001`
3. Check Python: `python3 --version` (must be 3.9+)
4. Test manually: `cd workflow-api && python3 -m uvicorn workflow_api:app --host 127.0.0.1 --port 8001`

### Build Fails in Jenkins
1. Check job logs: `http://127.0.0.1:8080/job/workflow-api/lastBuild/console`
2. Run local pipeline: `./run-pipeline-locally.sh` (to compare)
3. Verify dependencies: `python3 -m pip list`

## Next Steps

1. **Access Jenkins UI**
   - Open browser with Chrome extension relay attached
   - Navigate to http://127.0.0.1:8080/job/workflow-api/

2. **Configure Job** (if needed)
   - Set build triggers
   - Enable GitHub webhook (optional)

3. **Test Build**
   - Click "Build Now"
   - Monitor build progress
   - Verify API deploys successfully

4. **Integrate with Frontend**
   - Update overflow-client to use API endpoints
   - API base URL: `http://127.0.0.1:8001`

5. **Monitor Production**
   - Set up log monitoring
   - Configure alerts for failed builds
   - Track deployment history

## Summary

| Component | Status | Location |
|-----------|--------|----------|
| Jenkinsfile | ✅ Ready | `/Users/overclaw/projects/overflows-apis/Jenkinsfile` |
| deploy-api.sh | ✅ Ready | `/Users/overclaw/projects/overflows-apis/deploy-api.sh` |
| run-pipeline-locally.sh | ✅ Ready | `/Users/overclaw/projects/overflows-apis/run-pipeline-locally.sh` |
| Jenkins Job Config | ✅ Ready | `~/.jenkins/jobs/workflow-api/config.xml` |
| GitHub Repository | ✅ Pushed | https://github.com/overclow/overflows-apis.git |
| Workflow API | ✅ Running | http://127.0.0.1:8001 |
| Jenkins UI | ⚠️ Requires Auth | http://127.0.0.1:8080 |

---

**Everything is configured and ready. Just access Jenkins UI and trigger the first build, or continue using the local pipeline script!**

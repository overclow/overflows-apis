#!/bin/bash

###############################################################################
# Workflow API Deployment Script
# 
# Usage: ./deploy-api.sh [port] [host] [python_path]
# 
# Examples:
#   ./deploy-api.sh                    # Use defaults: port 8001, host 127.0.0.1
#   ./deploy-api.sh 8001 127.0.0.1    # Custom port and host
#   ./deploy-api.sh 8001 0.0.0.0 python3.12  # Custom Python path
#
# Environment Variables (override defaults):
#   API_PORT     - Port to run on (default: 8001)
#   API_HOST     - Host to bind to (default: 127.0.0.1)
#   PYTHON_PATH  - Path to Python executable (default: python3)
###############################################################################

set -e

# Configuration
API_PORT="${1:-${API_PORT:-8001}}"
API_HOST="${2:-${API_HOST:-127.0.0.1}}"
PYTHON_PATH="${3:-${PYTHON_PATH:-python3}}"
WORKSPACE="${WORKSPACE:-.}"
API_LOG_FILE="${WORKSPACE}/api.log"
PID_FILE="${WORKSPACE}/api.pid"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_header() {
    echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

# Main logic
print_header "Workflow API Deployment"

echo ""
echo "Configuration:"
echo "  Port: ${API_PORT}"
echo "  Host: ${API_HOST}"
echo "  Python: ${PYTHON_PATH}"
echo "  Workspace: ${WORKSPACE}"
echo "  Log File: ${API_LOG_FILE}"
echo "  PID File: ${PID_FILE}"
echo ""

# Stop any existing instance on this port
print_info "Checking for existing instances on port ${API_PORT}..."
if lsof -ti:${API_PORT} > /dev/null 2>&1; then
    print_warning "Found existing process on port ${API_PORT}, stopping it..."
    lsof -ti:${API_PORT} | xargs -r kill -9 2>/dev/null || true
    sleep 2
    print_success "Previous instance stopped"
else
    print_info "No existing instance found"
fi

echo ""

# Verify Python
print_info "Verifying Python installation..."
if ! command -v "${PYTHON_PATH}" &> /dev/null; then
    print_error "Python not found at: ${PYTHON_PATH}"
    exit 1
fi

PYTHON_VERSION=$(${PYTHON_PATH} --version)
print_success "Python verified: ${PYTHON_VERSION}"

echo ""

# Check dependencies
print_info "Checking FastAPI installation..."
if ! ${PYTHON_PATH} -c "import fastapi; print(f'FastAPI {fastapi.__version__}')" 2>/dev/null; then
    print_error "FastAPI not installed. Run: pip install -r requirements.txt"
    exit 1
fi

${PYTHON_PATH} -c "import fastapi; import uvicorn; import pydantic; print('✅ Core dependencies OK')"

echo ""

# Start API
print_info "Starting Workflow API..."

# Create start command
START_CMD="${PYTHON_PATH} -m uvicorn workflow_api:app \\
    --host ${API_HOST} \\
    --port ${API_PORT} \\
    --workers 2 \\
    --log-level info"

echo "Command: ${START_CMD}"
echo ""

cd "${WORKSPACE}/workflow-api"

# Start with nohup
nohup ${START_CMD} > "${API_LOG_FILE}" 2>&1 &
API_PID=$!

# Save PID for later reference
echo ${API_PID} > "${PID_FILE}"

print_success "Workflow API started (PID: ${API_PID})"

# Disown the process so it survives script exit
disown

echo ""

# Wait for startup
print_info "Waiting for API to start..."
sleep 3

# Health check
print_info "Running health check..."
API_URL="http://${API_HOST}:${API_PORT}"
MAX_ATTEMPTS=20
ATTEMPT=0

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    ATTEMPT=$((ATTEMPT + 1))
    
    if curl -s -f "${API_URL}/" > /dev/null 2>&1; then
        RESPONSE=$(curl -s "${API_URL}/" | head -c 100)
        print_success "API is responding!"
        echo "Response: ${RESPONSE}"
        
        echo ""
        print_header "Deployment Complete"
        echo ""
        echo "API Details:"
        echo "  URL: ${API_URL}"
        echo "  PID: ${API_PID}"
        echo "  Log: ${API_LOG_FILE}"
        echo ""
        echo "Available Endpoints:"
        echo "  GET  ${API_URL}/"
        echo "  POST ${API_URL}/workflow/create"
        echo "  GET  ${API_URL}/workflows"
        echo "  POST ${API_URL}/workflow/execute"
        echo "  GET  ${API_URL}/workflow/executions"
        echo "  GET  ${API_URL}/workflow/result/{result_id}"
        echo ""
        
        # Check process status
        echo "Process Status:"
        if kill -0 ${API_PID} 2>/dev/null; then
            print_success "Process is running and will persist after this script exits"
        fi
        
        echo ""
        print_header "Next Steps"
        echo ""
        echo "View logs:"
        echo "  tail -f ${API_LOG_FILE}"
        echo ""
        echo "Stop API:"
        echo "  kill ${API_PID}"
        echo ""
        echo "Restart API:"
        echo "  ./deploy-api.sh ${API_PORT} ${API_HOST}"
        echo ""
        
        exit 0
    fi
    
    echo "⏳ Waiting... (${ATTEMPT}/${MAX_ATTEMPTS})"
    sleep 2
done

print_error "API failed to start after ${MAX_ATTEMPTS} attempts"
echo ""
echo "📋 Last 30 lines of log:"
if [ -f "${API_LOG_FILE}" ]; then
    tail -30 "${API_LOG_FILE}"
else
    echo "No log file found"
fi

exit 1

#!/bin/bash

###############################################################################
# Local Pipeline Simulator - Runs Jenkinsfile stages without Jenkins
# 
# This script simulates the Jenkins pipeline locally for testing/debugging
# Usage: ./run-pipeline-locally.sh
###############################################################################

set -e

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
NC='\033[0m'

# Configuration
WORKSPACE="$(pwd)"
PYTHON_VERSION='3.12'
API_PORT='8001'
API_HOST='127.0.0.1'
VENV_DIR="${WORKSPACE}/venv"
PYTHON_PATH="${VENV_DIR}/bin/python"
PIP_PATH="${VENV_DIR}/bin/pip"
API_LOG_FILE="${WORKSPACE}/api_deployment.log"

# Build ID for this run
BUILD_ID="local-$(date +%Y%m%d-%H%M%S)"

print_header() {
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
}

print_section() {
    echo ""
    echo -e "${MAGENTA}▶ $1${NC}"
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

# Start pipeline
print_header "WORKFLOW API LOCAL PIPELINE - Build ${BUILD_ID}"

# Stage 1: Checkout
print_section "Stage 1/11: Checkout"
echo "Repository: $(pwd)"
echo "Branch: $(git rev-parse --abbrev-ref HEAD)"
echo "Commit: $(git rev-parse --short HEAD)"
print_success "Code checked out"

# Stage 2: Setup Python Environment
print_section "Stage 2/11: Setup Python Environment"
if [ -d "${VENV_DIR}" ]; then
    print_info "Removing old venv..."
    rm -rf "${VENV_DIR}"
fi

print_info "Creating Python 3.12 virtual environment..."
if ! command -v python3.12 &> /dev/null; then
    print_error "Python 3.12 not found. Using python3 instead."
    python3 -m venv "${VENV_DIR}"
else
    python3.12 -m venv "${VENV_DIR}"
fi

print_success "Virtual environment created"
"${PYTHON_PATH}" --version

# Stage 3: Install Dependencies
print_section "Stage 3/11: Install Dependencies"
"${PIP_PATH}" install --upgrade pip setuptools wheel -q
cd "${WORKSPACE}/workflow-api"

if [ -f "requirements.txt" ]; then
    print_info "Installing requirements.txt..."
    "${PIP_PATH}" install -r requirements.txt -q
    print_success "Dependencies installed"
else
    print_error "requirements.txt not found!"
    exit 1
fi

# Stage 4: Lint & Quality
print_section "Stage 4/11: Lint & Quality"
print_info "Checking Python syntax..."
"${PYTHON_PATH}" -m py_compile workflow_api.py
print_success "Syntax check passed"

# Stage 5: Test
print_section "Stage 5/11: Test"
print_info "Testing application import..."
if "${PYTHON_PATH}" -c "from workflow_api import app; print('FastAPI app loaded successfully')" 2>/dev/null; then
    print_success "Import test passed"
else
    print_error "Import test failed"
    exit 1
fi

# Stage 6: Build Verification
print_section "Stage 6/11: Build Verification"
if [ -f "workflow_api.py" ]; then
    print_success "workflow_api.py found"
    if [ -d "app" ]; then
        print_success "app/ directory structure verified"
        print_info "App modules: $(ls -1 app/ | tr '\n' ', ')"
    fi
else
    print_error "workflow_api.py not found!"
    exit 1
fi

# Stage 7: Stop Previous Instance
print_section "Stage 7/11: Stop Previous Instance"
if lsof -ti:${API_PORT} > /dev/null 2>&1; then
    print_info "Stopping existing process on port ${API_PORT}..."
    lsof -ti:${API_PORT} | xargs -r kill -9 2>/dev/null || true
    sleep 2
    print_success "Previous instance stopped"
else
    print_info "No previous instance found"
fi

# Stage 8: Deploy & Start API
print_section "Stage 8/11: Deploy & Start API"
print_info "Starting Workflow API..."
print_info "  Host: ${API_HOST}"
print_info "  Port: ${API_PORT}"
print_info "  Workers: 2"

cd "${WORKSPACE}/workflow-api"

# Start API with nohup
nohup "${PYTHON_PATH}" -m uvicorn workflow_api:app \
    --host ${API_HOST} \
    --port ${API_PORT} \
    --workers 2 \
    --log-level info \
    > "${API_LOG_FILE}" 2>&1 &

API_PID=$!
echo ${API_PID} > "${WORKSPACE}/api.pid"

print_success "Workflow API started (PID: ${API_PID})"
disown

# Stage 9: Health Check
print_section "Stage 9/11: Health Check"
API_URL="http://${API_HOST}:${API_PORT}"
MAX_ATTEMPTS=20
ATTEMPT=0

echo "Checking API at ${API_URL}..."

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    ATTEMPT=$((ATTEMPT + 1))
    
    if curl -s -f "${API_URL}/" > /dev/null 2>&1; then
        print_success "API is responding!"
        RESPONSE=$(curl -s "${API_URL}/" | python3 -c "import sys, json; data=json.load(sys.stdin); print(f\"{data['service']} v{data['version']}\")")
        echo "Response: ${RESPONSE}"
        break
    fi
    
    if [ $ATTEMPT -eq $MAX_ATTEMPTS ]; then
        print_error "API failed to start after ${MAX_ATTEMPTS} attempts"
        echo "Last 30 lines of log:"
        tail -30 "${API_LOG_FILE}"
        exit 1
    fi
    
    echo "⏳ Waiting for API... (${ATTEMPT}/${MAX_ATTEMPTS})"
    sleep 2
done

# Stage 10: Verify Running Process
print_section "Stage 10/11: Verify Running Process"
if kill -0 ${API_PID} 2>/dev/null; then
    print_success "Process is running and persistent"
    echo "Process details:"
    lsof -i :${API_PORT} | tail -2
else
    print_error "Process is not running"
    exit 1
fi

# Stage 11: Report
print_section "Stage 11/11: Report"

print_header "PIPELINE COMPLETED SUCCESSFULLY ✅"

echo ""
echo "Build Summary:"
echo "  Build ID: ${BUILD_ID}"
echo "  Status: SUCCESS"
echo "  Duration: $((SECONDS / 60)) minutes"
echo ""
echo "API Details:"
echo "  Service: Workflow Orchestration API"
echo "  URL: ${API_URL}"
echo "  Port: ${API_PORT}"
echo "  PID: ${API_PID}"
echo "  Python: $(${PYTHON_PATH} --version)"
echo ""
echo "Available Endpoints:"
echo "  GET  ${API_URL}/"
echo "  POST ${API_URL}/workflow/create"
echo "  GET  ${API_URL}/workflows"
echo "  POST ${API_URL}/workflow/execute"
echo "  GET  ${API_URL}/workflow/executions"
echo "  GET  ${API_URL}/workflow/result/{result_id}"
echo "  GET  ${API_URL}/workflow/results"
echo "  DELETE ${API_URL}/workflow/result/{result_id}"
echo ""
echo "Logs:"
echo "  ${API_LOG_FILE}"
echo ""
echo "Resources:"
echo "  Virtual Environment: ${VENV_DIR}"
echo "  PID File: ${WORKSPACE}/api.pid"
echo ""
echo "Next Steps:"
echo "  1. Test endpoints: curl ${API_URL}/"
echo "  2. View logs: tail -f ${API_LOG_FILE}"
echo "  3. Stop API: kill ${API_PID}"
echo ""

print_header "Ready for Production 🚀"

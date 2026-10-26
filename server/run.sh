#!/bin/bash
# Lambda Web Adapter runs this once per execution environment, then forwards each request to the app.
# Same form as the adapter's FastAPI zip example: dependencies sit next to the code in LAMBDA_TASK_ROOT.
PATH=$PATH:$LAMBDA_TASK_ROOT/bin \
    PYTHONPATH=$PYTHONPATH:/opt/python:$LAMBDA_RUNTIME_DIR \
    exec python -m uvicorn --port="${AWS_LWA_PORT:-8080}" --no-access-log remote_mcp.app:app

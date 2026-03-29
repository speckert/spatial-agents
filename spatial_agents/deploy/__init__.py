"""
Deploy — Deployment configurations for local Mac and cloud targets.

Supports two deployment modes from a single codebase:
    local_mac  — M1 Mini on LAN, FastAPI on port 8012, local tile storage
    cloud      — S3 tile sync, CDN delivery, containerized API (ARM64)

Version History:
    0.1.0  2026-03-28  Initial deploy package with local Mac uvicorn config,
                       S3 tile sync, and ARM64 Dockerfile
    0.1.1  2026-03-28  Added argparse --help to local_mac and cloud_s3 entry points
"""

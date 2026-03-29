"""
Serving — FastAPI REST API and static tile server.

Single FastAPI process on port 8012 handles both static H3 tile
delivery and dynamic intelligence queries.

Version History:
    0.1.0  2026-03-28  Initial serving package with FastAPI app, API routes,
                       tile routes, health routes, and file exporter
"""

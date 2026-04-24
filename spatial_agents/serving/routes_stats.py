"""
Stats Routes — Server traffic metrics from Apache access log.

Version History:
    0.1.0  2026-04-09  Initial stats endpoint with 24-hour traffic metrics
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import socket
from functools import lru_cache

from fastapi import APIRouter, Query

router = APIRouter()

_server_started = datetime.now(timezone.utc)
_feed_manager = None


def set_feed_manager(manager: Any) -> None:
    """Set the feed manager reference (called during app startup)."""
    global _feed_manager
    _feed_manager = manager

ACCESS_LOG = Path("/private/var/log/apache2/spatialagents-access_log")
SPECKTECH_ACCESS_LOG = Path("/private/var/log/apache2/specktech-access_log")

# Apache combined log format regex
_LOG_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ '
    r'\[(?P<time>[^\]]+)\] '
    r'"(?P<method>\S+) (?P<path>\S+) \S+" '
    r'(?P<status>\d+) (?P<size>\S+)'
)
_TIME_FMT = "%d/%b/%Y:%H:%M:%S %z"


def _parse_log(hours: int = 24) -> list[dict]:
    """Parse recent Apache access log entries."""
    if not ACCESS_LOG.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    entries = []

    # Read file in reverse for efficiency — stop when we pass the cutoff
    lines = ACCESS_LOG.read_text().splitlines()
    for line in reversed(lines):
        m = _LOG_RE.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group("time"), _TIME_FMT)
        except ValueError:
            continue
        if ts < cutoff:
            break
        entries.append({
            "ip": m.group("ip"),
            "time": ts,
            "method": m.group("method"),
            "path": m.group("path"),
            "status": int(m.group("status")),
        })

    entries.reverse()
    return entries


@lru_cache(maxsize=256)
def _reverse_dns(ip: str) -> str:
    """Reverse DNS lookup, cached. Returns hostname or empty string."""
    try:
        host = socket.gethostbyaddr(ip)[0]
        return host
    except (socket.herror, socket.gaierror, OSError):
        return ""


_geo_cache: dict[str, dict] = {}


def _geoip_lookup(ip: str) -> dict:
    """Look up country/city for an IP via ip-api.com. Cached in memory."""
    if ip in _geo_cache:
        return _geo_cache[ip]
    try:
        import httpx
        r = httpx.get(f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city,isp", timeout=3)
        data = r.json()
        if data.get("status") == "success":
            result = {
                "country": data.get("country", ""),
                "country_code": data.get("countryCode", ""),
                "region": data.get("regionName", ""),
                "city": data.get("city", ""),
                "isp": data.get("isp", ""),
            }
        else:
            result = {}
    except Exception:
        result = {}
    _geo_cache[ip] = result
    return result


def _resolve_self_ips() -> set[str]:
    """Resolve the server's own public IP(s)."""
    ips = set()
    try:
        for info in socket.getaddrinfo("agents.specktech.com", 443):
            ips.add(info[4][0])
    except socket.gaierror:
        pass
    return ips


@router.get("/stats")
async def get_stats(
    exclude_self: bool = Query(default=False, description="Exclude server's own IP from stats"),
):
    """Return 24-hour traffic metrics."""
    entries = _parse_log(24)
    now = datetime.now(timezone.utc)

    if exclude_self:
        self_ips = _resolve_self_ips()
        entries = [e for e in entries if e["ip"] not in self_ips]

    if not entries:
        return {
            "server_started": _server_started.isoformat(),
            "period_hours": 24,
            "total_requests": 0,
            "unique_ips": 0,
            "active_now": 0,
            "requests_per_minute": 0,
            "hourly": [],
            "top_endpoints": [],
            "top_ips": [],
        }

    # Active in last 5 minutes
    active_cutoff = now - timedelta(minutes=5)
    active_ips = {e["ip"] for e in entries if e["time"] > active_cutoff}

    # Requests in last 5 minutes for rate calc
    recent = [e for e in entries if e["time"] > active_cutoff]
    rpm = len(recent) / 5.0 if recent else 0

    # Hourly breakdown
    hourly: dict[str, dict] = {}
    for h in range(24):
        t = now - timedelta(hours=23 - h)
        key = t.strftime("%H:00")
        hourly[key] = {"hour": key, "requests": 0, "unique_ips": set()}

    for e in entries:
        key = e["time"].strftime("%H:00")
        if key in hourly:
            hourly[key]["requests"] += 1
            hourly[key]["unique_ips"].add(e["ip"])

    hourly_list = []
    for h in hourly.values():
        hourly_list.append({
            "hour": h["hour"],
            "requests": h["requests"],
            "unique_ips": len(h["unique_ips"]),
        })

    # Top endpoints
    endpoint_counts: dict[str, int] = defaultdict(int)
    for e in entries:
        # Strip query params for grouping
        path = e["path"].split("?")[0]
        endpoint_counts[path] += 1
    top_endpoints = sorted(endpoint_counts.items(), key=lambda x: -x[1])[:10]

    # Top IPs
    ip_counts: dict[str, int] = defaultdict(int)
    for e in entries:
        ip_counts[e["ip"]] += 1
    top_ips = sorted(ip_counts.items(), key=lambda x: -x[1])[:10]

    # Entity counts from feed manager
    vessels = len(_feed_manager.get_latest_vessels()) if _feed_manager else 0
    aircraft = len(_feed_manager.get_latest_aircraft()) if _feed_manager else 0

    return {
        "server_started": _server_started.isoformat(),
        "period_hours": 24,
        "total_requests": len(entries),
        "unique_ips": len({e["ip"] for e in entries}),
        "active_now": len(active_ips),
        "requests_per_minute": round(rpm, 1),
        "vessels_tracked": vessels,
        "aircraft_tracked": aircraft,
        "hourly": hourly_list,
        "top_endpoints": [{"path": p, "count": c} for p, c in top_endpoints],
        "top_ips": [
            {"ip": ip, "host": _reverse_dns(ip), **_geoip_lookup(ip), "count": c}
            for ip, c in top_ips
        ],
    }


def _parse_specktech_log(hours: int = 24) -> list[dict]:
    """Parse recent SpeckTech Apache access log entries."""
    if not SPECKTECH_ACCESS_LOG.exists():
        return []

    # SpeckTech uses common log format (no referer/user-agent)
    log_re = re.compile(
        r'^(?P<ip>\S+) \S+ \S+ '
        r'\[(?P<time>[^\]]+)\] '
        r'"(?P<method>\S+) (?P<path>\S+) \S+" '
        r'(?P<status>\d+) (?P<size>\S+)'
    )

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    entries = []

    lines = SPECKTECH_ACCESS_LOG.read_text().splitlines()
    for line in reversed(lines):
        m = log_re.match(line)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group("time"), _TIME_FMT)
        except ValueError:
            continue
        if ts < cutoff:
            break
        entries.append({
            "ip": m.group("ip"),
            "time": ts,
            "method": m.group("method"),
            "path": m.group("path"),
            "status": int(m.group("status")),
        })

    entries.reverse()
    return entries


def _normalize_path(path: str) -> str:
    """Strip query params and absolute URL prefix, return just the path."""
    p = path.split("?")[0]
    # Some log entries have full URL: http://specktech.com/AppStore/...
    for prefix in ("http://specktech.com", "https://specktech.com"):
        if p.lower().startswith(prefix.lower()):
            p = p[len(prefix):]
            break
    return p


def _categorize_path(path: str) -> str:
    """Categorize a request path into VisualResume, AppStore, or Other."""
    p = _normalize_path(path).lower()
    if p.startswith("/visualresume"):
        return "VisualResume"
    if p.startswith("/appstore"):
        return "AppStore"
    return "Other"


def _parse_appstore_path(path: str) -> dict | None:
    """Parse an AppStore path into app, page_type, language.

    Pattern: /AppStore/{App}_{Type}_{Language}.html
    App names may contain underscores (e.g. Spatial_Agents_Mac, Hummingbird_Moments).
    """
    p = _normalize_path(path)
    if not p.lower().startswith("/appstore/"):
        return None
    filename = p.split("/")[-1]
    if not filename.endswith(".html"):
        return None
    name = filename[:-5]  # strip .html

    # Page types to look for (split from the right since app names have underscores)
    for ptype in ("Marketing", "Privacy", "Support"):
        # Match _Type_Language or _Type (no language = English)
        if name.endswith(f"_{ptype}"):
            app = name[:-(len(ptype) + 1)].replace("_", " ")
            return {"app": app, "page_type": ptype, "language": "English"}
        idx = name.find(f"_{ptype}_")
        if idx != -1:
            app = name[:idx].replace("_", " ")
            lang = name[idx + len(ptype) + 2:].replace("_", " ")
            return {"app": app, "page_type": ptype, "language": lang}

    return None


def _compute_appstore_breakdown(entries: list[dict]) -> list[dict]:
    """Break down AppStore traffic by app, with country info per visitor."""
    app_data: dict[str, dict] = {}  # app -> {requests, ips: set, countries: Counter, pages: Counter}

    for e in entries:
        parsed = _parse_appstore_path(e["path"])
        if not parsed:
            continue
        app = parsed["app"]
        if app not in app_data:
            app_data[app] = {
                "requests": 0,
                "ips": set(),
                "countries": defaultdict(int),
                "pages": defaultdict(int),
            }
        app_data[app]["requests"] += 1
        app_data[app]["ips"].add(e["ip"])
        app_data[app]["pages"][parsed["page_type"]] += 1

    # Enrich with country data per IP
    all_ips = set()
    for d in app_data.values():
        all_ips |= d["ips"]

    ip_geo = {ip: _geoip_lookup(ip) for ip in all_ips}

    # Build country code -> full name map, and count per app
    code_to_name: dict[str, str] = {}
    for d in app_data.values():
        for ip in d["ips"]:
            geo = ip_geo.get(ip, {})
            code = geo.get("country_code") or "Unknown"
            name = geo.get("country") or "Unknown"
            code_to_name[code] = name
            d["countries"][code] += 1

    result = []
    for app, d in sorted(app_data.items(), key=lambda x: -x[1]["requests"]):
        # Format countries as "Full Name (XX): count"
        countries_formatted = {
            f"{code_to_name.get(c, c)} ({c})": n
            for c, n in sorted(d["countries"].items(), key=lambda x: -x[1])
        }
        result.append({
            "app": app,
            "requests": d["requests"],
            "unique_ips": len(d["ips"]),
            "countries": countries_formatted,
            "pages": dict(d["pages"]),
        })

    return result


def _compute_resume_sessions(entries: list[dict]) -> list[dict]:
    """Compute VisualResume session durations per IP.

    A session is a sequence of requests from the same IP to /VisualResume/*
    with no gap longer than 30 minutes.
    """
    # Group VisualResume requests by IP, sorted by time
    ip_times: dict[str, list[datetime]] = defaultdict(list)
    for e in entries:
        if _categorize_path(e["path"]) == "VisualResume":
            ip_times[e["ip"]].append(e["time"])

    sessions = []
    for ip, times in ip_times.items():
        times.sort()
        # Split into sessions (30-min gap)
        session_start = times[0]
        session_end = times[0]
        for t in times[1:]:
            if (t - session_end).total_seconds() > 1800:
                sessions.append({"ip": ip, "start": session_start, "end": session_end})
                session_start = t
            session_end = t
        sessions.append({"ip": ip, "start": session_start, "end": session_end})

    # Enrich with geo + duration
    result = []
    for s in sessions:
        duration_sec = (s["end"] - s["start"]).total_seconds()
        geo = _geoip_lookup(s["ip"])
        result.append({
            "ip": s["ip"],
            "host": _reverse_dns(s["ip"]),
            "city": geo.get("city", ""),
            "region": geo.get("region", ""),
            "country_code": geo.get("country_code", ""),
            "start": s["start"].isoformat(),
            "duration_sec": int(duration_sec),
            "duration_display": (
                f"{int(duration_sec // 60)}m {int(duration_sec % 60)}s"
                if duration_sec >= 60
                else f"{int(duration_sec)}s"
            ),
        })

    result.sort(key=lambda x: x["start"], reverse=True)
    return result


@router.get("/stats/specktech")
async def get_specktech_stats(
    exclude_self: bool = Query(default=False, description="Exclude server's own IP from stats"),
):
    """Return 24-hour traffic metrics for specktech.com."""
    entries = _parse_specktech_log(24)
    now = datetime.now(timezone.utc)

    if exclude_self:
        self_ips = _resolve_self_ips()
        entries = [e for e in entries if e["ip"] not in self_ips]

    if not entries:
        return {
            "period_hours": 24,
            "total_requests": 0,
            "unique_ips": 0,
            "categories": {},
            "hourly_requests": [],
            "hourly_ips": [],
            "resume_sessions": [],
            "appstore_breakdown": [],
            "top_other": [],
            "top_visitors": [],
        }

    # Category breakdown
    cat_counts: dict[str, int] = defaultdict(int)
    cat_ips: dict[str, set] = defaultdict(set)
    for e in entries:
        cat = _categorize_path(e["path"])
        cat_counts[cat] += 1
        cat_ips[cat].add(e["ip"])

    categories = {}
    for cat in ["VisualResume", "AppStore", "Other"]:
        categories[cat] = {
            "requests": cat_counts.get(cat, 0),
            "unique_ips": len(cat_ips.get(cat, set())),
        }

    # Hourly breakdown by category
    hourly: dict[str, dict] = {}
    for h in range(24):
        t = now - timedelta(hours=23 - h)
        key = t.strftime("%H:00")
        hourly[key] = {
            "hour": key,
            "VisualResume": 0, "AppStore": 0, "Other": 0,
            "unique_ips": set(),
        }

    for e in entries:
        key = e["time"].strftime("%H:00")
        if key in hourly:
            cat = _categorize_path(e["path"])
            hourly[key][cat] += 1
            hourly[key]["unique_ips"].add(e["ip"])

    hourly_requests = []
    hourly_ips = []
    for h in hourly.values():
        hourly_requests.append({
            "hour": h["hour"],
            "VisualResume": h["VisualResume"],
            "AppStore": h["AppStore"],
            "Other": h["Other"],
            "total": h["VisualResume"] + h["AppStore"] + h["Other"],
        })
        hourly_ips.append({
            "hour": h["hour"],
            "unique_ips": len(h["unique_ips"]),
        })

    # Resume sessions — look back 7 days
    resume_entries = _parse_specktech_log(168)
    if exclude_self:
        resume_entries = [e for e in resume_entries if e["ip"] not in _resolve_self_ips()]
    resume_sessions = _compute_resume_sessions(resume_entries)

    # AppStore breakdown by app — look back 7 days
    appstore_breakdown = _compute_appstore_breakdown(resume_entries)

    # Top "Other" pages — 7 days
    other_counts: dict[str, int] = defaultdict(int)
    for e in resume_entries:
        if _categorize_path(e["path"]) == "Other":
            path = e["path"].split("?")[0]
            other_counts[path] += 1
    top_other = [
        {"path": p, "count": c}
        for p, c in sorted(other_counts.items(), key=lambda x: -x[1])[:10]
    ]

    # Top visitors (all categories)
    ip_counts: dict[str, int] = defaultdict(int)
    for e in entries:
        ip_counts[e["ip"]] += 1
    top_visitors = sorted(ip_counts.items(), key=lambda x: -x[1])[:15]

    return {
        "period_hours": 24,
        "total_requests": len(entries),
        "unique_ips": len({e["ip"] for e in entries}),
        "categories": categories,
        "hourly_requests": hourly_requests,
        "hourly_ips": hourly_ips,
        "resume_sessions": resume_sessions,
        "appstore_breakdown": appstore_breakdown,
        "top_other": top_other,
        "top_visitors": [
            {"ip": ip, "host": _reverse_dns(ip), **_geoip_lookup(ip), "count": c}
            for ip, c in top_visitors
        ],
    }

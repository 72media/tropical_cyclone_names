#!/usr/bin/env python3
"""Small same-origin proxy for the JMA tropical-cyclone JSON feed."""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
JMA_BASE = "https://www.data.jma.go.jp/multi/data/VPTW60"
JMA_DETAIL_BASE = "https://www.data.jma.go.jp/multi/cyclone/cyclone_detail.html"
CURRENT_IDS = range(60, 66)
CACHE_SECONDS = 45
_cache: dict[str, object] = {"at": 0.0, "payload": None}


def get_json(storm_id: int) -> dict:
    url = f"{JMA_BASE}/{storm_id}_en.json?ts={int(time.time())}"
    req = Request(url, headers={"User-Agent": "storm-dashboard/1.0"})
    with urlopen(req, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def is_recent(report: dict) -> bool:
    try:
        dt = datetime.strptime(report["reportDateTime"], "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() <= 24 * 3600 + 9 * 3600
    except (KeyError, ValueError):
        return False


def clean_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "", value.upper())


def normalize(storm_id: int, report: dict) -> dict:
    current = (report.get("meteorologicalInfos") or [{}])[0]
    center = current.get("centerPart") or {}
    wind = current.get("windPart") or {}
    category = (current.get("classPart") or {}).get("intensityAndTyphoonClass") or "Tropical Cyclone"
    track = []
    for index, info in enumerate(report.get("meteorologicalInfos") or []):
        info_center = info.get("centerPart") or {}
        probability = info_center.get("probabilityCircle") or {}
        lat = info_center.get("coordinateLat") or probability.get("basePointLat", "")
        lon = info_center.get("coordinateLon") or probability.get("basePointLon", "")
        if not lat or not lon:
            continue
        info_wind = info.get("windPart") or {}
        info_class = info.get("classPart") or {}
        track.append({
            "time": info.get("dateTime", ""),
            "lat": lat,
            "lon": lon,
            "isForecast": index > 0,
            "category": info_class.get("intensityAndTyphoonClass", ""),
            "categoryCode": info_class.get("typhoonClass", ""),
            "pressure": info_center.get("pressure", ""),
            "windKmh": round(float(info_wind["windSpeedMS"]) * 3.6) if info_wind.get("windSpeedMS") else "",
            "windKt": info_wind.get("windSpeedKnot", ""),
            "radiusKm": probability.get("axis", {}).get("radiusKm", "") if probability else "",
        })
    return {
        "id": storm_id,
        "name": report.get("name", "Unknown"),
        "number": report.get("number", ""),
        "category": category,
        "categoryCode": (current.get("classPart") or {}).get("typhoonClass", ""),
        "reportDateTime": report.get("reportDateTime", ""),
        "targetDateTime": report.get("targetDateTime", ""),
        "lat": center.get("coordinateLat", ""),
        "lon": center.get("coordinateLon", ""),
        "direction": center.get("direction", ""),
        "speedKmh": center.get("speedKmH", ""),
        "pressure": center.get("pressure", ""),
        "windMs": wind.get("windSpeedMS", ""),
        "windKt": wind.get("windSpeedKnot", ""),
        "windKmh": round(float(wind["windSpeedMS"]) * 3.6) if wind.get("windSpeedMS") else "",
        "track": track,
        "detailUrl": f"{JMA_DETAIL_BASE}?id={storm_id}&lang=en",
        "sourceUrl": "https://www.data.jma.go.jp/multi/cyclone/index.html?lang=en",
    }


def fetch_storms() -> dict:
    now = time.time()
    if _cache["payload"] is not None and now - float(_cache["at"]) < CACHE_SECONDS:
        return _cache["payload"]  # type: ignore[return-value]

    storms = []
    errors = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {pool.submit(get_json, storm_id): storm_id for storm_id in CURRENT_IDS}
        for job in as_completed(jobs):
            storm_id = jobs[job]
            try:
                report = job.result()
                if is_recent(report):
                    storms.append(normalize(storm_id, report))
            except (OSError, URLError, ValueError, KeyError) as exc:
                errors.append(f"{storm_id}: {exc.__class__.__name__}")

    storms.sort(key=lambda item: item["reportDateTime"], reverse=True)
    payload = {
        "source": "Japan Meteorological Agency (JMA), RSMC Tokyo",
        "sourceUrl": "https://www.data.jma.go.jp/multi/cyclone/index.html?lang=en",
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "storms": storms,
        "partial": bool(errors) and bool(storms),
        "errors": errors,
    }
    _cache.update(at=now, payload=payload)
    return payload


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/api/storms":
            try:
                body = json.dumps(fetch_storms(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
            except Exception as exc:  # keep the page available with a useful error
                body = json.dumps({"error": str(exc), "storms": []}, ensure_ascii=False).encode("utf-8")
                self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        if self.path.startswith("/api/"):
            super().log_message(format, *args)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Serve the live storm dashboard")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"Storm dashboard: http://127.0.0.1:{args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()

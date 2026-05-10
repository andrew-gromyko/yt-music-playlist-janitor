#!/usr/bin/env python3
"""Core YouTube Music fetch, duplicate planning, and rating operations.

Rules:
1) Same link (same video_id) => duplicates
2) Same artist/channel + same title => duplicates

This module is used by the interactive CLI. It intentionally has no credential
storage or command-line entrypoint.
"""

from __future__ import annotations

import csv
import json
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


BASE_DIR = Path("/Users/andrew/Documents/git/yt-music-dedup")
BACKUP_ROOT = BASE_DIR / "duplicate_report" / "full_dedupe_backups"
PLAYLIST_ID = "LM"

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_API_BASE = "https://www.googleapis.com/youtube/v3"


class ApiError(RuntimeError):
    pass


@dataclass
class Row:
    idx: int
    playlist_item_id: str
    playlist_position: int
    video_id: str
    title: str
    channel: str

    @property
    def norm_title(self) -> str:
        return normalize(self.title)

    @property
    def norm_channel(self) -> str:
        return normalize(self.channel)

    @property
    def watch_url(self) -> str:
        return f"https://music.youtube.com/watch?v={self.video_id}" if self.video_id else ""


class DSU:
    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra = self.find(a)
        rb = self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1


def normalize(value: str) -> str:
    value = (value or "").strip().lower()
    return re.sub(r"\s+", " ", value)


def api_get_json(access_token: str, path: str, params: Dict[str, str]) -> Dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{YT_API_BASE}/{path}?{qs}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ApiError(f"GET {path} failed (HTTP {e.code}): {body}") from e


def api_rate(access_token: str, video_id: str, rating: str) -> None:
    qs = urllib.parse.urlencode({"id": video_id, "rating": rating})
    req = urllib.request.Request(
        f"{YT_API_BASE}/videos/rate?{qs}",
        headers={"Authorization": f"Bearer {access_token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req):
            return
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ApiError(f"videos.rate({video_id}, {rating}) failed (HTTP {e.code}): {body}") from e


def fetch_lm_rows(access_token: str) -> List[Row]:
    raw_rows: List[Row] = []
    page_token: Optional[str] = None
    idx = 0
    while True:
        params = {
            "part": "id,snippet,contentDetails,status",
            "playlistId": PLAYLIST_ID,
            "maxResults": "50",
        }
        if page_token:
            params["pageToken"] = page_token
        data = api_get_json(access_token, "playlistItems", params)
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            cd = it.get("contentDetails", {})
            raw_rows.append(
                Row(
                    idx=idx,
                    playlist_item_id=str(it.get("id", "")),
                    playlist_position=int(sn.get("position", 10**9)),
                    video_id=str(sn.get("resourceId", {}).get("videoId") or cd.get("videoId") or ""),
                    title=str(sn.get("title", "")),
                    channel=str(sn.get("videoOwnerChannelTitle", "")),
                )
            )
            idx += 1
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return raw_rows


def write_rows_csv(path: Path, rows: List[Row]) -> None:
    fieldnames = [
        "playlist_item_id",
        "playlist_position",
        "playlist_id",
        "video_id",
        "video_title",
        "video_channel_title",
        "watch_url",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "playlist_item_id": r.playlist_item_id,
                    "playlist_position": r.playlist_position,
                    "playlist_id": PLAYLIST_ID,
                    "video_id": r.video_id,
                    "video_title": r.title,
                    "video_channel_title": r.channel,
                    "watch_url": r.watch_url,
                }
            )


def build_dedupe_plan(rows: List[Row]) -> Dict:
    by_video: Dict[str, List[int]] = defaultdict(list)
    by_artist_title: Dict[Tuple[str, str], List[int]] = defaultdict(list)

    for r in rows:
        if r.video_id:
            by_video[r.video_id].append(r.idx)
        if r.norm_channel and r.norm_title:
            by_artist_title[(r.norm_channel, r.norm_title)].append(r.idx)

    by_video = {k: v for k, v in by_video.items() if len(v) > 1}
    by_artist_title = {k: v for k, v in by_artist_title.items() if len(v) > 1}

    dsu = DSU(len(rows))
    for idxs in by_video.values():
        first = idxs[0]
        for i in idxs[1:]:
            dsu.union(first, i)
    for idxs in by_artist_title.values():
        first = idxs[0]
        for i in idxs[1:]:
            dsu.union(first, i)

    components: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(rows)):
        components[dsu.find(i)].append(i)

    duplicate_components = [idxs for idxs in components.values() if len(idxs) > 1]

    keep_indices: Set[int] = set()
    remove_indices: Set[int] = set()

    for idxs in duplicate_components:
        sorted_by_position = sorted(idxs, key=lambda i: rows[i].playlist_position)
        keep = sorted_by_position[0]
        keep_indices.add(keep)
        for i in sorted_by_position[1:]:
            remove_indices.add(i)

    operations: List[Dict] = []
    by_video_all: Dict[str, List[int]] = defaultdict(list)
    for r in rows:
        if r.video_id:
            by_video_all[r.video_id].append(r.idx)

    for video_id, idxs in by_video_all.items():
        keep_here = [i for i in idxs if i in keep_indices]
        remove_here = [i for i in idxs if i in remove_indices]
        if not remove_here:
            continue
        if keep_here:
            action = "unlike_then_relike_once"
        else:
            action = "unlike_only"
        operations.append(
            {
                "action": action,
                "video_id": video_id,
                "affected_count": len(idxs),
                "kept_rows_for_video": len(keep_here),
                "removed_rows_for_video": len(remove_here),
            }
        )

    operations.sort(key=lambda x: (x["action"], x["video_id"]))

    remove_rows = [rows[i] for i in sorted(remove_indices, key=lambda i: rows[i].playlist_position)]
    keep_rows = [rows[i] for i in sorted(keep_indices, key=lambda i: rows[i].playlist_position)]

    plan = {
        "playlist_id": PLAYLIST_ID,
        "total_rows": len(rows),
        "duplicate_components": len(duplicate_components),
        "rows_to_keep_in_duplicate_components": len(keep_rows),
        "rows_to_remove": len(remove_rows),
        "duplicate_groups_same_video_id": len(by_video),
        "duplicate_groups_same_artist_title": len(by_artist_title),
        "video_rate_operations": len(operations),
        "estimated_quota_units_for_rate_calls": sum(
            50 if op["action"] == "unlike_only" else 100 for op in operations
        ),
        "operations": operations,
        "remove_rows": [
            {
                "playlist_position": r.playlist_position,
                "playlist_item_id": r.playlist_item_id,
                "video_id": r.video_id,
                "title": r.title,
                "channel": r.channel,
                "watch_url": r.watch_url,
            }
            for r in remove_rows
        ],
        "keep_rows": [
            {
                "playlist_position": r.playlist_position,
                "playlist_item_id": r.playlist_item_id,
                "video_id": r.video_id,
                "title": r.title,
                "channel": r.channel,
                "watch_url": r.watch_url,
            }
            for r in keep_rows
        ],
    }
    return plan


def execute_plan(access_token: str, plan: Dict) -> Dict:
    done = []
    for op in plan["operations"]:
        vid = op["video_id"]
        action = op["action"]
        if action == "unlike_only":
            api_rate(access_token, vid, "none")
            done.append({"video_id": vid, "steps": ["none"]})
        elif action == "unlike_then_relike_once":
            api_rate(access_token, vid, "none")
            time.sleep(0.15)
            api_rate(access_token, vid, "like")
            done.append({"video_id": vid, "steps": ["none", "like"]})
        else:
            raise ApiError(f"Unknown operation action: {action}")
    return {
        "executed_operation_count": len(done),
        "executed_operations": done,
    }


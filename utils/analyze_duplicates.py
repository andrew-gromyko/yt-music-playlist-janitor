#!/usr/bin/env python3
"""Analyze duplicates in YouTube Music liked export and build a local HTML viewer.

Duplicate rules:
1) Same link: identical `video_id`
2) Same artist + same song name: normalized `video_channel_title` + `video_title`
"""

from __future__ import annotations

import csv
import html
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Set, Tuple


def normalize_text(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    return value


BASE_DIR = "/Users/andrew/Documents/git/yt-music-dedup"
INPUT_CSV_PATH = os.path.join(BASE_DIR, "liked_music_export.csv")
OUTPUT_DIR_PATH = os.path.join(BASE_DIR, "duplicate_report")


@dataclass
class Row:
    index: int
    playlist_item_id: str
    playlist_position: str
    video_id: str
    video_title: str
    video_channel_title: str

    @property
    def watch_url(self) -> str:
        if not self.video_id:
            return ""
        return f"https://music.youtube.com/watch?v={self.video_id}"

    @property
    def norm_title(self) -> str:
        return normalize_text(self.video_title)

    @property
    def norm_artist(self) -> str:
        return normalize_text(self.video_channel_title)


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


def read_rows(input_csv: str) -> List[Row]:
    rows: List[Row] = []
    with open(input_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, raw in enumerate(reader):
            rows.append(
                Row(
                    index=i,
                    playlist_item_id=(raw.get("playlist_item_id") or "").strip(),
                    playlist_position=(raw.get("playlist_position") or "").strip(),
                    video_id=(raw.get("video_id") or "").strip(),
                    video_title=(raw.get("video_title") or "").strip(),
                    video_channel_title=(raw.get("video_channel_title") or "").strip(),
                )
            )
    return rows


def group_indices_by_key(rows: List[Row]) -> Tuple[Dict[str, List[int]], Dict[Tuple[str, str], List[int]]]:
    by_video: Dict[str, List[int]] = defaultdict(list)
    by_artist_title: Dict[Tuple[str, str], List[int]] = defaultdict(list)

    for r in rows:
        if r.video_id:
            by_video[r.video_id].append(r.index)
        if r.norm_artist and r.norm_title:
            by_artist_title[(r.norm_artist, r.norm_title)].append(r.index)

    by_video = {k: v for k, v in by_video.items() if len(v) > 1}
    by_artist_title = {k: v for k, v in by_artist_title.items() if len(v) > 1}
    return by_video, by_artist_title


def write_link_groups_csv(path: str, rows: List[Row], by_video: Dict[str, List[int]]) -> None:
    fieldnames = [
        "video_id",
        "instances",
        "duplicate_copies",
        "title_sample",
        "artist_sample",
        "watch_url",
        "playlist_item_ids",
        "playlist_positions",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for video_id, idxs in sorted(by_video.items(), key=lambda kv: len(kv[1]), reverse=True):
            sample = rows[idxs[0]]
            w.writerow(
                {
                    "video_id": video_id,
                    "instances": len(idxs),
                    "duplicate_copies": len(idxs) - 1,
                    "title_sample": sample.video_title,
                    "artist_sample": sample.video_channel_title,
                    "watch_url": sample.watch_url,
                    "playlist_item_ids": "|".join(rows[i].playlist_item_id for i in idxs),
                    "playlist_positions": "|".join(rows[i].playlist_position for i in idxs),
                }
            )


def write_artist_title_groups_csv(
    path: str, rows: List[Row], by_artist_title: Dict[Tuple[str, str], List[int]]
) -> None:
    fieldnames = [
        "artist_normalized",
        "title_normalized",
        "instances",
        "duplicate_copies",
        "artist_sample",
        "title_sample",
        "video_ids",
        "playlist_item_ids",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for (artist_norm, title_norm), idxs in sorted(
            by_artist_title.items(), key=lambda kv: len(kv[1]), reverse=True
        ):
            sample = rows[idxs[0]]
            w.writerow(
                {
                    "artist_normalized": artist_norm,
                    "title_normalized": title_norm,
                    "instances": len(idxs),
                    "duplicate_copies": len(idxs) - 1,
                    "artist_sample": sample.video_channel_title,
                    "title_sample": sample.video_title,
                    "video_ids": "|".join(rows[i].video_id for i in idxs),
                    "playlist_item_ids": "|".join(rows[i].playlist_item_id for i in idxs),
                }
            )


def build_components(
    rows: List[Row], by_video: Dict[str, List[int]], by_artist_title: Dict[Tuple[str, str], List[int]]
) -> List[Dict]:
    dsu = DSU(len(rows))
    for idxs in by_video.values():
        first = idxs[0]
        for i in idxs[1:]:
            dsu.union(first, i)
    for idxs in by_artist_title.values():
        first = idxs[0]
        for i in idxs[1:]:
            dsu.union(first, i)

    members: Dict[int, List[int]] = defaultdict(list)
    for i in range(len(rows)):
        members[dsu.find(i)].append(i)

    duplicated_components = [m for m in members.values() if len(m) > 1]
    component_lookup: Dict[int, int] = {}
    for cid, comp in enumerate(duplicated_components, start=1):
        for idx in comp:
            component_lookup[idx] = cid

    reasons: Dict[int, Set[str]] = defaultdict(set)
    for idxs in by_video.values():
        comp_id = component_lookup.get(idxs[0])
        if comp_id:
            reasons[comp_id].add("same_link")
    for idxs in by_artist_title.values():
        comp_id = component_lookup.get(idxs[0])
        if comp_id:
            reasons[comp_id].add("same_artist_and_title")

    components: List[Dict] = []
    for comp_id, idxs in enumerate(duplicated_components, start=1):
        titles = Counter(rows[i].video_title for i in idxs if rows[i].video_title)
        artists = Counter(rows[i].video_channel_title for i in idxs if rows[i].video_channel_title)
        rep_title = titles.most_common(1)[0][0] if titles else ""
        rep_artist = artists.most_common(1)[0][0] if artists else ""
        components.append(
            {
                "component_id": comp_id,
                "entries_count": len(idxs),
                "duplicate_copies": len(idxs) - 1,
                "reasons": ",".join(sorted(reasons.get(comp_id, set()))),
                "representative_title": rep_title,
                "representative_artist": rep_artist,
                "video_ids": "|".join(rows[i].video_id for i in idxs),
                "playlist_item_ids": "|".join(rows[i].playlist_item_id for i in idxs),
                "playlist_positions": "|".join(rows[i].playlist_position for i in idxs),
            }
        )

    components.sort(key=lambda x: x["entries_count"], reverse=True)
    return components


def write_components_csv(path: str, components: List[Dict]) -> None:
    fieldnames = [
        "component_id",
        "entries_count",
        "duplicate_copies",
        "reasons",
        "representative_title",
        "representative_artist",
        "video_ids",
        "playlist_item_ids",
        "playlist_positions",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(components)


def write_stats_csv(
    path: str,
    total_entries: int,
    by_video: Dict[str, List[int]],
    by_artist_title: Dict[Tuple[str, str], List[int]],
    components: List[Dict],
) -> Dict[str, int]:
    entries_by_link = sum(len(v) for v in by_video.values())
    extra_by_link = sum(len(v) - 1 for v in by_video.values())
    entries_by_artist = sum(len(v) for v in by_artist_title.values())
    extra_by_artist = sum(len(v) - 1 for v in by_artist_title.values())
    duplicated_entries_combined = sum(c["entries_count"] for c in components)
    duplicate_copies_combined = sum(c["duplicate_copies"] for c in components)

    stats = {
        "total_playlist_entries": total_entries,
        "link_duplicate_groups": len(by_video),
        "entries_in_link_duplicate_groups": entries_by_link,
        "duplicate_copies_by_link": extra_by_link,
        "artist_title_duplicate_groups": len(by_artist_title),
        "entries_in_artist_title_duplicate_groups": entries_by_artist,
        "duplicate_copies_by_artist_title": extra_by_artist,
        "combined_duplicate_components": len(components),
        "entries_marked_duplicate_combined": duplicated_entries_combined,
        "duplicate_copies_combined": duplicate_copies_combined,
        "unique_entries_after_dedup_estimate": total_entries - duplicate_copies_combined,
    }

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(stats.keys()))
        w.writeheader()
        w.writerow(stats)
    return stats


def _table_rows_from_components(components: List[Dict]) -> str:
    rows_html = []
    for c in components:
        first_video_id = c["video_ids"].split("|")[0] if c["video_ids"] else ""
        watch = f"https://music.youtube.com/watch?v={first_video_id}" if first_video_id else ""
        watch_cell = f'<a href="{html.escape(watch)}" target="_blank">open</a>' if watch else ""
        rows_html.append(
            "<tr>"
            f"<td>{c['entries_count']}</td>"
            f"<td>{c['duplicate_copies']}</td>"
            f"<td>{html.escape(c['representative_title'])}</td>"
            f"<td>{html.escape(c['representative_artist'])}</td>"
            f"<td>{html.escape(c['reasons'])}</td>"
            f"<td>{watch_cell}</td>"
            "</tr>"
        )
    return "\n".join(rows_html)


def _table_rows_simple(groups: Iterable[Tuple[str, int, int, str, str, str]]) -> str:
    rows_html = []
    for key, instances, copies, title, artist, watch in groups:
        watch_cell = f'<a href="{html.escape(watch)}" target="_blank">open</a>' if watch else ""
        rows_html.append(
            "<tr>"
            f"<td>{instances}</td>"
            f"<td>{copies}</td>"
            f"<td>{html.escape(title)}</td>"
            f"<td>{html.escape(artist)}</td>"
            f"<td>{html.escape(key)}</td>"
            f"<td>{watch_cell}</td>"
            "</tr>"
        )
    return "\n".join(rows_html)


def build_viewer_html(
    out_html: str,
    stats: Dict[str, int],
    components: List[Dict],
    by_video: Dict[str, List[int]],
    by_artist_title: Dict[Tuple[str, str], List[int]],
    rows: List[Row],
) -> None:
    link_groups = []
    for vid, idxs in sorted(by_video.items(), key=lambda kv: len(kv[1]), reverse=True):
        s = rows[idxs[0]]
        link_groups.append((vid, len(idxs), len(idxs) - 1, s.video_title, s.video_channel_title, s.watch_url))

    artist_groups = []
    for (artist_norm, title_norm), idxs in sorted(by_artist_title.items(), key=lambda kv: len(kv[1]), reverse=True):
        s = rows[idxs[0]]
        key = f"{artist_norm} | {title_norm}"
        artist_groups.append((key, len(idxs), len(idxs) - 1, s.video_title, s.video_channel_title, s.watch_url))

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube Music Duplicate Report</title>
  <style>
    :root {{
      --bg: #f6f8fb;
      --card: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --line: #e5e7eb;
      --accent: #0f766e;
    }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", Helvetica, Arial, sans-serif;
      color: var(--text);
      background: radial-gradient(circle at top right, #e6f4f1, var(--bg) 55%);
    }}
    .wrap {{
      max-width: 1100px;
      margin: 28px auto;
      padding: 0 16px 24px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 16px;
      margin-bottom: 14px;
      box-shadow: 0 6px 24px rgba(17, 24, 39, 0.05);
    }}
    h1 {{ margin: 4px 0 8px; font-size: 24px; }}
    h2 {{ margin: 0 0 10px; font-size: 18px; }}
    p {{ margin: 0; color: var(--muted); }}
    .kpi {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin-top: 12px;
    }}
    .kpi > div {{
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
    }}
    .num {{ font-size: 22px; font-weight: 700; color: var(--accent); line-height: 1.1; }}
    .lbl {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid var(--line); text-align: left; padding: 8px 6px; vertical-align: top; }}
    th {{ font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.02em; }}
    a {{ color: var(--accent); text-decoration: none; }}
    .hint {{ font-size: 12px; color: var(--muted); margin-top: 8px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>YouTube Music Duplicate Report</h1>
      <p>Rules: same link (<code>video_id</code>) OR same artist + same title.</p>
      <div class="kpi">
        <div><div class="num">{stats['total_playlist_entries']}</div><div class="lbl">Total playlist entries</div></div>
        <div><div class="num">{stats['entries_marked_duplicate_combined']}</div><div class="lbl">Entries flagged as duplicates (combined)</div></div>
        <div><div class="num">{stats['duplicate_copies_combined']}</div><div class="lbl">Duplicate copies (remove these to dedupe)</div></div>
        <div><div class="num">{stats['unique_entries_after_dedup_estimate']}</div><div class="lbl">Estimated unique entries after dedupe</div></div>
      </div>
    </div>

    <div class="card">
      <h2>Combined Duplicate Components</h2>
      <table>
        <thead>
          <tr>
            <th>Instances</th><th>Duplicates</th><th>Title</th><th>Artist/Channel</th><th>Reasons</th><th>Link</th>
          </tr>
        </thead>
        <tbody>
          {_table_rows_from_components(components)}
        </tbody>
      </table>
      <div class="hint">Sorted by largest duplicate groups first.</div>
    </div>

    <div class="card">
      <h2>Same Link Groups (video_id)</h2>
      <table>
        <thead>
          <tr>
            <th>Instances</th><th>Duplicates</th><th>Title</th><th>Artist/Channel</th><th>video_id</th><th>Link</th>
          </tr>
        </thead>
        <tbody>
          {_table_rows_simple(link_groups)}
        </tbody>
      </table>
    </div>

    <div class="card">
      <h2>Same Artist + Title Groups</h2>
      <table>
        <thead>
          <tr>
            <th>Instances</th><th>Duplicates</th><th>Title</th><th>Artist/Channel</th><th>Normalized Key</th><th>Link</th>
          </tr>
        </thead>
        <tbody>
          {_table_rows_simple(artist_groups)}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html_doc)


def main() -> int:
    out_dir = os.path.abspath(OUTPUT_DIR_PATH)
    os.makedirs(out_dir, exist_ok=True)

    rows = read_rows(INPUT_CSV_PATH)
    by_video, by_artist_title = group_indices_by_key(rows)
    components = build_components(rows, by_video, by_artist_title)

    csv_link = os.path.join(out_dir, "duplicate_groups_by_link.csv")
    csv_artist = os.path.join(out_dir, "duplicate_groups_by_artist_title.csv")
    csv_components = os.path.join(out_dir, "duplicate_components_combined.csv")
    csv_stats = os.path.join(out_dir, "duplicate_stats.csv")
    html_viewer = os.path.join(out_dir, "duplicates_viewer.html")

    write_link_groups_csv(csv_link, rows, by_video)
    write_artist_title_groups_csv(csv_artist, rows, by_artist_title)
    write_components_csv(csv_components, components)
    stats = write_stats_csv(csv_stats, len(rows), by_video, by_artist_title, components)
    build_viewer_html(html_viewer, stats, components, by_video, by_artist_title, rows)

    print(f"Input entries: {len(rows)}")
    print(f"Duplicate components (combined): {stats['combined_duplicate_components']}")
    print(f"Entries marked duplicate (combined): {stats['entries_marked_duplicate_combined']}")
    print(f"Duplicate copies to remove (combined): {stats['duplicate_copies_combined']}")
    print(f"Estimated unique entries after dedupe: {stats['unique_entries_after_dedup_estimate']}")
    print("")
    print(f"Wrote: {csv_link}")
    print(f"Wrote: {csv_artist}")
    print(f"Wrote: {csv_components}")
    print(f"Wrote: {csv_stats}")
    print(f"Wrote: {html_viewer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

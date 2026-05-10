#!/usr/bin/env python3
"""One-stop CLI for YouTube Music liked-song dedupe."""

from __future__ import annotations

import csv
import curses
import datetime as dt
import io
import json
import os
import queue
import re
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import analyze_duplicates as analysis
import dedupe_core as live
import secure_store
from oauth_device import OAuthDeviceClient


BASE_DIR = Path("/Users/andrew/Documents/git/yt-music-dedup")
EXPORT_CSV = BASE_DIR / "liked_music_export.csv"
REPORT_DIR = BASE_DIR / "duplicate_report"
STATS_CSV = REPORT_DIR / "duplicate_stats.csv"
COMPONENTS_CSV = REPORT_DIR / "duplicate_components_combined.csv"
BACKUP_ROOT = REPORT_DIR / "full_dedupe_backups"
STALE_SCAN_SECONDS = 20 * 60
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class Cancelled(RuntimeError):
    pass


class C:
    reset = "\033[0m"
    bold = "\033[1m"
    dim = "\033[2m"
    red = "\033[31m"
    green = "\033[32m"
    yellow = "\033[33m"
    blue = "\033[34m"
    cyan = "\033[36m"


def color(text: str, style: str) -> str:
    return f"{style}{text}{C.reset}"


def ok(text: str) -> str:
    return color(text, C.green)


def warn(text: str) -> str:
    return color(text, C.yellow)


def bad(text: str) -> str:
    return color(text, C.red)


def title(text: str) -> str:
    return color(text, C.bold + C.cyan)


def print_header() -> None:
    print()
    print(title("YouTube Music Dedup"))
    print(color("Liked songs, duplicate scan, and careful cleanup.", C.dim))
    print()


def explain_auth_error(raw: str) -> str:
    text = raw.lower()
    if "service_disabled" in text or "accessnotconfigured" in text or "youtube data api" in text:
        return "YouTube Data API v3 is disabled for this Google Cloud project. Enable it and try again."
    if "org_internal" in text:
        return "OAuth app is Internal. Change the OAuth audience to External, then add yourself as a test user."
    if "access_denied" in text and "testing" in text:
        return "OAuth app is in Testing mode and this Google account is not listed as a test user."
    if "invalid_client" in text or "unauthorized_client" in text:
        return "OAuth client ID/secret did not work. Check that you created a TV and Limited Input client."
    if "invalid_grant" in text:
        return "Saved authorization expired or was revoked. Run setup again."
    if "insufficient authentication scopes" in text:
        return f"OAuth scope is too narrow. This app needs {live.YOUTUBE_SCOPE}."
    return raw


def require_live_access() -> str:
    creds = secure_store.get_client_credentials()
    if not creds:
        if sys.stdin.isatty():
            creds = secure_store.prompt_for_client_credentials()
        else:
            raise live.ApiError("OAuth is not set up yet. Run `python3 playlist_janitor.py setup` first.")

    oauth = OAuthDeviceClient(creds.client_id, creds.client_secret)
    refresh_token = secure_store.get_refresh_token()
    if refresh_token:
        try:
            token = oauth.refresh_access_token(refresh_token)
            return token.access_token
        except Exception as e:
            raise live.ApiError(explain_auth_error(str(e))) from e

    try:
        device = oauth.start_device_flow()
    except Exception as e:
        raise live.ApiError(explain_auth_error(str(e))) from e

    verification_url = device.get("verification_url") or device.get("verification_uri")
    user_code = device.get("user_code")
    if not verification_url or not user_code:
        raise live.ApiError(f"Google returned an unexpected device auth response: {device}")

    print()
    print("Authorize this app:")
    print(f"  1. Open {verification_url}")
    print(f"  2. Enter code {user_code}")
    print()
    print("Waiting for Google authorization...")

    try:
        token = oauth.poll_for_token(device["device_code"], int(device.get("interval", 5)))
    except Exception as e:
        raise live.ApiError(explain_auth_error(str(e))) from e

    if not token.refresh_token:
        raise live.ApiError("Google did not return a refresh token. Revoke app access and authorize again.")
    secure_store.save_refresh_token(token.refresh_token)
    return token.access_token


def analyze_local() -> Dict[str, int]:
    rows = analysis.read_rows(str(EXPORT_CSV))
    by_video, by_artist_title = analysis.group_indices_by_key(rows)
    components = analysis.build_components(rows, by_video, by_artist_title)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    analysis.write_link_groups_csv(str(REPORT_DIR / "duplicate_groups_by_link.csv"), rows, by_video)
    analysis.write_artist_title_groups_csv(
        str(REPORT_DIR / "duplicate_groups_by_artist_title.csv"), rows, by_artist_title
    )
    analysis.write_components_csv(str(COMPONENTS_CSV), components)
    return analysis.write_stats_csv(str(STATS_CSV), len(rows), by_video, by_artist_title, components)


def read_stats() -> Optional[Dict[str, int]]:
    if not STATS_CSV.exists():
        return None
    with STATS_CSV.open(newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f), None)
    if not row:
        return None
    return {k: int(v) for k, v in row.items()}


def last_scan_age_seconds() -> Optional[float]:
    if not EXPORT_CSV.exists():
        return None
    return max(0.0, time.time() - EXPORT_CSV.stat().st_mtime)


def format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h {minutes % 60}m ago"
    days = hours // 24
    return f"{days}d {hours % 24}h ago"


def check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("Cancelled. No new artifacts were written.")


def fetch_lm_rows_cancellable(access_token: str, cancel_event: Optional[threading.Event]) -> List[live.Row]:
    rows: List[live.Row] = []
    page_token: Optional[str] = None
    idx = 0
    while True:
        check_cancel(cancel_event)
        params = {
            "part": "id,snippet,contentDetails,status",
            "playlistId": live.PLAYLIST_ID,
            "maxResults": "50",
        }
        if page_token:
            params["pageToken"] = page_token
        data = live.api_get_json(access_token, "playlistItems", params)
        check_cancel(cancel_event)
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            cd = it.get("contentDetails", {})
            rows.append(
                live.Row(
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
            return rows


def print_stats(stats: Dict[str, int], heading: str = "Status") -> None:
    print(title(heading))
    print(f"  Total liked entries:       {color(str(stats['total_playlist_entries']), C.bold)}")
    print(f"  Duplicate groups:          {color(str(stats['combined_duplicate_components']), C.bold)}")
    print(f"  Entries in duplicate sets: {color(str(stats['entries_marked_duplicate_combined']), C.bold)}")
    print(f"  Duplicate copies:          {color(str(stats['duplicate_copies_combined']), C.bold)}")
    print(f"  Estimated after cleanup:   {color(str(stats['unique_entries_after_dedup_estimate']), C.bold)}")
    print()


def scan_live_and_analyze(cancel_event: Optional[threading.Event] = None) -> Dict[str, int]:
    print(color("Fetching live YouTube Music liked songs...", C.dim))
    access_token = require_live_access()
    check_cancel(cancel_event)
    rows = fetch_lm_rows_cancellable(access_token, cancel_event)
    check_cancel(cancel_event)
    live.write_rows_csv(EXPORT_CSV, rows)
    print(ok(f"Exported {len(rows)} live liked songs to {EXPORT_CSV.name}."))

    check_cancel(cancel_event)
    stats = analyze_local()
    print(ok("Duplicate CSVs refreshed."))
    print()
    print_stats(stats, "Fresh Live Scan")
    return stats


def show_duplicates(limit: int = 20) -> None:
    if not COMPONENTS_CSV.exists():
        print(warn("No duplicate report yet. Run scan first."))
        return

    with COMPONENTS_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print(ok("No duplicates found."))
        return

    print(title(f"Top Duplicate Groups ({min(limit, len(rows))}/{len(rows)})"))
    for i, row in enumerate(rows[:limit], start=1):
        count = row["entries_count"]
        copies = row["duplicate_copies"]
        reason = row["reasons"].replace("_", " ")
        song = row["representative_title"]
        artist = row["representative_artist"]
        print(f"{color(str(i).rjust(2), C.dim)}. {color(song, C.bold)}")
        print(f"    {artist}")
        print(f"    {count} entries, {copies} duplicate copies | {color(reason, C.dim)}")
    print()


def make_run_dir() -> Path:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = BACKUP_ROOT / now
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def preview_full_plan(cancel_event: Optional[threading.Event] = None) -> Tuple[Path, Dict]:
    print(color("Fetching live playlist and building cleanup plan...", C.dim))
    access_token = require_live_access()
    check_cancel(cancel_event)
    rows = fetch_lm_rows_cancellable(access_token, cancel_event)
    check_cancel(cancel_event)
    run_dir = make_run_dir()

    before_csv = run_dir / "liked_music_before.csv"
    plan_path = run_dir / "dedupe_plan.json"
    live.write_rows_csv(before_csv, rows)

    check_cancel(cancel_event)
    plan = live.build_dedupe_plan(rows)
    plan["mode"] = "dry_run"
    plan["generated_at_utc"] = run_dir.name
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    print(ok("Plan ready. No likes changed."))
    print(f"  Before backup: {before_csv}")
    print(f"  Plan JSON:     {plan_path}")
    print()
    print_plan_summary(plan)
    return run_dir, plan


def print_plan_summary(plan: Dict) -> None:
    print(title("Plan"))
    print(f"  Total live entries:     {color(str(plan['total_rows']), C.bold)}")
    print(f"  Duplicate groups:       {color(str(plan['duplicate_components']), C.bold)}")
    print(f"  Rows to remove:         {color(str(plan['rows_to_remove']), C.bold)}")
    print(f"  API rate operations:    {color(str(plan['video_rate_operations']), C.bold)}")
    print(f"  Est. quota units:       {color(str(plan['estimated_quota_units_for_rate_calls']), C.bold)}")
    print()


def ensure_recent_scan(cancel_event: Optional[threading.Event] = None) -> None:
    age = last_scan_age_seconds()
    if age is not None and age <= STALE_SCAN_SECONDS:
        return

    if age is None:
        print(warn("No previous live scan found. Running one before dedupe."))
    else:
        print(warn(f"Last live scan was {format_age(age)}. Refreshing before dedupe."))
    scan_live_and_analyze(cancel_event)


def execute_full_dedupe() -> None:
    ensure_recent_scan()
    run_dir, plan = preview_full_plan()
    if plan["rows_to_remove"] == 0:
        print(ok("Nothing to remove. Your liked songs are clean by these rules."))
        return

    print(warn("This will unlike duplicate videos according to the plan above."))
    confirmation = input(color("Type DEDUPE to continue: ", C.bold))
    if confirmation.strip() != "DEDUPE":
        print(warn("Cancelled. No likes changed."))
        return

    access_token = require_live_access()
    exec_result = live.execute_plan(access_token, plan)
    exec_path = run_dir / "execution_log.json"
    exec_path.write_text(json.dumps(exec_result, indent=2, ensure_ascii=False), encoding="utf-8")

    access_token = require_live_access()
    after_rows = live.fetch_lm_rows(access_token)
    after_csv = run_dir / "liked_music_after.csv"
    live.write_rows_csv(after_csv, after_rows)

    after_plan = live.build_dedupe_plan(after_rows)
    after_summary = {
        "total_rows_after": after_plan["total_rows"],
        "duplicate_components_after": after_plan["duplicate_components"],
        "rows_to_remove_after": after_plan["rows_to_remove"],
    }
    after_path = run_dir / "after_summary.json"
    after_path.write_text(json.dumps(after_summary, indent=2), encoding="utf-8")

    live.write_rows_csv(EXPORT_CSV, after_rows)
    stats = analyze_local()

    print()
    print(ok("Full dedupe complete."))
    print(f"  Execution log: {exec_path}")
    print(f"  After backup:  {after_csv}")
    print(f"  After summary: {after_path}")
    print()
    print_stats(stats, "Updated Local Report")


def local_status() -> None:
    if not EXPORT_CSV.exists():
        print(warn("No local export found yet."))
        return

    try:
        stats = analyze_local()
    except FileNotFoundError:
        print(warn("Local export is missing. Run scan first."))
        return
    print_stats(stats, "Local Report")


def setup_credentials() -> None:
    secure_store.reset_client_credentials()
    print("Credentials saved.")
    print("Starting account authorization and first scan.")
    scan_live_and_analyze()


def menu() -> None:
    curses.wrapper(curses_menu)


def _capture_output(fn: Callable[[], object]) -> Tuple[str, Optional[BaseException]]:
    buf = io.StringIO()
    try:
        with redirect_stdout(buf), redirect_stderr(buf):
            fn()
        return buf.getvalue(), None
    except Exception as e:
        return buf.getvalue(), e


def _plain_lines(text: str) -> List[str]:
    return [ANSI_RE.sub("", line) for line in text.splitlines()]


def _stats_lines() -> List[str]:
    stats = read_stats()
    if not stats and EXPORT_CSV.exists():
        try:
            stats = analyze_local()
        except OSError:
            stats = None

    if not stats:
        return [f"Last live scan          {format_age(last_scan_age_seconds())}", "No local report yet. Run Smart scan first."]

    return [
        f"Last live scan          {format_age(last_scan_age_seconds())}",
        f"Total liked entries       {stats['total_playlist_entries']}",
        f"Duplicate groups          {stats['combined_duplicate_components']}",
        f"Entries in duplicate sets {stats['entries_marked_duplicate_combined']}",
        f"Duplicate copies          {stats['duplicate_copies_combined']}",
        f"Estimated after cleanup   {stats['unique_entries_after_dedup_estimate']}",
    ]


def _duplicate_preview_lines(limit: int = 30) -> List[str]:
    if not COMPONENTS_CSV.exists():
        return ["No duplicate report yet. Run Smart scan first."]

    with COMPONENTS_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return ["No duplicates found."]

    lines = [f"Top duplicate groups ({min(limit, len(rows))}/{len(rows)})", ""]
    for i, row in enumerate(rows[:limit], start=1):
        reason = row["reasons"].replace("_", " ")
        lines.append(f"{i:2}. {row['representative_title']}")
        lines.append(f"    {row['representative_artist']}")
        lines.append(f"    {row['entries_count']} entries, {row['duplicate_copies']} duplicates | {reason}")
        lines.append("")
    return lines


def _draw_text_screen(stdscr: "curses._CursesWindow", title_text: str, lines: List[str]) -> None:
    selected = 0
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 2, title_text, max(1, w - 4), curses.color_pair(1) | curses.A_BOLD)
        stdscr.addnstr(1, 2, "Up/Down scroll | q/backspace return", max(1, w - 4), curses.color_pair(4))
        usable_h = max(1, h - 4)
        max_scroll = max(0, len(lines) - usable_h)
        selected = min(selected, max_scroll)

        for y, line in enumerate(lines[selected : selected + usable_h], start=3):
            stdscr.addnstr(y, 2, line, max(1, w - 4))

        ch = stdscr.getch()
        if ch in (ord("q"), 27, curses.KEY_BACKSPACE, 127):
            return
        if ch in (curses.KEY_DOWN, ord("j")):
            selected = min(max_scroll, selected + 1)
        elif ch in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif ch == curses.KEY_NPAGE:
            selected = min(max_scroll, selected + usable_h)
        elif ch == curses.KEY_PPAGE:
            selected = max(0, selected - usable_h)


def _review_text_screen(stdscr: "curses._CursesWindow", title_text: str, lines: List[str]) -> bool:
    selected = 0
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 2, title_text, max(1, w - 4), curses.color_pair(1) | curses.A_BOLD)
        stdscr.addnstr(1, 2, "Enter continues | q/backspace cancels | Up/Down scroll", max(1, w - 4), curses.color_pair(4))
        usable_h = max(1, h - 4)
        max_scroll = max(0, len(lines) - usable_h)
        selected = min(selected, max_scroll)

        for y, line in enumerate(lines[selected : selected + usable_h], start=3):
            stdscr.addnstr(y, 2, line, max(1, w - 4))

        ch = stdscr.getch()
        if ch in (10, 13):
            return True
        if ch in (ord("q"), 27, curses.KEY_BACKSPACE, 127):
            return False
        if ch in (curses.KEY_DOWN, ord("j")):
            selected = min(max_scroll, selected + 1)
        elif ch in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif ch == curses.KEY_NPAGE:
            selected = min(max_scroll, selected + usable_h)
        elif ch == curses.KEY_PPAGE:
            selected = max(0, selected - usable_h)


def _run_action_screen(
    stdscr: "curses._CursesWindow",
    title_text: str,
    fn: Callable[[], object],
) -> None:
    stdscr.clear()
    h, w = stdscr.getmaxyx()
    stdscr.addnstr(0, 2, title_text, max(1, w - 4), curses.color_pair(1) | curses.A_BOLD)
    stdscr.addnstr(2, 2, "Working...", max(1, w - 4), curses.color_pair(4))
    stdscr.refresh()

    output, err = _capture_output(fn)
    lines = _plain_lines(output)
    if err:
        lines.append("")
        lines.append(f"Error: {err}")
    if not lines:
        lines = ["Done."]
    lines.append("")
    lines.append("Press q or backspace to return.")
    _draw_text_screen(stdscr, title_text, lines)


def _run_cancellable_action_screen(
    stdscr: "curses._CursesWindow",
    title_text: str,
    fn: Callable[[threading.Event], object],
    show_result: bool = True,
) -> Tuple[str, Optional[BaseException]]:
    cancel_event = threading.Event()
    result_q: "queue.Queue[Tuple[str, Optional[BaseException]]]" = queue.Queue(maxsize=1)

    def worker() -> None:
        result_q.put(_capture_output(lambda: fn(cancel_event)))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    spinner = ["|", "/", "-", "\\"]
    frame = 0
    stopping = False
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 2, title_text, max(1, w - 4), curses.color_pair(1) | curses.A_BOLD)
        hint = "q/backspace cancels | waiting for current request to stop" if stopping else "q/backspace cancels"
        stdscr.addnstr(1, 2, hint, max(1, w - 4), curses.color_pair(4))
        status = "Stopping..." if stopping else f"Working... {spinner[frame % len(spinner)]}"
        stdscr.addnstr(3, 2, status, max(1, w - 4), curses.color_pair(3) | curses.A_BOLD)
        stdscr.refresh()

        try:
            output, err = result_q.get_nowait()
            if not show_result:
                return output, err
            lines = _plain_lines(output)
            if err:
                lines.append("")
                lines.append(f"Error: {err}")
            if not lines:
                lines = ["Done."]
            lines.append("")
            lines.append("Press q or backspace to return.")
            _draw_text_screen(stdscr, title_text, lines)
            return output, err
        except queue.Empty:
            pass

        stdscr.timeout(120)
        ch = stdscr.getch()
        if ch in (ord("q"), 27, curses.KEY_BACKSPACE, 127):
            cancel_event.set()
            stopping = True
        frame += 1


def _confirm_dedupe_screen(stdscr: "curses._CursesWindow") -> bool:
    typed = ""
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 2, "Execute Full Dedupe", max(1, w - 4), curses.color_pair(3) | curses.A_BOLD)
        msg = "This will unlike duplicate videos from your YouTube Music liked songs."
        stdscr.addnstr(2, 2, msg, max(1, w - 4), curses.color_pair(3))
        stdscr.addnstr(4, 2, "Type DEDUPE to continue, or Esc to cancel.", max(1, w - 4))
        stdscr.addnstr(6, 2, f"> {typed}", max(1, w - 4), curses.A_BOLD)
        ch = stdscr.getch()
        if ch in (27, ord("q")):
            return False
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            typed = typed[:-1]
        elif ch in (10, 13):
            return typed == "DEDUPE"
        elif 32 <= ch <= 126 and len(typed) < 24:
            typed += chr(ch)


def _read_field(stdscr: "curses._CursesWindow", y: int, x: int, secret: bool = False) -> str:
    value = ""
    while True:
        display = "*" * len(value) if secret else value
        stdscr.move(y, x)
        stdscr.clrtoeol()
        stdscr.addstr(y, x, display)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (10, 13):
            return value.strip()
        if ch in (27,):
            return ""
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            value = value[:-1]
        elif 32 <= ch <= 126:
            value += chr(ch)


def _setup_oauth_screen(stdscr: "curses._CursesWindow", run_scan_after: bool = True) -> None:
    try:
        curses.curs_set(1)
    except curses.error:
        pass

    stdscr.clear()
    h, w = stdscr.getmaxyx()
    stdscr.addnstr(0, 2, "Setup OAuth", max(1, w - 4), curses.color_pair(1) | curses.A_BOLD)
    stdscr.addnstr(2, 2, "Paste the OAuth client from Google Cloud.", max(1, w - 4))
    stdscr.addnstr(4, 2, "Client ID:", max(1, w - 4))
    client_id = _read_field(stdscr, 4, 14)
    if not client_id:
        _draw_text_screen(stdscr, "Setup OAuth", ["Cancelled."])
        return

    stdscr.addnstr(6, 2, "Client secret:", max(1, w - 4))
    client_secret = _read_field(stdscr, 6, 17, secret=True)
    if not client_secret:
        _draw_text_screen(stdscr, "Setup OAuth", ["Cancelled."])
        return

    secure_store.save_client_credentials(client_id, client_secret)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    authorized = _authorize_oauth_screen(stdscr)
    if run_scan_after and authorized:
        _run_cancellable_action_screen(stdscr, "Smart Scan", scan_live_and_analyze)


def _authorize_oauth_screen(stdscr: "curses._CursesWindow") -> bool:
    creds = secure_store.get_client_credentials()
    if not creds:
        _draw_text_screen(stdscr, "Setup OAuth", ["Missing OAuth credentials."])
        return False

    stdscr.clear()
    stdscr.addnstr(0, 2, "Authorize Google Account", 80, curses.color_pair(1) | curses.A_BOLD)
    stdscr.addnstr(2, 2, "Starting Google device authorization...", 120, curses.color_pair(4))
    stdscr.refresh()

    oauth = OAuthDeviceClient(creds.client_id, creds.client_secret)
    try:
        device = oauth.start_device_flow()
    except Exception as e:
        _draw_text_screen(stdscr, "Authorize Google Account", [f"Error: {explain_auth_error(str(e))}"])
        return False

    verification_url = device.get("verification_url") or device.get("verification_uri")
    user_code = device.get("user_code")
    device_code = device.get("device_code")
    if not verification_url or not user_code or not device_code:
        _draw_text_screen(stdscr, "Authorize Google Account", [f"Google returned an unexpected response: {device}"])
        return False

    cancel_event = threading.Event()
    result_q: "queue.Queue[Tuple[Optional[str], Optional[BaseException]]]" = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            token = oauth.poll_for_token(
                str(device_code),
                int(device.get("interval", 5)),
                should_cancel=cancel_event.is_set,
            )
            if not token.refresh_token:
                raise live.ApiError("Google did not return a refresh token. Revoke app access and authorize again.")
            secure_store.save_refresh_token(token.refresh_token)
            result_q.put(("Authorized successfully.", None))
        except Exception as e:
            result_q.put((None, e))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    spinner = ["|", "/", "-", "\\"]
    frame = 0
    stopping = False
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 2, "Authorize Google Account", max(1, w - 4), curses.color_pair(1) | curses.A_BOLD)
        hint = "q/backspace cancels | waiting for current request to stop" if stopping else "q/backspace cancels"
        stdscr.addnstr(1, 2, hint, max(1, w - 4), curses.color_pair(4))
        stdscr.addnstr(3, 2, "1. Open this URL:", max(1, w - 4), curses.A_BOLD)
        stdscr.addnstr(4, 5, str(verification_url), max(1, w - 7), curses.color_pair(4))
        stdscr.addnstr(6, 2, "2. Enter this code:", max(1, w - 4), curses.A_BOLD)
        stdscr.addnstr(7, 5, str(user_code), max(1, w - 7), curses.color_pair(3) | curses.A_BOLD)
        status = "Stopping..." if stopping else f"Waiting for Google authorization... {spinner[frame % len(spinner)]}"
        stdscr.addnstr(9, 2, status, max(1, w - 4), curses.color_pair(3))
        stdscr.refresh()

        try:
            msg, err = result_q.get_nowait()
            if err:
                text = explain_auth_error(str(err))
                if "cancelled" in text.lower():
                    _draw_text_screen(stdscr, "Authorize Google Account", ["Cancelled. Account was not authorized."])
                else:
                    _draw_text_screen(stdscr, "Authorize Google Account", [f"Error: {text}"])
                return False
            _ = msg
            return True
        except queue.Empty:
            pass

        stdscr.timeout(120)
        ch = stdscr.getch()
        if ch in (ord("q"), 27, curses.KEY_BACKSPACE, 127):
            cancel_event.set()
            stopping = True
        frame += 1


def _ensure_authorized_screen(stdscr: "curses._CursesWindow") -> bool:
    if not secure_store.has_client_credentials():
        _setup_oauth_screen(stdscr, run_scan_after=False)
        return bool(secure_store.get_refresh_token())
    if secure_store.get_refresh_token():
        return True
    return _authorize_oauth_screen(stdscr)


def _smart_scan_action(stdscr: "curses._CursesWindow") -> None:
    if not _ensure_authorized_screen(stdscr):
        return
    _run_cancellable_action_screen(stdscr, "Smart Scan", scan_live_and_analyze)


def _preview_plan_action(stdscr: "curses._CursesWindow") -> None:
    if not _ensure_authorized_screen(stdscr):
        return
    _run_cancellable_action_screen(stdscr, "Preview Full Dedupe Plan", preview_full_plan)


def _execute_existing_plan(run_dir: Path, plan: Dict) -> None:
    access_token = require_live_access()
    exec_result = live.execute_plan(access_token, plan)
    exec_path = run_dir / "execution_log.json"
    exec_path.write_text(json.dumps(exec_result, indent=2, ensure_ascii=False), encoding="utf-8")

    access_token = require_live_access()
    after_rows = live.fetch_lm_rows(access_token)
    after_csv = run_dir / "liked_music_after.csv"
    live.write_rows_csv(after_csv, after_rows)

    after_plan = live.build_dedupe_plan(after_rows)
    after_summary = {
        "total_rows_after": after_plan["total_rows"],
        "duplicate_components_after": after_plan["duplicate_components"],
        "rows_to_remove_after": after_plan["rows_to_remove"],
    }
    after_path = run_dir / "after_summary.json"
    after_path.write_text(json.dumps(after_summary, indent=2), encoding="utf-8")

    live.write_rows_csv(EXPORT_CSV, after_rows)
    stats = analyze_local()

    print("Full dedupe complete.")
    print(f"Execution log: {exec_path}")
    print(f"After backup:  {after_csv}")
    print(f"After summary: {after_path}")
    print("")
    print(f"Total liked entries:       {stats['total_playlist_entries']}")
    print(f"Duplicate groups:          {stats['combined_duplicate_components']}")
    print(f"Duplicate copies:          {stats['duplicate_copies_combined']}")


def _execute_action(stdscr: "curses._CursesWindow") -> None:
    if not _ensure_authorized_screen(stdscr):
        return

    age = last_scan_age_seconds()
    if age is None or age > STALE_SCAN_SECONDS:
        age_text = format_age(age)
        stdscr.clear()
        stdscr.addnstr(0, 2, "Refreshing Stale Scan", 80, curses.color_pair(3) | curses.A_BOLD)
        stdscr.addnstr(2, 2, f"Last live scan was {age_text}. Refreshing before dedupe.", 120)
        stdscr.refresh()
        output, err = _run_cancellable_action_screen(
            stdscr,
            "Refreshing Stale Scan",
            scan_live_and_analyze,
            show_result=False,
        )
        if err:
            lines = _plain_lines(output) + ["", f"Error: {err}", "", "Press q or backspace to return."]
            _draw_text_screen(stdscr, "Execute Full Dedupe", lines)
            return

    stdscr.clear()
    stdscr.addnstr(0, 2, "Building plan first...", 80, curses.color_pair(4))
    stdscr.refresh()

    holder: Dict[str, object] = {}
    def build_plan(cancel_event: threading.Event) -> None:
        run_dir, plan = preview_full_plan(cancel_event)
        holder["run_dir"] = run_dir
        holder["plan"] = plan

    output, err = _run_cancellable_action_screen(
        stdscr,
        "Execute Full Dedupe · Build Plan",
        build_plan,
        show_result=False,
    )
    if err:
        lines = _plain_lines(output) + ["", f"Error: {err}", "", "Press q or backspace to return."]
        _draw_text_screen(stdscr, "Execute Full Dedupe", lines)
        return

    run_dir = holder["run_dir"]
    plan = holder["plan"]
    if plan["rows_to_remove"] == 0:
        lines = _plain_lines(output) + ["", "Nothing to remove. Your liked songs are clean by these rules."]
        _draw_text_screen(stdscr, "Execute Full Dedupe", lines)
        return

    lines = _plain_lines(output)
    lines.append("")
    lines.append("Review the summary. Enter continues to confirmation.")
    if not _review_text_screen(stdscr, "Execute Full Dedupe · Plan Preview", lines):
        _draw_text_screen(stdscr, "Execute Full Dedupe", ["Cancelled. No likes changed."])
        return

    if not _confirm_dedupe_screen(stdscr):
        _draw_text_screen(stdscr, "Execute Full Dedupe", ["Cancelled. No likes changed."])
        return

    _run_action_screen(stdscr, "Execute Full Dedupe", lambda: _execute_existing_plan(run_dir, plan))


def _init_curses() -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        curses.start_color()
        if not curses.has_colors():
            return
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK

    for pair_id, fg, pair_bg in [
        (1, curses.COLOR_CYAN, bg),
        (2, curses.COLOR_GREEN, bg),
        (3, curses.COLOR_YELLOW, bg),
        (4, curses.COLOR_BLUE, bg),
        (5, curses.COLOR_BLACK, curses.COLOR_CYAN),
    ]:
        try:
            curses.init_pair(pair_id, fg, pair_bg)
        except curses.error:
            pass


def curses_menu(stdscr: "curses._CursesWindow") -> None:
    _init_curses()
    if not secure_store.has_client_credentials():
        _setup_oauth_screen(stdscr)
    selected = 0
    message = "Enter chooses | arrows/j/k move | q quits"
    actions = [
        ("Smart scan", "Fetch live playlist and refresh duplicate report", _smart_scan_action),
        ("Show duplicates", "Browse the most important duplicate groups", lambda s: _draw_text_screen(s, "Duplicate Groups", _duplicate_preview_lines())),
        ("Preview plan", "Create before backup and cleanup plan, no changes", _preview_plan_action),
        ("Execute dedupe", "Remove duplicates after typed confirmation", _execute_action),
        ("Setup OAuth", "Save or replace Google OAuth client credentials", _setup_oauth_screen),
        ("Quit", "Leave the CLI", None),
    ]

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 2, "YouTube Music Dedup", max(1, w - 4), curses.color_pair(1) | curses.A_BOLD)
        stdscr.addnstr(1, 2, message, max(1, w - 4), curses.color_pair(4))

        stats = _stats_lines()
        y = 3
        stdscr.addnstr(y, 2, "Local report", max(1, w - 4), curses.A_BOLD)
        y += 1
        for line in stats[:6]:
            stdscr.addnstr(y, 4, line, max(1, w - 6))
            y += 1

        y += 1
        stdscr.addnstr(y, 2, "Actions", max(1, w - 4), curses.A_BOLD)
        y += 1
        for i, (label, desc, _) in enumerate(actions):
            attr = curses.color_pair(5) | curses.A_BOLD if i == selected else curses.A_NORMAL
            prefix = "> " if i == selected else "  "
            stdscr.addnstr(y, 2, f"{prefix}{label}", max(1, w - 4), attr)
            y += 1
            if y < h - 1:
                stdscr.addnstr(y, 6, desc, max(1, w - 8), curses.color_pair(4))
            y += 1

        ch = stdscr.getch()
        if ch in (ord("q"), 27):
            return
        if ch in (curses.KEY_DOWN, ord("j")):
            selected = (selected + 1) % len(actions)
        elif ch in (curses.KEY_UP, ord("k")):
            selected = (selected - 1) % len(actions)
        elif ch in (10, 13):
            label, _, fn = actions[selected]
            if fn is None:
                return
            fn(stdscr)


def main(argv: List[str]) -> int:
    if len(argv) == 1:
        menu()
        return 0

    cmd = argv[1]
    try:
        if cmd == "scan":
            scan_live_and_analyze()
        elif cmd == "show":
            show_duplicates()
        elif cmd == "plan":
            preview_full_plan()
        elif cmd == "dedupe":
            execute_full_dedupe()
        elif cmd == "status":
            local_status()
        elif cmd == "setup":
            setup_credentials()
        else:
            print("Usage: playlist_janitor.py [scan|show|plan|dedupe|status|setup]")
            return 2
    except (live.ApiError, OSError) as e:
        print(bad(f"Error: {e}"), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))


def cli() -> int:
    return main(sys.argv)

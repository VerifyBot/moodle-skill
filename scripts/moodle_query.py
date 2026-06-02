#!/usr/bin/env python3
"""Run the user's Moodle CLI from any current working directory.

This wrapper also exposes a few higher-level read-only helpers that are useful
for LLM context gathering and are not part of the base Moodle CLI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import subprocess
import sys
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


PROJECT_DIR = Path(__file__).resolve().parents[1]
CLI = PROJECT_DIR / "scripts" / "moodle_cli.py"
DOWNLOAD_DIR = Path("/tmp/moodle-file-cache")
CUSTOM_COMMANDS = {"assignment-raw", "assignment-files", "file-text"}


class FileReadError(RuntimeError):
    """Raised when a Moodle file cannot be downloaded or extracted."""


def import_moodle_cli():
    sys.path.insert(0, str(CLI.parent))
    import moodle_cli  # type: ignore[import-not-found]

    return moodle_cli


def make_client(args: argparse.Namespace):
    moodle_cli = import_moodle_cli()
    config_path = Path(args.config) if args.config else PROJECT_DIR / "config.ini"
    config = moodle_cli.load_config(
        config_path,
        token_override=args.token,
        base_url_override=args.base_url,
    )
    return moodle_cli.MoodleClient(config.base_url, config.token), moodle_cli


def tokenized_file_url(file_url: str, token: str) -> str:
    """Add the Moodle token to pluginfile URLs without duplicating it."""
    if not file_url or "/pluginfile.php/" not in file_url:
        return file_url

    split = urlsplit(file_url)
    query = parse_qsl(split.query, keep_blank_values=True)
    if not any(key == "token" for key, _ in query):
        query.append(("token", token))
    return urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), split.fragment))


def clean_file_record(file_record: dict[str, Any], source: str) -> dict[str, Any]:
    cleaned = dict(file_record)
    cleaned["source"] = source
    return cleaned


def collect_assignment_files(
    assignment: dict[str, Any] | None,
    submission_status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Collect assignment prompt attachments and the user's submitted files."""
    files: list[dict[str, Any]] = []

    if assignment:
        for key in ("introattachments", "introfiles"):
            for file_record in assignment.get(key, []) or []:
                files.append(clean_file_record(file_record, key))

    status = submission_status or {}
    attachments = ((status.get("assignmentdata") or {}).get("attachments") or {})
    for area, area_files in attachments.items():
        for file_record in area_files or []:
            files.append(clean_file_record(file_record, f"assignmentdata.{area}"))

    submission = ((status.get("lastattempt") or {}).get("submission") or status.get("submission") or {})
    for plugin in submission.get("plugins", []) or []:
        if plugin.get("type") != "file":
            continue
        for file_area in plugin.get("fileareas", []) or []:
            area = file_area.get("area") or "submission_files"
            for file_record in file_area.get("files", []) or []:
                files.append(clean_file_record(file_record, area))

    return dedupe_files(files)


def dedupe_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for file_record in files:
        key = (str(file_record.get("filename", "")), str(file_record.get("fileurl", "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(file_record)
    return deduped


def filter_files(files: list[dict[str, Any]], filename_filter: str | None) -> list[dict[str, Any]]:
    if not filename_filter:
        return files
    needle = filename_filter.casefold()
    return [file_record for file_record in files if needle in str(file_record.get("filename", "")).casefold()]


def is_pdf_file(file_record: dict[str, Any]) -> bool:
    mimetype = str(file_record.get("mimetype") or "").casefold()
    filename = str(file_record.get("filename") or "").casefold()
    return mimetype == "application/pdf" or filename.endswith(".pdf")


def find_assignment(client: Any, assignid: int, courseid: int | None) -> dict[str, Any]:
    params: list[tuple[str, Any]] | None = [("courseids[0]", courseid)] if courseid is not None else None
    payload = client.request("mod_assign_get_assignments", params)
    for course in payload.get("courses", []) or []:
        for assignment in course.get("assignments", []) or []:
            if int(assignment.get("id", -1)) == assignid:
                return assignment
    raise FileReadError(f"Assignment {assignid} was not found")


def get_submission_status(client: Any, assignid: int) -> dict[str, Any] | None:
    try:
        return client.request("mod_assign_get_submission_status", {"assignid": assignid})
    except Exception:
        return None


def filename_from_url(file_url: str) -> str:
    path = urlsplit(file_url).path
    name = unquote(Path(path).name)
    return name or "moodle-file"


def safe_download_path(filename: str, file_url: str) -> Path:
    safe_name = "".join(char if char.isalnum() or char in ".-_" else "_" for char in filename)
    suffix = Path(safe_name).suffix or mimetypes.guess_extension("application/octet-stream") or ".bin"
    digest = hashlib.sha256(file_url.encode("utf-8")).hexdigest()[:12]
    return DOWNLOAD_DIR / f"{Path(safe_name).stem}-{digest}{suffix}"


def download_file(client: Any, file_record: dict[str, Any]) -> Path:
    file_url = str(file_record.get("fileurl") or "")
    if not file_url:
        raise FileReadError(f"{file_record.get('filename', 'file')} has no file URL")

    filename = str(file_record.get("filename") or filename_from_url(file_url))
    token_url = tokenized_file_url(file_url, client.token)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = safe_download_path(filename, token_url)

    try:
        response = client.session.get(token_url, timeout=30)
        response.raise_for_status()
    except Exception as exc:
        raise FileReadError(f"Failed to download {filename}: {exc}") from exc
    destination.write_bytes(response.content)
    return destination


def extract_pdf_text(path: Path) -> str:
    completed = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise FileReadError(completed.stderr.strip() or f"pdftotext failed for {path}")
    return completed.stdout.strip()


def extract_file_text(path: Path, file_record: dict[str, Any]) -> str:
    if is_pdf_file(file_record):
        return extract_pdf_text(path)
    filename = file_record.get("filename") or path.name
    mimetype = file_record.get("mimetype") or "unknown type"
    raise FileReadError(f"Unsupported file type for {filename}: {mimetype}")


def build_custom_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read Moodle data and attached files.")
    parser.add_argument("--config", help="Path to Moodle config.ini.")
    parser.add_argument("--base-url", help="Override Moodle REST endpoint or base Moodle URL.")
    parser.add_argument("--token", help="Override Moodle web service token.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    raw = subparsers.add_parser("assignment-raw", help="Print raw JSON for one Moodle assignment.")
    raw.add_argument("--assignid", type=int, required=True)
    raw.add_argument("--courseid", type=int)

    files = subparsers.add_parser("assignment-files", help="List prompt and submission files for one assignment.")
    files.add_argument("--assignid", type=int, required=True)
    files.add_argument("--courseid", type=int)
    files.add_argument("--filename", help="Case-insensitive filename substring filter.")

    text = subparsers.add_parser("file-text", help="Download Moodle files and extract readable text.")
    text.add_argument("--assignid", type=int, help="Read files attached to or submitted for an assignment.")
    text.add_argument("--courseid", type=int, help="Optional course id to speed up assignment lookup.")
    text.add_argument("--url", help="Read a direct Moodle file URL.")
    text.add_argument("--path", type=Path, help="Read a local file path.")
    text.add_argument("--filename", help="Case-insensitive filename substring filter.")

    return parser


def cmd_assignment_raw(args: argparse.Namespace) -> int:
    client, _ = make_client(args)
    assignment = find_assignment(client, args.assignid, args.courseid)
    print(json.dumps(assignment, ensure_ascii=False, indent=2))
    return 0


def cmd_assignment_files(args: argparse.Namespace) -> int:
    client, _ = make_client(args)
    assignment = find_assignment(client, args.assignid, args.courseid)
    submission_status = get_submission_status(client, args.assignid)
    files = filter_files(collect_assignment_files(assignment, submission_status), args.filename)

    print("| Source | Filename | MIME Type | Size |")
    print("|---|---|---|---:|")
    for file_record in files:
        print(
            f"| {file_record.get('source', '')} | "
            f"{file_record.get('filename', '')} | "
            f"{file_record.get('mimetype', '')} | "
            f"{file_record.get('filesize', '')} |"
        )
    return 0


def file_records_from_args(args: argparse.Namespace, client: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if args.path:
        mimetype, _ = mimetypes.guess_type(args.path.name)
        records.append({"filename": args.path.name, "path": args.path, "mimetype": mimetype or ""})
    if args.url:
        mimetype, _ = mimetypes.guess_type(filename_from_url(args.url))
        records.append({"filename": filename_from_url(args.url), "fileurl": args.url, "mimetype": mimetype or ""})
    if args.assignid:
        assignment = find_assignment(client, args.assignid, args.courseid)
        submission_status = get_submission_status(client, args.assignid)
        records.extend(collect_assignment_files(assignment, submission_status))
    return filter_files(records, args.filename)


def cmd_file_text(args: argparse.Namespace) -> int:
    if not (args.path or args.url or args.assignid):
        raise FileReadError("Provide --path, --url, or --assignid")

    client, _ = make_client(args)
    files = file_records_from_args(args, client)
    if not files:
        raise FileReadError("No matching files found")

    readable_files = [file_record for file_record in files if is_pdf_file(file_record)]
    if not readable_files:
        names = ", ".join(str(file_record.get("filename", "file")) for file_record in files)
        raise FileReadError(f"No supported readable files found. PDF is currently supported. Matched: {names}")

    for index, file_record in enumerate(readable_files):
        if index:
            print("\n\n---\n")
        filename = file_record.get("filename", "file")
        print(f"# {filename}\n")
        path = Path(file_record["path"]) if file_record.get("path") else download_file(client, file_record)
        print(extract_file_text(path, file_record))
    return 0


def run_custom_command(argv: list[str]) -> int:
    parser = build_custom_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "assignment-raw":
            return cmd_assignment_raw(args)
        if args.command == "assignment-files":
            return cmd_assignment_files(args)
        if args.command == "file-text":
            return cmd_file_text(args)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in CUSTOM_COMMANDS:
        return run_custom_command(sys.argv[1:])

    env = os.environ.copy()
    env.setdefault("UV_CACHE_DIR", "/tmp/uv-cache")
    command = ["uv", "run", "python", str(CLI), *sys.argv[1:]]
    completed = subprocess.run(command, cwd=PROJECT_DIR, env=env)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

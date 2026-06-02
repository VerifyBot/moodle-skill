#!/usr/bin/env python3
"""Read-only Moodle REST CLI for LLM-friendly context extraction."""

from __future__ import annotations

import argparse
import base64
import binascii
import configparser
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


DEFAULT_TOKEN = None
DEFAULT_DOMAIN = "moodle.technion.ac.il"
REST_PATH = "/webservice/rest/server.php"


class MoodleAPIError(RuntimeError):
    """Raised when Moodle returns an HTTP, JSON, or embedded API error."""


@dataclass(frozen=True)
class MoodleConfig:
    base_url: str
    token: str


def load_config(config_path: Path, *, token_override: str | None = None, base_url_override: str | None = None) -> MoodleConfig:
    """Load Moodle config, allowing CLI and environment overrides."""
    parser = configparser.ConfigParser()
    if config_path.exists():
        parser.read(config_path, encoding="utf-8")

    section = parser["moodle"] if parser.has_section("moodle") else {}
    token = token_override or os.environ.get("MOODLE_WSTOKEN") or resolve_config_token(section) or DEFAULT_TOKEN
    domain = section.get("domain", DEFAULT_DOMAIN)
    base_url = base_url_override or build_base_url(domain)

    return MoodleConfig(base_url=base_url, token=token)


def resolve_config_token(section: configparser.SectionProxy | dict[str, str]) -> str | None:
    """Resolve token from config.ini.

    The local Technion config has a base64-encoded triple token where the
    middle segment is the actual Moodle wstoken. Prefer that when present.
    """
    raw_token = section.get("token")
    decoded_token = extract_middle_triple_token(raw_token)
    return decoded_token or section.get("web_service_token") or raw_token


def extract_middle_triple_token(raw_token: str | None) -> str | None:
    if not raw_token:
        return None
    candidates = [raw_token]
    try:
        candidates.append(base64.b64decode(raw_token).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError):
        pass
    for candidate in candidates:
        parts = candidate.split(":::")
        if len(parts) == 3 and parts[1]:
            return parts[1]
    return None


def build_base_url(domain_or_url: str) -> str:
    """Build the Moodle REST endpoint from a domain or an explicit URL."""
    value = domain_or_url.strip().rstrip("/")
    if value.endswith(REST_PATH):
        return value
    if value.startswith("http://") or value.startswith("https://"):
        return f"{value}{REST_PATH}"
    return f"https://{value}{REST_PATH}"


class MoodleClient:
    """Small wrapper around Moodle's REST webservice endpoint."""

    def __init__(self, base_url: str, token: str, session: requests.Session | None = None) -> None:
        self.base_url = base_url
        self.token = token
        self.session = session or requests.Session()

    def request(self, wsfunction: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None) -> Any:
        """Call a Moodle REST function.

        Every Moodle Web Services request must include:
        - wstoken: service token
        - moodlewsrestformat=json: JSON response format
        - wsfunction: Moodle function name
        """
        data: list[tuple[str, Any]] = [
            ("wstoken", self.token),
            ("moodlewsrestformat", "json"),
            ("wsfunction", wsfunction),
        ]
        if isinstance(params, dict):
            data.extend(params.items())
        elif params:
            data.extend(params)

        try:
            response = self.session.post(self.base_url, data=data, timeout=10)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise MoodleAPIError(f"Network or HTTP error while calling {wsfunction}: {exc}") from exc
        except ValueError as exc:
            raise MoodleAPIError(f"Moodle returned non-JSON response for {wsfunction}") from exc

        if isinstance(payload, dict) and ("exception" in payload or "errorcode" in payload):
            errorcode = payload.get("errorcode", "unknown")
            message = clean_text(str(payload.get("message", payload.get("exception", ""))))
            raise MoodleAPIError(f"Moodle API error in {wsfunction}: {errorcode}: {message}")

        return payload


def clean_text(value: Any) -> str:
    """Remove basic HTML markup and collapse whitespace for compact LLM context."""
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", " ", text)
    text = re.sub(r"(?i)</\s*(p|div|li|h[1-6]|tr)\s*>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def markdown_cell(value: Any) -> str:
    return clean_text(value).replace("|", "\\|").replace("\n", " ")


def timestamp_to_datetime(timestamp: Any) -> str:
    try:
        seconds = int(timestamp)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def append_token_to_file_url(file_url: str, token: str) -> str:
    if not file_url:
        return ""
    if "/pluginfile.php/" not in file_url:
        return file_url
    separator = "&" if "?" in file_url else "?"
    return f"{file_url}{separator}token={token}"


def get_site_info(client: MoodleClient) -> dict[str, Any]:
    return client.request("core_webservice_get_site_info")


def resolve_userid(client: MoodleClient, userid: int | None) -> int:
    if userid is not None:
        return userid
    site_info = get_site_info(client)
    return int(site_info["userid"])


def filter_hidden_courses(courses: list[dict[str, Any]], *, include_hidden: bool = False) -> list[dict[str, Any]]:
    if include_hidden:
        return courses
    return [course for course in courses if not course.get("hidden")]


def get_user_courses(client: MoodleClient, userid: int | None, *, include_hidden: bool = False) -> list[dict[str, Any]]:
    resolved_userid = resolve_userid(client, userid)
    courses = client.request("core_enrol_get_users_courses", {"userid": resolved_userid})
    return filter_hidden_courses(courses, include_hidden=include_hidden)


def courseids_params(course_ids: list[int]) -> list[tuple[str, int]]:
    return [(f"courseids[{index}]", course_id) for index, course_id in enumerate(course_ids)]


def format_init(site_info: dict[str, Any]) -> str:
    return json.dumps(
        {
            "userid": site_info.get("userid"),
            "fullname": site_info.get("fullname"),
            "username": site_info.get("username"),
            "siteurl": site_info.get("siteurl"),
        },
        ensure_ascii=False,
        indent=2,
    )


def format_courses(courses: list[dict[str, Any]]) -> str:
    lines = [
        "| Course ID | Short Name | Full Name |",
        "|---:|---|---|",
    ]
    for course in courses:
        lines.append(
            f"| {markdown_cell(course.get('id'))} | "
            f"{markdown_cell(course.get('shortname'))} | "
            f"{markdown_cell(course.get('fullname'))} |"
        )
    return "\n".join(lines)


def format_course_content(sections: list[dict[str, Any]], token: str) -> str:
    lines: list[str] = []
    for section in sections:
        section_name = clean_text(section.get("name")) or f"Section {section.get('section', '')}".strip()
        lines.append(f"## {section_name}")
        summary = clean_text(section.get("summary"))
        if summary:
            lines.append(summary)
        for module in section.get("modules", []):
            module_name = clean_text(module.get("name")) or "Untitled module"
            modname = clean_text(module.get("modname"))
            url = module.get("url")
            suffix = f" ({modname})" if modname else ""
            if url:
                lines.append(f"- [{module_name}]({url}){suffix}")
            else:
                lines.append(f"- {module_name}{suffix}")
            description = clean_text(module.get("description"))
            if description:
                lines.append(f"  - Description: {description}")
            for content in module.get("contents", []):
                filename = clean_text(content.get("filename")) or clean_text(content.get("filepath")) or "file"
                fileurl = content.get("fileurl")
                if fileurl:
                    lines.append(f"  - File: [{filename}]({append_token_to_file_url(fileurl, token)})")
        lines.append("")
    return "\n".join(lines).strip()


def format_assignments(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for course in payload.get("courses", []):
        lines.append(f"## {clean_text(course.get('fullname') or course.get('shortname') or course.get('id'))}")
        for assignment in course.get("assignments", []):
            name = clean_text(assignment.get("name")) or f"Assignment {assignment.get('id')}"
            due = timestamp_to_datetime(assignment.get("duedate"))
            allowsubmissionsfrom = timestamp_to_datetime(assignment.get("allowsubmissionsfromdate"))
            cutoff = timestamp_to_datetime(assignment.get("cutoffdate"))
            lines.append(f"- **{name}**")
            lines.append(f"  - Assignment ID: {assignment.get('id')}")
            if due:
                lines.append(f"  - Due: {due}")
            if allowsubmissionsfrom:
                lines.append(f"  - Opens: {allowsubmissionsfrom}")
            if cutoff:
                lines.append(f"  - Cutoff: {cutoff}")
            intro = clean_text(assignment.get("intro"))
            if intro:
                lines.append(f"  - Description: {intro}")
        lines.append("")
    return "\n".join(lines).strip()


def extract_submitted(payload: dict[str, Any]) -> bool:
    last_attempt = payload.get("lastattempt") or {}
    submission = last_attempt.get("submission") or payload.get("submission") or {}
    status = str(submission.get("status", "")).lower()
    return bool(submission) and status not in {"", "new", "draft"}


def format_submission_status(payload: dict[str, Any]) -> str:
    last_attempt = payload.get("lastattempt") or {}
    submission = last_attempt.get("submission") or payload.get("submission") or {}
    due_ts = payload.get("duedate") or last_attempt.get("duedate")
    due = timestamp_to_datetime(due_ts)
    grading_status = (
        (payload.get("feedback") or {}).get("gradefordisplay")
        or last_attempt.get("gradingstatus")
        or payload.get("gradingstatus")
        or "unknown"
    )
    submitted = extract_submitted(payload)

    lines = ["## Submission Status"]
    if payload.get("assignmentname"):
        lines.append(f"- Assignment: {clean_text(payload.get('assignmentname'))}")
    lines.extend(
        [
            f"- Submitted: {submitted}",
            f"- Submission Status: {clean_text(submission.get('status', 'unknown')) or 'unknown'}",
            f"- Grading Status: {clean_text(grading_status)}",
        ]
    )
    if due:
        lines.append(f"- Due Date: {due}")
        try:
            remaining = int(due_ts) - int(time.time())
            if remaining >= 0:
                lines.append(f"- Time Remaining: {format_duration(remaining)}")
            else:
                lines.append(f"- Time Remaining: overdue by {format_duration(abs(remaining))}")
        except (TypeError, ValueError):
            pass
    return "\n".join(lines)


def find_assignment_details(assignments_payload: dict[str, Any], assignid: int) -> dict[str, Any] | None:
    for course in assignments_payload.get("courses", []):
        for assignment in course.get("assignments", []):
            if int(assignment.get("id", -1)) == assignid:
                return assignment
    return None


def enrich_submission_status(payload: dict[str, Any], assignment: dict[str, Any] | None) -> dict[str, Any]:
    if not assignment:
        return payload
    enriched = dict(payload)
    if not enriched.get("duedate"):
        enriched["duedate"] = assignment.get("duedate")
    if assignment.get("name"):
        enriched["assignmentname"] = assignment.get("name")
    return enriched


def format_duration(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def format_forum_log(forum: dict[str, Any], discussions: list[dict[str, Any]], posts_by_discussion: dict[int, list[dict[str, Any]]]) -> str:
    lines = [f"# Forum: {clean_text(forum.get('name') or forum.get('id'))}"]
    for discussion in discussions:
        discussion_id = int(discussion.get("discussion") or discussion.get("id"))
        title = clean_text(discussion.get("name") or discussion.get("subject") or discussion_id)
        user = clean_text(discussion.get("userfullname") or discussion.get("username") or "Unknown")
        lines.append("")
        lines.append(f"## Thread: {title}")
        if discussion.get("message"):
            lines.append(f"{user}: {clean_text(discussion.get('message'))}")
        for post in posts_by_discussion.get(discussion_id, []):
            post_user = clean_text(post.get("author", {}).get("fullname") if isinstance(post.get("author"), dict) else post.get("userfullname"))
            post_user = post_user or "Unknown"
            subject = clean_text(post.get("subject"))
            message = clean_text(post.get("message"))
            if subject and subject != title:
                lines.append(f"{post_user}: [{subject}] {message}")
            elif message:
                lines.append(f"{post_user}: {message}")
    return "\n".join(lines).strip()


def cmd_init(args: argparse.Namespace, client: MoodleClient) -> str:
    return format_init(get_site_info(client))


def cmd_courses(args: argparse.Namespace, client: MoodleClient) -> str:
    # core_enrol_get_users_courses requires userid.
    courses = get_user_courses(client, args.userid, include_hidden=args.include_hidden)
    return format_courses(courses)


def cmd_course_content(args: argparse.Namespace, client: MoodleClient) -> str:
    # core_course_get_contents requires courseid.
    sections = client.request("core_course_get_contents", {"courseid": args.courseid})
    return format_course_content(sections, client.token)


def cmd_assignments(args: argparse.Namespace, client: MoodleClient) -> str:
    # mod_assign_get_assignments accepts optional courseids[] array.
    params: list[tuple[str, Any]] | None = None
    if args.courseid is not None:
        params = [("courseids[0]", args.courseid)]
    elif not args.include_hidden:
        visible_courses = get_user_courses(client, args.userid, include_hidden=False)
        params = courseids_params([int(course["id"]) for course in visible_courses])
    payload = client.request("mod_assign_get_assignments", params)
    return format_assignments(payload)


def cmd_submission_status(args: argparse.Namespace, client: MoodleClient) -> str:
    # mod_assign_get_submission_status requires assignid.
    payload = client.request("mod_assign_get_submission_status", {"assignid": args.assignid})
    assignments_payload = client.request("mod_assign_get_assignments")
    payload = enrich_submission_status(payload, find_assignment_details(assignments_payload, args.assignid))
    return format_submission_status(payload)


def cmd_forum(args: argparse.Namespace, client: MoodleClient) -> str:
    # Chain: course forums -> forum discussions -> discussion posts.
    forums = client.request("mod_forum_get_forums_by_courses", [("courseids[0]", args.courseid)])
    if args.forumid is not None:
        forum = next((item for item in forums if int(item.get("id", -1)) == args.forumid), None)
        if forum is None:
            raise MoodleAPIError(f"Forum {args.forumid} was not found in course {args.courseid}")
    elif forums:
        forum = forums[0]
    else:
        raise MoodleAPIError(f"No forums found for course {args.courseid}")

    discussions_payload = client.request("mod_forum_get_forum_discussions", {"forumid": forum["id"]})
    discussions = discussions_payload.get("discussions", discussions_payload if isinstance(discussions_payload, list) else [])
    discussions = discussions[: args.limit]
    posts_by_discussion: dict[int, list[dict[str, Any]]] = {}
    for discussion in discussions:
        discussion_id = int(discussion.get("discussion") or discussion.get("id"))
        posts_payload = client.request("mod_forum_get_discussion_posts", {"discussionid": discussion_id})
        posts = posts_payload.get("posts", posts_payload if isinstance(posts_payload, list) else [])
        posts_by_discussion[discussion_id] = posts
    return format_forum_log(forum, discussions, posts_by_discussion)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Moodle REST CLI for LLM context generation.")
    parser.add_argument("--config", type=Path, default=Path("config.ini"), help="Path to config.ini.")
    parser.add_argument("--base-url", help="Override full Moodle REST endpoint or base Moodle URL.")
    parser.add_argument("--token", help="Override Moodle web service token. MOODLE_WSTOKEN also works.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Fetch current Moodle user/site info.")
    init_parser.set_defaults(func=cmd_init)

    courses_parser = subparsers.add_parser("courses", help="List enrolled courses.")
    courses_parser.add_argument("--userid", type=int, help="Moodle user id. Auto-discovered when omitted.")
    courses_parser.add_argument("--include-hidden", action="store_true", help="Include courses hidden on Moodle's course page.")
    courses_parser.set_defaults(func=cmd_courses)

    content_parser = subparsers.add_parser("course-content", help="Show course section/module/file map.")
    content_parser.add_argument("--courseid", type=int, required=True)
    content_parser.set_defaults(func=cmd_course_content)

    assignments_parser = subparsers.add_parser("assignments", help="Show assignments and deadlines.")
    assignments_parser.add_argument("--courseid", type=int, help="Limit to one course id.")
    assignments_parser.add_argument("--userid", type=int, help="Moodle user id for visible-course filtering. Auto-discovered when omitted.")
    assignments_parser.add_argument("--include-hidden", action="store_true", help="Include assignments from hidden courses.")
    assignments_parser.set_defaults(func=cmd_assignments)

    status_parser = subparsers.add_parser("submission-status", help="Show one assignment submission status.")
    status_parser.add_argument("--assignid", type=int, required=True)
    status_parser.set_defaults(func=cmd_submission_status)

    forum_parser = subparsers.add_parser("forum", help="Show forum threads and replies.")
    forum_parser.add_argument("--courseid", type=int, required=True)
    forum_parser.add_argument("--forumid", type=int, help="Forum id. Defaults to the first forum in the course.")
    forum_parser.add_argument("--limit", type=int, default=5, help="Maximum discussions to fetch.")
    forum_parser.set_defaults(func=cmd_forum)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config, token_override=args.token, base_url_override=args.base_url)
    client = MoodleClient(config.base_url, config.token)
    try:
        output = args.func(args, client)
    except MoodleAPIError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

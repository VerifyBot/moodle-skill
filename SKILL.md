---
name: moodle-context
description: Query Moodle via a local read-only CLI for courses, deadlines, assignments, submissions, grades, forums, and attached files.
---

# Moodle Context

Use `scripts/moodle_query.py` for live Moodle data. Do not guess course IDs,
assignment IDs, due dates, grades, submission status, forum posts, or file
contents.

Before answering, read `memory.md` if it exists.

## Defaults

- Ignore hidden Moodle courses unless the user explicitly asks for them.
- Treat visible courses as the current/relevant course set.
- Prefer live Moodle data over memory for deadlines, submissions, grades, forums, and files.
- Use `courses` to find course IDs and `assignments` to find assignment IDs.

## Commands

```bash
python3 scripts/moodle_query.py courses
python3 scripts/moodle_query.py assignments
```

Use course IDs from `courses` with `course-content`, `forum`, and
course-scoped `assignments`. Use assignment IDs from `assignments` with
`submission-status`, `assignment-raw`, `assignment-files`, and `file-text`.
Use `file-text` with a direct Moodle file URL or local PDF path when that is
more convenient.

## Notes

- `file-text` currently supports PDFs through `pdftotext`.
- Add local course aliases in `references/course-aliases.md` only when the user asks.
- Treat Moodle API errors as authoritative; do not invent fallback answers.

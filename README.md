# Moodle Context Skill

Small read-only Moodle helper for course lists, deadlines, submissions, forums, and assignment files.

It is built around Moodle's web-service API. Nothing here submits work or changes Moodle state.

## What actually matters

If you only want to give this to another agentic coding assistant, these are the important files:

- `scripts/moodle_cli.py` is the real Moodle API client and base command-line tool.
- `scripts/moodle_query.py` is the convenient wrapper. This is where the extra file commands live: `assignment-raw`, `assignment-files`, and `file-text`.
- `config.ini.example` shows the config shape. The real local file should be named `config.ini` and contain your token.
- `SKILL.md` tells an agent when to use the tool and which commands to run.

The rest is useful but not essential:

- `README.md` is setup help for a human.
- `agents/openai.yaml` is Codex skill metadata.
- `references/course-aliases.md` and `memory.md` are local context/preferences.
- `tests/` is for checking that the code still behaves.

So the smallest useful bundle is:

```text
scripts/moodle_cli.py
scripts/moodle_query.py
config.ini.example
SKILL.md
```

For an agent that does not understand Codex skills, paste or attach `SKILL.md` plus the two files in `scripts/`, then tell it to run commands through `python3 scripts/moodle_query.py ...`.

## Setup

Install the Python dependencies:

```bash
uv sync
```

PDF text extraction uses `pdftotext`. On Ubuntu/Debian:

```bash
sudo apt install poppler-utils
```

Create your local config:

```bash
cp config.ini.example config.ini
```

Now get a Moodle token:

1. Open DevTools in your browser.
2. Go to:

   `https://moodle25.technion.ac.il/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=12345&urlscheme=moodlemobile`

3. Moodle will redirect. In the redirected page URL, copy the token from the query string.
4. Paste it into `config.ini` as `token=...`.

`config.ini` is ignored by git. Do not commit it.

## Quick checks

```bash
python3 scripts/moodle_query.py init
python3 scripts/moodle_query.py courses
python3 scripts/moodle_query.py assignments
```

If you prefer `uv` explicitly:

```bash
uv run python scripts/moodle_query.py courses
```

## Useful commands

List course content:

```bash
python3 scripts/moodle_query.py course-content --courseid 12345
```

List assignments for one course:

```bash
python3 scripts/moodle_query.py assignments --courseid 12345
```

Check one assignment submission:

```bash
python3 scripts/moodle_query.py submission-status --assignid 67890
```

List the prompt files and your submitted files for an assignment:

```bash
python3 scripts/moodle_query.py assignment-files --assignid 67890 --courseid 12345
```

Print raw assignment metadata:

```bash
python3 scripts/moodle_query.py assignment-raw --assignid 67890 --courseid 12345
```

Extract text from assignment PDFs:

```bash
python3 scripts/moodle_query.py file-text --assignid 67890 --courseid 12345 --filename 'Project'
```

Extract text from a Moodle file URL or a local PDF:

```bash
python3 scripts/moodle_query.py file-text --url 'https://moodle25.technion.ac.il/.../file.pdf'
python3 scripts/moodle_query.py file-text --path /tmp/file.pdf
```

## Tests

```bash
uv run python -m unittest discover -s tests
```

The tests do not need a real Moodle token. Live commands do.

## Notes

- `file-text` currently supports PDFs.
- Add more formats by extending `extract_file_text` in `scripts/moodle_query.py`.
- The Moodle API can be slow or temporarily unreachable; the commands print the Moodle/API error directly when that happens.

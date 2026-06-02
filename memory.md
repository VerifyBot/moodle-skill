# Moodle Context Memory

Mutable local preferences for the `moodle-context` skill.

## Active Preferences

- Ignore Moodle courses where `hidden: true` unless the user explicitly asks to include hidden courses.
- Treat Moodle's visible courses as the current/relevant course set for tasks, deadlines, grades, submissions, forums, and course context.

## How To Update

- When the user asks to remember a Moodle-specific preference, add or edit a bullet under `Active Preferences`.
- When the user asks to forget or undo a Moodle-specific preference, remove or revise the relevant bullet.
- Keep this file concise and avoid duplicate or contradictory rules.

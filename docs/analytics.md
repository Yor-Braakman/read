# Analytics & Logging

All speech attempts and session starts are appended to a JSON Lines file inside the app’s user data directory:

```
${USER_DATA_DIR}/logs/events.jsonl
```

Each line is a UTF-8 JSON object containing:

- `timestamp` (UTC ISO-8601)
- `type` (e.g., `session_start`, `word_attempt`)
- Event-specific payload fields (word text, elapsed seconds, recognition score, mastery, etc.)

Example `word_attempt` entry:

```json
{
  "timestamp": "2026-02-04T18:32:19Z",
  "type": "word_attempt",
  "mode": "common",
  "language": "nl",
  "word": "vriend",
  "elapsed_seconds": 1.42,
  "is_correct": true,
  "score": 0.88,
  "transcript": "vriend",
  "mastery": 0.65,
  "interval": 9.0
}
```

Use this file to:

- Track pronunciation latency improvements
- Identify difficult vocabulary requiring review
- Feed dashboards or evaluation pipelines

The logger is thread-safe, so you can ship the same code to desktop and mobile. Rotate or truncate the JSONL file as needed for long-term storage.

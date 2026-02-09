# Custom Vocabulary Packs

You can add new language word lists without editing `words_data.py`. The app looks for JSON files inside two folders:

- Bundled vocabulary: `data/vocabulary` (next to the application code)
- User vocabulary: `${user_data_dir}/vocabulary` (created on first launch)

Each JSON file must have the following structure:

```json
{
  "code": "fr",
  "label": "Français",
  "words": ["le", "de", "et", "un", "être", "à", "il", "que", "qui", "ne"]
}
```

| Field   | Description                                                   |
|---------|---------------------------------------------------------------|
| `code`  | Unique language identifier used for selection (e.g., `fr`).   |
| `label` | Friendly name shown in the UI (`Français`, `Español`, etc.).  |
| `words` | An ordered array of vocabulary terms (max 1000 recommended).  |

Guidelines:

- Keep the list under 1,000 items to maintain fast review cycles.
- Words are deduplicated automatically while preserving order.
- Updating a JSON file and relaunching the app refreshes the language picker.
- To remove a language, delete its JSON file and restart the app.

Example layout inside `data/vocabulary`:

```
data/
  vocabulary/
    fr_core.json
    es_core.json
```

After adding files, start the app and choose the new language from the Mode screen spinner. Progress for each language is tracked independently because words are keyed by their text.

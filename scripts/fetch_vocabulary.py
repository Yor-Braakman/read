"""
Script to fetch 1000 common words from GitHub repository and update words_data.py

IMPORTANT: Only languages with available Vosk speech recognition models are included!
This ensures users can practice both reading AND pronunciation.

Vocabulary Source: https://github.com/bukowa/1000-common-words
Vosk Models: https://alphacephei.com/vosk/models
"""

import requests
from pathlib import Path
from typing import Dict, List

# Mapping of language codes to GitHub filenames and display labels
# CONSTRAINT: Only languages with corresponding Vosk small models are included
# Each language here has a vosk-model-small-XX available for speech recognition
LANGUAGE_MAPPING = {
    "en": ("English", "🇬🇧 English"),           # vosk-model-small-en-us-0.15
    "nl": ("Dutch", "🇳🇱 Nederlands"),          # vosk-model-small-nl-0.22
    "fr": ("French", "🇫🇷 Français"),           # vosk-model-small-fr-0.22
    "de": ("German", "🇩🇪 Deutsch"),            # vosk-model-small-de-0.15
    "es": ("Spanish", "🇪🇸 Español"),           # vosk-model-small-es-0.42
    "pt": ("Portuguese", "🇵🇹 Português"),      # vosk-model-small-pt-0.3
    "it": ("Italian", "🇮🇹 Italiano"),          # vosk-model-small-it-0.22
    "ca": ("Catalan", "🏴 Català"),             # vosk-model-small-ca-0.4
    "cs": ("Czech", "🇨🇿 Čeština"),             # vosk-model-small-cs-0.4-rhasspy
    "pl": ("Polish", "🇵🇱 Polski"),             # vosk-model-small-pl-0.22
    "ru": ("Russian", "🇷🇺 Русский"),           # vosk-model-small-ru-0.22
    "uk": ("Ukrainian", "🇺🇦 Українська"),      # vosk-model-small-uk-v3-nano
    "zh": ("Chinese", "🇨🇳 中文"),               # vosk-model-small-cn-0.22
    "ja": ("Japanese", "🇯🇵 日本語"),           # vosk-model-small-ja-0.22
    "ko": ("Korean", "🇰🇷 한국어"),              # vosk-model-small-ko-0.22
    "vi": ("Vietnamese", "🇻🇳 Tiếng Việt"),     # vosk-model-small-vn-0.4
    "hi": ("Hindi", "🇮🇳 हिन्दी"),              # vosk-model-small-hi-0.22
    "tr": ("Turkish", "🇹🇷 Türkçe"),            # vosk-model-small-tr-0.3
    "fa": ("Persian", "🇮🇷 فارسی"),             # vosk-model-small-fa-0.42
    "kk": ("Kazakh", "🇰🇿 Қазақша"),            # vosk-model-small-kz-0.42
    "uz": ("Uzbek", "🇺🇿 Oʻzbekcha"),          # vosk-model-small-uz-0.22
    "ky": ("Kyrgyz", "🇰🇬 Кыргызча"),           # vosk-model-small-ky-0.42
    "tg": ("Tajik", "🇹🇯 Тоҷикӣ"),              # vosk-model-small-tg-0.22
    "gu": ("Gujarati", "🇮🇳 ગુજરાતી"),          # vosk-model-small-gu-0.42
    "te": ("Telugu", "🇮🇳 తెలుగు"),             # vosk-model-small-te-0.42
    "eo": ("Esperanto", "🌍 Esperanto"),        # vosk-model-small-eo-0.42
}

BASE_URL = "https://raw.githubusercontent.com/bukowa/1000-common-words/master/{}-1000-common.txt"


def fetch_words(language_filename: str) -> List[str]:
    """Fetch words from GitHub for a specific language."""
    url = BASE_URL.format(language_filename)
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        # Split by newlines and clean up
        words = [word.strip().lower() for word in response.text.split('\n') if word.strip()]
        return words[:1000]  # Ensure we have exactly 1000 or fewer
    except Exception as e:
        print(f"Error fetching {language_filename}: {e}")
        return []


def generate_words_data_file(output_path: Path) -> None:
    """Generate the complete words_data.py file."""
    
    all_words: Dict[str, List[str]] = {}
    all_labels: Dict[str, str] = {}
    
    print("Fetching vocabulary from GitHub...")
    
    for code, (filename, label) in LANGUAGE_MAPPING.items():
        print(f"Fetching {label}...")
        words = fetch_words(filename)
        if words:
            all_words[code] = words
            all_labels[code] = label
            print(f"  ✓ Got {len(words)} words for {label}")
        else:
            print(f"  ✗ Failed to fetch {label}")
    
    print(f"\nGenerating words_data.py with {len(all_words)} languages...")
    
    # Generate the Python file content
    content = []
    
    # Add each language's word list
    for code in sorted(all_words.keys()):
        words = all_words[code]
        label = all_labels[code]
        
        content.append(f"# {label}")
        content.append(f"COMMON_WORDS_{code.upper()} = [")
        for word in words:
            # Escape single quotes in words
            escaped_word = word.replace("'", "\\'")
            content.append(f"    '{escaped_word}',")
        content.append("]\n")
    
    # Add the LANGUAGE_LABELS dictionary
    content.append("LANGUAGE_LABELS = {")
    for code in sorted(all_labels.keys()):
        content.append(f'    "{code}": "{all_labels[code]}",')
    content.append("}\n")
    
    # Add the COMMON_WORDS dictionary
    content.append("COMMON_WORDS = {")
    for code in sorted(all_words.keys()):
        content.append(f'    "{code}": COMMON_WORDS_{code.upper()},')
    content.append("}\n")
    
    # Write to file
    output_path.write_text('\n'.join(content), encoding='utf-8')
    print(f"✓ Successfully generated {output_path}")
    print(f"  Total languages: {len(all_words)}")
    print(f"  Total words: {sum(len(words) for words in all_words.values())}")


if __name__ == "__main__":
    # Get the project root directory
    script_dir = Path(__file__).parent
    project_root = script_dir.parent
    output_file = project_root / "words_data.py"
    
    print("=" * 60)
    print("Fetching 1000 Common Words for Literacy Coach")
    print("Source: https://github.com/bukowa/1000-common-words")
    print("=" * 60)
    print()
    
    generate_words_data_file(output_file)
    
    print()
    print("=" * 60)
    print("Done! You can now use these languages in your app.")
    print("=" * 60)

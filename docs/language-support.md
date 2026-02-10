# Language Support

## Supported Languages

This app supports **26 languages** for literacy practice. Each language includes:
- ✅ 1,000 most common words for reading practice
- ✅ Vosk speech recognition model for pronunciation practice
- ✅ Unicode flag emoji in the UI

## Language List

### 🌍 Why These Languages?

**Critical Constraint**: We only support languages that have **both**:
1. Word lists available from [1000 Common Words](https://github.com/bukowa/1000-common-words)
2. Small Vosk speech recognition models from [Vosk Models](https://alphacephei.com/vosk/models)

This ensures users can practice both **reading** and **pronunciation** in every supported language.

### Supported Language Table

| Code | Language | Flag | Vosk Model | Words |
|------|----------|------|------------|-------|
| `ca` | Català (Catalan) | 🏴 | vosk-model-small-ca-0.4 | 1,000 |
| `cs` | Čeština (Czech) | 🇨🇿 | vosk-model-small-cs-0.4 | 999 |
| `de` | Deutsch (German) | 🇩🇪 | vosk-model-small-de-0.15 | 1,000 |
| `en` | English | 🇬🇧 | vosk-model-small-en-us-0.15 | 1,000 |
| `eo` | Esperanto | 🌍 | vosk-model-small-eo-0.42 | 1,000 |
| `es` | Español (Spanish) | 🇪🇸 | vosk-model-small-es-0.42 | 1,000 |
| `fa` | فارسی (Persian/Farsi) | 🇮🇷 | vosk-model-small-fa-0.42 | 1,000 |
| `fr` | Français (French) | 🇫🇷 | vosk-model-small-fr-0.22 | 1,000 |
| `gu` | ગુજરાતી (Gujarati) | 🇮🇳 | vosk-model-small-gu-0.42 | 1,000 |
| `hi` | हिन्दी (Hindi) | 🇮🇳 | vosk-model-small-hi-0.22 | 999 |
| `it` | Italiano (Italian) | 🇮🇹 | vosk-model-small-it-0.22 | 1,000 |
| `ja` | 日本語 (Japanese) | 🇯🇵 | vosk-model-small-ja-0.22 | 1,000 |
| `kk` | Қазақша (Kazakh) | 🇰🇿 | vosk-model-small-kz-0.42 | 1,000 |
| `ko` | 한국어 (Korean) | 🇰🇷 | vosk-model-small-ko-0.22 | 1,000 |
| `ky` | Кыргызча (Kyrgyz) | 🇰🇬 | vosk-model-small-ky-0.42 | 1,000 |
| `nl` | Nederlands (Dutch) | 🇳🇱 | vosk-model-small-nl-0.22 | 1,000 |
| `pl` | Polski (Polish) | 🇵🇱 | vosk-model-small-pl-0.22 | 1,000 |
| `pt` | Português (Portuguese) | 🇵🇹 | vosk-model-small-pt-0.3 | 1,000 |
| `ru` | Русский (Russian) | 🇷🇺 | vosk-model-small-ru-0.22 | 1,000 |
| `te` | తెలుగు (Telugu) | 🇮🇳 | vosk-model-small-te-0.42 | 1,000 |
| `tg` | Тоҷикӣ (Tajik) | 🇹🇯 | vosk-model-small-tg-0.22 | 1,000 |
| `tr` | Türkçe (Turkish) | 🇹🇷 | vosk-model-small-tr-0.3 | 999 |
| `uk` | Українська (Ukrainian) | 🇺🇦 | vosk-model-small-uk-v3-nano | 999 |
| `uz` | Oʻzbekcha (Uzbek) | 🇺🇿 | vosk-model-small-uz-0.22 | 1,000 |
| `vi` | Tiếng Việt (Vietnamese) | 🇻🇳 | vosk-model-small-vn-0.4 | 1,000 |
| `zh` | 中文 (Chinese) | 🇨🇳 | vosk-model-small-cn-0.22 | 1,000 |

**Total**: 26 languages, 25,996 words

## Regional Coverage

### 🇪🇺 European Languages (10)
Dutch, French, German, Spanish, Portuguese, Italian, Catalan, Czech, Polish, Esperanto

### 🇷🇺 Slavic Languages (2)
Russian, Ukrainian

### 🌏 Asian Languages (5)
Chinese, Japanese, Korean, Vietnamese, Hindi

### 🕌 Middle Eastern Languages (2)
Turkish, Persian/Farsi

### 🏔️ Central Asian Languages (4)
Kazakh, Uzbek, Kyrgyz, Tajik

### 🇮🇳 Indian Subcontinent (2)
Gujarati, Telugu

## Adding New Languages

To add a new language, it must meet these requirements:

1. **Word list exists** in [bukowa/1000-common-words](https://github.com/bukowa/1000-common-words)
2. **Vosk model available** at [alphacephei.com/vosk/models](https://alphacephei.com/vosk/models)

Then:
1. Add the language mapping to `scripts/fetch_vocabulary.py`
2. Run `python scripts/fetch_vocabulary.py` to fetch words
3. Add the Vosk model URL to `DOWNLOADABLE_MODELS` in `main.py`

## Language Codes

We use ISO 639-1 two-letter codes where available:
- `en` = English
- `fr` = French
- `de` = German
- etc.

## Notes

- Some languages have 999 words instead of 1,000 (likely due to duplicates removed)
- All Vosk models are "small" versions optimized for mobile/desktop use
- Model sizes range from ~30MB to ~80MB
- All vocabulary is sourced from public domain word frequency lists

import json
import os
import queue
import threading
import time
import zipfile
import heapq
import shutil
import tempfile
import requests
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from difflib import SequenceMatcher

from kivy.app import App
from kivy.animation import Animation
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp, sp
from kivy.properties import ListProperty, NumericProperty, StringProperty
from kivy.resources import resource_find
from kivy.storage.jsonstore import JsonStore
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.screenmanager import Screen, ScreenManager, SlideTransition
from kivy.uix.spinner import Spinner
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout
from kivy.utils import platform

from datetime import datetime

from words_data import COMMON_WORDS, LANGUAGE_LABELS

try:
    from vosk import Model, KaldiRecognizer
except ImportError:  # pragma: no cover
    Model = None
    KaldiRecognizer = None

try:
    import pyaudio
except ImportError:  # pragma: no cover
    pyaudio = None

try:
    from jnius import JavaException, autoclass, cast, jarray
except ImportError:  # pragma: no cover
    JavaException = None
    autoclass = None
    cast = None
    jarray = None


class AnalyticsLogger:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log_path = self.directory / "events.jsonl"
        self._lock = threading.Lock()

    def log_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        entry: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "type": event_type,
        }
        entry.update(payload)
        encoded = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")

    def log_attempt(self, payload: Dict[str, Any]) -> None:
        self.log_event("word_attempt", payload)


@dataclass
class WordState:
    mastery: float = 0.0
    interval: float = 2.0
    streak: int = 0
    last_seen: float = 0.0
    due: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "mastery": self.mastery,
            "interval": self.interval,
            "streak": self.streak,
            "last_seen": self.last_seen,
            "due": self.due,
        }


class ProgressStore:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.store = JsonStore(path)

    def ensure(self, word: str) -> WordState:
        if not self.store.exists(word):
            state = WordState()
            self.store.put(word, **state.to_dict())
            return state
        data = self.store.get(word)
        return WordState(
            mastery=data.get("mastery", 0.0),
            interval=data.get("interval", 2.0),
            streak=data.get("streak", 0),
            last_seen=data.get("last_seen", 0.0),
            due=data.get("due", 0.0),
        )

    def save(self, word: str, state: WordState) -> None:
        self.store.put(word, **state.to_dict())

    def stats(self, vocabulary: List[str], threshold: float = 0.85) -> Tuple[int, int]:
        mastered = 0
        for word in vocabulary:
            if self.store.exists(word):
                if self.store.get(word).get("mastery", 0.0) >= threshold:
                    mastered += 1
        remaining = max(0, len(vocabulary) - mastered)
        return mastered, remaining


class VocabularyLoader:
    def __init__(self) -> None:
        self._languages: Dict[str, List[str]] = {code: list(words) for code, words in COMMON_WORDS.items()}
        self._labels: Dict[str, str] = dict(LANGUAGE_LABELS)
        self._external_directories: set[str] = set()

    def available_languages(self) -> Dict[str, str]:
        return dict(self._labels)

    def language_label(self, language: str) -> str:
        return self._labels.get(language, language)

    def load_common_words(self, language: str) -> List[str]:
        words = self._languages.get(language)
        if words is None:
            fallback = next(iter(self._languages.values()))
            return list(fallback)
        return list(words)

    def register_external_directory(self, path: str) -> None:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            return
        if str(resolved) in self._external_directories:
            return
        self._external_directories.add(str(resolved))
        self._load_directory(resolved)

    def refresh_external_sources(self) -> None:
        for directory in list(self._external_directories):
            self._load_directory(Path(directory))

    def _load_directory(self, directory: Path) -> None:
        try:
            files = list(directory.glob("*.json"))
        except Exception:
            return
        for file_path in files:
            self._load_json_file(file_path)

    def _load_json_file(self, file_path: Path) -> None:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return
        words = data.get("words")
        if not isinstance(words, list):
            return
        code = data.get("code") or data.get("language") or file_path.stem
        label = data.get("label") or code
        cleaned: List[str] = []
        seen = set()
        for item in words:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            cleaned.append(value)
        if not cleaned:
            return
        self._languages[code] = cleaned
        self._labels[code] = label

    def load_epub_words(self, path: str) -> List[str]:
        from ebooklib import ITEM_DOCUMENT, epub  # lazy import
        from bs4 import BeautifulSoup

        book = epub.read_epub(path)
        words: List[str] = []
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_body_content(), "html.parser")
            text = soup.get_text(" ")
            for token in text.split():
                cleaned = "".join(ch for ch in token if ch.isalpha()).lower()
                if cleaned:
                    words.append(cleaned)
        return words


class SRSDeck:
    def __init__(self, store: ProgressStore) -> None:
        self.store = store
        self.heap: List[Tuple[float, str]] = []
        self.active_words: List[str] = []
        self.lock = threading.Lock()

    def load(self, words: List[str], mode: str) -> None:
        now = time.time()
        with self.lock:
            self.heap.clear()
            self.active_words = words
            for idx, word in enumerate(words):
                state = self.store.ensure(word)
                due = state.due if state.due else now + (idx * 0.05 if mode == "epub" else 0)
                heapq.heappush(self.heap, (due, word))

    def next_word(self) -> Optional[str]:
        with self.lock:
            while self.heap:
                due, word = heapq.heappop(self.heap)
                if word in self.active_words:
                    return word
        return None

    def record(self, word: str, is_correct: bool, elapsed: float) -> WordState:
        state = self.store.ensure(word)
        now = time.time()
        if is_correct:
            state.streak += 1
            if elapsed < 2.0:
                state.mastery = min(1.0, state.mastery + 0.15)
                state.interval = min(state.interval * 2.0, 600.0)
            elif elapsed > 5.0:
                state.mastery = max(0.0, state.mastery - 0.1)
                state.interval = max(state.interval * 0.5, 5.0)
            else:
                state.mastery = min(1.0, state.mastery + 0.05)
                state.interval = min(state.interval * 1.5, 300.0)
        else:
            state.streak = 0
            state.mastery = max(0.0, state.mastery * 0.5)
            state.interval = 5.0
        state.last_seen = now
        state.due = now + max(1.5, state.interval * (1.0 - state.mastery + 0.1))
        self.store.save(word, state)
        with self.lock:
            heapq.heappush(self.heap, (state.due, word))
        return state


class SessionState:
    def __init__(self, mode: str, words: List[str], store: ProgressStore) -> None:
        self.mode = mode
        self.words = words
        self.unique_words = list(dict.fromkeys(words))
        self.deck = SRSDeck(store)
        self.deck.load(self.unique_words if mode == "common" else words, mode)
        self.current_word: Optional[str] = None

    def next_word(self) -> Optional[str]:
        word = self.deck.next_word()
        self.current_word = word
        return word

    def record(self, is_correct: bool, elapsed: float) -> WordState:
        if not self.current_word:
            return WordState()
        return self.deck.record(self.current_word, is_correct, elapsed)


class AudioListener:
    def __init__(self, model_path: str, result_queue: queue.Queue, stream_factory: Optional[Callable[[], Tuple[Optional[object], Optional[Callable[[], None]]]]] = None) -> None:
        self.model_path = model_path
        self.queue = result_queue
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._recognizer: Optional[KaldiRecognizer] = None
        self._stream = None
        self._audio = None
        self._stream_factory = stream_factory
        self._stream_cleanup: Optional[Callable[[], None]] = None

    def start(self) -> bool:
        if not Model or not KaldiRecognizer:
            return False
        if self._thread and self._thread.is_alive():
            return True
        model = Model(self.model_path)
        self._recognizer = KaldiRecognizer(model, 16000)
        if self._stream_factory:
            stream, cleanup = self._stream_factory()
            if stream is None:
                return False
            self._stream = stream
            self._stream_cleanup = cleanup
        else:
            if not pyaudio:
                return False
            self._audio = pyaudio.PyAudio()
            stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                frames_per_buffer=4096,
            )
            self._stream = stream

            def cleanup() -> None:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                stream.close()
                if self._audio:
                    self._audio.terminate()
                self._audio = None

            self._stream_cleanup = cleanup
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._stream_cleanup:
            self._stream_cleanup()
        self._stream_cleanup = None
        self._stream = None
        self._audio = None
        self._recognizer = None

    def _run(self) -> None:
        assert self._stream is not None
        assert self._recognizer is not None
        while self._running.is_set():
            data = self._stream.read(4096, exception_on_overflow=False)
            if not data:
                continue
            if self._recognizer.AcceptWaveform(data):
                payload = json.loads(self._recognizer.Result())
                text = payload.get("text", "")
                if text:
                    self.queue.put(("final", text))
            else:
                payload = json.loads(self._recognizer.PartialResult())
                partial = payload.get("partial", "")
                if partial:
                    self.queue.put(("partial", partial))


class DashboardScreen(Screen):
    mastered_text = StringProperty("0")
    remaining_text = StringProperty("0")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        layout = BoxLayout(orientation="vertical", padding=dp(24), spacing=dp(24))
        self.mastered_label = Label(text="Words Mastered: 0", font_size=sp(32), color=(0.86, 0.96, 0.99, 1))
        self.remaining_label = Label(text="Words Remaining: 0", font_size=sp(32), color=(0.86, 0.96, 0.99, 1))
        close_btn = Button(text="Back", size_hint=(1, 0.2), font_size=sp(20), background_normal="", background_color=(0.3, 0.4, 0.7, 1))
        close_btn.bind(on_release=lambda *_: App.get_running_app().transition_back())
        layout.add_widget(self.mastered_label)
        layout.add_widget(self.remaining_label)
        layout.add_widget(close_btn)
        self.add_widget(layout)

    def refresh(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        if not app.session:
            return
        mastered, remaining = app.progress.stats(app.session.unique_words)
        self.mastered_label.text = f"Words Mastered: {mastered}"
        self.remaining_label.text = f"Words Remaining: {remaining}"


class TrainingScreen(Screen):
    word_text = StringProperty("Tap a mode to begin")
    feedback_text = StringProperty("")
    timer_value = NumericProperty(0.0)
    bar_color = ListProperty([1, 1, 1, 1])

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._word_started_at: Optional[float] = None
        self._timer_event = None
        self._queue_event = None
        self._awaiting_result = False
        self._default_color = [1, 1, 1, 1]
        root = BoxLayout(orientation="vertical", padding=dp(24), spacing=dp(16))
        self.language_label = Label(text="Language: English", font_size=sp(20), size_hint=(1, 0.1), color=(0.75, 0.88, 1, 1))
        self.word_label = Label(text=self.word_text, font_size=sp(72), bold=True, size_hint=(1, 0.55), color=(0.97, 0.97, 1, 1))
        self._base_font_size = self.word_label.font_size
        self.progress_bar = ProgressBar(max=2.0, value=0.0, size_hint=(1, None), height=dp(24))
        self.progress_bar.color = (0.25, 0.74, 0.87, 1)
        self.feedback_label = Label(text="", font_size=sp(24), size_hint=(1, 0.2), color=(0.85, 0.93, 1, 1))
        controls = BoxLayout(size_hint=(1, 0.2), spacing=dp(16))
        skip_btn = Button(text="Skip", size_hint=(0.5, 1), font_size=sp(20), background_normal="", background_color=(0.8, 0.4, 0.4, 1))
        skip_btn.bind(on_release=lambda *_: self.skip_current())
        dash_btn = Button(text="Dashboard", size_hint=(0.5, 1), font_size=sp(20), background_normal="", background_color=(0.35, 0.6, 0.9, 1))
        dash_btn.bind(on_release=lambda *_: self.show_dashboard())
        controls.add_widget(skip_btn)
        controls.add_widget(dash_btn)
        root.add_widget(self.language_label)
        root.add_widget(self.word_label)
        root.add_widget(self.progress_bar)
        root.add_widget(self.feedback_label)
        root.add_widget(controls)
        self.add_widget(root)

    def on_enter(self) -> None:
        self._queue_event = Clock.schedule_interval(self._drain_audio_queue, 0.1)

    def on_leave(self) -> None:
        if self._timer_event is not None:
            self._timer_event.cancel()
            self._timer_event = None
        if self._queue_event is not None:
            self._queue_event.cancel()
            self._queue_event = None

    def display_word(self, word: str) -> None:
        self.word_text = word
        self.word_label.text = word.upper()
        self.progress_bar.value = 0.0
        self.feedback_label.text = ""
        self.word_label.color = (1, 1, 1, 1)
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        if hasattr(app, "loader"):
            self.set_language(app.loader.language_label(app.target_language))
        self._word_started_at = time.perf_counter()
        self._awaiting_result = True
        if self._timer_event is not None:
            self._timer_event.cancel()
        self._timer_event = Clock.schedule_interval(self._update_timer, 1 / 30)
        Animation.cancel_all(self.word_label)
        Animation(font_size=self._base_font_size * 1.08, duration=0.12).start(self.word_label)
        Clock.schedule_once(lambda *_: Animation(font_size=self._base_font_size, duration=0.15).start(self.word_label), 0.12)

    def _update_timer(self, _dt: float) -> None:
        if not self._word_started_at:
            return
        elapsed = time.perf_counter() - self._word_started_at
        self.progress_bar.value = min(elapsed, self.progress_bar.max)
        if elapsed > 5.0 and self._awaiting_result:
            self.feedback_label.text = f"Keep trying... {elapsed:.1f}s"

    def _drain_audio_queue(self, _dt: float) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        while not app.audio_queue.empty():
            event_type, payload = app.audio_queue.get()
            if event_type == "partial" and self._awaiting_result:
                self.feedback_label.text = payload
            elif event_type == "final" and self._awaiting_result:
                self._handle_recognition(payload)

    def _handle_recognition(self, transcript: str) -> None:
        if not self._awaiting_result or not self._word_started_at:
            return
        elapsed = time.perf_counter() - self._word_started_at
        score = self._score_transcript(self.word_text, transcript)
        is_correct = score >= 0.78
        self._awaiting_result = False
        if self._timer_event is not None:
            self._timer_event.cancel()
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        state = app.on_word_evaluated(is_correct, elapsed, transcript, score)
        if is_correct:
            self.word_label.color = (0.2, 0.8, 0.2, 1)
            self.feedback_label.text = f"Great! {elapsed:.2f}s"
            Animation.cancel_all(self.word_label)
            Animation(font_size=self._base_font_size * 1.05, duration=0.1).start(self.word_label)
        else:
            self.word_label.color = (0.9, 0.2, 0.2, 1)
            if transcript:
                self.feedback_label.text = f"Heard '{transcript}' ({score:.0%})"
            else:
                self.feedback_label.text = "Try again"
            Animation.cancel_all(self.word_label)
            Animation(font_size=self._base_font_size * 0.92, duration=0.1).start(self.word_label)
        Clock.schedule_once(self._restore_word_label_size, 0.25)
        Clock.schedule_once(lambda *_: self._request_next_word(), 0.8)

    def _request_next_word(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.prepare_next_word()

    def show_dashboard(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.show_dashboard()

    def skip_current(self) -> None:
        if not self._awaiting_result:
            return
        self._awaiting_result = False
        if self._timer_event is not None:
            self._timer_event.cancel()
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.on_word_evaluated(False, 6.0, "", 0.0)
        Clock.schedule_once(lambda *_: self._request_next_word(), 0.2)

    @staticmethod
    def _score_transcript(target: str, transcript: str) -> float:
        tokens = transcript.lower().split()
        if not tokens:
            return 0.0
        target = target.lower()
        scores = [SequenceMatcher(None, target, token).ratio() for token in tokens]
        return max(scores)

    def _restore_word_label_size(self, _dt: float) -> None:
        Animation.cancel_all(self.word_label)
        Animation(font_size=self._base_font_size, duration=0.16).start(self.word_label)

    def set_language(self, label: str) -> None:
        self.language_label.text = f"Language: {label}"


class FileChooserPopup(Popup):
    def __init__(self, on_select, filters: Optional[List[str]] = None, title: str = "Choose File", **kwargs) -> None:
        super().__init__(**kwargs)
        self.title = title
        self.size_hint = (0.9, 0.9)
        chooser = FileChooserListView(filters=filters or ["*"])
        chooser.bind(on_submit=lambda instance, selection, *_: self._choose(selection, on_select))
        chooser.bind(on_selection=lambda instance, selection: self._choose(selection, on_select, dismiss_only=True))
        layout = BoxLayout(orientation="vertical")
        layout.add_widget(chooser)
        cancel_btn = Button(text="Cancel", size_hint=(1, 0.1))
        cancel_btn.bind(on_release=lambda *_: self.dismiss())
        layout.add_widget(cancel_btn)
        self.add_widget(layout)

    def _choose(self, selection, on_select, dismiss_only: bool = False) -> None:
        if selection and not dismiss_only:
            on_select(selection[0])
            self.dismiss()


class ModeSelectionScreen(Screen):
    def __init__(self, languages: Dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.languages = languages
        self._code_by_label = {label: code for code, label in languages.items()}
        layout = BoxLayout(orientation="vertical", padding=dp(32), spacing=dp(24))
        title = Label(text="Voice-First Literacy Coach", font_size=sp(48), size_hint=(1, 0.25), color=(1, 1, 1, 1))
        self.language_spinner = Spinner(
            text="",
            values=tuple(languages.values()),
            size_hint=(1, 0.2),
            font_size=sp(20),
            background_normal="",
            background_color=(0.23, 0.35, 0.6, 1),
        )
        self.language_spinner.bind(text=self._on_language_selected)
        mode_a_btn = Button(text="Start 1000 Words", size_hint=(1, 0.22), font_size=sp(24), background_normal="", background_color=(0.18, 0.5, 0.82, 1))
        mode_b_btn = Button(text="Load ePub Story", size_hint=(1, 0.22), font_size=sp(24), background_normal="", background_color=(0.26, 0.67, 0.5, 1))
        settings_btn = Button(text="Settings & Models", size_hint=(1, 0.22), font_size=sp(22), background_normal="", background_color=(0.45, 0.45, 0.78, 1))
        mode_a_btn.bind(on_release=lambda *_: self.start_common())
        mode_b_btn.bind(on_release=lambda *_: self.choose_epub())
        settings_btn.bind(on_release=lambda *_: self.open_settings())
        layout.add_widget(title)
        layout.add_widget(self.language_spinner)
        layout.add_widget(mode_a_btn)
        layout.add_widget(mode_b_btn)
        layout.add_widget(settings_btn)
        self.add_widget(layout)
        Clock.schedule_once(lambda *_: self._sync_spinner_to_app(), 0)

    def start_common(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.start_session("common")

    def choose_epub(self) -> None:
        def on_select(path: str) -> None:
            app: VoiceFirstApp = App.get_running_app()  # type: ignore
            app.start_session("epub", path)

        popup = FileChooserPopup(on_select, filters=["*.epub"], title="Choose ePub")
        popup.open()

    def open_settings(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.show_settings()

    def _on_language_selected(self, _spinner: Spinner, value: str) -> None:
        code = self._code_by_label.get(value)
        if not code:
            return
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.set_target_language(code)

    def _sync_spinner_to_app(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        code = getattr(app, "target_language", next(iter(self.languages)))
        self.update_language_display(code)

    def update_language_display(self, language_code: str) -> None:
        label = self.languages.get(language_code)
        if not label:
            return
        self.language_spinner.text = label

    def update_languages(self, languages: Dict[str, str]) -> None:
        previous_selection = self.language_spinner.text
        self.languages = languages
        self._code_by_label = {label: code for code, label in languages.items()}
        values = tuple(languages.values())
        self.language_spinner.values = values
        if previous_selection in values:
            self.language_spinner.text = previous_selection
        elif values:
            self.language_spinner.text = values[0]


DOWNLOADABLE_MODELS: List[Dict[str, str]] = [
    {
        "label": "English (US) Small 0.15",
        "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
    },
    {
        "label": "Dutch Small 0.22",
        "url": "https://alphacephei.com/vosk/models/vosk-model-small-nl-0.22.zip",
    },
]


class ModelSettingsScreen(Screen):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        layout = BoxLayout(orientation="vertical", padding=dp(24), spacing=dp(18))
        header = Label(text="Speech Model Settings", font_size=sp(36), size_hint=(1, 0.15), color=(1, 1, 1, 1))
        self.status_label = Label(text="", font_size=sp(18), size_hint=(1, 0.1), color=(0.82, 0.9, 1, 1))
        self.models_layout = GridLayout(cols=1, spacing=dp(12), size_hint=(1, None), padding=(0, dp(6)))
        self.models_layout.bind(minimum_height=self.models_layout.setter("height"))
        scroll = ScrollView(size_hint=(1, 0.6))
        scroll.add_widget(self.models_layout)
        import_btn = Button(text="Import Vosk Model", size_hint=(1, 0.12), font_size=sp(20), background_normal="", background_color=(0.33, 0.54, 0.86, 1))
        import_btn.bind(on_release=lambda *_: self.import_model())
        back_btn = Button(text="Back", size_hint=(1, 0.12), font_size=sp(20), background_normal="", background_color=(0.28, 0.42, 0.68, 1))
        back_btn.bind(on_release=lambda *_: self.return_to_menu())
        layout.add_widget(header)
        layout.add_widget(self.status_label)
        layout.add_widget(scroll)
        layout.add_widget(import_btn)
        layout.add_widget(back_btn)
        self.add_widget(layout)

    def on_pre_enter(self) -> None:
        self.refresh_models()

    def set_status(self, message: str) -> None:
        self.status_label.text = message

    def refresh_models(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        models = app.list_vosk_models()
        active = app.model_path
        self.models_layout.clear_widgets()
        if not models:
            placeholder = Label(text="No models available", size_hint=(1, None), height=dp(48), font_size=sp(20), color=(0.88, 0.94, 1, 1))
            self.models_layout.add_widget(placeholder)
        else:
            for entry in models:
                label = entry.get("label", entry.get("path", ""))
                path = entry.get("path")
                if not path:
                    continue
                is_active = active and os.path.abspath(path) == os.path.abspath(active)
                button = Button(
                    text=f"{label}\n{path}",
                    size_hint=(1, None),
                    height=dp(64),
                    halign="left",
                    valign="middle",
                    background_normal="",
                    background_color=(0.24, 0.58, 0.4, 1) if is_active else (0.21, 0.28, 0.48, 1),
                )
                button.text_size = (Window.width - dp(96), None)
                button.bind(size=lambda inst, _size: setattr(inst, "text_size", (inst.width - dp(24), None)))
                button.padding = (dp(16), dp(12))
                button.bind(on_release=lambda *_btn, target=path: app.set_model_path(target))
                self.models_layout.add_widget(button)
        download_header = Label(text="Download official models", size_hint=(1, None), height=dp(42), font_size=sp(18), color=(0.82, 0.9, 1, 1))
        self.models_layout.add_widget(download_header)
        for spec in DOWNLOADABLE_MODELS:
            title = spec.get("label", spec.get("url", "Vosk Model"))
            download_btn = Button(
                text=f"Get {title}",
                size_hint=(1, None),
                height=dp(56),
                background_normal="",
                background_color=(0.33, 0.48, 0.78, 1),
            )
            download_btn.padding = (dp(16), dp(10))
            download_btn.bind(on_release=lambda *_btn, data=spec: self._download_preset(data))
            self.models_layout.add_widget(download_btn)

    def import_model(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        chooser = FileChooserPopup(app.import_vosk_model, filters=["*.zip"], title="Import Vosk Model")
        chooser.open()

    def return_to_menu(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.show_mode_selection()

    def _download_preset(self, spec: Dict[str, str]) -> None:
        url = spec.get("url")
        if not url:
            return
        label = spec.get("label", url)
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.queue_model_download(label, url)


def _locate_vosk_model(models_dir: Path) -> Optional[Path]:
    try:
        entries = sorted(models_dir.iterdir())
    except FileNotFoundError:
        return None
    for candidate in entries:
        if candidate.is_dir() and candidate.name.startswith("vosk-model"):
            return candidate
    return None


def _is_safe_member(name: str) -> bool:
    member_path = Path(name)
    if member_path.is_absolute():
        return False
    return ".." not in member_path.parts


def _extract_model_zip(zip_path: Path, models_dir: Path) -> Optional[Path]:
    if not zip_path.exists():
        return None
    with tempfile.TemporaryDirectory() as temp_root:
        with zipfile.ZipFile(zip_path, "r") as archive:
            members = [name for name in archive.namelist() if _is_safe_member(name)]
            if not members:
                return None
            archive.extractall(temp_root, members)
        extracted_root = Path(temp_root)
        children = [child for child in extracted_root.iterdir()]
        if len(children) == 1 and children[0].is_dir():
            source_dir = children[0]
        else:
            source_dir = extracted_root / zip_path.stem
            source_dir.mkdir(parents=True, exist_ok=True)
            for child in children:
                shutil.move(str(child), source_dir / child.name)
        destination = models_dir / source_dir.name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source_dir, destination)
        return destination


def prepare_vosk_model(app: App) -> Optional[str]:
    user_data_dir = Path(app.user_data_dir)
    models_dir = user_data_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    candidate = _locate_vosk_model(models_dir)
    if candidate:
        return str(candidate)

    legacy_dir = user_data_dir / "vosk-model-small-en-us"
    if legacy_dir.is_dir():
        return str(legacy_dir)

    bundled_zip = resource_find("models/vosk-model-small-en-us.zip")
    if bundled_zip and os.path.isfile(bundled_zip):
        with zipfile.ZipFile(bundled_zip, "r") as archive:
            archive.extractall(models_dir)
        candidate = _locate_vosk_model(models_dir)
        if candidate:
            return str(candidate)

    fallback = resource_find("models/vosk-model-small-en-us")
    if fallback and os.path.isdir(fallback):
        dest = models_dir / Path(fallback).name
        try:
            shutil.copytree(fallback, dest, dirs_exist_ok=True)
        except Exception:
            pass
        else:
            return str(dest)

    if os.path.isdir("vosk-model-small-en-us"):
        return os.path.abspath("vosk-model-small-en-us")
    return None


def android_stream_factory() -> Tuple[Optional[object], Optional[Callable[[], None]]]:
    if platform != "android":
        return None, None
    if not autoclass or not jarray:
        return None, None
    try:
        AudioRecord = autoclass("android.media.AudioRecord")
        AudioFormat = autoclass("android.media.AudioFormat")
        MediaRecorder = autoclass("android.media.MediaRecorder")
        buffer_size = AudioRecord.getMinBufferSize(
            16000,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        if buffer_size <= 0:
            buffer_size = 4096
        audio_record = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            16000,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            buffer_size,
        )
        if audio_record.getState() != AudioRecord.STATE_INITIALIZED:
            return None, None
        audio_record.startRecording()

        class AndroidAudioStream:
            def __init__(self, recorder, chunk_size: int) -> None:
                self._recorder = recorder
                self._chunk = chunk_size

            def read(self, num_bytes: int, exception_on_overflow: bool = False) -> bytes:  # noqa: ARG002
                size = max(num_bytes, self._chunk)
                buffer = jarray('b', size)
                read = self._recorder.read(buffer, 0, size)
                if read <= 0:
                    return b""
                return bytes(buffer[:read])

        stream = AndroidAudioStream(audio_record, buffer_size)

        def cleanup() -> None:
            try:
                if audio_record.getState() == AudioRecord.STATE_INITIALIZED:
                    audio_record.stop()
            except Exception:  # pragma: no cover - defensive on Android runtime
                pass
            audio_record.release()

        return stream, cleanup
    except Exception:  # pragma: no cover - Android-specific failures
        return None, None


def ensure_android_permissions() -> None:
    if platform != "android":
        return
    try:
        from android.permissions import Permission, request_permissions
    except ImportError:  # pragma: no cover
        return
    try:
        request_permissions([Permission.RECORD_AUDIO, Permission.READ_EXTERNAL_STORAGE])
    except Exception:  # pragma: no cover - ignore runtime permission issues
        pass


class VoiceEngine:
    def __init__(self, stream_factory: Optional[Callable[[], Tuple[Optional[object], Optional[Callable[[], None]]]]] = None) -> None:
        self.listener: Optional[AudioListener] = None
        self.stream_factory = stream_factory

    def start(self, model_path: Optional[str], result_queue: queue.Queue) -> bool:
        if not model_path:
            return False
        if self.listener:
            return self.listener.start()
        self.listener = AudioListener(model_path, result_queue, self.stream_factory)
        return self.listener.start()

    def stop(self) -> None:
        if self.listener:
            self.listener.stop()
            self.listener = None

    def set_stream_factory(self, factory: Callable[[], Tuple[Optional[object], Optional[Callable[[], None]]]]) -> None:
        self.stream_factory = factory
        if self.listener:
            self.listener.stop()
            self.listener = None


class VoiceFirstApp(App):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.progress: Optional[ProgressStore] = None
        self.loader = VocabularyLoader()
        self.language_labels = self.loader.available_languages()
        self.target_language = next(iter(self.language_labels)) if self.language_labels else "en"
        self.session: Optional[SessionState] = None
        self.audio_queue: queue.Queue = queue.Queue()
        if platform == "android":
            self.voice_engine = VoiceEngine(android_stream_factory)
        else:
            self.voice_engine = VoiceEngine()
        self._android_permissions_requested = False
        self.model_path: Optional[str] = None
        self.analytics: Optional[AnalyticsLogger] = None
        self.manager = ScreenManager(transition=SlideTransition())
        self.model_registry: Dict[str, Any] = {"models": {}, "active_model": None}
        self._download_thread: Optional[threading.Thread] = None

    def build(self) -> ScreenManager:
        storage_path = os.path.join(self.user_data_dir, "progress.json")
        self.progress = ProgressStore(storage_path)
        self.model_path = prepare_vosk_model(self)
        self._load_model_registry()
        if self.model_registry.get("active_model"):
            candidate = Path(self.model_registry["active_model"])
            if candidate.exists():
                self.model_path = str(candidate)
            else:
                self.model_registry["active_model"] = self.model_path
        elif self.model_path:
            self.model_registry["active_model"] = self.model_path
        if self.model_path:
            self._register_model(Path(self.model_path))
        self._save_model_registry()
        project_root = Path(getattr(self, "directory", os.getcwd()))
        project_vocab_dir = project_root / "data" / "vocabulary"
        user_vocab_dir = Path(self.user_data_dir) / "vocabulary"
        os.makedirs(user_vocab_dir, exist_ok=True)
        self.analytics = AnalyticsLogger(Path(self.user_data_dir) / "logs")
        self.loader.register_external_directory(project_vocab_dir)
        self.loader.register_external_directory(user_vocab_dir)
        self.loader.refresh_external_sources()
        self.language_labels = self.loader.available_languages()
        if not self.language_labels:
            self.language_labels = {"en": "English"}
        if self.target_language not in self.language_labels:
            self.target_language = next(iter(self.language_labels))
        Window.clearcolor = (0.08, 0.1, 0.18, 1)
        mode_screen = ModeSelectionScreen(name="mode", languages=self.language_labels)
        training_screen = TrainingScreen(name="training")
        dashboard_screen = DashboardScreen(name="dashboard")
        settings_screen = ModelSettingsScreen(name="settings")
        self.manager.add_widget(mode_screen)
        self.manager.add_widget(training_screen)
        self.manager.add_widget(dashboard_screen)
        self.manager.add_widget(settings_screen)
        self.update_language_ui()
        return self.manager

    @property
    def training_screen(self) -> TrainingScreen:
        return self.manager.get_screen("training")  # type: ignore

    @property
    def dashboard_screen(self) -> DashboardScreen:
        return self.manager.get_screen("dashboard")  # type: ignore

    @property
    def mode_screen(self) -> ModeSelectionScreen:
        return self.manager.get_screen("mode")  # type: ignore

    @property
    def settings_screen(self) -> ModelSettingsScreen:
        return self.manager.get_screen("settings")  # type: ignore

    def _models_dir(self) -> Path:
        models_dir = Path(self.user_data_dir) / "models"
        models_dir.mkdir(parents=True, exist_ok=True)
        return models_dir

    def _registry_path(self) -> Path:
        return self._models_dir() / "registry.json"

    def _load_model_registry(self) -> None:
        registry_path = self._registry_path()
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            models = data.get("models")
            if isinstance(models, dict):
                self.model_registry["models"] = models
            active = data.get("active_model")
            if isinstance(active, str):
                self.model_registry["active_model"] = active

    def _save_model_registry(self) -> None:
        registry_path = self._registry_path()
        payload = {
            "models": self.model_registry.get("models", {}),
            "active_model": self.model_registry.get("active_model"),
        }
        registry_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _register_model(self, path: Path) -> None:
        if "models" not in self.model_registry or not isinstance(self.model_registry["models"], dict):
            self.model_registry["models"] = {}
        label = self.model_registry["models"].get(str(path))
        if not label:
            pretty = path.name.replace("_", " ").replace("-", " ")
            self.model_registry["models"][str(path)] = pretty.title()

    def list_vosk_models(self) -> List[Dict[str, str]]:
        models: List[Dict[str, str]] = []
        registry = self.model_registry.get("models", {})
        if not isinstance(registry, dict):
            registry = {}
            self.model_registry["models"] = registry
        for entry in sorted(self._models_dir().iterdir(), key=lambda path: path.name.lower()):
            if entry.is_dir():
                label = registry.get(str(entry), entry.name)
                models.append({"path": str(entry), "label": label})
        legacy_dir = Path(self.user_data_dir) / "vosk-model-small-en-us"
        if legacy_dir.is_dir():
            label = registry.get(str(legacy_dir), f"{legacy_dir.name} (legacy)")
            models.append({"path": str(legacy_dir), "label": label})
        if self.model_path and all(model.get("path") != self.model_path for model in models):
            label = registry.get(self.model_path, Path(self.model_path).name)
            models.append({"path": self.model_path, "label": label})
        return models

    def _notify_model_status(self, message: str) -> None:
        def _apply(_dt: float) -> None:
            try:
                self.settings_screen.set_status(message)
            except Exception:
                pass

        Clock.schedule_once(_apply, 0)

    def set_model_path(self, path: str) -> None:
        target = Path(path)
        if not target.exists():
            self._notify_model_status("Selected model is unavailable")
            return
        self.model_path = str(target)
        self.model_registry["active_model"] = self.model_path
        self._register_model(target)
        self._save_model_registry()
        self._notify_model_status(f"Active model: {target.name}")
        try:
            self.settings_screen.refresh_models()
        except Exception:
            pass
        if self.session:
            self.voice_engine.stop()
            if not self.voice_engine.start(self.model_path, self.audio_queue):
                self._notify_model_status("Speech engine unavailable with selected model")

    def import_vosk_model(self, zip_path: str) -> None:
        models_dir = self._models_dir()
        try:
            extracted = _extract_model_zip(Path(zip_path), models_dir)
        except Exception:
            extracted = None
        if not extracted:
            self._notify_model_status("Failed to import model")
            return
        self._register_model(extracted)
        self.model_path = str(extracted)
        self.model_registry["active_model"] = self.model_path
        self._save_model_registry()
        try:
            self.settings_screen.refresh_models()
        except Exception:
            pass
        self._notify_model_status(f"Imported model: {extracted.name}")
        if self.session:
            self.voice_engine.stop()
            if not self.voice_engine.start(self.model_path, self.audio_queue):
                self._notify_model_status("Speech engine unavailable with imported model")

    def queue_model_download(self, label: str, url: str) -> None:
        if self._download_thread and self._download_thread.is_alive():
            self._notify_model_status("Another download is already running")
            return

        def _task() -> None:
            tmp_path: Optional[Path] = None
            try:
                self._notify_model_status(f"Downloading {label}...")
                response = requests.get(url, stream=True, timeout=30)
                response.raise_for_status()
                total = int(response.headers.get("content-length", 0))
                downloaded = 0
                last_report = -1
                with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as temp_file:
                    tmp_path = Path(temp_file.name)
                    for chunk in response.iter_content(chunk_size=8192):
                        if not chunk:
                            continue
                        temp_file.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            percent = int(downloaded * 100 / total)
                            if percent != last_report and percent % 5 == 0:
                                self._notify_model_status(f"Downloading {label}: {percent}%")
                                last_report = percent
                self._notify_model_status(f"Installing {label}...")
                if tmp_path:
                    self.import_vosk_model(str(tmp_path))
            except requests.RequestException as exc:
                self._notify_model_status(f"Download failed: {exc}")
            except Exception as exc:
                self._notify_model_status(f"Download failed: {exc}")
            finally:
                if tmp_path and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except Exception:
                        pass
                self._download_thread = None

        self._download_thread = threading.Thread(target=_task, daemon=True)
        self._download_thread.start()

    def show_settings(self) -> None:
        try:
            self.settings_screen.refresh_models()
        except Exception:
            pass
        self.manager.current = "settings"

    def show_mode_selection(self) -> None:
        self.manager.current = "mode"

    def set_target_language(self, language: str) -> None:
        available = self.loader.available_languages()
        if language not in available:
            return
        if language == self.target_language:
            return
        self.language_labels = available
        self.target_language = language
        self.update_language_ui()

    def refresh_languages(self) -> None:
        self.loader.refresh_external_sources()
        self.language_labels = self.loader.available_languages()
        if not self.language_labels:
            self.language_labels = {"en": "English"}
        if self.target_language not in self.language_labels:
            self.target_language = next(iter(self.language_labels))
        self.update_language_ui()

    def start_session(self, mode: str, source: Optional[str] = None) -> None:
        assert self.progress is not None
        if platform == "android" and not self._android_permissions_requested:
            ensure_android_permissions()
            self._android_permissions_requested = True
        if mode == "common":
            words = self.loader.load_common_words(self.target_language)
        else:
            if not source:
                raise ValueError("ePub path required")
            words = self.loader.load_epub_words(source)
        if not words:
            raise ValueError("No words available for the selected mode")
        self.session = SessionState(mode, words, self.progress)
        if self.analytics:
            self.analytics.log_event(
                "session_start",
                {
                    "mode": mode,
                    "language": self.target_language,
                    "vocabulary_size": len(self.session.unique_words if mode == "common" else words),
                    "source": source or "",
                },
            )
        self.manager.current = "training"
        self.update_language_ui()
        self.prepare_next_word()
        if not self.voice_engine.start(self.model_path, self.audio_queue):
            self.training_screen.feedback_label.text = "Speech engine unavailable; using manual mode"

    def prepare_next_word(self) -> None:
        if not self.session:
            return
        word = self.session.next_word()
        if not word:
            self.training_screen.word_label.text = "Session Complete"
            self.training_screen.feedback_label.text = ""
            return
        self.training_screen.set_language(self.loader.language_label(self.target_language))
        self.training_screen.display_word(word)

    def on_word_evaluated(self, is_correct: bool, elapsed: float, transcript: str, score: float) -> WordState:
        if not self.session:
            return WordState()
        state = self.session.record(is_correct, elapsed)
        if self.analytics:
            self.analytics.log_attempt(
                {
                    "mode": self.session.mode,
                    "language": self.target_language if self.session.mode == "common" else "epub",
                    "word": self.session.current_word,
                    "elapsed_seconds": round(elapsed, 3),
                    "is_correct": is_correct,
                    "score": round(score, 3),
                    "transcript": transcript,
                    "mastery": round(state.mastery, 3),
                    "interval": round(state.interval, 3),
                }
            )
        return state

    def show_dashboard(self) -> None:
        self.dashboard_screen.refresh()
        self.manager.current = "dashboard"

    def on_stop(self) -> None:
        self.voice_engine.stop()

    def transition_back(self) -> None:
        self.manager.current = "training"

    def update_language_ui(self) -> None:
        label = self.loader.language_label(self.target_language)
        try:
            self.training_screen.set_language(label)
        except Exception:
            pass
        try:
            self.mode_screen.update_languages(self.language_labels)
            self.mode_screen.update_language_display(self.target_language)
        except Exception:
            pass


if __name__ == "__main__":
    VoiceFirstApp().run()

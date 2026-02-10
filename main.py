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
import logging
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from difflib import SequenceMatcher

from kivy.app import App
from kivy.animation import Animation
from kivy.clock import Clock
from kivy.core.text import LabelBase
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
from ui_strings import LANGUAGE_UI_STRINGS

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('voicefirst_app.log', mode='a')
    ]
)
logger = logging.getLogger(__name__)

try:
    from vosk import Model, KaldiRecognizer
    logger.info("Vosk library loaded successfully")
except ImportError as e:  # pragma: no cover
    Model = None
    KaldiRecognizer = None
    logger.warning(f"Vosk library not available: {e}")

try:
    import pyaudio
    logger.info("PyAudio library loaded successfully")
except ImportError as e:  # pragma: no cover
    pyaudio = None
    logger.warning(f"PyAudio library not available: {e}")

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
        logger.debug(f"Saved progress for '{word}': mastery={state.mastery:.2f}, streak={state.streak}")

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
        logger.info(f"Word '{word}' evaluated: correct={is_correct}, mastery={state.mastery:.2f}, streak={state.streak}")
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
        try:
            if not Model or not KaldiRecognizer:
                logger.error("Voice recognition unavailable: Vosk library not loaded")
                logger.info("To enable voice: pip install vosk")
                return False
            
            if self._thread and self._thread.is_alive():
                logger.info("Audio listener already running")
                return True
            
            logger.info(f"Loading Vosk model from: {self.model_path}")
            if not os.path.exists(self.model_path):
                logger.error(f"Vosk model path does not exist: {self.model_path}")
                return False
            
            model = Model(self.model_path)
            self._recognizer = KaldiRecognizer(model, 16000)
            logger.info("Vosk model loaded successfully")
            
            if self._stream_factory:
                logger.info("Using custom audio stream factory")
                stream, cleanup = self._stream_factory()
                if stream is None:
                    logger.error("Custom stream factory returned None")
                    return False
                self._stream = stream
                self._stream_cleanup = cleanup
            else:
                if not pyaudio:
                    logger.error("Voice recognition unavailable: PyAudio library not loaded")
                    logger.info("To enable voice: pip install pyaudio")
                    return False
                
                logger.info("Initializing PyAudio stream")
                self._audio = pyaudio.PyAudio()
                stream = self._audio.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=16000,
                    input=True,
                    frames_per_buffer=4096,
                )
                self._stream = stream
                logger.info("PyAudio stream opened successfully")

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
            logger.info("Audio listener thread started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start audio listener: {type(e).__name__}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

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
        main_layout = BoxLayout(orientation="vertical", padding=dp(20), spacing=dp(12))
        
        # Stats summary
        self.mastered_label = Label(
            text="Words Mastered: 0",
            font_size=sp(28),
            size_hint=(1, 0.08),
            color=(0.86, 0.96, 0.99, 1)
        )
        self.remaining_label = Label(
            text="Words Remaining: 0",
            font_size=sp(28),
            size_hint=(1, 0.08),
            color=(0.86, 0.96, 0.99, 1)
        )
        
        # Word lists in scrollview
        scroll = ScrollView(size_hint=(1, 0.64))
        self.words_layout = BoxLayout(
            orientation="vertical",
            size_hint_y=None,
            spacing=dp(8),
            padding=dp(8)
        )
        self.words_layout.bind(minimum_height=self.words_layout.setter('height'))
        scroll.add_widget(self.words_layout)
        
        # Button controls
        button_controls = BoxLayout(size_hint=(1, 0.12), spacing=dp(16))
        self.continue_btn = Button(
            text="Continue",
            font_size=sp(20),
            background_normal="",
            background_color=(0.3, 0.7, 0.4, 1)
        )
        self.continue_btn.bind(on_release=lambda *_: self.continue_training())
        menu_btn = Button(
            text="Main Menu",
            font_size=sp(20),
            background_normal="",
            background_color=(0.5, 0.5, 0.5, 1)
        )
        menu_btn.bind(on_release=lambda *_: self.go_to_menu())
        button_controls.add_widget(self.continue_btn)
        button_controls.add_widget(menu_btn)
        
        main_layout.add_widget(self.mastered_label)
        main_layout.add_widget(self.remaining_label)
        main_layout.add_widget(scroll)
        main_layout.add_widget(button_controls)
        self.add_widget(main_layout)
    
    def on_pre_enter(self) -> None:
        """Refresh stats when entering the dashboard."""
        self.refresh()
    
    def continue_training(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        if app.session:
            app.manager.current = "training"
        else:
            # No active session, go back to menu
            app.manager.current = "mode"
    
    def go_to_menu(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.manager.current = "mode"

    def refresh(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        
        # Clear previous word lists
        self.words_layout.clear_widgets()
        
        # Determine which words to analyze
        if not app.session:
            # Show overall stats if no active session
            self.continue_btn.text = "Back"
            if app.progress and app.language_labels:
                words = app.loader.load_common_words(app.target_language)
            else:
                words = []
        else:
            # Active session - show session words
            self.continue_btn.text = "Continue"
            words = app.session.unique_words
        
        if not words:
            self.mastered_label.text = "Words Mastered: 0"
            self.remaining_label.text = "Words Remaining: 0"
            return
        
        # Categorize words
        mastered_words = []
        in_progress_words = []
        not_attempted_words = []
        
        for word in words:
            if app.progress.store.exists(word):
                mastery = app.progress.store.get(word).get("mastery", 0.0)
                if mastery >= 0.85:
                    mastered_words.append((word, mastery))
                elif mastery > 0.0:
                    in_progress_words.append((word, mastery))
                else:
                    not_attempted_words.append(word)
            else:
                not_attempted_words.append(word)
        
        # Update summary
        self.mastered_label.text = f"Words Mastered: {len(mastered_words)}"
        self.remaining_label.text = f"Words Remaining: {len(words) - len(mastered_words)}"
        
        # Display mastered words
        if mastered_words:
            self._add_section_header("✓ Mastered Words", (0.2, 0.8, 0.3, 1))
            for word, mastery in sorted(mastered_words, key=lambda x: -x[1]):
                self._add_word_item(word, mastery, (0.2, 0.7, 0.3, 1))
        
        # Display in-progress words
        if in_progress_words:
            self._add_section_header("⏳ In Progress", (0.3, 0.6, 0.9, 1))
            for word, mastery in sorted(in_progress_words, key=lambda x: -x[1]):
                self._add_word_item(word, mastery, (0.3, 0.5, 0.8, 1))
        
        # Display not attempted words (first 20 only to avoid clutter)
        if not_attempted_words:
            self._add_section_header(
                f"○ Not Attempted ({len(not_attempted_words)})",
                (0.6, 0.6, 0.6, 1)
            )
            for word in sorted(not_attempted_words[:20]):
                self._add_word_item(word, 0.0, (0.5, 0.5, 0.5, 1))
            if len(not_attempted_words) > 20:
                more_label = Label(
                    text=f"... and {len(not_attempted_words) - 20} more",
                    size_hint_y=None,
                    height=dp(32),
                    font_size=sp(14),
                    color=(0.7, 0.7, 0.7, 1),
                    italic=True
                )
                self.words_layout.add_widget(more_label)
    
    def _add_section_header(self, text: str, color: tuple) -> None:
        """Add a section header to the word list."""
        header = Label(
            text=text,
            size_hint_y=None,
            height=dp(40),
            font_size=sp(20),
            bold=True,
            color=color,
            halign="left",
            valign="middle"
        )
        header.bind(size=header.setter('text_size'))
        self.words_layout.add_widget(header)
    
    def _add_word_item(self, word: str, mastery: float, color: tuple) -> None:
        """Add a word item to the list."""
        mastery_pct = int(mastery * 100)
        if mastery > 0:
            text = f"{word} ({mastery_pct}%)"
        else:
            text = word
        
        label = Label(
            text=text,
            size_hint_y=None,
            height=dp(32),
            font_size=sp(16),
            color=color,
            halign="left",
            valign="middle"
        )
        label.bind(size=label.setter('text_size'))
        self.words_layout.add_widget(label)


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
        skip_btn = Button(text="Skip", size_hint=(0.33, 1), font_size=sp(20), background_normal="", background_color=(0.8, 0.4, 0.4, 1))
        skip_btn.bind(on_release=lambda *_: self.skip_current())
        dash_btn = Button(text="Dashboard", size_hint=(0.33, 1), font_size=sp(20), background_normal="", background_color=(0.35, 0.6, 0.9, 1))
        dash_btn.bind(on_release=lambda *_: self.show_dashboard())
        back_btn = Button(text="Main Menu", size_hint=(0.34, 1), font_size=sp(20), background_normal="", background_color=(0.5, 0.5, 0.5, 1))
        back_btn.bind(on_release=lambda *_: self.return_to_menu())
        controls.add_widget(skip_btn)
        controls.add_widget(dash_btn)
        controls.add_widget(back_btn)
        root.add_widget(self.language_label)
        root.add_widget(self.word_label)
        root.add_widget(self.progress_bar)
        root.add_widget(self.feedback_label)
        root.add_widget(controls)
        self.add_widget(root)

    def on_enter(self) -> None:
        self._queue_event = Clock.schedule_interval(self._drain_audio_queue, 0.1)
        # Restart timer if we're in the middle of a word
        if self._word_started_at and self._awaiting_result and self._timer_event is None:
            self._timer_event = Clock.schedule_interval(self._update_timer, 1 / 30)

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
            app: VoiceFirstApp = App.get_running_app()  # type: ignore
            ui_strings = LANGUAGE_UI_STRINGS.get(app.target_language, LANGUAGE_UI_STRINGS["en"])
            self.feedback_label.text = f"{ui_strings['keep_trying']} {elapsed:.1f}s"

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
        
        # Get localized UI strings
        ui_strings = LANGUAGE_UI_STRINGS.get(app.target_language, LANGUAGE_UI_STRINGS["en"])
        
        if is_correct:
            self.word_label.color = (0.2, 0.8, 0.2, 1)
            self.feedback_label.text = f"{ui_strings['great']} {elapsed:.2f}s"
            Animation.cancel_all(self.word_label)
            Animation(font_size=self._base_font_size * 1.05, duration=0.1).start(self.word_label)
        else:
            self.word_label.color = (0.9, 0.2, 0.2, 1)
            if transcript:
                self.feedback_label.text = f"{ui_strings['heard']} '{transcript}' ({score:.0%})"
            else:
                self.feedback_label.text = ui_strings['try_again']
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
    
    def return_to_menu(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.show_mode_selection()


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


class LanguageSelectionScreen(Screen):
    """Dedicated screen for selecting practice language with native scripts."""
    
    def __init__(self, languages: Dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.languages = languages
        self.selected_language = None
        self.language_buttons = {}
        
        main_layout = BoxLayout(orientation="vertical", padding=dp(20), spacing=dp(16))
        
        # Title
        title = Label(
            text="Select Practice Language",
            font_size=sp(36),
            size_hint=(1, 0.1),
            color=(1, 1, 1, 1),
            bold=True
        )
        
        # Scrollable language grid with larger buttons
        scroll = ScrollView(size_hint=(1, 0.75))
        self.lang_grid = GridLayout(
            cols=4,
            spacing=dp(12),
            size_hint_y=None,
            padding=dp(8)
        )
        self.lang_grid.bind(minimum_height=self.lang_grid.setter('height'))
        
        # Create language buttons with native scripts
        sorted_langs = sorted(languages.items(), key=lambda x: x[1])
        for code, label in sorted_langs:
            btn = Button(
                text=label,
                size_hint_y=None,
                height=dp(80),
                font_size=sp(20),
                font_name='NotoSans',  # Use Unicode-capable font
                background_normal="",
                background_color=(0.25, 0.35, 0.55, 1)
            )
            btn.bind(on_release=lambda b, c=code: self.select_language(c))
            self.language_buttons[code] = btn
            self.lang_grid.add_widget(btn)
        
        scroll.add_widget(self.lang_grid)
        
        # Back button
        back_btn = Button(
            text="Back to Main Menu",
            size_hint=(1, 0.12),
            font_size=sp(22),
            background_normal="",
            background_color=(0.5, 0.5, 0.5, 1)
        )
        back_btn.bind(on_release=lambda *_: self.return_to_menu())
        
        main_layout.add_widget(title)
        main_layout.add_widget(scroll)
        main_layout.add_widget(back_btn)
        
        self.add_widget(main_layout)
        Clock.schedule_once(lambda *_: self._sync_to_app(), 0)
    
    def select_language(self, code: str) -> None:
        """Update language selection and return to main menu."""
        # Update visual selection
        for lang_code, btn in self.language_buttons.items():
            if lang_code == code:
                btn.background_color = (0.2, 0.7, 0.3, 1)  # Green highlight
            else:
                btn.background_color = (0.25, 0.35, 0.55, 1)  # Normal
        
        self.selected_language = code
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.set_target_language(code)
        
        # Return to main menu after brief delay
        Clock.schedule_once(lambda *_: self.return_to_menu(), 0.3)
    
    def return_to_menu(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.show_mode_selection()
    
    def _sync_to_app(self) -> None:
        """Sync display with app's current language."""
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        code = getattr(app, "target_language", next(iter(self.languages)))
        self.update_language_display(code)
    
    def update_language_display(self, language_code: str) -> None:
        """Update button highlight to show current language."""
        if language_code not in self.language_buttons:
            return
        for lang_code, btn in self.language_buttons.items():
            if lang_code == language_code:
                btn.background_color = (0.2, 0.7, 0.3, 1)
            else:
                btn.background_color = (0.25, 0.35, 0.55, 1)
        self.selected_language = language_code


class ModeSelectionScreen(Screen):
    def __init__(self, languages: Dict[str, str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.languages = languages
        
        main_layout = BoxLayout(orientation="vertical", padding=dp(20), spacing=dp(16))
        
        # Title
        title = Label(text="Voice-First Literacy Coach", font_size=sp(36), size_hint=(1, 0.15), color=(1, 1, 1, 1))
        
        # Current language display with native script
        self.lang_display = Label(
            text="",
            font_size=sp(24),
            font_name='NotoSans',
            size_hint=(1, 0.10),
            color=(0.8, 0.9, 1, 1)
        )
        
        # Action buttons
        select_lang_btn = Button(
            text="Select Language",
            size_hint=(1, 0.13),
            font_size=sp(22),
            background_normal="",
            background_color=(0.2, 0.6, 0.9, 1)
        )
        mode_a_btn = Button(text="Start 1000 Words", size_hint=(1, 0.13), font_size=sp(22), background_normal="", background_color=(0.18, 0.5, 0.82, 1))
        mode_b_btn = Button(text="Load ePub Story", size_hint=(1, 0.13), font_size=sp(22), background_normal="", background_color=(0.26, 0.67, 0.5, 1))
        dashboard_btn = Button(text="Dashboard", size_hint=(1, 0.12), font_size=sp(20), background_normal="", background_color=(0.35, 0.6, 0.9, 1))
        settings_btn = Button(text="Settings & Models", size_hint=(1, 0.12), font_size=sp(20), background_normal="", background_color=(0.45, 0.45, 0.78, 1))
        quit_btn = Button(text="Quit", size_hint=(1, 0.12), font_size=sp(18), background_normal="", background_color=(0.7, 0.3, 0.3, 1))
        
        select_lang_btn.bind(on_release=lambda *_: self.select_language())
        mode_a_btn.bind(on_release=lambda *_: self.start_common())
        mode_b_btn.bind(on_release=lambda *_: self.choose_epub())
        dashboard_btn.bind(on_release=lambda *_: self.open_dashboard())
        settings_btn.bind(on_release=lambda *_: self.open_settings())
        quit_btn.bind(on_release=lambda *_: self.quit_app())
        
        # Add all to main layout
        main_layout.add_widget(title)
        main_layout.add_widget(self.lang_display)
        main_layout.add_widget(select_lang_btn)
        main_layout.add_widget(mode_a_btn)
        main_layout.add_widget(mode_b_btn)
        main_layout.add_widget(dashboard_btn)
        main_layout.add_widget(settings_btn)
        main_layout.add_widget(quit_btn)
        
        self.add_widget(main_layout)
        Clock.schedule_once(lambda *_: self._sync_to_app(), 0)
    
    def select_language(self) -> None:
        """Open language selection screen."""
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.show_language_selection()

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
    
    def open_dashboard(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.show_dashboard()

    def quit_app(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.stop()

    def _sync_to_app(self) -> None:
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        code = getattr(app, "target_language", next(iter(self.languages)))
        self.update_language_display(code)

    def update_language_display(self, language_code: str) -> None:
        """Update the language display label."""
        if language_code in self.languages:
            label = self.languages[language_code]
            self.lang_display.text = f"Practice Language: {label}"
        else:
            self.lang_display.text = "Practice Language: Not Selected"

    def update_languages(self, languages: Dict[str, str]) -> None:
        """Update available languages."""
        self.languages = languages


# Map language codes to model URL patterns for automatic model switching
LANGUAGE_MODEL_MAP = {
    "en": "en-us",  # English -> US model
    "nl": "nl",     # Dutch
    "de": "de",     # German
    "fr": "fr",     # French
    "es": "es",     # Spanish
    "pt": "pt",     # Portuguese
    "it": "it",     # Italian
    "ca": "ca",     # Catalan
    "cs": "cs",     # Czech
    "pl": "pl",     # Polish
    "ru": "ru",     # Russian
    "uk": "uk",     # Ukrainian
    "zh": "cn",     # Chinese -> cn in Vosk
    "ja": "ja",     # Japanese
    "ko": "ko",     # Korean
    "vi": "vn",     # Vietnamese -> vn in Vosk
    "hi": "hi",     # Hindi
    "tr": "tr",     # Turkish
    "fa": "fa",     # Farsi
    "kk": "kz",     # Kazakh -> kz in Vosk
    "uz": "uz",     # Uzbek
    "ky": "ky",     # Kyrgyz
    "tg": "tg",     # Tajik
    "gu": "gu",     # Gujarati
    "te": "te",     # Telugu
    "eo": "eo",     # Esperanto
}

DOWNLOADABLE_MODELS: List[Dict[str, str]] = [
    # English variants
    {"label": "English (US) Small 0.15", "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip", "size_mb": 40},
    {"label": "English (India) Small 0.4", "url": "https://alphacephei.com/vosk/models/vosk-model-small-en-in-0.4.zip", "size_mb": 36},
    
    # European languages
    {"label": "Dutch Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-nl-0.22.zip", "size_mb": 39},
    {"label": "French Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-fr-0.22.zip", "size_mb": 41},
    {"label": "German Small 0.15", "url": "https://alphacephei.com/vosk/models/vosk-model-small-de-0.15.zip", "size_mb": 45},
    {"label": "Spanish Small 0.42", "url": "https://alphacephei.com/vosk/models/vosk-model-small-es-0.42.zip", "size_mb": 39},
    {"label": "Portuguese Small 0.3", "url": "https://alphacephei.com/vosk/models/vosk-model-small-pt-0.3.zip", "size_mb": 31},
    {"label": "Italian Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip", "size_mb": 48},
    {"label": "Catalan Small 0.4", "url": "https://alphacephei.com/vosk/models/vosk-model-small-ca-0.4.zip", "size_mb": 42},
    {"label": "Czech Small 0.4", "url": "https://alphacephei.com/vosk/models/vosk-model-small-cs-0.4-rhasspy.zip", "size_mb": 44},
    {"label": "Polish Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-pl-0.22.zip", "size_mb": 50},
    
    # Slavic languages
    {"label": "Russian Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip", "size_mb": 45},
    {"label": "Ukrainian Small Nano", "url": "https://alphacephei.com/vosk/models/vosk-model-small-uk-v3-nano.zip", "size_mb": 73},
    {"label": "Ukrainian Small v3", "url": "https://alphacephei.com/vosk/models/vosk-model-small-uk-v3-small.zip", "size_mb": 133},
    
    # Asian languages
    {"label": "Chinese Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip", "size_mb": 42},
    {"label": "Japanese Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-ja-0.22.zip", "size_mb": 48},
    {"label": "Korean Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-ko-0.22.zip", "size_mb": 82},
    {"label": "Vietnamese Small 0.4", "url": "https://alphacephei.com/vosk/models/vosk-model-small-vn-0.4.zip", "size_mb": 32},
    {"label": "Hindi Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-hi-0.22.zip", "size_mb": 42},
    
    # Middle Eastern languages
    {"label": "Turkish Small 0.3", "url": "https://alphacephei.com/vosk/models/vosk-model-small-tr-0.3.zip", "size_mb": 35},
    {"label": "Farsi (Persian) Small 0.42", "url": "https://alphacephei.com/vosk/models/vosk-model-small-fa-0.42.zip", "size_mb": 53},
    
    # Central Asian languages
    {"label": "Kazakh Small 0.42", "url": "https://alphacephei.com/vosk/models/vosk-model-small-kz-0.42.zip", "size_mb": 58},
    {"label": "Uzbek Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-uz-0.22.zip", "size_mb": 49},
    {"label": "Kyrgyz Small 0.42", "url": "https://alphacephei.com/vosk/models/vosk-model-small-ky-0.42.zip", "size_mb": 49},
    {"label": "Tajik Small 0.22", "url": "https://alphacephei.com/vosk/models/vosk-model-small-tg-0.22.zip", "size_mb": 50},
    
    # Indian subcontinent languages
    {"label": "Gujarati Small 0.42", "url": "https://alphacephei.com/vosk/models/vosk-model-small-gu-0.42.zip", "size_mb": 100},
    {"label": "Telugu Small 0.42", "url": "https://alphacephei.com/vosk/models/vosk-model-small-te-0.42.zip", "size_mb": 58},
    
    # Other languages
    {"label": "Esperanto Small 0.42", "url": "https://alphacephei.com/vosk/models/vosk-model-small-eo-0.42.zip", "size_mb": 42},
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
                
                # Create horizontal layout with model button and delete button
                row = BoxLayout(orientation="horizontal", size_hint=(1, None), height=dp(64), spacing=dp(8))
                
                button = Button(
                    text=f"{label}\n{path}",
                    size_hint=(0.85, 1),
                    halign="left",
                    valign="middle",
                    background_normal="",
                    background_color=(0.24, 0.58, 0.4, 1) if is_active else (0.21, 0.28, 0.48, 1),
                )
                button.text_size = (Window.width - dp(150), None)
                button.bind(size=lambda inst, _size: setattr(inst, "text_size", (inst.width - dp(24), None)))
                button.padding = (dp(16), dp(12))
                button.bind(on_release=lambda *_btn, target=path: app.set_model_path(target))
                
                delete_btn = Button(
                    text="🗑️",
                    size_hint=(0.15, 1),
                    font_size=sp(24),
                    background_normal="",
                    background_color=(0.8, 0.3, 0.3, 1),
                )
                delete_btn.bind(on_release=lambda *_btn, target=path, model_label=label: self._confirm_delete_model(target, model_label))
                
                row.add_widget(button)
                row.add_widget(delete_btn)
                self.models_layout.add_widget(row)
        download_header = Label(text="Download official models", size_hint=(1, None), height=dp(48), font_size=sp(20), color=(0.82, 0.9, 1, 1), bold=True)
        self.models_layout.add_widget(download_header)
        
        # Group models by category for better organization
        current_category = None
        for spec in DOWNLOADABLE_MODELS:
            # Extract category from label (text before first language name)
            label = spec.get("label", "")
            if "English" in label and current_category != "English":
                current_category = "English"
                cat_label = Label(text="🇬🇧 English Variants", size_hint=(1, None), height=dp(32), font_size=sp(16), color=(0.7, 0.85, 1, 1), halign="left")
                cat_label.bind(size=cat_label.setter("text_size"))
                self.models_layout.add_widget(cat_label)
            elif any(lang in label for lang in ["Dutch", "French", "German", "Spanish", "Portuguese", "Italian", "Catalan", "Czech", "Polish"]) and current_category != "European":
                if "English" not in label:  # Skip if it's English
                    if current_category != "European":
                        current_category = "European"
                        cat_label = Label(text="🇪🇺 European Languages", size_hint=(1, None), height=dp(32), font_size=sp(16), color=(0.7, 0.85, 1, 1), halign="left")
                        cat_label.bind(size=cat_label.setter("text_size"))
                        self.models_layout.add_widget(cat_label)
            elif any(lang in label for lang in ["Russian", "Ukrainian"]) and current_category != "Slavic":
                current_category = "Slavic"
                cat_label = Label(text="🇷🇺 Slavic Languages", size_hint=(1, None), height=dp(32), font_size=sp(16), color=(0.7, 0.85, 1, 1), halign="left")
                cat_label.bind(size=cat_label.setter("text_size"))
                self.models_layout.add_widget(cat_label)
            elif any(lang in label for lang in ["Chinese", "Japanese", "Korean", "Vietnamese", "Hindi"]) and current_category != "Asian":
                current_category = "Asian"
                cat_label = Label(text="🌏 Asian Languages", size_hint=(1, None), height=dp(32), font_size=sp(16), color=(0.7, 0.85, 1, 1), halign="left")
                cat_label.bind(size=cat_label.setter("text_size"))
                self.models_layout.add_widget(cat_label)
            elif any(lang in label for lang in ["Turkish", "Farsi", "Persian"]) and current_category != "MiddleEast":
                current_category = "MiddleEast"
                cat_label = Label(text="🕌 Middle Eastern Languages", size_hint=(1, None), height=dp(32), font_size=sp(16), color=(0.7, 0.85, 1, 1), halign="left")
                cat_label.bind(size=cat_label.setter("text_size"))
                self.models_layout.add_widget(cat_label)
            elif any(lang in label for lang in ["Kazakh", "Uzbek", "Kyrgyz", "Tajik"]) and current_category != "CentralAsian":
                current_category = "CentralAsian"
                cat_label = Label(text="🏔️ Central Asian Languages", size_hint=(1, None), height=dp(32), font_size=sp(16), color=(0.7, 0.85, 1, 1), halign="left")
                cat_label.bind(size=cat_label.setter("text_size"))
                self.models_layout.add_widget(cat_label)
            elif any(lang in label for lang in ["Gujarati", "Telugu"]) and current_category != "IndianSubcontinent":
                current_category = "IndianSubcontinent"
                cat_label = Label(text="🇮🇳 Indian Subcontinent", size_hint=(1, None), height=dp(32), font_size=sp(16), color=(0.7, 0.85, 1, 1), halign="left")
                cat_label.bind(size=cat_label.setter("text_size"))
                self.models_layout.add_widget(cat_label)
            elif "Esperanto" in label and current_category != "Other":
                current_category = "Other"
                cat_label = Label(text="🌍 Other Languages", size_hint=(1, None), height=dp(32), font_size=sp(16), color=(0.7, 0.85, 1, 1), halign="left")
                cat_label.bind(size=cat_label.setter("text_size"))
                self.models_layout.add_widget(cat_label)
            
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
        size_mb = spec.get("size_mb", 45)  # Default to 45MB for small models
        
        # Check network type for large downloads
        if size_mb >= 30:
            network = get_network_type()
            if network == "cellular":
                self._show_cellular_warning(label, url, size_mb)
                return
            elif network == "none":
                self.set_status("No internet connection detected")
                return
        
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.queue_model_download(label, url)

    def _show_cellular_warning(self, label: str, url: str, size_mb: int) -> None:
        """Show warning when downloading on cellular data."""
        content = BoxLayout(orientation="vertical", padding=dp(16), spacing=dp(16))
        message = Label(
            text=f"You are on cellular data!\n\nModel: {label}\nSize: ~{size_mb}MB\n\nThis will use your mobile data. Continue?",
            size_hint=(1, 0.7),
            halign="center",
            valign="middle",
        )
        message.bind(size=message.setter("text_size"))
        content.add_widget(message)
        
        buttons = BoxLayout(orientation="horizontal", size_hint=(1, 0.3), spacing=dp(12))
        cancel_btn = Button(text="Cancel", background_normal="", background_color=(0.4, 0.4, 0.4, 1))
        download_btn = Button(text="Download Anyway", background_normal="", background_color=(0.2, 0.6, 0.9, 1))
        buttons.add_widget(cancel_btn)
        buttons.add_widget(download_btn)
        content.add_widget(buttons)
        
        popup = Popup(
            title="⚠️ Cellular Data Warning",
            content=content,
            size_hint=(0.85, 0.4),
            auto_dismiss=False,
        )
        
        cancel_btn.bind(on_release=popup.dismiss)
        download_btn.bind(on_release=lambda *_: self._confirm_cellular_download(label, url, popup))
        popup.open()

    def _confirm_cellular_download(self, label: str, url: str, popup: Popup) -> None:
        """Proceed with download after cellular warning."""
        popup.dismiss()
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        app.queue_model_download(label, url)

    def _confirm_delete_model(self, path: str, label: str) -> None:
        """Show confirmation dialog before deleting a model."""
        content = BoxLayout(orientation="vertical", padding=dp(16), spacing=dp(16))
        message = Label(
            text=f"Are you sure you want to delete this model?\n\n{label}\n\nThis action cannot be undone.",
            size_hint=(1, 0.7),
            halign="center",
            valign="middle",
        )
        message.bind(size=message.setter("text_size"))
        content.add_widget(message)
        
        buttons = BoxLayout(orientation="horizontal", size_hint=(1, 0.3), spacing=dp(12))
        cancel_btn = Button(text="Cancel", background_normal="", background_color=(0.4, 0.4, 0.4, 1))
        delete_btn = Button(text="Delete", background_normal="", background_color=(0.8, 0.2, 0.2, 1))
        buttons.add_widget(cancel_btn)
        buttons.add_widget(delete_btn)
        content.add_widget(buttons)
        
        popup = Popup(
            title="Confirm Deletion",
            content=content,
            size_hint=(0.85, 0.4),
            auto_dismiss=False,
        )
        
        cancel_btn.bind(on_release=popup.dismiss)
        delete_btn.bind(on_release=lambda *_: self._delete_model(path, popup))
        popup.open()

    def _delete_model(self, path: str, popup: Popup) -> None:
        """Delete the model and refresh the list."""
        app: VoiceFirstApp = App.get_running_app()  # type: ignore
        success = app.delete_model(path)
        popup.dismiss()
        if success:
            self.set_status("Model deleted successfully")
            self.refresh_models()
        else:
            self.set_status("Failed to delete model")


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
    """Request microphone permissions on Android.
    
    Android microphone permissions are handled here.
    Note: Permissions must also be declared in buildozer.spec:
        android.permissions = INTERNET,RECORD_AUDIO,READ_EXTERNAL_STORAGE
    """
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


def get_network_type() -> str:
    """Detect the current network type.
    
    Returns:
        'wifi' - Connected to WiFi
        'cellular' - Connected to cellular/mobile data
        'none' - No connection
        'unknown' - Cannot determine
    """
    if platform == "android":
        try:
            from jnius import autoclass
            Context = autoclass("android.content.Context")
            ConnectivityManager = autoclass("android.net.ConnectivityManager")
            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            
            activity = PythonActivity.mActivity
            connectivity = activity.getSystemService(Context.CONNECTIVITY_SERVICE)
            network_info = connectivity.getActiveNetworkInfo()
            
            if network_info is None or not network_info.isConnected():
                return "none"
            
            network_type = network_info.getType()
            # TYPE_WIFI = 1, TYPE_MOBILE = 0
            if network_type == 1:
                return "wifi"
            elif network_type == 0:
                return "cellular"
            return "unknown"
        except Exception:
            return "unknown"
    elif platform == "ios":
        # iOS network detection would require pyobjus and Reachability
        # For now, return unknown and show warning for all downloads
        return "unknown"
    else:
        # Windows/Linux/Mac - assume WiFi/Ethernet (no warning needed)
        return "wifi"


class VoiceEngine:
    def __init__(self, stream_factory: Optional[Callable[[], Tuple[Optional[object], Optional[Callable[[], None]]]]] = None) -> None:
        self.listener: Optional[AudioListener] = None
        self.stream_factory = stream_factory

    def start(self, model_path: Optional[str], result_queue: queue.Queue) -> bool:
        if not model_path:
            logger.error("Cannot start voice engine: No model path provided")
            return False
        
        logger.info("Starting voice engine...")
        if self.listener:
            return self.listener.start()
        
        self.listener = AudioListener(model_path, result_queue, self.stream_factory)
        success = self.listener.start()
        
        if success:
            logger.info("Voice engine started successfully")
        else:
            logger.error("Voice engine failed to start")
        
        return success

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
        self.config_store: Optional[JsonStore] = None
        # Don't load language in __init__, do it in build() when user_data_dir is ready
        self.target_language = "en" if "en" in self.language_labels else next(iter(self.language_labels))
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
        self._initializing = True  # Flag to prevent saves during initialization
    
    def _load_last_language(self) -> str:
        """Load the last selected language from config, or default to English."""
        try:
            config_path = os.path.join(self.user_data_dir, "config.json")
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    last_lang = config.get('last_language')
                    if last_lang and last_lang in self.language_labels:
                        logger.info(f"Restored last language: {last_lang}")
                        return last_lang
        except Exception as e:
            logger.warning(f"Could not load last language: {e}")
        
        # Default to English if available, otherwise first language
        default = "en" if "en" in self.language_labels else (next(iter(self.language_labels)) if self.language_labels else "en")
        logger.info(f"Using default language: {default}")
        return default
    
    def _save_last_language(self, language: str) -> None:
        """Save the selected language to config for next session."""
        if self._initializing:
            # Don't save during initialization
            return
        try:
            config_path = os.path.join(self.user_data_dir, "config.json")
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            config = {'last_language': language}
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
            logger.info(f"Saved language preference: {language}")
        except Exception as e:
            logger.warning(f"Could not save language preference: {e}")

    def build(self) -> ScreenManager:
        # Register Noto Sans font for Unicode support
        try:
            font_path = os.path.join(os.path.dirname(__file__), 'NotoSans-Regular.ttf')
            if os.path.exists(font_path):
                LabelBase.register(name='NotoSans', fn_regular=font_path)
                logger.info(f"Noto Sans font registered successfully from {font_path}")
            else:
                logger.warning(f"Noto Sans font not found at {font_path}")
        except Exception as e:
            logger.error(f"Failed to register Noto Sans font: {e}")
        
        # Load saved language preference now that user_data_dir is available
        self.target_language = self._load_last_language()
        
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
        language_screen = LanguageSelectionScreen(name="language", languages=self.language_labels)
        training_screen = TrainingScreen(name="training")
        dashboard_screen = DashboardScreen(name="dashboard")
        settings_screen = ModelSettingsScreen(name="settings")
        self.manager.add_widget(mode_screen)
        self.manager.add_widget(language_screen)
        self.manager.add_widget(training_screen)
        self.manager.add_widget(dashboard_screen)
        self.manager.add_widget(settings_screen)
        self.update_language_ui()
        self._initializing = False  # Initialization complete, allow saves
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
    def language_screen(self) -> LanguageSelectionScreen:
        return self.manager.get_screen("language")  # type: ignore

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

    def _extract_model_name_from_url(self, url: str) -> str:
        """Extract the model name from a download URL."""
        filename = url.split("/")[-1]
        if filename.endswith(".zip"):
            return filename[:-4]
        return filename

    def _is_model_cached(self, model_name: str) -> bool:
        """Check if a model with the given name already exists in cache."""
        models_dir = self._models_dir()
        for entry in models_dir.iterdir():
            if entry.is_dir() and entry.name == model_name:
                return True
        # Also check legacy location
        legacy_dir = Path(self.user_data_dir) / model_name
        if legacy_dir.is_dir():
            return True
        return False

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

    def delete_model(self, path: str) -> bool:
        """Delete a model from disk and update registry.
        
        Args:
            path: Absolute path to the model directory
            
        Returns:
            True if deletion was successful, False otherwise
        """
        try:
            target = Path(path)
            if not target.exists():
                return False
            
            # Don't allow deleting the currently active model
            if self.model_path and os.path.abspath(path) == os.path.abspath(self.model_path):
                # Switch to another model if available
                models = self.list_vosk_models()
                for model in models:
                    other_path = model.get("path")
                    if other_path and os.path.abspath(other_path) != os.path.abspath(path):
                        self.model_path = other_path
                        self.model_registry["active_model"] = other_path
                        break
                else:
                    # No other models available
                    self.model_path = None
                    self.model_registry["active_model"] = None
                    self.voice_engine.stop()
            
            # Remove from registry
            if str(target) in self.model_registry.get("models", {}):
                del self.model_registry["models"][str(target)]
            
            # Delete the directory
            if target.is_dir():
                shutil.rmtree(target)
            
            self._save_model_registry()
            return True
        except Exception:
            return False

    def queue_model_download(self, label: str, url: str) -> None:
        if self._download_thread and self._download_thread.is_alive():
            self._notify_model_status("Another download is already running")
            return

        # Check if model is already cached
        model_name = self._extract_model_name_from_url(url)
        if self._is_model_cached(model_name):
            self._notify_model_status(f"{label} is already downloaded (cached)")
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
    
    def show_language_selection(self) -> None:
        self.manager.current = "language"

    def set_target_language(self, language: str) -> None:
        available = self.loader.available_languages()
        if language not in available:
            return
        if language == self.target_language:
            return
        self.language_labels = available
        self.target_language = language
        self._save_last_language(language)
        self.update_language_ui()
        
        # Auto-switch to appropriate model for this language
        self._switch_model_for_language(language)

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
        
        logger.info(f"Starting training session - Mode: {mode}, Language: {self.target_language}")
        if not self.voice_engine.start(self.model_path, self.audio_queue):
            error_msg = "Speech engine unavailable; using manual mode"
            logger.warning(error_msg)
            logger.info("Check voicefirst_app.log for detailed error information")
            self.training_screen.feedback_label.text = error_msg

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

    def _switch_model_for_language(self, language: str) -> None:
        """Automatically switch to or suggest downloading model for language."""
        model_code = LANGUAGE_MODEL_MAP.get(language)
        if not model_code:
            logger.info(f"No model mapping found for language: {language}")
            return
        
        # Check if we already have a suitable model downloaded
        models = self.list_vosk_models()
        for model in models:
            model_path = model.get("path", "")
            # Check if model path contains the language code (e.g., "vosk-model-small-nl")
            if f"-{model_code}-" in model_path or model_path.endswith(f"-{model_code}"):
                logger.info(f"Switching to model for {language}: {model_path}")
                self.set_model_path(model_path)
                return
        
        # No suitable model found - log that user should download one
        logger.info(f"No {language} model found. User should download from Settings.")
        # Could optionally show a popup suggesting to download the model
    
    def show_dashboard(self) -> None:
        self.dashboard_screen.refresh()
        self.manager.current = "dashboard"

    def on_stop(self) -> None:
        self.voice_engine.stop()

    def transition_back(self) -> None:
        # If we have an active training session, go back to training
        # Otherwise, go back to the mode selection screen
        if self.session:
            self.manager.current = "training"
        else:
            self.manager.current = "mode"

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
        try:
            self.language_screen.update_language_display(self.target_language)
        except Exception:
            pass


if __name__ == "__main__":
    VoiceFirstApp().run()

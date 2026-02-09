# Packaging Guide

## Android (Buildozer)

1. Install buildozer and Android platform deps.
2. Place `vosk-model-small-en-us.zip` inside `models/`.
3. Update `buildozer.spec` identifiers (`package.domain`, `version`, signing keys).
4. Run `buildozer android debug` or `buildozer android release`. The hook in `scripts/copy_vosk_model.py` copies the Vosk model into the APK assets.
5. On first launch the app extracts the model via `prepare_vosk_model`.

## iOS (kivy-ios)

1. Install `kivy-ios` and create a new toolchain: `toolchain build python3 kivy vosk`.
2. Copy `vosk-model-small-en-us.zip` into `resources/models/` before packaging.
3. Add the same zip to the Xcode project’s bundle resources; ensure `NSMicrophoneUsageDescription` matches `buildozer.spec`.
4. Use `toolchain create voicefirstcoach ../path/to/project main.py` and open the generated Xcode project for signing.
5. The app copies the bundled model to the sandbox at runtime.

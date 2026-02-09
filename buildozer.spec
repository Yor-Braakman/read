[app]
title = VoiceFirst Literacy Coach
package.name = voicefirstcoach
package.domain = org.example.voicecoach
source.dir = .
source.include_exts = py,kv,txt,md,zip,json
source.include_patterns = models/*.zip,words_data.py,docs/**,scripts/**
version = 0.1.0
requirements = python3,kivy,vosk,pyjnius,requests,wordfreq,beautifulsoup4,ebooklib,lxml
presplash.filename = assets/presplash.png
icon.filename = assets/icon.png
android.permissions = RECORD_AUDIO,READ_EXTERNAL_STORAGE
android.features = android.hardware.microphone
android.archs = arm64-v8a,armeabi-v7a
android.minapi = 29
android.sdk = 31
android.ndk = 25b
android.white_list = models/*
android.entrypoint = main.py
android.log_level = 2
android.allow_backup = False
android.add_src = src
android.add_jars =
p4a.hook = scripts/copy_vosk_model.py

[buildozer]
log_level = 2
warn_on_root = 1

[app:android]
android.gradle_dependencies = com.github.wendykierp:JTransforms:3.1

[app:ios]
ios.kivy_ios_url = https://github.com/kivy/kivy-ios.git
ios.kivy_ios_branch = master
ios.plist_extra_entries = <key>NSMicrophoneUsageDescription</key><string>We need microphone access to assess reading fluency.</string>
ios.signing_identity = iPhone Developer
ios.team_id = YOUR_TEAM_ID_HERE

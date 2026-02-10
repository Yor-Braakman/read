# Microphone Permissions

## Overview

The Voice-First Literacy Coach app requires microphone access to enable speech recognition for pronunciation practice. This document explains how microphone permissions are handled on each platform.

## Android

### ✅ Handled Automatically

Microphone permissions are **fully implemented** for Android.

### Implementation Details

1. **Permission Declaration** (buildozer.spec):
```ini
android.permissions = INTERNET,RECORD_AUDIO,READ_EXTERNAL_STORAGE
```

2. **Runtime Permission Request** (main.py):
```python
def ensure_android_permissions() -> None:
    if platform != "android":
        return
    try:
        from android.permissions import Permission, request_permissions
        request_permissions([Permission.RECORD_AUDIO, Permission.READ_EXTERNAL_STORAGE])
    except Exception:
        pass
```

3. **When Permissions Are Requested**:
   - First time user starts a training session
   - Android shows system permission dialog
   - User can grant or deny

### User Experience
- App requests permission on first use
- User sees standard Android permission dialog
- If denied, app continues in "manual mode" (reading only)

## iOS

### ⚠️ Partially Implemented

Microphone permissions need additional configuration for iOS deployment.

### Requirements

1. **Info.plist Configuration**:
Add this key to your iOS project's Info.plist:
```xml
<key>NSMicrophoneUsageDescription</key>
<string>This app needs microphone access to help you practice pronunciation while learning to read.</string>
```

2. **Kivy-iOS Build**:
When building with kivy-ios or similar tools, ensure the microphone capability is enabled.

### Implementation Status
- ✅ PyAudio integration works on iOS (when configured)
- ⚠️ Automatic permission request not yet implemented
- ⚠️ Need to add platform-specific permission prompt code

### TODO for iOS
```python
# Add to main.py for iOS support
def request_ios_microphone_permission():
    if platform == "ios":
        try:
            from pyobjus import autoclass
            AVAudioSession = autoclass('AVAudioSession')
            session = AVAudioSession.sharedInstance()
            session.requestRecordPermission_(lambda granted: None)
        except ImportError:
            pass
```

## Windows

### ✅ Handled by System

Windows microphone permissions are managed at the system level.

### How It Works

1. **First Launch**:
   - Windows shows a system permission dialog when app first accesses microphone
   - User can grant or deny through Windows Settings

2. **Subsequent Launches**:
   - Permission persists based on user's choice
   - User can manage through: Settings → Privacy → Microphone

### User Experience
- Standard Windows microphone permission flow
- No app-specific code needed
- PyAudio automatically triggers system permission dialog

### System Requirements
- Windows 10/11 with microphone privacy controls
- PyAudio library properly installed
- Microphone device available

## macOS

### ✅ Handled by System

Similar to Windows, macOS handles microphone permissions at the system level.

### How It Works

1. **First Access**:
   - macOS shows permission dialog when app accesses microphone
   - User grants or denies

2. **Management**:
   - Settings → Privacy & Security → Microphone
   - Toggle permission for the app

### User Experience
- Standard macOS permission flow
- No additional configuration needed
- Works with PyAudio out of the box

## Linux

### ✅ No Permission System

Linux desktop distributions generally don't have permission systems for microphone access.

### Requirements

- ALSA or PulseAudio properly configured
- User has access to audio devices
- PyAudio installed with appropriate backend

## Fallback Behavior

### Manual Mode

If microphone permissions are denied or unavailable:

1. **App continues to work** for reading practice
2. **No speech recognition** is performed
3. **User sees message**: "Speech engine unavailable; using manual mode"
4. **Progress tracking** still works normally

### Detection

```python
if not self.voice_engine.start(self.model_path, self.audio_queue):
    self.training_screen.feedback_label.text = "Speech engine unavailable; using manual mode"
```

## Testing Permissions

### Android Testing
```bash
# Grant permission manually via ADB
adb shell pm grant com.yourapp.name android.permission.RECORD_AUDIO

# Revoke to test denial
adb shell pm revoke com.yourapp.name android.permission.RECORD_AUDIO
```

### iOS Testing
- Use iOS Simulator settings to toggle microphone access
- Test on physical device with Settings → Privacy → Microphone

### Windows Testing
- Settings → Privacy → Microphone → Toggle app permission
- Test with permission granted and denied

## Best Practices

### For Users

1. **Grant microphone permission** for full functionality
2. **Check system settings** if speech recognition isn't working
3. **Deny permission** if you only want to practice reading (no pronunciation)

### For Developers

1. **Always handle permission denial gracefully**
2. **Provide clear feedback** when permissions are missing
3. **Test on all target platforms** before release
4. **Document fallback behavior** for users

## Network Permissions

### Download Warning System

The app also includes a **cellular data warning** system for model downloads:

- ⚠️ Warns users when downloading on cellular/mobile data
- ✅ Detects WiFi vs. cellular connection on Android
- 📊 Shows model size before download
- 🔄 Allows users to proceed or cancel

Only downloads ≥30MB trigger the cellular warning.

## Summary

| Platform | Status | Implementation |
|----------|--------|----------------|
| Android | ✅ Complete | Runtime permission request |
| iOS | ⚠️ Needs Config | Info.plist + pyobjus |
| Windows | ✅ Complete | System-level permissions |
| macOS | ✅ Complete | System-level permissions |
| Linux | ✅ Complete | No permission system |

## Related Files

- `main.py` - Permission handling code
- `buildozer.spec` - Android permission declarations
- `requirements.txt` - PyAudio dependency

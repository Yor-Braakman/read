"""Check progress.json to see saved word states"""
import os
import json
from pathlib import Path

# Find the progress file
progress_file = Path.home() / ".voicefirstapp" / "progress.json"

print(f"Looking for progress at: {progress_file}")
print(f"Exists: {progress_file.exists()}")

if progress_file.exists():
    try:
        with open(progress_file, 'r') as f:
            data = json.load(f)
        
        print(f"\nTotal words tracked: {len(data)}")
        
        # Show first 10 words with mastery > 0
        mastered = [(word, info) for word, info in data.items() if info.get('mastery', 0) > 0]
        mastered.sort(key=lambda x: x[1].get('mastery', 0), reverse=True)
        
        print(f"\nWords with progress (top 10):")
        for word, info in mastered[:10]:
            mastery = info.get('mastery', 0)
            streak = info.get('streak', 0)
            print(f"  {word}: mastery={mastery:.2f}, streak={streak}")
        
        if not mastered:
            print("  No words have been practiced yet")
    except Exception as e:
        print(f"Error reading progress: {e}")
else:
    print("\nNo progress file found yet. Practice some words to create it!")

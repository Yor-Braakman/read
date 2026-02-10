from kivy.app import App
import os

class TestApp(App):
    pass

app = TestApp()
config_path = os.path.join(app.user_data_dir, "config.json")
print(f"Config should be at: {config_path}")
print(f"Exists: {os.path.exists(config_path)}")

if os.path.exists(config_path):
    with open(config_path, 'r') as f:
        print(f"Contents: {f.read()}")

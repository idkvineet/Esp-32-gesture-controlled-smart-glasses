import os
import time
import json
import tempfile
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog

import cv2
import mediapipe as mp
import numpy as np
from PIL import Image, ImageTk
import requests
import speech_recognition as sr
from deep_translator import GoogleTranslator
from gtts import gTTS
import websocket
import pygame

# Optional audio libs
try:
    import sounddevice as sd
except Exception:
    sd = None
try:
    import miniaudio
except Exception:
    miniaudio = None
try:
    import pyaudio
except Exception:
    pyaudio = None

# Config
GESTURE_CONFIG_FILE = "gesture_config.json"
DEFAULT_GESTURE_ACTIONS = {
    'thumbs_up': 'translate',
    'peace': 'repeat',
    'fist': 'stop',
    'open_palm': 'cycle_language',
    'pointing': 'send_msg',
    'ok_sign': 'none',
    'pinch': 'none'
}
AVAILABLE_ACTIONS = [
    'translate', 'repeat', 'stop', 'cycle_language', 'send_msg',
    'show_text_on_esp32', 'speak_custom_text', 'change_source_language',
    'reset_app', 'none'
]
TRANSLATION_ENGINES = ['GoogleTranslator', 'googletrans', 'GoogleCloud']  # UI choices

class GestureController:
    def __init__(self, root):
        self.root = root
        self.root.title("Gesture Translator — Settings + Engines")
        self.root.geometry("1200x820")
        self.root.minsize(1000,700)
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)

        # MediaPipe
        self.mp_hands = mp.solutions.hands
        self.hands = self.mp_hands.Hands(static_image_mode=False, max_num_hands=1,
                                         min_detection_confidence=0.5, min_tracking_confidence=0.5)
        self.mp_draw = mp.solutions.drawing_utils

        # Audio devices
        self.pyaudio_inst = None
        if pyaudio:
            try:
                self.pyaudio_inst = pyaudio.PyAudio()
            except Exception:
                self.pyaudio_inst = None
        self.available_mics = []
        self.available_speakers = []  # sounddevice device list
        self.selected_mic_index = None
        self.selected_speaker_device = None  # sounddevice index or None

        self.detect_audio_devices()

        # STT/Translator/TTS
        self.recognizer = sr.Recognizer()
        self.last_translation = ""
        self.temp_audio_file = None

        # ESP32 defaults
        self.esp32_ip = "192.168.1.42"
        self.esp32_stream_port = "80"
        self.esp32_ws_port = "81"
        self.display_method = "WebSocket"
        self.display_available = True

        # Language options
        self.languages = {
            'English': 'en', 'Spanish': 'es', 'French': 'fr', 'German': 'de',
            'Italian': 'it', 'Portuguese': 'pt', 'Russian': 'ru', 'Japanese': 'ja',
            'Korean': 'ko', 'Chinese (Simplified)': 'zh-CN', 'Arabic': 'ar',
            'Hindi': 'hi', 'Dutch': 'nl', 'Turkish': 'tr', 'Polish': 'pl', 'Swedish': 'sv'
        }
        self.source_lang = 'en'
        self.target_lang = 'es'

        # Gesture mapping
        self.gesture_actions = self.load_gesture_config()

        # Gesture detection state
        self.current_gesture = "None"
        self.last_gesture_time = 0
        self.gesture_cooldown = 1.0

        # Threads/queues
        self.is_running = False
        self.frame_queue = queue.Queue(maxsize=1)

        # Playback fallback: pygame
        pygame.mixer.init()

        # Default engines
        self.selected_translation_engine = 'GoogleTranslator'
        self.selected_stt_model = 'Google'
        self.selected_tts_engine = 'gTTS'

        # Build GUI
        self.create_gui()

    # ---------------- audio device discovery ----------------
    def detect_audio_devices(self):
        # microphones via pyaudio
        self.available_mics = []
        self.available_speakers = []
        if self.pyaudio_inst:
            try:
                count = self.pyaudio_inst.get_device_count()
                default_input = None
                try:
                    default_input = self.pyaudio_inst.get_default_input_device_info()['index']
                except:
                    default_input = None
                for i in range(count):
                    try:
                        info = self.pyaudio_inst.get_device_info_by_index(i)
                        if info.get('maxInputChannels', 0) > 0:
                            self.available_mics.append({'index': i, 'name': info.get('name', f'Device {i}'), 'default': (i==default_input)})
                    except Exception:
                        pass
                self.selected_mic_index = default_input
            except Exception:
                self.available_mics = []
        # sounddevice devices for speaker selection
        if sd:
            try:
                devs = sd.query_devices()
                for i, d in enumerate(devs):
                    max_out = d.get('max_output_channels', d.get('max_output_channels', 0))
                    if max_out and max_out > 0:
                        self.available_speakers.append({'index': i, 'name': d.get('name', f'Device {i}')})
                try:
                    self.selected_speaker_device = sd.default.device[1]
                except:
                    self.selected_speaker_device = None
            except Exception:
                self.available_speakers = []

    # ---------------- gesture config persistence ----------------
    def load_gesture_config(self):
        if os.path.exists(GESTURE_CONFIG_FILE):
            try:
                with open(GESTURE_CONFIG_FILE, 'r') as f:
                    cfg = json.load(f)
                for k in DEFAULT_GESTURE_ACTIONS.keys():
                    if k not in cfg:
                        cfg[k] = DEFAULT_GESTURE_ACTIONS[k]
                return cfg
            except Exception as e:
                print("Failed to load gesture config:", e)
        return DEFAULT_GESTURE_ACTIONS.copy()

    def save_gesture_config(self):
        try:
            with open(GESTURE_CONFIG_FILE, 'w') as f:
                json.dump(self.gesture_actions, f, indent=2)
            self.log("💾 Gesture config saved")
        except Exception as e:
            self.log(f"❌ Failed to save gesture config: {e}")

    def reset_gesture_config(self):
        self.gesture_actions = DEFAULT_GESTURE_ACTIONS.copy()
        if hasattr(self, 'gesture_vars'):
            for k, v in self.gesture_vars.items():
                v.set(self.gesture_actions.get(k, 'none'))
        self.save_gesture_config()
        self.log("🔄 Gesture config reset to defaults")

    # ---------------- GUI ----------------
    def create_gui(self):
        main_container = ttk.Frame(self.root)
        main_container.grid(row=0, column=0, sticky="nsew")
        main_container.rowconfigure(0, weight=1)
        main_container.columnconfigure(0, weight=1)

        notebook = ttk.Notebook(main_container)
        notebook.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        # Tabs: keep original tabs and add Settings tab
        self.main_tab = ttk.Frame(notebook)
        notebook.add(self.main_tab, text="📹 Main Control")

        self.manual_tab = ttk.Frame(notebook)
        notebook.add(self.manual_tab, text="🎮 Manual Controls")

        self.esp_tab = ttk.Frame(notebook)
        notebook.add(self.esp_tab, text="⚙️ ESP32 Settings")

        #self.audio_tab = ttk.Frame(notebook)
        #notebook.add(self.audio_tab, text="🎤 Audio Devices")

        self.gesture_tab = ttk.Frame(notebook)
        notebook.add(self.gesture_tab, text="✋ Gesture Actions")

        # NEW Settings tab
        self.settings_tab = ttk.Frame(notebook)
        notebook.add(self.settings_tab, text="⚙ Settings")

        # Build each tab using helper methods
        self.create_main_tab(self.main_tab)
        self.create_manual_tab(self.manual_tab)
        self.create_esp_settings_tab(self.esp_tab)
        #self.create_audio_settings_tab(self.audio_tab)
        self.create_gesture_tab(self.gesture_tab)
        self.create_settings_tab(self.settings_tab)

    # For brevity, reuse UI-building code similar to your original file but keep microphone & speaker moved to Settings.
    def create_main_tab(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)

        # Left Panel
        left_frame = ttk.Frame(parent, padding="10")
        left_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        left_frame.columnconfigure(0, weight=1)

        status_frame = ttk.LabelFrame(left_frame, text="📡 Connection Status", padding="10")
        status_frame.pack(fill="x", pady=5)
        self.start_btn = ttk.Button(status_frame, text="▶ Start Stream", command=self.toggle_stream)
        self.start_btn.pack(fill="x", pady=2)
        self.stream_status = ttk.Label(status_frame, text="Status: Disconnected", foreground="red", font=("Arial", 10, "bold"))
        self.stream_status.pack(pady=2)

        display_frame = ttk.LabelFrame(left_frame, text="📺 Quick Send to Display", padding="10")
        display_frame.pack(fill="x", pady=5)
        ttk.Label(display_frame, text="Message:").pack(anchor="w")
        self.esp_msg_entry = ttk.Entry(display_frame)
        self.esp_msg_entry.pack(fill="x", pady=2)
        ttk.Button(display_frame, text="📤 Send", command=self.send_custom_message).pack(fill="x", pady=2)

        settings_frame = ttk.LabelFrame(left_frame, text="⚡ Gesture Settings", padding="10")
        settings_frame.pack(fill="x", pady=5)
        ttk.Label(settings_frame, text="Cooldown:").pack(anchor="w")
        self.cooldown_var = tk.DoubleVar(value=1.0)
        cooldown_scale = ttk.Scale(settings_frame, from_=0.3, to=2.0, variable=self.cooldown_var,
                                    orient="horizontal", command=self.update_cooldown)
        cooldown_scale.pack(fill="x")
        self.cooldown_label = ttk.Label(settings_frame, text="1.0s", font=("Arial", 9))
        self.cooldown_label.pack()

        lang_frame = ttk.LabelFrame(left_frame, text="🌐 Translation", padding="10")
        lang_frame.pack(fill="x", pady=5)
        ttk.Label(lang_frame, text="Source:").pack(anchor="w")
        self.source_lang_var = tk.StringVar(value='English')
        self.source_combo = ttk.Combobox(lang_frame, textvariable=self.source_lang_var,
                                        values=list(self.languages.keys()), state="readonly")
        self.source_combo.pack(fill="x", pady=2)
        self.source_combo.bind('<<ComboboxSelected>>', self.update_source_lang)
        ttk.Label(lang_frame, text="Target:").pack(anchor="w")
        self.target_lang_var = tk.StringVar(value='Spanish')
        self.target_combo = ttk.Combobox(lang_frame, textvariable=self.target_lang_var,
                                        values=list(self.languages.keys()), state="readonly")
        self.target_combo.pack(fill="x", pady=2)
        self.target_combo.bind('<<ComboboxSelected>>', self.update_target_lang)

        # STT Model chooser (mirror of settings selection)
        model_frame = ttk.LabelFrame(left_frame, text="🧠 Speech-to-Text Model", padding="10")
        model_frame.pack(fill="x", pady=5)
        self.stt_model_var_main = tk.StringVar(value=self.selected_stt_model)
        self.stt_combo_main = ttk.Combobox(model_frame, textvariable=self.stt_model_var_main,
                                      values=['Google'], state="readonly")
        self.stt_combo_main.pack(fill="x", pady=2)

        # Right panel: video feed + status (unchanged)
        right_frame = ttk.Frame(parent)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        right_frame.rowconfigure(0, weight=3)
        right_frame.rowconfigure(1, weight=1)
        right_frame.columnconfigure(0, weight=1)

        video_frame = ttk.LabelFrame(right_frame, text="📹 ESP32-CAM Live Feed", padding="5")
        video_frame.grid(row=0, column=0, sticky="nsew", pady=5)
        video_frame.rowconfigure(0, weight=1)
        video_frame.columnconfigure(0, weight=1)
        self.video_label = ttk.Label(video_frame, text="📷 Connect to ESP32-CAM to start",
                                    anchor="center", background="black", foreground="white",
                                    font=("Arial", 12))
        self.video_label.grid(row=0, column=0, sticky="nsew")

        bottom_frame = ttk.Frame(right_frame)
        bottom_frame.grid(row=1, column=0, sticky="nsew")
        bottom_frame.rowconfigure(0, weight=1)
        bottom_frame.columnconfigure(0, weight=1)

        status_info = ttk.LabelFrame(bottom_frame, text="📊 Current Status", padding="10")
        status_info.grid(row=0, column=0, sticky="nsew", padx=5)
        self.gesture_label = ttk.Label(status_info, text="Gesture: None", font=("Arial", 16, "bold"))
        self.gesture_label.pack(anchor="w", pady=5)
        self.action_label = ttk.Label(status_info, text="Action: None", font=("Arial", 12))
        self.action_label.pack(anchor="w", pady=2)
        self.translation_label = ttk.Label(status_info, text="", wraplength=400, font=("Arial", 10))
        self.translation_label.pack(anchor="w", fill="x", pady=2)

        log_frame = ttk.LabelFrame(bottom_frame, text="📝 Activity Log", padding="5")
        log_frame.grid(row=0, column=1, sticky="nsew", padx=5)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, wrap=tk.WORD, font=("Consolas", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        ttk.Button(log_frame, text="🗑️ Clear", command=self.clear_log).grid(row=1, column=0, sticky="ew", pady=2)

    def create_manual_tab(self, parent):
        canvas = tk.Canvas(parent)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        center_frame = ttk.Frame(scrollable_frame)
        center_frame.pack(expand=True, padx=20, pady=20)

        ttk.Label(center_frame, text="🎮 Manual Controls",
                font=("Arial", 16, "bold")).pack(pady=10)
        trans_frame = ttk.LabelFrame(center_frame, text="🎤 Translation Controls", padding="20")
        trans_frame.pack(fill="x", pady=10)
        ttk.Button(trans_frame, text="🎤 Listen & Translate",
                command=lambda: threading.Thread(target=self.translate_and_speak, daemon=True).start(),
                width=30).pack(pady=5)
        ttk.Button(trans_frame, text="🔁 Repeat Last Translation",
                command=lambda: threading.Thread(target=self.repeat_last_translation, daemon=True).start(),
                width=30).pack(pady=5)
        ttk.Button(trans_frame, text="⏹️ Stop Audio",
                command=self.stop_audio,
                width=30).pack(pady=5)

        lang_frame = ttk.LabelFrame(center_frame, text="🌐 Language Controls", padding="20")
        lang_frame.pack(fill="x", pady=10)
        ttk.Button(lang_frame, text="⏭️ Next Target Language",
                command=self.cycle_target_language,
                width=30).pack(pady=5)

        disp_frame = ttk.LabelFrame(center_frame, text="📺 Display Controls", padding="20")
        disp_frame.pack(fill="x", pady=10)
        ttk.Label(disp_frame, text="Custom Message:").pack(anchor="w")
        self.manual_msg_entry = ttk.Entry(disp_frame, width=35)
        self.manual_msg_entry.pack(pady=5)
        ttk.Button(disp_frame, text="📤 Send to ESP32 Display",
                command=lambda: self.send_to_esp32_display(self.manual_msg_entry.get()),
                width=30).pack(pady=5)

        status_frame = ttk.LabelFrame(center_frame, text="ℹ️ Current Settings", padding="20")
        status_frame.pack(fill="x", pady=10)
        self.manual_status = ttk.Label(status_frame, text="", font=("Arial", 10), justify="left")
        self.manual_status.pack()
        self.update_manual_status()

    def create_esp_settings_tab(self, parent):
        canvas = tk.Canvas(parent)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        center_frame = ttk.Frame(scrollable_frame)
        center_frame.pack(expand=True, padx=20, pady=20)

        conn_frame = ttk.LabelFrame(center_frame, text="📡 Connection Settings", padding="20")
        conn_frame.pack(fill="both", pady=10)
        ttk.Label(conn_frame, text="ESP32 IP:", font=("Arial", 10, "bold")).grid(row=0, column=0, sticky="w", pady=5)
        self.ip_entry = ttk.Entry(conn_frame, width=30)
        self.ip_entry.insert(0, self.esp32_ip)
        self.ip_entry.grid(row=0, column=1, pady=5, padx=10)
        ttk.Label(conn_frame, text="Stream Port:", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="w", pady=5)
        self.stream_port_entry = ttk.Entry(conn_frame, width=30)
        self.stream_port_entry.insert(0, self.esp32_stream_port)
        self.stream_port_entry.grid(row=1, column=1, pady=5, padx=10)
        ttk.Label(conn_frame, text="WebSocket Port:", font=("Arial", 10, "bold")).grid(row=2, column=0, sticky="w", pady=5)
        self.ws_port_entry = ttk.Entry(conn_frame, width=30)
        self.ws_port_entry.insert(0, self.esp32_ws_port)
        self.ws_port_entry.grid(row=2, column=1, pady=5, padx=10)

        ttk.Label(conn_frame, text="Display Method:", font=("Arial", 10, "bold")).grid(row=3, column=0, sticky="w", pady=5)
        self.display_method_var = tk.StringVar(value=self.display_method)
        self.display_method_combo = ttk.Combobox(conn_frame, textvariable=self.display_method_var, values=["WebSocket", "HTTP"], state="readonly")
        self.display_method_combo.grid(row=3, column=1, pady=5, padx=10)

        btn_frame = ttk.Frame(conn_frame)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="💾 Update", command=self.update_esp32_settings).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🔍 Test", command=self.test_connection).pack(side="left", padx=5)

        wifi_frame = ttk.LabelFrame(center_frame, text="📶 Update ESP32 WiFi", padding="20")
        wifi_frame.pack(fill="both", pady=10)
        ttk.Label(wifi_frame, text="New SSID:", font=("Arial", 10, "bold")).grid(row=0, column=0, sticky="w", pady=5)
        self.new_ssid_entry = ttk.Entry(wifi_frame, width=30)
        self.new_ssid_entry.grid(row=0, column=1, pady=5, padx=10)
        ttk.Label(wifi_frame, text="New Password:", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="w", pady=5)
        self.new_password_entry = ttk.Entry(wifi_frame, width=30, show="*")
        self.new_password_entry.grid(row=1, column=1, pady=5, padx=10)
        self.show_password_var = tk.BooleanVar()
        ttk.Checkbutton(wifi_frame, text="👁️ Show Password", variable=self.show_password_var, command=self.toggle_password_visibility).grid(row=2, column=1, sticky="w", padx=10)
        ttk.Button(wifi_frame, text="📡 Send to ESP32", command=self.send_wifi_credentials).grid(row=3, column=0, columnspan=2, pady=10)
        ttk.Label(wifi_frame, text="⚠️ ESP32 must have /update_wifi endpoint", font=("Arial", 8), foreground="gray").grid(row=4, column=0, columnspan=2)

        display_frame = ttk.LabelFrame(center_frame, text="📺 I2C Display Control", padding="20")
        display_frame.pack(fill="both", pady=10)
        ttk.Label(display_frame, text="Line 1:", font=("Arial", 10, "bold")).grid(row=0, column=0, sticky="w", pady=5)
        self.display_line1_entry = ttk.Entry(display_frame, width=30)
        self.display_line1_entry.grid(row=0, column=1, pady=5, padx=10)
        ttk.Label(display_frame, text="Line 2:", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="w", pady=5)
        self.display_line2_entry = ttk.Entry(display_frame, width=30)
        self.display_line2_entry.grid(row=1, column=1, pady=5, padx=10)
        ttk.Button(display_frame, text="📤 Update Display", command=self.send_to_i2c_display).grid(row=2, column=0, columnspan=2, pady=10)

    def create_audio_settings_tab(self, parent):
        # kept for backward compatibility, but we moved mic/speaker to Settings
        frame = ttk.Frame(parent, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Audio Devices (moved to Settings)", font=("Arial", 11)).pack(pady=20)

    def create_gesture_tab(self, parent):
        frm = ttk.Frame(parent, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Configure gesture actions (saved to gesture_config.json):", font=("Arial", 11)).grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
        gestures = list(DEFAULT_GESTURE_ACTIONS.keys())
        self.gesture_vars = {}
        row = 1
        for g in gestures:
            ttk.Label(frm, text=g.replace('_', ' ').title() + ":", width=18).grid(row=row, column=0, sticky="w", pady=6)
            var = tk.StringVar(value=self.gesture_actions.get(g, 'none'))
            cb = ttk.Combobox(frm, textvariable=var, values=AVAILABLE_ACTIONS, state="readonly")
            cb.grid(row=row, column=1, sticky="ew", pady=6)
            cb.bind("<<ComboboxSelected>>", lambda e, gesture=g, var=var: self.update_gesture_mapping(gesture, var.get()))
            self.gesture_vars[g] = var
            row += 1
        btn_frame = ttk.Frame(frm)
        btn_frame.grid(row=row, column=0, columnspan=3, pady=10, sticky="ew")
        ttk.Button(btn_frame, text="💾 Save", command=self.save_gesture_config).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="🔄 Reset Defaults", command=self.reset_gesture_config).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="📥 Reload", command=lambda: (self.gesture_actions.update(self.load_gesture_config()), [v.set(self.gesture_actions.get(k)) for k, v in self.gesture_vars.items()], self.log("🔃 Reloaded gesture config"))).pack(side="left", padx=6)

    def create_settings_tab(self, parent):
        frm = ttk.Frame(parent, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        # Translation engine selector
        ttk.Label(frm, text="Translation Engine:", font=("Arial", 10, "bold")).grid(row=0, column=0, sticky="w", pady=6)
        self.translation_engine_var = tk.StringVar(value=self.selected_translation_engine)
        self.translation_engine_combo = ttk.Combobox(frm, textvariable=self.translation_engine_var, values=TRANSLATION_ENGINES, state="readonly")
        self.translation_engine_combo.grid(row=0, column=1, sticky="ew", pady=6)
        self.translation_engine_combo.bind("<<ComboboxSelected>>", self.update_translation_engine)

        # STT selector
        ttk.Label(frm, text="STT Model:", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="w", pady=6)
        self.stt_engine_var = tk.StringVar(value=self.selected_stt_model)
        self.stt_engine_combo = ttk.Combobox(frm, textvariable=self.stt_engine_var, values=['Google'], state="readonly")
        self.stt_engine_combo.grid(row=1, column=1, sticky="ew", pady=6)
        self.stt_engine_combo.bind("<<ComboboxSelected>>", self.update_stt_engine)

        # TTS selector
        ttk.Label(frm, text="TTS Engine:", font=("Arial", 10, "bold")).grid(row=2, column=0, sticky="w", pady=6)
        self.tts_engine_var = tk.StringVar(value=self.selected_tts_engine)
        self.tts_engine_combo = ttk.Combobox(frm, textvariable=self.tts_engine_var, values=['gTTS'], state="readonly")
        self.tts_engine_combo.grid(row=2, column=1, sticky="ew", pady=6)
        self.tts_engine_combo.bind("<<ComboboxSelected>>", self.update_tts_engine)

        # Microphone selection
        ttk.Label(frm, text="Microphone (input):", font=("Arial", 10, "bold")).grid(row=3, column=0, sticky="w", pady=6)
        mic_values = [f"{m['index']}: {m['name']}" for m in self.available_mics] or ["System Default"]
        self.settings_mic_var = tk.StringVar(value=mic_values[0] if mic_values else "System Default")
        self.settings_mic_combo = ttk.Combobox(frm, textvariable=self.settings_mic_var, values=mic_values, state="readonly")
        self.settings_mic_combo.grid(row=3, column=1, sticky="ew", pady=6)
        ttk.Button(frm, text="Set Microphone", command=self.apply_mic_selection).grid(row=3, column=2, padx=6)

        # Speaker selection (sounddevice)
        ttk.Label(frm, text="Speaker (output):", font=("Arial", 10, "bold")).grid(row=4, column=0, sticky="w", pady=6)
        if self.available_speakers:
            spk_values = [f"{s['index']}: {s['name']}" for s in self.available_speakers]
        else:
            spk_values = ["System Default (pygame)"]
        self.settings_spk_var = tk.StringVar(value=spk_values[0])
        self.settings_spk_combo = ttk.Combobox(frm, textvariable=self.settings_spk_var, values=spk_values, state="readonly")
        self.settings_spk_combo.grid(row=4, column=1, sticky="ew", pady=6)
        ttk.Button(frm, text="Set Speaker", command=self.apply_speaker_selection).grid(row=4, column=2, padx=6)

        # Save settings button
        btn_frame = ttk.Frame(frm)
        btn_frame.grid(row=5, column=0, columnspan=3, pady=12)
        ttk.Button(btn_frame, text="💾 Save Settings", command=self.save_settings).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="🔄 Refresh Devices", command=self.refresh_audio_devices).pack(side="left", padx=6)

    # ---------------- settings callbacks ----------------
    def update_translation_engine(self, event=None):
        self.selected_translation_engine = self.translation_engine_var.get()
        self.log(f"🧩 Translation engine set to: {self.selected_translation_engine}")

    def update_stt_engine(self, event=None):
        self.selected_stt_model = self.stt_engine_var.get()
        self.log(f"🧩 STT model set to: {self.selected_stt_model}")

    def update_tts_engine(self, event=None):
        self.selected_tts_engine = self.tts_engine_var.get()
        self.log(f"🧩 TTS engine set to: {self.selected_tts_engine}")

    def apply_mic_selection(self):
        sel = self.settings_mic_var.get()
        if ":" in sel:
            try:
                idx = int(sel.split(":",1)[0].strip())
                self.selected_mic_index = idx
                messagebox.showinfo("Microphone", f"Selected microphone index: {idx}")
                self.log(f"🎤 Microphone set to index {idx}")
            except Exception as e:
                messagebox.showwarning("Microphone", f"Could not set mic: {e}")
        else:
            self.selected_mic_index = None
            messagebox.showinfo("Microphone", "Using system default microphone")
            self.log("🎤 Microphone set to system default")

    def apply_speaker_selection(self):
        sel = self.settings_spk_var.get()
        if ":" in sel:
            try:
                idx = int(sel.split(":",1)[0].strip())
                self.selected_speaker_device = idx
                messagebox.showinfo("Speaker", f"Selected speaker device: {idx}")
                self.log(f"🔊 Speaker set to device {idx}")
            except Exception as e:
                messagebox.showwarning("Speaker", f"Could not set speaker: {e}")
        else:
            self.selected_speaker_device = None
            messagebox.showinfo("Speaker", "Using system default speaker (pygame)")
            self.log("🔊 Speaker set to system default (pygame)")

    def save_settings(self):
        # persist translation engine selection to a small JSON (optional)
        settings = {
            'translation_engine': self.selected_translation_engine,
            'stt_model': self.selected_stt_model,
            'tts_engine': self.selected_tts_engine,
            'mic_index': self.selected_mic_index,
            'speaker_device': self.selected_speaker_device
        }
        try:
            with open("app_settings.json", "w") as f:
                json.dump(settings, f, indent=2)
            messagebox.showinfo("Settings", "Settings saved to app_settings.json")
            self.log("💾 Settings saved")
        except Exception as e:
            messagebox.showwarning("Settings", f"Failed to save settings: {e}")

    def refresh_audio_devices(self):
        self.detect_audio_devices()
        messagebox.showinfo("Audio", "Audio device lists refreshed. Re-open Settings tab to see updates.")
        self.log("🔄 Audio devices refreshed")

    # ---------------- main functionality ----------------
    def update_manual_status(self):
        status_text = f"Source Language: {self.source_lang_var.get()}\n"
        status_text += f"Target Language: {self.target_lang_var.get()}\n"
        status_text += f"ESP32 IP: {self.esp32_ip}\n"
        status_text += f"Display Available: {'Yes' if self.display_available else 'No'}\n"
        status_text += f"Last Translation: {self.last_translation if self.last_translation else 'None'}"
        try:
            self.manual_status.config(text=status_text)
        except:
            pass

    def toggle_password_visibility(self):
        if self.show_password_var.get():
            self.new_password_entry.config(show="")
        else:
            self.new_password_entry.config(show="*")

    def update_cooldown(self, value):
        self.gesture_cooldown = float(value)
        self.cooldown_label.config(text=f"{self.gesture_cooldown:.1f}s")

    def toggle_stream(self):
        if not self.is_running:
            self.start_stream()
        else:
            self.stop_stream()

    def start_stream(self):
        try:
            test_url = f"http://{self.esp32_ip}:{self.esp32_stream_port}/stream"
            response = requests.head(test_url, timeout=3)
        except Exception as e:
            messagebox.showerror("Connection Error", f"Cannot connect to ESP32 at:\n{test_url}\n\n{e}")
            return
        self.is_running = True
        self.start_btn.config(text="⏹ Stop Stream")
        self.stream_thread = threading.Thread(target=self.process_esp32_stream, daemon=True)
        self.stream_thread.start()
        self.display_thread = threading.Thread(target=self.update_display, daemon=True)
        self.display_thread.start()
        self.log("▶ Stream started")

    def stop_stream(self):
        self.is_running = False
        self.start_btn.config(text="▶ Start Stream")
        self.log("⏹ Stream stopped")

    def process_esp32_stream(self):
        try:
            stream_url = f"http://{self.esp32_ip}:{self.esp32_stream_port}/stream"
            stream = requests.get(stream_url, stream=True, timeout=10)
            bytes_data = bytes()
            for chunk in stream.iter_content(chunk_size=2048):
                if not self.is_running:
                    break
                bytes_data += chunk
                a = bytes_data.find(b'\xff\xd8')
                b = bytes_data.find(b'\xff\xd9')
                if a != -1 and b != -1:
                    jpg = bytes_data[a:b+2]
                    bytes_data = bytes_data[b+2:]
                    frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    small = cv2.resize(frame, (320,240))
                    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                    results = self.hands.process(rgb)
                    gesture = "None"
                    if results.multi_hand_landmarks:
                        for hand_landmarks in results.multi_hand_landmarks:
                            self.mp_draw.draw_landmarks(small, hand_landmarks, self.mp_hands.HAND_CONNECTIONS)
                            gesture = self.detect_gesture(hand_landmarks.landmark)
                            break
                    now = time.time()
                    if gesture != "None" and (now - self.last_gesture_time) > self.gesture_cooldown:
                        self.last_gesture_time = now
                        self.current_gesture = gesture
                        threading.Thread(target=self.execute_gesture_action, args=(gesture,), daemon=True).start()
                    if self.frame_queue.empty():
                        self.frame_queue.put(small)
        except Exception as e:
            self.log(f"✗ Stream error: {e}")

    def update_display(self):
        while self.is_running:
            try:
                frame = self.frame_queue.get(timeout=0.1)
                h, w = frame.shape[:2]
                img = Image.fromarray(frame)
                imgtk = ImageTk.PhotoImage(image=img)
                self.video_label.imgtk = imgtk
                self.video_label.configure(image=imgtk)
            except queue.Empty:
                pass

    def detect_gesture(self, landmarks):
        thumb_tip = landmarks[4]; index_tip = landmarks[8]; middle_tip = landmarks[12]; ring_tip = landmarks[16]; pinky_tip = landmarks[20]
        index_base = landmarks[5]; middle_base = landmarks[9]; ring_base = landmarks[13]; pinky_base = landmarks[17]
        thumb_up = thumb_tip.y < landmarks[3].y
        index_up = index_tip.y < index_base.y
        middle_up = middle_tip.y < middle_base.y
        ring_up = ring_tip.y < ring_base.y
        pinky_up = pinky_tip.y < pinky_base.y
        fingers_up = sum([index_up, middle_up, ring_up, pinky_up])
        if thumb_up and not index_up and not middle_up and not ring_up and not pinky_up:
            return "thumbs_up"
        if index_up and middle_up and not ring_up and not pinky_up:
            return "peace"
        if fingers_up == 0 and not thumb_up:
            return "fist"
        if fingers_up == 4 and thumb_up:
            return "open_palm"
        if index_up and not middle_up and not ring_up and not pinky_up:
            return "pointing"
        thumb_index_dist = np.sqrt((thumb_tip.x - index_tip.x)**2 + (thumb_tip.y - index_tip.y)**2)
        if thumb_index_dist < 0.05 and middle_up and ring_up:
            return "ok_sign"
        if thumb_index_dist < 0.08 and not middle_up and not ring_up:
            return "pinch"
        return "None"

    def execute_gesture_action(self, gesture):
        action = self.gesture_actions.get(gesture, 'none')
        self.root.after(0, lambda: self.gesture_label.config(text=f"Gesture: {gesture.replace('_',' ').title()}"))
        self.root.after(0, lambda: self.action_label.config(text=f"Action: {action.replace('_',' ').title()}"))
        self.log(f"Gesture detected: {gesture} -> {action}")
        if action == 'none':
            return
        if action == 'translate':
            threading.Thread(target=self.translate_and_speak, daemon=True).start()
        elif action == 'repeat':
            threading.Thread(target=self.repeat_last_translation, daemon=True).start()
        elif action == 'stop':
            self.stop_audio()
        elif action == 'cycle_language':
            self.cycle_target_language()
        elif action == 'send_msg':
            msg = self.esp_msg_entry.get().strip() or (self.last_translation or " ")
            self.send_to_esp32_display(msg[:80])
        elif action == 'show_text_on_esp32':
            msg = self.last_translation or "No translation"
            self.send_to_esp32_display(msg[:80])
        elif action == 'speak_custom_text':
            self.root.after(0, lambda: threading.Thread(target=self._prompt_and_speak_custom, daemon=True).start())
        elif action == 'change_source_language':
            self.root.after(0, lambda: self._prompt_change_source_language())
        elif action == 'reset_app':
            self.reset_application()
        else:
            self.log(f"⚠ Unknown action: {action}")

    def _prompt_and_speak_custom(self):
        text = simpledialog.askstring("Custom Speak", "Enter text to speak:", parent=self.root)
        if text:
            self.log(f"🔊 Speaking custom: {text[:80]}")
            self.speak_text(text, self.target_lang)
            try:
                self.send_to_esp32_display(text[:80])
            except:
                pass

    def _prompt_change_source_language(self):
        options = list(self.languages.keys())
        choice = simpledialog.askstring("Change Source Language", f"Enter source language name (e.g. English):\nAvailable: {', '.join(options)}", parent=self.root)
        if choice and choice in self.languages:
            self.source_lang = self.languages[choice]
            self.source_lang_var.set(choice)
            self.log(f"🗣️ Source changed to {choice}")
        else:
            self.log("⚠ Invalid source change or cancelled")

    def reset_application(self):
        self.stop_stream()
        self.last_translation = ""
        try:
            self.log_text.delete('1.0', tk.END)
        except:
            pass
        self.log("🔁 Application reset")

    # ---------------- translation manager ----------------
    def translate_text(self, text, src_code, tgt_code):
        engine = self.selected_translation_engine
        try:
            if engine == 'GoogleTranslator':
                # deep_translator
                return GoogleTranslator(source=src_code, target=tgt_code).translate(text)
            elif engine == 'googletrans':
                # try googletrans if installed
                try:
                    from googletrans import Translator as GT
                    tr = GT()
                    res = tr.translate(text, src=src_code, dest=tgt_code)
  # googletrans API differences vary
                    return res.text
                except Exception as e:
                    self.log(f"googletrans error: {e}")
                    # fallback to deep_translator
                    return GoogleTranslator(source=src_code, target=tgt_code).translate(text)
            elif engine == 'GoogleCloud':
                # Placeholder - user must supply API key & client config
                self.log("Google Cloud Translate requested but no API configured.")
                return GoogleTranslator(source=src_code, target=tgt_code).translate(text)
            else:
                return GoogleTranslator(source=src_code, target=tgt_code).translate(text)
        except Exception as e:
            self.log(f"Translate error ({engine}): {e}")
            raise

    def translate_and_speak(self):
        self.log("🎤 Listening...")
        try:
            self.send_to_esp32_display("Listening...")
        except:
            pass
        try:
            mic_kwargs = {"device_index": self.selected_mic_index} if self.selected_mic_index is not None else {}
            with sr.Microphone(**mic_kwargs) as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.3)
                audio = self.recognizer.listen(source, timeout=6, phrase_time_limit=12)
            # STT (only Google implemented)
            src_code = self.languages.get(self.source_lang_var.get(), 'en')
            text = self.recognizer.recognize_google(audio, language=src_code)
            self.log(f"Heard: {text}")
            # Translate to target
            tgt_code = self.languages.get(self.target_lang_var.get(), 'es')
            translated_text = self.translate_text(text, src_code, tgt_code)
            self.last_translation = translated_text
            self.root.after(0, lambda: self.translation_label.config(text=f"Original: {text}\nTranslation: {translated_text}"))
            self.log(f"Translated ({tgt_code}): {translated_text}")
            # Speak
            self.speak_text(translated_text, tgt_code)
            try:
                self.send_to_esp32_display(translated_text[:80])
            except:
                pass
        except sr.WaitTimeoutError:
            self.log("⏱ STT timeout")
            try:
                self.send_to_esp32_display("Timeout")
            except:
                pass
        except sr.UnknownValueError:
            self.log("❌ STT could not understand audio")
            try:
                self.send_to_esp32_display("Not understood")
            except:
                pass
        except Exception as e:
            self.log(f"❌ Translate/Listen error: {e}")
            try:
                self.send_to_esp32_display("Error")
            except:
                pass

    # ---------------- playback ----------------
    def speak_text(self, text, lang):
        # Create MP3 via gTTS
        try:
            if self.temp_audio_file and os.path.exists(self.temp_audio_file):
                try: os.remove(self.temp_audio_file)
                except: pass
            self.temp_audio_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp3').name
            tts = gTTS(text=text, lang=lang, slow=False)
            tts.save(self.temp_audio_file)
        except Exception as e:
            self.log(f"gTTS error: {e}")
            return

        # Preferred pathway: miniaudio decode -> sounddevice play to selected device
        if miniaudio and sd and self.selected_speaker_device is not None:
            try:
                dec = miniaudio.DecodedAudioStream(self.temp_audio_file)
                frames = dec.read()
                srate = dec.sample_rate
                channels = dec.channels
                import numpy as np
                pcm = np.frombuffer(frames, dtype=np.int16)
                if channels > 1:
                    pcm = pcm.reshape(-1, channels)
                pcm_f32 = pcm.astype('float32') / 32768.0
                self.log(f"🔊 Playing via sounddevice (device {self.selected_speaker_device})")
                sd.play(pcm_f32, samplerate=srate, device=self.selected_speaker_device)
                sd.wait()
                return
            except Exception as e:
                self.log(f"miniaudio/sounddevice playback failed: {e} - falling back to pygame")

        # If selected_speaker_device is None, but miniaudio+sd available, try default device
        if miniaudio and sd and self.selected_speaker_device is None:
            try:
                dec = miniaudio.DecodedAudioStream(self.temp_audio_file)
                frames = dec.read()
                srate = dec.sample_rate
                channels = dec.channels
                import numpy as np
                pcm = np.frombuffer(frames, dtype=np.int16)
                if channels > 1:
                    pcm = pcm.reshape(-1, channels)
                pcm_f32 = pcm.astype('float32') / 32768.0
                self.log("🔊 Playing via sounddevice (default device)")
                sd.play(pcm_f32, samplerate=srate)
                sd.wait()
                return
            except Exception as e:
                self.log(f"miniaudio/sd default failed: {e}")

        # Fallback: pygame playback (system default)
        try:
            pygame.mixer.music.load(self.temp_audio_file)
            pygame.mixer.music.play()
            self.log("🔊 Playing via pygame fallback (system default speaker)")
        except Exception as e:
            self.log(f"Playback failed (pygame): {e}")

    def repeat_last_translation(self):
        if self.last_translation:
            self.log("🔁 Repeating last translation")
            # We have target lang in target_lang_var
            tgt_code = self.languages.get(self.target_lang_var.get(), 'es')
            self.speak_text(self.last_translation, tgt_code)
            try:
                self.send_to_esp32_display(self.last_translation[:80])
            except:
                pass
        else:
            self.log("⚠ No last translation to repeat")

    def stop_audio(self):
        try:
            if sd:
                try:
                    sd.stop()
                except:
                    pass
            try:
                pygame.mixer.music.stop()
            except:
                pass
        except Exception:
            pass
        self.log("⏹ Audio stopped")

    def cycle_target_language(self):
        keys = list(self.languages.keys())
        current = self.target_lang_var.get()
        try:
            idx = keys.index(current)
            new = keys[(idx + 1) % len(keys)]
        except:
            new = keys[0]
        self.target_lang_var.set(new)
        self.update_target_lang()
        self.log(f"🎯 Target language: {new}")

    def update_source_lang(self, event=None):
        lang_name = self.source_lang_var.get()
        self.source_lang = self.languages.get(lang_name, self.source_lang)
        self.log(f"🗣️ Source set to: {lang_name}")

    def update_target_lang(self, event=None):
        lang_name = self.target_lang_var.get()
        self.target_lang = self.languages.get(lang_name, self.target_lang)
        self.log(f"🎯 Target set to: {lang_name}")

    # ---------------- ESP / display ----------------
    def send_to_i2c_display(self):
        l1 = self.display_line1_entry.get()[:21]
        l2 = self.display_line2_entry.get()[:21]
        if not l1 and not l2:
            messagebox.showwarning("Warning", "Please enter at least one line")
            return
        message = f"{l1}|{l2}"
        self.send_to_esp32_display(message)
        self.log(f"📺 Sent to I2C: '{l1}' | '{l2}'")

    def send_to_esp32_display(self, message):
        if not self.display_available:
            self.log("⚠️ Display not available - continuing normally")
            return
        method = self.display_method_var.get() if hasattr(self, 'display_method_var') else self.display_method
        self.log(f"Sending to display via {method}: {message}")
        if method == "HTTP":
            try:
                url = f"http://{self.esp32_ip}:{self.esp32_stream_port}/display"
                resp = requests.post(url, json={"text": message}, timeout=3)
                if resp.status_code == 200:
                    self.log("✓ Display updated via HTTP")
                else:
                    self.log(f"✗ Display HTTP error {resp.status_code}")
            except Exception as e:
                self.log(f"⚠️ HTTP display error: {e}")
                self.display_available = False
        else:
            try:
                ws_url = f"ws://{self.esp32_ip}:{self.esp32_ws_port}"
                ws = websocket.create_connection(ws_url, timeout=2)
                ws.send(message)
                ws.close()
                self.log(f"✓ Display updated via WS")
            except Exception as e:
                self.log(f"⚠️ Display unavailable (WS): {str(e)}")
                self.display_available = False

    def update_esp32_settings(self):
        self.esp32_ip = self.ip_entry.get()
        self.esp32_stream_port = self.stream_port_entry.get()
        self.esp32_ws_port = self.ws_port_entry.get()
        if hasattr(self, 'display_method_var'):
            self.display_method = self.display_method_var.get()
        self.log(f"⚙️ Updated - IP: {self.esp32_ip}, Stream: {self.esp32_stream_port}, WS: {self.esp32_ws_port}, Method: {self.display_method}")
        messagebox.showinfo("Success", "ESP32 settings updated!")

    def test_connection(self):
        self.log("Testing ESP32 connection...")
        ip = self.ip_entry.get()
        port = self.stream_port_entry.get()
        test_url = f"http://{ip}:{port}/stream"
        self.log(f"Testing: {test_url}")
        try:
            response = requests.head(test_url, timeout=3)
            self.log(f"✓ Response: {response.status_code}")
            response = requests.get(test_url, stream=True, timeout=5)
            chunk = next(response.iter_content(chunk_size=1024))
            self.log(f"✓ Data received ({len(chunk)} bytes)")
            if b'\xff\xd8' in chunk:
                self.log("✓ JPEG stream detected!")
                messagebox.showinfo("Success", "✓ Connection successful!\n\nESP32-CAM streaming correctly.")
            else:
                self.log("⚠ Not JPEG stream")
                messagebox.showwarning("Warning", "Connection OK but format unexpected.")
        except Exception as e:
            self.log(f"✗ Error: {str(e)}")
            messagebox.showerror("Error", f"Test failed:\n{str(e)}")

    def send_wifi_credentials(self):
        ssid = self.new_ssid_entry.get().strip()
        password = self.new_password_entry.get().strip()

        if not ssid or not password:
            messagebox.showwarning("Missing Info", "Please enter both SSID and Password.")
            return

        url = f"http://{self.esp32_ip}:{self.esp32_stream_port}/update_wifi"

        payload = {
            "ssid": ssid,
            "password": password
        }

        try:
            self.log(f"📡 Sending WiFi credentials to ESP32: {url}")
            resp = requests.post(url, json=payload, timeout=4)

            if resp.status_code == 200:
                messagebox.showinfo("Success", "WiFi credentials sent successfully!")
                self.log("✓ ESP32 WiFi updated successfully")
            else:
                messagebox.showwarning("ESP32 Error",
                                       f"ESP32 responded with status {resp.status_code}\nResponse: {resp.text}")
                self.log(f"⚠ ESP32 responded {resp.status_code}: {resp.text}")

        except Exception as e:
            messagebox.showerror("Connection Error",
                                 f"Failed to send WiFi credentials:\n{e}")
            self.log(f"❌ Error sending WiFi credentials: {e}")

    # ---------------- gesture config and misc ----------------
    def update_gesture_mapping(self, gesture, action):
        self.gesture_actions[gesture] = action
        self.save_gesture_config()
        self.log(f"⚙️ {gesture} → {action}")

    def send_custom_message(self):
        message = self.esp_msg_entry.get()
        if message:
            self.send_to_esp32_display(message[:80])
            self.log(f"📤 Sent: {message[:80]}")
            self.esp_msg_entry.delete(0, tk.END)
        else:
            messagebox.showwarning("Warning", "Enter a message first")

    def _prompt_and_speak_custom(self):
        text = simpledialog.askstring("Custom Speak", "Enter text to speak:", parent=self.root)
        if text:
            self.log(f"🔊 Speaking custom: {text[:80]}")
            self.speak_text(text, self.target_lang)

    def reset_application(self):
        self.stop_stream()
        self.last_translation = ""
        try:
            self.log_text.delete('1.0', tk.END)
        except:
            pass
        self.log("🔁 Application reset")

    def repeat_last_translation(self):
        if self.last_translation:
            self.log("🔁 Repeating last translation")
            tgt_code = self.languages.get(self.target_lang_var.get(), 'es')
            self.speak_text(self.last_translation, tgt_code)
            try:
                self.send_to_esp32_display(self.last_translation[:80])
            except:
                pass
        else:
            self.log("⚠ No last translation")

    def log(self, message):
        ts = time.strftime("%H:%M:%S")
        try:
            self.log_text.insert(tk.END, f"[{ts}] {message}\n")
            self.log_text.see(tk.END)
        except:
            print(f"[{ts}] {message}")

    def clear_log(self):
        try:
            self.log_text.delete('1.0', tk.END)
        except:
            pass

    def cleanup(self):
        self.stop_stream()
        if self.temp_audio_file and os.path.exists(self.temp_audio_file):
            try: os.remove(self.temp_audio_file)
            except: pass
        try:
            pygame.mixer.quit()
        except:
            pass
        try:
            if self.pyaudio_inst:
                self.pyaudio_inst.terminate()
        except:
            pass
        try:
            self.save_gesture_config()
        except:
            pass
        self.root.destroy()

# ---------------- main ----------------
def main():
    root = tk.Tk()
    app = GestureController(root)
    root.protocol("WM_DELETE_WINDOW", app.cleanup)
    root.mainloop()

if __name__ == "__main__":
    main()

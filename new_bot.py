import os, time, datetime, subprocess, sys, textwrap
from dotenv import load_dotenv

# --- Selenium imports ---
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# --- Transcription + Summarization ---
from faster_whisper import WhisperModel
import google.generativeai as genai

# --- Speaking (updated to match test2.py behavior) ---
from TTS.api import TTS
import sounddevice as sd
import soundfile as sf

# ================== CONFIG ==================
MEET_URL = "https://meet.google.com/zot-dhfu-och"   # <-- your Meet link
CHROMEDRIVER_PATH = "/opt/homebrew/bin/chromedriver"

# Use your **cloned Chrome profile** (already logged in)
USER_DATA_DIR = "/Users/Saurabh/Desktop/MeetBot/chrome_profile"
PROFILE_DIR = "Default"

# BlackHole 2ch device index from `ffmpeg -f avfoundation -list_devices true -i ""`
BLACKHOLE_INDEX = "0"

# Whisper model size
WHISPER_MODEL = "small"
SAMPLE_RATE = "16000"

# Gemini model
GEMINI_MODEL = "gemini-1.5-flash"

# Predefined agenda text (for speaking)
AGENDA_TEXT = ""
# ============================================

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# ------------------- Coqui TTS init (single instance) ------------------- #
# This mirrors test2.py: initialize TTS once at module load.
tts = TTS(model_name="tts_models/en/ljspeech/tacotron2-DDC", progress_bar=False, gpu=False)

# ------------------- SPEAK FUNCTION (matches test2.py) ------------------- #
def bot_speak(text):
    """
    Convert text to speech with Coqui and route playback into BlackHole
    so Google Meet hears the audio (virtual mic).
    This implementation intentionally mirrors test2.py exactly.
    """
    try:
        output_path = "speech.wav"
        # Create WAV file using Coqui TTS (tts_to_file ensures consistent file output)
        tts.tts_to_file(text=text, file_path=output_path)

        # Load generated audio
        data, samplerate = sf.read(output_path)

        # Find BlackHole device by name
        devices = sd.query_devices()
        blackhole_index = None
        for i, d in enumerate(devices):
            # match substring "BlackHole" (case-sensitive as most devices report name like "BlackHole 2ch")
            if "BlackHole" in d.get("name", ""):
                blackhole_index = i
                break

        if blackhole_index is None:
            # Helpful diagnostic: print list of devices to help user troubleshoot
            print("❌ BlackHole device not found. Available audio devices:")
            for i, d in enumerate(devices):
                print(f"  {i}: {d.get('name')}")
            print("Please ensure BlackHole is installed and available. Aborting speak.")
            return

        # Play audio into BlackHole (virtual microphone)
        sd.play(data, samplerate, device=blackhole_index)
        sd.wait()
        print("🗣️ Spoke in Meet:", text)
    except Exception as e:
        print(f"⚠️ Error in bot_speak(): {e}")

# ------------------- EXISTING CODE ------------------- #
def join_meet_chrome():
    chrome_options = Options()
    chrome_options.add_argument("--disable-infobars")
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--user-data-dir=/Users/Saurabh/Desktop/MeetBot/chrome_profile")
    chrome_options.add_argument("--profile-directory=Default")
    service = Service(CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.get(MEET_URL)
    time.sleep(3)
    try:
        cam_button = driver.find_element(By.XPATH, '//div[contains(@aria-label, "camera")]')
        cam_button.click()
    except:
        print("⚠ Camera button not found.")
    try:
        join_button = driver.find_element(By.XPATH, "//span[text()='Join now']")
        join_button.click()
        print("✅ Clicked 'Join now'")
        print("🔒 Bot is in the meeting and ready to record…")
        return driver, False
    except:
        try:
            ask_button = driver.find_element(By.XPATH, "//span[text()='Ask to join']")
            ask_button.click()
            print("✅ Clicked 'Ask to join'")
            print("⏳ Waiting for host to admit...")
            admitted = False
            while not admitted:
                time.sleep(5)
                if "meet.google.com" in driver.current_url and "lookup" not in driver.current_url:
                    admitted = True
                    print("✅ Admitted into the meeting!")
            print("🔒 Bot is in the meeting and ready to record…")
            return driver, True
        except:
            print("⚠️ Neither 'Join now' nor 'Ask to join' found.")
    print("🔒 Bot is in the meeting and ready to record…")
    return driver, False

def is_in_meet(driver):
    try:
        return "meet.google.com" in driver.current_url
    except Exception:
        return False

def start_ffmpeg_recording(out_wav_path):
    cmd = [
        "ffmpeg","-y","-f","avfoundation","-i",f":{BLACKHOLE_INDEX}","-ac","1","-ar",SAMPLE_RATE,out_wav_path
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    print(f"🎙️  Recording started → {out_wav_path}")
    return proc

def stop_ffmpeg_safely(proc):
    if proc and proc.poll() is None:
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
            proc.wait(timeout=10)
        except Exception:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

def transcribe_wav(path):
    print("📝 Transcribing with faster-whisper…")
    model = WhisperModel(WHISPER_MODEL, compute_type="int8")
    segments, info = model.transcribe(path,vad_filter=True,vad_parameters=dict(min_silence_duration_ms=500),beam_size=5)
    transcript = " ".join([seg.text.strip() for seg in segments if seg.text.strip()])
    print(f"🗣️  Detected language: {getattr(info, 'language', 'unknown')}, prob={getattr(info, 'language_probability', 0):.2f}")
    return transcript

def summarize_with_gemini(text):
    if not text.strip():
        return "(Transcript was empty.)"
    capped = text[:120_000]
    prompt = textwrap.dedent(f"""
    You are a helpful meeting assistant. Summarize the following meeting transcript into:
    - Key decisions
    - Action items with owners (if mentioned)
    - Open questions / blockers
    - Brief timeline/next steps
    Keep it concise but clear. Support multilingual content if present.

    Transcript:
    {capped}
    """)
    model = genai.GenerativeModel(GEMINI_MODEL)
    resp = model.generate_content(prompt)
    return resp.text or "(No summary produced.)"

def main():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_wav = f"meeting_{timestamp}.wav"
    driver, ff = None, None
    try:
        driver, waited_for_admit = join_meet_chrome()

        # --- Speak agenda after joining (uses test2.py behavior) ---
        if waited_for_admit:
            time.sleep(5)  # Wait 5 seconds after being admitted
        else:
            time.sleep(3)  # Wait 3 seconds if joined instantly
        bot_speak(AGENDA_TEXT)

        ff = start_ffmpeg_recording(output_wav)
        print("🔒 Staying in the meeting; recording until it ends…  (Ctrl+C to stop)")
        while True:
            time.sleep(10)
            if not is_in_meet(driver):
                print("❌ Not in Meet anymore (tab closed or navigated).")
                break
    except KeyboardInterrupt:
        print("\n👋 Stopping on user request…")
    finally:
        if ff:
            stop_ffmpeg_safely(ff)
            print(f"💾 Saved audio → {output_wav}")
        try:
            if driver:
                driver.quit()
        except Exception:
            pass
    if os.path.exists(output_wav) and os.path.getsize(output_wav) > 44:
        transcript = transcribe_wav(output_wav)
        print("\n================ TRANSCRIPT (first 600 chars) ================\n")
        print(transcript[:600] + ("..." if len(transcript) > 600 else ""))
        print("\n==============================================================\n")
        summary = summarize_with_gemini(transcript)
        print("\n==================== MEETING SUMMARY ====================\n")
        print(summary)
        print("\n=========================================================\n")
    else:
        print("⚠️ No audio captured (empty file). Make sure Chrome’s output is routed to BlackHole 2ch.")

if __name__ == "__main__":
    if not os.path.exists(CHROMEDRIVER_PATH):
        print(f"❌ chromedriver not found at: {CHROMEDRIVER_PATH}")
        sys.exit(1)
    main()

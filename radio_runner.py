
# radio_runner.py
# Live loop: Windows default input -> Vosk STT -> Controller.handle_radio -> Piper/IVC TX.

import os, json, time, sys, numpy as np
import sounddevice as sd
from vosk import Model, KaldiRecognizer
import keyboard

from maivo_core import Controller, TrackIndex, TacviewAdapter, PiperSynth, IVCTxAdapter, AWACSMessage

# ---------------- Config ----------------
VOSK_MODEL = os.getenv("VOSK_MODEL", r"C:\vosk\vosk-model-small-en-us-0.15").strip()
AUDIO_IN   = os.getenv("AUDIO_IN", "").strip()    # '' or 'default' -> system default input
AUDIO_OUT  = os.getenv("AUDIO_OUT", None)         # None -> default output
PTT_KEY    = os.getenv("IVC_PTT_KEY", "F1")
TTV_ENABLE = os.getenv("TTV_ENABLE", "1")         # "1" to enable Tacview feeder
TTV_HOST   = os.getenv("TTV_HOST", "127.0.0.1")
TTV_PORT   = int(os.getenv("TTV_PORT", "42674"))
TTV_PW     = os.getenv("TTV_PASSWORD", "0")
TARGET_SR  = int(os.getenv("STT_SR", "16000"))
MIN_SPEAK_INTERVAL = 0.8

def ptt_down():
    try: keyboard.press(PTT_KEY)
    except Exception as e: print(f"[PTT] press failed: {e}")

def ptt_up():
    try: keyboard.release(PTT_KEY)
    except Exception as e: print(f"[PTT] release failed: {e}")

_last_speak = 0.0
def speak(ivc, text: str):
    global _last_speak
    if not text: return
    dt = time.time() - _last_speak
    if dt < MIN_SPEAK_INTERVAL:
        time.sleep(MIN_SPEAK_INTERVAL - dt)
    _last_speak = time.time()
    ivc(AWACSMessage(text=text))

def _resolve_vosk() -> str:
    if VOSK_MODEL and os.path.isdir(VOSK_MODEL):
        return VOSK_MODEL
    fallback = os.path.join(os.getcwd(), "vosk", "vosk-model-small-en-us-0.15")
    if os.path.isdir(fallback): return fallback
    print(f"[STT] Vosk model not found: {VOSK_MODEL}")
    print("      Download 'vosk-model-small-en-us-0.15' and set VOSK_MODEL to its folder.")
    sys.exit(2)

def main():
    tracks = TrackIndex()
    tac = None
    if TTV_ENABLE == "1":
        tac = TacviewAdapter(tracks, host=TTV_HOST, port=TTV_PORT, password=TTV_PW)
        tac.start()

    ctrl = Controller(tracks=tracks)
    piper = PiperSynth()  # default pronounce_map already maps MAIVO->MAYVO

    def audio_play_tx(pcm: bytes):
        sr = 22050
        meta = piper.model_path + ".json"
        if os.path.exists(meta):
            with open(meta, "r", encoding="utf-8") as f:
                sr = json.load(f).get("sample_rate", sr)
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        sd.play(audio, samplerate=sr, device=AUDIO_OUT, blocking=True)

    ivc = IVCTxAdapter(tts_synth=piper, audio_play=audio_play_tx, ptt_press=ptt_down, ptt_release=ptt_up)

    model_dir = _resolve_vosk()
    model = Model(model_dir)
    rec = KaldiRecognizer(model, TARGET_SR)
    rec.SetWords(True)

    use_default_in = (AUDIO_IN == "" or AUDIO_IN.lower() == "default")
    in_dev = None
    if use_default_in:
        try:
            idx = sd.default.device[0]
            name = sd.query_devices(idx)["name"] if idx is not None else "system default"
        except Exception:
            name = "system default"
        print(f"[STT] Listening on system default input '{name}' at {TARGET_SR} Hz")
    else:
        ds = sd.query_devices()
        for i, d in enumerate(ds):
            if d.get("max_input_channels", 0) > 0 and AUDIO_IN.lower() in d["name"].lower():
                in_dev = i; break
        if in_dev is None:
            print(f"[Audio] Input device not found: {AUDIO_IN}")
            print("Tip: leave AUDIO_IN unset to use the Windows default input.")
            sys.exit(3)
        print(f"[STT] Listening on '{sd.query_devices(in_dev)['name']}' at {TARGET_SR} Hz")

    def callback(indata, frames, time_info, status):
        if status: print("[STT] stream status:", status)
        if indata.ndim > 1:
            mono = np.mean(indata, axis=1)
        else:
            mono = indata
        pcm16 = (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        if rec.AcceptWaveform(pcm16):
            try:
                text = json.loads(rec.Result()).get("text", "").strip()
            except Exception:
                text = ""
            if text:
                print(f'[RX->STT] "{text}"')
                reply = ctrl.handle_radio(text)
                if reply:
                    print(f"[TX<-MAIVO] {reply}")
                    speak(ivc, reply)

    with sd.InputStream(device=in_dev, dtype="float32", channels=1,
                        samplerate=TARGET_SR, blocksize=1024, callback=callback):
        print("[Main] MAIVO live radio running — address as: 'MAIVO 1-1, Viper 1-1, picture'")
        print("       Ctrl+C to stop.")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            if tac: tac.stop()

if __name__ == "__main__":
    main()

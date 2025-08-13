
# maivo_core.py
# Minimal unified core for MAIVO:
#  - TrackIndex (friendly callsigns on scope)
#  - TacviewAdapter (optional, feeds TrackIndex)
#  - Controller (only answers when addressed as MAIVO 1-1: "MAIVO 1-1, <CALLSIGN>, <CMD>")
#  - PiperSynth (text -> raw PCM via piper.exe)
#  - IVCTxAdapter (hold PTT + play PCM to IVC mic)
#
# Notes:
#  - No triple-quoted strings to avoid Windows quote escaping issues.
#  - Keep comments short and human-friendly.

import os, re, time, socket, threading, subprocess, shutil, json
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum, auto

# ---------------- Types ----------------

class Priority(Enum):
    LOW = auto()
    NORMAL = auto()
    HIGH = auto()
    CRITICAL = auto()

@dataclass
class AWACSMessage:
    text: str
    speak: bool = True
    priority: Priority = Priority.NORMAL
    recipients: Optional[List[str]] = None
    tag: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

# ---------------- TrackIndex ----------------
# Minimal callsign index; use TacviewAdapter to keep it fresh.

class TrackIndex:
    def __init__(self) -> None:
        self._blue: Dict[str, float] = {}  # callsign -> last_seen_epoch

    def upsert_blue(self, callsign: str) -> None:
        if not callsign: return
        key = self.normalize(callsign)
        self._blue[key] = time.time()

    def seen_blue(self, callsign: str, ttl: float = 300.0) -> bool:
        if not callsign: return False
        key = self.normalize(callsign)
        ts = self._blue.get(key)
        if ts is None: return False
        if ttl <= 0: return True
        return (time.time() - ts) <= ttl

    def normalize(self, callsign: str) -> str:
        t = re.sub(r"\s+", " ", callsign.strip().upper())
        t = t.replace(" - ", "-")
        return t

# ---------------- TacviewAdapter ----------------
# Best-effort Real-Time Telemetry client. Adds Blue callsigns to TrackIndex.
# Safe if Tacview is not running (it will print a warning and you can still test).

class TacviewAdapter:
    def __init__(self, tracks: TrackIndex,
                 host: str = "127.0.0.1", port: int = 42674, password: str = "0") -> None:
        self.tracks = tracks
        self.host, self.port, self.password = host, port, password
        self._stop = threading.Event()
        self._thr: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    def start(self) -> None:
        if self._thr and self._thr.is_alive(): return
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.shutdown(socket.SHUT_RDWR)
                self._sock.close()
        except Exception:
            pass
        if self._thr: self._thr.join(timeout=1.5)

    def _run(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=3.0)
            self._sock.sendall(b"XtraLib.Stream.0\n")
            self._sock.sendall(b"Tacview.RealTimeTelemetry.Client\n")
            self._sock.sendall((self.password + "\n").encode("utf-8"))
            buff = b""
            print("[Tacview] connected")
            while not self._stop.is_set():
                chunk = self._sock.recv(4096)
                if not chunk: break
                buff += chunk
                while b"\n" in buff:
                    line, buff = buff.split(b"\n", 1)
                    self._on_line(line.decode("utf-8", "ignore").strip())
        except Exception as e:
            print(f"[Tacview] warning: {e}")

    def _on_line(self, line: str) -> None:
        # Look for Callsign=..., prefer Coalition=Blue if present
        if "Callsign=" not in line: return
        coal = None
        mcoal = re.search(r"Coalition=([^,]+)", line)
        if mcoal: coal = mcoal.group(1).strip().lower()
        if coal and coal not in ("blue", "allied", "ally", "friendly"):
            return
        m = re.search(r"Callsign=([^,]+)", line)
        if not m: return
        callsign = m.group(1).strip()
        if callsign:
            self.tracks.upsert_blue(callsign)

# ---------------- Controller ----------------
# Only responds when addressed by our AI callsign. Expects:
#   "MAIVO 1-1, <CALLSIGN>, <COMMAND>"
# It will ignore anything else or unknown callsigns (unless REQUIRE_ON_SCOPE=0).

AI_CALLSIGN = os.getenv("AI_CALLSIGN", "MAIVO 1-1").strip()
AI_CALLSIGN_ALIASES = [
    AI_CALLSIGN,
    "MAIVO",
    "MAYVO",   # spoken form
    "MAIVO 11",
    "MAIVO 1-1",
]
REQUIRE_ON_SCOPE = os.getenv("REQUIRE_ON_SCOPE", "1") != "0"

class Controller:
    def __init__(self, tracks: Optional[TrackIndex] = None) -> None:
        self.tracks = tracks or TrackIndex()
        self.radio_name = os.getenv("AWACS_RADIO_NAME", "AWACS")
        self.radio_freq = os.getenv("AWACS_RADIO_FREQ", "305.0")
        self.radio_mod  = os.getenv("AWACS_RADIO_MOD",  "AM")

    def _friendly(self, callsign: str) -> str:
        return callsign.strip()

    def parse_radio_frame(self, text: str) -> Tuple[bool, Optional[str], str]:
        t = (text or "").strip()
        if not t: return False, None, ""
        alias_alt = "|".join(re.escape(a) for a in AI_CALLSIGN_ALIASES if a)
        lead = re.compile(
            rf"^\s*(?:hey\s+|ok(?:ay)?\s+|awacs\s+)?(?:{alias_alt})\b[\s,]*"
            rf"(?P<callsign>[^,]+?)\s*,?\s*(?P<cmd>.+)$",
            re.IGNORECASE,
        )
        m = lead.match(t)
        if m:
            return True, m.group("callsign").strip(), m.group("cmd").strip()
        trail = re.compile(
            rf"^\s*(?P<callsign>[^,]+?)\s*,?\s*(?P<cmd>.+?)\s*,?\s*(?:{alias_alt})\s*$",
            re.IGNORECASE,
        )
        m2 = trail.match(t)
        if m2:
            return True, m2.group("callsign").strip(), m2.group("cmd").strip()
        return False, None, t

    def handle_radio(self, text: str) -> Optional[str]:
        addressed, caller, cmd = self.parse_radio_frame(text)
        if not addressed or not caller: return None
        if REQUIRE_ON_SCOPE and not self.tracks.seen_blue(caller):
            return None
        return self.handle_text(f"{self._friendly(caller)} {cmd}")

    def handle_text(self, text: str) -> Optional[str]:
        t = (text or "").strip().lower()
        caller = None
        m = re.match(r"([a-z0-9\- ]+)\s+(.*)$", t)
        if m: caller, t = m.group(1), m.group(2)

        if "bogey dope" in t or ("bogey" in t and "dope" in t):
            who = self._friendly(caller or "Fighter")
            return f"{who}, BRAA two six zero for 18, 15 thousand, hot."
        if "picture" in t:
            who = self._friendly(caller or "Package")
            return (f"{who}, picture, two groups. Lead group BRAA three one zero for 25, "
                    f"22 thousand, hot. Trail group BRAA three one five for 40, flight level 280, flanking.")
        if "declare" in t or "identify" in t or t.startswith("id "):
            who = self._friendly(caller or "Fighter")
            return f"{who}, unable declare specific track. Say bullseye or track number."
        if t.startswith("push"):
            m2 = re.search(r"(\d{3}\.\d)", t)
            freq = m2.group(1) if m2 else self.radio_freq
            return f"Package, push {freq}."
        if "cap" in t and ("request" in t or "task" in t or "switch" in t):
            who = self._friendly(caller or "Fighter")
            return f"{who}, copy task. Proceed CAP Alpha. Anchor bull three four zero for 35, flight level 260."
        return None

# ---------------- TTS (Piper) ----------------

class PiperSynth:
    # Wraps piper.exe; returns raw 16-bit mono PCM via --output-raw.
    def __init__(self,
                 piper_path: Optional[str] = None,
                 model_path: Optional[str] = None,
                 extra_args: Optional[List[str]] = None,
                 encoding: str = "utf-8",
                 pronounce_map: Optional[Dict[str,str]] = None) -> None:
        self.piper_path = piper_path or os.getenv("PIPER_EXE", "piper.exe")
        self.model_path = model_path or os.getenv("PIPER_MODEL", "en_US-amy-medium.onnx")
        self.extra_args = extra_args or []
        self.encoding = encoding
        self.pronounce_map = pronounce_map or {"MAIVO":"MAYVO"}
        self._resolve_paths()

    def _resolve_paths(self) -> None:
        exe = self.piper_path
        if not os.path.isabs(exe):
            found = shutil.which(exe)
            if found: exe = found
        if not os.path.exists(exe):
            raise FileNotFoundError(f"piper executable not found: {self.piper_path}")
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(f"piper model not found: {self.model_path}")
        self.piper_path = exe
        if not os.path.exists(self.model_path + ".json"):
            print(f"[Piper] Warning: missing JSON sidecar: {self.model_path}.json")

    def __call__(self, text: str) -> bytes:
        if self.pronounce_map:
            for k, v in self.pronounce_map.items():
                text = re.sub(rf"\b{re.escape(k)}\b", v, text, flags=re.IGNORECASE)
        cmd = [self.piper_path, "--model", self.model_path, "--output-raw"] + self.extra_args
        print("[Piper] Exec:", " ".join(repr(c) for c in cmd))
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        out, err = proc.communicate(input=text.encode(self.encoding))
        if proc.returncode != 0:
            raise RuntimeError(f"piper failed ({proc.returncode}): {err.decode(self.encoding, 'ignore')}")
        return out

# ---------------- IVC TX adapter ----------------

class IVCTxAdapter:
    # TTS -> PCM -> play to virtual mic (VB-Cable) while holding PTT.
    def __init__(self,
                 tts_synth,
                 audio_play,
                 ptt_press=None,
                 ptt_release=None) -> None:
        self.tts_synth = tts_synth
        self.audio_play = audio_play
        self.ptt_press = ptt_press
        self.ptt_release = ptt_release

    def __call__(self, msg: AWACSMessage) -> None:
        if not msg or not msg.text: return
        if self.ptt_press: self.ptt_press()
        try:
            pcm = self.tts_synth(msg.text)
            self.audio_play(pcm)
        finally:
            if self.ptt_release: self.ptt_release()

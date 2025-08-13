
# run_maivo.ps1
# 1) pip install -r requirements_min.txt
# 2) Place piper.exe + voice under .\piper\ (voice: .onnx + .onnx.json)
# 3) Download a Vosk model folder, e.g. C:\vosk\vosk-model-small-en-us-0.15
# 4) Run this script

$env:VOSK_MODEL = "C:\vosk\vosk-model-small-en-us-0.15"
# Use Windows default INPUT by leaving AUDIO_IN unset or setting 'default'
# $env:AUDIO_IN = "default"
# Optional: force a specific TX device (e.g., VB-Cable Input)
# $env:AUDIO_OUT = "CABLE Input (VB-Audio Virtual Cable)"
# PTT hotkey your IVC client listens to:
$env:IVC_PTT_KEY = "F1"

python .\radio_runner.py

# BPMtoOSC for macOS

Detects the BPM from an audio input (DJ mixer, audio interface or the Mac's own sound) and sends it as OSC to Resolume, grandMA3 or any other OSC program. It can also send the bar position (beat 1 to 4).

macOS fork of the original BPM-to-OSC (v1.2.0). Beat detection idea from [DrLuke/aubio-beat-osc](https://github.com/DrLuke/aubio-beat-osc). GPLv3, like the original.

## What changed

1. Runs on macOS and builds into a normal `.app`.
2. New input "System sound": captures Spotify, YouTube and everything the Mac plays. No BlackHole needed. Macos 13 or newer.
3. New tempo engine: follows a new song in 2 to 4 seconds instead of 30 to 40.
4. LOCK mode: big tempo jumps (over 6 %) are only sent after they hold for 10 seconds.
5. Target selector: Resolume, grandMA3 or Raw BPM.
6. Bar position output: `/beat/count`, `/beat/one`, `/beat/two`.
7. Auto reconnect after the Mac sleeps or the audio stops.
8. Fixes: Ping works on Mac, glitchy buttons, sender thread crash, 128 BPM never shown, +1 jumping with 1/2.
9. Hover help on every control.

## Install

Needs a Mac with macOS 13 or newer.

**1. Developer tools** (a window opens, click Install)
```
xcode-select --install
```

**2. Homebrew** (skip if you have it): https://brew.sh

**3. Python and audio library**
```
brew install python@3.11 portaudio
```

**4. Get the code.** Download the ZIP from GitHub (green Code button), unzip it, then open Terminal in that folder:
```
cd ~/BPMtoOSC
```

**5. Virtual environment**
```
python3.11 -m venv venv
source venv/bin/activate
```
Run the `source` line again every time you open a new Terminal.

**6. Packages**
```
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

**7. aubio** (the flag is needed because the new Mac compiler is strict)
```
CFLAGS="-Wno-incompatible-function-pointer-types" pip install aubio --no-build-isolation
```

**8. System sound helper**
```
swiftc -O -swift-version 5 sckaudio.swift -o sckaudio
```

**9. Run it**
```
python main_gui.py
```

**10. Build the app** (optional, takes a minute)
```
chmod +x build_mac.sh
./build_mac.sh
```
The app appears in `dist/`. Drag it to Applications. First launch: right click, Open, Open again (it is not signed by Apple).

## Permissions

System Settings, Privacy & Security:
1. Screen & System Audio Recording, for "System sound".
2. Microphone, for a mixer or audio interface.

After a rebuild, switch the permission off and on again if macOS ignores it.

## Resolume

1. Resolume: Preferences, OSC, enable OSC Input, port 7000.
2. App: target Resolume, enter the IP (`127.0.0.1` if same Mac), port 7000, press Ping, then START.
3. Press RESYNC BAR on the first beat of a bar.

| Purpose | Address | Value |
| --- | --- | --- |
| BPM | `/composition/tempocontroller/tempo` | (bpm minus 20) / 480 |
| RESYNC BAR | `/composition/tempocontroller/resync` | 1 |
| Bar position | `/beat/count` | 0 to 3 |
| Beat 1 | `/beat/one` | 1 on beat 1, else 0 |
| Beats 1 and 3 | `/beat/two` | 1 on beats 1 and 3, else 0 |

## Problems

1. BPM reads double or half (90 as 180): press x2 or /2.
2. No devices or the app closes: check the permissions above.
3. Debug: `BPM_DEBUG=1 python main_gui.py` prints tempo candidates and saves the last 45 seconds to `~/BPMtoOSC_capture.wav` on STOP.

## Credits

Original BPM-to-OSC authors and the forks by Teckiee, PoundlandBacon and luluzinha69. Beat detection: aubio and DrLuke/aubio-beat-osc.

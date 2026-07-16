# Pi Zero 2W Voice Assistant — Thin Client

A lightweight WebSocket client for the **Raspberry Pi Zero 2W** that connects to
`voice-assistant-app` running on your laptop. The Zero is a **thin peripheral**:
it captures microphone audio, plays back AI-generated speech relayed from the
laptop, runs mic calibration/gating, and reports device status. No API keys and
no Realtime logic live on the device.

This repo is a hardware retarget of the Pi 5 client: same WebSocket protocol,
same 24 kHz PCM16 mono audio, but driving a **GPIO I2S MEMS mic + I2S class-D
amp** (e.g. MAX98357A-class) instead of USB sound cards.

## How it fits in the system

```
┌──────────────────┐        WebSocket (Wi-Fi hotspot)     ┌──────────────────────┐
│  Pi Zero 2W      │ ────────────────────────────────────▶│  voice-assistant-app  │
│  (this client)   │◀──────────────────────────────────── │      (Laptop)         │
│  I2S mic + amp   │   HELLO, DEVICE_STATUS,               │                      │
│                  │   AUDIO_FRAME, PLAY_AUDIO             │  → OpenAI Realtime   │
└──────────────────┘                                       └──────────────────────┘
```

The Zero connects **as a client** to the laptop's WebSocket server. The laptop
owns all intelligence: API keys, session management, parent controls. The Zero
reports `device_type: "pi_zero_2w"`.

## Bring-up order

Do these in order — the hardware/ALSA step is the highest risk; the software is
straightforward once the I2S card appears.

1. **Flash Raspberry Pi OS Lite (64-bit Bookworm)** — enable SSH + Wi-Fi in the
   Imager. All three devices (Zero, laptop, and whatever drives the Zero) must
   share one network; a phone hotspot works if it allows device-to-device
   traffic.
2. **Wire + enable the I2S mic and amp** — see [docs/hardware_gpio_i2s.md](docs/hardware_gpio_i2s.md).
3. **Validate ALSA** — `arecord -l` / `aplay -l` show the card, and a local
   loopback records + plays 24 kHz mono. See [docs/alsa_bringup.md](docs/alsa_bringup.md).
4. **Configure + run this client** (below) and validate end-to-end with the app.
5. **Battery baseline + trims** — see [docs/battery.md](docs/battery.md).

## Install (on the Zero 2W)

```bash
sudo apt install alsa-utils            # arecord / aplay
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # websockets only
```

## Configure audio devices

Copy the example env and set the card numbers from `arecord -l` / `aplay -l`.
With a single I2S card doing duplex, capture and playback are usually the **same
card** (e.g. `plughw:0,0` for both) — confirm on your board.

```bash
cp .env.example .env
# edit AUDIO_INPUT_DEVICE / AUDIO_OUTPUT_DEVICE to match your card
```

`zero2w_client.py` loads `.env` automatically on startup.

## Run the client

Point it at the laptop's WebSocket server (find the laptop IP with
`hostname -I | awk '{print $1}'` on Linux, `ipconfig getifaddr en0` on macOS):

```bash
python zero2w_client.py ws://LAPTOP_IP:8765
python zero2w_client.py ws://192.168.1.42:8765 --debug   # verbose
```

Then from the laptop dashboard: **Start Session** → calibration → speak → hear
the reply → **Stop Session**.

## Tests (no hardware required)

Run on any machine:

```bash
python test_client.py
python test_audio.py
```

## What this device does and does not do

- **Does:** ALSA capture/playback, calibration + echo gating, WebSocket framing,
  status heartbeats.
- **Does not:** hold API keys, run OpenAI Realtime, or do barge-in / AEC. That
  intelligence stays on the laptop app.

`battery_percent` in `DEVICE_STATUS` stays `None` until a fuel-gauge exists; the
field is already in the schema.

## Relationship to the Pi 5 client

Protocol-identical to `voice-assistant-pi5`'s `device/pi5_client.py`. The message
schema (`HELLO`, `HELLO_ACK`, `DEVICE_STATUS`, `START/STOP_AUDIO_STREAM`,
`AUDIO_FRAME`, `PLAY_AUDIO`, calibration, `PLAYBACK_COMPLETE`) is unchanged, so
the existing app handlers work without modification. Only the hardware layer
(USB → GPIO I2S) and device type differ.

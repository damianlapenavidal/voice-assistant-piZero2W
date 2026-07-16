# Bring-up guide (handoff for Claude Code)

This file is the ordered build plan for the Pi Zero 2W voice-assistant thin
client. It exists so you can open **Claude Code on the laptop**, point it here,
and continue where the scaffolding left off. Work top to bottom; each phase has a
**checkpoint** you must pass before moving on.

> **Context for the agent:** This repo is a hardware retarget of the Pi 5 device
> client (`voice-assistant-pi5/device/`). The WebSocket protocol, audio format
> (24 kHz / S16_LE / mono), calibration, and gating are **unchanged** — do not
> modify the message schema. Only the hardware layer (USB → GPIO I2S) and
> `device_type` ("pi_zero_2w") differ. The laptop's `voice-assistant-app` owns
> all intelligence and API keys; this device is a thin peripheral.

## Current status (already done)

- [x] Repo scaffolded and pushed to `github.com/damianlapenavidal/voice-assistant-piZero2W`.
- [x] `zero2w_client.py` renamed from `pi5_client.py`: `class Zero2WClient`,
      `DEVICE_TYPE = "pi_zero_2w"`, logger/CLI strings updated.
- [x] `audio_capture.py`, `audio_playback.py`, `audio_gating.py`,
      `calibration_prompt.py`, `assets/say_hello_prompt.pcm` copied unchanged.
- [x] I2S `.env.example` + docs (`hardware_gpio_i2s.md`, `alsa_bringup.md`,
      `battery.md`) written.
- [x] Unit tests pass on a laptop with no hardware: **23/23**
      (`test_client.py` 9, `test_audio.py` 14).

## Architecture

```
Pi Zero 2W (this repo)  ──WebSocket (Wi-Fi hotspot)──▶  voice-assistant-app (laptop) ──▶ OpenAI Realtime
  I2S mic + I2S amp                                       owns keys + intelligence
```

## Network prerequisites (do first)

All three must reach each other on **one network** (a phone hotspot for v1, if it
allows device-to-device traffic):

- **Zero 2W** — Raspberry Pi OS Lite (64-bit Bookworm), SSH + Wi-Fi enabled in the
  Imager. User: `voice-assistant-pizero2w`.
- **Laptop** — runs `voice-assistant-app` **and** is where you run Claude Code +
  SSH into the Zero.
- Find the Zero's IP from the hotspot's connected-devices list (or an ARP scan
  from the laptop once both are on the hotspot).

Checkpoint: `ssh voice-assistant-pizero2w@<zero-ip> 'hostname; uname -a'` works.

---

## Phase 1 — Hardware wiring

Follow [docs/hardware_gpio_i2s.md](docs/hardware_gpio_i2s.md). Wire the I2S mic +
class-D amp: shared **BCLK (GPIO18)** and **LRCLK (GPIO19)**, mic **DOUT →
GPIO20**, amp **DIN → GPIO21**, plus power/ground.

> **Risk — speaker/amp rating:** many class-D amps are spec'd for ≥ 4 Ω. Read the
> 3 Ω-speaker warning in that doc before powering at 5 V / high volume.

**Checkpoint:** wiring matches the pin table; nothing gets hot on power-up.

## Phase 2 — Enable the I2S sound card (highest risk)

On the Zero, edit `/boot/firmware/config.txt` per
[docs/hardware_gpio_i2s.md](docs/hardware_gpio_i2s.md):

```ini
dtparam=i2s=on
dtoverlay=googlevoicehat-soundcard   # one overlay owns I2S; don't stack overlays
dtparam=audio=off
dtoverlay=vc4-kms-v3d,noaudio
```

Reboot.

**Checkpoint:** `arecord -l` and `aplay -l` both show the I2S card. Note the
`card,device` numbers. If not, `dmesg | grep -iE "i2s|asoc|voicehat"`.

## Phase 3 — Validate ALSA (the real proof)

Follow [docs/alsa_bringup.md](docs/alsa_bringup.md). Install `alsa-utils`, then:

```bash
# playback
speaker-test -D plughw:0,0 -c 1 -r 24000 -t wav
# capture
arecord -D plughw:0,0 -f S16_LE -r 24000 -c 1 -t raw -d 3 /tmp/cap.raw
# loopback (must hear your voice)
arecord -D plughw:0,0 -f S16_LE -r 24000 -c 1 -t raw -d 2 /tmp/lb.raw
aplay   -D plughw:0,0 -f S16_LE -r 24000 -c 1 -t raw /tmp/lb.raw
```

Replace `0,0` with your real `card,device`.

**Checkpoint:** loopback plays back recorded voice cleanly. Everything downstream
is easy once this passes; do not proceed until it does.

## Phase 4 — Install + configure the client

On the Zero:

```bash
git clone git@github.com:damianlapenavidal/voice-assistant-piZero2W.git
cd voice-assistant-piZero2W
sudo apt install alsa-utils
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # set AUDIO_INPUT_DEVICE / AUDIO_OUTPUT_DEVICE to your card
```

**Checkpoint:** `python test_client.py` and `python test_audio.py` pass on the
Zero (23/23).

## Phase 5 — End-to-end with the laptop app

1. Start `voice-assistant-app` on the laptop with the WebSocket server.
2. Find the laptop IP: `hostname -I | awk '{print $1}'` (Linux) /
   `ipconfig getifaddr en0` (macOS).
3. On the Zero:
   ```bash
   python zero2w_client.py ws://LAPTOP_IP:8765 --debug
   ```
4. From the laptop dashboard: **Start Session** → calibration → speak → hear the
   reply → **Stop Session**.

> **App coordination:** confirm the app accepts `device_type: "pi_zero_2w"` (or
> maps unknown types to a generic audio device). No protocol changes needed for
> v1. Echo/gating from the Pi 5 applies; keep calibration + post-playback
> mute/recovery as-is first, tune thresholds only if the new physical layout
> changes echo levels.

**Checkpoint:** full round trip works — HELLO → calibration → speak → PLAY_AUDIO
→ PLAYBACK_COMPLETE.

## Phase 6 — Battery baseline + trims

Follow [docs/battery.md](docs/battery.md). Measure idle vs streaming current
**before** optimizing, then apply OS trims. Defer amp SD_MODE / wake-button /
idle-disconnect policy until v1 works.

**Checkpoint:** recorded idle + streaming current numbers; OS trims applied
without breaking sessions.

---

## Out of scope for v1

- Moving OpenAI keys / Realtime onto the Zero.
- Barge-in / AEC (same hardware echo limits as the Pi 5).
- Fuel-gauge battery % (`battery_percent` stays `None` until hardware exists).
- Bluetooth transport — a future BT-PAN experiment to drop the hotspot; see the
  Bluetooth note in [docs/battery.md](docs/battery.md). Stay on Wi-Fi for v1.

## If you get stuck

| Phase | First thing to check |
| --- | --- |
| Card not detected (2) | overlay name + `dtparam=i2s=on`; reboot; `dmesg` |
| Silent capture (3) | mic DOUT on GPIO20, mic SEL pin tie |
| No playback (3) | amp power, DIN on GPIO21, GAIN/volume, speaker rating |
| Wrong card number (2/3) | onboard/HDMI audio grabbing card 0 → `dtparam=audio=off` |
| Client won't connect (5) | same network? laptop firewall? `ping LAPTOP_IP` |
| No speech detected (5) | calibration RMS thresholds in `audio_gating.py` |

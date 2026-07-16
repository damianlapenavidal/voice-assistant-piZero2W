# Battery efficiency (device-side)

Goal: keep capture, playback, calibration, and heartbeats working while cutting
idle draw so the Zero 2W runs off a LiPo. Measure first, trim second — don't
guess.

## Power source

- Prefer a **3.7 V LiPo + boost/charger HAT** sized for peak current: Wi-Fi TX
  bursts **and** the class-D amp transient can coincide. A weak boost converter
  browns out and reboots the Zero mid-session.
- Confirm the converter's peak-current rating covers `Wi-Fi TX + amp at target
  volume`. This interacts with the 3 Ω speaker warning in
  [hardware_gpio_i2s.md](hardware_gpio_i2s.md): a 3 Ω load at 5 V is a big amp
  transient.

## Measure baseline before optimizing

Take current readings (USB power meter inline, or the HAT's fuel gauge if
present) in three states:

1. **Idle** — client connected, no session.
2. **Streaming** — session active, mic capturing + speaker playing.
3. **Deep idle** — client disconnected / Wi-Fi powersave (for comparison).

Record the numbers; every trim below should be judged against them.

## OS / board trims (Raspberry Pi OS Lite, Bookworm)

- Start from the **Lite** image (no desktop) — already the biggest win.
- Disable unused services:
  ```bash
  sudo systemctl disable --now bluetooth hciuart   # if BT unused
  ```
- Disable HDMI/analog audio conflict and headless display power:
  ```ini
  # /boot/firmware/config.txt
  dtparam=audio=off
  dtoverlay=vc4-kms-v3d,noaudio
  ```
- CPU governor to `ondemand` (or `powersave` if thermby/battery-bound):
  ```bash
  echo ondemand | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
  ```

## Client behavior (already battery-friendly)

The client is designed so the biggest idle cost — an always-on `arecord` — never
happens:

- **`arecord` does not run until `START_AUDIO_STREAM`.** No always-listening
  capture. This is the single largest win vs a naive design.
- On `STOP_AUDIO_STREAM` the capture/playback subprocesses are **fully stopped**.
- The WebSocket stays connected for low-latency sessions.

Future (app-coordinated, only after basic bring-up works):

- Longer `DEVICE_STATUS` heartbeat interval when not recording.
- Disconnect-until-wake (button) instead of holding the socket.
- Drive amp **SD_MODE** from a GPIO so the amp sleeps when idle. **Avoid**
  Adafruit's continuous silent `aplay /dev/zero` click-fix service on battery —
  it keeps the amp powered 24/7.

## Wi-Fi

- Same network as the laptop; a DHCP reservation for the Zero keeps its IP
  stable.
- **Wi-Fi powersave can drop the WebSocket mid-session.** Tune only after
  measuring idle current — the savings are often not worth the dropouts:
  ```bash
  iw dev wlan0 set power_save off     # test stability first
  ```

## Future: dropping the hotspot with Bluetooth

If the motivation is removing the phone hotspot from the loop (Zero talks
straight to the laptop) or saving radio power, a direct **Zero↔laptop BT-PAN
link** could carry the same WebSocket. Caveats before trusting it:

- The audio is ~**384 kbps each way, continuous** (24 kHz PCM16 mono). BR/EDR
  gives ~1–1.5 Mbps real-world and 50–150 ms latency, so duplex audio is right
  at the edge and prone to dropouts.
- The alternative — standard BT audio profiles (A2DP/HFP) — would make the Zero a
  generic BT speaker/headset and **discard the thin-client protocol, calibration,
  and gating**. That's a redesign, not a config change.

Verdict: **stay on Wi-Fi for v1.** Prototype BT-PAN later and compare measured
latency + battery against the Wi-Fi baseline above before adopting it.

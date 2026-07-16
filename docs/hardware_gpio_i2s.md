# Hardware: GPIO I2S mic + class-D amp

Target hardware for the Zero 2W: an **I2S MEMS microphone** (e.g. SPH0645 /
ICS-43434-class) and an **I2S class-D amplifier** (MAX98357A-class) driving a
small speaker. I2S is a synchronous serial audio bus; the mic and amp share the
bit clock and word-select lines and use separate data lines.

> If your breakout boards differ, only the `config.txt` overlay and the `.env`
> card numbers change — the client always targets ALSA device strings.

## Pins (physical BCM numbering, MAX98357A + I2S mic)

The Zero 2W exposes the same 40-pin header as other Pis. Typical wiring:

| Signal | BCM | Header pin | Mic | Amp |
| --- | --- | --- | --- | --- |
| I2S bit clock (BCLK) | GPIO18 | 12 | BCLK / SCK | BCLK |
| I2S word select (LRCLK/FS) | GPIO19 | 35 | LRCL / WS | LRC |
| I2S data in (mic → Pi) | GPIO20 | 38 | DOUT | — |
| I2S data out (Pi → amp) | GPIO21 | 40 | — | DIN |
| 3V3 power | — | 1 or 17 | 3V | Vin (if 3V3) |
| 5V power | — | 2 or 4 | — | Vin (5V, louder) |
| Ground | — | 6/9/14/… | GND | GND |

Notes:

- **BCLK (GPIO18) and LRCLK (GPIO19) are shared** by the mic and amp — wire both
  to the same pins.
- The mic's **SEL** pin sets which channel (L/R) it drives; tie it per the
  breakout's datasheet (commonly GND for the left slot).
- SPH0645-class mics need `3V3`. MAX98357A runs 2.5–5.5 V; **5 V gives more
  output power** but check your supply's headroom (see amp/speaker note below).
- The MAX98357A **GAIN** pin sets output gain; leave floating for the default
  9 dB or set per datasheet.

## Enable the I2S card (config.txt)

On Bookworm the file is `/boot/firmware/config.txt`. Prefer **one overlay that
owns the I2S bus** rather than stacking a separate Adafruit mic overlay and a
separate DAC overlay — they fight over `bcm2835-i2s`.

For a mic + MAX98357A pair, the `googlevoicehat-soundcard` overlay commonly
provides simultaneous capture + playback on one card:

```ini
# /boot/firmware/config.txt
dtparam=i2s=on
dtoverlay=googlevoicehat-soundcard

# Free the I2S pins / avoid the onboard audio grabbing default:
dtparam=audio=off
```

Alternatives depending on your boards:

- `dtoverlay=hifiberry-dac` (playback only, MAX98357A) **plus** a mic overlay —
  avoid unless you specifically need it; two overlays often conflict.
- `dtoverlay=i2s-mmap` may be needed for some capture paths.

Reboot after editing, then verify with [alsa_bringup.md](alsa_bringup.md).

## Disable conflicting onboard audio

The Zero 2W has no analog jack, but the KMS/HDMI audio node can still claim
"card 0" and shuffle your I2S card number. Keep the I2S card deterministic:

```ini
dtparam=audio=off
dtoverlay=vc4-kms-v3d,noaudio
```

## Speaker / amp rating — read before powering a 3 Ω speaker

Many class-D amps (including several MAX98357A breakouts) are **specified for a
≥ 4 Ω load**. Running a **3 Ω** speaker:

- draws more current than the amp's rated output and can push it past its Safe
  Operating Area, especially at 5 V and high volume;
- stresses the 5 V supply (a Zero 2W + Wi-Fi TX + amp transient can brown out a
  weak boost converter).

Options, in order of preference:

1. Use a **4 Ω (or 8 Ω) speaker** — simplest and safest.
2. Keep the 3 Ω speaker but **cap the volume / GAIN** and confirm the amp
   datasheet's minimum load and your supply's peak-current rating.
3. Add a **small series resistor** to raise the effective load (wastes some
   power, but keeps the amp in spec).

Do not run a 3 Ω speaker at full volume on a 5 V rail without checking the amp's
minimum-load spec first.

## Amp shutdown (SD_MODE) — optional, for battery

If your amp exposes **SD_MODE / shutdown**, you can later drive it from a spare
GPIO so the amp sleeps when nothing is playing. Deferred until basic bring-up
works — see [battery.md](battery.md). Avoid Adafruit's continuous silent
`aplay /dev/zero` click-fix service on battery; it keeps the amp awake 24/7.

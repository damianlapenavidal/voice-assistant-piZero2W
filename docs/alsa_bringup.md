# ALSA bring-up checklist

Do this **before** trusting the client. Goal: confirm the I2S card can capture
and play 24 kHz / S16_LE / mono, and learn the `plughw:X,0` strings for `.env`.

## 1. Install tools

```bash
sudo apt install alsa-utils
```

## 2. Confirm the card appears

After enabling the overlay (see [hardware_gpio_i2s.md](hardware_gpio_i2s.md)) and
rebooting:

```bash
arecord -l    # capture devices — look for your I2S card
aplay -l      # playback devices
```

You should see the I2S card (e.g. `card 0: sndrpigooglevoi [snd_rpi_googlevoicehat_soundcard]`).
Note the **card number** and **device number** (`card X, device 0`). With a
single duplex I2S card, the same `X` appears in both `arecord -l` and `aplay -l`.

If nothing shows:

- re-check the overlay name and `dtparam=i2s=on` in `/boot/firmware/config.txt`;
- `dmesg | grep -iE "i2s|asoc|simple-card|voicehat"` for driver errors;
- make sure you didn't stack two overlays that both claim `bcm2835-i2s`.

## 3. Playback test (amp + speaker)

```bash
# Tone/noise via speaker-test (Ctrl+C to stop)
speaker-test -D plughw:0,0 -c 1 -r 24000 -t wav

# Or play a known file
aplay -D plughw:0,0 -f S16_LE -r 24000 -c 1 -t raw assets/say_hello_prompt.pcm
```

Replace `0,0` with your `card,device`. If you hear nothing: check amp power,
GAIN, wiring of DIN/BCLK/LRCLK, and the speaker/amp rating notes.

## 4. Capture test (mic)

```bash
arecord -D plughw:0,0 -f S16_LE -r 24000 -c 1 -t raw -d 3 /tmp/cap.raw
ls -l /tmp/cap.raw          # should be ~144000 bytes for 3 s (24000*2*3)
```

A file near zero bytes or all-silence means the mic data line (GPIO20) or SEL
pin is wrong.

## 5. Loopback (the real proof)

Record 2 s then play it straight back through the amp:

```bash
arecord -D plughw:0,0 -f S16_LE -r 24000 -c 1 -t raw -d 2 /tmp/lb.raw
aplay   -D plughw:0,0 -f S16_LE -r 24000 -c 1 -t raw /tmp/lb.raw
```

You should hear your recorded voice. This exercises the exact format the client
uses (24 kHz, S16_LE, mono).

## 6. Install the ALSA plugin chains

The client does not touch samples itself — ALSA applies both gains and selects
the mic's channel. That is far cheaper (the two per-sample loops in Python cost
~25% of one core while the assistant spoke) and, for volume, more responsive:
`softvol` sits downstream of `aplay`'s buffer, so a change reaches audio
already committed to it.

```bash
cp config/asoundrc.softvol ~/.asoundrc
```

Edit the card name in it if yours differs from `sndrpigooglevoi` — it appears
in every `slave.pcm` and `control.card`. Then check both directions:

```bash
# playback
aplay -D softvol_out -f S16_LE -r 24000 -c 1 -t raw assets/say_hello_prompt.pcm
amixer -c 0 sget PCM       # 0-100, -51.00 dB .. 0.00 dB
amixer -c 0 cset name='PCM Playback Volume' 40   # audibly quieter

# capture: mono out of a 2-channel card, and a control that boosts
arecord -D mic_in -f S16_LE -r 24000 -c 1 -t raw -d 3 /tmp/mic.raw
ls -l /tmp/mic.raw                                # 144000 bytes for 3 s
amixer -c 0 cset name='Mic Capture Volume' 231    # ~20x, the deployed gain
```

Neither control exists until its plugin has been opened once, so run the
`aplay`/`arecord` before the `amixer`. (The client handles that case itself, by
opening the silent `softvol_prime` / `mic_prime` PCMs.)

## 7. Write .env

```bash
cp .env.example .env
```

Set the strings you just validated:

```ini
AUDIO_INPUT_DEVICE=mic_in
AUDIO_OUTPUT_DEVICE=softvol_out
AUDIO_MIXER_CARD=0
```

Both chains keep a `plughw:` underneath (not raw `hw:`), so ALSA still does any
needed rate/format conversion. If capture and playback are different cards on
your board, set `AUDIO_MIC_MIXER_CARD` as well.

To run without the plugins, point both back at `plughw:0,0`. Audio still works,
but at fixed levels: the sliders do nothing, and capture gets the halved
downmix described in [audio_capture.py](../audio_capture.py).

## Common issues

| Symptom | Likely cause |
| --- | --- |
| Card not in `-l` output | overlay wrong / not applied / needs reboot |
| `Device or resource busy` | two overlays or a service holding the card |
| Recording is silent | mic DOUT not on GPIO20, or SEL mis-tied |
| No playback sound | amp unpowered, DIN not on GPIO21, GAIN/volume |
| Distorted/clipping playback | 3 Ω speaker at high volume — see amp rating notes |
| Wrong card number after reboot | onboard/HDMI audio grabbing card 0 — `dtparam=audio=off` |
| `aplay: No such device` on `softvol_out` | `~/.asoundrc` missing, or its card name doesn't match `aplay -l` |
| Volume slider does nothing | `AUDIO_MIXER_CARD`/`AUDIO_MIXER_CONTROL` don't match `~/.asoundrc` — check the client log for `amixer exited with code` |

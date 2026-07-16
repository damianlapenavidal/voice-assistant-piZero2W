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

## 6. Write .env

```bash
cp .env.example .env
```

Set the strings you just validated:

```ini
AUDIO_INPUT_DEVICE=plughw:0,0
AUDIO_OUTPUT_DEVICE=plughw:0,0
```

Use the `plughw:` prefix (not raw `hw:`) so ALSA does any needed rate/format
conversion. If capture and playback are different cards on your board, set them
accordingly.

## Common issues

| Symptom | Likely cause |
| --- | --- |
| Card not in `-l` output | overlay wrong / not applied / needs reboot |
| `Device or resource busy` | two overlays or a service holding the card |
| Recording is silent | mic DOUT not on GPIO20, or SEL mis-tied |
| No playback sound | amp unpowered, DIN not on GPIO21, GAIN/volume |
| Distorted/clipping playback | 3 Ω speaker at high volume — see amp rating notes |
| Wrong card number after reboot | onboard/HDMI audio grabbing card 0 — `dtparam=audio=off` |

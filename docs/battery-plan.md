# Battery-efficiency work plan

**Demo: Wednesday 2026-07-29.** Baseline frozen 2026-07-25. The demo runs the
frozen baseline — no battery work ships before it.

Companion to [battery.md](battery.md), which covers OS/board trims. This file
covers the *software* work: moving processing off the device and cutting radio
time. Read [battery.md](battery.md) first for the measure-before-you-trim rule,
which still applies.

---

## 1. Recoverability (done 2026-07-25)

Everything below already exists. This section is the drill, not a to-do.

### What was captured

| Repo | Baseline tag | Commit | Pushed |
| --- | --- | --- | --- |
| `voice-assistant-app` | `demo-baseline-2026-07-25` | `dae9c4c` | yes |
| `voice-assistant-pi5` | `demo-baseline-2026-07-25` | `1d2beb0` | yes |
| `voice-assistant-piZero2W` | `demo-baseline-2026-07-25` | `ab68f3d` | yes |

All three tags point at their repo's `main` HEAD. The Pi Zero 2W is on
`ab68f3d`, so app and device are in sync.

> These are **commit** SHAs, which is what `git rev-parse HEAD` reports. The
> tags are annotated, so `git rev-parse demo-baseline-2026-07-25` returns a
> different id (the tag object). Under time pressure that looks like a mismatch
> and it isn't — dereference with `git rev-parse demo-baseline-2026-07-25^{}`
> to compare like with like.

Two app commits were made to turn previously-uncommitted work into a real
rollback point: `6b2d0ba` (Realtime pre-warm at HELLO_ACK) and `dae9c4c`
(dashboard readiness signal). Tests: **267 passing** app-side, **39** device-side.

### Offline backup

`../_voice-assistant-BACKUP-2026-07-25/` — outside all three repos, so no git
operation can touch it:

- `*.bundle` — complete clonable copies of all three repos, all refs, verified.
- `gitignored-configs/` — the files that exist in **no** repo and would
  otherwise be unrecoverable:
  - `app--config--targets.local.env` — SSH hosts, SSIDs, verified ALSA config
  - `app--dotenv-CONTAINS-OPENAI-KEY` — **contains a live secret**, mode `700`
  - `pizero2w-DEVICE--dotenv` — the Pi's hand-tuned `INPUT_GAIN=20.0`,
    `PLAYBACK_GAIN=1.0` and card IDs. This lives **only on the Pi**.
  - `pizero2w-DEVICE--systemd-and-asoundrc`

> The backup directory holds an OpenAI key in plaintext. Don't commit it, don't
> sync it to a shared drive, and delete it once the demo is past.

### Rollback drill — rehearse this before Wednesday, don't improvise it

**Device** (the Pi cannot roll back by pulling — `deploy.sh` uses
`git pull --ff-only`, so going *backwards* needs an explicit checkout):

```bash
ssh voice-assistant-pizero2w
cd ~/voice-assistant-piZero2W
git fetch --tags origin
git checkout demo-baseline-2026-07-25      # detached HEAD; that is fine
systemctl --user restart voice-assistant-pizero2w
systemctl --user status voice-assistant-pizero2w --no-pager
```

To return the Pi to normal tracking afterwards: `git checkout main`.

**App** (Mac):

```bash
cd ~/Desktop/CODE/VS_Cursor/voice-assistant-app
git checkout demo-baseline-2026-07-25
```

**A config file got mangled:**

```bash
BK=~/Desktop/CODE/VS_Cursor/_voice-assistant-BACKUP-2026-07-25/gitignored-configs
cp "$BK/app--config--targets.local.env" config/targets.local.env
scp "$BK/pizero2w-DEVICE--dotenv" voice-assistant-pizero2w:~/voice-assistant-piZero2W/.env
```

**Total loss of a repo:**

```bash
git clone ../_voice-assistant-BACKUP-2026-07-25/voice-assistant-app.bundle recovered-app
```

### Pre-demo checklist — Tuesday, not Wednesday morning

1. `config/targets.local.env` has `PIZERO2W_BRANCH="main"` ← **the most likely
   way to demo the wrong code**
2. All three repos on `main`, `git status` clean
3. Pi HEAD matches the baseline:
   `ssh voice-assistant-pizero2w 'git -C ~/voice-assistant-piZero2W rev-parse --short HEAD'` → `ab68f3d`
4. App HEAD is `dae9c4c`
5. Pi `.env` matches `gitignored-configs/pizero2w-DEVICE--dotenv`
6. Full launcher run + a real end-to-end voice round trip, start to finish
7. Battery charged; spare power bank

---

## 2. Working model

**The trap:** `PIZERO2W_BRANCH="main"` in `config/targets.local.env` means the
launcher pulls `main` onto the Pi. Anything merged to `main` and pushed lands on
the device at the next launch. So `main` stays demo-clean until Wednesday is
over.

**Develop on branches.** To test a branch on real hardware, edit
`PIZERO2W_BRANCH` in `config/targets.local.env` — it is gitignored and
Mac-local, so this is a one-line change that commits nothing:

```bash
PIZERO2W_BRANCH="battery/alsa-offload"
```

Set it back to `"main"` when done. Checklist item 1 exists because forgetting
this is the single most likely demo failure.

**Branch names:** `battery/alsa-offload` (2a, done),
`battery/alsa-capture-route` (2b + 2c, done, stacked on 2a),
`battery/mic-gain-to-app` (Phase 3 — but see the note there first),
`battery/binary-frames`.

Neither Phase 2 branch is pushed, and the device is back on the baseline
commit with its original `.env` and no `~/.asoundrc`. To put Phase 2 back on
the hardware: `scp config/asoundrc.softvol voice-assistant-pizero2w:~/.asoundrc`
and set `AUDIO_INPUT_DEVICE=mic_in`, `AUDIO_OUTPUT_DEVICE=softvol_out` plus the
`AUDIO_*MIXER_*` keys from `.env.example`.

**The code is duplicated, not shared.** `audio_capture.py`,
`audio_playback.py`, `audio_gating.py` and `calibration_prompt.py` exist as
independent copies in `voice-assistant-pi5` and `voice-assistant-piZero2W`, and
they have **already diverged** (pi5 has a mic-gain clipping cap the Zero
doesn't). Do not attempt to extract a shared module before the demo — it is a
bigger job than it looks and touches every file in both device repos. Land
changes on the Zero first; port to the Pi 5 deliberately, afterwards.

---

## 3. Measured baseline

Measured on the device 2026-07-25 (not estimated — `timeit` against the real
functions with a real-size chunk):

| Path | Per 100 ms chunk | Sustained cost |
| --- | --- | --- |
| `_left_channel_with_gain` @ `INPUT_GAIN=20.0` | 15.16 ms | **15.2% of one core** |
| `_left_channel_with_gain` @ gain 1.0 (loop skipped) | 0.10 ms | 0.1% |
| `_apply_gain` @ `PLAYBACK_GAIN=0.35` | 10.24 ms | **10.2% of one core** |
| **Combined, while the assistant is speaking** | | **25.4% of one core** |

Two things follow. First, the entire cost is the per-sample Python loop — at
gain exactly 1.0 the guard skips it and the work vanishes, so this is pure
interpreter overhead, not real signal processing. Second, this is ~3× higher
than I estimated from reading the code, which moves the DSP items from
"worthwhile" to "the obvious first target".

**Still needed:** actual current draw (idle / streaming / deep idle) per
[battery.md](battery.md#measure-baseline-before-optimizing). The CPU numbers
above justify the DSP work on their own, but only a power meter can confirm the
radio-vs-CPU ratio that decides whether Phase 4 is worth its cost.

### Measured after Phase 2 (2026-07-26)

Same device, whole-process CPU sampled from `/proc/<pid>/stat` across a 12 s
window of live streaming (122 frames, 10.1/s) rather than `timeit` on one
function — so this includes base64, JSON and the WebSocket, not just the DSP:

| Path | Baseline | After Phase 2 |
| --- | --- | --- |
| Client process, streaming mic audio | **18.4%** of one core | **3.4%** |
| `arecord` (the ALSA side of the same work) | 2.02% | 1.94% |
| `aplay`, 10 s of audio | 0.64% | 0.65% |

The 15.0 points came out of Python and did **not** reappear in ALSA — the
plugin work is free within measurement noise, and `arecord` now carries half
the bytes (one channel, not two). Playback's ~10% went the same way in 2a.

> Note on the `timeit` figures above: they were taken on a zero-filled
> buffer, which never reaches the soft-knee branch. Real audio at
> `INPUT_GAIN=20` does, so 15.16 ms was if anything an under-estimate. The
> whole-process numbers here are the ones to trust.

### Constraints found while measuring

- **The Pi runs Python 3.13.5**, which **removed `audioop`**. The obvious C-speed
  fix is therefore unavailable, and `numpy` is not installed in the venv or
  system-wide. This is why Phase 2 goes through ALSA rather than a faster
  Python path.
- **`calibration_prompt.py`'s espeak fallback is already dead code.** It imports
  `audioop` (removed in 3.13) *and* neither `espeak-ng` nor `espeak` is
  installed. If `assets/say_hello_prompt.pcm` were ever missing or truncated,
  calibration would fail with an unhandled `ImportError` rather than the
  intended clean error. The asset is currently present (65224 bytes, 1.36 s), so
  this is latent, not live. See Phase 2c.

---

## 4. The work

Ordered by payoff per unit of risk. Nothing here ships before Wednesday.

### Phase 1 — Power baseline (do first)

Inline USB power meter, three readings per [battery.md](battery.md): idle,
streaming, deep idle. Record them in [battery.md](battery.md). Everything below
is judged against these numbers, and Phase 4's cost is only justifiable if the
radio dominates as expected.

Effort: 30 min. Risk: none.

### Phase 2 — Push DSP into ALSA (device-only, no protocol change)

**Done 2026-07-26**, on `battery/alsa-offload` (2a) and
`battery/alsa-capture-route` (2b, 2c, branched off 2a since both edit the same
`config/asoundrc.softvol`). Unmerged and unpushed — `main` stays demo-clean.
Results in §3; what actually happened, per item, below.

Removes most of the 25.4% with no cross-repo coordination. Highest
payoff-to-risk on the list.

**2a. Playback gain → ALSA `softvol`.** Delete `_apply_gain` from the write
path; create a `softvol` plugin in `~/.asoundrc` and drive it from `SET_VOLUME`
via `amixer`. Recovers ~10.2%.

Bonus: softvol applies at the ALSA layer, so a volume change affects audio
*already buffered in aplay* — strictly more responsive than the current
per-sub-chunk approach, which can only affect audio not yet written. The write
pacing in [audio_playback.py:202-229](../audio_playback.py#L202-L229) can then be
simplified, since its whole purpose is bounding how much pre-scaled audio is
committed downstream.

Cost: an `amixer` subprocess per slider move (rare, and free during playback).

**2b. Left-channel extraction → ALSA `route` plugin.** Recovers the remaining
~5% and the stereo→mono copy. A `route` plugin selects the left slot in C,
avoiding both the Python loop and the `plughw` downmix that halves the signal
(the reason for capturing 2 channels in the first place — see
[audio_capture.py:17-22](../audio_capture.py#L17-L22)).

Keep this on the device. Sending both channels to let the app do it would
**double the payload** to carry a slot that is silent by design.

> **As built, 2b did more than this — and the "~5%" above was wrong.** The
> table in §3 says it plainly: `_left_channel_with_gain` costs 15.16 ms per
> chunk at `INPUT_GAIN=20` but 0.10 ms with the gain loop skipped. The
> extraction is **0.1%** of a core; the gain is the other 15%. Shipping only
> the `route` plugin would have recovered nothing worth measuring.
>
> So the mic gain moved into ALSA too, as a second `softvol` — which boosts as
> readily as it attenuates (`max_dB +34` measured at exactly 50.1×). That
> keeps the whole capture cost off Python **without** a protocol change, and
> without touching calibration: Python still receives post-gain audio, so
> every threshold in [audio_gating.py](../audio_gating.py) stays valid. See
> Phase 3, whose reason for existing this changes.
>
> Two behaviour changes, both accepted deliberately: capture saturates where
> Python soft-limited (measured, that knee only ever engaged on `arecord`'s
> first-chunk transient — raw peak ~1755 against ~100 steady-state — which
> calibration's quiet-phase guard already discards), and softvol's 0.33 dB
> steps make the deployed gain 19.952× rather than 20.0×, a 0.24% level shift.

**2c. Delete the espeak fallback.** It cannot run (§3). Replace it with a clear
startup error if the asset is missing. Keep the asset itself on the device —
it is 65 KB played from local flash at zero radio cost, and moving it to the app
would *add* a 65 KB transfer per session.

> Done. A missing or truncated asset now raises `CalibrationPromptError`,
> which the prompt path already reports to the app as a recoverable
> `SPEAKER_ERROR`, and startup logs the asset line at ERROR instead of INFO.
> Deliberately **not** fatal: a resumed session skips calibration, so a device
> with a damaged asset is degraded rather than useless.

Effort: half a day, mostly `.asoundrc` iteration on real hardware. Risk: medium
— ALSA plugin config is fiddly and this specific card
(`plughw:CARD=sndrpigooglevoi`) has no `~/.asoundrc` today, so it is all new.
Fully revertible: delete `~/.asoundrc`, restart the service.

**How it was verified** (all on the device, both directions):

- Plugin arithmetic pinned against the real plugins by pushing a known signal
  through them into a file and reading back every index — that is where the
  numbers in `gain_to_softvol_index`'s tests come from, rather than from the
  implementation.
- Capture path A/B'd against the Python path on the *same* mic audio: RMS
  1206.5 vs 1210.0, peak 12809 vs 12840, mean per-sample error 0.216% (the
  step quantisation, not drift). Repeated live with the gain stage bypassed to
  separate the two stages: ratios 1.02 / 0.98, then 1.05 / 0.96 with gain.
- A full session driven end-to-end from a control-absent (fresh boot) state:
  both controls created on demand and set to the exact expected indexes,
  calibration completed (`noise_floor` 275.7 against a baseline cluster of
  201–276), 25 `AUDIO_FRAME`s of 4800 bytes at matching RMS.
- 41 tests pass on the device.

### Phase 3 — Mic gain → app

> **Re-decide this before starting it. Phase 2b already banked the 15.2%,**
> in C on the device, with no protocol change and no calibration coupling.
> What is left of Phase 3 is not a battery argument:
>
> - *Fidelity* — the device still re-quantizes int16 after scaling, so the
>   rounding step below is real. It is also inaudible at these levels, and
>   would be better addressed by capturing S32 than by moving the gain.
> - *Architecture* — "the app owns processing" is a defensible principle, but
>   it now costs a mandatory capability negotiation and a threshold rescale to
>   buy nothing measurable.
>
> Against that, doing it means **undoing** working, verified device code and
> re-introducing the coupling described below. My recommendation: drop it, and
> put the effort into Phase 4, which the measurements say is the real prize.
> Keeping it would need a reason that isn't CPU.

Recovers the ~15.2% capture cost. The device sends the raw left channel; the app
applies the multiplier and the soft-knee limiter.

Slightly *better* fidelity, too: scaling already-quantized int16 on-device and
re-quantizing before transmit adds a rounding step the app avoids.

**The coupling, and how to avoid it.** Calibration measures RMS *after* gain, so
naively moving gain breaks every threshold in
[audio_gating.py](../audio_gating.py) (they are tuned for post-gain levels, and
`INPUT_GAIN=20.0` means raw audio is 20× quieter). This does **not** require
moving calibration as well: the device still knows the gain value even when it
no longer applies it, so it can compare raw RMS against `threshold / gain`. That
is mathematically equivalent in the linear region, and calibration thresholds
(noise floor ~400, voice peak capped at floor+1200) sit far below the limiter
knee, so the non-linearity never bites.

**Version negotiation is mandatory.** A baseline device sends gained audio; a
Phase-3 device sends raw. The app cannot tell them apart from the bytes. Add a
capability flag to `HELLO` (e.g. `"raw_capture"` in `capabilities`) and have the
app apply gain only when it sees it. Without this, a Pi 5 still on baseline code
paired with a Phase-3 app produces audio 20× too quiet, and the failure looks
like a hardware fault.

Effort: 1 day across two repos. Risk: medium — the version negotiation is the
part to get right.

### Phase 4 — Binary frames (post-demo, largest single win)

Each 100 ms chunk is 4800 bytes of PCM that goes out as ~6560 bytes after base64
and the JSON wrapper — **~27% of radio time spent on encoding overhead**, both
directions. Nothing else on this list is close.

Keep JSON for control messages; send `AUDIO_FRAME` and `PLAY_AUDIO` as binary
WebSocket frames with a short header (type + sequence + timestamp). The app's
transport already has a `bytes` branch at
`websocket_transport.py:136`, so the receive side is half-built.

**Why this is post-demo:** BRINGUP.md explicitly freezes the message schema, and
`docs/protocol.md` (607 lines) plus both device repos and the Pi 5 all encode
it. This needs the same capability negotiation as Phase 3, because the Pi 5 and
the Zero will migrate at different times.

Effort: 2-3 days across three repos + docs. Risk: high. Payoff: highest.

### Phase 5 — Radio duty cycle (post-demo)

**5a. Heartbeat interval.** Already flagged as future work in
[battery.md:58](battery.md#L58). The app can derive `is_recording` from its own
state and `cpu_temp` only matters during a session, so this could become
event-driven rather than a radio wake every 10 s, forever.

Effort: 1 hour. Risk: low.

**5b. Elide silence.** The device streams continuous PCM including silence
because OpenAI's server VAD needs it
([zero2w_client.py:643-651](../zero2w_client.py#L643-L651)) — but the *app* can
synthesize those zeros. The threshold is already computed and currently unused.

Two caveats, both real. Radio power tracks *wake frequency* nearly as much as
byte count, so the win only materialises if silence markers are batched (say one
per second), which adds up to 1 s of latency to end-of-turn detection. And the
app must reconstruct the timeline faithfully or turn-taking breaks — the most
demo-visible failure mode on this list.

Effort: 1-2 days. Risk: high. Do it last, behind a flag.

---

## 5. Timeline

| Day | Work |
| --- | --- |
| **Sat 25 Jul** | Phase 2a done and verified on hardware. Phase 1 **not done** — still needs the power meter. |
| **Sun 26 Jul** (today) | Phase 2b + 2c done and verified. CPU delta measured (§3). Phase 1 still outstanding. |
| **Mon 27 Jul** | Phase 1 power baseline — it is now the only unmeasured thing on this list, and Phase 4's justification depends on it. Re-decide Phase 3. **Do not merge to `main`.** |
| **Tue 28 Jul** | **Hard freeze.** Pre-demo checklist (§1). Rollback drill. Full dry run. Nothing else. |
| **Wed 29 Jul** | Demo the baseline. |
| **After** | Merge what's proven. Then Phase 4, Phase 5. Then port to the Pi 5. |

Tuesday is a freeze-and-rehearse day, not a development day. If Phase 2 is not
finished and verified by Monday night, it waits until after the demo — the
branch keeps.

---

## 6. Loose ends

- `../voice-assistant-pi5-calibration-fix.patch` sits unversioned in the parent
  directory. Unknown provenance; check whether it is already applied before the
  Pi 5 port.
- The `demo-baseline-2026-07-25-uncommitted` tag in the app repo is now
  redundant (that work is committed as `6b2d0ba`/`dae9c4c`). Harmless; delete
  after the demo.
- `CalibrationPhase` is imported but unused at
  [zero2w_client.py:35](../zero2w_client.py#L35).
- `speech_start_threshold()` is computed and shipped to the app but never used
  on-device — despite the module name, `audio_gating.py` does calibration only,
  and runtime gating is entirely the app's `MUTE_MIC`/`UNMUTE_MIC`. Worth
  correcting the "echo gating" claim in [README.md:88-89](../README.md#L88-L89).
- `config/targets.local.env` records `PIZERO2W_SAMPLE_RATE="48000"` and
  `PIZERO2W_CAPTURE_FORMAT="S32_LE"`, but the client hardcodes 24 kHz / `S16_LE`
  ([audio_capture.py:13-14](../audio_capture.py#L13-L14)). Those keys appear to
  feed `scripts/audio-diagnostic.sh` rather than the client — worth confirming
  before Phase 2, since ALSA plugin config interacts with both.

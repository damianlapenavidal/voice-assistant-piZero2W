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

### Echo and voice levels (2026-07-26)

Measured to answer one question: can the mic stay open while the assistant
speaks, so the user can interrupt? All figures are mic RMS at the deployed
`INPUT_GAIN=20`, i.e. the numbers the client itself sees. The stimulus is the
prompt asset — real speech, which is what TTS sounds like.

| Playback volume | Plugin | Ambient | Echo RMS | Echo 20 ms peak | Over ambient |
| --- | --- | --- | --- | --- | --- |
| **100** (deployed `PLAYBACK_GAIN=1.0`) | 0.0 dB | 519 | **8241** | ~25 500 | **+24.0 dB** |
| 83 (app slider 60%) | −8.7 dB | 521 | 3359 | 11 792 | +16.2 dB |
| 76 (slider 50%) | −12.2 dB | 561 | 2314 | 8069 | +12.3 dB |
| 50 (slider ~22%) | −25.5 dB | 552 | 622 | 1080 | +1.0 dB |
| 22 | −39.8 dB | 544 | 521 | 729 | −0.4 dB |

Against a 6 s voice recording taken at the user's position:

| | RMS at 20× gain |
| --- | --- |
| ambient (quiet third) | 584 |
| speech (loudest quarter) | **1880** |
| loudest 100 ms window | 2577 |
| raw sample peak | 398 → 7941 after gain (**no clipping**) |

Speech sits only **+10.2 dB** over the room. Margin against the echo above:
**−12.8 dB at volume 100, −5.0 at 83, −1.8 at 76, +9.6 at 50, +11.1 at 22.**

Five things follow.

1. **At the deployed volume, a level threshold cannot work.** The echo is
   12.8 dB *louder* than the voice, and its 20 ms peaks (~25 500) are near
   full scale, so the capture path is clipping. Clipping is non-linear, which
   also puts it beyond what an echo canceller could subtract.
2. **Ducking is linear and has no crosstalk floor.** Predicted-versus-measured
   tracks within 0.8–1.6 dB down to −12 dB, and by −25 dB the echo has
   disappeared into the mic's own noise (+1.0 dB over ambient). Nothing leaks
   through the shared I2S bus electrically.
3. **You must duck to about −25 dB (index ~50) to hear the user** — which is
   nearly to silence. Continuous listening at a comfortable volume is not
   available from level thresholds alone.
4. **The acoustic tail is under 50 ms.** Even a burst stopping at full
   amplitude is back within 3 dB of ambient in the first 20 ms window. So an
   AEC filter would be short, and after ducking the mic is trustworthy almost
   at once. `PROMPT_SETTLE_SEC = 0.6` is conservative by roughly 4×.
5. **The amp is not the noise floor.** Opening a playback stream and feeding
   it pure zeros moved the mic floor by +0.2 dB. Gating the amp's SD_MODE pin
   would still save power, but it buys no SNR.

> **Two caveats on this data.** A 440 Hz tone burst produced ~8 dB *less* echo
> than speech at the same RMS — small drivers barely radiate 440 Hz, so tone
> tests understate this badly; only the speech figures are quoted above. And
> the voice sample is one recording of one (adult) speaker at one distance. A
> child at the same spot may well be quieter, so treat +10.2 dB as the
> optimistic end.

### Capture format (resolves the `S32_LE` loose end in §6)

`hw:0,0` is **S32_LE / 48 kHz / 2ch only**. When the client asks `plughw` for
S16_LE / 24 kHz, the hardware still runs S32/48k and `plug` resamples and
truncates to 16 bits — *before* route, softvol, or Python sees anything. So
the mic's quiet signal (peaks ~0.5% of full scale) is quantised to roughly 8
usable bits before any gain is applied, in every design considered so far.

That matters for everything below: SNR is the currency both barge-in and voice
gating spend. Applying the gain in the 32-bit domain — `route` and `softvol`
at S32/48k straight off `hw`, with `plug` on the *outside* converting after
the boost — would recover about 4 bits. It is a `.asoundrc` reordering now
that the chain exists. Untested; worth an hour before either phase below.

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

### Phase 3 — Mic gain → app ~~(planned)~~ **DROPPED 2026-07-26**

> **Decided: not doing this.** Phase 2b already banked the 15.2%, in C on the
> device, with no protocol change and no calibration coupling. What was left
> of Phase 3 was not a battery argument:
>
> - *Fidelity* — the device still re-quantizes int16 after scaling, so the
>   rounding step below is real. But §3 measured where the loss actually is:
>   `plug` truncates S32 to S16 *before* any gain, costing ~8 bits, which
>   moving the multiply to the app does nothing about. Capturing in the 32-bit
>   domain fixes it; Phase 3 does not.
> - *Architecture* — "the app owns processing" is defensible, but it costs a
>   mandatory capability negotiation and a threshold rescale to buy nothing
>   measurable. And gain expressed in `~/.asoundrc` is arguably *more* of a
>   thin peripheral than gain expressed in device Python.
>
> Against that, doing it would mean **undoing** working, verified device code
> and re-introducing the coupling described below. The effort goes to Phase 4,
> which the measurements say is the real prize.
>
> The section is kept rather than deleted so the reasoning survives — if
> someone proposes moving capture processing to the app again, this is why it
> was rejected, and what would have to change for the answer to differ.

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

Measured 2026-07-26, so this one is exact: 4800 bytes of PCM → 6400 base64 →
plus a 161-byte JSON envelope = **6561 bytes on the wire, 26.8% overhead**.

> **It is a byte figure, not a battery figure.** Airtime tracks bytes for the
> payload, but the radio stays associated and awake for the whole session, so
> how much of session *energy* is byte-proportional is unknown until Phase 1
> exists. Phase 4 is still the biggest lever on bytes by a wide margin; its
> payoff at the battery is unmeasured.

> **Do not sell this as a CPU win.** Measured on the device: base64 0.07% of a
> core, `json.dumps` 0.14% — **0.25% together**, against a client that spends
> 3.4% while streaming. The rest is websockets framing/masking, asyncio and
> I/O. Binary frames cut masked bytes by 27%, so expect perhaps 1% of a core
> in total. This is a radio optimisation.

Keep JSON for control messages; send `AUDIO_FRAME` and `PLAY_AUDIO` as binary
WebSocket frames with a short header (type + sequence + timestamp). The app's
transport already has a `bytes` branch at
`websocket_transport.py:136`, so the receive side is half-built.

**Design the header for Phase 6 too.** Barge-in's better options (6c
predicted-echo, 6d real AEC) need playback position / reference offset
alongside the sequence number. Leaving room for it now is nearly free;
retrofitting it means a second migration across three repos, which is exactly
the cost that makes this phase post-demo in the first place. For the same
reason, fold in Phase 6's `MUTE_MIC` change of meaning ("device stops sending"
→ "device keeps sending, app decides") — same Pi 5 versus Zero migration
problem, so it should ride the same capability flag rather than need a second.

**Why this is post-demo:** BRINGUP.md explicitly freezes the message schema, and
`docs/protocol.md` (607 lines) plus both device repos and the Pi 5 all encode
it. It needs capability negotiation, because the Pi 5 and the Zero will migrate
at different times.

> That negotiation used to be shared with Phase 3. **Phase 3 is dropped** (see
> the note there), so Phase 4 is now its first and only consumer and has to
> carry the whole cost rather than inherit it. The estimate below includes it.

Effort: 2-3 days across three repos + docs. Risk: high. Payoff: highest.

### Phase 5 — Radio duty cycle (post-demo)

**5a. Heartbeat interval — do this before Phase 4, not after.** Already flagged
as future work in [battery.md:58](battery.md#L58). The app can derive
`is_recording` from its own state and `cpu_temp` only matters during a session,
so this could become event-driven rather than a radio wake every 10 s, forever.

It is an hour of work, carries no schema change, and is the only item here that
pays off while the device is *idle* — which is most of its life. There is no
reason it should queue behind a 2-3 day high-risk protocol migration.

> One thing to preserve on the way: `DEVICE_STATUS` doubles as the app's
> liveness signal. Make it event-driven and the app can no longer tell a dead
> device from a quiet one. Lean on WebSocket `PING`/`PONG` at a longer interval
> instead — the client already answers `PING` today.

Effort: 1 hour. Risk: low.

> **Done 2026-07-29.** Checked the liveness caveat above against the app
> repo first: `voice-assistant-app` never actually sends an app-level `PING`
> today (the client's `PING`→`PONG` handler was, and remains, unused), and
> nothing in `session.py` times out on a missing `DEVICE_STATUS` — the
> dashboard just displays whatever `device_status` payload arrived most
> recently. The app's real liveness signal is the WebSocket connection object
> itself, kept alive by `websockets`' own low-level ping/pong (its default
> `ping_interval`/`ping_timeout`, unconfigured on either side, so both default
> to 20s). So event-driving `DEVICE_STATUS` needed **no app-side change** —
> the risk the caveat named doesn't apply to this codebase as it stands today.
>
> Implementation, device-only: `Zero2WClient` gained a `_status_event`
> (`asyncio.Event`), set on every `is_recording` transition in `_start_audio`/
> `_stop_audio`. `_status_loop` now blocks on that event indefinitely while
> idle (zero sends, zero radio wakes) and with a `STATUS_INTERVAL_SECONDS`
> timeout while recording (unchanged 10s cadence, since `cpu_temp` only
> matters mid-session) — either way sending the instant it wakes. 4 new tests
> in `test_audio.py` cover: silence while idle, immediate sends on both
> recording transitions, periodic sends while recording, and that
> `_start_audio`/`_stop_audio` actually set the event. 45/45 tests pass.

**5b. Elide silence.** The device streams continuous PCM including silence
because OpenAI's server VAD needs it
([zero2w_client.py:643-651](../zero2w_client.py#L643-L651)) — but the *app* can
synthesize those zeros. The threshold is already computed and currently unused.

Two caveats, both real. Radio power tracks *wake frequency* nearly as much as
byte count, so the win only materialises if silence markers are batched (say one
per second), which adds up to 1 s of latency to end-of-turn detection. And the
app must reconstruct the timeline faithfully or turn-taking breaks — the most
demo-visible failure mode on this list.

Three more, learned since:

> **Phase 7 strictly dominates this, and they must share one mechanism.** Both
> need to tell the app "there was a gap of N ms here" so it can rebuild the
> timeline for OpenAI's VAD. Phase 7's gate elides silence *and* room noise
> *and* other speakers — everything 5b does and more. Build the marker twice
> and you get two incompatible ways to skip audio. Either generalise 5b's
> silence marker into a segment marker Phase 7 reuses, or skip 5b's own
> threshold entirely and let Phase 7's gate do the eliding.

> **This re-introduces the per-sample Python loop Phase 2 just deleted.** The
> device computes no RMS while streaming today — only during calibration.
> Measured cost of adding one: **2.01% of a core** for a full RMS, **0.93%
> even for `max()` alone**, because `struct.unpack` over 2400 samples dominates
> before any arithmetic happens. `audioop` is gone (§3), so there is no C
> shortcut. That is up to 60% of the client's entire remaining streaming CPU,
> spent to save radio. Probably still the right trade given what this plan
> assumes about radio-versus-CPU — but it is a trade, not a freebie, and
> sampling every 4th sample halves it.

> **Phase 6 makes this harder.** If barge-in keeps the mic open during
> responses, the device streams *more*, and the child's audio interleaves with
> the assistant's own speech — so the timeline the app has to reconstruct is no
> longer a simple "silence here" gap.

Effort: 1-2 days. Risk: high. Do it last, behind a flag.

---

### Phase 6 — Barge-in: letting the user interrupt (post-demo)

The mic is muted while the assistant speaks, so the child cannot interrupt.
Fixing that is an echo problem, and §3 measured it.

**There is no hardware answer on this board.** The intuition — "we know what
we sent, subtract it" — fails because what reaches the mic is that signal
convolved with the enclosure (tens of ms of tail), delayed by aplay's buffer,
and distorted non-linearly by a class-D amp driving a 3 Ω speaker. The mic is
a digital I2S part, so there is no analog node to inject an inverted signal
into, and ALSA ships no echo canceller (`route` and `softvol` are per-sample
arithmetic on one stream; cancellation needs two, plus a delay estimate and an
adaptive filter). Real hardware AEC exists — XMOS VocalFusion-class parts that
take a loopback reference — but that is a new component, not a config change.

One thing about this build *is* favourable: mic and amp share BCLK and LRCLK,
so capture and playback are **sample-clock-locked** and the echo delay is
fixed. That is the hard part of AEC handed over for free, and it is why the
device is better-conditioned for AEC than the laptop, despite having ~50× less
CPU — over Wi-Fi the two streams have independent clocks and jitter.

Four approaches, cheapest first:

**6a. Listen in the gaps.** TTS is full of pauses. The tail is <50 ms, so
during any pause longer than ~150 ms the echo has already fallen to the noise
floor and the mic is usable at full volume. The app knows where the pauses are
— it holds the audio it is sending. No ducking, no AEC, no new hardware.
Effort: ~1 day, app-side. Risk: low. Detects only interruptions that begin
during a gap, which for a chatty model is most of them.

**6b. Duck-then-listen.** Drop the volume when the app wants to allow an
interruption; §3 says −25 dB clears the echo entirely, and 2a made a volume
change reach audio *already buffered in aplay*. The catch from §3 is that
−25 dB is nearly inaudible, so this is a brief "am I being interrupted?"
probe, not a listening posture. Effort: ~1 day across both repos. Risk: low.

**6c. Threshold against predicted echo.** Instead of a fixed threshold,
compare the mic against the echo the app *expects* right now — it has the
reference signal and, from §3, the coupling. Worth perhaps 6–10 dB over 6b
and costs no new dependency. Effort: 2–3 days. Risk: medium; needs the
reference aligned to what is actually leaving the speaker.

**6d. Real AEC.** speexdsp's canceller is the light option; webrtc-audio-
processing is better and heavier. The blocker is not the filter, it is
alignment: aligning the reference needs `snd_pcm_delay()`, and the client
pipes to `aplay` as a subprocess, so **the whole subprocess-and-pipe design
would have to become a real ALSA binding**. Also no numpy and no `audioop` on
the device, so this is a C dependency, not Python. Effort: a week, plus a
refactor of code we just tuned. Risk: high. Do not start here.

> **Barge-in costs battery, and the plan should say so out loud.** Keeping the
> mic open during responses means streaming audio while the assistant talks —
> roughly doubling radio time per response, against everything Phase 4 and 5
> are for. 6a is the cheapest in this respect too, since it only listens in
> gaps. Price this against Phase 1's current draw before committing.

Protocol note: `MUTE_MIC` currently means "device stops sending". Barge-in
changes it to "device keeps sending, app decides", which is a semantic change
the Pi 5 shares. Phase 4's binary frames want a sequence and timestamp header
anyway — design it so it can carry the alignment metadata 6c/6d need, because
retrofitting that later is expensive.

### Phase 7 — Send only the child's voice (post-demo)

Goal: ignore room noise, background parents, and unrelated chatter. Four
different technologies get conflated here and they are not interchangeable:

| | Answers | Needs | Fits? |
| --- | --- | --- | --- |
| VAD | is *anyone* speaking? | a threshold (already present) | necessary, not sufficient |
| Diarization | who spoke when, as anonymous clusters | seconds of audio, clustering | ✗ never says which cluster is the child |
| Speaker verification | is this the enrolled child? | enrollment + embedding model | ✓ this is the actual ask |
| Wake word / push-to-talk | did the user *ask* to be heard? | a button, or a small KWS model | ✓ sidesteps the problem entirely |

**Run it on the laptop, not the Zero 2W.** An ECAPA-TDNN-class embedding needs
numpy plus an ONNX runtime — neither is installed — on a 512 MB device with no
optimised BLAS, and would spend a good share of the core Phase 2 just
recovered. There is a subtler reason: **a gate on the device destroys the data
needed to tune the gate.** A rejection that is never transmitted cannot be
reviewed. Log laptop-side first, move the gate down later if the radio saving
proves necessary.

Caveats that will bite, specific to a child:

- Pretrained speaker models are trained on adult corpora (VoxCeleb and
  friends). Accuracy on child voices degrades — higher pitch, shorter vocal
  tract, different formants.
- A child's voiceprint **drifts as they grow**. Plan re-enrollment or slow
  adaptation; this is not enroll-once.
- Embeddings need ~1–3 s of *speech* to be stable, which fights realtime
  turn-taking. Sub-second verdicts are unreliable.
- Overlapping speech (child + parent) blends into one embedding and fails
  unpredictably. Separation is far heavier than verification.
- **Failure asymmetry matters more than accuracy.** A false reject means the
  child is ignored, repeats themselves, and the product feels broken. A false
  accept means answering the wrong person — annoying and wasteful. For a young
  child the first is worse, so bias permissive and suppress non-targets with
  other signals.

Cheaper signals worth exhausting first:

1. **Near-field level gate.** The child is close; parents are across the room.
   §3 measured speech at +10.2 dB over ambient at the child's position. Costs
   nothing — the RMS is already computed.
2. **Pitch band gate.** Children sit around 250–400 Hz F0 against 85–155 Hz
   for adult males; cheap autocorrelation rejects adult male speech for
   almost nothing. Will not reliably separate a child from an adult female.
3. **A second microphone — the highest-leverage option on this list.** The I2S
   frame already carries a second channel that is *silent by design*, and the
   ICS-43434 has an L/R select pin, so a second mic joins the same bus with no
   extra GPIO and no protocol change. Two mics buy near-field discrimination
   from level and phase difference, beamforming toward the child, **and a
   spatial null that can be aimed at the speaker** — attacking Phase 6 and
   Phase 7 with one component. Phase 2b's `route` plugin already makes channel
   selection a config change.
4. **Push-to-talk.** Unfashionable, but for a child's device a hold-to-talk
   button is robust, teaches turn-taking, removes echo and speaker selection
   entirely, and slashes radio time. [battery.md](battery.md) already lists a
   wake button as future work.

Order: level gate + logging (days) → evaluate from real logs → laptop-side
verification only if the logs demand it → evaluate the second mic before
buying more software. Effort: 1 day for the level gate, ~1 week for
verification. Risk: low, then medium.

Protocol note: audio you decide not to forward interacts with OpenAI's server
VAD turn detection — the same "reconstruct the timeline faithfully or
turn-taking breaks" caveat as Phase 5b.

## 5. Timeline

| Day | Work |
| --- | --- |
| **Sat 25 Jul** | Phase 2a done and verified on hardware. Phase 1 **not done** — still needs the power meter. |
| **Sun 26 Jul** (today) | Phase 2b + 2c done and verified. CPU delta measured (§3). Echo/voice levels measured; Phases 6 and 7 written up. Phase 1 still outstanding. |
| **Mon 27 Jul** | Phase 1 power baseline — it is now the only unmeasured thing on this list, and Phase 4's justification depends on it. Phase 3 is dropped, so nothing else competes for the day. **Do not merge to `main`.** |
| **Tue 28 Jul** | **Hard freeze.** Pre-demo checklist (§1). Rollback drill. Full dry run. Nothing else. |
| **Wed 29 Jul** | Demo the baseline. |
| **After** | Merge what's proven. Then **5a** (an hour, idle-power, no schema change), then Phase 4, then the rest of Phase 5. Then port to the Pi 5. Phases 6 and 7 are product work, not battery work — they *cost* radio time, so schedule them against Phase 1's numbers, not instead of them. |

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
- ~~`config/targets.local.env` records `PIZERO2W_SAMPLE_RATE="48000"` and
  `PIZERO2W_CAPTURE_FORMAT="S32_LE"`, but the client hardcodes 24 kHz /
  `S16_LE`~~ — **answered 2026-07-26.** Those values are the hardware's, not a
  contradiction: `hw:0,0` supports *only* S32_LE / 48 kHz / 2ch, and `plug`
  bridges to the client's 24 kHz / S16_LE. The consequence is in §3: the
  truncation to 16 bits happens before any gain, and moving the gain into the
  32-bit domain would recover about 4 bits of the mic's resolution.
- The barge-in and voice-gating work (Phases 6 and 7) both spend SNR, and both
  would benefit from that 32-bit change more than from anything else on this
  list. Measure it before designing either.

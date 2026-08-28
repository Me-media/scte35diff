# scte35_idr_diff — SCTE-35 vs. IDR PTS delta over multicast

**Version: 0.10** (run `python3 scte35_idr_diff.py --version` to confirm
which version is running; see "Version history" at the end of this
document.)

Measures, live off a multicast MPEG-TS stream (or from a saved capture
file), the difference in milliseconds between the PTS an SCTE-35 splice
message points to (`splice_insert` / `time_signal`, program-level) and the
PTS of the nearest IDR frame in the video PID.

This is the metric that actually determines whether a downstream
splicer/switch can perform a clean transition at the signaled point. A
splice command only tells a decoder or splicer *where* it would like a cut
to happen; it is the encoder's job to make sure a real IDR frame lands
there, since a clean switch can only occur on a frame that doesn't
reference anything before it. If the PTS delta between the signaled point
and the nearest IDR is large, the encoder did not force one at the splice
point, and viewers get a visible glitch or a black frame on ad insertion.
This tool exists to make that alignment error a plain number, live, instead
of something you can only infer after the fact from a support ticket.

## Architecture and why it looks the way it does

The tool combines:

- **threefive3** (pure Python library, MIT license) — decodes SCTE-35
  `splice_info_section` payloads into a structured `Cue` object
  (`command.pts_time` etc). Used only for that one job.
- **Custom code** for everything else: multicast join, RTP
  de-encapsulation, TS/PSI/PES demuxing, NAL scanning for IDR detection,
  and the matching logic between splice points and IDRs.

Neither threefive3 nor a general-purpose tool like TSDuck provides a
ready-made "diff SCTE-35 PTS against the nearest video IDR PTS, live"
function on its own — threefive3 does no video analysis at all, and TSDuck
has the building blocks (its `pes` plugin, for instance) but nothing that
correlates them against SCTE-35 in a single pass. Hence this small,
self-contained script: it only needs to do the one specific correlation,
so it doesn't need the surface area of a full TS toolkit. TSDuck remains a
good independent reference to cross-check a result against if one looks
surprising (see "Verification" further down).

## Installation

```bash
# Debian/Ubuntu
sudo apt install python3 python3-pip
pip install threefive3 --break-system-packages
```

Works with Python 3.8+. No other dependency is required **unless you use
`--snapshot-dir`** (see below), which also needs `ffmpeg`:

```bash
sudo apt install ffmpeg
```

## Running it

```bash
# Auto-detect video and SCTE-35 PID from PAT/PMT, plain UDP-TS
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000

# No CUEI registration_descriptor in the PMT: set PIDs manually
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \
    --pid-video 0x101 --pid-scte35 0x1F0 --codec h264

# Local testing against loopback, no real multicast network needed
python3 scte35_idr_diff.py --addr 127.0.0.1 --port 5000

# Reprocess a saved capture file instead of a live feed
python3 scte35_idr_diff.py --input-file capture.ts --csv-out report.csv

# Save a JPEG of every splice-relevant IDR, so you can see what the frame
# actually showed (black, corrupted, wrong content?) instead of trusting
# the PTS math alone. Requires ffmpeg.
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \
    --csv-out splice_report.csv --snapshot-dir snapshots/

# Save a raw TS dump around every SCTE-35 event, for offline analysis in
# TSDuck/VLC/ffplay -- no ffmpeg needed, just disk space
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \
    --csv-out splice_report.csv \
    --ts-dump-dir ts_dumps/ --ts-dump-window 60s --ts-dump-max-files 200
```

Stop a live run with Ctrl-C; a file-input run stops on its own once the
file ends. Results print to stdout continuously, plus CSV/JSON/SCTE-35 logs
if configured.

Key flags:

| Flag | Meaning |
|---|---|
| `--addr` | Multicast group address, e.g. `239.1.1.1`. Anything inside 224.0.0.0-239.255.255.255 is joined as a real multicast group. A value outside that range — most usefully `127.0.0.1` — is instead treated as a plain unicast address: no group join is attempted, the tool just listens for UDP sent directly to that address/port. That makes it possible to test against a local stream on loopback without any real multicast network. |
| `--iface` | Local interface to join the multicast group on. Only meaningful when `--addr` is an actual multicast address. |
| `--input-file` | Read raw MPEG-TS from a local file instead of a live source — see "File input mode" below. Mutually exclusive with `--addr`/`--port`. |
| `--tolerance-ms` | How far *after* the target PTS an IDR may land and still be accepted as a match (default 6000 ms). Set this in line with how much lead time your ad-decisioning/splicer chain normally works with. |
| `--max-early-ms` | How far *before* the target PTS an IDR may land and still be accepted (default 50 ms), kept deliberately small — see "Why an early IDR is rejected" below. |
| `--timeout-s` | How long a splice point waits for a matching IDR before being reported as missed (default 12 s). |
| `--ok-threshold-ms` | Threshold for verdict OK vs. OUT_OF_SPEC (default ~41 ms, roughly one frame at 24 fps — adjust to your own GOP structure/frame rate). |
| `--include-cra` | Also count HEVC CRA pictures as splice candidates (flagged separately — see caveats below). |
| `--program` | Program number to follow if the multiplex carries several. |
| `--scte35-out FILE` | Every decoded SCTE-35 message in full, as JSON-lines — every field, every descriptor, regardless of whether it could be matched against an IDR or even carried a time-specified PTS. Rows join to `--csv-out`/`--json-out` via `cue_seq`. |
| `--scte35-log-file FILE` | The same per-message detail as `--scte35-out`, as plain text log lines instead of JSON. Moves the detailed per-cue lines off the console into this file, keeping the console focused on lifecycle and match/miss verdicts. |
| `--snapshot-dir DIR` | Saves a JPEG of the matched IDR for each splice event. Requires `ffmpeg`. See "Saving IDR frames as JPEG" below. |
| `--snapshot-all-idr` | With `--snapshot-dir`: save every IDR, not just the ones relevant to a pending splice point. High volume — off by default. |
| `--pre-frames N` | With `--snapshot-dir`: also save the N frames immediately before the matched IDR, in display order. See below. |
| `--au-buffer-size N` | With `--pre-frames`: how many access units to buffer (default scales with `pre_frames`). Raise for unusually large GOPs. |
| `--ts-dump-dir DIR` | Save a raw TS dump (every PID) around each SCTE-35 event. See "Raw TS dump around SCTE-35 events" below. |
| `--ts-dump-window DUR` | Window length for the TS dumps, e.g. `30s`, `2m` (default 60s). |
| `--ts-dump-all` | With `--ts-dump-dir`: save every window, turning this into a plain rolling recorder rather than an event-triggered one. |
| `--ts-dump-no-preroll` | With `--ts-dump-dir`: don't prepend the previous window to a saved dump. |
| `--ts-dump-max-files N` | With `--ts-dump-dir`: cap how many saved dumps accumulate before the oldest are deleted. Strongly recommended for any unattended run. |
| `--preroll-tolerance-ms` | How far the *actual* pre-roll may fall short of the *declared* one before being flagged `PREROLL_SHORT` (default 500 ms). See "Time to event vs. actual pre-roll" below. |
| `--min-time-to-event-ms` | The minimum declared lead time a splice event's first transmission needs to satisfy SCTE-35's own advance-notice requirement (default 4000 ms). See the same section. |

**Upgrading from an older version:** the `--csv-out` schema has gained
columns over time, and the header row is only written once, when the file
is first created. Point `--csv-out` at a fresh file after upgrading rather
than appending to one from a previous version, or the columns will no
longer line up with the header.

### Why an early IDR is rejected

An encoder cannot react to a splice cue it hasn't seen yet, so an IDR that
lands noticeably *before* the signaled splice point can never be a genuine
reaction to that cue — it has to be an unrelated IDR the encoder would
have produced anyway, from its normal periodic GOP structure. A naive
symmetric matching window (accept any IDR within N seconds either side of
the target) will happily grab that unrelated, earlier IDR if it happens to
fall inside the window, producing a nonsensical negative delta that looks
like the encoder reacted before being told to.

`--max-early-ms` (default 50 ms) exists specifically to close that gap: an
IDR must land at or after (target PTS − `--max-early-ms`) to be considered
a candidate at all. Keep it small — it's a tolerance for measurement noise
around the target, not a real matching window in its own right.

## Example output

```
[2026-08-24T10:15:03] event_id=4242 type=SpliceInsert oon=True target_pts=100.000000s idr_pts=100.020000s delta=+20.0ms verdict=OK
[2026-08-24T10:22:47] event_id=4243 type=SpliceInsert oon=False target_pts=310.500000s idr_pts=n/a delta=n/a verdict=MISSED (no IDR near target PTS within timeout)
```

## Saving IDR frames as JPEG

With `--snapshot-dir <dir>`, the tool saves a JPEG of the actual video
frame at each matched splice point, so a suspicious delta can be checked
visually instead of trusting the PTS math alone — was it a clean frame, a
black frame, or something corrupted?

It does this without decoding the whole video stream: it takes just the
raw H.264/HEVC NAL units for the one access unit matched against the
splice point and pipes them into a short-lived `ffmpeg` subprocess that
decodes that single picture. Because an IDR is by definition a complete,
self-contained frame with no reference to anything earlier, this can be
done one frame at a time without touching the surrounding GOP.

One detail matters here: a standalone decoder needs the parameter sets
(SPS/PPS, or VPS/SPS/PPS for HEVC) to make sense of a frame. Many encoders
are configured to repeat those parameter sets in every IDR access unit, in
which case nothing extra is needed. If yours doesn't, the tool
automatically caches the most recently seen parameter sets from the stream
and prepends them to a lone IDR that lacks its own, so decoding still
succeeds either way.

The decode work happens in a background thread, so it never blocks
multicast reception. By default only IDRs that actually occur while a
splice point is pending are saved (not every GOP boundary in the stream,
which would produce an unmanageable number of files) — `--snapshot-all-idr`
lifts that if you want every IDR regardless.

## Saving frames before the IDR (`--pre-frames`)

`--snapshot-dir` alone only shows the IDR at the splice point itself.
Sometimes that isn't enough to judge the transition — you also want to see
what was showing right before the cut. `--pre-frames N` saves the N frames
immediately before the matched IDR, in display order, in addition to the
IDR's own JPEG.

This is harder than a lone IDR, because ordinary frames reference other
frames and can't be decoded in isolation. The tool handles this by
continuously buffering raw access units in transport order, then — once an
IDR matches a splice point — walking back to the nearest preceding
IDR/CRA (a guaranteed decodable starting point), decoding that whole
stretch once, and keeping only the last N+1 images in display order. If
fewer than N frames were actually available (for example, right at the
start of a session), it saves as many as it has rather than erroring.

`--pre-frames` requires `--snapshot-dir`. The extra decode work still
happens in the background thread, but it is more expensive than a single
IDR decode, since a whole GOP gets decoded instead of one frame — worth
keeping an eye on if splice events are frequent and GOPs are large.

## Raw TS dump around SCTE-35 events (`--ts-dump-dir`)

Sometimes a single frame isn't enough and you want the entire transport
stream around an event — every PID, playable in VLC or loadable in TSDuck,
or useful as evidence to hand to a vendor. `--ts-dump-dir <dir>` does this
as a plain byte-for-byte copy of the TS packets the tool already receives,
with no re-encoding and no `ffmpeg` dependency.

The stream is continuously chopped into fixed-length windows
(`--ts-dump-window`, default 60s). A window is only written to disk if at
least one SCTE-35 message was decoded during it; empty windows are
discarded. By default the immediately preceding window is also included
(disable with `--ts-dump-no-preroll`), so an event near the start of a
window still has real context before it — in practice a saved dump can
contain up to roughly twice the window length.

Each saved `.ts` file gets a `.json` sidecar recording the window's
start/end time and which `cue_seq` values (joinable with `--scte35-out`)
triggered the save, so a raw file can always be traced back to the exact
SCTE-35 detail that caused it to be kept.

Two trade-offs worth knowing before enabling this on a live stream:

- **Memory.** With pre-roll included (the default), roughly twice one
  window's worth of raw TS stays resident in memory at all times — this
  scales directly with stream bitrate and window length, so keep the
  window short, or disable pre-roll, on a high-bitrate feed if memory is
  tight.
- **Disk.** Without `--ts-dump-max-files` there is no upper bound on how
  much gets written over time. Always set it for any unattended run; the
  tool then deletes the oldest dump (and its sidecar) once the limit is
  exceeded.

`--ts-dump-dir` is independent of `--snapshot-dir`/`--pre-frames` and can
be combined freely with them — one gives individual frames, the other
gives the entire raw stream.

## File input mode (`--input-file`)

The tool can run against a local file instead of a live source:

```bash
python3 scte35_idr_diff.py --input-file capture.ts --csv-out report.csv
```

`--input-file` accepts any standard, byte-aligned MPEG-TS file — a dump
saved by `--ts-dump-dir`, a file cut with TSDuck/VLC/ffmpeg, or an earlier
recording you want to re-analyze with a newer version of the tool. It is
read once, start to finish, through exactly the same parsing and matching
logic as a live stream, producing the same CSV/JSON/console output. The
tool exits on its own once the file ends.

There is one important consequence to keep in mind when reading the
results: the file is read as fast as disk I/O allows, not paced to
whatever cadence the stream originally had. That makes `actual_preroll_ms`
and `preroll_verdict` meaningless in file mode — there is no genuine,
real-time lead time being measured, only however long reading the file
took. `time_to_event_ms` and `signal_verdict` are unaffected, since they
are computed purely from PTS values and never depend on wall-clock time.
Likewise, reaching the end of the file with a splice point still open is
treated as conclusive on its own — no matching IDR ever arrived — so it's
reported immediately as missed rather than waiting out `--timeout-s`. The
same now applies to stopping a live run with Ctrl-C: any splice point
still open at that point is reported as missed instead of silently
disappearing from the output.

## Time to event vs. actual pre-roll

Every match/miss row carries fields that compare the *declared* lead time
a splice cue signals against the *actual* lead time that was really
observed, and whether the signaling itself met SCTE-35's own timing
requirement. All of this is always computed, no flag required:

| Field | Meaning |
|---|---|
| `time_to_event_ms` | Declared lead time: the cue's target PTS minus the most recently observed video PTS at the moment the cue was registered — in other words, how far into the future the splice was signaled, per the stream's own clock. Empty if no video had been seen yet when the cue arrived. |
| `actual_preroll_ms` | Actual lead time: real, wall-clock time between registering the cue and observing the matching IDR. Empty for a missed cue. |
| `preroll_delta_ms` | `actual_preroll_ms − time_to_event_ms`. Negative means the actual lead time was shorter than declared — downstream equipment got less real reaction time than the cue promised. |
| `preroll_verdict` | Flagged when `preroll_delta_ms` falls short by more than `--preroll-tolerance-ms`. This is the tool's own operational measure, not a cited SCTE-35 requirement — see below. |
| `signal_verdict` | Flagged when the first transmission of a splice event had a declared lead time below `--min-time-to-event-ms`. Later retransmissions of the same event are reported separately rather than re-evaluated. Unlike `preroll_verdict`, this one is intended to reflect an actual SCTE-35 requirement — see below. |

To make the difference between the two concrete: imagine a marker where
the cue's own PTS declared a lead time of a few seconds, but by the time
the matching IDR actually showed up, well under half of that lead time had
genuinely elapsed — a real, material shortfall against what was promised.
That's exactly the kind of gap `preroll_verdict` is built to catch.

**Does a gap like that violate the SCTE-35 standard?** The question splits
into two separate parts:

1. **Was the message itself sent far enough in advance?** Secondary
   sources citing ANSI/SCTE 35 describe a requirement that a splice
   message be sent at least once, a minimum number of seconds ahead of
   its own target time — independently corroborated by at least one other
   standards document that cites the same figure. If a cue's first
   transmission falls short of that, it's a genuine shortfall at the
   source, not just a delivery problem — that's what `signal_verdict`
   checks. This citation has not been verified against the primary
   ANSI/SCTE 35 text directly (the full standard was not available while
   building this), so treat it as corroborated by secondary sources
   rather than independently confirmed, and check it against a licensed
   copy before relying on it for anything formal.
2. **Was the actual pre-roll delivered in line with what the cue
   declared?** Here the answer is more clear-cut: SCTE-35 does not require
   the declared and the actually-delivered lead time to match, and
   specifies no tolerance for the gap between them. `preroll_verdict` is
   this tool's own operational check, not a cited requirement. The idea of
   a configured "pre-roll time" for the whole chain — automation system,
   encoder, network — instead comes from a separate companion protocol
   that carries that parameter between the automation system and the
   encoder, plus whatever policy an operator or vendor agreement sets on
   top of it.

Whatever the formal answer, a real, material shortfall of this kind is
operationally significant regardless of standards status: it means
downstream ad-decisioning or splicing equipment got less real reaction
time than the cue implied, which in the worst case shows up as a delayed
or missed switch.

One complication worth understanding: the advance-notice requirement in
part 1 is about the *first* transmission of a given event. Encoders
commonly resend the same cue more than once as insurance against packet
loss, and each later resend naturally has a smaller declared lead time as
the splice point gets closer — that's expected and not itself a problem.
The tool tracks which event identifiers it has already seen so that only
the first occurrence is checked against `--min-time-to-event-ms`; later
occurrences of the same event are reported separately rather than
re-flagged.

Three limitations worth keeping in mind:

- `time_to_event_ms` uses the nearest preceding video frame's PTS as a
  stand-in for "when the message was sent", rather than a full clock-
  reference interpolation the way a dedicated stream analyzer would do it.
  That introduces a small, bounded error — on the order of one frame
  interval.
- `actual_preroll_ms` is only meaningful against a stream genuinely
  arriving in real time. A recording replayed faster or slower than
  real time will make this figure — and `preroll_verdict` — misleading.
- The dedup behind `signal_verdict` assumes an encoder reuses the same
  event identifier when resending an event, and never reuses one across
  two genuinely different events. If a chain deviates from that
  assumption, this logic may need revisiting.

## Important caveats — read before treating results as ground truth in a compliance report

1. **pts_adjustment**: per the SCTE-35 spec, a value carried in the
   section header must be added to every PTS in the command to get the
   actually intended timestamp, since intermediate equipment is allowed to
   adjust it when relaying a cue. threefive3 does not apply this
   automatically — the script applies it itself. If this value is
   normally zero in your chain, it makes no practical difference.
2. **PES/access-unit assumption**: the tool assumes one PES packet per
   video access unit, true for essentially all broadcast-profile
   encoders. If an access unit is ever split across multiple PES packets,
   the PTS is still read correctly from the packet carrying the first NAL
   unit.
3. **HEVC CRA vs. IDR**: some encoders place a splice point on a CRA
   picture rather than a true IDR. A CRA is not fully independent (leading
   pictures may reference backward), so it isn't counted as a valid match
   by default — `--include-cra` includes these, flagged for review rather
   than treated as OK.
4. **MPEG-2 video**: the IDR concept doesn't exist in MPEG-2; the tool
   detects the PID/codec but does no frame-type classification for it in
   this version.
5. **PTS wraparound**: the 90 kHz counter wraps roughly every 26.5 hours.
   The matching logic handles this correctly within normal matching
   windows; a long-running monitor should still be restarted periodically
   or run with reasonable `--timeout-s` values.
6. This is a **diagnostic/QC tool**, not a certified SCTE-35 conformance
   tester. For unexpected or critical results, verify against a reference
   implementation such as TSDuck before escalating to a vendor.
7. **`time_to_event_ms`/`actual_preroll_ms` are approximations, not a
   certified measurement** — see "Time to event vs. actual pre-roll"
   above for what each figure does and doesn't tell you before citing a
   result as a standards violation.
8. **`signal_verdict` rests on a secondary-source-cited, not
   primary-source-verified, reading of SCTE-35** — see the same section
   above for the caveat on that citation, and on the assumption behind its
   retransmission handling.
9. **`--input-file` does not measure genuine lead time**: the file is
   read as fast as disk I/O allows, not at its original pace, so
   `actual_preroll_ms`/`preroll_verdict` are not meaningful in file mode.
   See "File input mode" above.

## Multiple instances at once (multiple channels/terminals)

Running several instances against different multicast addresses but the
same UDP port (common when many channels are distinguished only by
multicast IP) is supported cleanly: each instance binds to its own
specific group address rather than to any address on that port, so
instances don't cross-contaminate each other's stream. Just point
`--csv-out`/`--json-out`/`--scte35-out`/`--scte35-log-file` at different
files per instance so they don't collide while writing.

## Running as a continuous probe

The script runs in the foreground and stops with Ctrl-C. For continuous
operation, run it under a process supervisor (e.g. `systemd`, restarting
on failure) and feed `--csv-out`/`--json-out` into whatever already
collects logs or metrics in your environment — a small tail script
alerting on anything other than an OK verdict is a natural next step if
you want production alerting rather than manual runs during
troubleshooting.

## Tested

`test_offline.py` builds synthetic TS packets — PAT/PMT, video access
units with IDRs, simulated SCTE-35 cues — and runs the full demux and
matching chain without any network, covering PAT/PMT parsing, PES/NAL
decoding, the PTS-wraparound math, the matching/timeout logic, the
snapshot and pre-frame paths (using a real `ffmpeg` encode where that
matters), the raw TS dump feature, file input mode, address handling, and
the declared/actual pre-roll and signal-timing checks described above.
Timing-sensitive tests force their conditions deterministically (by
adjusting an internal clock reading directly) rather than relying on real
`sleep()` calls, so the suite runs quickly and isn't sensitive to machine
load.

The actual SCTE-35 binary decoding is delegated to threefive3, a
well-established third-party library, and is exercised through the same
interface the tests use rather than re-tested here — install it and test
against a real or recorded stream before relying on a new environment for
production use.

```bash
python3 test_offline.py
```

## Version history

The tool's version (check with `--version`) is also stamped into every
`--json-out`/`--scte35-out`/`--ts-dump-dir` sidecar record, so a saved
report or capture can always be traced back to the version that produced
it.

- **v0.10** (current): file input mode (`--input-file`) for re-analyzing
  a saved capture without a live network. A splice point still open when
  a run ends — end of file, or Ctrl-C on a live run — is now reported as
  missed instead of silently disappearing from the output.
- v0.9: support for running against a plain unicast/loopback address for
  local testing without a real multicast network.
- v0.8: the declared-lead-time signaling check (`signal_verdict` /
  `--min-time-to-event-ms`), and version numbering itself.
- Earlier, not retroactively numbered: the declared-vs-actual pre-roll
  measurement (`time_to_event_ms`/`actual_preroll_ms`/`preroll_verdict`),
  address validation improvements, the raw TS dump feature, and this
  document.

Future changes will be added here with a version number, so it's possible
to see what changed between the versions actually running in production.

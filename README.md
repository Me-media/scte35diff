# scte35_idr_diff — SCTE-35 vs. IDR PTS delta over multicast

Measures, live off a multicast MPEG-TS stream, the difference in milliseconds
between the PTS an SCTE-35 splice message points to (`splice_insert` /
`time_signal`, program-level) and the PTS of the nearest IDR frame in the
video PID. This is exactly the metric that determines whether a downstream
splicer/switch can perform a clean transition at the signaled point — if the
PTS delta is large, the encoder has not forced an IDR at the splice point,
which produces visible glitches / black frames on ad insertion.

## Architecture and why it looks the way it does

The tool combines:

- **threefive3** (pure Python lib, MIT) — decodes SCTE-35
  `splice_info_section` into a structured `Cue` with `command.pts_time` etc.
  Used only for the actual SCTE-35 decoding.
- **Custom code** for everything else: multicast join, RTP de-encapsulation,
  TS/PSI/PES demux, NAL scanning for IDR detection, and the matching logic
  between splice points and IDRs. No TSDuck dependency is required, but
  TSDuck (`tsp -P scte35`) is recommended as a reference/second-opinion tool
  when results look questionable (see "Verification" below).

Why neither threefive nor TSDuck alone is enough out of the box: neither
exposes a ready-made "diff SCTE-35 PTS against video IDR PTS" function.
threefive does no video analysis at all; TSDuck has building blocks (the
`pes` plugin with `--intra-image`) but nothing that correlates against
SCTE-35 in a single run. Hence this small, self-contained script.

## Installation

```bash
# Debian/Ubuntu
sudo apt install python3 python3-pip
pip install threefive3 --break-system-packages
```

Works with Python 3.8+. No other external dependency **unless you use
`--snapshot-dir`** (see next section) — that also requires `ffmpeg`:

```bash
sudo apt install ffmpeg
```

## Running it

```bash
# Auto-detect video and SCTE-35 PID via PAT/PMT, plain UDP-TS
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000

# RTP-encapsulated multicast, explicit interface, CSV log for follow-up
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \
    --iface 10.0.0.5 --transport rtp --csv-out splice_report.csv

# Headend with no CUEI registration_descriptor in the PMT: set PIDs manually
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \
    --pid-video 0x101 --pid-scte35 0x1F0 --codec h264

# Capture EVERY SCTE-35 message in full (every command, every descriptor
# field, including splice_null/canceled/immediate messages that never get
# matched against an IDR) into its own files, alongside the normal console
# output and the match/miss CSV:
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \
    --csv-out splice_report.csv \
    --scte35-out scte35_full.jsonl --scte35-log-file scte35.log

# Save a JPEG of every splice-relevant IDR, so you can see what the frame
# at the splice point actually showed (black frame, corruption, wrong
# content?) instead of trusting the PTS math alone. Requires ffmpeg on PATH.
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \
    --csv-out splice_report.csv --snapshot-dir snapshots/

# Same, but also save the 3 frames IMMEDIATELY BEFORE the IDR, so you can
# see the actual transition into the splice point, not just the isolated
# IDR image:
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \
    --csv-out splice_report.csv --snapshot-dir snapshots/ --pre-frames 3

# Save a RAW TS dump (60-second windows) around every SCTE-35 event, for
# offline analysis in TSDuck/VLC/ffplay -- no ffmpeg required, just disk
# space. Always set --ts-dump-max-files in production:
python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \
    --csv-out splice_report.csv \
    --ts-dump-dir ts_dumps/ --ts-dump-window 60s --ts-dump-max-files 200
```

Key flags:

| Flag | Meaning |
|---|---|
| `--tolerance-ms` | How far AFTER the target PTS an IDR may land and still be accepted as a match (default 6000 ms). Set this in line with how much "lead time" your ad server/splicer normally allows. |
| `--max-early-ms` | How far BEFORE the target PTS an IDR may land and still be accepted (default 50 ms). Keep this small — see "Known bug fixed" below. |
| `--timeout-s` | How long a splice point waits for a matching IDR before being reported as missed (default 12 s). |
| `--ok-threshold-ms` | Threshold for verdict OK vs. OUT_OF_SPEC (default ~41 ms, roughly one frame at 24 fps — adjust to your GOP structure/frame rate). |
| `--include-cra` | Also count HEVC CRA pictures as splice candidates (flagged separately, see caveats below). |
| `--program` | Program number to follow if the multiplex carries several. |
| `--scte35-out FILE` | JSON-lines: every decoded SCTE-35 section in full — every field threefive3 parsed, every descriptor (e.g. the `segmentation_descriptor` fields that carry the actual event identity for a `time_signal()` cue), regardless of whether the message could be matched to an IDR or even had a time-specified PTS. This is the file to reach for when you need the complete raw SCTE-35 picture, not just the PTS-diff outcome. Rows are joined with `--csv-out`/`--json-out` via the `cue_seq` column. |
| `--scte35-log-file FILE` | Same information as `--scte35-out`, but as human-readable text log lines instead of JSON — for tailing/grepping without a JSON parser. When set, the detailed per-cue lines move off the console into this file, so the console isn't drowned out on a chatty SCTE-35 PID; join/PAT/PMT and the final OK/OUT_OF_SPEC/MISSED lines stay on the console as before. |
| `--snapshot-dir DIR` | Saves a JPEG of the matched IDR for every splice event into this directory. Requires `ffmpeg` on PATH (`sudo apt install ffmpeg`). Referenced as `snapshot_path` in the CSV/JSON/console line. See "Saving IDR frames as JPEG" below. |
| `--snapshot-all-idr` | With `--snapshot-dir`: save EVERY IDR in the stream, not just the ones that happen to occur while an SCTE-35 splice point is waiting for a match. High volume (one file per GOP, around the clock) — the default is to save only splice-relevant IDRs. |
| `--pre-frames N` | With `--snapshot-dir`: also save the N frames immediately BEFORE the matched IDR (in display/PTS order) as additional JPEGs. See "Saving frames before the IDR" below. |
| `--au-buffer-size N` | With `--pre-frames`: how many access units to buffer in transport order (default `max(300, pre_frames * 15)`). Raise this for unusually large GOPs. |
| `--ts-dump-dir DIR` | Save a RAW TS dump (every PID, not just video/SCTE-35) around each SCTE-35 event, for offline analysis in TSDuck/VLC/ffplay. See "Raw TS dump around SCTE-35 events" below. |
| `--ts-dump-window DUR` | Window length for the TS dumps, e.g. `30s`, `90s`, `2m`, `1.5m` (bare numbers are seconds). Default 60s. |
| `--ts-dump-all` | With `--ts-dump-dir`: save EVERY window, not just ones with an SCTE-35 event — turns the tool into a plain rolling raw-TS recorder. |
| `--ts-dump-no-preroll` | With `--ts-dump-dir`: do NOT include the previous window in the saved dump (default is to include it for pre-roll context). Halves the memory footprint and file size. |
| `--ts-dump-max-files N` | With `--ts-dump-dir`: delete the oldest saved dumps once more than N exist. **Strongly recommended** in production — otherwise there's no upper bound on disk usage. |

Stop with Ctrl-C. Results are printed to stdout continuously, plus
CSV/JSON/SCTE-35 logs if configured.

**Note when upgrading from an earlier version:** the `--csv-out` schema has
gained new columns over time (`cue_seq`, `raw_pts_time_s`,
`pts_adjustment_ticks`, `segmentation_summary`, `snapshot_path`, and most
recently `pre_frame_snapshot_paths`). The header row is only written when
the file is empty, so an existing CSV file from before these changes will
get **misaligned columns** if you keep writing to the same path — point
`--csv-out` at a new file after upgrading.

### Known bug fixed: false large negative delta values

A field test (`lookaheadDepth` lowered from 25 to 4) produced a result of
**−2643.5 ms** — i.e. the "matched" IDR landed 2.6 seconds BEFORE the
signaled splice point. That's physically implausible for a reactive
mechanism (the encoder can't force an IDR in reaction to a cue it hasn't
seen yet) and was caused by a weakness in the matching logic, not by real
encoder behavior: the old symmetric window (`abs(delta) <= tolerance-ms`)
greedily grabbed the FIRST IDR that happened to fall within ±6 seconds of
the target — including a completely unrelated periodic IDR (from
`intraPeriod` or a GOP boundary) that happened to occur a few seconds
earlier, instead of waiting for the real, later IDR that actually belonged
to the cue.

Fixed by introducing `--max-early-ms` (default 50 ms): an IDR must now land
at or after (target PTS − 50 ms) to be accepted at all. If you see sharply
negative delta values in older runs/logs from before this fix, disregard
them — they measure a coincidence in the periodic GOP structure, not the
actual splice reaction. Re-run with the updated version for correct
numbers.

## Example output

```
[2026-08-24T10:15:03] event_id=4242 type=SpliceInsert oon=True target_pts=100.000000s idr_pts=100.020000s delta=+20.0ms verdict=OK
[2026-08-24T10:22:47] event_id=4243 type=SpliceInsert oon=False target_pts=310.500000s idr_pts=n/a delta=n/a verdict=MISSED (no IDR near target PTS within timeout)
```

## Saving IDR frames as JPEG

Yes, that's possible — with `--snapshot-dir <dir>`. Here's how it works:

The tool does NOT decode the entire video stream (that would be far more
expensive and unnecessary for this purpose). Instead it takes just the raw
H.264/HEVC NAL units for the one access unit matched against an SCTE-35
splice point and pipes them into an `ffmpeg` subprocess
(`ffmpeg -f h264/hevc -i pipe:0 -frames:v 1 output.jpg`), which decodes just
that single frame and writes a JPEG. Because an IDR is by definition a
complete, self-contained picture (no reference to earlier pictures
required), this can be done frame-by-frame without decoding the surrounding
GOP structure.

One detail that matters: an IDR access unit needs SPS/PPS (HEVC:
VPS/SPS/PPS) available for a standalone decoder to make sense of it. If
your encoder runs with `repeatHeaders=1` (as in your xcoder parameters),
SPS/PPS already accompany every IDR, and that's sufficient. If
`repeatHeaders=0` anywhere in your chain, the tool automatically caches the
most recently seen SPS/PPS units from the stream and prepends them to a
lone IDR that lacks its own, so decoding still succeeds.

The work happens in a background thread (queue + separate thread running
`ffmpeg`), so a JPEG decode (tens of milliseconds) never blocks multicast
reception — otherwise the network buffer could overflow and packets get
dropped mid-measurement.

By default, only IDRs that actually occur while an SCTE-35 splice point is
waiting for a match are saved (i.e. the ones relevant to an ad break), not
every other GOP boundary in the entire stream — otherwise you'd quickly end
up with tens of thousands of files per day. `--snapshot-all-idr` lifts that
restriction if you want all of them.

Filenames encode the IDR's own PTS (`idr_pts_<seconds>_<codec>.jpg`), and
the CSV/JSON/console line's `snapshot_path`/`snapshot=` field points to
exactly the right file for each event, so you don't have to go looking.

## Saving frames before the IDR (`--pre-frames`)

`--snapshot-dir` alone only shows the IDR image at the splice point.
Sometimes that isn't enough to assess the transition — you also want to see
what was showing right BEFORE the cut (e.g. to determine whether a black or
frozen frame appears in the transition, not just whether the IDR itself
looks correct). `--pre-frames N` adds exactly that: in addition to the
IDR's own JPEG, it saves the N frames immediately before it, in display
order (the same order a viewer sees them in, not the order they were
carried in the transport stream).

This is technically harder than a lone IDR image: a P/B frame, unlike an
IDR, cannot be decoded in isolation — it references other frames. The tool
solves this by:

1. Continuously buffering RAW access units (not just IDRs) in transport
   order, up to `--au-buffer-size` (default `max(300, pre_frames * 15)`).
2. When an IDR matches a splice point: walking backward in the buffer to
   the NEAREST PRECEDING IDR/CRA (a guaranteed decodable GOP boundary) and
   taking the whole chunk from there through the matched IDR.
3. Decoding that whole chunk ONCE with `ffmpeg -vsync 0` (one image per
   decoded frame, in display order), and keeping only the last `N + 1`
   images (the N requested plus the IDR itself).

If the preceding GOP boundary is too close (e.g. the very first IDR in a
session, or you request more frames than were actually available since the
previous IDR/start), as many as were actually available are saved — no
phantom images, but no error either; the log notes this at debug level.

Filenames for the extra images:
`idr_pts_<IDR-seconds>_<codec>_pre<offset>_pts_<frame's-own-seconds>.jpg`,
where `pre1` is the frame immediately before the IDR, `pre2` two before,
and so on. The CSV/JSON/console line's `pre_frame_snapshot_paths` field
lists them in the same order (oldest first), while `snapshot_path`
continues to point to the IDR's own image just as it does without
`--pre-frames`.

**Requirement**: `--pre-frames` requires `--snapshot-dir` (otherwise the
script exits immediately with an error). Performance impact is marginal
since all the decoding still happens in the background thread — but each
such sequence decode is more expensive than a single IDR decode (the whole
GOP gets decoded, not just one image), so with very frequent splice events
and large GOPs, keep an eye on whether the background queue keeps up.

## Raw TS dump around SCTE-35 events (`--ts-dump-dir`)

Sometimes individual frames aren't enough — you want the entire transport
stream (every PID: video, audio, SCTE-35, PAT/PMT, everything) around an
event, e.g. to play it back in VLC, run it through TSDuck, or hand it to a
vendor as evidence. `--ts-dump-dir <dir>` does exactly that, and needs no
`ffmpeg` (it's a plain byte-for-byte copy of the 188-byte TS packets the
tool already receives, not a re-encode).

**How it works:** the stream is continuously chopped into fixed-length
windows (`--ts-dump-window`, default 60s). When a window ends, the tool
checks whether at least one SCTE-35 message was decoded during it
(regardless of whether it could be matched to an IDR or even had a
time-specified PTS — any SCTE-35 activity counts as an "event" here). Only
qualifying windows are written to disk; empty windows are discarded. By
default, the immediately PRECEDING window is also included in the saved
file (`--ts-dump-no-preroll` to turn this off), so an event that occurs
early in a window still gets real context before it instead of an abrupt
cut — in practice a saved dump can therefore contain up to ~2x
`--ts-dump-window` seconds of stream.

Every saved `.ts` file gets a `.json` sidecar with the same base name
(`ts_dump_000123_2026-08-26T14-05-30_n2events.ts` +
`ts_dump_000123_2026-08-26T14-05-30_n2events.ts.json`) containing the
window's start/end time, whether the previous window was included, and
exactly which `cue_seq` values (from `--scte35-out`) triggered the save —
so you can go straight from a raw TS file to the full SCTE-35 detail for
that specific event.

The work (both buffering and file writes) happens in a background thread
just like the JPEG snapshots, so a large disk write never blocks multicast
reception.

**Important trade-offs before enabling this on a production stream:**

1. **Memory.** With pre-roll enabled (the default), up to 2x one window's
   worth of raw TS stays resident in RAM continuously — roughly
   `bitrate_bps × window_s × 2 / 8` bytes. Example: a 20 Mbit/s stream with
   a 60-second window ⇒ ~300 MB constantly buffered. A 5-minute window at
   the same bitrate ⇒ ~1.5 GB. Keep the window small, or use
   `--ts-dump-no-preroll`, on high-bitrate streams if memory is tight.
2. **Disk.** Without `--ts-dump-max-files` there's no upper bound — if
   your SCTE-35 PID is active often (ad breaks every few minutes is
   normal on linear TV), the disk fills up sooner or later. Always set
   `--ts-dump-max-files` for unattended/production runs; the tool then
   deletes the oldest dump (plus its `.json` sidecar) once the limit is
   exceeded.
3. **The filename's sequence number** (`ts_dump_000123_...`) guarantees
   uniqueness and chronological order regardless of clock resolution or
   NTP jumps — sort/prune files by filename, not by file mtime.
4. **On Ctrl-C**, the in-progress (possibly partial) window is flushed if
   it qualifies (an event occurred, or `--ts-dump-all` is set), so an
   ongoing ad break isn't lost just because you stopped the measurement
   mid-way.

`--ts-dump-dir` is completely independent of `--snapshot-dir`/`--pre-frames`
and can be freely combined with them — one gives you individual JPEG
frames, the other gives you the entire raw stream.

## Important caveats — read before treating results as ground truth in a compliance report

1. **pts_adjustment**: Per the SCTE-35 spec, `pts_adjustment` in
   `splice_info_section` must be added (modulo 2^33) to every `pts_time` in
   the command to get the actual intended PTS, because intermediate
   equipment is allowed to adjust it when relaying. threefive3 does **not**
   do this automatically (verified against the library source) — the
   script applies it itself. If `pts_adjustment` is normally 0 for you (no
   re-stamping upstream of the probe), this doesn't matter in practice.
2. **PES/access-unit assumption**: The tool assumes one PES packet per
   video access unit, which holds for essentially all broadcast-profile
   encoders. If an AU is split across multiple PES packets (unusual), the
   PTS is still correct because it's read from the PES header of the
   packet carrying the first NAL unit of the AU.
3. **HEVC CRA vs. IDR**: Some encoders place the splice point on a CRA
   picture (NAL type 21) instead of a true IDR (19/20). A CRA is not fully
   independent (leading pictures may reference backward) and is therefore
   not counted as a valid IDR match by default — set `--include-cra` to
   also see these, flagged as "REVIEW", not "OK".
4. **MPEG-2 video**: The IDR concept doesn't exist in MPEG-2; the tool
   detects the PID/codec but does no frame-type classification for MPEG-2
   in this version. Relevant mainly if you still have MPEG-2 channels left
   in the network.
5. **PTS wraparound**: The 90 kHz counter wraps roughly every 26.5 hours.
   The matching logic handles this correctly for normal matching windows;
   if you run the probe as a longer-term monitor, restart it periodically
   or run it with reasonable `--timeout-s` values.
6. This is a **diagnostic/QC tool**, not a certified SCTE-35 conformance
   tester. For unexpected or critical results: verify against a reference
   implementation, e.g. `tsp -P scte35 -a -P pes -i --pid <video-pid>` in
   TSDuck, before escalating to a vendor.

## Multiple instances at once (multiple channels/terminals)

Fixed bug: if you run several instances against different multicast
addresses but the **same UDP port** (common in a headend where many SPTS
channels are distinguished only by multicast IP), one instance could
previously start receiving and incorrectly parsing the OTHER instance's
stream — visible as the PAT lines in the log switching between several
programs/PMT PIDs that don't belong to your own channel. The cause was
that the socket was bound to `INADDR_ANY` (`""`), which on Linux only
filters by destination port, not by multicast group — as soon as another
process on the same host joined a different group on the same port, the
kernel delivered both groups' traffic to both processes. The script now
instead binds to the specific multicast address, so several instances
against different groups (even on the same port) no longer cross-
contaminate each other. Just point `--csv-out`/`--json-out`/`--scte35-out`/
`--scte35-log-file` at different files per instance/channel so you don't
get write conflicts between the terminals.

## Running as a continuous probe

The script is written to run in the foreground and stop with Ctrl-C
(`SIGINT`); for continuous operation, run it under `systemd` as a simple
`simple`-type service with `Restart=on-failure`, and feed `--csv-out`/
`--json-out` into a log collected by your existing monitoring (e.g. a
Prometheus textfile exporter reading the CSV/JSON file, or a small tail
script alerting on `verdict != OK` to Slack/PagerDuty). That's not included
in this delivery but is a straightforward extension if you want production
alerting rather than manual runs during troubleshooting.

## Tested

`test_offline.py` builds synthetic TS packets (PAT/PMT, H.264 PES with an
IDR, simulated SCTE-35 cues) and runs the entire demux and matching chain
without a network, to verify PAT/PMT parsing, PES/NAL decoding, the
PTS-wraparound math, `pts_adjustment` handling, and the matching/timeout
logic. Two of the tests (`test_idr_jpeg_snapshot`, `test_pre_frame_snapshots`)
encode a real H.264 sequence with a real `ffmpeg` and run it through the
entire snapshot/pre-frames path, including correct naming, display order,
and the intended graceful fallback behavior when there aren't enough frames
before an IDR (e.g. the very first IDR in a session). Three more tests
(`test_ts_dump_only_saves_windows_with_events`, `test_ts_dump_no_preroll`,
`test_ts_dump_max_files_prunes_oldest`) verify `--ts-dump-dir`: that a
window with no SCTE-35 event gets discarded while one with an event is
saved byte-exact (including the pre-roll merge), that `--ts-dump-no-preroll`
correctly omits the previous window, and that `--ts-dump-max-files` deletes
the oldest dumps (plus their `.json` sidecars) in the right order. These
tests force window rotation deterministically (by back-dating the window
start timestamp) instead of relying on real `sleep()` calls, so they're
fast and not sensitive to machine load. This sandbox has no PyPI access, so
the actual `threefive3` decoding (external, well-established library) could
not be exercised here — run `pip install threefive3` and test against a
real or recorded multicast stream before production use.

```bash
python3 test_offline.py
```

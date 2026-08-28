#!/usr/bin/env python3
"""
scte35_idr_diff.py
Version: 0.8

Measures the PTS delta between SCTE-35 splice points (splice_insert /
time_signal, program-level splice_time) carried in a SCTE-35 PID and the
nearest IDR (video I-frame) access unit PTS in a video PID, read live from
a multicast MPEG-TS stream (UDP, raw or RTP-encapsulated).

Why this exists:
  SCTE-35 splice points are only cleanly executable by downstream splicers
  if the target PTS lands on (or very close to) a video IDR frame. Encoders
  that do not force an IDR at the splice point produce cue messages that
  cannot be spliced cleanly -> visible glitches / black frames on ad
  insertion. This tool quantifies that alignment error in milliseconds,
  live, off a multicast feed, for headend QC and encoder acceptance testing.

Dependencies:
  pip install threefive3 --break-system-packages
  (threefive3 is used only to decode SCTE-35 splice_info_section payloads
  once we have located and reassembled them from the TS ourselves; all
  MPEG-TS / RTP / PES / NAL demuxing below is custom and has no other
  external dependency.)

Design notes / important caveats (read before treating output as ground
truth in a compliance report):

  1. pts_adjustment: per SCTE-35 (ANSI/SCTE 35), any pts_time found in a
     splice command must have splice_info_section.pts_adjustment ADDED to
     it (modulo 2^33) to get the true intended PTS, because devices that
     relay a cue are allowed to adjust it. threefive does NOT apply this
     automatically (verified against the library source) -- this script
     applies it explicitly. If your headend never re-stamps cues upstream
     of this probe, pts_adjustment will normally be 0 and this is moot.

  2. Access-unit/PES alignment: this tool assumes one PES packet == one
     video access unit, which holds for essentially all broadcast-profile
     encoders. If an access unit is split across PES packets (unusual),
     the reported IDR PTS is still correct because it is read from the
     PES header of the packet carrying the first NAL unit of that AU.

  3. HEVC CRA vs IDR: some encoders use CRA (NAL type 21) as the splice
     point instead of true IDR (19/20). CRA pictures are not fully
     independent (leading pictures may reference before the CRA), so they
     are reported separately unless --include-cra is set. Treat a
     CRA-based splice point as "needs review", not automatically OK.

  4. PTS wraparound: the 90kHz PTS counter wraps every ~26.5h (2^33
     ticks). Matching logic below tolerates this within the matching
     window but a splice point queued for longer than the window across
     a wrap could mismatch. Not a concern for a live probe restarted
     periodically.

  5. This is a diagnostic/QC tool, not a certified SCTE-35 conformance
     tester. Validate findings against a reference tool (e.g. TSDuck's
     `tsp -P scte35`) before escalating to a vendor.

  6. time_to_event_ms / actual_preroll_ms: time_to_event_ms is a PTS-domain
     figure (target PTS minus the most recently observed video PTS at the
     moment the cue is registered) -- it approximates "how far in the
     future, per the transport stream's own clock, was this splice
     signaled", the same concept a PCR/PTS-correlating analyzer computes,
     but WITHOUT parsing PCR: it uses the nearest preceding video AU's PTS
     instead of a true STC interpolation, so it carries a small error
     bounded by roughly one video frame interval. actual_preroll_ms is a
     WALL-CLOCK figure (real elapsed time, via a monotonic clock, between
     registering the cue and observing the matching IDR) -- it is only
     meaningful if the feed is arriving in genuine real time (a live
     encoder/multicast feed; NOT a file replayed faster/slower than real
     time, which would make actual_preroll_ms meaningless). ANSI/SCTE 35
     itself does NOT require these two to be equal, nor does it define a
     tolerance for that gap -- preroll_delta_ms/PREROLL_SHORT is this
     tool's own operational check, not a cited standards requirement. A
     large negative preroll_delta_ms (actual well below declared) is
     therefore not itself a standards violation, but IS operationally
     significant: it means downstream ad-decisioning/splicing equipment
     got materially less real reaction time than the cue's own timing
     implied.

  7. signal_verdict / --min-time-to-event-ms: unlike preroll_delta_ms
     above, SCTE-35 DOES appear to specify a minimum advance-notice
     requirement for the message itself -- per secondary sources citing
     ANSI/SCTE 35 (2019) sections 9.2 (splice_insert) and 10.3.3
     (time_signal): "sent at least once a minimum of 4 seconds in advance
     of the desired splice time" (corroborated independently by ETSI TS
     103 752-1 clause 7.2, which cites the same 4-second SCTE-35 minimum).
     This has NOT been verified against the primary ANSI/SCTE 35 text
     itself (access to the full standard was not available when this was
     written) -- treat the citation as secondary-source-corroborated, not
     primary-verified. signal_verdict=SIGNAL_LATE flags the FIRST
     registered occurrence of a splice_event_id whose time_to_event_ms is
     below --min-time-to-event-ms (default 4000). Later retransmissions of
     the same event_id (common practice, for resilience against packet
     loss) are reported as RETRANSMISSION rather than re-evaluated, since
     the 4-second requirement is about the initial signal, not every
     repeat as the splice point gets closer -- if your encoder does not
     retransmit, or reuses splice_event_id across genuinely distinct
     events, this dedup heuristic may need revisiting.

Usage examples:
  # Raw UDP multicast, auto-detect video codec + PIDs from PAT/PMT
  sudo python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000

  # RTP-encapsulated multicast, explicit interface, CSV log
  sudo python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \\
      --iface 10.0.0.5 --transport rtp --csv-out splice_report.csv

  # Manual PID override (no CUEI registration descriptor in PMT)
  python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \\
      --pid-video 0x101 --pid-scte35 0x1F0

  # Capture EVERYTHING SCTE-35 related (all commands, all descriptor
  # fields, including splice_null/canceled/immediate messages that never
  # get matched against an IDR) into its own files, alongside the normal
  # console output and match/miss CSV:
  python3 scte35_idr_diff.py --addr 239.1.1.1 --port 5000 \\
      --csv-out splice_report.csv \\
      --scte35-out scte35_full.jsonl --scte35-log-file scte35.log

Output channels (independent of each other, enable any combination):
  console            Lifecycle (PAT/PMT/join) + match/miss verdicts (this
                      is what you see by default; unaffected by the flags
                      below unless --scte35-log-file is also set, in which
                      case the per-cue detail lines move off the console
                      and into that file instead, to avoid drowning out
                      the verdict lines on a busy SCTE-35 PID).
  --csv-out FILE      One row per match/miss OUTCOME (tabular, for
                      spreadsheets/Grafana/etc).
  --json-out FILE     Same outcomes as --csv-out, as JSON-lines.
  --scte35-out FILE   EVERY decoded SCTE-35 message, in full -- every
                      field threefive parsed, every descriptor (e.g. the
                      segmentation_descriptor fields that carry a
                      time_signal() cue's actual event identity), as
                      JSON-lines. Independent of whether the message had a
                      time-specified pts_time at all, so this is the one
                      to reach for when you need the complete raw SCTE-35
                      picture rather than just the IDR-alignment verdict.
                      Join to --json-out/--csv-out rows via "cue_seq".
  --scte35-log-file FILE   Same per-cue information as --scte35-out, but
                      as a human-readable text log line (one line per
                      SCTE-35 message) instead of structured JSON -- for
                      tailing/grepping without a JSON parser.
  --snapshot-dir DIR  Save a JPEG of the matched IDR access unit for each
                      splice event (requires ffmpeg on PATH -- decodes just
                      that one access unit standalone). Referenced from the
                      --csv-out/--json-out rows and the console verdict
                      line as "snapshot_path", so you can visually confirm
                      what the splice frame actually looked like (clean
                      program/ad content vs. a black frame, corruption,
                      etc.) instead of trusting the PTS math alone.
  --pre-frames N      With --snapshot-dir, also save the N access units
                      immediately BEFORE the matched IDR, in DISPLAY (PTS)
                      order, as additional JPEGs -- so you can see the
                      actual visual transition into the splice point, not
                      just the IDR itself. Since P/B frames are not
                      independently decodable, this buffers raw access
                      units (up to --au-buffer-size of them) and decodes
                      the whole GOP chunk back to the previous IDR/CRA once
                      per snapshot, then keeps only the frames asked for.
                      Referenced from --csv-out/--json-out/console as
                      "pre_frame_snapshot_paths" (oldest first), while
                      "snapshot_path" keeps meaning the IDR's own frame.
  --ts-dump-dir DIR   Continuously chop the RAW, byte-exact multicast feed
                      (every PID -- not just video/SCTE-35, so the file is
                      a proper standalone .ts playable in VLC/ffplay or
                      loadable in TSDuck) into fixed-length windows
                      (--ts-dump-window), and only WRITE a window to disk
                      if at least one SCTE-35 message was decoded during
                      it -- so you get a raw capture around every actual
                      SCTE-35 event for offline forensic analysis, without
                      recording 24/7. By default the immediately preceding
                      window is prepended too (--ts-dump-no-preroll to
                      disable), so an event near the start of a window
                      still has real pre-roll context instead of an
                      abrupt cut. Each saved dump gets a JSON sidecar
                      (window start/end, SCTE-35 cue_seq values seen --
                      joinable with --scte35-out) alongside the .ts file.
                      See --ts-dump-window/--ts-dump-all/--ts-dump-max-files
                      and the "Raw TS dump around SCTE-35 events" README
                      section for the memory/disk trade-offs before
                      enabling this on a high-bitrate feed.
  time_to_event_ms / actual_preroll_ms / preroll_delta_ms / preroll_verdict
                      Always computed (no flag needed), in every match/miss
                      CSV/JSON row and the console verdict line: how much
                      lead time the SCTE-35 cue's own PTS declared
                      (time_to_event_ms), how much lead time was actually
                      observed in real time (actual_preroll_ms), and their
                      difference (preroll_delta_ms). Flagged
                      preroll_verdict=PREROLL_SHORT when the actual pre-roll
                      falls more than --preroll-tolerance-ms short of the
                      declared one -- i.e. downstream ad-decisioning got
                      less real reaction time than the cue implied. See
                      caveat 6 above and the "Time to event vs. actual
                      pre-roll" README section before treating this as a
                      strict SCTE-35 conformance check -- it is not one.
  signal_verdict      Always computed (no flag needed): whether the FIRST
                      registered transmission of a given splice_event_id
                      met SCTE-35's own apparent minimum advance-notice
                      requirement (>= --min-time-to-event-ms, default
                      4000ms -- see caveat 7 above). SIGNAL_LATE if below
                      that floor, OK if not, RETRANSMISSION for later
                      repeats of the same event_id (not re-evaluated), N/A
                      if time_to_event_ms itself could not be computed.
                      Unlike preroll_verdict, this one IS meant to reflect
                      an actual (secondary-source-cited, not
                      primary-verified) SCTE-35 requirement -- see caveat 7.
"""

import argparse
import collections
import csv
import json
import logging
import os
import queue
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

try:
    from threefive3 import Cue
except ImportError:  # fall back to the older package name
    try:
        from threefive import Cue
    except ImportError:
        Cue = None

__version__ = "0.8"

TS_PACKET_SIZE = 188
SYNC_BYTE = 0x47
PTS_HZ = 90000
PTS_MAX = 1 << 33  # 33-bit PTS counter

STREAM_TYPE_MPEG2_VIDEO = 0x02
STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_HEVC = 0x24
STREAM_TYPE_SCTE35 = 0x86

VIDEO_STREAM_TYPES = {
    STREAM_TYPE_MPEG2_VIDEO: "mpeg2",
    STREAM_TYPE_H264: "h264",
    STREAM_TYPE_HEVC: "hevc",
}

log = logging.getLogger("scte35_idr_diff")


# --------------------------------------------------------------------------
# PTS helpers
# --------------------------------------------------------------------------

def pts_diff_seconds(a_ticks, b_ticks):
    """Signed difference (a - b) in seconds, accounting for 33-bit wraparound
    by picking the shorter path around the circle."""
    diff = (a_ticks - b_ticks) % PTS_MAX
    if diff > PTS_MAX // 2:
        diff -= PTS_MAX
    return diff / PTS_HZ


def read_pts_dts(bits5):
    """Decode a 5-byte (40-bit) PTS or DTS field per ISO/IEC 13818-1."""
    b = bits5
    pts = ((b[0] >> 1) & 0x07) << 30
    pts |= b[1] << 22
    pts |= (b[2] >> 1) << 15
    pts |= b[3] << 7
    pts |= b[4] >> 1
    return pts


# --------------------------------------------------------------------------
# PSI (PAT/PMT) parsing -- just enough to locate video + SCTE-35 PIDs
# --------------------------------------------------------------------------

class SectionReassembler:
    """Reassembles PSI sections (PAT/PMT) from TS packets on one PID."""

    def __init__(self):
        self.buf = bytearray()
        self.started = False

    def push(self, payload, pusi):
        if pusi:
            pointer = payload[0]
            self.buf = bytearray(payload[1 + pointer:])
            self.started = True
        elif self.started:
            self.buf += payload
        sections = []
        while self.started and len(self.buf) >= 3:
            section_length = ((self.buf[1] & 0x0F) << 8) | self.buf[2]
            total = 3 + section_length
            if len(self.buf) < total:
                break
            sections.append(bytes(self.buf[:total]))
            self.buf = self.buf[total:]
        return sections


def parse_pat(section):
    """Returns list of (program_number, pmt_pid)."""
    programs = []
    data = section[8:-4]  # skip section header, drop CRC32
    for i in range(0, len(data), 4):
        program_number = (data[i] << 8) | data[i + 1]
        pid = ((data[i + 2] & 0x1F) << 8) | data[i + 3]
        if program_number != 0:  # skip NIT PID entry
            programs.append((program_number, pid))
    return programs


def parse_pmt(section):
    """Returns (pcr_pid, [(stream_type, elementary_pid, descriptors_bytes)])."""
    pcr_pid = ((section[8] & 0x1F) << 8) | section[9]
    program_info_length = ((section[10] & 0x0F) << 8) | section[11]
    pos = 12 + program_info_length
    end = len(section) - 4  # drop CRC32
    streams = []
    while pos < end:
        stream_type = section[pos]
        elementary_pid = ((section[pos + 1] & 0x1F) << 8) | section[pos + 2]
        es_info_length = ((section[pos + 3] & 0x0F) << 8) | section[pos + 4]
        descriptors = section[pos + 5: pos + 5 + es_info_length]
        streams.append((stream_type, elementary_pid, descriptors))
        pos += 5 + es_info_length
    return pcr_pid, streams


def _is_jsonable(value):
    try:
        json.dumps(value)
        return True
    except TypeError:
        return False


def _shallow_obj_to_dict(obj):
    """Best-effort dump of a threefive sub-object's public attributes,
    keeping only JSON-serializable values. Used as a last-resort fallback
    if cue.get()/cue.get_json() are unavailable in a given threefive
    version."""
    if obj is None:
        return None
    try:
        return {k: v for k, v in vars(obj).items()
                if not k.startswith("_") and _is_jsonable(v)}
    except TypeError:
        return str(obj)


def cue_to_dict(cue):
    """Return the FULL decoded SCTE-35 cue as a plain dict: info_section,
    command, and every descriptor (segmentation_descriptor etc.) with all
    their fields -- not just the handful of fields this tool's matching
    logic cares about. Tries threefive's own cue.get() / cue.get_json()
    first (these already return everything threefive parsed), and only
    falls back to a manual attribute dump if those aren't available."""
    try:
        d = cue.get()
        if isinstance(d, dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    try:
        j = cue.get_json()
        if isinstance(j, str):
            return json.loads(j)
        if isinstance(j, dict):
            return j
    except Exception:  # noqa: BLE001
        pass
    return {
        "info_section": _shallow_obj_to_dict(getattr(cue, "info_section", None)),
        "command": _shallow_obj_to_dict(getattr(cue, "command", None)),
        "descriptors": [_shallow_obj_to_dict(d) for d in (getattr(cue, "descriptors", None) or [])],
    }


def summarize_descriptors(cue):
    """Human-readable one-liner per descriptor (mainly segmentation_descriptor,
    which is where SCTE-35 carries the actual event semantics -- CUE-OUT/IN
    type, segmentation_event_id, UPID, duration -- for time_signal() cues,
    since the time_signal command itself carries no event identity)."""
    lines = []
    for d in (getattr(cue, "descriptors", None) or []):
        cls = type(d).__name__
        fields = []
        for name in ("segmentation_event_id", "segmentation_type_id",
                     "segmentation_type_id_name", "segmentation_upid_type",
                     "segmentation_upid", "segment_num", "segments_expected",
                     "segmentation_duration", "duration"):
            val = getattr(d, name, None)
            if val is not None:
                fields.append(f"{name}={val}")
        lines.append(cls + ("(" + ", ".join(fields) + ")" if fields else ""))
    return lines


def has_cuei_registration(descriptors):
    """SCTE-35 ES loops should carry a registration_descriptor (tag 0x05)
    with format_identifier 'CUEI'."""
    pos = 0
    while pos + 2 <= len(descriptors):
        tag = descriptors[pos]
        length = descriptors[pos + 1]
        payload = descriptors[pos + 2: pos + 2 + length]
        if tag == 0x05 and payload[:4] == b"CUEI":
            return True
        pos += 2 + length
    return False


# --------------------------------------------------------------------------
# PES reassembly
# --------------------------------------------------------------------------

class PesReassembler:
    """Reassembles PES packets from TS packets on one PID and yields
    (pts_ticks_or_None, dts_ticks_or_None, elementary_stream_payload)."""

    def __init__(self):
        self.buf = bytearray()
        self.started = False

    def push(self, payload, pusi):
        out = None
        if pusi:
            if self.started and self.buf:
                out = self._parse(self.buf)
            self.buf = bytearray(payload)
            self.started = True
        elif self.started:
            self.buf += payload
        return out

    def flush(self):
        if self.started and self.buf:
            out = self._parse(self.buf)
            self.buf = bytearray()
            return out
        return None

    @staticmethod
    def _parse(buf):
        if len(buf) < 9 or buf[0:3] != b"\x00\x00\x01":
            return None
        flags = buf[7]
        pts_dts_flags = (flags >> 6) & 0x03
        header_data_length = buf[8]
        header_end = 9 + header_data_length
        if header_end > len(buf):
            return None
        pts = dts = None
        off = 9
        if pts_dts_flags in (0x02, 0x03):
            pts = read_pts_dts(buf[off:off + 5])
            off += 5
        if pts_dts_flags == 0x03:
            dts = read_pts_dts(buf[off:off + 5])
            off += 5
        es_payload = bytes(buf[header_end:])
        return pts, dts, es_payload


# --------------------------------------------------------------------------
# NAL unit scanning (Annex B) -- H.264 / HEVC IDR detection
# --------------------------------------------------------------------------

def iter_nal_units(data):
    """Yield (nal_type_or_None, start_offset, end_offset) for every Annex B
    NAL unit in data, where data[start_offset:end_offset] is the COMPLETE
    NAL unit including its own start code prefix (3 or 4 bytes). nal_type
    is None here (codec-agnostic offsets only); callers derive the type
    from the header byte per codec, since H.264/HEVC mask it differently."""
    n = len(data)
    start_codes = []  # offsets of the 00 00 01 (3-byte) marker itself
    i = 0
    while i < n - 2:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            start_codes.append(i)
            i += 3
        else:
            i += 1
    units = []
    for idx, sc in enumerate(start_codes):
        unit_start = sc - 1 if sc > 0 and data[sc - 1] == 0 else sc
        header_off = sc + 3
        unit_end = start_codes[idx + 1] - 1 if idx + 1 < len(start_codes) else n
        # unit_end above assumes the next unit's start code has no leading
        # zero byte belonging to this unit's trailing padding; trim any
        # leftover trailing zero bytes is unnecessary for our purposes
        # (type/parameter-set extraction only reads the header + payload).
        if idx + 1 < len(start_codes):
            next_sc = start_codes[idx + 1]
            next_unit_start = next_sc - 1 if next_sc > 0 and data[next_sc - 1] == 0 else next_sc
            unit_end = next_unit_start
        if header_off >= n:
            continue
        units.append((header_off, unit_start, unit_end))
    return units


def find_nal_types(es_payload, codec):
    """Yield nal_unit_type for every NAL unit found via Annex B start codes."""
    for header_off, _unit_start, _unit_end in iter_nal_units(es_payload):
        b0 = es_payload[header_off]
        if codec == "h264":
            yield b0 & 0x1F
        elif codec == "hevc":
            yield (b0 >> 1) & 0x3F


def extract_param_set_nals(es_payload, codec):
    """Return the raw bytes (with start codes) of every SPS/PPS (H.264) or
    VPS/SPS/PPS (HEVC) NAL unit found in this access unit, concatenated in
    order. Used to build a cache of the most recently seen parameter sets,
    so a lone IDR access unit can still be decoded standalone by ffmpeg
    even on a stream where repeatHeaders=0 (parameter sets sent only once,
    not repeated before every IDR)."""
    out = bytearray()
    for header_off, unit_start, unit_end in iter_nal_units(es_payload):
        b0 = es_payload[header_off]
        if codec == "h264":
            nal_type = b0 & 0x1F
            is_param_set = nal_type in (7, 8)  # SPS, PPS
        elif codec == "hevc":
            nal_type = (b0 >> 1) & 0x3F
            is_param_set = nal_type in (32, 33, 34)  # VPS, SPS, PPS
        else:
            is_param_set = False
        if is_param_set:
            out += es_payload[unit_start:unit_end]
    return bytes(out)


def has_param_sets(es_payload, codec):
    """True if this access unit already carries its own SPS/PPS (H.264) or
    VPS/SPS/PPS (HEVC) -- e.g. repeatHeaders=1 on the encoder -- meaning we
    should NOT prepend cached parameter sets before feeding it to ffmpeg
    (that would duplicate them)."""
    return len(extract_param_set_nals(es_payload, codec)) > 0


def classify_idr(nal_types, codec, include_cra):
    """Returns 'idr', 'cra', or None for a set of NAL types found in one AU."""
    types = set(nal_types)
    if codec == "h264":
        return "idr" if 5 in types else None
    if codec == "hevc":
        if types & {19, 20}:
            return "idr"
        if include_cra and 21 in types:
            return "cra"
        return None
    if codec == "mpeg2":
        # Not handled at NAL granularity; MPEG-2 uses picture_coding_type
        # in the picture header. Out of scope for this tool -- flag it.
        return None
    return None


# --------------------------------------------------------------------------
# Misc CLI helpers
# --------------------------------------------------------------------------

def parse_duration_seconds(value):
    """Parse a human duration like '30', '30s', '90s', '2m', '1.5m', '1h'
    into seconds (float). A bare number is seconds. Used for
    --ts-dump-window so the window can be given in whichever unit is
    convenient (seconds or minutes)."""
    s = str(value).strip().lower()
    # Longest suffix first so "ms" is checked before the bare "s" suffix
    # (which "ms" would also match).
    for suffix, multiplier in (("ms", 0.001), ("s", 1.0), ("m", 60.0), ("h", 3600.0)):
        if s.endswith(suffix):
            number_part = s[: -len(suffix)]
            try:
                return float(number_part) * multiplier
            except ValueError:
                raise argparse.ArgumentTypeError(f"invalid duration: {value!r}")
    try:
        return float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid duration: {value!r}")


def validate_ipv4_literal(value, flag_name):
    """Raise a clear, actionable error if `value` is not a plain dotted-
    quad IPv4 address (e.g. 239.1.1.1) -- used for --addr/--iface.

    Why this exists: socket.socket.bind((host, port)) resolves `host`
    through the system's normal getaddrinfo()/NSS machinery even when it's
    already a numeric literal, same as it would for a DNS hostname. If
    `value` is anything other than a valid IPv4 literal (a typo, a stray
    character, or an actual hostname that isn't resolvable on THIS host --
    e.g. a headend/internal DNS name that only exists on the server you
    tested on before), bind() fails with the opaque
    "socket.gaierror: [Errno -2] Name or service not known" and a raw
    traceback instead of a message that says what's actually wrong.
    socket.inet_aton() below does pure string parsing -- it never touches
    DNS/NSS -- so this check fails fast with a clear diagnosis before any
    network call is attempted."""
    try:
        socket.inet_aton(value)
    except OSError:
        log.error(
            "%s must be a plain numeric IPv4 address (e.g. 239.1.1.1), not a hostname -- "
            "got %r. This is also the most common cause of the confusing "
            "'socket.gaierror: Name or service not known' error: a DNS name (or internal "
            "hosts-file entry) that resolved on one server may simply not exist on another.",
            flag_name, value)
        sys.exit(1)


# --------------------------------------------------------------------------
# Multicast / RTP ingest
# --------------------------------------------------------------------------

def open_multicast_socket(addr, port, iface):
    # Validate BEFORE any network call: inet_aton() is pure string parsing
    # (never touches DNS/NSS), so this fails fast with a clear message
    # instead of bind() below raising an opaque socket.gaierror -- see
    # validate_ipv4_literal() for why that matters on a host where addr
    # isn't a valid literal (typo, or a hostname not resolvable here).
    validate_ipv4_literal(addr, "--addr")
    if iface:
        validate_ipv4_literal(iface, "--iface")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass  # not available on every platform; SO_REUSEADDR above is enough on Linux
    # Bind to the SPECIFIC multicast group address, not "" (INADDR_ANY).
    # A socket bound to INADDR_ANY:port is only filtered by destination
    # PORT, not by multicast group -- if another process on this host has
    # joined a *different* group on the same port (extremely common in a
    # headend where many SPTS channels share one UDP port across different
    # multicast addresses), the kernel delivers BOTH groups' traffic to
    # BOTH sockets once either one has caused that port to receive
    # multicast at all. Binding to the group address instead of "" makes
    # the kernel filter by destination address too, so running several
    # instances of this tool against different multicast groups (even on
    # the same port) no longer cross-contaminates each other's PAT/PMT/
    # SCTE-35/video parsing.
    try:
        sock.bind((addr, port))
    except socket.gaierror as exc:
        # Defense in depth: validate_ipv4_literal() above should already
        # have caught a non-numeric addr, so reaching this normally means
        # something more unusual about the host's resolver setup. Fail with
        # a diagnosis instead of a bare traceback either way.
        log.error(
            "Failed to bind to %s:%d (%s). %r passed IPv4-literal validation but the "
            "system's own address resolution (getaddrinfo) still rejected it -- this can "
            "happen on a host with a broken/missing resolver configuration "
            "(e.g. no /etc/nsswitch.conf or /etc/resolv.conf). Try binding to 0.0.0.0 "
            "manually to confirm, or check the host's network/DNS configuration.",
            addr, port, exc, addr)
        sock.close()
        sys.exit(1)
    mreq = struct.pack("4s4s", socket.inet_aton(addr),
                        socket.inet_aton(iface) if iface else socket.INADDR_ANY.to_bytes(4, "big"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(2.0)
    return sock


def looks_like_rtp(payload):
    if len(payload) < 12:
        return False
    version = (payload[0] >> 6) & 0x03
    payload_type = payload[1] & 0x7F
    # RTP v2 carrying MPEG-TS is payload type 33 (MP2T) by convention, but
    # some vendors use dynamic PTs -- version==2 plus a TS sync byte right
    # after a plausible 12-byte header is a decent heuristic fallback.
    if version == 2 and payload_type == 33:
        return True
    if version == 2 and len(payload) > 12 and payload[12] == SYNC_BYTE:
        return True
    return False


def strip_rtp(payload):
    if len(payload) < 12:
        return payload
    csrc_count = payload[0] & 0x0F
    header_len = 12 + 4 * csrc_count
    ext = (payload[0] >> 4) & 0x01
    if ext and len(payload) >= header_len + 4:
        ext_len_words = struct.unpack(">H", payload[header_len + 2:header_len + 4])[0]
        header_len += 4 + 4 * ext_len_words
    return payload[header_len:]


# --------------------------------------------------------------------------
# Main probe
# --------------------------------------------------------------------------

class Probe:
    def __init__(self, args):
        self.args = args
        self.pat_reasm = SectionReassembler()
        self.pmt_reasm = None
        self.pmt_pid = None
        self.video_pid = args.pid_video
        self.scte35_pid = args.pid_scte35
        self.video_codec = args.codec
        self.pes_video = PesReassembler()
        self.pes_scte35 = PesReassembler()  # SCTE-35 PID often section-based, handled separately
        self.scte35_section = SectionReassembler()
        self.pending = []  # list of dicts: target_pts, event_id, kind, deadline_wallclock
        self.match_window_s = args.tolerance_ms / 1000.0
        self.max_early_s = args.max_early_ms / 1000.0
        self.timeout_s = args.timeout_s
        self.ok_threshold_ms = args.ok_threshold_ms
        self.preroll_tolerance_ms = getattr(args, "preroll_tolerance_ms", 500.0)
        # SCTE-35's own minimum advance-notice requirement (per secondary
        # sources citing ANSI/SCTE 35 (2019) 9.2/10.3.3: "sent at least once
        # a minimum of 4 seconds in advance of the desired splice time") --
        # see _register_cue()/signal_verdict and the "Time to event vs.
        # actual pre-roll" README section for the caveats on this citation
        # and on what "first signal" means for a retransmitted event.
        self.min_time_to_event_ms = getattr(args, "min_time_to_event_ms", 4000.0)
        # splice_event_id values already seen, so a legitimately retransmitted
        # copy of the same event (common practice, to survive packet loss) is
        # not re-evaluated against the 4-second-minimum requirement -- that
        # requirement is about the FIRST time the event is signaled, not
        # every repeat as the target PTS gets closer (which would trivially
        # -- and wrongly -- flag every retransmitted event as late).
        self._seen_scte35_event_ids = set()
        self.include_cra = args.include_cra
        # Most recently observed video access unit's PTS, updated on EVERY
        # AU (not just IDRs) -- used as the "live video position" reference
        # point for computing each cue's declared time-to-event at the
        # moment it's registered. See _register_cue()/_emit() for the
        # actual/declared pre-roll measurement this feeds.
        self.last_video_pts_ticks = None
        self.scte35_seq = 0
        self.csv_writer = None
        self.csv_file = None
        self.json_out = None
        self.scte35_out = None
        self.scte35_logger = log
        self.last_param_sets = b""  # most recently seen SPS/PPS(/VPS) NALs
        self.snapshot_dir = args.snapshot_dir
        self.snapshot_all_idr = args.snapshot_all_idr
        self.pre_frames = max(0, getattr(args, "pre_frames", 0) or 0)
        au_buffer_size = getattr(args, "au_buffer_size", None) or max(300, self.pre_frames * 15)
        # Rolling buffer of EVERY access unit (not just IDRs), in transport/
        # decode order, so a snapshot request for "N frames before this IDR"
        # can walk back to the previous IDR/CRA (a guaranteed decodable GOP
        # boundary) and hand ffmpeg a complete chunk. Only allocated when
        # --pre-frames is actually requested.
        self.au_buffer = collections.deque(maxlen=au_buffer_size) if self.pre_frames > 0 else None
        # idr_ticks -> [pre-frame snapshot paths, oldest first], populated by
        # _enqueue_snapshot_sequence and consumed by _emit(). Bounded so a
        # long-running probe with --snapshot-all-idr (which enqueues
        # sequences for IDRs that never end up matching a pending cue, and
        # so are never popped) can't grow this unboundedly.
        self._pending_pre_frame_paths = collections.OrderedDict()
        self.snapshot_queue = None
        self.snapshot_thread = None
        if self.snapshot_dir:
            os.makedirs(self.snapshot_dir, exist_ok=True)
            self.snapshot_queue = queue.Queue()
            self.snapshot_thread = threading.Thread(
                target=self._snapshot_worker, daemon=True)
            self.snapshot_thread.start()

        # -- Raw TS dump around SCTE-35 events --------------------------
        self.ts_dump_dir = getattr(args, "ts_dump_dir", None)
        self.ts_dump_window_s = getattr(args, "ts_dump_window", None)
        self.ts_dump_all = bool(getattr(args, "ts_dump_all", False))
        self.ts_dump_include_previous = not getattr(args, "ts_dump_no_preroll", False)
        self.ts_dump_max_files = getattr(args, "ts_dump_max_files", None)
        self.ts_dump_queue = None
        self.ts_dump_thread = None
        self._ts_dump_cur = None
        self._ts_dump_prev = b""
        self._ts_dump_window_start = None
        self._ts_dump_window_start_wall = None
        self._ts_dump_event_count = 0
        self._ts_dump_event_seqs = []
        self._ts_dump_seq = 0  # monotonic counter, disambiguates filenames within one wallclock second
        if self.ts_dump_dir:
            os.makedirs(self.ts_dump_dir, exist_ok=True)
            self._ts_dump_cur = bytearray()
            self._ts_dump_window_start = time.monotonic()
            self._ts_dump_window_start_wall = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.ts_dump_queue = queue.Queue()
            self.ts_dump_thread = threading.Thread(
                target=self._ts_dump_worker, daemon=True)
            self.ts_dump_thread.start()
            log.info(
                "TS dump enabled: window=%.1fs dir=%s only-on-event=%s "
                "include-previous-window=%s max-files=%s",
                self.ts_dump_window_s, self.ts_dump_dir, not self.ts_dump_all,
                self.ts_dump_include_previous, self.ts_dump_max_files or "unlimited")

        if args.csv_out:
            self.csv_file = open(args.csv_out, "a", newline="")
            self.csv_writer = csv.writer(self.csv_file)
            if self.csv_file.tell() == 0:
                self.csv_writer.writerow([
                    # NOTE: if you already have an older CSV from before this
                    # column set, point --csv-out at a NEW file -- the header
                    # is only (re)written when the file is empty, so an old
                    # file would silently get misaligned columns appended.
                    "wallclock", "cue_seq", "event_id", "command_type", "out_of_network",
                    "target_pts_s", "raw_pts_time_s", "pts_adjustment_ticks",
                    "idr_pts_s", "delta_ms", "verdict", "codec", "au_kind",
                    "segmentation_summary", "snapshot_path", "pre_frame_snapshot_paths",
                    "time_to_event_ms", "actual_preroll_ms", "preroll_delta_ms", "preroll_verdict",
                    "signal_verdict",
                ])
        if args.json_out:
            self.json_out = open(args.json_out, "a")
        if args.scte35_out:
            self.scte35_out = open(args.scte35_out, "a")
        if args.scte35_out or args.scte35_log_file:
            # Separate logger -> optionally a separate handler/file,
            # independent of the main console log and of --csv-out/--json-out
            # (which only record match/miss *outcomes*, not the raw SCTE-35
            # content). Set an explicit level so this works regardless of
            # whatever level the root logger ends up at.
            self.scte35_logger = logging.getLogger("scte35_idr_diff.scte35")
            self.scte35_logger.setLevel(logging.DEBUG if args.verbose else logging.INFO)
            self.scte35_logger.propagate = True  # still show a summary on console too
            if args.scte35_log_file:
                handler = logging.FileHandler(args.scte35_log_file)
                handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                handler.setLevel(logging.DEBUG if args.verbose else logging.INFO)
                self.scte35_logger.addHandler(handler)
                self.scte35_logger.propagate = False  # avoid double-printing to console

    # -- PSI handling --------------------------------------------------

    def handle_pat(self, payload, pusi):
        for section in self.pat_reasm.push(payload, pusi):
            programs = parse_pat(section)
            if not programs:
                continue
            program_number, pmt_pid = programs[0]
            if self.args.program and self.args.program != program_number:
                for pn, pid_ in programs:
                    if pn == self.args.program:
                        pmt_pid = pid_
                        break
            if pmt_pid != self.pmt_pid:
                log.info("PAT: program %d -> PMT PID 0x%X", program_number, pmt_pid)
                self.pmt_pid = pmt_pid
                self.pmt_reasm = SectionReassembler()

    def handle_pmt(self, payload, pusi):
        for section in self.pmt_reasm.push(payload, pusi):
            pcr_pid, streams = parse_pmt(section)
            for stream_type, pid_, descriptors in streams:
                if stream_type in VIDEO_STREAM_TYPES:
                    # Auto-detect the video PID if none was given on the
                    # command line; either way, fill in the codec from the
                    # PMT stream_type if --codec was not forced explicitly.
                    if self.video_pid is None:
                        self.video_pid = pid_
                        log.info("PMT: video PID 0x%X (%s)", pid_, VIDEO_STREAM_TYPES[stream_type])
                    if pid_ == self.video_pid and self.video_codec is None:
                        self.video_codec = VIDEO_STREAM_TYPES[stream_type]
                        log.info("PMT: codec for video PID 0x%X is %s", pid_, self.video_codec)
                if stream_type == STREAM_TYPE_SCTE35 and self.scte35_pid is None:
                    self.scte35_pid = pid_
                    cuei = has_cuei_registration(descriptors)
                    log.info("PMT: SCTE-35 PID 0x%X (CUEI registration descriptor %s)",
                             pid_, "present" if cuei else "absent -- accepted on stream_type 0x86 alone")

    # -- SCTE-35 handling ------------------------------------------------

    def handle_scte35(self, payload, pusi):
        for section in self.scte35_section.push(payload, pusi):
            if Cue is None:
                log.error("threefive3 is not installed; cannot decode SCTE-35. "
                          "pip install threefive3 --break-system-packages")
                continue
            try:
                cue = Cue(section)
                cue.decode()
            except Exception as exc:  # noqa: BLE001 - keep the probe alive
                log.warning("Failed to decode SCTE-35 section: %s", exc)
                continue
            self.scte35_seq += 1
            seq = self.scte35_seq
            self._ts_dump_note_event(seq)
            self._log_full_cue(seq, cue, section)
            self._register_cue(cue, seq)

    def _log_full_cue(self, seq, cue, section_bytes):
        """Unconditionally record EVERY decoded SCTE-35 message -- including
        splice_null, canceled events, and immediate splices that
        _register_cue() below has nothing to match and will skip -- to the
        dedicated SCTE-35 log/file. This is independent of the match/miss
        CSV or JSON output, which only records outcomes for commands that
        carry a time-specified pts_time."""
        cmd = getattr(cue, "command", None)
        command_type = type(cmd).__name__ if cmd is not None else "Unknown"
        descriptor_summary = summarize_descriptors(cue)
        self.scte35_logger.info(
            "SCTE-35 #%d type=%s descriptors=[%s] section=%d bytes",
            seq, command_type, "; ".join(descriptor_summary) or "none", len(section_bytes),
        )
        if self.scte35_out:
            full = cue_to_dict(cue)
            self.scte35_out.write(json.dumps({
                "tool_version": __version__,
                "cue_seq": seq,
                "wallclock": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "scte35_pid": self.scte35_pid,
                "command_type": command_type,
                "descriptor_summary": descriptor_summary,
                "section_hex": section_bytes.hex(),
                "cue": full,
            }, default=str) + "\n")
            self.scte35_out.flush()

    def _register_cue(self, cue, seq):
        info = getattr(cue, "info_section", None)
        cmd = getattr(cue, "command", None)
        if cmd is None:
            return
        pts_time = getattr(cmd, "pts_time", None)
        if pts_time is None:
            self.scte35_logger.debug(
                "SCTE-35 #%d has no time-specified pts_time (immediate splice, "
                "splice_null, or canceled event) -- nothing to match against IDR.", seq)
            return
        pts_adjustment = getattr(info, "pts_adjustment", 0) or 0
        target_ticks = (int(round(pts_time * PTS_HZ)) + int(pts_adjustment)) % PTS_MAX
        event_id = getattr(cmd, "splice_event_id", None)
        out_of_network = getattr(cmd, "out_of_network_indicator", None)
        command_type = type(cmd).__name__
        segmentation_summary = summarize_descriptors(cue)
        now = time.monotonic()
        # "Declared" time-to-event: how far in the future the splice is
        # signaled to occur, measured in the PTS domain, relative to the
        # most recently observed video PTS at the moment THIS cue arrives
        # (i.e. the live video position the probe has actually seen so
        # far -- an approximation of "the PTS at which the SCTE-35 message
        # itself was transmitted", the same concept professional TS
        # analyzers derive via PCR/PTS correlation, here approximated by
        # nearest-preceding video AU instead of a full PCR clock recovery).
        # None if no video AU has been seen yet when the cue arrives.
        if self.last_video_pts_ticks is not None:
            time_to_event_ms = pts_diff_seconds(target_ticks, self.last_video_pts_ticks) * 1000.0
        else:
            time_to_event_ms = None
            self.scte35_logger.debug(
                "SCTE-35 #%d: no video PTS observed yet -- cannot compute a "
                "declared time-to-event for this cue.", seq)

        # -- signal_verdict: was THIS event first signaled far enough ahead
        # of its own splice time to meet SCTE-35's own minimum advance-
        # notice requirement (per secondary sources citing ANSI/SCTE 35
        # (2019) 9.2/10.3.3: "sent at least once a minimum of 4 seconds in
        # advance of the desired splice time", default --min-time-to-event-ms
        # 4000)? That requirement is about the FIRST transmission of a given
        # splice_event_id -- an encoder is expected to retransmit the same
        # event as insurance against packet loss, and each later retransmit
        # necessarily has a smaller time_to_event_ms as the splice point
        # approaches, so only the first occurrence of an event_id is
        # evaluated; later ones are reported as RETRANSMISSION rather than
        # re-flagged as late. If event_id itself is missing/None (nothing to
        # dedupe on), every occurrence is treated as first.
        if event_id is not None and event_id in self._seen_scte35_event_ids:
            signal_verdict = "RETRANSMISSION"
        else:
            if event_id is not None:
                self._seen_scte35_event_ids.add(event_id)
            if time_to_event_ms is None:
                signal_verdict = "N/A"
            elif time_to_event_ms < self.min_time_to_event_ms:
                signal_verdict = "SIGNAL_LATE"
            else:
                signal_verdict = "OK"

        entry = {
            "cue_seq": seq,
            "target_ticks": target_ticks,
            "raw_pts_time": pts_time,
            "pts_adjustment": pts_adjustment,
            "event_id": event_id,
            "command_type": command_type,
            "out_of_network": out_of_network,
            "segmentation_summary": segmentation_summary,
            "deadline": now + self.timeout_s,
            "register_monotonic": now,
            "time_to_event_ms": time_to_event_ms,
            "signal_verdict": signal_verdict,
        }
        self.pending.append(entry)
        # Full detail goes to the dedicated SCTE-35 channel; the main log
        # stream stays focused on lifecycle (PAT/PMT/join) and the eventual
        # match/miss verdict in _emit(), so it doesn't get drowned out on a
        # busy SCTE-35 PID.
        self.scte35_logger.info(
            "SCTE-35 #%d queued for matching: %s event_id=%s target_pts=%.6fs "
            "(raw pts_time=%.6f, pts_adjustment=%s) time_to_event=%s "
            "signal_verdict=%s descriptors=[%s]",
            seq, command_type, event_id, target_ticks / PTS_HZ, pts_time,
            pts_adjustment,
            "n/a" if time_to_event_ms is None else f"{time_to_event_ms:.1f}ms",
            signal_verdict,
            "; ".join(segmentation_summary))
        if signal_verdict == "SIGNAL_LATE":
            # Worth surfacing immediately, not just once the eventual
            # match/miss resolves (which may be seconds away, or never, if
            # the splice is later missed) -- an operator watching the main
            # log should see this as soon as it's known.
            self.scte35_logger.warning(
                "SCTE-35 #%d event_id=%s: first-signaled time_to_event=%.1fms "
                "is BELOW the %.0fms SCTE-35 minimum advance-notice "
                "requirement (see README) -- signal_verdict=SIGNAL_LATE",
                seq, event_id, time_to_event_ms, self.min_time_to_event_ms)

    # -- Video / IDR handling --------------------------------------------

    def handle_video(self, payload, pusi):
        result = self.pes_video.push(payload, pusi)
        if result:
            self._process_video_pes(result)

    def _process_video_pes(self, parsed):
        pts, dts, es_payload = parsed
        if self.video_codec not in ("h264", "hevc"):
            return
        if pts is not None:
            # Track the live video position from EVERY access unit (not
            # just IDRs) -- this is the reference point _register_cue()
            # uses to compute each cue's declared time-to-event.
            self.last_video_pts_ticks = pts
        nal_types = list(find_nal_types(es_payload, self.video_codec))
        if self.snapshot_dir:
            # Keep the most recently seen parameter sets warm from EVERY
            # access unit (not just IDRs) so a lone IDR can still be
            # decoded standalone even on a repeatHeaders=0 stream that only
            # sends SPS/PPS once, at the very start.
            params = extract_param_set_nals(es_payload, self.video_codec)
            if params:
                self.last_param_sets = params
        kind = classify_idr(nal_types, self.video_codec, self.include_cra)
        # Buffer EVERY access unit (in transport/decode order) when
        # --pre-frames is enabled, BEFORE the "not an IDR -> return" early
        # exit below, since the whole point is to also have the non-IDR
        # frames around. pts is normally present on every AU; if it isn't
        # (shouldn't happen for a compliant stream) we still buffer with
        # pts=None so the deque stays contiguous, it just can't become an
        # IDR target itself.
        if self.au_buffer is not None:
            self.au_buffer.append({
                "pts": pts,
                "data": es_payload,
                "kind": kind,
                "has_params": bool(has_param_sets(es_payload, self.video_codec)),
            })
        if kind is None:
            return
        if pts is None:
            log.warning("IDR/CRA access unit found with no PTS in its PES "
                        "header -- cannot match against SCTE-35 (kind=%s)", kind)
            return
        # Snapshot BEFORE matching, using the current (pre-match) pending
        # state: any IDR that is about to match a pending splice point
        # necessarily has self.pending non-empty right now, so this always
        # captures splice-relevant IDRs even without --snapshot-all-idr.
        if self.snapshot_dir and (self.snapshot_all_idr or self.pending):
            if self.pre_frames > 0:
                self._enqueue_snapshot_sequence(pts, es_payload)
            else:
                self._enqueue_snapshot(pts, es_payload)
        self._match_idr(pts, kind)

    def _idr_snapshot_path(self, idr_ticks):
        return os.path.join(self.snapshot_dir,
                             f"idr_pts_{idr_ticks / PTS_HZ:.6f}_{self.video_codec}.jpg")

    def _pre_frame_snapshot_path(self, idr_ticks, offset, frame_pts):
        pts_label = f"{frame_pts / PTS_HZ:.6f}" if frame_pts is not None else "unknown"
        return os.path.join(
            self.snapshot_dir,
            f"idr_pts_{idr_ticks / PTS_HZ:.6f}_{self.video_codec}_pre{offset}_pts_{pts_label}.jpg")

    def _enqueue_snapshot(self, idr_ticks, es_payload):
        path = self._idr_snapshot_path(idr_ticks)
        if has_param_sets(es_payload, self.video_codec):
            data = es_payload  # already self-contained (e.g. repeatHeaders=1)
        elif self.last_param_sets:
            data = self.last_param_sets + es_payload
        else:
            log.debug("No cached SPS/PPS yet for snapshot %s -- ffmpeg may "
                      "fail to decode this one standalone.", path)
            data = es_payload
        self.snapshot_queue.put({"kind": "single", "path": path, "data": data,
                                  "codec": self.video_codec})

    def _enqueue_snapshot_sequence(self, idr_ticks, es_payload):
        """Enqueue a task that decodes the matched IDR PLUS up to
        self.pre_frames preceding frames (in display/PTS order) as separate
        JPEGs. P/B frames are not independently decodable, so we hand
        ffmpeg a complete, valid chunk: everything in self.au_buffer from
        the PREVIOUS IDR/CRA (a guaranteed GOP boundary) up to and
        including this IDR, decoded once, then we keep only the frames
        actually wanted."""
        buf = list(self.au_buffer) if self.au_buffer is not None else []
        # Find this IDR's own slot -- search from the end since it was just
        # appended; match on PTS (identity would also work but PTS is what
        # every caller already has).
        idr_idx = None
        for i in range(len(buf) - 1, -1, -1):
            if buf[i]["pts"] == idr_ticks and buf[i]["kind"] in ("idr", "cra"):
                idr_idx = i
                break
        if idr_idx is None:
            log.warning("Could not locate matched IDR (pts=%.6fs) in the "
                        "access-unit buffer -- falling back to a single-"
                        "frame snapshot.", idr_ticks / PTS_HZ)
            self._enqueue_snapshot(idr_ticks, es_payload)
            return
        # Walk backward from just before the IDR to find the previous
        # IDR/CRA to use as the decodable chunk's starting point. If none
        # is found (e.g. very start of the buffer/stream), fall back to
        # starting the chunk at the buffer's own beginning -- best effort;
        # ffmpeg will simply produce fewer usable leading frames if some of
        # those AUs turn out to reference frames older than the chunk.
        chunk_start = 0
        for i in range(idr_idx - 1, -1, -1):
            if buf[i]["kind"] in ("idr", "cra"):
                chunk_start = i
                break
        chunk = buf[chunk_start:idr_idx + 1]
        if len(chunk) <= 1:
            # Nothing usable before the IDR itself (e.g. stream start) --
            # just do the plain single-frame snapshot.
            self._enqueue_snapshot(idr_ticks, es_payload)
            return
        first = chunk[0]
        prefix = b"" if first["has_params"] else self.last_param_sets
        decode_bytes = prefix + b"".join(au["data"] for au in chunk)
        # ffmpeg's decoder emits frames in DISPLAY (PTS) order, so sort the
        # chunk the same way to know which decoded-frame index corresponds
        # to which access unit.
        ranked = sorted(
            (au for au in chunk if au["pts"] is not None),
            key=lambda au: au["pts"])
        idr_positions = [i for i, au in enumerate(ranked) if au["pts"] == idr_ticks]
        if not idr_positions:
            log.warning("Matched IDR (pts=%.6fs) missing from its own decode "
                        "chunk after sorting -- falling back to a single-"
                        "frame snapshot.", idr_ticks / PTS_HZ)
            self._enqueue_snapshot(idr_ticks, es_payload)
            return
        idr_pos = idr_positions[-1]
        lo = max(0, idr_pos - self.pre_frames)
        targets = []
        for rank_pos in range(lo, idr_pos + 1):
            offset = idr_pos - rank_pos  # 0 = the IDR itself, 1 = immediately before, ...
            frame_pts = ranked[rank_pos]["pts"]
            path = (self._idr_snapshot_path(idr_ticks) if offset == 0
                    else self._pre_frame_snapshot_path(idr_ticks, offset, frame_pts))
            targets.append({"rank_pos": rank_pos, "path": path})
        if lo > 0:
            log.debug("Only %d frame(s) available before IDR pts=%.6fs in the "
                      "buffer (requested %d) -- capturing what's available.",
                      idr_pos - lo, idr_ticks / PTS_HZ, self.pre_frames)
        # targets is ordered oldest -> newest, ending with the IDR itself
        # (offset 0); record everything but that last one as the "pre
        # frame" paths for _emit() to surface once matching completes.
        self._pending_pre_frame_paths[idr_ticks] = [t["path"] for t in targets[:-1]]
        while len(self._pending_pre_frame_paths) > 2000:
            self._pending_pre_frame_paths.popitem(last=False)
        self.snapshot_queue.put({
            "kind": "sequence",
            "decode_bytes": decode_bytes,
            "codec": self.video_codec,
            "total_frames": len(ranked),
            "targets": targets,
            "idr_ticks": idr_ticks,
        })

    def _snapshot_worker(self):
        """Runs in a background thread so decoding a frame to JPEG (an
        ffmpeg subprocess call, tens of milliseconds) never blocks the
        socket-receive loop -- a stall there risks dropping multicast
        packets, which matters far more than a snapshot landing a moment
        late."""
        input_format = {"h264": "h264", "hevc": "hevc"}
        while True:
            item = self.snapshot_queue.get()
            if item is None:
                self.snapshot_queue.task_done()
                break
            try:
                if item.get("kind") == "sequence":
                    self._run_sequence_snapshot(item, input_format)
                else:
                    self._run_single_snapshot(item, input_format)
            except Exception as exc:  # noqa: BLE001 -- keep the worker alive
                log.warning("Snapshot failed: %s", exc)
            finally:
                self.snapshot_queue.task_done()

    def _run_single_snapshot(self, item, input_format):
        path, data, codec = item["path"], item["data"], item["codec"]
        fmt = input_format.get(codec)
        if fmt is None:
            log.warning("Snapshot failed for %s: unsupported codec %s", path, codec)
            return
        proc = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", fmt, "-i", "pipe:0",
             "-frames:v", "1", "-q:v", "2", path],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=10,
        )
        if proc.returncode != 0 or not os.path.exists(path):
            log.warning("Snapshot failed for %s (ffmpeg exit %s): %s",
                        path, proc.returncode,
                        proc.stderr.decode(errors="replace").strip()[-300:])
        else:
            log.debug("Saved IDR snapshot: %s", path)

    def _run_sequence_snapshot(self, item, input_format):
        codec = item["codec"]
        targets = item["targets"]
        fmt = input_format.get(codec)
        if fmt is None:
            log.warning("Sequence snapshot failed: unsupported codec %s", codec)
            return
        with tempfile.TemporaryDirectory(prefix="scte35_idr_seq_") as tmpdir:
            pattern = os.path.join(tmpdir, "f_%06d.jpg")
            proc = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", fmt, "-i", "pipe:0",
                 "-vsync", "0", "-q:v", "2", pattern],
                input=item["decode_bytes"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=20,
            )
            if proc.returncode != 0:
                log.warning("Sequence snapshot decode failed (ffmpeg exit %s): %s",
                            proc.returncode,
                            proc.stderr.decode(errors="replace").strip()[-300:])
                return
            for target in targets:
                # ffmpeg numbers its output frames 1-based, in the same
                # (display/PTS) order we sorted `ranked` into, so
                # rank_pos (0-based) -> ffmpeg frame number rank_pos + 1.
                src = os.path.join(tmpdir, f"f_{target['rank_pos'] + 1:06d}.jpg")
                dest = target["path"]
                if not os.path.exists(src):
                    log.warning("Sequence snapshot missing expected frame %s "
                                "(decoded %s frame(s), wanted rank %d) for %s",
                                src, item.get("total_frames"), target["rank_pos"], dest)
                    continue
                shutil.copyfile(src, dest)
                log.debug("Saved snapshot: %s", dest)

    def _match_idr(self, idr_ticks, kind):
        now = time.monotonic()
        # drop timed-out pending entries and report them as missed
        still_pending = []
        for entry in self.pending:
            if now > entry["deadline"]:
                self._emit(entry, idr_ticks=None, kind=None, missed=True)
            else:
                still_pending.append(entry)
        self.pending = still_pending

        best = None
        best_diff = None
        for entry in self.pending:
            diff_s = pts_diff_seconds(idr_ticks, entry["target_ticks"])
            # Only accept an IDR at/after (target - max_early_s), never
            # arbitrarily far before it. A forced/reactive IDR cannot be
            # caused by a cue it hasn't been told about yet -- an IDR that
            # lands, say, 2.6s *before* the target is essentially always an
            # unrelated periodic IDR (from a non-zero intraPeriod, or a
            # gopPresetIdx GOP boundary) that happened to fall inside a
            # generous symmetric window, not a reaction to this cue. Without
            # this floor, the old symmetric "abs(diff) <= window" check would
            # greedily grab the first nearby IDR in processing order even
            # when it obviously precedes the cue by seconds, producing a
            # nonsensical large negative delta instead of waiting for the
            # real (later) candidate or timing out.
            if -self.max_early_s <= diff_s <= self.match_window_s:
                if best is None or abs(diff_s) < abs(best_diff):
                    best, best_diff = entry, diff_s
        if best is not None:
            self.pending.remove(best)
            self._emit(best, idr_ticks=idr_ticks, kind=kind, missed=False)

    def _emit(self, entry, idr_ticks, kind, missed):
        wallclock = time.strftime("%Y-%m-%dT%H:%M:%S")
        if missed:
            delta_ms = None
            verdict = "MISSED (no IDR near target PTS within timeout)"
            idr_pts_s = None
        else:
            delta_ms = pts_diff_seconds(idr_ticks, entry["target_ticks"]) * 1000.0
            idr_pts_s = idr_ticks / PTS_HZ
            if kind == "cra":
                verdict = "REVIEW (splice point lands on CRA, not IDR)"
            elif abs(delta_ms) <= self.ok_threshold_ms:
                verdict = "OK"
            else:
                verdict = "OUT_OF_SPEC"
        target_pts_s = entry["target_ticks"] / PTS_HZ
        snapshot_path = (self._idr_snapshot_path(idr_ticks)
                         if (self.snapshot_dir and not missed) else None)
        pre_frame_paths = (self._pending_pre_frame_paths.pop(idr_ticks, [])
                           if (self.snapshot_dir and not missed) else [])

        # -- Declared vs. actual pre-roll --------------------------------
        # time_to_event_ms: the DECLARED lead time, computed once at cue
        # registration (see _register_cue) from target_ticks minus the
        # video PTS observed at that moment -- a PTS-domain, "what the
        # cue itself claims" figure.
        # actual_preroll_ms: the REAL, wall-clock elapsed time between
        # registering the cue and actually observing/matching the IDR --
        # this is what a downstream ad-decisioning system genuinely got
        # to react in, regardless of what the PTS math promised. Only
        # meaningful for an actual match (a MISSED cue never got an IDR to
        # measure against); it also assumes the feed is arriving live/in
        # real time -- see the "Time to event vs. actual pre-roll" README
        # section for why a non-real-time replay invalidates this figure.
        time_to_event_ms = entry.get("time_to_event_ms")
        actual_preroll_ms = ((time.monotonic() - entry["register_monotonic"]) * 1000.0
                             if not missed else None)
        preroll_delta_ms = (None if (time_to_event_ms is None or actual_preroll_ms is None)
                            else actual_preroll_ms - time_to_event_ms)
        if preroll_delta_ms is None:
            preroll_verdict = "N/A"
        elif preroll_delta_ms < -self.preroll_tolerance_ms:
            # Actual delivered lead time fell short of what the cue's own
            # PTS declared -- the operationally important case: downstream
            # ad-decisioning/splicing may have gotten less warning than it
            # was promised.
            preroll_verdict = "PREROLL_SHORT"
        else:
            preroll_verdict = "OK"

        # signal_verdict was already decided at registration time (see
        # _register_cue) -- whether THIS event's first transmission met
        # SCTE-35's own minimum advance-notice requirement. Carried through
        # here unchanged so it rides along with the rest of the match/miss
        # outcome in every output channel.
        signal_verdict = entry.get("signal_verdict", "N/A")

        line = (f"[{wallclock}] event_id={entry['event_id']} "
                f"type={entry['command_type']} oon={entry['out_of_network']} "
                f"target_pts={target_pts_s:.6f}s "
                f"idr_pts={'n/a' if idr_pts_s is None else f'{idr_pts_s:.6f}s'} "
                f"delta={'n/a' if delta_ms is None else f'{delta_ms:+.1f}ms'} "
                f"verdict={verdict}"
                + (f" snapshot={snapshot_path}" if snapshot_path else "")
                + (f" pre_frames={len(pre_frame_paths)}" if pre_frame_paths else "")
                + (f" time_to_event={'n/a' if time_to_event_ms is None else f'{time_to_event_ms:.1f}ms'}"
                   f" actual_preroll={'n/a' if actual_preroll_ms is None else f'{actual_preroll_ms:.1f}ms'}"
                   f" preroll_verdict={preroll_verdict} signal_verdict={signal_verdict}"))
        (log.warning if missed or (delta_ms is not None and abs(delta_ms) > self.ok_threshold_ms)
         or preroll_verdict == "PREROLL_SHORT" or signal_verdict == "SIGNAL_LATE" else log.info)(line)

        if self.csv_writer:
            self.csv_writer.writerow([
                wallclock, entry.get("cue_seq"), entry["event_id"], entry["command_type"],
                entry["out_of_network"], f"{target_pts_s:.6f}",
                entry.get("raw_pts_time"), entry.get("pts_adjustment"),
                "" if idr_pts_s is None else f"{idr_pts_s:.6f}",
                "" if delta_ms is None else f"{delta_ms:.1f}",
                verdict, self.video_codec, kind,
                "; ".join(entry.get("segmentation_summary") or []),
                snapshot_path or "",
                "; ".join(pre_frame_paths),
                "" if time_to_event_ms is None else f"{time_to_event_ms:.1f}",
                "" if actual_preroll_ms is None else f"{actual_preroll_ms:.1f}",
                "" if preroll_delta_ms is None else f"{preroll_delta_ms:.1f}",
                preroll_verdict,
                signal_verdict,
            ])
            self.csv_file.flush()
        if self.json_out:
            self.json_out.write(json.dumps({
                "tool_version": __version__,
                "wallclock": wallclock, "cue_seq": entry.get("cue_seq"),
                "event_id": entry["event_id"],
                "command_type": entry["command_type"],
                "out_of_network": entry["out_of_network"],
                "target_pts_s": target_pts_s,
                "raw_pts_time_s": entry.get("raw_pts_time"),
                "pts_adjustment_ticks": entry.get("pts_adjustment"),
                "segmentation_summary": entry.get("segmentation_summary"),
                "idr_pts_s": idr_pts_s,
                "delta_ms": delta_ms, "verdict": verdict,
                "codec": self.video_codec, "au_kind": kind,
                "snapshot_path": snapshot_path,
                "pre_frame_snapshot_paths": pre_frame_paths,
                "time_to_event_ms": time_to_event_ms,
                "actual_preroll_ms": actual_preroll_ms,
                "preroll_delta_ms": preroll_delta_ms,
                "preroll_verdict": preroll_verdict,
                "signal_verdict": signal_verdict,
                # cross-reference with --scte35-out using cue_seq for the
                # complete raw SCTE-35 structure (all descriptor fields).
            }, default=str) + "\n")
            self.json_out.flush()

    # -- Raw TS dump around SCTE-35 events --------------------------------

    def _ts_dump_note_event(self, seq):
        """Called for EVERY decoded SCTE-35 message (see handle_scte35),
        regardless of whether it carries a time-specified pts_time -- for
        the purpose of "was there SCTE-35 activity in this window at all",
        a splice_null or canceled event is still activity worth dumping
        raw TS around."""
        if not self.ts_dump_dir:
            return
        self._ts_dump_event_count += 1
        self._ts_dump_event_seqs.append(seq)

    def _ts_dump_maybe_rotate(self):
        now = time.monotonic()
        if now - self._ts_dump_window_start < self.ts_dump_window_s:
            return
        self._rotate_ts_dump_window(now)

    def _rotate_ts_dump_window(self, now):
        """End the current dump window: write it out (with the previous
        window prepended, unless --ts-dump-no-preroll) if it qualifies --
        --ts-dump-all, or at least one SCTE-35 message was decoded during
        it -- then start a fresh window. The just-ended window is always
        kept as `_ts_dump_prev` for one more rotation regardless of
        whether IT gets saved, so the next window (if it has an event)
        gets real pre-roll context even when the event landed right at the
        start of its own window."""
        had_event = self._ts_dump_event_count > 0
        if (self.ts_dump_all or had_event) and self._ts_dump_cur:
            payload = (bytes(self._ts_dump_prev) + bytes(self._ts_dump_cur)
                       if self.ts_dump_include_previous else bytes(self._ts_dump_cur))
            self._enqueue_ts_dump(payload, self._ts_dump_event_count,
                                   list(self._ts_dump_event_seqs),
                                   self._ts_dump_window_start_wall)
        self._ts_dump_prev = self._ts_dump_cur
        self._ts_dump_cur = bytearray()
        self._ts_dump_event_count = 0
        self._ts_dump_event_seqs = []
        self._ts_dump_window_start = now
        self._ts_dump_window_start_wall = time.strftime("%Y-%m-%dT%H:%M:%S")

    def _enqueue_ts_dump(self, payload, event_count, event_seqs, window_start_wall):
        window_end_wall = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._ts_dump_seq += 1
        # Sequence number first, zero-padded, so filenames sort correctly
        # (and stay unique) even with a short --ts-dump-window that ends
        # more than one window inside the same wallclock second, or across
        # an NTP clock step.
        fname = (f"ts_dump_{self._ts_dump_seq:06d}_{window_end_wall.replace(':', '-')}"
                 f"_n{event_count}events.ts")
        path = os.path.join(self.ts_dump_dir, fname)
        meta = {
            "tool_version": __version__,
            "path": path,
            "window_start_wallclock": window_start_wall,
            "window_end_wallclock": window_end_wall,
            "window_s": self.ts_dump_window_s,
            "included_previous_window": self.ts_dump_include_previous,
            "event_count": event_count,
            "event_cue_seqs": event_seqs,
            "size_bytes": len(payload),
        }
        self.ts_dump_queue.put({"path": path, "data": bytes(payload), "meta": meta})

    def _ts_dump_worker(self):
        """Runs in a background thread so writing tens/hundreds of MB to
        disk never blocks the socket-receive loop."""
        while True:
            item = self.ts_dump_queue.get()
            if item is None:
                self.ts_dump_queue.task_done()
                break
            try:
                path, data, meta = item["path"], item["data"], item["meta"]
                with open(path, "wb") as f:
                    f.write(data)
                with open(path + ".json", "w") as f:
                    json.dump(meta, f, indent=2)
                log.info("Saved TS dump: %s (%d SCTE-35 event(s), %.1f MB)",
                          path, meta["event_count"], len(data) / 1e6)
                if self.ts_dump_max_files:
                    self._prune_ts_dumps()
            except Exception as exc:  # noqa: BLE001 -- keep the worker alive
                log.warning("Failed to write TS dump %s: %s", item.get("path"), exc)
            finally:
                self.ts_dump_queue.task_done()

    def _prune_ts_dumps(self):
        """Delete the oldest saved dumps (by filename, which sorts
        chronologically) once --ts-dump-max-files is exceeded, so a
        long-running probe on a busy SCTE-35 PID can't silently fill the
        disk."""
        try:
            files = sorted(f for f in os.listdir(self.ts_dump_dir)
                           if f.startswith("ts_dump_") and f.endswith(".ts"))
        except OSError:
            return
        excess = len(files) - self.ts_dump_max_files
        for fname in files[:max(0, excess)]:
            base = os.path.join(self.ts_dump_dir, fname)
            for p in (base, base + ".json"):
                try:
                    os.remove(p)
                except OSError:
                    pass
            log.info("Pruned old TS dump (over --ts-dump-max-files=%d): %s",
                      self.ts_dump_max_files, base)

    # -- TS packet dispatch ------------------------------------------------

    def handle_ts_packet(self, pkt):
        if pkt[0] != SYNC_BYTE:
            return
        if self.ts_dump_dir:
            # Buffer the RAW packet -- every PID, before any filtering
            # below -- so a saved dump is a complete, standalone .ts file,
            # not just the PIDs this tool otherwise cares about.
            self._ts_dump_cur += pkt
            self._ts_dump_maybe_rotate()
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        pusi = bool(pkt[1] & 0x40)
        adaptation_field_control = (pkt[3] >> 4) & 0x03
        pos = 4
        if adaptation_field_control in (0x02, 0x03):
            af_len = pkt[4]
            pos += 1 + af_len
        if adaptation_field_control in (0x01, 0x03) and pos < TS_PACKET_SIZE:
            payload = pkt[pos:]
        else:
            payload = b""
        if not payload:
            return

        if pid == 0x0000:
            self.handle_pat(payload, pusi)
        elif self.pmt_pid is not None and pid == self.pmt_pid:
            self.handle_pmt(payload, pusi)
        elif self.scte35_pid is not None and pid == self.scte35_pid:
            self.handle_scte35(payload, pusi)
        elif self.video_pid is not None and pid == self.video_pid:
            self.handle_video(payload, pusi)

    def close(self):
        if self.csv_file:
            self.csv_file.close()
        if self.json_out:
            self.json_out.close()
        if self.scte35_out:
            self.scte35_out.close()
        if self.snapshot_thread:
            self.snapshot_queue.put(None)
            self.snapshot_queue.join()
            self.snapshot_thread.join(timeout=15.0)
        if self.ts_dump_thread:
            # Flush whatever is left in the current (possibly partial)
            # window -- same qualification rule (event present, or
            # --ts-dump-all) as a normal rotation -- so Ctrl-C doesn't
            # silently drop an in-progress event window.
            self._rotate_ts_dump_window(time.monotonic())
            self.ts_dump_queue.put(None)
            self.ts_dump_queue.join()
            self.ts_dump_thread.join(timeout=60.0)


def extract_ts_packets(buf):
    """Split a byte buffer into full 188-byte TS packets, resyncing on 0x47
    if the buffer does not start aligned (e.g. after a dropped fragment or
    mid-stream join). Returns (list_of_packets, leftover_bytes)."""
    n = len(buf)
    i = 0
    packets = []
    while i + TS_PACKET_SIZE <= n:
        if buf[i] == SYNC_BYTE:
            packets.append(bytes(buf[i:i + TS_PACKET_SIZE]))
            i += TS_PACKET_SIZE
        else:
            i += 1
    return packets, buf[i:]


def main():
    ap = argparse.ArgumentParser(
        description="Measure SCTE-35 splice-point PTS vs. video IDR PTS "
                    "delta live from a multicast MPEG-TS stream.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--addr", required=True, help="Multicast group address, e.g. 239.1.1.1")
    ap.add_argument("--port", required=True, type=int, help="UDP port")
    ap.add_argument("--iface", default=None, help="Local interface IP to join the group on")
    ap.add_argument("--transport", choices=["auto", "ts", "rtp"], default="auto",
                    help="Payload framing: raw MPEG-TS-over-UDP, RTP-encapsulated, or auto-detect")
    ap.add_argument("--program", type=int, default=None,
                    help="Program number to follow if the mux carries several (default: first in PAT)")
    ap.add_argument("--pid-video", type=lambda x: int(x, 0), default=None,
                    help="Override auto-detected video PID (e.g. 0x101)")
    ap.add_argument("--pid-scte35", type=lambda x: int(x, 0), default=None,
                    help="Override auto-detected SCTE-35 PID (e.g. 0x1F0)")
    ap.add_argument("--codec", choices=["h264", "hevc"], default=None,
                    help="Force video codec instead of relying on PMT stream_type")
    ap.add_argument("--tolerance-ms", type=float, default=6000.0,
                    help="How far AFTER the target PTS an IDR may land and still be accepted as "
                         "the candidate match (ms)")
    ap.add_argument("--max-early-ms", type=float, default=50.0,
                    help="How far BEFORE the target PTS an IDR may land and still be accepted "
                         "(ms). Keep this small: a forced/reactive IDR cannot be caused by a cue "
                         "it hasn't been told about yet, so an IDR seconds before the target is "
                         "essentially always an unrelated periodic IDR (from intraPeriod or a "
                         "GOP boundary), not a reaction to this cue -- raising this value re-opens "
                         "the tool to greedily matching those and reporting nonsensical large "
                         "negative deltas")
    ap.add_argument("--timeout-s", type=float, default=12.0,
                    help="How long to wait for a matching IDR before declaring a splice point missed")
    ap.add_argument("--ok-threshold-ms", type=float, default=41.0,
                    help="Delta below which alignment is reported OK (default ~1 frame at 24fps)")
    ap.add_argument("--preroll-tolerance-ms", type=float, default=500.0,
                    help="How far the ACTUAL (wall-clock measured) pre-roll may fall short of the "
                         "DECLARED time-to-event (computed from the cue's own PTS at registration) "
                         "before being flagged PREROLL_SHORT (default 500 ms). Only actual pre-roll "
                         "shorter than declared is flagged -- extra pre-roll is never a problem. See "
                         "'time_to_event_ms'/'actual_preroll_ms'/'preroll_delta_ms' in the "
                         "csv-out/json-out/console output and the README section on this measurement "
                         "for what it does and does not tell you.")
    ap.add_argument("--min-time-to-event-ms", type=float, default=4000.0,
                    help="Minimum DECLARED time-to-event (time_to_event_ms) a splice event's first "
                         "transmission must have to satisfy SCTE-35's own minimum advance-notice "
                         "requirement (per secondary sources citing ANSI/SCTE 35 (2019) 9.2/10.3.3: "
                         "'sent at least once a minimum of 4 seconds in advance of the desired splice "
                         "time' -- default 4000 ms; not independently verified against the primary "
                         "standard text, see the README). Below this, the FIRST occurrence of a given "
                         "splice_event_id is flagged signal_verdict=SIGNAL_LATE. Later retransmissions "
                         "of the same event_id are reported as RETRANSMISSION, not re-flagged, since "
                         "the requirement is about the initial signal, not every repeat as the splice "
                         "point approaches.")
    ap.add_argument("--include-cra", action="store_true",
                    help="Also match HEVC CRA pictures as candidate splice points (flagged for review)")
    ap.add_argument("--csv-out", default=None, help="Append match/miss outcomes to this CSV file")
    ap.add_argument("--json-out", default=None, help="Append match/miss outcomes as JSON-lines to this file")
    ap.add_argument("--scte35-out", default=None,
                    help="Append EVERY decoded SCTE-35 message (all fields, all descriptors, "
                         "including splice_null/canceled/immediate ones that never get matched "
                         "against an IDR) as JSON-lines to this file")
    ap.add_argument("--scte35-log-file", default=None,
                    help="Also write SCTE-35 log lines (registration + descriptor summaries) to "
                         "this separate human-readable text log file, in addition to the console "
                         "(when set, SCTE-35 lines stop duplicating onto the console -- everything "
                         "else, e.g. PAT/PMT/match/miss lines, still goes to the console as before)")
    ap.add_argument("--snapshot-dir", default=None,
                    help="Save a JPEG of the matched IDR access unit for every splice event to "
                         "this directory (requires ffmpeg on PATH; decodes just that one access "
                         "unit standalone, using cached SPS/PPS if the stream doesn't repeat them "
                         "before every IDR). By default only IDRs that occur while a SCTE-35 "
                         "splice point is pending get snapshotted, to keep volume proportional to "
                         "actual ad-break activity rather than the whole stream's GOP cadence -- "
                         "see --snapshot-all-idr to change that. Filename encodes the IDR's own "
                         "PTS; the match/miss CSV/JSON/console output references it as "
                         "'snapshot_path' so you can open the exact frame in question.")
    ap.add_argument("--snapshot-all-idr", action="store_true",
                    help="With --snapshot-dir, save EVERY IDR (and CRA, if --include-cra) in the "
                         "stream, not just ones near a pending splice point. High volume on a "
                         "normal GOP cadence -- one file roughly every few seconds, 24/7.")
    ap.add_argument("--pre-frames", type=int, default=0,
                    help="With --snapshot-dir, also save this many access units immediately "
                         "BEFORE the matched IDR, in display/PTS order, as additional JPEGs "
                         "(e.g. --pre-frames 3 saves 3 pre-splice frames plus the IDR itself, 4 "
                         "files total). Since P/B frames aren't independently decodable this "
                         "decodes the whole GOP chunk back to the previous IDR/CRA once per "
                         "snapshot -- fewer frames than requested are saved if the stream start "
                         "or buffer limit is reached first. Requires --snapshot-dir.")
    ap.add_argument("--au-buffer-size", type=int, default=None,
                    help="With --pre-frames, how many access units to keep buffered in transport "
                         "order (default: max(300, pre_frames * 15)). Raise this if your GOP size "
                         "is large enough that --pre-frames frames aren't reliably found within "
                         "the previous GOP boundary.")
    ap.add_argument("--ts-dump-dir", default=None,
                    help="Continuously chop the raw multicast feed (every PID) into fixed-length "
                         "windows and save a window's raw .ts bytes to this directory whenever at "
                         "least one SCTE-35 message was decoded during it (see --ts-dump-all to "
                         "save every window instead). Each saved dump gets a JSON sidecar with the "
                         "window's timing and the SCTE-35 cue_seq values seen -- joinable with "
                         "--scte35-out. No ffmpeg dependency -- this is a raw byte-exact copy, "
                         "playable directly in VLC/ffplay/TSDuck.")
    ap.add_argument("--ts-dump-window", type=parse_duration_seconds, default=60.0,
                    help="Length of each TS dump window, e.g. '30s', '90s', '2m', '1.5m' (bare "
                         "numbers are seconds). Default 60s. Mind the memory footprint: with "
                         "--ts-dump-no-preroll NOT set (the default), up to 2x this window's worth "
                         "of raw TS stays resident in RAM at all times (roughly "
                         "bitrate_bps * window_s * 2 / 8 bytes) -- e.g. a 20 Mbit/s feed with a "
                         "60s window keeps ~300 MB buffered continuously.")
    ap.add_argument("--ts-dump-all", action="store_true",
                    help="With --ts-dump-dir, save EVERY window, not just ones containing a "
                         "decoded SCTE-35 message. Turns this into a plain rolling raw-TS recorder "
                         "-- high disk usage, combine with --ts-dump-max-files.")
    ap.add_argument("--ts-dump-no-preroll", action="store_true",
                    help="With --ts-dump-dir, do NOT prepend the previous window to a saved dump. "
                         "Default is to prepend it, so an event near the start of a window still "
                         "has real context before it instead of an abrupt cut; disabling this "
                         "halves the resident memory footprint and the saved file size.")
    ap.add_argument("--ts-dump-max-files", type=int, default=None,
                    help="With --ts-dump-dir, delete the oldest saved dumps once this many exist, "
                         "so a busy SCTE-35 PID can't silently fill the disk. Default: unlimited -- "
                         "strongly recommended to set this for unattended/production runs.")
    ap.add_argument("--duration", type=float, default=None, help="Stop after N seconds (default: run forever)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    log.info("scte35_idr_diff.py version %s starting", __version__)

    if Cue is None:
        log.error("threefive3 not found. Install with: "
                  "pip install threefive3 --break-system-packages")
        sys.exit(1)

    if args.snapshot_dir and shutil.which("ffmpeg") is None:
        log.error("--snapshot-dir requires ffmpeg on PATH to decode IDR access units to JPEG. "
                  "Install with: sudo apt install ffmpeg")
        sys.exit(1)

    if args.pre_frames and not args.snapshot_dir:
        log.error("--pre-frames requires --snapshot-dir (it saves additional JPEGs alongside "
                  "the IDR snapshot).")
        sys.exit(1)

    if args.ts_dump_dir and args.ts_dump_window <= 0:
        log.error("--ts-dump-window must be > 0 (got %s)", args.ts_dump_window)
        sys.exit(1)

    probe = Probe(args)
    sock = open_multicast_socket(args.addr, args.port, args.iface)
    log.info("Joined multicast %s:%d (iface=%s, transport=%s)",
             args.addr, args.port, args.iface or "any", args.transport)

    stop = {"flag": False}

    def _sigint(_sig, _frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sigint)

    start = time.monotonic()
    leftover = bytearray()
    transport_mode = args.transport
    packets_seen = 0

    try:
        while not stop["flag"]:
            if args.duration and (time.monotonic() - start) > args.duration:
                break
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                continue

            if transport_mode == "auto":
                transport_mode = "rtp" if looks_like_rtp(data) else "ts"
                log.info("Auto-detected transport: %s", transport_mode)
            if transport_mode == "rtp":
                data = strip_rtp(data)

            leftover += data
            packets, leftover = extract_ts_packets(leftover)
            leftover = bytearray(leftover)
            for pkt in packets:
                packets_seen += 1
                probe.handle_ts_packet(pkt)
    finally:
        probe.close()
        sock.close()
        log.info("Stopped. %d TS packets processed.", packets_seen)


if __name__ == "__main__":
    main()

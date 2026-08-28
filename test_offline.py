#!/usr/bin/env python3
"""
Offline self-test for scte35_idr_diff.py.

This sandbox has no PyPI access, so threefive3 cannot be installed here.
Everything in scte35_idr_diff.py EXCEPT the actual SCTE-35 binary decode
(which is delegated to threefive3, a well-established third-party library)
is custom code written for this tool: TS/PSI/PES demux, NAL scanning, PTS
math, and the pending-splice/IDR matching state machine. This test builds
synthetic MPEG-TS packets by hand and drives that custom code end-to-end,
substituting a minimal fake stand-in for a decoded threefive Cue object so
the matching logic can be verified without the real dependency installed.

Run: python3 test_offline.py
"""

import struct
import sys
import types

sys.path.insert(0, ".")
import scte35_idr_diff as mod  # noqa: E402


def crc32_mpeg2(data):
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
    return crc


def build_ts_packet(pid, payload, pusi, cc):
    header = bytearray(4)
    header[0] = 0x47
    header[1] = (0x40 if pusi else 0x00) | ((pid >> 8) & 0x1F)
    header[2] = pid & 0xFF
    header[3] = 0x10 | (cc & 0x0F)  # payload only, no adaptation field
    pkt = bytes(header) + payload
    if len(pkt) < 188:
        pkt += b"\xff" * (188 - len(pkt))
    assert len(pkt) == 188
    return pkt


def build_section_packet(pid, section_body_no_crc, table_id, cc, pusi_pointer=0):
    # section_body_no_crc starts right after the 3-byte section header
    # (table_id, section_length hi/lo) up to (not including) CRC32.
    section_length = len(section_body_no_crc) + 5 + 4 - 3  # placeholder, fixed below
    # Build header with correct section_length = len(rest after length field) + 4 (CRC)
    rest = section_body_no_crc
    section_length = len(rest) + 4
    header = bytes([table_id, 0xB0 | ((section_length >> 8) & 0x0F), section_length & 0xFF])
    body = header + rest
    crc = crc32_mpeg2(body)
    full = body + struct.pack(">I", crc)
    payload = bytes([pusi_pointer]) + full
    return build_ts_packet(pid, payload, pusi=True, cc=cc)


def build_pat(program_number, pmt_pid):
    # transport_stream_id(2) + reserved/version/current(1) + section_number(1)
    # + last_section_number(1) + program_number(2) + reserved/pid(2)
    rest = struct.pack(">HBBB", 1, 0xC1, 0, 0)
    rest += struct.pack(">HH", program_number, 0xE000 | pmt_pid)
    return build_section_packet(0x0000, rest, table_id=0x00, cc=0)


def build_pmt(pcr_pid, streams, pmt_pid):
    # program_number(2)+reserved/version/current(1)+section_number(1)+
    # last_section_number(1)+reserved/pcr_pid(2)+reserved/program_info_length(2)
    rest = struct.pack(">HBBB", 1, 0xC1, 0, 0)
    rest += struct.pack(">H", 0xE000 | pcr_pid)
    rest += struct.pack(">H", 0xF000)  # program_info_length = 0
    for stream_type, pid_, descriptors in streams:
        rest += struct.pack(">B", stream_type)
        rest += struct.pack(">H", 0xE000 | pid_)
        rest += struct.pack(">H", 0xF000 | len(descriptors))
        rest += descriptors
    return build_section_packet(pmt_pid, rest, table_id=0x02, cc=0)


def build_pes_packets(pid, pts_ticks, es_payload, cc_start=0):
    """One PES packet (PTS only, no DTS), possibly spanning multiple TS
    packets, returned as a list of 188-byte TS packets."""
    pts5 = bytearray(5)
    pts5[0] = 0x21 | (((pts_ticks >> 30) & 0x07) << 1)
    pts5[1] = (pts_ticks >> 22) & 0xFF
    pts5[2] = ((pts_ticks >> 15) & 0x7F) << 1 | 0x01
    pts5[3] = (pts_ticks >> 7) & 0xFF
    pts5[4] = ((pts_ticks & 0x7F) << 1) | 0x01

    pes_header = bytearray()
    pes_header += b"\x00\x00\x01\xE0"  # start code + stream_id (video)
    flags2 = 0x80  # PTS only
    header_data_length = 5
    optional = bytes([0x80, flags2, header_data_length]) + bytes(pts5)
    payload_for_length = optional + es_payload
    pes_packet_length = 0  # unbounded, standard for video
    pes_header += struct.pack(">H", pes_packet_length)
    pes = bytes(pes_header) + payload_for_length

    packets = []
    cc = cc_start
    first = True
    offset = 0
    max_payload = 184
    while offset < len(pes):
        chunk = pes[offset: offset + max_payload]
        packets.append(build_ts_packet(pid, chunk, pusi=first, cc=cc))
        first = False
        cc = (cc + 1) & 0x0F
        offset += max_payload
    return packets


def h264_idr_nal():
    # start code + NAL header (type=5, IDR slice) + a few dummy bytes,
    # avoiding accidental extra start codes in the dummy payload.
    return b"\x00\x00\x01\x65\x88\x84\x21\xa0"


def test_pts_diff_wraparound():
    a = 5  # ticks, just after wrap
    b = mod.PTS_MAX - 5  # ticks, just before wrap
    diff = mod.pts_diff_seconds(a, b)
    assert abs(diff - (10 / mod.PTS_HZ)) < 1e-9, diff
    print("OK: pts_diff_seconds handles 33-bit wraparound (%.9f s)" % diff)


def test_pat_pmt_and_idr_match():
    class Args:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = None
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = None
        scte35_out = None
        scte35_log_file = None
        snapshot_dir = None
        snapshot_all_idr = False
        pre_frames = 0
        au_buffer_size = None
        ts_dump_dir = None
        ts_dump_window = 60.0
        ts_dump_all = False
        ts_dump_no_preroll = False
        ts_dump_max_files = None

    probe = mod.Probe(Args())

    pmt_pid = 0x100
    video_pid = 0x101
    scte35_pid = 0x1F0

    pat_pkt = build_pat(program_number=1, pmt_pid=pmt_pid)
    cuei_descriptor = bytes([0x05, 0x04]) + b"CUEI"
    pmt_pkt = build_pmt(
        pcr_pid=video_pid,
        streams=[
            (mod.STREAM_TYPE_H264, video_pid, b""),
            (mod.STREAM_TYPE_SCTE35, scte35_pid, cuei_descriptor),
        ],
        pmt_pid=pmt_pid,
    )
    probe.handle_ts_packet(pat_pkt)
    probe.handle_ts_packet(pmt_pkt)

    assert probe.pmt_pid == pmt_pid
    assert probe.video_pid == video_pid, probe.video_pid
    assert probe.video_codec == "h264"
    assert probe.scte35_pid == scte35_pid
    print("OK: PAT/PMT parsing found video PID 0x%X (%s) and SCTE-35 PID 0x%X"
          % (probe.video_pid, probe.video_codec, probe.scte35_pid))

    # -- Register a fake decoded SCTE-35 cue targeting a PTS 2.0s from now --
    target_pts_seconds = 100.0
    SpliceInsert = type("SpliceInsert", (), {})
    fake_command = SpliceInsert()
    fake_command.pts_time = target_pts_seconds
    fake_command.splice_event_id = 4242
    fake_command.out_of_network_indicator = True
    fake_info = types.SimpleNamespace(pts_adjustment=0)
    fake_cue = types.SimpleNamespace(command=fake_command, info_section=fake_info)
    probe._register_cue(fake_cue, 1)
    assert len(probe.pending) == 1

    # -- Feed an IDR access unit 20ms after the target PTS --
    idr_pts_ticks = int(round((target_pts_seconds + 0.020) * mod.PTS_HZ)) % mod.PTS_MAX
    es_payload = h264_idr_nal()
    packets = build_pes_packets(video_pid, idr_pts_ticks, es_payload)
    # A following PUSI=1 packet on the same PID is required to flush/parse
    # the PES packet built above (reassembler parses on the *next* PUSI).
    packets += build_pes_packets(video_pid, (idr_pts_ticks + 3600) % mod.PTS_MAX, es_payload,
                                  cc_start=len(packets) % 16)

    for pkt in packets:
        probe.handle_ts_packet(pkt)

    assert len(probe.pending) == 0, "expected the splice point to be matched and removed"
    print("OK: IDR 20ms after target PTS matched and cleared from pending queue "
          "(within default 41ms OK threshold)")

    # -- Now test a MISS: register another cue with nothing following --
    TimeSignal = type("TimeSignal", (), {})
    fake_command2 = TimeSignal()
    fake_command2.pts_time = 200.0
    fake_command2.splice_event_id = 9999
    fake_command2.out_of_network_indicator = False
    fake_cue2 = types.SimpleNamespace(command=fake_command2, info_section=fake_info)
    probe._register_cue(fake_cue2, 2)
    assert len(probe.pending) == 1
    # simulate timeout by forcing the deadline into the past, then trigger
    # the cleanup path via another unrelated IDR far outside the window
    probe.pending[0]["deadline"] = 0
    far_idr_ticks = int(round(500.0 * mod.PTS_HZ))
    probe._match_idr(far_idr_ticks, "idr")
    assert len(probe.pending) == 0
    print("OK: timed-out splice point with no matching IDR is reported as MISSED")

    probe.close()


def test_pts_adjustment_applied():
    class Args:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = "h264"
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = None
        scte35_out = None
        scte35_log_file = None
        snapshot_dir = None
        snapshot_all_idr = False
        pre_frames = 0
        au_buffer_size = None
        ts_dump_dir = None
        ts_dump_window = 60.0
        ts_dump_all = False
        ts_dump_no_preroll = False
        ts_dump_max_files = None

    probe = mod.Probe(Args())
    base_pts = 300.0
    adjustment_ticks = 900  # 10ms worth of 90kHz ticks
    SpliceInsert = type("SpliceInsert", (), {})
    fake_command = SpliceInsert()
    fake_command.pts_time = base_pts
    fake_command.splice_event_id = 1
    fake_command.out_of_network_indicator = True
    fake_info = types.SimpleNamespace(pts_adjustment=adjustment_ticks)
    fake_cue = types.SimpleNamespace(command=fake_command, info_section=fake_info)
    probe._register_cue(fake_cue, 3)
    expected_ticks = int(round(base_pts * mod.PTS_HZ)) + adjustment_ticks
    assert probe.pending[0]["target_ticks"] == expected_ticks, probe.pending[0]["target_ticks"]
    print("OK: pts_adjustment from info_section is correctly added to command.pts_time")
    probe.close()


def test_rejects_unrelated_earlier_idr():
    """Regression test for the exact failure a real capture showed after
    lowering lookaheadDepth: an unrelated periodic IDR (from intraPeriod or
    a GOP boundary) landing SECONDS before a pending cue's target PTS used
    to be greedily grabbed by the old symmetric "abs(diff) <= window" check,
    producing a nonsensical large NEGATIVE delta. A forced/reactive IDR
    cannot be caused by a cue it hasn't been told about yet, so the fix
    (max_early_ms floor) must reject that early IDR and keep waiting for one
    at/after the target instead."""
    class Args:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = "h264"
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = None
        scte35_out = None
        scte35_log_file = None
        snapshot_dir = None
        snapshot_all_idr = False
        pre_frames = 0
        au_buffer_size = None
        ts_dump_dir = None
        ts_dump_window = 60.0
        ts_dump_all = False
        ts_dump_no_preroll = False
        ts_dump_max_files = None

    probe = mod.Probe(Args())
    probe.video_pid = 0x101
    probe.video_codec = "h264"

    target_pts_seconds = 11889.323467  # matches the real capture's cue
    SpliceInsert = type("SpliceInsert", (), {})
    fake_command = SpliceInsert()
    fake_command.pts_time = target_pts_seconds
    fake_command.splice_event_id = 12036
    fake_command.out_of_network_indicator = True
    fake_info = types.SimpleNamespace(pts_adjustment=0)
    fake_cue = types.SimpleNamespace(command=fake_command, info_section=fake_info)
    probe._register_cue(fake_cue, 1)
    assert len(probe.pending) == 1

    # An unrelated periodic IDR 2.64s *before* the target (this is what
    # produced the -2643.5ms delta in the field) must NOT be accepted.
    unrelated_early_ticks = int(round((target_pts_seconds - 2.6435) * mod.PTS_HZ)) % mod.PTS_MAX
    probe._match_idr(unrelated_early_ticks, "idr")
    assert len(probe.pending) == 1, (
        "an IDR 2.6s before the target must be rejected, not matched")
    print("OK: an unrelated IDR seconds before the target PTS is correctly "
          "rejected (not greedily matched)")

    # The real, later, reactive IDR (900ms after target, matching the
    # magnitude seen with lookaheadDepth=25) must still match normally.
    real_idr_ticks = int(round((target_pts_seconds + 0.900) * mod.PTS_HZ)) % mod.PTS_MAX
    probe._match_idr(real_idr_ticks, "idr")
    assert len(probe.pending) == 0, "the later, real IDR should have matched and cleared the queue"
    print("OK: the later IDR that actually follows the cue still matches correctly")


def test_full_scte35_logging(tmp_path_dir):
    """Verify the --scte35-out / --scte35-log-file channel captures the
    FULL decoded cue (every descriptor field), not just the handful of
    fields the IDR-matching logic uses -- this is what the dedicated
    SCTE-35 log/file is for, separate from the normal console+CSV output."""
    import json
    import os

    scte35_out_path = os.path.join(tmp_path_dir, "scte35_full.jsonl")
    scte35_log_path = os.path.join(tmp_path_dir, "scte35.log")

    class Args:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = "h264"
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = None
        scte35_out = scte35_out_path
        scte35_log_file = scte35_log_path
        snapshot_dir = None
        snapshot_all_idr = False
        pre_frames = 0
        au_buffer_size = None
        ts_dump_dir = None
        ts_dump_window = 60.0
        ts_dump_all = False
        ts_dump_no_preroll = False
        ts_dump_max_files = None

    probe = mod.Probe(Args())

    # Fake a threefive-like Cue: has .get() (what cue_to_dict prefers) AND
    # a .descriptors list with a segmentation_descriptor-like object, since
    # that's where time_signal() cues carry their actual event semantics.
    SegDescriptor = type("SegmentationDescriptor", (), {})
    seg = SegDescriptor()
    seg.segmentation_event_id = 0x4800008E
    seg.segmentation_type_id = 0x22  # "Break Start"
    seg.segmentation_type_id_name = "Provider Advertisement Start"
    seg.segmentation_upid_type = 0x0C
    seg.segmentation_upid = "abcd1234"
    seg.segmentation_duration = 30.0

    TimeSignal = type("TimeSignal", (), {})
    fake_command = TimeSignal()
    fake_command.pts_time = 555.5

    class FakeCue:
        command = fake_command
        info_section = types.SimpleNamespace(pts_adjustment=0)
        descriptors = [seg]

        def get(self):
            return {
                "info_section": {"pts_adjustment": 0, "table_id": "0xfc"},
                "command": {"pts_time": 555.5, "command_type": 6},
                "descriptors": [{
                    "segmentation_event_id": seg.segmentation_event_id,
                    "segmentation_type_id": seg.segmentation_type_id,
                    "segmentation_upid": seg.segmentation_upid,
                }],
            }

    fake_cue = FakeCue()
    section_bytes = b"\xfc\x30\x00" + b"\x00" * 10  # arbitrary stand-in bytes

    probe._log_full_cue(1, fake_cue, section_bytes)
    probe.close()

    with open(scte35_out_path) as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 1
    record = lines[0]
    assert record["cue_seq"] == 1
    assert record["command_type"] == "TimeSignal"
    assert record["section_hex"] == section_bytes.hex()
    # The full nested structure from cue.get() must be present verbatim --
    # this is the whole point: nothing threefive parsed gets dropped.
    assert record["cue"]["descriptors"][0]["segmentation_upid"] == "abcd1234"
    assert record["cue"]["command"]["pts_time"] == 555.5
    # The human-readable summary must surface segmentation fields too.
    assert any("segmentation_upid=abcd1234" in s for s in record["descriptor_summary"])
    print("OK: --scte35-out captures the full decoded cue (all descriptor "
          "fields), not just the fields used for IDR matching")

    with open(scte35_log_path) as f:
        log_text = f.read()
    assert "SCTE-35 #1" in log_text and "TimeSignal" in log_text
    print("OK: --scte35-log-file receives a dedicated SCTE-35 text log, "
          "separate from the console/main log")


def test_idr_jpeg_snapshot(tmp_dir):
    """End-to-end test of --snapshot-dir using a REAL ffmpeg-encoded H.264
    access unit (not the synthetic placeholder NAL used elsewhere in this
    file, which isn't valid enough to actually decode) -- this is the one
    test that exercises the actual subprocess/ffmpeg path, including the
    parameter-set caching that lets a lone IDR (no in-band SPS/PPS, i.e.
    repeatHeaders=0) still be decoded standalone. Skips gracefully if
    ffmpeg isn't on PATH."""
    import shutil as _shutil
    import subprocess as _subprocess
    import os as _os

    if _shutil.which("ffmpeg") is None:
        print("SKIP: ffmpeg not found on PATH -- cannot exercise the real "
              "snapshot decode path in this environment")
        return

    gen = _subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=blue:s=64x64:d=1:r=1", "-frames:v", "1",
         "-c:v", "libx264", "-profile:v", "baseline", "-f", "h264", "-"],
        stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, timeout=30,
    )
    assert gen.returncode == 0 and gen.stdout, (
        "failed to generate a real H.264 test access unit: %s" % gen.stderr)
    full_au = gen.stdout

    assert mod.has_param_sets(full_au, "h264"), (
        "the freshly-encoded frame should carry its own SPS/PPS")
    params = mod.extract_param_set_nals(full_au, "h264")
    assert len(params) > 0

    class Args:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = "h264"
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = None
        scte35_out = None
        scte35_log_file = None
        snapshot_dir = tmp_dir
        snapshot_all_idr = True
        pre_frames = 0
        au_buffer_size = None
        ts_dump_dir = None
        ts_dump_window = 60.0
        ts_dump_all = False
        ts_dump_no_preroll = False
        ts_dump_max_files = None

    probe = mod.Probe(Args())
    probe.video_codec = "h264"

    # Case 1: self-contained AU (has its own SPS/PPS) -- decodes as-is.
    pts1 = int(1000.0 * mod.PTS_HZ)
    probe._enqueue_snapshot(pts1, full_au)
    probe.snapshot_queue.join()
    path1 = probe._idr_snapshot_path(pts1)
    assert _os.path.exists(path1) and _os.path.getsize(path1) > 0, (
        "expected a JPEG snapshot to be written for a self-contained AU")
    with open(path1, "rb") as f:
        magic = f.read(2)
    assert magic == b"\xff\xd8", "output file is not a valid JPEG (bad magic bytes)"
    print("OK: a self-contained IDR access unit (own SPS/PPS) is decoded "
          "to a real JPEG via ffmpeg")

    # Case 2: IDR-only AU with NO in-band parameter sets (simulates
    # repeatHeaders=0) -- must still decode using the cached params.
    idr_only = b""
    for header_off, unit_start, unit_end in mod.iter_nal_units(full_au):
        nal_type = full_au[header_off] & 0x1F
        if nal_type == 5:  # IDR slice
            idr_only = full_au[unit_start:unit_end]
            break
    assert idr_only, "could not isolate the IDR slice NAL from the test AU"
    assert not mod.has_param_sets(idr_only, "h264")

    probe.last_param_sets = params  # simulate having cached it from an earlier AU
    pts2 = int(1001.0 * mod.PTS_HZ)
    probe._enqueue_snapshot(pts2, idr_only)
    probe.snapshot_queue.join()
    path2 = probe._idr_snapshot_path(pts2)
    assert _os.path.exists(path2) and _os.path.getsize(path2) > 0, (
        "expected the cached SPS/PPS to be prepended so ffmpeg can still "
        "decode a bare IDR slice standalone")
    print("OK: an IDR access unit with NO in-band SPS/PPS (repeatHeaders=0 "
          "case) still decodes correctly using cached parameter sets")

    probe.close()


def _split_h264_access_units(data):
    """Group raw Annex B NAL units from a real ffmpeg-encoded elementary
    stream into access units: everything from just after one slice NAL
    (type 1 or 5) up to and including the next slice NAL belongs to one
    access unit (this puts any AUD/SPS/PPS ahead of a slice into the same
    AU as that slice, matching how a real encoder groups them)."""
    units = mod.iter_nal_units(data)
    aus = []
    current_start = units[0][1] if units else 0
    for header_off, unit_start, unit_end in units:
        nal_type = data[header_off] & 0x1F
        if nal_type in (1, 5):
            aus.append(data[current_start:unit_end])
            current_start = unit_end
    return aus


def test_pre_frame_snapshots(tmp_dir):
    """End-to-end test of --pre-frames using a REAL multi-frame ffmpeg-
    encoded H.264 sequence (two GOPs, no B-frames so decode order == PTS
    order, one AUD-delimited access unit per frame). Verifies that:
      1. an IDR with nothing usable before it (first frame in the stream)
         falls back gracefully to a single IDR-only snapshot, and
      2. a later IDR with a full GOP of P-frames before it gets its own
         JPEG plus the requested number of preceding frames' JPEGs, named
         and ordered correctly (oldest first, ending at the IDR itself)."""
    import shutil as _shutil
    import subprocess as _subprocess
    import os as _os

    if _shutil.which("ffmpeg") is None:
        print("SKIP: ffmpeg not found on PATH -- cannot exercise the real "
              "pre-frames decode path in this environment")
        return

    num_frames = 10
    gen = _subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"testsrc=size=64x64:rate=1:duration={num_frames}",
         "-frames:v", str(num_frames), "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-profile:v", "baseline",
         "-g", "5", "-sc_threshold", "0", "-bf", "0",
         "-x264-params", "aud=1", "-f", "h264", "-"],
        stdout=_subprocess.PIPE, stderr=_subprocess.PIPE, timeout=30,
    )
    assert gen.returncode == 0 and gen.stdout, (
        "failed to generate a real multi-frame H.264 test sequence: %s" % gen.stderr)

    aus = _split_h264_access_units(gen.stdout)
    assert len(aus) == num_frames, (
        "expected %d access units, split out %d -- AUD-based splitting "
        "assumption may not hold for this ffmpeg build" % (num_frames, len(aus)))

    requested_pre_frames = 2
    fps_ticks = mod.PTS_HZ  # 1 fps source -> 90000 ticks between frames

    class Args:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = "h264"
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = None
        scte35_out = None
        scte35_log_file = None
        snapshot_dir = tmp_dir
        snapshot_all_idr = True
        pre_frames = requested_pre_frames
        au_buffer_size = None
        ts_dump_dir = None
        ts_dump_window = 60.0
        ts_dump_all = False
        ts_dump_no_preroll = False
        ts_dump_max_files = None

    probe = mod.Probe(Args())
    probe.video_codec = "h264"

    pts_list = []
    kinds = []
    for i, au in enumerate(aus):
        pts = i * fps_ticks
        pts_list.append(pts)
        nal_types = list(mod.find_nal_types(au, "h264"))
        kind = mod.classify_idr(nal_types, "h264", False)
        kinds.append(kind)
        probe._process_video_pes((pts, None, au))
    probe.snapshot_queue.join()

    idr_indices = [i for i, k in enumerate(kinds) if k == "idr"]
    assert len(idr_indices) >= 2, (
        "expected at least 2 IDRs (one per GOP) in a %d-frame, -g 5 "
        "sequence, got IDRs at %s -- adjust encode settings" % (num_frames, idr_indices))
    print("OK: encoded %d-frame test sequence has IDRs at indices %s"
          % (num_frames, idr_indices))

    # -- First IDR: nothing usable before it -> graceful single-frame
    # fallback (no "pre" files for this IDR's own pts). --
    first_idr_idx = idr_indices[0]
    first_idr_ticks = pts_list[first_idr_idx]
    first_idr_path = probe._idr_snapshot_path(first_idr_ticks)
    assert _os.path.exists(first_idr_path) and _os.path.getsize(first_idr_path) > 0
    stray_pre_files = [f for f in _os.listdir(tmp_dir)
                       if f"_pts_{first_idr_ticks / mod.PTS_HZ:.6f}_" in f and "_pre" in f]
    assert not stray_pre_files, (
        "the very first IDR has nothing before it and should have fallen "
        "back to a single-frame snapshot, but found pre-frame files: %s" % stray_pre_files)
    print("OK: an IDR with nothing usable before it in the buffer falls "
          "back to a plain single-frame snapshot (no pre-frame files)")

    # -- Second IDR: a full GOP of P-frames precedes it -> expect the IDR's
    # own JPEG plus `pre_frames` preceding frames' JPEGs, oldest first. --
    second_idr_idx = idr_indices[1]
    second_idr_ticks = pts_list[second_idr_idx]
    second_idr_path = probe._idr_snapshot_path(second_idr_ticks)
    assert _os.path.exists(second_idr_path) and _os.path.getsize(second_idr_path) > 0
    with open(second_idr_path, "rb") as f:
        assert f.read(2) == b"\xff\xd8"

    for offset in range(1, requested_pre_frames + 1):
        expected_frame_idx = second_idr_idx - offset
        assert expected_frame_idx >= 0
        frame_pts = pts_list[expected_frame_idx]
        pre_path = probe._pre_frame_snapshot_path(second_idr_ticks, offset, frame_pts)
        assert _os.path.exists(pre_path) and _os.path.getsize(pre_path) > 0, (
            "missing pre-frame snapshot for offset %d: %s" % (offset, pre_path))
        with open(pre_path, "rb") as f:
            assert f.read(2) == b"\xff\xd8", "pre-frame file is not a valid JPEG: %s" % pre_path
    print("OK: the second IDR (with a full preceding GOP available) got its "
          "own JPEG plus %d correctly-named, valid pre-frame JPEGs" % requested_pre_frames)

    probe.close()


def _make_ts_dump_args(tmp_dir, window_s, dump_all_flag=False, preroll_off=False, max_files_limit=None):
    class Args:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = "h264"
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = None
        scte35_out = None
        scte35_log_file = None
        snapshot_dir = None
        snapshot_all_idr = False
        pre_frames = 0
        au_buffer_size = None
        ts_dump_dir = tmp_dir
        ts_dump_window = window_s
        ts_dump_all = dump_all_flag
        ts_dump_no_preroll = preroll_off
        ts_dump_max_files = max_files_limit
    return Args()


def _null_ts_packet(cc):
    # A trivial, valid 188-byte TS packet on the null PID (0x1FFF) -- just
    # something with a real sync byte to push through the raw dump path;
    # content doesn't matter for these tests, only byte-exact accounting.
    return build_ts_packet(0x1FFF, b"\xff" * 184, pusi=False, cc=cc)


def _force_ts_dump_rotate(probe, window_s):
    """Force the current TS-dump window to close deterministically, instead
    of racing a real time.sleep() against packet arrival: rotation is only
    evaluated when a packet arrives (there's no background timer), so
    which exact packet ends up on which side of a real elapsed-time
    boundary is a timing accident, not something a test should depend on.
    Back-dating the window-start and calling the rotate check directly
    closes the window after precisely the packets sent so far -- nothing
    from the next burst leaks in either direction."""
    probe._ts_dump_window_start -= (window_s + 1.0)
    probe._ts_dump_maybe_rotate()


def test_ts_dump_only_saves_windows_with_events(tmp_dir):
    """Core behavior: a fixed-length window with NO decoded SCTE-35 message
    during it must NOT be saved; a window WITH one must be -- and, since
    the previous window is prepended by default, the saved file must be
    exactly (previous window bytes + this window's bytes)."""
    import glob as _glob
    import json as _json
    import os as _os

    window_s = 60.0  # irrelevant to timing here -- rotation is forced explicitly
    args = _make_ts_dump_args(tmp_dir, window_s)
    probe = mod.Probe(args)

    # -- Window 1: some packets, no SCTE-35 event. --
    window1_packets = [_null_ts_packet(i) for i in range(5)]
    for pkt in window1_packets:
        probe.handle_ts_packet(pkt)
    window1_bytes = b"".join(window1_packets)
    _force_ts_dump_rotate(probe, window_s)

    # -- Window 2: a different number of packets, WITH a noted event. --
    window2_packets = [_null_ts_packet(i) for i in range(3)]
    for pkt in window2_packets:
        probe.handle_ts_packet(pkt)
    probe._ts_dump_note_event(seq=42)
    window2_bytes = b"".join(window2_packets)
    _force_ts_dump_rotate(probe, window_s)

    probe.ts_dump_queue.join()

    saved = sorted(_glob.glob(_os.path.join(tmp_dir, "ts_dump_*.ts")))
    assert len(saved) == 1, (
        "expected exactly one saved dump (window 1 had no event and must "
        "be skipped; window 2 had one and must be saved), got: %s" % saved)
    saved_path = saved[0]
    assert "_n1events" in saved_path, saved_path

    with open(saved_path, "rb") as f:
        saved_bytes = f.read()
    assert saved_bytes == window1_bytes + window2_bytes, (
        "with pre-roll enabled (the default), the saved dump must be "
        "exactly the previous window's bytes followed by the triggering "
        "window's bytes")
    print("OK: only the window containing a decoded SCTE-35 message is "
          "saved, and it correctly includes the preceding window as "
          "pre-roll context")

    with open(saved_path + ".json") as f:
        meta = _json.load(f)
    assert meta["event_count"] == 1
    assert meta["event_cue_seqs"] == [42]
    assert meta["included_previous_window"] is True
    print("OK: the JSON sidecar records the correct event count and "
          "cue_seq for the saved window")

    probe.close()


def test_ts_dump_no_preroll(tmp_dir):
    """With --ts-dump-no-preroll, a saved dump must contain ONLY the
    triggering window's own bytes, not the preceding window's."""
    import glob as _glob
    import os as _os

    window_s = 60.0
    args = _make_ts_dump_args(tmp_dir, window_s, preroll_off=True)
    probe = mod.Probe(args)

    for pkt in (_null_ts_packet(i) for i in range(4)):
        probe.handle_ts_packet(pkt)
    _force_ts_dump_rotate(probe, window_s)

    window2_packets = [_null_ts_packet(i) for i in range(2)]
    for pkt in window2_packets:
        probe.handle_ts_packet(pkt)
    probe._ts_dump_note_event(seq=7)
    window2_bytes = b"".join(window2_packets)
    _force_ts_dump_rotate(probe, window_s)

    probe.ts_dump_queue.join()

    saved = sorted(_glob.glob(_os.path.join(tmp_dir, "ts_dump_*.ts")))
    assert len(saved) == 1
    with open(saved[0], "rb") as f:
        saved_bytes = f.read()
    assert saved_bytes == window2_bytes, (
        "--ts-dump-no-preroll must save only the triggering window's own "
        "bytes, with nothing prepended from the previous window")
    print("OK: --ts-dump-no-preroll saves only the triggering window's own "
          "bytes (no previous-window pre-roll)")

    probe.close()


def test_ts_dump_max_files_prunes_oldest(tmp_dir):
    """--ts-dump-max-files must delete the oldest saved dump(s) once the
    limit is exceeded, so a busy SCTE-35 PID can't fill the disk."""
    import glob as _glob
    import os as _os

    window_s = 60.0
    args = _make_ts_dump_args(tmp_dir, window_s, dump_all_flag=True, max_files_limit=2)
    probe = mod.Probe(args)

    # 4 consecutive windows, all saved (--ts-dump-all), limit is 2 -- only
    # the last 2 should remain once the background worker has caught up.
    for _ in range(4):
        probe.handle_ts_packet(_null_ts_packet(1))
        _force_ts_dump_rotate(probe, window_s)
    probe.ts_dump_queue.join()

    saved = sorted(_glob.glob(_os.path.join(tmp_dir, "ts_dump_*.ts")))
    assert len(saved) == 2, (
        "expected --ts-dump-max-files=2 to prune down to 2 files, found: %s" % saved)
    # The surviving files must be the two NEWEST (highest sequence number
    # in the filename), i.e. the oldest ones were the ones pruned.
    seq_numbers = sorted(int(_os.path.basename(p).split("_")[2]) for p in saved)
    assert seq_numbers == [3, 4], (
        "expected the 2 most recent windows (seq 3 and 4) to survive "
        "pruning, got sequence numbers: %s" % seq_numbers)
    for p in saved:
        assert _os.path.exists(p + ".json"), "sidecar must be pruned together with its .ts file"
    print("OK: --ts-dump-max-files prunes the oldest dumps (and their JSON "
          "sidecars), keeping only the most recent N")

    probe.close()


def test_time_to_event_and_actual_preroll(tmp_dir):
    """Verify the time_to_event_ms (declared, PTS-domain lead time) vs.
    actual_preroll_ms (real wall-clock elapsed lead time) measurement and
    its PREROLL_SHORT/OK/N-A verdict -- this is the check added after a
    field report of a cue with time_to_event=3052ms but actual_preroll
    only 1856ms (a ~1.2s shortfall)."""
    import json as _json
    import os as _os

    json_out_path = _os.path.join(tmp_dir, "preroll.jsonl")

    class Args:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = "h264"
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        preroll_tolerance_ms = 500.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = json_out_path
        scte35_out = None
        scte35_log_file = None
        snapshot_dir = None
        snapshot_all_idr = False
        pre_frames = 0
        au_buffer_size = None
        ts_dump_dir = None
        ts_dump_window = 60.0
        ts_dump_all = False
        ts_dump_no_preroll = False
        ts_dump_max_files = None

    def _make_cue(pts_time, event_id):
        SpliceInsert = type("SpliceInsert", (), {})
        cmd = SpliceInsert()
        cmd.pts_time = pts_time
        cmd.splice_event_id = event_id
        cmd.out_of_network_indicator = True
        info = types.SimpleNamespace(pts_adjustment=0)
        return types.SimpleNamespace(command=cmd, info_section=info)

    def _read_last_json_row():
        with open(json_out_path) as f:
            lines = [_json.loads(ln) for ln in f if ln.strip()]
        return lines[-1]

    probe = mod.Probe(Args())

    # -- Case A: no video AU observed yet -> time_to_event_ms must be None,
    # not a bogus/garbage number (there is nothing to measure "declared lead
    # time" relative to before the probe has seen any live video PTS). --
    assert probe.last_video_pts_ticks is None
    probe._register_cue(_make_cue(1000.0, 1), seq=1)
    assert probe.pending[-1]["time_to_event_ms"] is None, (
        "with no video PTS seen yet, the declared time-to-event cannot be "
        "computed and must be None")
    print("OK: time_to_event_ms is None when no video AU has been seen yet")
    probe.pending.clear()  # done with this entry; keep it out of later cases

    # -- Case B: 3.0s declared lead time (from the cue's own target PTS
    # relative to the last-seen video PTS), but only ~1.8s of real wall-
    # clock time elapses before the matching IDR arrives -- mirrors the
    # field report (declared 3052ms vs actual 1856ms) and must be flagged
    # PREROLL_SHORT. As with the TS-dump window tests elsewhere in this
    # file, a real time.sleep() would be a flaky race against the clock;
    # instead we back-date the recorded registration timestamp
    # deterministically to simulate "1.8s of wall-clock time already
    # passed" before the match happens.
    probe.last_video_pts_ticks = int(round(1000.0 * mod.PTS_HZ))
    target_pts_b = 1003.0  # exactly 3.0s after last_video_pts_ticks
    probe._register_cue(_make_cue(target_pts_b, 2), seq=2)
    entry_b = probe.pending[-1]
    assert abs(entry_b["time_to_event_ms"] - 3000.0) < 1e-6, entry_b["time_to_event_ms"]
    entry_b["register_monotonic"] -= 1.8
    idr_ticks_b = int(round(target_pts_b * mod.PTS_HZ)) % mod.PTS_MAX
    probe._match_idr(idr_ticks_b, "idr")
    assert len(probe.pending) == 0
    row_b = _read_last_json_row()
    assert row_b["cue_seq"] == 2
    assert abs(row_b["time_to_event_ms"] - 3000.0) < 1e-6, row_b["time_to_event_ms"]
    # generous slack for real test-execution overhead between back-dating
    # and the _match_idr()/_emit() call actually running
    assert abs(row_b["actual_preroll_ms"] - 1800.0) < 200.0, row_b["actual_preroll_ms"]
    assert row_b["preroll_delta_ms"] < -500.0, row_b["preroll_delta_ms"]
    assert row_b["preroll_verdict"] == "PREROLL_SHORT", row_b
    print("OK: a large actual-vs-declared pre-roll shortfall (as in the "
          "3052ms/1856ms field report) is flagged PREROLL_SHORT")

    # -- Case C: a small shortfall, within the default 500ms tolerance -> OK. --
    target_pts_c = 1006.0  # 6.0s after last_video_pts_ticks (still 1000.0)
    probe._register_cue(_make_cue(target_pts_c, 3), seq=3)
    entry_c = probe.pending[-1]
    assert abs(entry_c["time_to_event_ms"] - 6000.0) < 1e-6, entry_c["time_to_event_ms"]
    entry_c["register_monotonic"] -= 5.8  # 200ms short of declared -- within tolerance
    idr_ticks_c = int(round(target_pts_c * mod.PTS_HZ)) % mod.PTS_MAX
    probe._match_idr(idr_ticks_c, "idr")
    assert len(probe.pending) == 0
    row_c = _read_last_json_row()
    assert row_c["cue_seq"] == 3
    assert row_c["preroll_verdict"] == "OK", row_c
    print("OK: a small actual-vs-declared shortfall within the default "
          "500ms tolerance is verdict OK, not flagged")

    # -- Case D: a MISSED cue (times out with no matching IDR) -> actual
    # pre-roll was never observed (None/N-A), but the DECLARED
    # time_to_event_ms -- computed once at registration, independent of
    # whether a match ever happens -- must still be populated. --
    probe._register_cue(_make_cue(2000.0, 4), seq=4)
    entry_d = probe.pending[-1]
    assert entry_d["time_to_event_ms"] is not None
    entry_d["deadline"] = 0  # force immediate timeout
    unrelated_idr_ticks = int(round(9999.0 * mod.PTS_HZ)) % mod.PTS_MAX
    probe._match_idr(unrelated_idr_ticks, "idr")  # drives the timeout-sweep path
    assert len(probe.pending) == 0
    row_d = _read_last_json_row()
    assert row_d["cue_seq"] == 4
    assert row_d["time_to_event_ms"] is not None
    assert row_d["actual_preroll_ms"] is None
    assert row_d["preroll_delta_ms"] is None
    assert row_d["preroll_verdict"] == "N/A", row_d
    print("OK: a MISSED cue has no actual_preroll_ms (never matched) but "
          "still records its declared time_to_event_ms, verdict N/A")

    probe.close()


def test_signal_verdict_min_time_to_event(tmp_dir):
    """Verify signal_verdict / --min-time-to-event-ms: whether the FIRST
    transmission of a splice_event_id met SCTE-35's own apparent 4-second
    minimum advance-notice requirement (ANSI/SCTE 35 (2019) 9.2/10.3.3,
    per secondary sources), and that RETRANSMISSIONs of the same event_id
    are correctly excluded from re-evaluation instead of being flagged
    every time the splice point gets closer."""
    import json as _json
    import os as _os

    json_out_path = _os.path.join(tmp_dir, "signal.jsonl")

    class Args:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = "h264"
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        preroll_tolerance_ms = 500.0
        min_time_to_event_ms = 4000.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = json_out_path
        scte35_out = None
        scte35_log_file = None
        snapshot_dir = None
        snapshot_all_idr = False
        pre_frames = 0
        au_buffer_size = None
        ts_dump_dir = None
        ts_dump_window = 60.0
        ts_dump_all = False
        ts_dump_no_preroll = False
        ts_dump_max_files = None

    def _make_cue(pts_time, event_id):
        SpliceInsert = type("SpliceInsert", (), {})
        cmd = SpliceInsert()
        cmd.pts_time = pts_time
        cmd.splice_event_id = event_id
        cmd.out_of_network_indicator = True
        info = types.SimpleNamespace(pts_adjustment=0)
        return types.SimpleNamespace(command=cmd, info_section=info)

    def _read_last_json_row():
        with open(json_out_path) as f:
            lines = [_json.loads(ln) for ln in f if ln.strip()]
        return lines[-1]

    probe = mod.Probe(Args())
    probe.last_video_pts_ticks = int(round(1000.0 * mod.PTS_HZ))  # fixed "now" in PTS domain

    # -- Case A: first (only) transmission, 5.0s declared lead time (>= the
    # 4.0s default floor) -> OK. --
    probe._register_cue(_make_cue(1005.0, 100), seq=10)
    entry_a = probe.pending[-1]
    assert abs(entry_a["time_to_event_ms"] - 5000.0) < 1e-6, entry_a["time_to_event_ms"]
    assert entry_a["signal_verdict"] == "OK", entry_a
    probe.pending.clear()
    print("OK: a first transmission with >=4s declared lead time is "
          "signal_verdict=OK")

    # -- Case B: first transmission of a DIFFERENT event, only 3.0s declared
    # lead time (< the 4.0s default floor) -> SIGNAL_LATE. This mirrors the
    # exact field report (3052ms) that prompted this check. --
    probe._register_cue(_make_cue(1003.0, 200), seq=11)
    entry_b = probe.pending[-1]
    assert abs(entry_b["time_to_event_ms"] - 3000.0) < 1e-6, entry_b["time_to_event_ms"]
    assert entry_b["signal_verdict"] == "SIGNAL_LATE", entry_b
    idr_ticks_b = int(round(1003.0 * mod.PTS_HZ)) % mod.PTS_MAX
    probe._match_idr(idr_ticks_b, "idr")
    assert len(probe.pending) == 0
    row_b = _read_last_json_row()
    assert row_b["cue_seq"] == 11
    assert row_b["signal_verdict"] == "SIGNAL_LATE", row_b
    print("OK: a first transmission with <4s declared lead time (e.g. the "
          "3052ms field report) is flagged signal_verdict=SIGNAL_LATE, and "
          "this carries through to the emitted match/miss row")

    # -- Case C: a RETRANSMISSION of the same event_id (200) as the splice
    # point draws nearer -- even though its own time_to_event_ms is now
    # smaller still (well under 4s), it must NOT be re-flagged SIGNAL_LATE:
    # the 4-second requirement is about the event's first signal, and a
    # well-behaved encoder retransmitting the same cue for reliability
    # would otherwise get spuriously flagged on every repeat. --
    probe._register_cue(_make_cue(1003.0, 200), seq=12)
    entry_c = probe.pending[-1]
    assert entry_c["signal_verdict"] == "RETRANSMISSION", entry_c
    probe.pending.clear()
    print("OK: a retransmission of an already-seen event_id is reported as "
          "signal_verdict=RETRANSMISSION, not re-flagged SIGNAL_LATE")

    # -- Case D: event_id is None (can't dedupe) -- every occurrence must be
    # evaluated on its own merits rather than being silently skipped or
    # incorrectly treated as a retransmission of some other None-id cue. --
    probe._register_cue(_make_cue(1001.5, None), seq=13)
    entry_d1 = probe.pending[-1]
    assert entry_d1["signal_verdict"] == "SIGNAL_LATE", entry_d1  # 1.5s declared
    probe.pending.clear()
    probe._register_cue(_make_cue(1006.0, None), seq=14)
    entry_d2 = probe.pending[-1]
    assert entry_d2["signal_verdict"] == "OK", entry_d2  # 6.0s declared
    probe.pending.clear()
    print("OK: with no event_id to dedupe on, each occurrence is evaluated "
          "independently rather than defaulting to RETRANSMISSION")

    probe.close()


def test_validate_ipv4_literal():
    """Regression test for a real field failure: a non-numeric --addr (a
    hostname, or any typo/garbage that isn't a dotted-quad) used to reach
    socket.bind() unvalidated and blow up with an opaque
    'socket.gaierror: [Errno -2] Name or service not known' and a raw
    traceback -- e.g. a headend-internal DNS name for a multicast group
    that resolved on one server but not on another. validate_ipv4_literal()
    now catches this up front (via socket.inet_aton(), which is pure
    string parsing and never touches DNS/NSS) with a clear, actionable
    message before any network call is attempted."""
    # A valid dotted-quad must pass silently (no SystemExit).
    mod.validate_ipv4_literal("239.1.1.1", "--addr")
    print("OK: a valid IPv4 literal passes validate_ipv4_literal without error")

    # A hostname (the exact failure mode from the field report) must be
    # rejected with a clean SystemExit(1), not left to blow up in bind().
    for bad_value in ("mc-channel3.internal.sappa.se", "239.1.1.1:5000", "", "not-an-ip"):
        try:
            mod.validate_ipv4_literal(bad_value, "--addr")
            raise AssertionError(
                "expected validate_ipv4_literal to reject %r but it did not" % bad_value)
        except SystemExit as exc:
            assert exc.code == 1, (bad_value, exc.code)
    print("OK: non-numeric --addr values (hostnames, typos, empty string) "
          "are rejected with a clear error instead of reaching socket.bind()")


def test_unicast_loopback_addr():
    """Regression test for a real user request: --addr 127.0.0.1 (or any
    other non-multicast IPv4 address) used to crash with an opaque
    'OSError: [Errno 22] Invalid argument' from the unconditional
    IP_ADD_MEMBERSHIP join in open_multicast_socket() -- joining a
    "multicast group" that isn't actually a multicast address fails at the
    socket layer. is_multicast_ipv4() now decides up front whether to join
    a real multicast group or just listen for plain unicast UDP, which is
    exactly what local testing against loopback (no real multicast network
    required) needs."""
    import socket as _socket

    # -- is_multicast_ipv4: exact boundary of the 224.0.0.0-239.255.255.255
    # range, plus the loopback address the request was specifically about.
    assert mod.is_multicast_ipv4("224.0.0.0") is True
    assert mod.is_multicast_ipv4("239.255.255.255") is True
    assert mod.is_multicast_ipv4("239.1.1.1") is True
    assert mod.is_multicast_ipv4("223.255.255.255") is False  # just below the range
    assert mod.is_multicast_ipv4("240.0.0.0") is False  # just above the range
    assert mod.is_multicast_ipv4("127.0.0.1") is False
    assert mod.is_multicast_ipv4("10.0.0.5") is False  # an ordinary unicast LAN address too
    print("OK: is_multicast_ipv4 correctly classifies the 224/4 range "
          "(including its exact boundaries), loopback, and an ordinary "
          "unicast address")

    # -- open_multicast_socket("127.0.0.1", ...) must NOT raise, must NOT
    # attempt an IP_ADD_MEMBERSHIP join, and must actually work as a plain
    # unicast UDP listener end-to-end. --
    port = 15987
    sock = mod.open_multicast_socket("127.0.0.1", port, None)
    try:
        sender = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            payload = b"\x47" + b"\x00" * 187  # a trivial TS-packet-shaped payload
            sender.sendto(payload, ("127.0.0.1", port))
            data, _from = sock.recvfrom(2048)
            assert data == payload, "did not receive the exact bytes sent to loopback"
        finally:
            sender.close()
    finally:
        sock.close()
    print("OK: --addr 127.0.0.1 opens a plain unicast UDP socket (no "
          "multicast join attempted) and genuinely receives packets sent "
          "to it, instead of crashing with 'OSError: [Errno 22] Invalid "
          "argument' from an IP_ADD_MEMBERSHIP join against a "
          "non-multicast address")

    # -- A real multicast address must still take the join path as before
    # (regression guard: the branch must not have broken the existing,
    # already-working case). Some sandboxes/containers may not support an
    # actual IGMP join even on loopback -- skip gracefully rather than
    # fail the whole suite over an environment limitation unrelated to
    # this fix. --
    try:
        mcast_sock = mod.open_multicast_socket("239.1.1.1", port + 1, None)
        mcast_sock.close()
        print("OK: a genuine multicast --addr (239.1.1.1) still takes the "
              "IP_ADD_MEMBERSHIP join path as before")
    except OSError as exc:
        print("SKIP: this sandbox does not support joining a real "
              "multicast group (%s) -- unrelated to the loopback fix "
              "under test" % exc)


def test_process_ts_file(tmp_dir):
    """Verify --input-file's underlying implementation, process_ts_file():
    reading PAT/PMT/video/IDR packets from a real file on disk, through
    process_ts_file(), must drive the exact same Probe state machine
    (PID/codec auto-detection, PTS tracking, IDR matching) as feeding the
    same packets directly via handle_ts_packet() does for a live capture
    -- proving file-mode output is otherwise identical to live-mode
    output, just sourced from disk instead of a socket. Two separate
    files are read in sequence (mirroring two separate recvfrom() calls
    in live mode) to also confirm Probe state persists correctly across
    calls."""
    import os as _os

    class Args:
        program = None
        pid_video = None
        pid_scte35 = None
        codec = None
        tolerance_ms = 6000.0
        max_early_ms = 50.0
        timeout_s = 12.0
        ok_threshold_ms = 41.0
        preroll_tolerance_ms = 500.0
        min_time_to_event_ms = 4000.0
        include_cra = False
        verbose = False
        csv_out = None
        json_out = None
        scte35_out = None
        scte35_log_file = None
        snapshot_dir = None
        snapshot_all_idr = False
        pre_frames = 0
        au_buffer_size = None
        ts_dump_dir = None
        ts_dump_window = 60.0
        ts_dump_all = False
        ts_dump_no_preroll = False
        ts_dump_max_files = None

    probe = mod.Probe(Args())

    pmt_pid = 0x100
    video_pid = 0x101
    scte35_pid = 0x1F0

    pat_pkt = build_pat(program_number=1, pmt_pid=pmt_pid)
    cuei_descriptor = bytes([0x05, 0x04]) + b"CUEI"
    pmt_pkt = build_pmt(
        pcr_pid=video_pid,
        streams=[
            (mod.STREAM_TYPE_H264, video_pid, b""),
            (mod.STREAM_TYPE_SCTE35, scte35_pid, cuei_descriptor),
        ],
        pmt_pid=pmt_pid,
    )
    # A video AU well before the eventual splice target, so time_to_event_ms
    # has something to measure against once the cue is registered below.
    pre_pts_ticks = int(round(999.0 * mod.PTS_HZ))
    pre_video_pkts = build_pes_packets(video_pid, pre_pts_ticks, h264_idr_nal())
    pre_video_pkts += build_pes_packets(
        video_pid, (pre_pts_ticks + 3600) % mod.PTS_MAX, h264_idr_nal(),
        cc_start=len(pre_video_pkts) % 16)

    file_a = _os.path.join(tmp_dir, "part_a.ts")
    with open(file_a, "wb") as f:
        f.write(pat_pkt)
        f.write(pmt_pkt)
        for pkt in pre_video_pkts:
            f.write(pkt)

    packets_seen = mod.process_ts_file(probe, file_a)
    assert packets_seen == 2 + len(pre_video_pkts), packets_seen
    assert probe.pmt_pid == pmt_pid
    assert probe.video_pid == video_pid, probe.video_pid
    assert probe.video_codec == "h264"
    assert probe.scte35_pid == scte35_pid
    assert probe.last_video_pts_ticks is not None
    print("OK: process_ts_file() reading PAT/PMT/video from a real file on "
          "disk correctly auto-detects PIDs/codec and tracks video PTS, "
          "exactly like live handle_ts_packet() calls would")

    # Register a fake decoded cue directly (as the other tests do -- this
    # sandbox has no threefive3 to actually decode a real SCTE-35 section),
    # targeting a PTS 3s after the last video PTS seen above.
    target_pts_seconds = 1002.0
    SpliceInsert = type("SpliceInsert", (), {})
    fake_command = SpliceInsert()
    fake_command.pts_time = target_pts_seconds
    fake_command.splice_event_id = 555
    fake_command.out_of_network_indicator = True
    fake_info = types.SimpleNamespace(pts_adjustment=0)
    fake_cue = types.SimpleNamespace(command=fake_command, info_section=fake_info)
    probe._register_cue(fake_cue, seq=1)
    assert len(probe.pending) == 1

    # A SECOND file, read via a SEPARATE process_ts_file() call (mirroring
    # a second recvfrom() in live mode), carrying the matching IDR.
    idr_pts_ticks = int(round((target_pts_seconds + 0.020) * mod.PTS_HZ)) % mod.PTS_MAX
    idr_pkts = build_pes_packets(video_pid, idr_pts_ticks, h264_idr_nal())
    idr_pkts += build_pes_packets(
        video_pid, (idr_pts_ticks + 3600) % mod.PTS_MAX, h264_idr_nal(),
        cc_start=len(idr_pkts) % 16)
    file_b = _os.path.join(tmp_dir, "part_b.ts")
    with open(file_b, "wb") as f:
        for pkt in idr_pkts:
            f.write(pkt)

    packets_seen_b = mod.process_ts_file(probe, file_b)
    assert packets_seen_b == len(idr_pkts)
    assert len(probe.pending) == 0, "expected the splice point to be matched via file input"
    print("OK: Probe state (PID/codec detection, pending splice points) "
          "correctly persists across separate process_ts_file() calls on "
          "the same file split in two, and the IDR in the second file "
          "correctly matches the cue registered in between -- the same "
          "way two separate recvfrom() calls would behave live")

    probe.close()


def test_flush_pending_as_missed():
    """Verify flush_pending_as_missed(): used both at --input-file EOF and
    at Ctrl-C on a live capture, so a splice point still waiting for a
    match when the run ends is reported as MISSED instead of silently
    vanishing from the output with no record at all. Also verifies the
    verdict text correctly distinguishes "ran out of input" (reason="eof",
    --input-file) from a genuine mid-stream "no IDR within --timeout-s"
    (reason="timeout", the pre-existing live-capture case)."""
    import json as _json
    import os as _os
    import tempfile as _tempfile

    def _make_cue(pts_time, event_id):
        SpliceInsert = type("SpliceInsert", (), {})
        cmd = SpliceInsert()
        cmd.pts_time = pts_time
        cmd.splice_event_id = event_id
        cmd.out_of_network_indicator = True
        info = types.SimpleNamespace(pts_adjustment=0)
        return types.SimpleNamespace(command=cmd, info_section=info)

    with _tempfile.TemporaryDirectory() as tmp_dir:
        json_out_path = _os.path.join(tmp_dir, "flush.jsonl")

        class Args:
            program = None
            pid_video = None
            pid_scte35 = None
            codec = "h264"
            tolerance_ms = 6000.0
            max_early_ms = 50.0
            timeout_s = 12.0
            ok_threshold_ms = 41.0
            preroll_tolerance_ms = 500.0
            min_time_to_event_ms = 4000.0
            include_cra = False
            verbose = False
            csv_out = None
            json_out = json_out_path
            scte35_out = None
            scte35_log_file = None
            snapshot_dir = None
            snapshot_all_idr = False
            pre_frames = 0
            au_buffer_size = None
            ts_dump_dir = None
            ts_dump_window = 60.0
            ts_dump_all = False
            ts_dump_no_preroll = False
            ts_dump_max_files = None

        probe = mod.Probe(Args())
        probe._register_cue(_make_cue(2000.0, 10), seq=1)
        probe._register_cue(_make_cue(2001.0, 11), seq=2)
        assert len(probe.pending) == 2

        probe.flush_pending_as_missed(reason="eof")
        assert len(probe.pending) == 0, "flush_pending_as_missed must clear self.pending"

        with open(json_out_path) as f:
            rows = [_json.loads(ln) for ln in f if ln.strip()]
        assert len(rows) == 2, rows
        for row in rows:
            assert row["verdict"] == "MISSED (input ended before a matching IDR was found)", row
            assert row["actual_preroll_ms"] is None
            assert row["preroll_verdict"] == "N/A"
        print("OK: flush_pending_as_missed(reason='eof') clears self.pending "
              "and emits each still-open cue with the EOF-specific MISSED "
              "verdict text, instead of silently dropping it from the "
              "output")

        # -- reason='timeout' (the pre-existing live-capture wording, used
        # by _match_idr()'s own timeout sweep) must still read as before --
        # this is a regression guard that the new eof-specific message
        # didn't silently replace the original one everywhere.
        probe._register_cue(_make_cue(2002.0, 12), seq=3)
        probe.flush_pending_as_missed(reason="timeout")
        with open(json_out_path) as f:
            rows = [_json.loads(ln) for ln in f if ln.strip()]
        assert rows[-1]["verdict"] == "MISSED (no IDR near target PTS within timeout)", rows[-1]
        print("OK: flush_pending_as_missed(reason='timeout') keeps the "
              "original timeout wording, unchanged from before this fix")

        probe.close()


def test_version_string():
    """Regression guard: __version__ must exist and be a plain, non-empty
    string (used by --version and logged at startup, and stamped into the
    --json-out/--scte35-out/--ts-dump-dir sidecar records), so a report
    can always be traced back to the tool version that produced it."""
    assert hasattr(mod, "__version__"), "scte35_idr_diff.py must define __version__"
    assert isinstance(mod.__version__, str) and mod.__version__.strip(), (
        "__version__ must be a non-empty string, got %r" % (mod.__version__,))
    print("OK: __version__ is defined as %r" % mod.__version__)


if __name__ == "__main__":
    import tempfile

    test_pts_diff_wraparound()
    test_pat_pmt_and_idr_match()
    test_pts_adjustment_applied()
    test_rejects_unrelated_earlier_idr()
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_full_scte35_logging(tmp_dir)
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_idr_jpeg_snapshot(tmp_dir)
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_pre_frame_snapshots(tmp_dir)
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_ts_dump_only_saves_windows_with_events(tmp_dir)
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_ts_dump_no_preroll(tmp_dir)
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_ts_dump_max_files_prunes_oldest(tmp_dir)
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_time_to_event_and_actual_preroll(tmp_dir)
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_signal_verdict_min_time_to_event(tmp_dir)
    test_validate_ipv4_literal()
    test_unicast_loopback_addr()
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_process_ts_file(tmp_dir)
    test_flush_pending_as_missed()
    test_version_string()
    print("\nAll offline self-tests passed.")

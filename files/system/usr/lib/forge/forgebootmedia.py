"""Bootup media for Forge OS: turn a picture or video into boot splash frames, prepare the login screen
picture, and pack both for the root helper. Runs as you, never as root.

The decoders (ffmpeg, ffprobe, and as a fallback rsvg-convert or ImageMagick) read files from anywhere, so
they run with hard limits: a time limit, memory and file size limits, only local files (ffmpeg
-protocol_whitelist file), a fixed demuxer picked from the file's bytes, and, when /usr/bin/bwrap is
installed and works, inside a bubblewrap sandbox with no network and only /usr, the one input file and an
empty output folder.

Caps for the boot splash (the root helper checks them again):
  at most 90 frames (60 for a loop), at most 480 x 480 pixels, at most 8 MB of PNG together.
  Plymouth decodes every frame into memory before the disk is unlocked (90 x 480 x 480 x 4 bytes is
  about 83 MB at most), and the frames ride along in every boot image in /boot.
"""
import ctypes
import json
import os
import re
import resource
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import threading
import time

MAX_FRAMES = 90
LOOP_MAX_FRAMES = 60          # Plymouth plays a loop in 2 seconds at about 30 frames a second
MAX_SIDE = 480
TOTAL_MAX = 8 * 1024 * 1024
FRAME_MAX = 2 * 1024 * 1024
LOGIN_IMAGE_MAX = 64 * 1024 * 1024
LOGIN_VIDEO_MAX = 256 * 1024 * 1024
LOGIN_MAX_W, LOGIN_MAX_H = 7680, 4320
LOGIN_VIDEO_FORMATS = ("mp4", "webm", "mkv", "mov")
MAX_PIXELS = 64 * 1024 * 1024     # largest picture a decoder may open (8192 x 8192)
MAX_DIM = 16384
PROBE_TIMEOUT_S = 20
STEP_TIMEOUT_S = 45
DEADLINE_S = 80
BWRAP = "/usr/bin/bwrap"
DEADLINE = None               # time.monotonic() value after which no decoder may run (set by the backend)


def set_deadline(when):
    global DEADLINE
    DEADLINE = when


def _cap(name, default):
    try:
        v = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return v if 0 < v < default else default


INPUT_MAX = _cap("FORGE_BOOT_INPUT_MAX", 1024 ** 3)     # largest file the boot splash converts (1 GiB)

# The ffmpeg demuxer for each format forgeappearance finds in a file's bytes. Fixing it means a file can
# never be read as a playlist or a list of other files.
DEMUXERS = {
    "png": "png_pipe", "apng": "apng", "jpeg": "jpeg_pipe", "gif": "gif", "webp": "webp_pipe", "bmp": "bmp_pipe",
    "tiff": "tiff_pipe", "svg": "svg_pipe", "jxl": "jpegxl_pipe", "ico": "ico", "avif": "mov", "heic": "mov",
    "heif": "mov", "mp4": "mov", "m4v": "mov", "mov": "mov", "3gp": "mov", "mkv": "matroska", "webm": "matroska",
    "avi": "avi", "wmv": "asf", "flv": "flv", "mpeg": "mpeg", "ogv": "ogg", "ts": "mpegts", "_seq": "image2",
}
# ImageMagick coders for the picture fallback (an explicit coder: the file is never "guessed").
IM_CODERS = {"png": "PNG", "jpeg": "JPEG", "gif": "GIF", "webp": "WEBP", "bmp": "BMP", "tiff": "TIFF",
             "avif": "AVIF", "heic": "HEIC", "heif": "HEIC", "jxl": "JXL", "ico": "ICO"}


class ConvertError(Exception):
    """The file could not be used. The text is for the user."""


# Running the decoders

def _which(name):
    return shutil.which(name, path=os.environ.get("PATH", "/usr/bin:/bin"))


def _die_with_parent():
    """Ask the kernel to kill this child if the backend goes away (PR_SET_PDEATHSIG), best effort."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _limits(cpu_s):
    def apply():
        os.setsid()
        _die_with_parent()
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))
        resource.setrlimit(resource.RLIMIT_AS, (4 * 1024 ** 3, 4 * 1024 ** 3))
        resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 ** 2, 256 * 1024 ** 2))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def _run(argv, timeout):
    if DEADLINE is not None:
        left = DEADLINE - time.monotonic()
        if left < 2:
            raise ConvertError("converting took too long. Try a shorter or smaller file")
        timeout = max(1, min(timeout, int(left)))
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": tempfile.gettempdir()}
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=env, preexec_fn=_limits(timeout), close_fds=True)
    except OSError as e:
        raise ConvertError(f"{os.path.basename(argv[0])} could not start ({e.strerror})")
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return None, b""
    return p.returncode, out[-4000:]


_sandbox = {}


def _bwrap():
    """The bwrap program if it is installed and works here (user namespaces allowed), else None."""
    path = os.environ.get("FORGE_BOOT_BWRAP") or BWRAP
    if path not in _sandbox:
        ok = os.path.isfile(path) and os.access(path, os.X_OK)
        if ok:
            true = _which("true") or "/usr/bin/true"
            rc, _ = _run([path] + _bwrap_base() + ["--", true], 10)
            ok = rc == 0
        _sandbox[path] = path if ok else None
    return _sandbox[path]


def _bwrap_base():
    return ["--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
            "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache", "--ro-bind-try", "/etc/fonts", "/etc/fonts",
            "--ro-bind-try", "/etc/ImageMagick-6", "/etc/ImageMagick-6",
            "--ro-bind-try", "/etc/ImageMagick-7", "/etc/ImageMagick-7",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--setenv", "PATH", "/usr/bin", "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
            "--chdir", "/tmp"]


def run_tool(build, src, outdir, timeout):
    """Run build(input_path, output_dir) -> argv, sandboxed when possible. Returns (rc or None, output)."""
    bw = _bwrap()
    if bw:
        argv = build("/in/input", "/out")
        exe = os.path.realpath(argv[0])
        if exe.startswith("/usr/"):
            return _run([bw] + _bwrap_base() + ["--ro-bind", src, "/in/input", "--bind", outdir, "/out", "--",
                                                exe] + argv[1:], timeout)
    return _run(build(src, outdir), timeout)


# Checking the input

def _open_input(path, cap):
    """Check path is a regular file of yours (not a link) and not too large."""
    try:
        st = os.lstat(path)
    except OSError:
        raise ConvertError("the file is missing from your media library")
    if stat.S_ISLNK(st.st_mode):
        raise ConvertError("the file is a link, so it is not used")
    if not stat.S_ISREG(st.st_mode):
        raise ConvertError("the file is not a regular file")
    if st.st_size > cap:
        raise ConvertError(f"the file is too large for this (at most {cap // 1024 // 1024} MB)")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        os.close(fd)
    except OSError as e:
        raise ConvertError(f"the file could not be opened ({e.strerror})")
    return st


def _not_root():
    if os.geteuid() == 0:
        raise ConvertError("media is never converted as root")


def probe(path, fmt, scratch):
    """Width, height and duration of the first video stream, from ffprobe in the sandbox."""
    ffprobe = _which("ffprobe")
    if not ffprobe:
        raise ConvertError("ffprobe is not installed")
    demux = DEMUXERS.get(fmt)
    if not demux:
        raise ConvertError("this kind of file can't be used here")
    rc, out = run_tool(lambda i, o: [ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", demux,
                                     "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                                     "-of", "json", i], path, scratch, PROBE_TIMEOUT_S)
    if rc is None:
        raise ConvertError("reading the file took too long. Try a shorter or smaller file")
    if rc != 0:
        raise ConvertError("the file could not be read (it may be damaged or in an unknown format)")
    try:
        info = json.loads(out.decode("utf-8", "replace"))
        s = info["streams"][0]
        w, h = int(s["width"]), int(s["height"])
    except (ValueError, KeyError, IndexError, TypeError):
        raise ConvertError("no picture or video was found in the file")
    if not (0 < w <= MAX_DIM and 0 < h <= MAX_DIM) or w * h > MAX_PIXELS:
        raise ConvertError(f"the picture is too large ({w} x {h} pixels; at most {MAX_PIXELS // 1024 // 1024} "
                           "megapixels)")
    try:
        duration = float(info.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return w, h, duration


# PNG helpers

def png_size(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or data[12:16] != b"IHDR":
        raise ConvertError("a frame is not a PNG picture")
    return struct.unpack(">II", data[16:24])


def _collect(outdir, side, cap=FRAME_MAX):
    names = sorted(n for n in os.listdir(outdir))
    if not names or names != [f"frame-{i:04d}.png" for i in range(1, len(names) + 1)]:
        return None
    frames = []
    for n in names:
        p = os.path.join(outdir, n)
        st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cap:
            return None
        with open(p, "rb") as f:
            data = f.read()
        w, h = png_size(data)
        if not (1 <= w <= side and 1 <= h <= side):
            return None
        frames.append(data)
    if len({png_size(f) for f in frames}) != 1:
        return None
    return frames


def _clear(d):
    for n in os.listdir(d):
        p = os.path.join(d, n)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)


# Boot splash

def ffmpeg_argv(ffmpeg, inp, fmt, outdir, side, frames, length, palette, still, framerate=None):
    """The ffmpeg command for one try. Only local files, a fixed demuxer, the first video stream only."""
    demux = DEMUXERS.get(fmt)
    if not demux:
        raise ConvertError("this kind of file can't be used here")
    a = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "2",
         "-protocol_whitelist", "file", "-max_pixels", str(MAX_PIXELS), "-f", demux]
    if framerate:
        a += ["-framerate", f"{framerate:.6f}", "-start_number", "1"]
    if not still:
        a += ["-t", f"{length:.3f}"]
    a += ["-i", inp, "-map", "0:v:0", "-an", "-sn", "-dn"]
    vf = [] if still else [f"fps={frames / length:.6f}"]
    vf += [f"scale=w='min(iw,{side})':h='min(ih,{side})':force_original_aspect_ratio=decrease:flags=lanczos",
           "format=rgba"]
    graph = ",".join(vf)
    if palette:
        graph += (",split[a][b];[a]palettegen=max_colors=256:reserve_transparent=1:stats_mode=full[p];"
                  "[b][p]paletteuse=dither=sierra2_4a")
    a += ["-vf", graph, "-frames:v", str(1 if still else frames), "-f", "image2", "-start_number", "1",
          "-compression_level", "9", os.path.join(outdir, "frame-%04d.png")]
    return a


def _fallback_decode(src, kind, fmt, outdir, side, frames, scratch=None, cap=FRAME_MAX):
    """Pictures ffmpeg can't read (an animated WebP on older ffmpeg, some SVG): rsvg-convert or ImageMagick."""
    if fmt == "svg":
        rsvg = _which("rsvg-convert")
        if not rsvg:
            return False
        # An SVG may point at other files next to it. Render a copy that sits alone in a private folder,
        # so there is nothing else it could pull in (also without the bwrap sandbox).
        alone = tempfile.mkdtemp(prefix=".forge-svg-", dir=scratch or outdir)
        try:
            copy = os.path.join(alone, "input.svg")
            fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as f, open(copy, "wb") as g:
                shutil.copyfileobj(f, g)
            rc, _ = run_tool(lambda i, o: [rsvg, "--keep-aspect-ratio", "-w", str(side), "-h", str(side), "-f",
                                           "png", "-o", os.path.join(o, "frame-0001.png"), i], copy, outdir,
                             STEP_TIMEOUT_S)
        finally:
            shutil.rmtree(alone, ignore_errors=True)
        return rc == 0
    coder = IM_CODERS.get(fmt)
    im = _which("magick") or _which("convert")
    if not coder or not im:
        return False
    count = 1 if kind == "image" else frames
    rc, _ = run_tool(lambda i, o: [im, "-limit", "memory", "256MiB", "-limit", "map", "512MiB", "-limit", "disk",
                                   "1GiB", "-limit", "area", "64MP", "-limit", "width", "16KP", "-limit", "height",
                                   "16KP", "-limit", "time", str(STEP_TIMEOUT_S), "-limit", "thread", "2",
                                   f"{coder}:{i}[0-{count - 1}]", "-coalesce", "-resize", f"{side}x{side}>",
                                   "-strip", "-scene", "1", "PNG32:" + os.path.join(o, "frame-%04d.png")],
                     src, outdir, STEP_TIMEOUT_S)
    return rc == 0 and _collect(outdir, max(side, MAX_SIDE), cap) is not None


def _ladder(ffmpeg, src, sfmt, out, side, want, length, still, rate, t0, sandbox=True):
    """Try smaller and smaller output until the frames fit the caps. Returns the frames or raises."""
    tries = [(side, want, False), (side, want, True), (side, max(1, want // 2), True),
             (max(16, side * 3 // 4), max(1, want // 2), True), (max(16, side // 2), max(1, want // 3), True)]
    last = "the file could not be converted"
    for s, n, pal in tries:
        if time.monotonic() - t0 > DEADLINE_S:
            raise ConvertError("converting took too long. Try a shorter or smaller file")
        _clear(out)

        def build(i, o, s=s, n=n, pal=pal):
            return ffmpeg_argv(ffmpeg, i, sfmt, o, s, n, length, pal, still, framerate=rate)
        rc, _ = run_tool(build, src, out, STEP_TIMEOUT_S) if sandbox else _run(build(src, out), STEP_TIMEOUT_S)
        if rc is None:
            raise ConvertError("converting took too long. Try a shorter or smaller file")
        if rc != 0:
            raise ConvertError("the file could not be converted (it may be damaged)")
        frames = _collect(out, s)
        if frames is None:
            raise ConvertError("the converted frames were not as expected")
        if sum(len(f) for f in frames) <= TOTAL_MAX:
            return frames
        last = f"the frames stay larger than {TOTAL_MAX // 1024 // 1024} MB. Try a shorter part or a smaller size"
    raise ConvertError(last)


def convert_splash(path, kind, fmt, play, length, side, workdir):
    """Frames for the boot splash. Returns {"frames": [png bytes], "width", "height"}. ConvertError if not."""
    _not_root()
    if play not in ("loop", "once") or kind not in ("image", "animated-image", "video"):
        raise ConvertError("unknown play mode or kind of file")
    side = max(16, min(int(side), MAX_SIDE))
    length = max(0.5, min(float(length), 10.0))
    _open_input(path, INPUT_MAX)
    ffmpeg = _which("ffmpeg")
    if not ffmpeg:
        raise ConvertError("ffmpeg is not installed, so pictures and videos can't be converted")
    still = kind == "image"
    want = 1 if still else min(LOOP_MAX_FRAMES if play == "loop" else MAX_FRAMES, max(2, round(30 * length)))
    t0 = time.monotonic()
    tmp = tempfile.mkdtemp(prefix=".forge-boot-", dir=workdir)
    try:
        out = os.path.join(tmp, "out")
        os.mkdir(out)
        try:
            probe(path, fmt, out)
            frames = _ladder(ffmpeg, path, fmt, out, side, want, length, still, None, t0)
            if kind == "animated-image" and len(frames) < 2:
                raise ConvertError("only one frame came out of an animated picture")
        except ConvertError as e:
            if kind == "video" or fmt not in ("svg",) + tuple(IM_CODERS):
                raise
            # Pictures ffmpeg can't read well: decode them with the fallback tool, then squeeze the result.
            seq = os.path.join(tmp, "seq")
            os.mkdir(seq)
            if not _fallback_decode(path, kind, fmt, seq, MAX_SIDE, want, scratch=tmp):
                raise e
            n = len(os.listdir(seq))
            seq_still = n == 1
            frames = _ladder(ffmpeg, os.path.join(seq, "frame-%04d.png"), "_seq", out, side, min(want, n), length,
                             seq_still, None if seq_still else n / length, t0, sandbox=False)
        w, h = png_size(frames[0])
        # "wanted": how many frames the chosen length asked for. Fewer means frames were left out evenly to
        # stay under the caps: the timing stays the same, the animation is less smooth.
        return {"frames": frames, "width": w, "height": h, "wanted": want}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Login screen

def prepare_login(path, kind, fmt, workdir):
    """The login screen file: a PNG for any picture (made here), or the video itself if it is MP4, WebM,
    MKV or MOV. Returns (extension, path)."""
    _not_root()
    if kind == "video":
        if fmt not in LOGIN_VIDEO_FORMATS:
            raise ConvertError("the login screen plays MP4, WebM, MKV or MOV videos. Convert this one first")
        _open_input(path, LOGIN_VIDEO_MAX)
        scratch = tempfile.mkdtemp(prefix=".forge-login-", dir=workdir)
        try:
            probe(path, fmt, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return fmt, path
    _open_input(path, INPUT_MAX)
    ffmpeg = _which("ffmpeg")
    if not ffmpeg:
        raise ConvertError("ffmpeg is not installed, so pictures can't be converted")
    out = tempfile.mkdtemp(prefix=".forge-login-", dir=workdir)
    try:
        ok = False
        try:
            probe(path, fmt, out)
            for w, h in ((LOGIN_MAX_W, LOGIN_MAX_H), (3840, 2160)):
                _clear(out)
                rc, _ = run_tool(lambda i, o, w=w, h=h: [
                    ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "2",
                    "-protocol_whitelist", "file", "-max_pixels", str(MAX_PIXELS), "-f", DEMUXERS[fmt], "-i", i,
                    "-map", "0:v:0", "-frames:v", "1", "-vf",
                    f"scale=w='min(iw,{w})':h='min(ih,{h})':force_original_aspect_ratio=decrease,format=rgb24",
                    "-compression_level", "3", "-f", "image2", os.path.join(o, "frame-0001.png")],
                    path, out, STEP_TIMEOUT_S)
                p = os.path.join(out, "frame-0001.png")
                if rc == 0 and os.path.isfile(p) and os.path.getsize(p) <= LOGIN_IMAGE_MAX:
                    ok = True
                    break
        except ConvertError:
            if fmt not in ("svg",) + tuple(IM_CODERS):
                raise
        if not ok:
            _clear(out)
            if not _fallback_decode(path, "image", fmt, out, 3840, 1, scratch=workdir, cap=LOGIN_IMAGE_MAX):
                raise ConvertError("the picture could not be converted (it may be damaged)")
        final = os.path.join(workdir, f".forge-login-{os.getpid()}.png")
        os.replace(os.path.join(out, "frame-0001.png"), final)
        return "png", final
    finally:
        shutil.rmtree(out, ignore_errors=True)


# The bundle for the root helper

MAGIC = b"FORGEBOOT1\n"


def write_bundle(stream, splash, login, frames=(), login_file=None):
    """Write the bundle forge-boot-apply reads: a small JSON header, then the files in a fixed order.
    login_file: (format, path) when the login part is "on"."""
    files = [(f"frame-{i:04d}.png", len(f)) for i, f in enumerate(frames, 1)]
    if login_file:
        files.append((f"login.{login_file[0]}", os.path.getsize(login_file[1])))
    header = json.dumps({"version": 1, "splash": splash, "login": login,
                         "files": [{"name": n, "size": s} for n, s in files]}).encode()
    stream.write(MAGIC + str(len(header)).encode() + b"\n" + header)
    for f in frames:
        stream.write(f)
    if login_file:
        size = files[-1][1]
        fd = os.open(login_file[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as f:
            sent = 0
            while sent < size:
                chunk = f.read(min(1 << 20, size - sent))
                if not chunk:
                    raise ConvertError("the login file changed while it was being sent")
                stream.write(chunk)
                sent += len(chunk)
    stream.flush()


def send_bundle(argv, timeout, splash, login, frames=(), login_file=None):
    """Start argv (pkexec ...), write the bundle to its stdin, wait. Returns (rc or None on timeout, output)."""
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         close_fds=True)
    got = []

    def feed():
        try:
            write_bundle(p.stdin, splash, login, frames, login_file)
        except (OSError, ConvertError):
            pass                       # the other side stopped reading: its answer says why
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass

    def drain():
        while True:
            chunk = p.stdout.read(65536)
            if not chunk:
                break
            got.append(chunk)
    threads = [threading.Thread(target=feed, daemon=True), threading.Thread(target=drain, daemon=True)]
    for t in threads:
        t.start()
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            p.kill()        # works only while pkexec still waits for the password; the root part can't be stopped
        except OSError:
            pass
        return None, b"".join(got)[-16000:]
    threads[1].join(5)
    return p.returncode, b"".join(got)[-16000:]

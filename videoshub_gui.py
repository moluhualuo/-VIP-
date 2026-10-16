#!/usr/bin/env python3
"""
VideosHub Downloader — tkinter desktop UI.

A thin front-end over `videoshub_core`. Every network operation runs on a
worker thread; the UI thread only touches widgets. The worker talks back
through a thread-safe queue of closures that the UI drains on a timer, which
avoids the classic tkinter cross-thread crash.

Run from source:  python videoshub_gui.py
Packaged exe:     dist/VideosHubDownloader.exe
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import videoshub_core as core
import videoshub_player as player

APP_TITLE = "VideosHub 视频下载器"
VERSION = "1.1"

# ---------------- look & feel ----------------
# One small palette, used everywhere, so the window reads as a single piece
# instead of a pile of default-grey widgets. Change it here to retheme.
BG        = "#f4f5f7"   # window background
CARD      = "#ffffff"   # section surface
BORDER    = "#e4e7ec"   # hairline around cards
TEXT      = "#1f2328"   # primary text
MUTED     = "#6b7280"   # hints / secondary text
ACCENT    = "#2f6feb"   # primary action
ACCENT_HI = "#1f5bd6"   # primary action, hover
ACCENT_OFF = "#aebfe4"  # primary action, disabled
SUCCESS   = "#16a34a"
LOG_BG    = "#0f1419"   # console panel
LOG_FG    = "#c9d1d9"
FONT      = "Microsoft YaHei UI"   # renders CJK cleanly on Windows
FONT_MONO = "Consolas"


def _resource_dir() -> str:
    """Where bundled read-only assets live (works both frozen and from source)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _label(parent, text, muted=False, bold=False, bg=CARD, **kw):
    """A themed tk.Label. tk (not ttk) so the colour is actually honoured."""
    font = (FONT, 9, "bold") if bold else (FONT, 9)
    return tk.Label(parent, text=text, bg=bg, fg=MUTED if muted else TEXT,
                    font=font, **kw)


def _primary_button(parent, text, command):
    """A flat, coloured call-to-action button."""
    return tk.Button(parent, text=text, command=command,
                     bg=ACCENT, fg="#ffffff",
                     activebackground=ACCENT_HI, activeforeground="#ffffff",
                     disabledforeground="#e8eefc",
                     relief="flat", bd=0, padx=24, pady=8,
                     font=(FONT, 10, "bold"), cursor="hand2")


def _set_enabled(btn, on: bool, primary=False):
    """Enable/disable a button, keeping the primary button's colour coherent."""
    btn.configure(state="normal" if on else "disabled")
    if primary:
        btn.configure(bg=ACCENT if on else ACCENT_OFF)


def _setup_style(root):
    """Configure ttk so its widgets match the palette.

    The default Windows themes ignore most colour options, so we switch to
    'clam', which honours them.
    """
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass

    st.configure(".", background=CARD, foreground=TEXT, font=(FONT, 9))
    for name in ("TFrame", "TLabel"):
        st.configure(name, background=CARD, foreground=TEXT)
    for name in ("TRadiobutton", "TCheckbutton"):
        st.configure(name, background=CARD, foreground=TEXT, focuscolor=CARD)
        st.map(name, background=[("active", CARD)])

    st.configure("TEntry", fieldbackground="#ffffff", bordercolor=BORDER,
                 lightcolor=BORDER, darkcolor=BORDER, padding=5)
    st.map("TEntry", bordercolor=[("focus", ACCENT)],
           lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])

    st.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff",
                 bordercolor=BORDER, arrowcolor=MUTED, padding=4)
    st.map("TCombobox",
           fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f2f3f5")],
           foreground=[("disabled", MUTED)],
           bordercolor=[("focus", ACCENT)])

    st.configure("TButton", padding=(12, 6), font=(FONT, 9),
                 background="#eef0f3", bordercolor=BORDER, focuscolor=CARD)
    st.map("TButton",
           background=[("active", "#e2e5ea"), ("disabled", "#f6f7f9")],
           foreground=[("disabled", "#a9b0ba")])

    st.configure("Accent.Horizontal.TProgressbar",
                 troughcolor="#e6e9ef", background=ACCENT,
                 bordercolor="#e6e9ef", lightcolor=ACCENT, darkcolor=ACCENT,
                 thickness=8)
    st.configure("Vertical.TScrollbar", background="#cbd2dc",
                 troughcolor=LOG_BG, bordercolor=LOG_BG, arrowcolor="#8b98a8")


class QueueProgress(core.Progress):
    """Marshals worker-thread events onto the UI thread via a queue.

    Segment events are throttled at the source: with 8 parallel workers the
    raw rate would flood the UI thread and make the window unresponsive.
    The progress bar only ever needs the latest value, so we drop the rest.
    """

    # Emit at most one segment event per this many segments, plus the last one.
    SEGMENT_STRIDE = 4

    def __init__(self, q: "queue.Queue"):
        self.q = q

    def log(self, message: str) -> None:
        self.q.put(("log", message))

    def stage(self, name: str) -> None:
        self.q.put(("stage", name))

    def segment(self, done, total, name, enc, dec) -> None:
        if done % self.SEGMENT_STRIDE and done != total:
            return
        self.q.put(("segment", (done, total, name, enc, dec)))

    def finish(self, path: str) -> None:
        self.q.put(("finish", path))

    def partial(self, path: str, done: int, total: int) -> None:
        self.q.put(("partial", (path, done, total)))


class App(tk.Frame):
    def __init__(self, master: tk.Tk):
        # tk.Frame (not ttk) so the window background colour is honoured.
        super().__init__(master, bg=BG, padx=16, pady=16)
        self.master = master
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        self.q: "queue.Queue" = queue.Queue()
        self.progress = QueueProgress(self.q)
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.start_time = 0.0
        self.last_result: str | None = None

        self._build_vars()
        self._build_widgets()
        self._poll_queue()

    # ---------------- state ----------------

    def _build_vars(self):
        self.var_mode = tk.StringVar(value="random")
        self.var_video_id = tk.StringVar()
        self.var_quality = tk.StringVar(value="1080p")
        self.var_max_seg = tk.StringVar(value="0")
        self.var_workers = tk.StringVar(value="8")
        self.var_outdir = tk.StringVar(value=os.path.join(os.getcwd(), "videoshub_out"))
        self.var_outname = tk.StringVar(value="output")
        self.var_keep_ts = tk.BooleanVar(value=True)
        self.var_make_mp4 = tk.BooleanVar(value=True)
        self.var_url = tk.StringVar()
        self.var_status = tk.StringVar(value="就绪")
        self.var_progress = tk.DoubleVar(value=0.0)
        self.var_autoplay = tk.BooleanVar(value=False)
        self.var_player = tk.StringVar(value="系统默认播放器")
        # Path of the in-progress .ts, so the preview button knows what to open.
        self.partial_ts: str | None = None
        self._preview_ready = False
        self.pending_finish = False
        self._log_lines = 0

    # ---------------- layout ----------------

    def _card(self, row, title, expand=False):
        """A white panel with a hairline border and a small section title.

        Returns the inner frame to put content in.
        """
        outer = tk.Frame(self, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1)
        outer.grid(row=row, column=0, sticky="nsew" if expand else "ew",
                   pady=(0, 10))
        outer.columnconfigure(0, weight=1)
        _label(outer, title, muted=True, bold=True).grid(
            row=0, column=0, sticky="w", padx=14, pady=(8, 0))
        body = tk.Frame(outer, bg=CARD)
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(6, 10))
        body.columnconfigure(0, weight=1)
        if expand:
            outer.rowconfigure(1, weight=1)
            body.rowconfigure(0, weight=1)
        return body

    def _build_widgets(self):
        # ---- header ----------------------------------------------------
        header = tk.Frame(self, bg=CARD, highlightbackground=BORDER,
                          highlightthickness=1)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        tk.Label(header, text=APP_TITLE, bg=CARD, fg=TEXT,
                 font=(FONT, 14, "bold")).grid(row=0, column=0, sticky="w",
                                               padx=14, pady=(10, 0))
        _label(header, "HLS 并发下载 · AES-128 解密 · 自动转 MP4",
               muted=True).grid(row=1, column=0, sticky="w",
                                padx=14, pady=(1, 9))
        _label(header, f"v{VERSION}", muted=True).grid(
            row=0, column=1, rowspan=2, sticky="e", padx=14)
        row = 1

        # ---- download mode ---------------------------------------------
        body = self._card(row, "下载方式"); row += 1

        modes = tk.Frame(body, bg=CARD)
        modes.grid(row=0, column=0, sticky="w")
        for i, (txt, val) in enumerate((("随机视频", "random"),
                                        ("指定视频 ID", "id"),
                                        ("直接输入 URL", "url"))):
            ttk.Radiobutton(modes, text=txt, value=val,
                            variable=self.var_mode,
                            command=self._sync_mode).grid(row=0, column=i,
                                                          padx=(0, 20))

        # Each input lives in its own frame so _sync_mode can show only the one
        # the current mode needs — keeps this card (and the window) short.
        self.row_id = tk.Frame(body, bg=CARD)
        self.row_id.columnconfigure(0, weight=1)
        self.ent_video_id = ttk.Entry(self.row_id, textvariable=self.var_video_id,
                                      font=(FONT, 10))
        self.ent_video_id.grid(row=0, column=0, sticky="ew")
        _label(self.row_id, "例如 32238", muted=True).grid(row=0, column=1,
                                                           padx=(10, 0))

        self.row_url = tk.Frame(body, bg=CARD)
        self.row_url.columnconfigure(0, weight=1)
        self.ent_url = ttk.Entry(self.row_url, textvariable=self.var_url,
                                 font=(FONT, 10))
        self.ent_url.grid(row=0, column=0, sticky="ew")

        self.lbl_url_hint = _label(
            body, "支持 .m3u8 完整播放列表（免登录直下），"
                  "或含 /p2/preview/ 的预览地址（自动签名）", muted=True)

        # ---- parameters -------------------------------------------------
        body = self._card(row, "参数"); row += 1
        g = tk.Frame(body, bg=CARD)
        g.grid(row=0, column=0, sticky="ew")
        g.columnconfigure(1, weight=1)
        g.columnconfigure(4, weight=1)

        _label(g, "清晰度").grid(row=0, column=0, sticky="w", pady=3)
        self.cmb_quality = ttk.Combobox(g, textvariable=self.var_quality,
                                        values=["480p", "720p", "1080p"],
                                        state="readonly", width=10,
                                        font=(FONT, 10))
        self.cmb_quality.grid(row=0, column=1, sticky="w", padx=(8, 24), pady=3)

        _label(g, "最大分片数").grid(row=0, column=2, sticky="w", pady=3)
        self.ent_maxseg = ttk.Entry(g, textvariable=self.var_max_seg,
                                    width=10, font=(FONT, 10))
        self.ent_maxseg.grid(row=0, column=3, sticky="w", padx=(8, 8), pady=3)
        _label(g, "0 = 全部", muted=True).grid(row=0, column=4, sticky="w",
                                              pady=3)

        _label(g, "并发连接").grid(row=1, column=0, sticky="w", pady=3)
        self.cmb_workers = ttk.Combobox(
            g, textvariable=self.var_workers, state="readonly", width=6,
            values=["1", "2", "4", "8", "12", "16", "24", "32"],
            font=(FONT, 10))
        self.cmb_workers.grid(row=1, column=1, sticky="w", padx=(8, 24), pady=3)
        _label(g, "越大越快；网络差或服务端限速时调小（推荐 8）",
               muted=True).grid(row=1, column=2, columnspan=3, sticky="w",
                                pady=3)

        _label(g, "输出目录").grid(row=2, column=0, sticky="w", pady=3)
        self.ent_outdir = ttk.Entry(g, textvariable=self.var_outdir,
                                    font=(FONT, 10))
        self.ent_outdir.grid(row=2, column=1, columnspan=3, sticky="ew",
                             padx=(8, 8), pady=3)
        ttk.Button(g, text="浏览…", width=8, command=self._choose_dir).grid(
            row=2, column=4, sticky="w", pady=3)

        # Filename and the two toggles share a row to keep the card short.
        _label(g, "文件名").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(g, textvariable=self.var_outname, width=20,
                  font=(FONT, 10)).grid(row=3, column=1, sticky="w",
                                        padx=(8, 24), pady=3)
        checks = tk.Frame(g, bg=CARD)
        checks.grid(row=3, column=2, columnspan=3, sticky="w", pady=3)
        ttk.Checkbutton(checks, text="自动转成 MP4",
                        variable=self.var_make_mp4).grid(row=0, column=0,
                                                         padx=(0, 18))
        ttk.Checkbutton(checks, text="转换后保留中间 .ts 文件",
                        variable=self.var_keep_ts).grid(row=0, column=1)

        # ---- playback ---------------------------------------------------
        body = self._card(row, "播放"); row += 1
        prow = tk.Frame(body, bg=CARD)
        prow.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(prow, text="下载完成后自动播放",
                        variable=self.var_autoplay).grid(row=0, column=0,
                                                         sticky="w")
        _label(prow, "播放器").grid(row=0, column=1, padx=(20, 6))
        self.players = player.find_players()
        self.cmb_player = ttk.Combobox(
            prow, textvariable=self.var_player, state="readonly", width=24,
            font=(FONT, 10),
            values=["系统默认播放器"] + [n for n, _ in self.players])
        self.cmb_player.grid(row=0, column=2, sticky="w")
        self.btn_preview = ttk.Button(prow, text="预览已下载部分",
                                      command=self._preview_partial,
                                      state="disabled")
        self.btn_preview.grid(row=0, column=3, padx=(16, 0))

        if not self.players:
            # Inline rather than on its own row, to keep the card one line tall.
            _label(prow, "未检测到播放器，将用系统默认", muted=True).grid(
                row=0, column=4, padx=(14, 0))

        # ---- actions + progress -----------------------------------------
        body = self._card(row, "操作"); row += 1

        act = tk.Frame(body, bg=CARD)
        act.grid(row=0, column=0, sticky="ew")
        act.columnconfigure(2, weight=1)
        self.btn_start = _primary_button(act, "开始下载", self._start)
        self.btn_start.grid(row=0, column=0, padx=(0, 8))
        self.btn_stop = ttk.Button(act, text="停止", width=10,
                                   command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=1)
        self.btn_open = ttk.Button(act, text="打开输出目录",
                                   command=self._open_outdir, state="disabled")
        self.btn_open.grid(row=0, column=3, sticky="e")

        prog = tk.Frame(body, bg=CARD)
        prog.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        prog.columnconfigure(0, weight=1)
        self.pb = ttk.Progressbar(prog, variable=self.var_progress,
                                  maximum=100.0, mode="determinate",
                                  style="Accent.Horizontal.TProgressbar")
        self.pb.grid(row=0, column=0, sticky="ew")
        self.lbl_pct = tk.Label(prog, text="0%", bg=CARD, fg=ACCENT,
                                font=(FONT, 10, "bold"), width=5, anchor="e")
        self.lbl_pct.grid(row=0, column=1, padx=(10, 0))
        self.lbl_status = _label(prog, "", muted=True, anchor="w")
        self.lbl_status.configure(textvariable=self.var_status)
        self.lbl_status.grid(row=1, column=0, columnspan=2, sticky="ew",
                             pady=(6, 0))

        # ---- log ---------------------------------------------------------
        logcard = tk.Frame(self, bg=CARD, highlightbackground=BORDER,
                           highlightthickness=1)
        logcard.grid(row=row, column=0, sticky="nsew", pady=(0, 0))
        logcard.columnconfigure(0, weight=1)
        logcard.rowconfigure(1, weight=1)
        self.rowconfigure(row, weight=1)
        row += 1

        _label(logcard, "日志", muted=True, bold=True).grid(
            row=0, column=0, sticky="w", padx=14, pady=(9, 0))
        wrap = tk.Frame(logcard, bg=LOG_BG)
        wrap.grid(row=1, column=0, sticky="nsew", padx=12, pady=(7, 12))
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self.txt = tk.Text(wrap, height=9, wrap="none", state="normal",
                           background=LOG_BG, foreground=LOG_FG,
                           insertbackground=LOG_FG, relief="flat", bd=0,
                           font=(FONT_MONO, 9), padx=10, pady=8,
                           selectbackground="#264f78")
        self.txt.grid(row=0, column=0, sticky="nsew")
        # Read-only in effect: block typing/editing but avoid state toggling.
        self.txt.bind("<Key>", lambda e: "break")
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.txt.yview)
        vs.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=vs.set)

        self._sync_mode()

    # ---------------- mode switching ----------------

    def _sync_mode(self):
        """Enable, and show, only the input the selected mode actually uses."""
        mode = self.var_mode.get()

        # Clear every optional row first, then re-grid the one we need. This
        # keeps the card from reserving space for hidden fields.
        for w in (self.row_id, self.row_url, self.lbl_url_hint):
            w.grid_remove()

        if mode == "id":
            self.row_id.grid(row=1, column=0, sticky="ew", pady=(10, 0))
            self.ent_video_id.configure(state="normal")
        elif mode == "url":
            self.row_url.grid(row=1, column=0, sticky="ew", pady=(10, 0))
            self.lbl_url_hint.grid(row=2, column=0, sticky="w", pady=(5, 0))
            self.ent_url.configure(state="normal")

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_outdir.get() or os.getcwd())
        if d:
            self.var_outdir.set(d)

    def _selected_player(self) -> str | None:
        """Map the combobox choice to an executable path, or None for default."""
        name = self.var_player.get()
        if name == "系统默认播放器":
            return None
        for n, path in self.players:
            if n == name:
                return path
        return None

    def _preview_partial(self):
        """Open the .ts downloaded so far; it plays up to the current point."""
        path = self.partial_ts
        if not path or not os.path.exists(path):
            messagebox.showinfo(APP_TITLE, "还没有可预览的内容,请先开始下载。")
            return
        size = os.path.getsize(path)
        if size == 0:
            messagebox.showinfo(APP_TITLE, "第一个分片还没写完,稍等片刻再试。")
            return
        try:
            player.open_with(path, self._selected_player())
            self._append_log(f"预览已下载部分 ({human_size(size)}) —— "
                             "注意:这是正在增长的片段,只能看到已下载的部分。")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"无法启动播放器:\n{e}")

    def _open_outdir(self):
        d = self.var_outdir.get()
        if not os.path.isdir(d):
            messagebox.showinfo(APP_TITLE, "输出目录还不存在。")
            return
        target = self.last_result if self.last_result and os.path.isfile(self.last_result) else d
        try:
            if os.path.isfile(target):
                subprocess_open(target)
            else:
                os.startfile(d)  # noqa: S606 - Windows-only convenience
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"无法打开目录:\n{e}")

    # ---------------- logging ----------------

    def _append_log(self, line: str):
        """Append one line. Cheap enough to call in bursts.

        We keep the widget in its normal state and rely on the scrollback trim
        in _poll_queue to bound growth; toggling state per line was the
        expensive part.
        """
        ts = time.strftime("%H:%M:%S")
        self.txt.insert("end", f"[{ts}] {line}\n")
        self.txt.see("end")
        self._log_lines += 1
        # Batch-trim: only touch the widget when it has grown well past the
        # cap, and delete a whole block at once. Trimming on every tick (and
        # toggling state while doing it) was both slow and buggy.
        if self._log_lines > 400:
            try:
                self.txt.delete("1.0", "201.0")
                self._log_lines -= 200
            except Exception:
                pass

    # ---------------- run / stop ----------------

    def _collect_options(self):
        mode = self.var_mode.get()
        if mode == "id":
            vid = self.var_video_id.get().strip()
            if not vid:
                raise ValueError("请输入视频 ID,或切换到「随机视频」。")
            if not vid.isdigit():
                raise ValueError("视频 ID 应该是纯数字。")
        elif mode == "url":
            vid = None
            if not self.var_url.get().strip():
                raise ValueError("请输入 URL。")
        else:
            vid = None

        max_seg = self.var_max_seg.get().strip() or "0"
        if not max_seg.lstrip("-").isdigit():
            raise ValueError("最大分片数应该是整数(0 = 全部)。")

        try:
            workers = int(self.var_workers.get())
        except ValueError:
            workers = 8
        workers = max(1, min(workers, 64))

        outdir = self.var_outdir.get().strip()
        if not outdir:
            raise ValueError("请填写输出目录。")
        outname = self.var_outname.get().strip() or "output"
        if any(c in outname for c in '\\/:*?"<>|'):
            raise ValueError("文件名里不能包含 \\ / : * ? \" < > | 这些字符。")

        return dict(
            url=self.var_url.get().strip() if mode == "url" else None,
            video_id=vid,
            quality=self.var_quality.get(),
            max_segments=int(max_seg),
            outdir=outdir,
            out_name=outname,
            keep_ts=self.var_keep_ts.get(),
            make_mp4=self.var_make_mp4.get(),
            auto_play=self.var_autoplay.get(),
            player_path=self._selected_player(),
            workers=workers,
        )

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            opts = self._collect_options()
        except ValueError as e:
            messagebox.showwarning(APP_TITLE, str(e))
            return

        self.cancel_event.clear()
        self.last_result = None
        self.partial_ts = None
        self._preview_ready = False
        self.pending_finish = False
        self.var_progress.set(0.0)
        self.var_status.set("准备中...")
        _set_enabled(self.btn_start, False, primary=True)
        self.btn_stop.configure(state="normal")
        self.btn_open.configure(state="disabled")
        self.btn_preview.configure(state="disabled")

        self._append_log("=" * 46)
        self._append_log("开始新任务")
        backend = core.aes_backend()
        self._append_log(f"解密后端: {backend}"
                         + ("（C 加速，正常）" if backend != "python"
                            else "（纯 Python 回退，会非常慢！）"))
        self.start_time = time.time()

        self.worker = threading.Thread(target=self._run_worker,
                                       args=(opts,), daemon=True)
        self.worker.start()

    def _stop(self):
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.btn_stop.configure(state="disabled")
            self.var_status.set("正在停止...")
            self._append_log("已请求停止 —— 等当前分片收完就会退出。")

    def _run_worker(self, opts):
        try:
            final = core.run(progress=self.progress,
                             should_stop=self.cancel_event.is_set,
                             **opts)
            self.q.put(("done", final))
        except core.DownloadCancelled:
            self.q.put(("cancelled", None))
        except Exception as e:
            detail = traceback.format_exc()
            self.q.put(("error", (str(e), detail)))

    # ---------------- queue pump ----------------

    def _poll_queue(self):
        """Drain worker events, but cheaply.

        With N parallel downloads the worker can enqueue segment events far
        faster than the UI should repaint. So we (a) cap how many events we
        handle per tick, and (b) collapse segment/progress spam down to the
        latest state instead of setting Tk variables once per event.
        """
        LOG_CAP = 200          # kept for reference; trimming is incremental
        handled = 0
        latest_segment = None  # only the newest one matters for the display

        try:
            while handled < 300:
                try:
                    kind, payload = self.q.get_nowait()
                except queue.Empty:
                    break
                handled += 1

                if kind == "log":
                    self._append_log(payload)
                elif kind == "stage":
                    self.var_status.set(f"阶段: {STAGE_LABELS.get(payload, payload)}")
                elif kind == "segment":
                    # Coalesce: keep only the newest, apply after the loop.
                    latest_segment = payload
                elif kind == "partial":
                    path, done, total = payload
                    self.partial_ts = path
                    if not self._preview_ready and os.path.exists(path) \
                            and os.path.getsize(path) > 0:
                        self._preview_ready = True
                        self.btn_preview.configure(state="normal")
                elif kind == "finish":
                    self.pending_finish = True
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "cancelled":
                    self._on_cancelled()
                elif kind == "error":
                    self._on_error(*payload)
        except queue.Empty:
            pass
        finally:
            # Apply the coalesced progress exactly once per tick.
            if latest_segment is not None:
                done, total, name, enc, dec = latest_segment
                self.var_progress.set(done * 100.0 / max(total, 1))
                self.var_status.set(f"下载分片 {done}/{total}")
                # Short label only — the segment name is a huge signed URL.
                if done % 40 == 0 or done == total:
                    self._append_log(f"[+] {done}/{total} ({enc} -> {dec} bytes)")
            if self.pending_finish:
                self.pending_finish = False
                self.var_progress.set(100.0)
                self.var_status.set("完成")

            # Keep the percentage readout in step with the bar.
            try:
                self.lbl_pct.configure(text=f"{int(self.var_progress.get())}%")
            except Exception:
                pass

        # Trim the log so a long run cannot slow every redraw down.
        # (Handled incrementally in _append_log; nothing to do per tick.)

        self.master.after(150, self._poll_queue)

    # ---------------- terminal states ----------------

    def _reset_buttons(self):
        _set_enabled(self.btn_start, True, primary=True)
        self.btn_stop.configure(state="disabled")
        # A finished run no longer has a "growing file" to preview.
        self.btn_preview.configure(state="disabled")

    def _on_done(self, path):
        self._reset_buttons()
        self.btn_open.configure(state="normal")
        self.last_result = path
        elapsed = time.time() - self.start_time
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        self.var_status.set(f"完成 · {human_size(size)} · 用时 {human_time(elapsed)}")
        self._append_log(f"完成: {path}  ({human_size(size)}, 用时 {human_time(elapsed)})")
        messagebox.showinfo(APP_TITLE, f"下载完成!\n\n{path}\n\n{human_size(size)}")

    def _on_cancelled(self):
        self._reset_buttons()
        self.var_status.set("已停止")
        self._append_log("任务已被用户停止。")
        messagebox.showinfo(APP_TITLE, "任务已停止。已下载的部分保留在输出目录里。")

    def _on_error(self, msg, detail):
        self._reset_buttons()
        self.var_status.set("出错")
        self._append_log(f"[错误] {msg}")
        for line in (detail or "").strip().splitlines():
            self._append_log("    " + line)
        messagebox.showerror(APP_TITLE, f"出错了:\n\n{msg}")


STAGE_LABELS = {
    "login": "登录",
    "list": "获取列表",
    "preview": "获取预览",
    "playlist": "拉取播放列表",
    "download": "下载分片",
    "remux": "转换为 MP4",
    "play": "启动播放器",
}


def subprocess_open(path: str) -> None:
    """Reveal a file in Explorer with it selected."""
    import subprocess
    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])


def human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.2f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{n} B"


def human_time(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec} 秒"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} 分 {s} 秒"
    h, m = divmod(m, 60)
    return f"{h} 小时 {m} 分"


def main():
    # Hidden self-test: `VideosHubDownloader.exe --selftest [segments] [workers]`
    # Runs a download with a UI-thread heartbeat and prints freeze statistics.
    # Used to measure responsiveness inside the frozen environment.
    if "--selftest" in sys.argv:
        _selftest()
        return

    root = tk.Tk()
    root.title(f"{APP_TITLE} v{VERSION}")
    root.configure(bg=BG)
    _setup_style(root)
    root.geometry("840x900")
    root.minsize(760, 640)
    App(root)
    root.mainloop()


def _selftest():
    """UI-thread heartbeat while a real download runs. Writes a report file.

    The report is written incrementally so that even a killed run leaves data
    behind. Each stall is tagged with the status text that was on screen at
    the time, which tells us *which phase* froze the UI.
    """
    args = [a for a in sys.argv[1:] if a != "--selftest" and not a.startswith("--")]
    max_seg = int(args[0]) if args else 150
    workers = args[1] if len(args) > 1 else "8"
    base = (os.path.dirname(sys.executable)
            if getattr(sys, "frozen", False) else os.getcwd())
    report = os.path.join(base, "_selftest_report.txt")

    root = tk.Tk()
    root.configure(bg=BG)
    _setup_style(root)
    # Visible window: repaint cost only shows up when Tk actually has to
    # redraw. Withdrawing the window would hide the very problem we measure.
    if "--hidden" in sys.argv:
        root.withdraw()
    else:
        root.geometry("840x900")
        root.deiconify()
    app = App(root)
    app.var_mode.set("random")
    app.var_quality.set("1080p")
    app.var_max_seg.set(str(max_seg))
    app.var_workers.set(workers)
    app.var_outdir.set(os.path.join(base, "_selftest_out"))

    samples = []          # (gap_ms, status_text_at_that_moment)
    last = [0.0]
    stop = threading.Event()

    def write_report(final=False):
        gaps = sorted(s[0] for s in samples)
        n = len(gaps)
        out = ["selftest: segments=%s workers=%s frozen=%s visible=%s" %
               (max_seg, workers, getattr(sys, "frozen", False),
                "--hidden" not in sys.argv),
               "aes_backend=%s" % core.aes_backend(),
               "samples=%d" % n]
        if n:
            out += [
                "median=%.1fms" % gaps[n // 2],
                "p90=%.1fms" % gaps[min(n - 1, int(n * 0.90))],
                "p99=%.1fms" % gaps[min(n - 1, int(n * 0.99))],
                "max=%.1fms" % gaps[-1],
                "over150=%d" % len([g for g in gaps if g > 150]),
                "over500=%d" % len([g for g in gaps if g > 500]),
                "over2000=%d" % len([g for g in gaps if g > 2000]),
            ]
            worst = sorted(samples, key=lambda s: -s[0])[:8]
            out.append("--- worst stalls (gap_ms @ status) ---")
            for g, st in worst:
                out.append("  %8.0f ms  @ %s" % (g, st))
        text = "\n".join(out)
        try:
            with open(report, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass
        if final:
            print(text)

    def heartbeat():
        now = time.perf_counter()
        if last[0]:
            gap = (now - last[0]) * 1000.0
            try:
                st = app.var_status.get()
            except Exception:
                st = "?"
            samples.append((gap, st))
        last[0] = now
        if not stop.is_set():
            root.after(50, heartbeat)

    def watchdog():
        start = time.time()
        while time.time() - start < 420:
            w = app.worker
            if w is None or not w.is_alive():
                time.sleep(0.3)
                if app.worker is None or not app.worker.is_alive():
                    break
            # Keep a rolling report on disk so an abort still leaves evidence.
            if int(time.time() - start) % 5 == 0:
                write_report(final=False)
            time.sleep(0.1)
        stop.set()
        root.after(500, root.destroy)

    heartbeat()
    app._start()
    threading.Thread(target=watchdog, daemon=True).start()
    root.mainloop()
    write_report(final=True)


if __name__ == "__main__":
    main()

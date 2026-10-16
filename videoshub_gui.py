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

APP_TITLE = "VideosHub 视频下载器"
VERSION = "1.0"


def _resource_dir() -> str:
    """Where bundled read-only assets live (works both frozen and from source)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


class QueueProgress(core.Progress):
    """Marshals worker-thread events onto the UI thread via a queue."""

    def __init__(self, q: "queue.Queue"):
        self.q = q

    def log(self, message: str) -> None:
        self.q.put(("log", message))

    def stage(self, name: str) -> None:
        self.q.put(("stage", name))

    def segment(self, done, total, name, enc, dec) -> None:
        self.q.put(("segment", (done, total, name, enc, dec)))

    def finish(self, path: str) -> None:
        self.q.put(("finish", path))


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
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
        self.var_outdir = tk.StringVar(value=os.path.join(os.getcwd(), "videoshub_out"))
        self.var_outname = tk.StringVar(value="output")
        self.var_keep_ts = tk.BooleanVar(value=True)
        self.var_make_mp4 = tk.BooleanVar(value=True)
        self.var_url = tk.StringVar()
        self.var_status = tk.StringVar(value="就绪")
        self.var_progress = tk.DoubleVar(value=0.0)

    # ---------------- layout ----------------

    def _build_widgets(self):
        row = 0

        # --- mode -------------------------------------------------------
        box_mode = ttk.LabelFrame(self, text=" 下载方式 ", padding=8)
        box_mode.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        box_mode.columnconfigure(1, weight=1)
        row += 1

        ttk.Radiobutton(box_mode, text="随机视频", value="random",
                        variable=self.var_mode,
                        command=self._sync_mode).grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Radiobutton(box_mode, text="指定视频 ID", value="id",
                        variable=self.var_mode,
                        command=self._sync_mode).grid(row=0, column=1, sticky="w", padx=(0, 12))
        ttk.Radiobutton(box_mode, text="直接输入 URL", value="url",
                        variable=self.var_mode,
                        command=self._sync_mode).grid(row=0, column=2, sticky="w")

        self.ent_video_id = ttk.Entry(box_mode, textvariable=self.var_video_id, width=24)
        self.ent_video_id.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(box_mode, text="例如 32238", foreground="#888").grid(
            row=1, column=2, sticky="w", padx=(6, 0))

        self.ent_url = ttk.Entry(box_mode, textvariable=self.var_url)
        self.ent_url.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(box_mode,
                  text="支持 .m3u8 完整播放列表(免登录直下)或含 /p2/preview/ 的预览地址(自动签名)",
                  foreground="#888").grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # --- options ----------------------------------------------------
        box_opt = ttk.LabelFrame(self, text=" 参数 ", padding=8)
        box_opt.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        box_opt.columnconfigure(0, weight=1)
        row += 1

        grid = ttk.Frame(box_opt)
        grid.grid(row=0, column=0, sticky="ew")
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(4, weight=1)

        ttk.Label(grid, text="清晰度:").grid(row=0, column=0, sticky="w")
        self.cmb_quality = ttk.Combobox(grid, textvariable=self.var_quality,
                                        values=["480p", "720p", "1080p"],
                                        state="readonly", width=10)
        self.cmb_quality.grid(row=0, column=1, sticky="w", padx=(4, 16))

        ttk.Label(grid, text="最大分片数:").grid(row=0, column=2, sticky="w")
        self.ent_maxseg = ttk.Entry(grid, textvariable=self.var_max_seg, width=10)
        self.ent_maxseg.grid(row=0, column=3, sticky="w", padx=(4, 16))
        ttk.Label(grid, text="0 = 全部", foreground="#888").grid(row=0, column=4, sticky="w")

        ttk.Label(grid, text="输出目录:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.ent_outdir = ttk.Entry(grid, textvariable=self.var_outdir)
        self.ent_outdir.grid(row=1, column=1, columnspan=3, sticky="ew",
                             padx=(4, 4), pady=(8, 0))
        ttk.Button(grid, text="浏览...", width=8,
                   command=self._choose_dir).grid(row=1, column=4, sticky="e", pady=(8, 0))

        ttk.Label(grid, text="文件名:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(grid, textvariable=self.var_outname, width=24).grid(
            row=2, column=1, sticky="w", padx=(4, 16), pady=(8, 0))

        checks = ttk.Frame(grid)
        checks.grid(row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Checkbutton(checks, text="自动转成 MP4", variable=self.var_make_mp4).grid(
            row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Checkbutton(checks, text="转换后保留中间 .ts 文件",
                        variable=self.var_keep_ts).grid(row=0, column=1, sticky="w")

        # --- actions ----------------------------------------------------
        box_act = ttk.Frame(self)
        box_act.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        box_act.columnconfigure(2, weight=1)
        row += 1

        self.btn_start = ttk.Button(box_act, text="开始下载", command=self._start)
        self.btn_start.grid(row=0, column=0, padx=(0, 6))
        self.btn_stop = ttk.Button(box_act, text="停止", command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(0, 6))
        self.btn_open = ttk.Button(box_act, text="打开输出目录",
                                   command=self._open_outdir, state="disabled")
        self.btn_open.grid(row=0, column=3, sticky="e")

        # --- progress ---------------------------------------------------
        box_prog = ttk.Frame(self)
        box_prog.grid(row=row, column=0, sticky="ew")
        box_prog.columnconfigure(0, weight=1)
        row += 1

        self.pb = ttk.Progressbar(box_prog, variable=self.var_progress,
                                  maximum=100.0, mode="determinate")
        self.pb.grid(row=0, column=0, sticky="ew")
        ttk.Label(box_prog, textvariable=self.var_status).grid(
            row=1, column=0, sticky="w", pady=(2, 6))

        # --- log --------------------------------------------------------
        box_log = ttk.LabelFrame(self, text=" 日志 ", padding=4)
        box_log.grid(row=row, column=0, sticky="nsew", pady=(8, 0))
        box_log.columnconfigure(0, weight=1)
        box_log.rowconfigure(0, weight=1)
        self.rowconfigure(row, weight=1)
        row += 1

        self.txt = tk.Text(box_log, height=14, wrap="none", state="disabled",
                           background="#1e1e1e", foreground="#d4d4d4",
                           insertbackground="#d4d4d4", relief="flat",
                           font=("Consolas", 9))
        self.txt.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(box_log, orient="vertical", command=self.txt.yview)
        vs.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=vs.set)

        self._sync_mode()

    # ---------------- mode switching ----------------

    def _sync_mode(self):
        mode = self.var_mode.get()
        self.ent_video_id.configure(state="normal" if mode == "id" else "disabled")
        self.ent_url.configure(state="normal" if mode == "url" else "disabled")

    def _choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.var_outdir.get() or os.getcwd())
        if d:
            self.var_outdir.set(d)

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
        ts = time.strftime("%H:%M:%S")
        self.txt.configure(state="normal")
        self.txt.insert("end", f"[{ts}] {line}\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

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
        self.var_progress.set(0.0)
        self.var_status.set("准备中...")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_open.configure(state="disabled")

        self._append_log("=" * 46)
        self._append_log("开始新任务")
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
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "stage":
                    self.var_status.set(f"阶段: {STAGE_LABELS.get(payload, payload)}")
                elif kind == "segment":
                    done, total, name, enc, dec = payload
                    self.var_progress.set(done * 100.0 / max(total, 1))
                    self.var_status.set(f"下载分片 {done}/{total}  {name}")
                    if done % 20 == 0 or done == total:
                        self._append_log(
                            f"[+] {done}/{total} {name} ({enc} -> {dec} bytes)")
                elif kind == "finish":
                    self.var_progress.set(100.0)
                    self.var_status.set("完成")
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "cancelled":
                    self._on_cancelled()
                elif kind == "error":
                    self._on_error(*payload)
        except queue.Empty:
            pass
        finally:
            self.master.after(120, self._poll_queue)

    # ---------------- terminal states ----------------

    def _reset_buttons(self):
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

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
    root = tk.Tk()
    root.title(f"{APP_TITLE} v{VERSION}")
    root.geometry("760x680")
    root.minsize(660, 580)
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

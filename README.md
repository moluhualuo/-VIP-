# VideosHub 视频下载器(桌面版)

tkinter 图形界面 + PyInstaller 打包的独立 exe。核心下载逻辑与界面分离,便于维护和二次开发。

## 文件说明

| 文件 | 作用 |
|---|---|
| `dist/VideosHubDownloader.exe` | **最终交付物**,双击即用,无需安装 Python |
| `videoshub_gui.py` | 图形界面(tkinter) |
| `videoshub_core.py` | 核心下载逻辑,与界面解耦,可单独 import 使用 |
| `videoshub_player.py` | 播放器探测与调用 |
| `videoshub_watch.py` | 最初的命令行版本,保留作参考 |
| `VideosHubDownloader.spec` | PyInstaller 打包配置 |
| `build_exe.bat` | 一键重新打包脚本 |

## 直接使用

双击 `dist\VideosHubDownloader.exe`。

### 三种下载方式

1. **随机视频** — 从列表第一页随机挑一个,最省事。
2. **指定视频 ID** — 填纯数字 ID,例如 `32238`。
3. **直接输入 URL** — 支持两种地址:
   - `.m3u8` 完整播放列表:**免登录直下**,原样使用。
   - 含 `/p2/preview/` 的预览地址:自动用内嵌 key 签名成完整列表。

### 参数

| 项 | 说明 |
|---|---|
| 清晰度 | `480p` / `720p` / `1080p` |
| **并发连接** | **1~32,默认 8。这是最大的提速开关**:分片并行下载,实测 8 并发约 **5 倍**提速。网络差或服务端限速时调小 |
| 最大分片数 | `0` = 全部。**建议先用 3 试水**,确认链路通再跑全片 |
| 输出目录 | 默认 exe 同级的 `videoshub_out` |
| 文件名 | 输出文件基础名(不含扩展名) |
| 自动转成 MP4 | 关掉则只留 `.ts` |
| 保留中间 .ts | 关掉则 MP4 写好后自动删除 `.ts`,省一半空间 |

> 并发下载仍是**按下标顺序写盘**的,所以 TS 流不会错乱 —— 实测串行与 8 并发产出的文件
> SHA256 完全一致。并发只是让网络请求同时进行,落盘顺序始终严格有序。

### 界面反馈

- **进度条 + 状态行** — 实时显示 `阶段 / 下载分片 123/748 / 文件名`
- **日志面板** — 每 20 个分片打一行摘要,出错时打印完整堆栈
- **停止按钮** — 当前分片收完后安全退出,已下载部分保留
- **打开输出目录** — 完成后直接在资源管理器里定位到文件

### 播放

- **下载完成后自动播放** — 勾选后,片子一下完就自动用播放器打开。
- **预览已下载部分** — 下载过程中随时点这个按钮,能立刻播放**已经下好的部分**
  (打开的是正在增长的 `.ts`,只能看到当前进度为止的内容)。
- **播放器选择** — 自动探测 VLC / mpv / MPC-HC / PotPlayer;没检测到就用系统默认播放器。

## 从源码运行

```bash
pip install av          # 可选：没有 ffmpeg 时用它转 MP4
python videoshub_gui.py
```

tkinter 是 Python 标准库,不需要额外安装。**注意需要带 tkinter 的 Python**
(Windows 官方安装包默认带;某些精简版/嵌入式发行版可能没有)。

## 重新打包 exe

```bash
pip install pyinstaller av
build_exe.bat
```

或手动:

```bash
python -m PyInstaller --noconfirm --clean --name "VideosHubDownloader" ^
  --onefile --windowed --collect-all av ^
  --hidden-import av --hidden-import videoshub_core videoshub_gui.py
```

产物在 `dist\VideosHubDownloader.exe`(约 39 MB)。

### 打包要点

- **`--collect-all av` 必须加**。PyAV 依赖一批 FFmpeg 原生 DLL,默认不会被自动收集,
  漏了的话 exe 能启动但一转 MP4 就报 `ImportError: DLL load failed`。
- **`--windowed`** 用于去掉黑色控制台窗口。
- **`--hidden-import videoshub_core`** 确保被 GUI 间接引用的核心模块被打进包里。
- 打包机的 Python 必须带 tkinter(`pyi_rth__tkinter` 运行时钩子依赖它)。

## MP4 转换策略

程序按 **ffmpeg → PyAV** 顺序自动降级:

1. 若 PATH 上有 `ffmpeg`,优先用它(`-c copy`,最快)。
2. 否则用内置的 PyAV(FFmpeg 的 Python 绑定,已打进 exe)。

两种都是**只搬压缩包、不重编码**,所以速度极快、画质无损。也就是说
**用户机器上不装 ffmpeg 也能直接出 MP4**。

## 已知边界

- 不支持断点续传。中断后重跑会从第 1 个分片开始。
- 748 分片的长片串行下载约 13 分钟(受网速限制),这是主要耗时。
- 任务一次只能跑一个;运行中「开始下载」按钮会禁用。
- 打包的是 64 位 Windows exe,且未做代码签名,首次运行可能触发 SmartScreen 提示。

"""外部媒体工具的定位与调用。

ffmpeg 与 SILK 解码器按优先级解析，仓库不再捆绑 ffmpeg 二进制：
1. 环境变量 ``FFMPEG_BINARY`` / ``SILK_DECODER_BINARY``
2. ``PATH`` 中的同名可执行文件
3. Python 包自带的二进制（``imageio-ffmpeg`` / ``pilk``）
4. ``lib/`` 下历史遗留的 Windows 二进制（可选回退）

所有命令以参数列表形式执行，不经过 shell，因此路径含空格/中文也不会出错。
"""

import functools
import os
import shutil
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LEGACY_FFMPEG = os.path.join(PROJECT_ROOT, "lib", "ffmpeg-lgpl", "ffmpeg.exe")
_LEGACY_SILK_DECODER = os.path.join(PROJECT_ROOT, "lib", "silk-decoder", "silk_v3_decoder.exe")

# SILK 语音统一按 44.1kHz 解码，保持与历史导出一致
SILK_SAMPLE_RATE = 44100


class MediaToolError(RuntimeError):
    """外部媒体工具缺失或执行失败。"""


def _env_binary(name):
    path = os.environ.get(name)
    if not path:
        return None
    if not os.path.isfile(path):
        raise MediaToolError(f"{name} 指向的文件不存在：{path}")
    return path


@functools.lru_cache(maxsize=None)
def ffmpeg_path():
    """返回可用的 ffmpeg 路径。"""
    path = _env_binary("FFMPEG_BINARY")
    if path:
        return path

    path = shutil.which("ffmpeg")
    if path:
        return path

    try:
        import imageio_ffmpeg
    except ImportError:
        pass
    else:
        return imageio_ffmpeg.get_ffmpeg_exe()

    if os.path.isfile(_LEGACY_FFMPEG):
        return _LEGACY_FFMPEG

    raise MediaToolError(
        "找不到 ffmpeg：请安装 ffmpeg 并加入 PATH，或设置 FFMPEG_BINARY，"
        "或安装 imageio-ffmpeg（uv add imageio-ffmpeg）"
    )


@functools.lru_cache(maxsize=None)
def silk_decoder_path():
    """返回可用的 silk_v3_decoder 路径（仅在没有 pilk 时使用）。"""
    path = _env_binary("SILK_DECODER_BINARY")
    if path:
        return path

    path = shutil.which("silk_v3_decoder")
    if path:
        return path

    if os.path.isfile(_LEGACY_SILK_DECODER):
        return _LEGACY_SILK_DECODER

    raise MediaToolError(
        "找不到 SILK 解码器：请安装 pilk（uv add pilk），或设置 SILK_DECODER_BINARY，"
        "或把 silk_v3_decoder 加入 PATH"
    )


def _pilk():
    try:
        import pilk
    except ImportError:
        return None
    return pilk


def run(cmd, timeout=30):
    """执行外部命令，失败时抛出带 stderr 摘要的 MediaToolError。"""
    try:
        subprocess.run(
            cmd,
            check=True,
            timeout=timeout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise MediaToolError(f"找不到可执行文件：{cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(f"命令超时（{timeout}s）：{' '.join(cmd)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        message = "命令执行失败：" + " ".join(cmd)
        if detail:
            message += "\n" + "\n".join(detail[-3:])
        raise MediaToolError(message) from exc


def video_thumbnail(video_path, out_path, timeout=10):
    """截取视频首帧作为缩略图。"""
    run(
        [ffmpeg_path(), "-y", "-ss", "0", "-i", video_path, "-frames:v", "1", out_path],
        timeout=timeout,
    )
    if not os.path.isfile(out_path):
        raise MediaToolError(f"ffmpeg 未生成缩略图：{out_path}")
    return out_path


def audio_to_mp3(src, dst, timeout=30):
    """把 ffmpeg 能直接识别的音频（如 AMR）转成 mp3。"""
    run([ffmpeg_path(), "-y", "-i", src, dst], timeout=timeout)
    if not os.path.isfile(dst):
        raise MediaToolError(f"ffmpeg 未生成音频：{dst}")
    return dst


def pcm_to_mp3(pcm_path, dst, sample_rate=SILK_SAMPLE_RATE, timeout=30):
    """把 16bit 单声道 PCM 转成 mp3。"""
    run(
        [
            ffmpeg_path(),
            "-y",
            "-f", "s16le",
            "-ar", str(sample_rate),
            "-ac", "1",
            "-i", pcm_path,
            dst,
        ],
        timeout=timeout,
    )
    if not os.path.isfile(dst):
        raise MediaToolError(f"ffmpeg 未生成音频：{dst}")
    return dst


def silk_to_pcm(silk_path, pcm_path, sample_rate=SILK_SAMPLE_RATE):
    """解码 SILK 语音为 PCM，优先用 pilk，其次调用外部解码器。"""
    pilk = _pilk()
    if pilk is not None:
        try:
            pilk.decode(silk_path, pcm_path, pcm_rate=sample_rate)
        except Exception as exc:
            raise MediaToolError(f"pilk 解码 SILK 失败：{exc}") from exc
        if not os.path.isfile(pcm_path):
            raise MediaToolError(f"pilk 未生成 PCM：{pcm_path}")
        return pcm_path

    run([silk_decoder_path(), silk_path, pcm_path, "-Fs_API", str(sample_rate)])
    if not os.path.isfile(pcm_path):
        raise MediaToolError(f"silk_v3_decoder 未生成 PCM：{pcm_path}")
    return pcm_path

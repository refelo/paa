"""Local optional media tools extracted from the verified course workflow.

No cloud dependencies, model downloads, or semantic-completion claims.
"""

from pathlib import Path
import re
import shutil
import time
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .automation import digest_file
from .cards import fingerprint, read, save


def transcribe_cached(path, directory, *, model="turbo", device="cuda", language="zh"):
    """Twenty-minute chunks survive interruption; successful chunks are reused."""
    from faster_whisper import WhisperModel

    recipe = fingerprint([model, device, language, "course-asr-v4"])[:16]
    chunks = directory / "asr-chunks" / recipe
    chunks.mkdir(parents=True, exist_ok=True)
    recognizer = None
    repair_model = None
    cue_rows, flags, repairs = [], [], []
    chunk_count = 0
    for index, (offset, audio) in enumerate(windows(str(path))):
        chunk = chunks / f"{index:06}.json"
        chunk_count += 1
        if chunk.exists():
            value = read(chunk)
        else:
            if recognizer is None:
                recognizer = WhisperModel(
                    model,
                    device=device,
                    compute_type="int8_float16" if device == "cuda" else "int8",
                    cpu_threads=4,
                    num_workers=1,
                    local_files_only=True,
                )
            segments, _ = recognizer.transcribe(
                audio,
                language=language,
                beam_size=5,
                vad_filter=True,
                word_timestamps=True,
                vad_parameters={"min_silence_duration_ms": 700},
                condition_on_previous_text=False,
            )
            value = {"cues": [], "flags": [], "repairs": []}
            for segment in segments:
                selected = segment
                reason = broken_text(segment.text)
                if reason:
                    replacement = None
                    try:
                        if repair_model is None:
                            repair_model = WhisperModel(
                                "small",
                                device="cpu",
                                compute_type="int8",
                                cpu_threads=4,
                                num_workers=1,
                                local_files_only=True,
                            )
                        replacement = repair_segment(
                            segment, audio, repair_model, language
                        )
                    except (OSError, RuntimeError, ValueError) as error:
                        value["flags"].append(
                            {
                                "start": offset + segment.start,
                                "repair_error": str(error),
                            }
                        )
                    value["repairs"].append(
                        {
                            "start": offset + segment.start,
                            "reason": reason,
                            "repaired": replacement is not None,
                        }
                    )
                    selected = replacement or segment
                for start, end, text in cues(selected):
                    if text:
                        value["cues"].append([offset + start, offset + end, text])
                if segment.avg_logprob < -1 or segment.compression_ratio > 2.4:
                    value["flags"].append(
                        {
                            "start": offset + segment.start,
                            "reason": "recognition_uncertain",
                        }
                    )
            save(chunk, value)
        cue_rows.extend(value["cues"])
        flags.extend(value["flags"])
        repairs.extend(value["repairs"])
    output = directory / "source.srt"
    temporary = output.with_suffix(".partial.srt")
    last_end = 0.0
    with temporary.open("w", encoding="utf-8") as stream:
        for number, (start, end, text) in enumerate(cue_rows, 1):
            start = max(last_end, start)
            end = max(start + 0.01, end)
            stream.write(f"{number}\n{stamp(start)} --> {stamp(end)}\n{text}\n\n")
            last_end = end
    temporary.replace(output)
    return {
        "srt_path": str(output),
        "srt_sha256": digest_file(output),
        "cues": len(cue_rows),
        "asr_chunks": chunk_count,
        "recognition_flags": flags,
        "quality_repairs": repairs,
        "asr_state": "complete",
        "full_text_read": False,
        "visual_analysis_done": False,
    }


def sample_frames(path, directory, *, step=90, seconds=None, width=1600):
    import av
    if step <= 0:
        raise ValueError("抽帧间隔须为正数。")
    details = seconds is not None
    target = directory / ("details" if details else "overview")
    target.mkdir(parents=True, exist_ok=True)
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = (
        ImageFont.truetype(str(font_path), 16)
        if font_path.exists()
        else ImageFont.load_default()
    )
    records, sheets, pending = [], [], []
    with av.open(str(path)) as container:
        if not container.streams.video:
            return {
                "sheets": [],
                "details": [],
                "reason": "no_video_stream",
                "model_has_viewed": False,
            }
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        duration = (
            float(stream.duration * stream.time_base)
            if stream.duration
            else float(container.duration / av.time_base)
        )
        times = sorted(
            set(
                seconds
                if details
                else [
                    min(2, duration / 4),
                    max(0, duration - 0.1),
                    *np.arange(0, duration, step),
                ]
            )
        )
        if any(t < 0 or t >= duration for t in times):
            raise ValueError("详情时间超出视频范围。")
        for index, second in enumerate(times):
            image, actual = frame_at(
                container, float(second), width if details else 640
            )
            if details:
                destination = target / f"{second:012.3f}.jpg"
                image.save(destination, quality=92)
                records.append(
                    {
                        "requested": float(second),
                        "actual": actual,
                        "path": str(destination),
                    }
                )
            else:
                pending.append((float(second), actual, image))
                if len(pending) == 6 or index == len(times) - 1:
                    height = max(im.height for _, _, im in pending) + 26
                    sheet = Image.new("RGB", (1280, height * 3), "#181818")
                    draw = ImageDraw.Draw(sheet)
                    for n, (requested, real, im) in enumerate(pending):
                        x, y = (n % 2) * 640, (n // 2) * height
                        sheet.paste(im, (x, y + 26))
                        draw.text(
                            (x + 5, y + 3),
                            f"{requested:.1f}s / actual {real:.2f}s",
                            font=font,
                            fill="white",
                        )
                        records.append({"requested": requested, "actual": real})
                    destination = target / f"{len(sheets):05}.jpg"
                    sheet.save(destination, quality=88)
                    sheets.append(str(destination))
                    pending.clear()
    return {
        "sheets": sheets,
        "details": records if details else [],
        "samples": records if not details else [],
        "model_has_viewed": False,
        "notice": "这些是采样画面，不代表完整视频观看。",
    }


def prepare_media(
    source, directory, digest, *, model="turbo", device="cuda", frame_step=90
):
    source, directory = Path(source).resolve(), Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    recipe = {
        "source_sha256": digest,
        "asr": "course-asr-v4",
        "model": model,
        "device": device,
        "frames": "course-frames-v1",
        "step": frame_step,
    }
    receipt_path = directory / "media.json"
    if receipt_path.exists():
        receipt = read(receipt_path)
        if (
            receipt.get("recipe") == recipe
            and Path(receipt["srt_path"]).is_file()
            and digest_file(receipt["srt_path"]) == receipt["srt_sha256"]
        ):
            if Path(receipt["frames"]).is_file():
                return {**receipt, "reused": True}
    if digest_file(source) != digest:
        raise ValueError("媒体准备前原文件已改变。")
    cache = directory / ("input" + source.suffix.lower())
    if not cache.exists() or digest_file(cache) != digest:
        temporary = cache.with_suffix(cache.suffix + ".partial")
        shutil.copyfile(source, temporary)
        if digest_file(temporary) != digest or digest_file(source) != digest:
            raise ValueError("媒体复制过程中源文件改变。")
        temporary.replace(cache)
    started = time.monotonic()
    asr_path = directory / "asr.json"
    if source.suffix.lower() == ".srt":
        target = directory / "source.srt"
        shutil.copyfile(cache, target)
        result = {
            "srt_path": str(target),
            "srt_sha256": digest_file(target),
            "asr_state": "provided_srt",
        }
        frames = {
            "sheets": [],
            "details": [],
            "model_has_viewed": False,
            "reason": "subtitle_only",
        }
    else:
        import av
        with av.open(str(cache)) as container:
            audio = bool(container.streams.audio)
        if not audio:
            target = directory / "source.srt"
            target.write_text("", encoding="utf-8")
            result = {
                "srt_path": str(target),
                "srt_sha256": digest_file(target),
                "asr_state": "empty",
                "reason": "no_audio_stream",
            }
        elif asr_path.exists() and read(asr_path).get("recipe") == recipe:
            result = read(asr_path)
            if digest_file(result["srt_path"]) != result["srt_sha256"]:
                raise ValueError("已有字幕缓存改变，不能无声覆盖。")
        else:
            result = transcribe_cached(cache, directory, model=model, device=device)
            save(asr_path, {**result, "recipe": recipe})
        frames = sample_frames(cache, directory, step=frame_step)
    if digest_file(source) != digest:
        raise ValueError("处理期间原文件改变；未发布准备结果。")
    frames_path = directory / "frames.json"
    save(frames_path, frames)
    receipt = {
        **result,
        "recipe": recipe,
        "frames": str(frames_path),
        "prepared_at": time.time(),
        "seconds": round(time.monotonic() - started, 3),
        "reused": False,
        "full_text_read": False,
        "visual_analysis_done": False,
    }
    save(receipt_path, receipt)
    return receipt


def stamp(seconds):
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def cues(segment):
    """Split only at word-aligned times, preserving every recognized word."""
    if not segment.words:
        yield segment.start, segment.end, segment.text.strip()
        return
    pending = []
    for word in segment.words:
        pending.append(word)
        text = "".join(w.word for w in pending).strip()
        if (
            len(text) >= 36
            or word.end - pending[0].start >= 7
            or (
                len(text) >= 8
                and text.endswith(("。", "？", "！", "?", "!", "，", ","))
            )
        ):
            yield pending[0].start, pending[-1].end, text
            pending = []
    if pending:
        yield (
            pending[0].start,
            pending[-1].end,
            "".join(w.word for w in pending).strip(),
        )


def broken_text(text):
    compact = re.sub(r"[\s，,。.!！?？、：:；;…—\-·]+", "", text)
    if not compact and len(text.strip()) >= 4:
        return "punctuation_only"
    repeated = re.search(r"(.)\1{8,}", compact)
    if repeated and len(repeated.group()) >= max(12, len(compact) * 0.55):
        return "repeated_character"
    repeated = re.search(r"(.{2,12})\1{4,}", compact)
    if repeated and len(repeated.group()) >= max(16, len(compact) * 0.55):
        return "repeated_phrase"
    return None


def repair_segment(segment, audio, model, language):
    start = max(0, segment.start - 2)
    end = min(len(audio) / 16000, segment.end + 2)
    segments, _ = model.transcribe(
        audio[int(start * 16000) : int(end * 16000)],
        language=language,
        beam_size=5,
        vad_filter=False,
        condition_on_previous_text=False,
        word_timestamps=True,
    )
    words = []
    for item in segments:
        for word in item.words or []:
            midpoint = start + (word.start + word.end) / 2
            if segment.start <= midpoint <= segment.end:
                words.append(
                    SimpleNamespace(
                        word=word.word,
                        start=max(segment.start, start + word.start),
                        end=min(segment.end, start + word.end),
                    )
                )
    text = "".join(w.word for w in words).strip()
    if not text or broken_text(text):
        return None
    return SimpleNamespace(start=segment.start, end=segment.end, words=words, text=text)


def windows(path, seconds=1200, max_seconds=None):
    import av
    size = int(seconds * 16000)
    parts = []
    n = 0
    offset = 0.0
    with av.open(path) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for f in resampler.resample(frame):
                a = f.to_ndarray().flatten()
                parts.append(a)
                n += len(a)
                while n >= size:
                    joined = np.concatenate(parts)
                    take = joined[:size]
                    rest = joined[size:]
                    yield offset, take.astype(np.float32) / 32768
                    offset += seconds
                    parts = [rest] if len(rest) else []
                    n = len(rest)
                    if max_seconds is not None and offset >= max_seconds:
                        return
        for f in resampler.resample(None):
            a = f.to_ndarray().flatten()
            parts.append(a)
            n += len(a)
        if n:
            data = np.concatenate(parts)
            if max_seconds is not None:
                data = data[: max(0, int((max_seconds - offset) * 16000))]
            if len(data):
                yield offset, data.astype(np.float32) / 32768


def frame_at(container, second, width):
    stream = container.streams.video[0]
    container.seek(int(second / float(stream.time_base)), stream=stream, backward=True)
    last = None
    for frame in container.decode(stream):
        last = frame
        if frame.time is None or frame.time >= second:
            height = max(2, round(width * frame.height / frame.width))
            return frame.reformat(
                width=width, height=height, format="rgb24"
            ).to_image(), frame.time
    if last is not None:
        height = max(2, round(width * last.height / last.width))
        return last.reformat(width=width, height=height, format='rgb24').to_image(), last.time
    raise ValueError("No frame found")

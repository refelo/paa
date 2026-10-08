from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

from PIL import Image, ImageOps

PROJECT = Path(os.environ.get('PAA_WORKSPACE', Path(__file__).resolve().parent.parent)).resolve()
LOCAL = PROJECT / ".local"
MODEL_ID = "voyage-multimodal-3.5"
EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}


class SourceError(ValueError):
    pass


def contained(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise SourceError("路径超出已授权素材目录。")
    return resolved


def source_key(root: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(root.resolve())).encode()).hexdigest()


def read_settings(path: Path | None = None) -> dict:
    path = path or LOCAL / "workspace.json"
    if (LOCAL / 'install/pending.json').exists():
        raise SourceError('安装更新尚未完成；请先运行安装器 recover。')
    data = json.loads(path.read_text(encoding="utf-8"))
    root = Path(data['library']).resolve(strict=True) if data.get('library') else None
    if root is not None and (root.suffix.lower() != ".library" or not (root / "images").is_dir()):
        raise SourceError("配置的目录不是可读取的 Eagle 图库。")
    # Project output must never be written inside the source library.
    if root is not None and LOCAL.resolve().is_relative_to(root):
        raise SourceError("项目 .local 不能位于原素材库内。")
    data["library"] = root
    if data.get('embedding_model', MODEL_ID) != MODEL_ID:
        raise SourceError('本项目仅保留Voyage；旧本地模型已退出，不能使用旧索引。')
    for key in ("index_dir", "card_store"):
        if data.get(key):
            target = Path(data[key]).resolve(strict=(key == 'card_store'))
            if not target.is_relative_to(LOCAL.resolve()):
                raise SourceError(f"{key} 必须位于项目 .local 内。")
            if root is not None and target.is_relative_to(root):
                raise SourceError(f'{key} 不能写入素材库内。')
            data[key] = target
    return data


def item_context(root: Path, item_id: str) -> dict:
    """Return the current fields together, without classifying authorship."""
    _, metadata = item_source(root, item_id)
    fields = ("tags", "comments", "annotation", "star", "folders")
    values = {field: metadata.get(field) for field in fields}
    return {
        **values,
        "missing_fields": [field for field in fields if field not in metadata],
    }


def item_source(root: Path, item_id: str) -> tuple[Path, dict]:
    if not item_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in item_id):
        raise SourceError("无效的 Eagle 素材编号。")
    folder = contained(root / "images" / f"{item_id}.info", root)
    metadata_path = contained(folder / "metadata.json", root)
    data = json.loads(metadata_path.read_text(encoding="utf-8"))
    if data.get("id") != item_id or data.get("isDeleted"):
        raise SourceError("素材已删除或编号不匹配，请更新索引。")
    ext, name = str(data.get("ext", "")).lower(), str(data.get("name", ""))
    if ext not in EXTENSIONS:
        raise SourceError("仅处理 JPG、PNG、WebP 和 GIF 图片。")
    if not name or any(c in name for c in '/\\\x00:') or name in {".", ".."}:
        raise SourceError("素材文件名无效。")
    original = contained(folder / f"{name}.{ext}", root)
    if not original.is_file():
        raise SourceError("原图片不存在。")
    return original, data


def catalog(root: Path) -> tuple[list[str], dict[str, int]]:
    ids, skipped = [], {"unsupported": 0, "deleted_or_invalid": 0}
    for directory in (root / "images").iterdir():
        if not directory.name.endswith(".info"):
            continue
        item_id = directory.name.removesuffix(".info")
        try:
            p = contained(directory / "metadata.json", root)
            data = json.loads(p.read_text(encoding="utf-8"))
            if str(data.get("ext", "")).lower() not in EXTENSIONS:
                skipped["unsupported"] += 1
                continue
            item_source(root, item_id)
            ids.append(item_id)
        except (OSError, ValueError, TypeError):
            skipped["deleted_or_invalid"] += 1
    # Sampling by hash avoids taking only the earliest imports or one filename prefix.
    ids.sort(key=lambda value: hashlib.sha256(value.encode()).hexdigest())
    return ids, skipped


def asset_id(root: Path, item_id: str, digest: str) -> str:
    return f"eagle:{source_key(root)[:16]}:{item_id}:{digest[:16]}"


def legacy_asset_id(root: Path, item_id: str, digest: str) -> str:
    return hashlib.sha256(f"{source_key(root)}\0{item_id}\0{digest}".encode()).hexdigest()[:24]


def load_image(root: Path, item_id: str) -> tuple[dict, Image.Image]:
    original, _ = item_source(root, item_id)
    before = original.stat()
    payload = original.read_bytes()
    after = original.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise SourceError("读取时原图片发生变化，请重试。")
    digest = hashlib.sha256(payload).hexdigest()
    with Image.open(io.BytesIO(payload)) as raw:
        frame_count = getattr(raw, "n_frames", 1)
        image = ImageOps.exif_transpose(raw).convert("RGB")
    return {
        "asset_id": asset_id(root, item_id, digest),
        "item_id": item_id,
        "relative_path": original.relative_to(root).as_posix(),
        "sha256": digest,
        "width": image.width,
        "height": image.height,
        "frame_count": frame_count,
    }, image


def verify_record(root: Path, record: dict) -> Image.Image:
    current, image = load_image(root, record["item_id"])
    valid_ids = {current['asset_id'], legacy_asset_id(root, record['item_id'], current['sha256'])}
    if record['asset_id'] not in valid_ids:
        image.close()
        raise SourceError("图片已替换；旧编号不再有效。")
    for key in ("sha256", "relative_path"):
        if current[key] != record[key]:
            image.close()
            raise SourceError("图片已替换、移动或重命名；旧结果不能作为当前素材返回，请更新索引。")
    return image


def frame_previews(root: Path, record: dict, image: Image.Image, local: Path) -> list[dict]:
    count = record.get('frame_count', 1)
    indices = sorted({round((count - 1) * i / 3) for i in range(4)})
    # Filenames must be valid on Windows; the public ID contains colons.
    key = hashlib.sha256(record['asset_id'].encode()).hexdigest()[:24]
    if count == 1:
        return [{'frame_index': 0, 'preview_path': write_preview(image, key, local).as_posix()}]
    original, _ = item_source(root, record['item_id'])
    previews = []
    with Image.open(original) as animation:
        for index in indices:
            animation.seek(index)
            frame = animation.convert('RGB')
            try:
                path = write_preview(frame, f'{key}-{index}', local)
                previews.append({'frame_index': index, 'preview_path': path.as_posix()})
            finally:
                frame.close()
    return previews


def write_preview(image: Image.Image, asset_id: str, local: Path = LOCAL) -> Path:
    target = local / "previews" / f"{asset_id}.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    preview = image.copy()
    preview.thumbnail((768, 768), Image.Resampling.LANCZOS)
    # Re-encoding omits the original EXIF and other private metadata.
    buffer = io.BytesIO()
    preview.save(buffer, format="JPEG", quality=87)
    preview.close()
    target.write_bytes(buffer.getvalue())
    return target.resolve()

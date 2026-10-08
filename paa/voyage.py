"""One bounded Voyage client for image queries and knowledge-card indexing."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import io
import json
import math
from pathlib import Path
import time
import urllib.request

from PIL import Image, ImageOps

from .cards import CardError, fingerprint, normalize, read, save
from .credentials import load_key

VOYAGE_MODEL = 'voyage-multimodal-3.5'
RESERVE_PER_INPUT = 32000 * 0.12 / 1_000_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class VoyageEncoder:
    model = VOYAGE_MODEL

    def __init__(self, root: Path, *, allow_online=False, budget_usd=0.1, role='query'):
        if budget_usd is not None and (isinstance(budget_usd, bool)
                or not isinstance(budget_usd, (int, float))
                or not math.isfinite(budget_usd) or budget_usd < 0):
            raise CardError("Voyage预算必须非负；0只允许复用，明确授权不设上限时使用null。")
        self.root = Path(root)
        if role not in ('query', 'document'):
            raise CardError('无效的embedding角色。')
        self.role = role
        self.allow_online, self.budget = allow_online, budget_usd
        self.last = {}

    @contextmanager
    def lock(self):
        from .locking import file_lock
        with file_lock(self.root / '.api.lock', timeout=60):
            yield

    def __call__(self, texts, role):
        import numpy as np

        if not texts:
            raise CardError('文字输入不能为空。')
        operation = self.document_items if role == 'document' else self.embed
        return np.concatenate([operation([[{'type': 'text', 'text': t}] for t in texts[start:start + 8]], role)
                               for start in range(0, len(texts), 8)])

    def document_items(self, contents, role='document'):
        """Reuse per-document vectors after interruption or batch boundary changes."""
        import numpy as np
        self.validate_inputs(contents, role)
        values = [self.cached([item], role)[2] for item in contents]
        missing = [i for i, value in enumerate(values) if value is None]
        if missing:
            unique = {}
            for i in missing:
                unique.setdefault(fingerprint(contents[i]), i)
            encoded = self.embed([contents[i] for i in unique.values()], role)
            mapped = dict(zip(unique, encoded, strict=True))
            for i in missing:
                vector = mapped[fingerprint(contents[i])]
                key, path, _ = self.cached([contents[i]], role)
                save(path, {'fingerprint': key, 'model': self.model, 'vectors': [vector.tolist()]})
                values[i] = vector.reshape(1, -1)
        return np.concatenate(values)

    def encode(self, *, text=None, images=None):
        if text is not None and images is not None and len(text) != len(images):
            raise CardError("图文组合数量不一致。")
        contents = []
        for i in range(len(images) if images is not None else len(text or [])):
            parts = []
            if text is not None and text[i].strip():
                parts.append({'type': 'text', 'text': text[i].strip()})
            if images is not None:
                image = ImageOps.contain(images[i].convert('RGB'), (1568, 1568))
                buffer = io.BytesIO()
                image.save(buffer, format='JPEG', quality=90)
                image.close()
                parts.append({'type': 'image_base64', 'image_base64':
                              'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')})
            contents.append(parts)
        if len(contents) > 8:
            # Reuse the original small-batch cache when increasing image batch size.
            self.validate_inputs(contents, self.role)
            cached = [self.cached(contents[start:start + 8], self.role)[2]
                      for start in range(0, len(contents), 8)]
            if all(value is not None for value in cached):
                import numpy as np
                self.last = {'cached': True, 'seconds': 0}
                return np.concatenate(cached)
        return self.embed(contents, self.role)

    def validate_inputs(self, contents, role):
        image_batch = (8 < len(contents) <= 32 and all(len(parts) == 1 and
                       parts[0].get('type') == 'image_base64' for parts in contents))
        if role not in ('query', 'document') or not (1 <= len(contents) <= 8 or image_batch):
            raise CardError("Voyage接受1至8条输入，纯图片批次最多32条。")
        for parts in contents:
            if not parts or len(parts) > 2:
                raise CardError("每条输入仅支持文字、一张图片或图文组合。")
            for part in parts:
                if part.get('type') == 'text':
                    if not isinstance(part.get('text'), str) or not 0 < len(part['text'].encode()) <= 12000:
                        raise CardError("查询文字为空或过长。")
                elif part.get('type') == 'image_base64':
                    if not part.get('image_base64', '').startswith('data:image/jpeg;base64,'):
                        raise CardError("只接受已处理的JPEG图片。")
                    if len(part['image_base64']) > 8_000_000:
                        raise CardError("查询图片过大。")
                    if image_batch:
                        with Image.open(io.BytesIO(base64.b64decode(part['image_base64'].split(',', 1)[1]))) as image:
                            if max(image.size) > 1568:
                                raise CardError('大批图片须先缩放至1568像素以内。')
                else:
                    raise CardError("不支持此输入类型。")

    def cached(self, contents, role):
        key = fingerprint([self.model, role, contents])
        path = self.root / (key + '.json')
        if not path.exists():
            return key, path, None
        try:
            cached = read(path)
            if cached.get('fingerprint') != key or cached.get('model') != self.model:
                raise CardError('Voyage缓存身份不符。')
            values = normalize(cached['vectors'], len(contents))
            if values.shape[1] != 1024:
                raise CardError('Voyage缓存维度错误。')
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise CardError('Voyage缓存损坏或身份／维度不符。') from None
        return key, path, values

    def ledger(self):
        path = self.root / 'usage.json'
        try:
            ledger = read(path) if path.exists() else {'estimated_usd': 0, 'events': []}
            spent = ledger['estimated_usd']
            if (isinstance(spent, bool) or not isinstance(spent, (int, float))
                    or not math.isfinite(spent) or spent < 0 or not isinstance(ledger['events'], list)):
                raise ValueError('invalid ledger')
        except (OSError, ValueError, TypeError, KeyError):
            raise CardError('Voyage用量记录损坏；不得清账后继续。') from None
        return ledger

    def preflight(self, texts, role):
        """Read-only, whole-run feasibility check; never reads a credential or sends."""
        if not texts or self.model != VOYAGE_MODEL:
            raise CardError('卡片输入为空或模型不支持。')
        missing, cached_batches = {}, 0
        for start in range(0, len(texts), 8):
            contents = [[{'type': 'text', 'text': t}] for t in texts[start:start + 8]]
            self.validate_inputs(contents, role)
            key, _, values = self.cached(contents, role)
            if values is None:
                for item in contents:
                    item_key, _, item_value = self.cached([item], role)
                    if item_value is None:
                        missing[item_key] = 1
            else:
                cached_batches += 1
        reserved = sum(missing.values()) * RESERVE_PER_INPUT
        spent = self.ledger()['estimated_usd']
        if self.budget is not None and spent + reserved > self.budget:
            raise CardError('整轮卡片重建超过本运行目录累计API预算；尚未发送请求。')
        if missing and not self.allow_online:
            raise CardError('卡片重建存在未缓存批次；需在授权范围显式开启online。')
        return {'inputs': len(texts), 'cached_batches': cached_batches, 'new_requests': (len(missing) + 7) // 8,
                'additional_reserved_usd': reserved, 'cumulative_reserved_usd': spent + reserved,
                'budget_usd': self.budget}

    def embed(self, contents, role='query'):
        self.validate_inputs(contents, role)
        with self.lock():
            key, path, values = self.cached(contents, role)
            if values is not None:
                self.last = {'cached': True, 'seconds': 0}
                return values
            if not self.allow_online:
                raise CardError("该查询没有Voyage缓存。授权范围内可设置allow_online；metadata文字路线无需API。")
            ledger_path = self.root / 'usage.json'
            ledger = self.ledger()
            # Full model context at conservative text rate covers bounded inputs.
            reserve = len(contents) * RESERVE_PER_INPUT
            if self.budget is not None and ledger['estimated_usd'] + reserve > self.budget:
                raise CardError("已达到本运行目录的保守累计API预算。")
            key_value = load_key()
            event = {'fingerprint': key, 'status': 'reserved', 'reserved_usd': reserve}
            ledger['events'].append(event)
            ledger['estimated_usd'] += reserve
            save(ledger_path, ledger)
            payload = {'model': self.model, 'input_type': role, 'truncation': False,
                       'inputs': [{'content': parts} for parts in contents]}
            request = urllib.request.Request('https://api.voyageai.com/v1/multimodalembeddings',
                                            data=json.dumps(payload).encode(),
                                            headers={'Content-Type': 'application/json',
                                                     'Authorization': 'Bearer ' + key_value})
            started = time.perf_counter()
            try:
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=120 if len(contents) > 8 else 45) as response:
                    body = json.load(response)
                rows = sorted(body['data'], key=lambda row: row['index'])
                if [r['index'] for r in rows] != list(range(len(contents))):
                    raise CardError("Voyage结果编号不完整。")
                values = normalize([r['embedding'] for r in rows], len(contents))
                if values.shape[1] != 1024:
                    raise CardError("Voyage维度改变，需重新核对模型。")
                # Keep reservation as cost upper bound; no false exact-invoice claim.
                event.update(status='ok', usage=body.get('usage', {}), seconds=time.perf_counter() - started)
                save(path, {'fingerprint': key, 'model': self.model, 'vectors': values.tolist()})
                self.last = {'cached': False, 'seconds': event['seconds']}
                return values
            except Exception as error:
                event.update(status='failed_or_uncertain', error_type=type(error).__name__, http_status=getattr(error, 'code', None))
                raise CardError(f"Voyage请求失败：{type(error).__name__}；HTTP {getattr(error, 'code', None)}。未自动重试。") from None
            finally:
                save(ledger_path, ledger)
                key_value = None

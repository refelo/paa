"""One project facade around the existing image search and card store."""
from __future__ import annotations

from .cards import CardError, CardIndexUnavailable, CardStore
from .library import read_settings
from .search import Search
from .voyage import VOYAGE_MODEL, VoyageEncoder


class Workspace:
    def __init__(self, settings=None):
        self.settings = settings or read_settings()
        self.images = Search(self.settings)

    def cards(self):
        if not self.settings.get('card_store'):
            raise CardError('本项目尚未配置知识卡store。')
        return CardStore(self.settings['card_store'], initialize=False)

    def status(self):
        from . import __version__
        from .library import PROJECT
        result = self.images.status()
        result['runtime_version'] = __version__
        import os
        result['runtime_release'] = os.environ.get('PAA_RELEASE', 'development')
        result['workspace'] = str(PROJECT)
        resources = PROJECT / '.agents/skills/paa-public/references'
        result['methods_directory'] = str(resources / 'docs' if resources.is_dir() else PROJECT / 'docs')
        result['aesthetics'] = self.settings.get('aesthetics', 'unanswered')
        result['references_directory'] = str(self.images.local / 'references')
        result['online_default'] = self.settings.get('query_online_default', False)
        result['user_reference_available'] = (PROJECT / '.local/data/user-reference.md').is_file()
        if self.settings.get('card_store'):
            result['card_vector_index'] = self.cards().vector_status('voyage', VOYAGE_MODEL, 1024)
            result['active_cards'] = result['card_vector_index']['active_cards']
        return result

    def search_cards(self, query, limit=3, route='keyword', allow_online=None):
        if allow_online is None:
            allow_online = self.settings.get('query_online_default', False)
        if allow_online and self.settings.get('allow_paid_api') is False:
            raise CardError('本次配置不允许新增付费Provider调用。')
        if route not in ('keyword', 'hybrid'):
            raise CardError('卡片route应为keyword或hybrid。')
        store = self.cards()
        before = store.snapshot()[2]
        keyword = store.keyword(query, limit)
        results = {r['id']: {**r, 'matched_by': ['keyword']} for r in keyword}
        warnings, effective_route = [], 'keyword'
        if route == 'hybrid':
            vector = []
            try:
                store.prepare_vector('voyage', VOYAGE_MODEL, 1024)
            except CardIndexUnavailable as error:
                warnings.append(str(error))
            else:
                try:
                    encoder = VoyageEncoder(self.images.local / 'voyage', allow_online=allow_online,
                                            budget_usd=self.settings.get('budget_usd', 0.1))
                    query_vector = encoder([query], 'query')[0]
                except CardError as error:
                    warnings.append(str(error))
                else:
                    try:
                        vector = store.vector(query_vector, 'voyage', VOYAGE_MODEL, limit)
                        effective_route = 'hybrid'
                    except CardIndexUnavailable as error:
                        warnings.append(str(error))
            for row in vector:
                if row['id'] in results:
                    results[row['id']]['matched_by'].append('vector')
                else:
                    results[row['id']] = {**row, 'matched_by': ['vector']}
        store.ensure_current(before)
        if warnings:
            warnings.append('语义路线未完成，仅返回关键词候选；未自动重建或重试。')
        return {'query': query, 'route': route, 'effective_route': effective_route,
                'warnings': warnings, 'candidates': list(results.values()),
                'notice': '候选不是适用结论。先核对条件与限制；没有支持则不用。原字幕不参与检索。'}

    def get_card(self, card_id):
        return self.cards().get(card_id)

    def get_user_reference(self):
        from .library import LOCAL
        path = LOCAL / 'data/user-reference.md'
        return {'available': path.is_file(), 'content': path.read_text(encoding='utf-8') if path.is_file() else '',
                'notice': '个人直接表达与跨图归纳按正文区分；AI备注不是新的人工喜好证据。'}

    def index_cards(self, allow_online=False, rebuild=False):
        if allow_online and self.settings.get('allow_paid_api') is False:
            raise CardError('本次配置不允许新增付费Provider调用。')
        encoder = VoyageEncoder(self.images.local / 'voyage', allow_online=allow_online,
                                budget_usd=self.settings.get('budget_usd', 0.1))
        def checked_encode(texts, role):
            encoder.preflight(texts, role)
            return encoder(texts, role)
        return self.cards().vector_build('voyage', encoder.model, checked_encode, rebuild=rebuild)

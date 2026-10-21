from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable

try:
    import chromadb
except Exception:  # pragma: no cover - graceful fallback when optional dep is absent
    chromadb = None


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode('utf-8')).hexdigest()


class NovelMemoryRetrieval:
    COLLECTION_NAME = 'mempalace_drawers'
    MANIFEST_VERSION = 1
    CHUNK_SIZE = 800
    CHUNK_OVERLAP = 120
    MIN_CHUNK_SIZE = 30

    def __init__(
        self,
        project_dir: Path,
        *,
        wing: str = '',
        enabled: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.memory_dir = self.project_dir / 'memory'
        self.palace_dir = self.memory_dir / 'mempalace'
        self.manifest_path = self.memory_dir / 'mempalace_manifest.json'
        self.wing = (wing or self.project_dir.name).strip() or self.project_dir.name
        self.logger = logger or logging.getLogger(__name__)
        self.enabled = bool(enabled and chromadb is not None)
        self._disabled_reason = '' if self.enabled else 'chromadb unavailable'
        self._client = None
        self._collection = None
        self._manifest = self._load_manifest()
        self._manifest_dirty = False

    @property
    def available(self) -> bool:
        return self.enabled

    @property
    def disabled_reason(self) -> str:
        return self._disabled_reason

    def backfill(self, completed_chapters: Iterable[dict[str, Any]]) -> dict[str, int]:
        stats = {
            'enabled': int(self.enabled),
            'synced': 0,
            'skipped': 0,
            'removed': 0,
            'failed': 0,
        }
        if not self.enabled:
            return stats

        live_keys: set[str] = set()
        for source in self._iter_sources(completed_chapters):
            source_path = Path(source['path']).resolve()
            source_key = self._entry_key(source_path)
            live_keys.add(source_key)

            if not source_path.exists():
                if self._delete_source(source_key, flush=False):
                    stats['removed'] += 1
                continue

            status = self.sync_text(
                room=str(source['room']),
                source_path=source_path,
                text=source_path.read_text(encoding='utf-8'),
                chapter_number=int(source.get('chapter_number', 0) or 0),
                source_kind=str(source.get('source_kind', '') or source['room']),
                flush=False,
            )
            if status in stats:
                stats[status] += 1

        stale_keys = [key for key in list(self._manifest.get('files', {})) if key not in live_keys]
        for source_key in stale_keys:
            if self._delete_source(source_key, flush=False):
                stats['removed'] += 1

        self._flush_manifest()
        return stats

    def sync_text(
        self,
        *,
        room: str,
        source_path: Path,
        text: str,
        chapter_number: int = 0,
        source_kind: str = '',
        flush: bool = True,
    ) -> str:
        if not self.enabled:
            return 'skipped'

        source_path = Path(source_path).resolve()
        source_key = self._entry_key(source_path)
        clean_text = str(text or '').strip()
        if not clean_text:
            return 'removed' if self._delete_source(source_key, flush=flush) else 'skipped'

        digest = _sha1_text(clean_text)
        current = self._manifest.get('files', {}).get(source_key, {})
        if (
            current.get('digest') == digest
            and current.get('room') == room
            and int(current.get('chapter_number', 0) or 0) == int(chapter_number or 0)
        ):
            return 'skipped'

        chunks = self._chunk_text(clean_text)
        if not chunks:
            return 'removed' if self._delete_source(source_key, flush=flush) else 'skipped'

        collection = self._get_collection()
        if collection is None:
            return 'failed'

        source_label = self._source_label(source_path)
        id_prefix = _sha1_text(f'{self.wing}|{room}|{source_label}')[:20]
        new_ids = [f'{id_prefix}:{index}:{digest[:12]}' for index in range(len(chunks))]
        metadatas: list[dict[str, Any]] = []
        for index, _chunk in enumerate(chunks):
            metadata: dict[str, Any] = {
                'wing': self.wing,
                'room': room,
                'source_file': source_label,
                'source_kind': source_kind or room,
                'chunk_index': index,
            }
            if chapter_number:
                metadata['chapter_number'] = int(chapter_number)
            metadatas.append(metadata)

        try:
            old_ids = set(current.get('ids') or [])
            stale_ids = list(old_ids - set(new_ids))
            if stale_ids:
                collection.delete(ids=stale_ids)
            collection.upsert(ids=new_ids, documents=chunks, metadatas=metadatas)
        except Exception as exc:
            self.logger.warning('[memory_retrieval] sync failed for %s: %s', source_label, exc)
            return 'failed'

        self._manifest.setdefault('files', {})[source_key] = {
            'digest': digest,
            'room': room,
            'source_file': source_label,
            'source_kind': source_kind or room,
            'chapter_number': int(chapter_number or 0),
            'ids': new_ids,
        }
        self._manifest_dirty = True
        if flush:
            self._flush_manifest()
        return 'synced'

    def search(
        self,
        query: str,
        *,
        rooms: Iterable[str] | None = None,
        n_results: int = 4,
        max_chars: int = 1200,
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        query = str(query or '').strip()
        if not query:
            return []

        collection = self._get_collection()
        if collection is None:
            return []

        allowed_rooms = {str(room).strip() for room in (rooms or []) if str(room).strip()}
        query_count = max(int(n_results or 4) * 4, 12)
        try:
            results = collection.query(
                query_texts=[query],
                n_results=query_count,
                include=['documents', 'metadatas', 'distances'],
                where={'wing': self.wing},
            )
        except Exception as exc:
            self.logger.warning('[memory_retrieval] query failed: %s', exc)
            return []

        docs = ((results.get('documents') or [[]])[0]) if isinstance(results, dict) else []
        metas = ((results.get('metadatas') or [[]])[0]) if isinstance(results, dict) else []
        dists = ((results.get('distances') or [[]])[0]) if isinstance(results, dict) else []

        hits: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, int, int]] = set()
        used_chars = 0
        max_chars = max(200, int(max_chars or 1200))
        for doc, meta, dist in zip(docs, metas, dists):
            metadata = dict(meta or {})
            room = str(metadata.get('room', '') or '')
            if allowed_rooms and room not in allowed_rooms:
                continue

            text = str(doc or '').strip()
            if not text:
                continue

            chunk_key = (
                room,
                str(metadata.get('source_file', '') or ''),
                int(metadata.get('chapter_number', 0) or 0),
                int(metadata.get('chunk_index', 0) or 0),
            )
            if chunk_key in seen_keys:
                continue
            seen_keys.add(chunk_key)

            remaining = max_chars - used_chars
            if remaining <= 0:
                break
            if len(text) > remaining:
                if hits:
                    break
                text = text[:remaining].rstrip()

            similarity = None
            try:
                similarity = round(max(0.0, 1.0 - float(dist)), 3)
            except Exception:
                similarity = None

            hit = {
                'text': text,
                'room': room,
                'source_file': str(metadata.get('source_file', '') or ''),
                'source_kind': str(metadata.get('source_kind', '') or ''),
                'chapter_number': int(metadata.get('chapter_number', 0) or 0),
                'chunk_index': int(metadata.get('chunk_index', 0) or 0),
                'similarity': similarity,
            }
            hits.append(hit)
            used_chars += len(text)
            if len(hits) >= int(n_results or 4):
                break

        return hits

    def _iter_sources(self, completed_chapters: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
        yield {'room': 'brief', 'path': self.project_dir / 'brief.md'}
        yield {'room': 'series_bible', 'path': self.memory_dir / 'series_bible.md'}
        yield {'room': 'series_bible_short', 'path': self.memory_dir / 'series_bible_short.md'}
        yield {'room': 'story_memory', 'path': self.memory_dir / 'story_memory.md'}
        ordered = sorted(
            list(completed_chapters or []),
            key=lambda item: int(item.get('chapter_number', 0) or 0),
        )
        for item in ordered:
            summary_file = str(item.get('summary_file', '') or '').strip()
            if not summary_file:
                continue
            yield {
                'room': 'chapter_summary',
                'path': Path(summary_file),
                'chapter_number': int(item.get('chapter_number', 0) or 0),
                'source_kind': 'summary',
            }

    def _get_collection(self):
        if not self.enabled:
            return None
        if self._collection is not None:
            return self._collection
        try:
            self.palace_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.palace_dir))
            self._collection = self._client.get_or_create_collection(self.COLLECTION_NAME)
            return self._collection
        except Exception as exc:
            self.enabled = False
            self._disabled_reason = str(exc)
            self.logger.warning('[memory_retrieval] disabled: %s', exc)
            return None

    def _chunk_text(self, text: str) -> list[str]:
        normalized = text.replace('\r\n', '\n').strip()
        if not normalized:
            return []
        if len(normalized) <= self.CHUNK_SIZE:
            return [normalized]

        chunks: list[str] = []
        step = max(1, self.CHUNK_SIZE - self.CHUNK_OVERLAP)
        for start in range(0, len(normalized), step):
            chunk = normalized[start:start + self.CHUNK_SIZE].strip()
            if not chunk:
                continue
            if len(chunk) < self.MIN_CHUNK_SIZE and chunks:
                continue
            if not chunks or chunk != chunks[-1]:
                chunks.append(chunk)
        return chunks

    def _entry_key(self, source_path: Path) -> str:
        try:
            return source_path.resolve().relative_to(self.project_dir).as_posix()
        except ValueError:
            return source_path.resolve().as_posix()

    def _source_label(self, source_path: Path) -> str:
        return self._entry_key(source_path)

    def _delete_source(self, source_key: str, *, flush: bool = True) -> bool:
        entry = self._manifest.get('files', {}).pop(source_key, None)
        if not entry:
            return False

        ids = list(entry.get('ids') or [])
        collection = self._get_collection()
        if collection is not None and ids:
            try:
                collection.delete(ids=ids)
            except Exception as exc:
                self.logger.warning('[memory_retrieval] delete failed for %s: %s', source_key, exc)

        self._manifest_dirty = True
        if flush:
            self._flush_manifest()
        return True

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {'version': self.MANIFEST_VERSION, 'files': {}}
        try:
            data = json.loads(self.manifest_path.read_text(encoding='utf-8'))
        except Exception:
            return {'version': self.MANIFEST_VERSION, 'files': {}}
        if not isinstance(data, dict):
            return {'version': self.MANIFEST_VERSION, 'files': {}}
        files = data.get('files')
        if not isinstance(files, dict):
            data['files'] = {}
        data.setdefault('version', self.MANIFEST_VERSION)
        return data

    def _flush_manifest(self) -> None:
        if not self._manifest_dirty:
            return
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.manifest_path.with_suffix('.json.tmp')
        tmp_path.write_text(json.dumps(self._manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp_path.replace(self.manifest_path)
        self._manifest_dirty = False

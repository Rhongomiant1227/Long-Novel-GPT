import { renderDocument } from './workspace-reader.js';

const localFrontend = location.protocol === 'file:'
    || (['localhost', '127.0.0.1', '[::1]'].includes(location.hostname) && location.port === '8000');
const apiBase = window._env_?.SERVER_URL || (localFrontend ? 'http://127.0.0.1:7869' : '/api');
const projectsRoute = '/auto_novel/projects';
const format = new Intl.NumberFormat('zh-CN');
const percentage = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 });
const byId = id => document.getElementById(id);
const typeLabels = { draft: '正文', outline: '大纲', plot: '剧情梗概', summary: '摘要' };
const stageLabels = {
    draft: '章节正文', outline: '章节大纲', plot: '剧情梗概', summary: '章节摘要',
    plan: '分卷规划', series_bible: '全书设定', memory: '剧情记忆', critic: '审校',
};
const shelfGroups = [
    { source: undefined, label: '我的作品' },
    { source: 'example', label: '内置示例' },
    { source: 'import', label: '导入存档' },
];
const speedNotes = {
    provider: '模型服务返回的解码速度',
    usage: '按模型返回的输出 token 总数和请求耗时计算，包含首个 token 的等待时间',
    tokenizer: '按模型分词器统计最近一段输出，包含已返回的推理和正文 token',
    estimate: '根据最近一段输出的文本长度估算',
};

const state = {
    projects: [], defaults: {}, project: null, context: null, document: null,
    view: 'reading', filter: 'chapters', path: '', follow: true, source: null, stage: {}, liveText: '',
    documentRequest: null, action: '', toast: 0,
    fontSize: Math.min(25, Math.max(13, Number(localStorage.getItem('novel-workspace-font')) || 17)),
    documents: new Map(), chapters: [], volumes: [], docGroups: [], expanded: new Map(),
};

const streamNode = document.createTextNode('');
byId('stream-text').append(streamNode);
applyFont();

function icon(name) {
    const element = document.createElement('span');
    element.className = 'icon';
    element.setAttribute('aria-hidden', 'true');
    element.textContent = name;
    return element;
}

function text(id, value) {
    const element = byId(id);
    if (element.textContent !== String(value)) element.textContent = value;
}

function setConnection(status, label) {
    byId('connection').dataset.state = status;
    text('connection-text', label);
}

function notify(message, error = false) {
    const element = byId('toast');
    clearTimeout(state.toast);
    element.textContent = message;
    element.dataset.error = String(error);
    element.hidden = false;
    state.toast = setTimeout(() => { element.hidden = true; }, error ? 6500 : 2600);
}

async function request(path, { signal, data } = {}) {
    const response = await fetch(apiBase + path, {
        cache: 'no-store',
        signal,
        ...(data === undefined ? {} : {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
        }),
    });
    if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || '请求失败（HTTP ' + response.status + '）');
    }
    return response.json();
}

function projectRoute(context, endpoint) {
    const route = context.source ? '/auto_novel/library' : projectsRoute;
    return route + '/' + encodeURIComponent(context.id) + endpoint;
}

function setDrawer(open, restore = true) {
    const toggle = byId('library-toggle');
    byId('library').classList.toggle('open', open);
    byId('drawer-shade').classList.toggle('open', open);
    toggle.setAttribute('aria-expanded', String(open));
    document.body.classList.toggle('drawer-open', open);
    document.querySelector('.topbar').inert = open;
    byId('workspace').inert = open;
    if (open) byId('library-close').focus();
    else if (restore && toggle.offsetWidth) toggle.focus();
}

function setView(view, keyboard = false) {
    if (view === 'live' && state.project?.source) return;
    state.view = view;
    for (const option of ['reading', 'live']) {
        const active = option === view;
        const tab = byId('view-' + option);
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        byId(option + '-view').hidden = !active;
        if (active && keyboard) tab.focus();
    }
    if (view === 'live') scrollStream();
}

function applyFont() {
    document.documentElement.style.setProperty('--read-size', state.fontSize + 'px');
    byId('font-minus').disabled = state.fontSize <= 13;
    byId('font-plus').disabled = state.fontSize >= 25;
}

function changeFont(delta) {
    state.fontSize = Math.min(25, Math.max(13, state.fontSize + delta));
    localStorage.setItem('novel-workspace-font', String(state.fontSize));
    applyFont();
}

function archiveOnly(project = state.project) {
    return project && !project.source && state.context?.ready && project.has_state === false
        && !project.running && (state.chapters.some(chapter => chapter.files.draft?.characters > 0)
            || state.documents.get('manuscript/full_novel.txt')?.size > 0);
}

function projectStatus(project) {
    if (!project) return ['idle', '未选项目'];
    if (project.source) return ['archived', project.source === 'import' ? '导入存档' : '内置示例'];
    if (project.stalled) return ['error', '等待恢复'];
    if (state.action === 'starting') return ['active', '启动中'];
    if (state.action === 'stopping') return ['active', '停止中'];
    if (project.running) return ['active', '生成中'];
    if (project.status === 'completed') return ['done', '已完成'];
    if (archiveOnly(project)) return ['archived', '已有存档'];
    if (project.last_error) return ['error', '任务中断'];
    if (project.status === 'stopped' || project.status === 'running') return ['idle', '已暂停'];
    return ['idle', '待生成'];
}

function renderHeartbeat() {
    const project = state.project;
    if (!project?.running || project.heartbeat_age_seconds == null) {
        text('heartbeat', '—');
        return;
    }
    const seconds = project.heartbeat_age_seconds + Math.floor((Date.now() - state.context.statusAt) / 1000);
    text('heartbeat', seconds < 10 ? '刚刚' : seconds < 60 ? seconds + ' 秒前' : Math.floor(seconds / 60) + ' 分钟前');
}

function renderStatus() {
    const project = state.project;
    const archive = archiveOnly(project);
    const [status, label] = projectStatus(project);
    const active = project?.running;
    const outputStage = state.stage.active && state.stage.label ? state.stage.label : '';
    const phase = outputStage || project?.current_stage || '准备生成';
    const drafts = state.chapters.filter(({ files }) => files.draft?.characters > 0);
    const count = Math.max(project?.generated_chapters || project?.archived_chapters || 0, drafts.length);
    const chars = Number(project?.generated_chars || project?.archived_chars) || drafts.reduce((total, chapter) => total + chapter.files.draft.characters, 0);
    const target = Number(project?.target_chars) || 0;
    const progress = target > 0 ? (chars / target) * 100 : 0;
    const activeChapter = state.stage.active ? state.stage.chapter_number : null;
    const latest = drafts.at(-1);

    text('project-title', project?.display_name || '长篇创作工作站');
    text('chapters-label', project?.source ? '收录章节' : '归档章节');
    text('chars-label', project?.source ? '正文字数' : '已写字数');
    byId('target-metric').hidden = Boolean(project?.source);
    byId('project-state').dataset.state = status;
    text('project-state-text', label);
    byId('project-state').title = active ? phase : archive ? '已有正文存档，续写需要 state.json' : label;
    text('metric-chapters', project ? format.format(count) : '—');
    text('metric-chars', project ? format.format(chars) : '—');
    text('metric-target', target ? format.format(target) : '—');
    text('current-stage', active ? phase : archive ? '已归档 · 可浏览历史章节'
        : state.action === 'starting' ? '正在启动写作进程' : label);
    text('position-label', active ? '当前章节' : '最近归档');
    text('stage-position', active ? '第 ' + (activeChapter || project.next_chapter_number) + ' 章'
        : latest ? '第 ' + latest.number + ' 章' : '—');
    text('progress-percent', target ? percentage.format(progress) + '%' : '—');
    byId('progress-track').setAttribute('aria-valuenow', String(Math.round(Math.min(progress, 100))));
    byId('progress-fill').style.width = Math.min(progress, 100) + '%';
    byId('stage-error').hidden = !project?.last_error;
    text('stage-error-text', project?.last_error || '');
    byId('run-project').hidden = Boolean(active || archive || project?.source || status === 'done');
    byId('run-project').disabled = !state.context?.ready || !project || Boolean(state.action);
    byId('stop-project').hidden = !active;
    byId('stop-project').disabled = Boolean(state.action);
    text('run-label', state.action === 'starting' ? '正在启动…' : count || project?.has_state ? '继续创作' : '开始创作');
    byId('archive-note').hidden = !archive;
    renderHeartbeat();
    renderStream();
}

function scrollStream() {
    if (state.view !== 'live' || !state.follow) return;
    const viewport = byId('stream-scroll');
    viewport.scrollTop = viewport.scrollHeight;
}

function renderGenerationSpeed() {
    const project = state.project;
    const stage = state.stage;
    const live = Boolean(project?.running && stage.active);
    const speed = stage.generation_speed;
    const rate = speed?.tokens_per_second;
    const recent = stage.finished && !stage.active;
    const stale = live && speed && performance.now() - state.context.speedSeenAt > 8000;
    const showRate = (live || recent) && !stale && Number.isFinite(rate) && rate > 0;
    const unitRate = showRate ? (['estimate', 'tokenizer'].includes(speed.source) ? '≈ ' : '')
        + percentage.format(rate) + ' tk/s' : '';
    const speedLabel = recent && showRate ? '上次生成速度' : '生成速度';
    const note = showRate ? speedNotes[speed.source] || '' : '';
    text('speed-label', speedLabel);
    text('stage-speed', unitRate || (stale ? '等待输出' : live ? '采样中…' : '—'));
    byId('stage-speed').title = note;
    byId('stream-rate').hidden = !showRate;
    text('stream-rate', showRate ? speedLabel + ' ' + unitRate : '');
    byId('stream-rate').title = note;
    const mobilePhase = project?.running ? (stage.active && stage.label) || project.current_stage || '准备生成' : '';
    text('mobile-stage', [live && unitRate, mobilePhase].filter(Boolean).join(' · '));
}

function renderStream() {
    const project = state.project;
    const stage = state.stage;
    const live = Boolean(project?.running && stage.active);
    renderGenerationSpeed();
    const chapter = stage.chapter_number;
    const label = stage.label || '等待输出';
    const awaiting = project?.running ? '正在等待模型返回文本…' : '当前没有生成任务。';
    const snippet = state.liveText.trim().slice(-115).trimStart();
    byId('tab-signal').hidden = !project?.running;
    text('stream-title', label);
    text('stream-kicker', [chapter ? '第 ' + chapter + ' 章' : '', stageLabels[stage.stage_type] || '模型输出'].filter(Boolean).join(' · '));
    text('stream-meta', state.liveText ? [
        live ? '更新中' : '最近一次输出',
        format.format(stage.text_length ?? state.liveText.length) + ' 字',
        stage.updated_at?.slice(11, 19),
    ].filter(Boolean).join(' · ') : '');
    byId('stream-empty').hidden = Boolean(state.liveText);
    text('stream-empty', awaiting);
    byId('stream-content').hidden = !state.liveText;
    byId('stream-caret').hidden = !live;
    byId('preview-indicator').dataset.state = project?.running ? 'active' : 'idle';
    byId('preview-label').hidden = !stage.label;
    text('preview-label', stage.label || '');
    byId('stream-preview').classList.toggle('has-text', Boolean(snippet));
    text('stream-preview', snippet ? (state.liveText.trim().length > 115 ? '…' : '') + snippet
        : awaiting + (project?.running ? '' : '开始创作后，新文本将在这里滚动更新。'));
}

function updateStream(stage, chunk, append = false) {
    if (state.context && stage.generation_speed?.sampled_at !== state.stage.generation_speed?.sampled_at) {
        state.context.speedSeenAt = performance.now();
    }
    if (append) {
        streamNode.appendData(chunk);
        state.liveText += chunk;
    } else if (state.liveText !== chunk) {
        streamNode.data = chunk;
        state.liveText = chunk;
    }
    state.stage = stage;
    renderStatus();
    scrollStream();
}

function renderProjects() {
    const select = byId('project-select');
    const groups = [];
    for (const { source, label } of shelfGroups) {
        const books = state.projects.filter(book => book.source === source);
        if (!books.length) continue;
        const group = document.createElement('optgroup');
        group.label = label;
        group.append(...books.map(book => new Option(book.display_name || book.name, book.id)));
        groups.push(group);
    }
    select.replaceChildren(...(groups.length ? groups : [new Option('暂无作品', '')]));
    select.disabled = !state.projects.length;
    select.value = state.project?.id || '';
    if (byId('shelf-dialog').open) renderShelf();
}

function renderShelf() {
    const fragment = document.createDocumentFragment();
    for (const { source, label } of shelfGroups) {
        const books = state.projects.filter(book => book.source === source);
        if (source && !books.length) continue;
        const heading = document.createElement('h3');
        heading.textContent = label;
        fragment.append(heading);
        if (!books.length) {
            const empty = document.createElement('p');
            empty.className = 'shelf-empty';
            empty.textContent = '还没有自己的小说。点「开新坑」写下第一个设定。';
            fragment.append(empty);
        }
        for (const book of books) {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'shelf-book';
            button.dataset.book = book.id;
            button.dataset.source = source || 'project';
            const selected = book.id === state.context?.id;
            if (selected) button.setAttribute('aria-current', 'true');
            const cover = icon('auto_stories');
            cover.classList.add('shelf-cover');
            const body = document.createElement('span');
            body.className = 'shelf-book-body';
            const title = document.createElement('strong');
            title.textContent = book.display_name || book.name;
            const meta = document.createElement('small');
            const chapters = book.generated_chapters || book.archived_chapters || 0;
            const characters = book.generated_chars || book.archived_chars || 0;
            meta.textContent = [format.format(chapters) + ' 章', format.format(characters) + ' 字',
                book.running ? '生成中' : '', selected ? '正在浏览' : ''].filter(Boolean).join(' · ');
            body.append(title, meta);
            if (book.description) {
                const description = document.createElement('span');
                description.className = 'shelf-description';
                description.textContent = book.description;
                body.append(description);
            }
            button.append(cover, body, icon('chevron_right'));
            fragment.append(button);
        }
    }
    byId('shelf-books').replaceChildren(fragment);
}

function fileButton(file) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'tree-entry';
    button.dataset.path = file.path;
    if (file.chapter) {
        const number = document.createElement('span');
        number.className = 'entry-number';
        number.textContent = '第 ' + file.chapter.number + ' 章';
        button.append(number);
    } else {
        button.append(icon('description'));
    }
    const title = document.createElement('span');
    title.className = 'entry-title';
    title.textContent = file.chapter?.title || (file.chapter ? typeLabels[file.type] : file.label);
    button.append(title);
    button.title = file.chapter?.label || file.label;
    return button;
}

function setActiveEntries() {
    for (const button of byId('library-tree').querySelectorAll('[data-path]')) {
        const file = state.documents.get(button.dataset.path);
        const active = file && (file.path === state.path
            || (file.chapter && file.chapter === state.documents.get(state.path)?.chapter));
        if (active) button.setAttribute('aria-current', 'true');
        else button.removeAttribute('aria-current');
    }
}

function renderLibrary() {
    const tree = byId('library-tree');
    const top = tree.scrollTop;
    const query = byId('library-search').value.trim().toLocaleLowerCase().replace(/\s+/g, '');
    const matches = label => !query || label.toLocaleLowerCase().replace(/\s+/g, '').includes(query);
    const output = document.createDocumentFragment();
    const currentChapter = state.documents.get(state.path)?.chapter;

    if (state.filter === 'chapters') {
        for (const volume of state.volumes.toReversed()) {
            const files = volume.chapters.filter(chapter => matches(chapter.label)
                || (query && query === String(chapter.number))).map(chapter => chapter.primary);
            if (!files.length) continue;
            const details = document.createElement('details');
            details.className = 'tree-group';
            details.open = Boolean(query || (state.expanded.get(volume.number)
                ?? (currentChapter ? currentChapter.volume === volume.number : volume === state.volumes.at(-1))));
            const summary = document.createElement('summary');
            const count = document.createElement('span');
            count.className = 'group-count';
            count.textContent = String(files.length);
            const arrow = icon('chevron_right');
            arrow.classList.add('group-arrow');
            summary.append(icon('folder_open'), volume.label, count, arrow);
            const children = document.createElement('div');
            children.className = 'tree-children';
            const fill = () => {
                if (children.childElementCount) return;
                children.append(...files.map(fileButton));
                setActiveEntries();
            };
            if (details.open) fill();
            details.addEventListener('toggle', () => {
                if (!details.isConnected) return;
                if (!query) state.expanded.set(volume.number, details.open);
                if (details.open) fill();
            });
            details.append(summary, children);
            output.append(details);
        }
    } else {
        for (const group of state.docGroups) {
            const files = group.files.filter(file => matches(file.label) || matches(group.label));
            if (!files.length) continue;
            const title = document.createElement('h3');
            title.className = 'group-label';
            title.append(icon(group.icon), group.label);
            output.append(title, ...files.map(fileButton));
        }
    }
    if (!output.childElementCount) {
        const empty = document.createElement('p');
        empty.className = 'tree-empty';
        empty.textContent = query ? '没有匹配的资料。' : state.project ? '此分类暂无内容。' : '暂无项目。';
        output.append(empty);
    }
    tree.replaceChildren(output);
    setActiveEntries();
    tree.scrollTop = top;
}

function indexTree(tree) {
    state.documents = new Map();
    state.chapters = [];
    state.volumes = [];
    state.docGroups = [];
    for (const group of tree) {
        if (group.type !== 'volumes') {
            for (const item of group.children) state.documents.set(item.path, item);
            state.docGroups.push({
                label: group.label, files: group.children,
                icon: group.type === 'logs' ? 'terminal' : 'description',
            });
            continue;
        }
        for (const volume of group.children) {
            const entry = { label: volume.label, number: volume.volume_number, chapters: [] };
            const docs = [];
            for (const child of volume.children) {
                if (child.kind === 'file') {
                    state.documents.set(child.path, child);
                    docs.push(child);
                    continue;
                }
                const chapter = {
                    label: child.label, number: child.chapter_number, volume: entry.number,
                    title: child.title || child.label.split(' · ')[1] || '',
                    files: {},
                };
                for (const file of child.children) {
                    const item = { ...file, chapter };
                    state.documents.set(item.path, item);
                    chapter.files[item.type] = item;
                }
                chapter.primary = chapter.files.draft || chapter.files.outline || Object.values(chapter.files)[0];
                entry.chapters.push(chapter);
                state.chapters.push(chapter);
            }
            if (entry.chapters.length) state.volumes.push(entry);
            if (docs.length) state.docGroups.push({ label: volume.label, files: docs, icon: 'layers' });
        }
    }
    state.chapters.sort((left, right) => left.number - right.number);
    state.volumes.sort((left, right) => left.number - right.number);
}

function setFilter(filter) {
    state.filter = filter;
    byId('filter-chapters').setAttribute('aria-pressed', String(filter === 'chapters'));
    byId('filter-docs').setAttribute('aria-pressed', String(filter === 'documents'));
    renderLibrary();
}

function renderDocumentHeader(file) {
    const chapter = file?.chapter;
    text('reader-title', chapter?.title || (chapter ? '第 ' + chapter.number + ' 章' : file?.label || '暂无文档'));
    const volumeName = state.project?.source ? state.project.display_name : '第 ' + chapter?.volume + ' 卷';
    text('reader-context', chapter ? volumeName + ' · 第 ' + chapter.number + ' 章' : '作品资料库');

    const tabs = byId('doc-tabs');
    const buttons = [];
    if (chapter) {
        for (const [type, label] of Object.entries(typeLabels)) {
            const item = chapter.files[type];
            if (!item) continue;
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = label;
            button.dataset.path = item.path;
            button.setAttribute('aria-pressed', String(item.path === file.path));
            buttons.push(button);
        }
    }
    tabs.replaceChildren(...buttons);
    tabs.hidden = !buttons.length;
    const position = state.chapters.indexOf(chapter);
    byId('chapter-nav').hidden = position < 0;
    byId('chapter-prev').disabled = position < 1;
    byId('chapter-next').disabled = position < 0 || position >= state.chapters.length - 1;
    text('document-info', '');
}

function readerMessage(message) {
    const paragraph = document.createElement('p');
    paragraph.className = 'muted';
    paragraph.textContent = message;
    byId('reader-content').replaceChildren(paragraph);
}

async function openDocument(path, { keepFocus = false } = {}) {
    const file = state.documents.get(path);
    const context = state.context;
    if (!file || !context) return;
    state.documentRequest?.abort();
    const controller = new AbortController();
    state.documentRequest = controller;
    state.path = path;
    state.document = null;
    localStorage.setItem('novel-workspace-document:' + context.id, path);
    byId('copy-content').disabled = true;
    setView('reading');
    renderDocumentHeader(file);
    if (keepFocus) byId('doc-tabs').querySelector('[aria-pressed="true"]')?.focus();
    setActiveEntries();
    readerMessage('正在读取文档…');
    byId('reader-scroll').scrollTop = 0;
    try {
        const data = await request(projectRoute(context, '/content?path=' + encodeURIComponent(path)), { signal: controller.signal });
        if (controller.signal.aborted || state.context !== context) return;
        state.document = data;
        renderDocument(byId('reader-content'), data.content, data.type);
        byId('copy-content').disabled = !data.content;
        const count = file.characters || data.content.trim().length;
        text('document-info', [format.format(count) + ' 字', data.truncated ? '预览已截断' : ''].filter(Boolean).join(' · '));
    } catch (error) {
        if (error.name !== 'AbortError') readerMessage('读取失败：' + error.message);
    }
}

function moveChapter(delta) {
    const file = state.documents.get(state.path);
    const index = state.chapters.indexOf(file?.chapter);
    const next = index < 0 ? null : state.chapters[index + delta];
    if (!next) return;
    state.expanded.set(next.volume, true);
    openDocument((next.files[file.type] || next.primary).path);
    renderLibrary();
    byId('library-tree').querySelector('[aria-current="true"]')?.scrollIntoView({ block: 'nearest', container: 'nearest' });
}

async function refreshTree(context = state.context, { initial = false, tree: suppliedTree } = {}) {
    if (!context || context.loading || context !== state.context) return;
    context.loading = true;
    try {
        const tree = suppliedTree || (await request(projectRoute(context, '/tree'), {
            signal: context.controller.signal,
        })).tree;
        if (context !== state.context) return;
        const fingerprint = JSON.stringify(tree);
        if (fingerprint !== context.treeKey) {
            const wasReady = context.ready;
            context.ready = true;
            context.treeKey = fingerprint;
            indexTree(tree);
            if (!state.path || !state.documents.has(state.path)) {
                const remembered = localStorage.getItem('novel-workspace-document:' + context.id);
                const latest = state.chapters.findLast(({ files }) => files.draft?.characters > 0)
                    || state.chapters.at(-1);
                const file = state.documents.get(remembered) || (context.source ? state.chapters[0]?.primary : latest?.primary)
                    || state.documents.get('brief.md') || state.documents.values().next().value;
                state.path = '';
                if (file && (!wasReady || state.view === 'reading')) {
                    setFilter(file.chapter ? 'chapters' : 'documents');
                    openDocument(file.path);
                } else if (!file) {
                    renderDocumentHeader();
                    readerMessage('这个项目还没有可阅读的内容。');
                }
            } else if (state.view === 'reading') {
                renderDocumentHeader(state.documents.get(state.path));
                if (state.document) {
                    text('document-info', format.format(state.document.content.trim().length) + ' 字'
                        + (state.document.truncated ? ' · 预览已截断' : ''));
                }
            }
            renderLibrary();
            renderStatus();
        }
    } catch (error) {
        if (error.name === 'AbortError') return;
        if (initial || !context.ready) {
            readerMessage('资料库读取失败：' + error.message);
            byId('library-tree').firstElementChild.textContent = '读取失败，可刷新资料库重试。';
        } else {
            notify('资料库刷新失败：' + error.message, true);
        }
    } finally {
        context.loading = false;
    }
}

function updateProject(project, context) {
    if (context !== state.context || project.id !== context.id) return;
    const previous = state.project;
    const changed = !context.ready || previous?.generated_chapters !== project.generated_chapters
        || previous?.running !== project.running;
    state.project = project;
    context.statusAt = Date.now();
    if (project.running && context.startTimer) {
        clearTimeout(context.startTimer);
        context.startTimer = null;
        state.action = '';
    }
    const index = state.projects.findIndex(item => item.id === project.id);
    if (index >= 0) state.projects[index] = { ...state.projects[index], ...project };
    const option = byId('project-select').selectedOptions[0];
    if (option?.value === project.id) option.textContent = project.display_name;
    renderStatus();
    if (changed) refreshTree(context);
}

function connectStream(context) {
    const fallback = async () => {
        if (context.polling || context !== state.context) return;
        context.polling = true;
        try {
            const { project, live_stage: stage } = await request(projectRoute(context, '/live?compact=1'), {
                signal: context.controller.signal,
            });
            if (context !== state.context || context.connected) return;
            updateStream(stage || {}, stage?.text || '');
            updateProject(project, context);
            setConnection('online', '轮询更新');
        } catch (error) {
            if (error.name !== 'AbortError' && context === state.context && !context.connected) {
                setConnection('offline', '连接中断');
            }
        } finally {
            context.polling = false;
        }
    };
    context.fallback = fallback;
    if (typeof EventSource === 'undefined') {
        fallback();
        return;
    }
    const source = new EventSource(apiBase + projectRoute(context, '/stream'));
    state.source = source;
    source.addEventListener('open', () => {
        if (state.source !== source) return;
        context.connected = true;
        setConnection('online', '已连接');
    });
    source.addEventListener('status', event => {
        if (state.source === source) updateProject(JSON.parse(event.data), context);
    });
    source.addEventListener('live', event => {
        if (state.source !== source) return;
        const { stage, text: chunk, append } = JSON.parse(event.data);
        updateStream(stage, chunk, append);
    });
    source.addEventListener('error', () => {
        if (state.source !== source) return;
        context.connected = false;
        setConnection('waiting', '重连中');
        fallback();
    });
}

function selectProject(project, tree) {
    state.source?.close();
    state.source = null;
    state.context?.controller.abort();
    clearTimeout(state.context?.startTimer);
    state.documentRequest?.abort();
    state.project = project;
    state.context = project ? {
        id: project.id, source: project.source, controller: new AbortController(),
        statusAt: Date.now(), speedSeenAt: performance.now(), ready: false,
    } : null;
    state.path = '';
    state.document = null;
    state.action = '';
    state.expanded.clear();
    byId('library-search').value = '';
    byId('copy-content').disabled = true;
    byId('layout').classList.toggle('library-reading', Boolean(project?.source));
    byId('view-live').hidden = Boolean(project?.source);
    document.querySelector('.activity').hidden = Boolean(project?.source);
    indexTree([]);
    renderProjects();
    setFilter('chapters');
    setView('reading');
    renderDocumentHeader();
    text('reader-title', project ? '正在载入' : '还没有作品');
    readerMessage(project ? '正在载入项目内容…' : '新建项目后，可以在这里查看作品设定、章节和实时生成。');
    updateStream(project?.live_stage || {}, project?.live_stage?.text || '');
    if (!project) return;
    const loading = refreshTree(state.context, { initial: true, tree });
    if (project.source) setConnection('online', '浏览中');
    else connectStream(state.context);
    return loading;
}

async function loadProjects() {
    try {
        const { projects, library_books: books = [], defaults } = await request(projectsRoute);
        state.projects = [...projects, ...books];
        state.defaults = defaults;
        if (!state.context || state.context.source) setConnection('online', state.context?.source ? '浏览中' : '已连接');
        const activeId = state.project?.id || localStorage.getItem('novel-workspace-project');
        const selected = state.projects.find(project => project.id === activeId) || state.projects[0] || null;
        if (!state.context || selected?.id !== state.context.id) selectProject(selected);
        else renderProjects();
    } catch (error) {
        setConnection('offline', '后端离线');
        if (!state.project) {
            text('reader-title', '无法连接到后端');
            readerMessage('请确认 Long Novel 服务已启动，然后刷新资料库。');
            byId('library-tree').firstElementChild.textContent = '无法连接。';
        }
        throw error;
    }
}

async function runAction(action) {
    if (!state.context || state.context.source || state.action) return;
    const context = state.context;
    state.action = action === 'continue' ? 'starting' : 'stopping';
    renderStatus();
    try {
        const project = await request(projectRoute(context, '/' + action), { data: {} });
        if (state.context !== context) return;
        if (action === 'stop' && !project.stop_result?.stopped && project.running) throw new Error('未能停止当前任务。');
        updateProject(project, context);
        if (action === 'continue') {
            setView('live');
            if (project.started?.spawned && !project.running) {
                context.startTimer = setTimeout(() => {
                    context.startTimer = null;
                    state.action = '';
                    renderStatus();
                }, 12000);
            }
            notify(project.started?.spawned ? '写作进程已启动' : '这个项目已在运行');
        } else {
            notify(project.stop_result?.stopped ? '生成已停止' : '项目没有正在运行的任务');
        }
    } catch (error) {
        if (state.context === context) notify(error.message, true);
    } finally {
        if (state.context === context && !context.startTimer) {
            state.action = '';
            renderStatus();
        }
    }
}

const createDialog = byId('create-dialog');
const createForm = byId('create-form');

async function openCreateDialog() {
    const fields = createForm.elements;
    createForm.reset();
    for (const key of ['target_chars', 'chapter_char_target', 'main_model', 'sub_model']) {
        if (state.defaults[key] != null) fields.namedItem(key).value = state.defaults[key];
    }
    byId('form-error').hidden = true;
    createDialog.showModal();
    const settings = await request('/setting').catch(() => null);
    if (!settings) return;
    byId('model-options').replaceChildren(...Object.entries(settings.models).flatMap(
        ([provider, models]) => models.map(model => new Option(provider + '/' + model)),
    ));
}

async function createProject(event) {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(createForm));
    data.auto_start = Boolean(data.auto_start);
    const submit = byId('submit-create');
    submit.disabled = true;
    byId('form-error').hidden = true;
    try {
        const { tree, ...project } = await request(projectsRoute, { data });
        state.projects = [project, ...state.projects.filter(item => item.id !== project.id)];
        localStorage.setItem('novel-workspace-project', project.id);
        await selectProject(project, tree);
        createDialog.close();
        if (project.started?.spawned) setView('live');
        if (project.start_error) notify('作品已保存，启动失败：' + project.start_error, true);
        else notify('作品已加入书架');
    } catch (error) {
        text('form-error', error.message);
        byId('form-error').hidden = false;
    } finally {
        submit.disabled = false;
    }
}

function openBook(id) {
    const project = state.projects.find(item => item.id === id);
    if (!project || project.id === state.context?.id) return;
    localStorage.setItem('novel-workspace-project', project.id);
    selectProject(project);
}

byId('project-select').addEventListener('change', event => openBook(event.target.value));
const shelfDialog = byId('shelf-dialog');
byId('open-shelf').addEventListener('click', async () => {
    renderShelf();
    byId('shelf-message').hidden = true;
    shelfDialog.showModal();
    try {
        await loadProjects();
    } catch (error) {
        text('shelf-message', '书架刷新失败：' + error.message);
        byId('shelf-message').hidden = false;
    }
});
byId('close-shelf').addEventListener('click', () => shelfDialog.close());
byId('shelf-books').addEventListener('click', event => {
    const book = event.target.closest('[data-book]');
    if (!book) return;
    shelfDialog.close();
    openBook(book.dataset.book);
});
byId('shelf-create').addEventListener('click', () => {
    shelfDialog.close();
    openCreateDialog();
});
byId('library-toggle').addEventListener('click', () => setDrawer(true));
byId('library-close').addEventListener('click', () => setDrawer(false));
byId('drawer-shade').addEventListener('click', () => setDrawer(false));
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && byId('library').classList.contains('open')) setDrawer(false);
});
matchMedia('(max-width: 790px)').addEventListener('change', () => setDrawer(false, false));
byId('filter-chapters').addEventListener('click', () => setFilter('chapters'));
byId('filter-docs').addEventListener('click', () => setFilter('documents'));
byId('library-search').addEventListener('input', renderLibrary);
byId('library-tree').addEventListener('click', event => {
    const path = event.target.closest('[data-path]')?.dataset.path;
    if (!path) return;
    openDocument(path);
    if (byId('library').classList.contains('open')) setDrawer(false);
});
byId('doc-tabs').addEventListener('click', event => {
    const path = event.target.closest('[data-path]')?.dataset.path;
    if (path && path !== state.path) openDocument(path, { keepFocus: true });
});
byId('chapter-prev').addEventListener('click', () => moveChapter(-1));
byId('chapter-next').addEventListener('click', () => moveChapter(1));
byId('view-reading').addEventListener('click', () => {
    setView('reading');
    if (!state.path && state.context?.ready) {
        const file = state.chapters.at(-1)?.primary || state.documents.get('brief.md');
        if (file) openDocument(file.path);
    }
});
byId('view-live').addEventListener('click', () => setView('live'));
byId('open-live').addEventListener('click', () => {
    setView('live', true);
    byId('view-live').scrollIntoView({ block: 'nearest' });
});
document.querySelector('.reader-nav').addEventListener('keydown', event => {
    if (state.project?.source) return;
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Home') setView('reading', true);
    else if (event.key === 'End') setView('live', true);
    else setView(state.view === 'reading' ? 'live' : 'reading', true);
});
byId('focus').addEventListener('click', event => {
    const active = byId('layout').classList.toggle('focus-mode');
    event.currentTarget.setAttribute('aria-pressed', String(active));
    if (active) setDrawer(false, false);
});
byId('follow').addEventListener('click', event => {
    state.follow = !state.follow;
    event.currentTarget.setAttribute('aria-pressed', String(state.follow));
    text('follow-label', state.follow ? '跟随输出' : '跟随已关闭');
    scrollStream();
});
byId('font-minus').addEventListener('click', () => changeFont(-1));
byId('font-plus').addEventListener('click', () => changeFont(1));
byId('copy-content').addEventListener('click', async () => {
    if (!state.document?.content) return;
    try {
        await navigator.clipboard.writeText(state.document.content);
        notify('已复制当前文本');
    } catch {
        notify('复制失败，请选中正文后手动复制。', true);
    }
});
byId('open-create').addEventListener('click', openCreateDialog);
byId('close-create').addEventListener('click', () => createDialog.close());
byId('cancel-create').addEventListener('click', () => createDialog.close());
createForm.addEventListener('submit', createProject);
byId('run-project').addEventListener('click', () => runAction('continue'));
byId('stop-project').addEventListener('click', () => runAction('stop'));
byId('refresh').addEventListener('click', async () => {
    try {
        await loadProjects();
        await refreshTree();
        state.context?.fallback?.();
    } catch (error) {
        notify(error.message, true);
    }
});

loadProjects().catch(() => {});
setInterval(() => {
    if (!document.hidden && state.project?.running) {
        renderHeartbeat();
        renderGenerationSpeed();
    }
}, 1000);
setInterval(() => {
    const context = state.context;
    if (!context || document.hidden) return;
    if (!context.connected) context.fallback?.();
}, 1800);
setInterval(() => {
    if (!document.hidden && state.project?.running) refreshTree();
}, 9000);

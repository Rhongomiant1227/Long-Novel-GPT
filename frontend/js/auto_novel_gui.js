import { showToast } from './utils.js';

const state = {
    visible: false,
    projects: [],
    defaults: null,
    selectedProjectId: '',
    selectedPath: '',
    tree: [],
    realtime: true,
    createFormInitialized: false,
    liveBusy: false,
    projectBusy: false,
    projectPollTimer: null,
    livePollTimer: null,
};

const els = {};

function byId(id) {
    return document.getElementById(id);
}

function getSelectedProject() {
    return state.projects.find(project => project.id === state.selectedProjectId) || null;
}

async function fetchJson(url, options = {}) {
    const response = await fetch(url, options);
    let data = {};
    try {
        data = await response.json();
    } catch (error) {
        data = {};
    }
    if (!response.ok) {
        throw new Error(data.error || '请求失败');
    }
    return data;
}

async function postJson(url, body = {}) {
    return fetchJson(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
    });
}

function formatNumber(value) {
    return Number(value || 0).toLocaleString('zh-CN');
}

function formatAge(seconds) {
    if (seconds == null) {
        return '未知';
    }
    if (seconds < 60) {
        return `${seconds}s`;
    }
    if (seconds < 3600) {
        return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    }
    return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function getBadgeInfo(project) {
    if (!project) {
        return { text: '未选择', className: 'idle' };
    }
    if (project.running && project.stalled) {
        return { text: '疑似停滞', className: 'stalled' };
    }
    if (project.running) {
        return { text: '运行中', className: 'running' };
    }
    return { text: project.status || '未启动', className: 'idle' };
}

function getReaderClass(type) {
    if (type === 'draft') {
        return 'reader-draft';
    }
    if (type === 'manuscript') {
        return 'reader-manuscript';
    }
    return '';
}

function setToolbarState(project) {
    els.currentProject.textContent = project
        ? `当前项目：${project.display_name || project.name}`
        : '当前未选择项目';

    const disabled = !project;
    els.continueBtn.disabled = disabled;
    els.stopBtn.disabled = disabled;
}

function ensureCreateFormDefaults() {
    if (!state.defaults || state.createFormInitialized) {
        return;
    }
    els.projectName.value = '';
    els.targetChars.value = state.defaults.target_chars;
    els.chapterChars.value = state.defaults.chapter_char_target;
    els.mainModel.value = state.defaults.main_model;
    els.subModel.value = state.defaults.sub_model;
    state.createFormInitialized = true;
}

function renderProjects() {
    els.projectList.innerHTML = '';

    if (!state.projects.length) {
        const empty = document.createElement('div');
        empty.className = 'auto-empty';
        empty.textContent = '还没有项目。先在上方创建一个。';
        els.projectList.appendChild(empty);
        setToolbarState(null);
        return;
    }

    state.projects.forEach(project => {
        const badge = getBadgeInfo(project);
        const item = document.createElement('div');
        item.className = `auto-project-item${project.id === state.selectedProjectId ? ' active' : ''}`;
        item.addEventListener('click', () => {
            selectProject(project.id);
        });

        const head = document.createElement('div');
        head.className = 'auto-project-head';

        const title = document.createElement('div');
        title.className = 'auto-project-title';
        title.textContent = project.display_name || project.name;

        const badgeNode = document.createElement('span');
        badgeNode.className = `auto-badge ${badge.className}`;
        badgeNode.textContent = badge.text;

        head.appendChild(title);
        head.appendChild(badgeNode);

        const meta = document.createElement('div');
        meta.className = 'auto-project-meta';
        meta.textContent = `${formatNumber(project.generated_chapters)} 章 · ${formatNumber(project.generated_chars)} 字 · 进度 ${Number(project.progress_percent || 0).toFixed(2)}%`;

        const stage = document.createElement('div');
        stage.className = 'auto-project-stage';
        stage.textContent = project.current_stage || '暂无当前阶段';

        const foot = document.createElement('div');
        foot.className = 'auto-project-foot';
        foot.textContent = `最近更新：${project.updated_at || '未知'} · 下章：${project.next_chapter_number || 1}`;

        item.appendChild(head);
        item.appendChild(meta);
        item.appendChild(stage);
        item.appendChild(foot);
        els.projectList.appendChild(item);
    });

    setToolbarState(getSelectedProject());
}

function renderStatus(project) {
    els.statusGrid.innerHTML = '';
    if (!project) {
        els.statusGrid.innerHTML = '<div class="auto-empty">请选择一个项目。</div>';
        els.statusError.classList.remove('visible');
        els.statusError.textContent = '';
        return;
    }

    const items = [
        ['状态', getBadgeInfo(project).text],
        ['当前阶段', project.current_stage || '空闲'],
        ['累计进度', `${formatNumber(project.generated_chapters)} 章 / ${formatNumber(project.generated_chars)} 字`],
        ['目标进度', `${Number(project.progress_percent || 0).toFixed(2)}% / ${formatNumber(project.target_chars)} 字`],
        ['心跳', project.running ? formatAge(project.heartbeat_age_seconds) : '未运行'],
    ];

    items.forEach(([label, value]) => {
        const card = document.createElement('div');
        card.className = 'auto-status-item';
        card.innerHTML = `
            <div class="auto-status-label">${label}</div>
            <div class="auto-status-value">${value}</div>
        `;
        els.statusGrid.appendChild(card);
    });

    if (project.last_error) {
        els.statusError.textContent = project.last_error;
        els.statusError.classList.add('visible');
    } else {
        els.statusError.textContent = '';
        els.statusError.classList.remove('visible');
    }
}

function findDefaultPath(nodes) {
    let fallback = '';
    let bestDraft = { chapter: -1, path: '' };

    const visit = items => {
        items.forEach(node => {
            if (node.kind === 'file' && node.path) {
                if (!fallback && node.type === 'brief') {
                    fallback = node.path;
                }
                if (node.type === 'draft') {
                    const chapter = Number(node.chapter_number || 0);
                    if (chapter >= bestDraft.chapter) {
                        bestDraft = { chapter, path: node.path };
                    }
                }
            }
            if (node.children) {
                visit(node.children);
            }
        });
    };

    visit(nodes || []);
    return bestDraft.path || fallback;
}

function createTreeNode(node) {
    if (node.children && node.kind !== 'file') {
        const details = document.createElement('details');
        details.open = node.kind === 'group';

        const summary = document.createElement('summary');
        summary.textContent = node.label;
        details.appendChild(summary);

        const children = document.createElement('div');
        children.className = 'auto-tree-children';
        node.children.forEach(child => children.appendChild(createTreeNode(child)));
        details.appendChild(children);
        return details;
    }

    const button = document.createElement('button');
    button.type = 'button';
    button.className = `auto-tree-file type-${node.type}${node.path === state.selectedPath ? ' active' : ''}`;
    button.textContent = node.label;
    button.addEventListener('click', async () => {
        if (state.realtime) {
            setRealtime(false);
        }
        await openHistoryFile(node.path);
    });
    return button;
}

function renderTree(tree) {
    els.tree.innerHTML = '';
    if (!tree.length) {
        const empty = document.createElement('div');
        empty.className = 'auto-empty';
        empty.textContent = '当前项目还没有可浏览内容。';
        els.tree.appendChild(empty);
        return;
    }
    tree.forEach(node => els.tree.appendChild(createTreeNode(node)));
}

function renderReader(data) {
    els.readerTitle.textContent = data?.label || '阅读区';
    els.readerMeta.textContent = data?.meta || '关闭实时模式后，可点击左侧历史内容自由浏览。';
    els.readerContent.className = `auto-reader-content ${getReaderClass(data?.type || '')}`.trim();
    els.readerContent.textContent = data?.content || '暂无内容';
}

function renderLive(project, liveStage) {
    const liveText = liveStage?.text || '';
    const liveLabel = liveStage?.label || project?.current_stage || '暂无实时阶段';
    const liveUpdatedAt = liveStage?.updated_at || project?.updated_at || '未知';

    els.liveMeta.textContent = `${liveLabel} · 更新于 ${liveUpdatedAt}`;
    els.liveContent.textContent = liveText;
    els.livePlaceholder.style.display = liveText ? 'none' : 'block';
    els.livePlaceholder.textContent = project?.running
        ? '项目仍在运行中，当前阶段暂未产出新的可显示文本。'
        : '当前没有运行中的实时文本。';

    if (state.realtime) {
        renderReader({
            label: `${liveLabel}（实时）`,
            meta: `项目 ${project?.display_name || project?.name || ''} · ${liveUpdatedAt}`,
            type: liveStage?.stage_type || 'live',
            content: liveText || '等待新的实时文本输出…',
        });
    }
}

async function loadTree() {
    const project = getSelectedProject();
    if (!project) {
        state.tree = [];
        renderTree([]);
        return;
    }
    const data = await fetchJson(`${window._env_?.SERVER_URL}/auto_novel/projects/${encodeURIComponent(project.id)}/tree`);
    state.tree = data.tree || [];
    renderTree(state.tree);
}

async function openHistoryFile(path) {
    const project = getSelectedProject();
    if (!project || !path) {
        return;
    }
    const data = await fetchJson(`${window._env_?.SERVER_URL}/auto_novel/projects/${encodeURIComponent(project.id)}/content?path=${encodeURIComponent(path)}`);
    state.selectedPath = data.path;
    renderTree(state.tree);
    renderReader({
        label: data.label,
        meta: `${data.path} · ${data.updated_at}${data.truncated ? ' · 已截断显示' : ''}`,
        type: data.type,
        content: data.content,
    });
}

async function openDefaultHistoryFile() {
    const path = findDefaultPath(state.tree);
    if (path) {
        await openHistoryFile(path);
        return;
    }
    renderReader({
        label: '阅读区',
        meta: '当前项目还没有可浏览文本。',
        type: 'text',
        content: '当前项目还没有可浏览文本。',
    });
}

async function refreshLive() {
    if (!state.visible || !state.selectedProjectId || state.liveBusy) {
        return;
    }
    state.liveBusy = true;
    try {
        const payload = await fetchJson(`${window._env_?.SERVER_URL}/auto_novel/projects/${encodeURIComponent(state.selectedProjectId)}/live`);
        const project = payload.project || null;
        if (project) {
            state.projects = state.projects.map(item => item.id === project.id ? project : item);
            renderProjects();
            renderStatus(project);
            setToolbarState(project);
        }
        renderLive(project, payload.live_stage || {});
    } catch (error) {
        console.error(error);
    } finally {
        state.liveBusy = false;
    }
}

async function refreshProjects({ forceContextReload = false } = {}) {
    if (state.projectBusy) {
        return;
    }
    state.projectBusy = true;
    try {
        const data = await fetchJson(`${window._env_?.SERVER_URL}/auto_novel/projects`);
        state.projects = data.projects || [];
        state.defaults = data.defaults || state.defaults;
        ensureCreateFormDefaults();

        const hasSelection = state.projects.some(project => project.id === state.selectedProjectId);
        if (!hasSelection) {
            state.selectedProjectId = state.projects[0]?.id || '';
            state.selectedPath = '';
        }

        renderProjects();

        const selected = getSelectedProject();
        renderStatus(selected);
        setToolbarState(selected);

        if (selected && (forceContextReload || state.visible)) {
            await loadTree();
            if (!state.realtime && !state.selectedPath) {
                await openDefaultHistoryFile();
            }
        }

        if (!selected) {
            state.tree = [];
            renderTree([]);
            renderReader({
                label: '阅读区',
                meta: '关闭实时模式后，可点击左侧历史内容自由浏览。',
                type: 'text',
                content: '还没有项目。先在左侧创建一个。',
            });
        }
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        state.projectBusy = false;
    }
}

function stopPolling() {
    if (state.projectPollTimer) {
        clearInterval(state.projectPollTimer);
        state.projectPollTimer = null;
    }
    if (state.livePollTimer) {
        clearInterval(state.livePollTimer);
        state.livePollTimer = null;
    }
}

function startPolling() {
    stopPolling();
    state.projectPollTimer = setInterval(() => refreshProjects({ forceContextReload: false }), 8000);
    state.livePollTimer = setInterval(() => refreshLive(), 1500);
}

async function selectProject(projectId) {
    if (!projectId) {
        return;
    }
    state.selectedProjectId = projectId;
    state.selectedPath = '';
    renderProjects();
    await loadTree();
    await refreshLive();
    if (!state.realtime) {
        await openDefaultHistoryFile();
    }
}

function setRealtime(enabled) {
    state.realtime = enabled;
    els.realtime.checked = enabled;
    if (enabled) {
        state.selectedPath = '';
        renderTree(state.tree);
        refreshLive();
        return;
    }
    openDefaultHistoryFile().catch(error => {
        console.error(error);
    });
}

async function handleCreateProject() {
    const briefText = els.projectBrief.value.trim();
    if (!briefText) {
        showToast('请先填写项目设定', 'warning');
        return;
    }

    try {
        const payload = await postJson(`${window._env_?.SERVER_URL}/auto_novel/projects`, {
            project_name: els.projectName.value.trim(),
            brief_text: briefText,
            target_chars: Number(els.targetChars.value || 0),
            chapter_char_target: Number(els.chapterChars.value || 0),
            main_model: els.mainModel.value.trim(),
            sub_model: els.subModel.value.trim(),
            auto_start: els.startImmediately.checked,
        });
        showToast('项目创建成功', 'success');
        els.projectName.value = '';
        state.selectedProjectId = payload.id;
        state.selectedPath = '';
        await refreshProjects({ forceContextReload: true });
        await selectProject(payload.id);
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function handleContinueProject() {
    const project = getSelectedProject();
    if (!project) {
        showToast('请先选择项目', 'warning');
        return;
    }
    try {
        await postJson(`${window._env_?.SERVER_URL}/auto_novel/projects/${encodeURIComponent(project.id)}/continue`);
        showToast('已发送继续创作指令', 'success');
        await refreshProjects({ forceContextReload: true });
        await refreshLive();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function handleStopProject() {
    const project = getSelectedProject();
    if (!project) {
        showToast('请先选择项目', 'warning');
        return;
    }
    try {
        await postJson(`${window._env_?.SERVER_URL}/auto_novel/projects/${encodeURIComponent(project.id)}/stop`);
        showToast('已尝试停止项目', 'success');
        await refreshProjects({ forceContextReload: true });
        await refreshLive();
    } catch (error) {
        showToast(error.message, 'error');
    }
}

async function show() {
    state.visible = true;
    els.section.style.display = 'flex';
    startPolling();
    await refreshProjects({ forceContextReload: true });
    await refreshLive();
}

function hide() {
    state.visible = false;
    els.section.style.display = 'none';
    stopPolling();
}

function bindEvents() {
    document.querySelectorAll('.auto-refresh-btn').forEach(button => {
        button.addEventListener('click', async () => {
            await refreshProjects({ forceContextReload: true });
            await refreshLive();
        });
    });
    els.continueBtn.addEventListener('click', handleContinueProject);
    els.stopBtn.addEventListener('click', handleStopProject);
    els.createBtn.addEventListener('click', handleCreateProject);
    els.realtime.addEventListener('change', event => {
        setRealtime(event.target.checked);
    });
}

document.addEventListener('DOMContentLoaded', () => {
    els.section = byId('autoNovelSection');
    els.realtime = byId('autoNovelRealtime');
    els.currentProject = document.querySelector('.auto-current-project');
    els.continueBtn = document.querySelector('.auto-continue-btn');
    els.stopBtn = document.querySelector('.auto-stop-btn');
    els.projectName = byId('autoProjectName');
    els.projectBrief = byId('autoProjectBrief');
    els.targetChars = byId('autoTargetChars');
    els.chapterChars = byId('autoChapterChars');
    els.mainModel = byId('autoMainModel');
    els.subModel = byId('autoSubModel');
    els.startImmediately = byId('autoStartImmediately');
    els.createBtn = document.querySelector('.auto-create-btn');
    els.projectList = document.querySelector('.auto-project-list');
    els.tree = document.querySelector('.auto-tree');
    els.statusGrid = document.querySelector('.auto-status-grid');
    els.statusError = document.querySelector('.auto-status-error');
    els.liveMeta = document.querySelector('.auto-live-meta');
    els.livePlaceholder = document.querySelector('.auto-live-placeholder');
    els.liveContent = document.querySelector('.auto-live-content');
    els.readerTitle = document.querySelector('.auto-reader-title');
    els.readerMeta = document.querySelector('.auto-reader-meta');
    els.readerContent = document.querySelector('.auto-reader-content');

    bindEvents();

    window.autoNovelGui = {
        show,
        hide,
        refresh: () => refreshProjects({ forceContextReload: true }),
    };
});

const inlineMarkup = /(\*\*[^*\n]+\*\*|__[^_\n]+__|\x60[^\x60\n]+\x60)/g;
const listItem = /^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/;

function appendInline(parent, text) {
    let offset = 0;
    for (const match of text.matchAll(inlineMarkup)) {
        parent.append(text.slice(offset, match.index));
        const token = match[0];
        const isCode = token.charCodeAt(0) === 96;
        const node = document.createElement(isCode ? 'code' : 'strong');
        node.textContent = isCode ? token.slice(1, -1) : token.slice(2, -2);
        parent.append(node);
        offset = match.index + token.length;
    }
    parent.append(text.slice(offset));
}

function tableCells(line) {
    return line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
}

function isTableRule(line) {
    return Boolean(line?.includes('|') && tableCells(line).every(cell => /^:?-{3,}:?$/.test(cell)));
}

function appendTable(parent, headers, rows) {
    const wrapper = document.createElement('div');
    wrapper.className = 'table-scroll';
    const table = document.createElement('table');
    const header = table.createTHead().insertRow();
    for (const value of headers) {
        const cell = document.createElement('th');
        cell.scope = 'col';
        appendInline(cell, value);
        header.append(cell);
    }
    const body = table.createTBody();
    for (const row of rows) {
        const element = body.insertRow();
        headers.forEach((_, index) => appendInline(element.insertCell(), row[index] || ''));
    }
    wrapper.append(table);
    parent.append(wrapper);
}

export function renderDocument(container, text, type) {
    container.replaceChildren();
    if (!text.trim()) {
        const empty = document.createElement('p');
        empty.className = 'muted';
        empty.textContent = '这个文件还没有内容。';
        container.append(empty);
        return;
    }

    if (['state', 'config', 'json', 'live', 'runner_log', 'watchdog_log', 'log'].includes(type)) {
        const pre = document.createElement('pre');
        pre.textContent = text;
        container.append(pre);
        return;
    }

    const output = document.createDocumentFragment();
    const lines = text.trim().split(/\r?\n/);
    if (type === 'draft' && /^(?:#+\s*)?(?:第\s*[\d一二三四五六七八九十百千〇零两]+\s*章|chapter\s+\d+\b)/i.test(lines[0])) {
        lines.shift();
    }

    if (type === 'draft' || type === 'manuscript') {
        for (const line of lines) {
            if (!line.trim()) continue;
            const paragraph = document.createElement('p');
            paragraph.textContent = line;
            output.append(paragraph);
        }
        container.append(output);
        return;
    }

    let list = null;
    let paragraph = [];
    let code = null;
    const isFence = value => /^(?:\x60{3}|~{3})/.test(value);
    const flushParagraph = () => {
        if (!paragraph.length) return;
        const node = document.createElement('p');
        node.className = 'line-breaks';
        appendInline(node, paragraph.join('\n'));
        output.append(node);
        paragraph = [];
    };
    const flushCode = () => {
        const node = document.createElement('pre');
        node.textContent = code.join('\n');
        output.append(node);
        code = null;
    };

    for (let index = 0; index < lines.length; index++) {
        const line = lines[index].trim();
        if (code) {
            if (isFence(line)) flushCode();
            else code.push(lines[index]);
            continue;
        }

        const item = line.match(listItem);
        if (item) {
            flushParagraph();
            const tag = item[2] ? 'ol' : 'ul';
            if (list?.localName !== tag) {
                list = document.createElement(tag);
                if (item[2]) list.start = Number(item[2]);
                output.append(list);
            }
            const node = document.createElement('li');
            appendInline(node, item[3]);
            list.append(node);
            continue;
        }

        list = null;
        const heading = line.match(/^(#{1,6})\s+(.+)$/);
        if (heading) {
            flushParagraph();
            const node = document.createElement('h' + Math.min(heading[1].length + 2, 6));
            appendInline(node, heading[2]);
            output.append(node);
        } else if (isFence(line)) {
            flushParagraph();
            code = [];
        } else if (/^(?:-{3,}|\*{3,}|_{3,})$/.test(line)) {
            flushParagraph();
            output.append(document.createElement('hr'));
        } else if (line.startsWith('>')) {
            flushParagraph();
            const quote = document.createElement('blockquote');
            const quoteLines = [line.replace(/^>\s?/, '')];
            while (lines[index + 1]?.trim().startsWith('>')) {
                quoteLines.push(lines[++index].trim().replace(/^>\s?/, ''));
            }
            appendInline(quote, quoteLines.join('\n'));
            output.append(quote);
        } else if (line.includes('|') && isTableRule(lines[index + 1])) {
            flushParagraph();
            const rows = [];
            index++;
            while (lines[index + 1]?.includes('|')) {
                rows.push(tableCells(lines[++index]));
            }
            appendTable(output, tableCells(line), rows);
        } else if (line) {
            paragraph.push(line);
        } else {
            flushParagraph();
        }
    }

    if (code) flushCode();
    flushParagraph();
    container.append(output);
}

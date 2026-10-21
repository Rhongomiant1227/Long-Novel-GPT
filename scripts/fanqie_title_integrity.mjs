import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const MAX_TITLE_LENGTH = 10;
export const MIN_READER_TITLE_LENGTH = 4;
export const MIN_BODY_CHARS = 1000;

const META_TERMS = [
  "尾声",
  "合章",
  "收束",
  "结局",
  "后日谈",
  "普通日",
  "是否继续",
];
const ADMIN_TERMS = [
  "回执",
  "归档",
  "样本",
  "验证",
  "存续",
  "签发",
  "执行",
  "裁定",
  "留痕",
  "协议",
  "权限",
  "入口",
  "模板",
  "方案",
  "编号",
  "字段",
  "定义权",
  "连续性",
  "终判",
  "复核",
  "基线",
  "终裁",
];
const HUMAN_SIGNALS = [
  "她",
  "他",
  "我",
  "你",
  "谁",
  "别",
  "不",
  "还",
  "竟",
  "原来",
  "真的",
  "终于",
  "先",
  "只",
  "来",
  "醒",
  "死",
  "活",
  "说",
  "问",
  "签",
  "按",
  "看",
  "拿",
  "走",
  "回",
  "开",
  "关",
  "杀",
  "救",
];
const MOJIBAKE_RE = /(?:\uFFFD|Ã|Â|â|ä¸|ï¼|ã€|ðŸ)/u;
const PUNCTUATION_RE = /[0-9A-Za-z，。！？；：、,.!?;:“”"'‘’（）()【】[\]《》<>+·—-]/u;
const HEADING_RE = /^第\s*(\d+)\s*章(?:\s+|[：:，,、.．。]\s*)(.+)$/u;

export function compactText(value) {
  return String(value ?? "").replace(/[\s\uFEFF\u200B-\u200D]+/gu, "");
}

export function normalizeTitle(value) {
  return String(value ?? "")
    .replace(/[\uFEFF\u200B-\u200D]/gu, "")
    .replace(/\s+/gu, " ")
    .trim();
}

export function bodySha256(value) {
  return crypto.createHash("sha256").update(compactText(value), "utf8").digest("hex");
}

export function parseChapterFile(content, filePath = "") {
  const lines = String(content ?? "").replace(/\r\n?/gu, "\n").split("\n");
  const firstNonEmptyIndex = lines.findIndex((line) => normalizeTitle(line));
  const heading = firstNonEmptyIndex >= 0 ? normalizeTitle(lines[firstNonEmptyIndex]) : "";
  const match = HEADING_RE.exec(heading);
  if (!match) {
    throw new Error(`章节标题格式无效: ${filePath || "<memory>"}；首行=${JSON.stringify(heading)}`);
  }
  const number = Number(match[1]);
  const title = normalizeTitle(match[2]);
  const body = lines.slice(firstNonEmptyIndex + 1).join("\n").trim();
  return {
    number,
    title,
    body,
    heading,
    bodyChars: compactText(body).length,
    bodySha256: bodySha256(body),
  };
}

export function titleQualityIssues(title, { strictReaderFacing = true } = {}) {
  const normalized = normalizeTitle(title);
  const issues = [];
  const length = Array.from(normalized).length;
  if (!normalized) {
    return ["标题为空"];
  }
  if (length > MAX_TITLE_LENGTH) {
    issues.push(`标题超过${MAX_TITLE_LENGTH}字`);
  }
  if (MOJIBAKE_RE.test(normalized)) {
    issues.push("标题疑似乱码");
  }
  if (PUNCTUATION_RE.test(normalized)) {
    issues.push("标题含数字、字母或标点");
  }
  if (!strictReaderFacing) {
    return issues;
  }
  if (length < MIN_READER_TITLE_LENGTH) {
    issues.push(`标题少于${MIN_READER_TITLE_LENGTH}字`);
  }
  const metaHits = META_TERMS.filter((term) => normalized.includes(term));
  if (metaHits.length) {
    issues.push(`标题含写作提纲词:${metaHits.join("/")}`);
  }
  const adminHits = ADMIN_TERMS.filter((term) => normalized.includes(term));
  const hasHumanSignal = HUMAN_SIGNALS.some((term) => normalized.includes(term));
  if (adminHits.length >= 2 || (adminHits.length && !hasHumanSignal)) {
    issues.push(`标题后台术语过重:${adminHits.join("/")}`);
  }
  return issues;
}

function readJson(filePath, fallback) {
  if (!fs.existsSync(filePath)) {
    return fallback;
  }
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

export function auditLocalProject({
  chaptersDir,
  selectedNumbers = [],
  strictReaderFacing = true,
}) {
  const resolvedChaptersDir = path.resolve(chaptersDir);
  const projectDir = path.resolve(resolvedChaptersDir, "..", "..");
  const statePath = path.join(projectDir, "state.json");
  const manifestPath = path.join(resolvedChaptersDir, "chapters_manifest.json");
  const state = readJson(statePath, {});
  const manifest = readJson(manifestPath, []);
  const stateTitles = new Map(
    [...(state.completed_chapters ?? []), ...(state.pending_chapters ?? [])]
      .filter((item) => item && Number.isInteger(Number(item.chapter_number)))
      .map((item) => [Number(item.chapter_number), normalizeTitle(item.title)]),
  );
  const manifestHeadings = new Map(
    (Array.isArray(manifest) ? manifest : [])
      .filter((item) => item && Number.isInteger(Number(item.output_number)))
      .map((item) => [Number(item.output_number), normalizeTitle(item.heading_line)]),
  );
  const selected = new Set(selectedNumbers.map(Number));
  const files = fs.readdirSync(resolvedChaptersDir)
    .filter((name) => /^ch_\d{4}\.txt$/iu.test(name))
    .sort((left, right) => left.localeCompare(right, "en"));
  const rows = [];
  const titleNumbers = new Map();
  const numberSet = new Set();

  for (const name of files) {
    const fileNumber = Number(name.match(/\d+/u)?.[0] ?? 0);
    const filePath = path.join(resolvedChaptersDir, name);
    const parsed = parseChapterFile(fs.readFileSync(filePath, "utf8"), filePath);
    const issues = titleQualityIssues(parsed.title, { strictReaderFacing });
    if (parsed.number !== fileNumber) {
      issues.push(`文件名与首行章号不一致:${fileNumber}/${parsed.number}`);
    }
    if (stateTitles.has(fileNumber) && stateTitles.get(fileNumber) !== parsed.title) {
      issues.push("state.json 标题不一致");
    }
    if (manifestHeadings.has(fileNumber) && manifestHeadings.get(fileNumber) !== parsed.heading) {
      issues.push("chapters_manifest.json 标题不一致");
    }
    if (parsed.bodyChars < MIN_BODY_CHARS) {
      issues.push(`正文疑似不完整:${parsed.bodyChars}/${MIN_BODY_CHARS}`);
    }
    if (!titleNumbers.has(parsed.title)) {
      titleNumbers.set(parsed.title, []);
    }
    titleNumbers.get(parsed.title).push(fileNumber);
    numberSet.add(fileNumber);
    rows.push({
      ...parsed,
      filePath,
      fileNumber,
      selected: selected.has(fileNumber),
      issues,
    });
  }

  for (const row of rows) {
    const duplicates = titleNumbers.get(row.title) ?? [];
    if (row.title && duplicates.length > 1) {
      row.issues.push(`标题重复:${duplicates.join("/")}`);
    }
  }

  const selectedRows = rows.filter((row) => row.selected);
  const missingSelected = [...selected].filter((number) => !numberSet.has(number));
  return {
    projectDir,
    chaptersDir: resolvedChaptersDir,
    statePath,
    manifestPath,
    rows,
    selectedRows,
    missingSelected,
    errors: selectedRows.flatMap((row) =>
      row.issues.map((issue) => `第${row.fileNumber}章 ${issue}`),
    ),
  };
}

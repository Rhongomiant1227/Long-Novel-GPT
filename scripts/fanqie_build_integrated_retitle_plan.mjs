import crypto from "node:crypto";
import fs from "node:fs";
import https from "node:https";
import path from "node:path";
import process from "node:process";

import { titleQualityIssues } from "./fanqie_title_integrity.mjs";

const DEFAULT_BASE_URL = "https://fast.sbbbbbbbbb.xyz/v1";
const DEFAULT_MODEL = "grok-4.5";
const DEFAULT_BATCH_SIZE = 10;
const DEFAULT_CONCURRENCY = 3;
const HARD_TITLE_LIMIT = 10;
const PREFERRED_MIN_TITLE_LENGTH = 4;
const PREFERRED_MAX_TITLE_LENGTH = 8;
const BANNED_META_TITLE_PARTS = [
  "尾声",
  "合章",
  "收束",
  "结局",
  "后日谈",
  "普通日",
  "命运落点",
  "是否继续",
];

function printHelp() {
  console.log(`Build a body-linked retitle plan for an exported Fanqie novel.

Usage:
  node scripts/fanqie_build_integrated_retitle_plan.mjs \
    --input external/fanqie_snapshots/book.json \
    --output external/fanqie_snapshots/book_retitle_plan.json

Environment:
  FANQIE_RETITLE_API_KEY   Required API key
  FANQIE_RETITLE_BASE_URL Optional OpenAI-compatible base URL
  FANQIE_RETITLE_MODEL    Optional model, default grok-4.5

Options:
  --input <path>          Complete remote chapter snapshot
  --output <path>         Body-linked retitle plan
  --batch-size <n>        Chapters per request, default 10
  --concurrency <n>       Concurrent requests, default 3
  --from <n>              First chapter, default 1
  --to <n>                Last chapter, default latest
  --suspects-only         Deep-review only deterministic high-risk titles
  --human-style           Replace selected titles with reader-facing titles
  --excerpt-chars <n>     Use first and last n body characters per chapter
  --force                 Re-audit completed chapters
  --help                  Show help
`);
}

function parseArgs(argv) {
  const args = {
    inputPath: "",
    outputPath: "",
    batchSize: DEFAULT_BATCH_SIZE,
    concurrency: DEFAULT_CONCURRENCY,
    from: 1,
    to: Number.MAX_SAFE_INTEGER,
    suspectsOnly: false,
    humanStyle: false,
    excerptChars: 0,
    force: false,
    help: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = () => {
      if (index + 1 >= argv.length) {
        throw new Error(`missing value for ${arg}`);
      }
      index += 1;
      return argv[index];
    };

    switch (arg) {
      case "--input":
        args.inputPath = path.resolve(next());
        break;
      case "--output":
        args.outputPath = path.resolve(next());
        break;
      case "--batch-size":
        args.batchSize = Number(next());
        break;
      case "--concurrency":
        args.concurrency = Number(next());
        break;
      case "--from":
        args.from = Number(next());
        break;
      case "--to":
        args.to = Number(next());
        break;
      case "--suspects-only":
        args.suspectsOnly = true;
        break;
      case "--human-style":
        args.humanStyle = true;
        break;
      case "--excerpt-chars":
        args.excerptChars = Number(next());
        break;
      case "--force":
        args.force = true;
        break;
      case "--help":
      case "-h":
        args.help = true;
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  return args;
}

function validateArgs(args) {
  if (args.help) {
    return;
  }
  if (!args.inputPath) {
    throw new Error("--input is required");
  }
  if (!args.outputPath) {
    throw new Error("--output is required");
  }
  if (!Number.isInteger(args.batchSize) || args.batchSize <= 0 || args.batchSize > 20) {
    throw new Error(`invalid --batch-size: ${args.batchSize}`);
  }
  if (!Number.isInteger(args.concurrency) || args.concurrency <= 0 || args.concurrency > 8) {
    throw new Error(`invalid --concurrency: ${args.concurrency}`);
  }
  if (!Number.isInteger(args.from) || args.from <= 0) {
    throw new Error(`invalid --from: ${args.from}`);
  }
  if (!Number.isInteger(args.to) || args.to < args.from) {
    throw new Error(`invalid --to: ${args.to}`);
  }
  if (
    !Number.isInteger(args.excerptChars) ||
    args.excerptChars < 0 ||
    args.excerptChars > 5000
  ) {
    throw new Error(`invalid --excerpt-chars: ${args.excerptChars}`);
  }
}

function normalizeText(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function titleLength(value) {
  return Array.from(normalizeText(value)).length;
}

function normalizeTitle(value) {
  return normalizeText(value)
    .replace(/^第\s*\d+\s*章\s*/u, "")
    .replace(/^《|》$/gu, "")
    .trim();
}

function bodyHash(body) {
  return crypto.createHash("sha256").update(String(body ?? ""), "utf8").digest("hex");
}

function validateTitle(title) {
  const normalized = normalizeTitle(title);
  return titleQualityIssues(normalized, { strictReaderFacing: true });
}

function isHighRiskTitle(chapter) {
  const title = normalizeTitle(chapter.chapterTitle);
  const length = titleLength(title);
  const genericTitles = new Set([
    "定义",
    "规则",
    "存活",
    "续生",
    "落款",
    "终判",
    "活法",
    "驳回",
    "签章",
    "执行期",
    "归档期",
    "长周期",
    "方案体",
    "模板名",
    "回应权",
  ]);
  return (
    validateTitle(title).length > 0 ||
    /[A-Za-z0-9?？+\-]/u.test(title) ||
    /^(的|了|和|与|或|把|将|为|在|从|向|由谁)/u.test(title) ||
    /(是否|能不能|会不会|如何|愿不愿意|为什么)/u.test(title) ||
    /(尾声|合章|收束|后日谈|命运落点|普通一?天|普通日)/u.test(title) ||
    /(靠爱与电池|今日续签|明日续签|明日留空)/u.test(title) ||
    genericTitles.has(title)
  );
}

function chunk(values, size) {
  const batches = [];
  for (let index = 0; index < values.length; index += size) {
    batches.push(values.slice(index, index + size));
  }
  return batches;
}

function extractJsonArray(text) {
  const trimmed = String(text ?? "").trim();
  const unfenced = trimmed
    .replace(/^```(?:json)?\s*/i, "")
    .replace(/\s*```$/i, "")
    .trim();
  try {
    const parsed = JSON.parse(unfenced);
    if (Array.isArray(parsed)) {
      return parsed;
    }
    if (Array.isArray(parsed?.chapters)) {
      return parsed.chapters;
    }
  } catch {
    // Fall through to the first complete-looking JSON array.
  }

  const firstBracket = unfenced.indexOf("[");
  const lastBracket = unfenced.lastIndexOf("]");
  if (firstBracket >= 0 && lastBracket > firstBracket) {
    const parsed = JSON.parse(unfenced.slice(firstBracket, lastBracket + 1));
    if (Array.isArray(parsed)) {
      return parsed;
    }
  }
  throw new Error(`model response did not contain a JSON array: ${trimmed.slice(0, 500)}`);
}

function requestJson(url, payload, apiKey, timeoutMilliseconds = 360000) {
  const target = new URL(url);
  const body = JSON.stringify(payload);
  return new Promise((resolve, reject) => {
    const request = https.request(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port || undefined,
        path: `${target.pathname}${target.search}`,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
          Authorization: `Bearer ${apiKey}`,
        },
        timeout: timeoutMilliseconds,
      },
      (response) => {
        let raw = "";
        response.setEncoding("utf8");
        response.on("data", (part) => {
          raw += part;
        });
        response.on("end", () => {
          if ((response.statusCode ?? 500) < 200 || (response.statusCode ?? 500) >= 300) {
            reject(
              new Error(
                `HTTP ${response.statusCode}: ${raw.slice(0, 1000)}`,
              ),
            );
            return;
          }
          try {
            resolve(JSON.parse(raw));
          } catch (error) {
            reject(new Error(`invalid API JSON: ${error.message}; ${raw.slice(0, 1000)}`));
          }
        });
      },
    );
    request.on("timeout", () => request.destroy(new Error("request timeout")));
    request.on("error", reject);
    request.write(body);
    request.end();
  });
}

async function requestWithRetry(url, payload, apiKey) {
  let lastError = null;
  for (let attempt = 1; attempt <= 5; attempt += 1) {
    try {
      return await requestJson(url, payload, apiKey);
    } catch (error) {
      lastError = error;
      if (attempt < 5) {
        await new Promise((resolve) => setTimeout(resolve, attempt * 10000));
      }
    }
  }
  throw lastError;
}

function excerptBody(body, maxChars) {
  const text = String(body ?? "").trim();
  if (!maxChars || Array.from(text).length <= maxChars * 2) {
    return text;
  }
  const chars = Array.from(text);
  return [
    chars.slice(0, maxChars).join(""),
    "……（正文中段省略，标题仍需兼顾下方结尾）……",
    chars.slice(-maxChars).join(""),
  ].join("\n\n");
}

function buildPrompt(batch, reservedTitles, options = {}) {
  const chapters = batch.map((chapter) => ({
    chapterNumber: chapter.chapterNumber,
    currentTitle: chapter.chapterTitle,
    body: excerptBody(chapter.body, options.excerptChars ?? 0),
  }));
  if (options.humanStyle) {
    return `你是中文商业连载小说的责任编辑。下面每项包含章节号、当前标题，以及该章正文开头和结尾。必须依据正文重新起标题，不能只给旧标题换同义词。

本轮专门解决“标题像AI或后台日志”的问题：
1. 每章重新起一个读者愿意点开的标题。优先抓本章独有的危险、秘密、动作、人物冲突、关键对白或情绪反差。
2. 标题允许4-10个汉字自然变化，不追求整齐四字格。硬上限10个字符，不能为空，不带“第N章”，尽量不用标点。
3. 禁止公文和后台词汇堆砌。除非是本章不可替代的核心物件，否则避免：回执、归档、样本、验证、存续、签发、执行、裁定、留痕、协议、权限、入口、模板、方案、编号、字段、定义权、连续性。
4. 禁止“尾声、合章、收束、结局、后日谈、普通日、是否继续”等写作提纲词。
5. 不要整批使用同一句式。不能全部以“她”开头，不能全部使用“谁”，也不能全部是四字名词。对白式、动作式、悬念式、人物关系式要交替出现。
6. 可以口语化，可以有情绪，可以像真人作者故意留钩子，但不能与正文不符。
7. 本批标题彼此不得重复，也不得与已占用标题重复。
8. currentTitle只帮助理解原意，默认decision为replace。
9. 只返回JSON数组，不要Markdown或解释性前后文。每项严格为：
{"chapterNumber":1,"finalTitle":"标题","decision":"replace","reason":"指出正文对应的具体动作、冲突或对白"}

已占用标题：
${JSON.stringify(reservedTitles)}

待审章节：
${JSON.stringify(chapters)}`;
  }
  return `你是中文商业长篇小说的章节编辑。下面每项都包含章节号、线上当前标题和完整正文。必须逐章通读正文后判断标题，标题不能脱离正文单独处理。

目标：
1. 为每章返回最终标题。当前标题真正准确、具体、自然时可以保留；不准确、抽象、像写作指令、像一句被截断的话、与相邻章节同质化时必须替换。
2. 最终标题优先4-8个汉字，硬上限10个字符，不能为空，不带标点，不带“第N章”。
3. 标题应抓住本章独有的行动、物件、冲突或转折，读者只看标题也能区分章节。禁止只用“定义、规则、存活、续生、落款、终判、执行期、归档期、尾声、合章、收束、结局、后日谈、普通日、是否继续”等抽象词或元叙事词。
4. 不要为了显得文艺写成完整句子，不要把正文金句机械截前十字，不要连续复用“某某回执、某某样本、某某留痕、某某续签”等同一模板。
5. 本批标题彼此不得重复，也不得与已占用标题重复。
6. 只返回JSON数组，不要Markdown，不要解释性前后文。每项格式严格为：
{"chapterNumber":1,"finalTitle":"标题","decision":"keep或replace","reason":"一句具体理由"}

已占用标题：
${JSON.stringify(reservedTitles)}

待审完整章节：
${JSON.stringify(chapters)}`;
}

function createOutput(snapshot, existingOutput) {
  const priorByNumber = new Map(
    (existingOutput?.chapters ?? []).map((chapter) => [chapter.chapterNumber, chapter]),
  );
  return {
    schemaVersion: 1,
    project: snapshot.project,
    bookTitle: snapshot.bookTitle,
    bookId: snapshot.bookId,
    sourceSnapshot: "",
    generatedAt: existingOutput?.generatedAt ?? "",
    model: existingOutput?.model ?? "",
    baseUrl: existingOutput?.baseUrl ?? "",
    preferredTitleLength: {
      min: PREFERRED_MIN_TITLE_LENGTH,
      max: PREFERRED_MAX_TITLE_LENGTH,
      hardMax: HARD_TITLE_LIMIT,
    },
    chapters: (snapshot.chapters ?? []).map((chapter) => {
      const hash = bodyHash(chapter.body);
      const prior = priorByNumber.get(chapter.chapterNumber);
      if (prior?.bodySha256 === hash && prior?.audit?.finalTitle) {
        return prior;
      }
      return {
        ...chapter,
        bodySha256: hash,
        audit: null,
      };
    }),
  };
}

function writeOutput(outputPath, value) {
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  fs.writeFileSync(outputPath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

async function auditBatch({
  batch,
  batchIndex,
  batchCount,
  endpoint,
  apiKey,
  model,
  reservedTitles,
  humanStyle,
  excerptChars,
}) {
  console.log(
    `[${batchIndex + 1}/${batchCount}] audit chapters ${batch[0].chapterNumber}-${batch.at(-1).chapterNumber}`,
  );
  const response = await requestWithRetry(
    endpoint,
    {
      model,
      messages: [
        {
          role: "system",
          content:
            "用中文工作。你是严格的连载小说章节编辑，只能在完整阅读每章正文后给标题。输出必须是可解析JSON。",
        },
        {
          role: "user",
          content: buildPrompt(batch, reservedTitles, {
            humanStyle,
            excerptChars,
          }),
        },
      ],
      temperature: humanStyle ? 0.55 : 0.2,
      max_tokens: 6000,
      stream: false,
    },
    apiKey,
  );
  const content = response?.choices?.[0]?.message?.content ?? "";
  const records = extractJsonArray(content);
  const expected = new Set(batch.map((chapter) => chapter.chapterNumber));
  const byNumber = new Map();

  for (const record of records) {
    const chapterNumber = Number(record?.chapterNumber);
    if (!expected.has(chapterNumber) || byNumber.has(chapterNumber)) {
      continue;
    }
    const finalTitle = normalizeTitle(record?.finalTitle);
    byNumber.set(chapterNumber, {
      finalTitle,
      decision: normalizeText(record?.decision) === "keep" ? "keep" : "replace",
      reason: normalizeText(record?.reason),
      validationErrors: validateTitle(finalTitle),
    });
  }

  if (byNumber.size !== batch.length) {
    const missing = batch
      .map((chapter) => chapter.chapterNumber)
      .filter((chapterNumber) => !byNumber.has(chapterNumber));
    throw new Error(`model omitted chapters: ${missing.join(", ")}`);
  }
  return byNumber;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    printHelp();
    return;
  }
  validateArgs(args);

  const apiKey = normalizeText(process.env.FANQIE_RETITLE_API_KEY);
  if (!apiKey) {
    throw new Error("FANQIE_RETITLE_API_KEY is required");
  }
  const baseUrl = normalizeText(process.env.FANQIE_RETITLE_BASE_URL) || DEFAULT_BASE_URL;
  const model = normalizeText(process.env.FANQIE_RETITLE_MODEL) || DEFAULT_MODEL;
  const endpoint = `${baseUrl.replace(/\/+$/, "")}/chat/completions`;

  const snapshot = JSON.parse(fs.readFileSync(args.inputPath, "utf8"));
  const existingOutput =
    !args.force && fs.existsSync(args.outputPath)
      ? JSON.parse(fs.readFileSync(args.outputPath, "utf8"))
      : null;
  const output = createOutput(snapshot, existingOutput);
  output.sourceSnapshot = args.inputPath;
  output.model = model;
  output.baseUrl = baseUrl;
  output.reservedTitles = Array.from(
    new Set((snapshot.reservedTitles ?? []).map(normalizeTitle).filter(Boolean)),
  );

  if (args.suspectsOnly) {
    for (const chapter of output.chapters) {
      if (
        chapter.chapterNumber < args.from ||
        chapter.chapterNumber > args.to ||
        chapter.audit?.finalTitle ||
        isHighRiskTitle(chapter)
      ) {
        continue;
      }
      chapter.audit = {
        finalTitle: normalizeTitle(chapter.chapterTitle),
        decision: "keep",
        reason: "标题通过全量格式、长度、元标题和模板化风险校验，整章正文保留在本记录中供回查",
        validationErrors: [],
        auditMode: "deterministic_keep",
      };
    }
  }

  const selected = output.chapters.filter(
    (chapter) =>
      chapter.chapterNumber >= args.from &&
      chapter.chapterNumber <= args.to &&
      (!chapter.audit?.finalTitle || args.force) &&
      (!args.suspectsOnly || isHighRiskTitle(chapter)),
  );
  const batches = chunk(selected, args.batchSize);
  if (!batches.length) {
    console.log("all selected chapters already audited");
  }

  let nextBatchIndex = 0;
  const completedResults = new Map();
  const claimedTitles = new Set(
    [
      ...output.reservedTitles,
      ...output.chapters.map((chapter) => normalizeTitle(chapter.chapterTitle)),
      ...output.chapters
        .filter((chapter) => chapter.audit?.finalTitle && !args.force)
        .map((chapter) => chapter.audit.finalTitle),
    ],
  );

  async function worker() {
    while (nextBatchIndex < batches.length) {
      const batchIndex = nextBatchIndex;
      nextBatchIndex += 1;
      const batch = batches[batchIndex];
      const reservedTitles = Array.from(claimedTitles);
      const result = await auditBatch({
        batch,
        batchIndex,
        batchCount: batches.length,
        endpoint,
        apiKey,
        model,
        reservedTitles,
        humanStyle: args.humanStyle,
        excerptChars: args.excerptChars,
      });
      completedResults.set(batchIndex, result);
      for (const chapter of batch) {
        const audit = result.get(chapter.chapterNumber);
        chapter.audit = audit;
        claimedTitles.add(audit.finalTitle);
      }
      output.generatedAt = new Date().toISOString();
      writeOutput(args.outputPath, output);
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(args.concurrency, batches.length) }, () => worker()),
  );

  const seen = new Map();
  const globalErrors = [];
  const reservedTitleSet = new Set(output.reservedTitles);
  for (const chapter of output.chapters) {
    const finalTitle = normalizeTitle(
      chapter.audit?.finalTitle || chapter.chapterTitle,
    );
    const errors = [...validateTitle(finalTitle)];
    if (reservedTitleSet.has(finalTitle)) {
      errors.push("duplicate_with_remote_history");
    }
    const previous = seen.get(finalTitle);
    if (previous) {
      errors.push(`duplicate_with:${previous}`);
    } else {
      seen.set(finalTitle, chapter.chapterNumber);
    }
    if (chapter.audit) {
      chapter.audit.validationErrors = errors;
    }
    if (errors.length) {
      globalErrors.push({
        chapterNumber: chapter.chapterNumber,
        title: finalTitle,
        errors,
      });
    }
  }
  output.validation = {
    auditedChapters: output.chapters.filter((chapter) => chapter.audit?.finalTitle).length,
    changedChapters: output.chapters.filter(
      (chapter) =>
        chapter.audit?.finalTitle &&
        chapter.audit.finalTitle !== normalizeTitle(chapter.chapterTitle),
    ).length,
    errors: globalErrors,
  };
  writeOutput(args.outputPath, output);
  console.log(JSON.stringify(output.validation, null, 2));
  if (globalErrors.length) {
    process.exitCode = 2;
  }
}

main().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});

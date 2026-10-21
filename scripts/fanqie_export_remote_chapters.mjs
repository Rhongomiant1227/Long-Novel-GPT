import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import { chromium } from "playwright-core";

const DEFAULT_START_URL = "https://fanqienovel.com/main/writer/?enter_from=author_zone";
const DEFAULT_SEED_PROFILE = path.resolve(".run", "fanqie_writer_seed_profile");
const DEFAULT_WORK_PROFILE = path.resolve(".run", "tmp_fanqie_remote_chapters_profile");
const DEFAULT_OUTPUT = path.resolve(".run", "fanqie_remote_chapters.json");

function printHelp() {
  console.log(`Export complete chapters from the Fanqie writer dashboard.

Usage:
  node scripts/fanqie_export_remote_chapters.mjs \
    --project lychee_prelude \
    --book-title "她还不会爱人" \
    --from 240 \
    --to 272

Options:
  --project <name>          Project identifier, required
  --book-title <title>      Fanqie book title, required
  --from <n>                First chapter number, default 1
  --to <n>                  Last chapter number, default latest
  --chapters <spec>         Specific chapters, e.g. 1,3,8-12
  --output <path>           Output JSON path
  --seed-profile-dir <dir>  Logged-in seed profile
  --profile-dir <dir>       Temporary working profile
  --chrome-path <path>      Chrome executable path
  --start-url <url>         Writer dashboard URL
  --help                    Show help
`);
}

function parseArgs(argv) {
  const args = {
    project: "",
    bookTitle: "",
    from: 1,
    to: Number.MAX_SAFE_INTEGER,
    chapterNumbers: [],
    outputPath: DEFAULT_OUTPUT,
    seedProfileDir: DEFAULT_SEED_PROFILE,
    profileDir: DEFAULT_WORK_PROFILE,
    chromePath: "",
    startUrl: DEFAULT_START_URL,
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
      case "--project":
        args.project = next().trim();
        break;
      case "--book-title":
        args.bookTitle = next().trim();
        break;
      case "--from":
        args.from = Number(next());
        break;
      case "--to":
        args.to = Number(next());
        break;
      case "--chapters":
        args.chapterNumbers = parseChapterSpec(next());
        break;
      case "--output":
        args.outputPath = path.resolve(next());
        break;
      case "--seed-profile-dir":
        args.seedProfileDir = path.resolve(next());
        break;
      case "--profile-dir":
        args.profileDir = path.resolve(next());
        break;
      case "--chrome-path":
        args.chromePath = path.resolve(next());
        break;
      case "--start-url":
        args.startUrl = next();
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
  if (!args.project) {
    throw new Error("--project is required");
  }
  if (!args.bookTitle) {
    throw new Error("--book-title is required");
  }
  if (!Number.isInteger(args.from) || args.from <= 0) {
    throw new Error(`invalid --from: ${args.from}`);
  }
  if (!Number.isInteger(args.to) || args.to < args.from) {
    throw new Error(`invalid --to: ${args.to}`);
  }
}

function parseChapterSpec(value) {
  const result = new Set();
  for (const rawPart of String(value ?? "").split(",")) {
    const part = rawPart.trim();
    if (!part) {
      continue;
    }
    const range = /^(\d+)-(\d+)$/u.exec(part);
    if (range) {
      const start = Number(range[1]);
      const end = Number(range[2]);
      if (start <= 0 || end < start) {
        throw new Error(`invalid chapter range: ${part}`);
      }
      for (let number = start; number <= end; number += 1) {
        result.add(number);
      }
      continue;
    }
    if (!/^\d+$/u.test(part) || Number(part) <= 0) {
      throw new Error(`invalid chapter number: ${part}`);
    }
    result.add(Number(part));
  }
  return [...result].sort((left, right) => left - right);
}

function detectChromePath() {
  const candidates = [];
  if (process.platform === "win32") {
    const programFiles = process.env.PROGRAMFILES ?? "C:\\Program Files";
    const programFilesX86 = process.env["PROGRAMFILES(X86)"] ?? "C:\\Program Files (x86)";
    const localAppData = process.env.LOCALAPPDATA ?? path.join(process.env.USERPROFILE ?? "", "AppData", "Local");
    candidates.push(
      path.join(programFiles, "Google", "Chrome", "Application", "chrome.exe"),
      path.join(programFilesX86, "Google", "Chrome", "Application", "chrome.exe"),
      path.join(localAppData, "Google", "Chrome", "Application", "chrome.exe"),
      path.join(programFiles, "Microsoft", "Edge", "Application", "msedge.exe"),
      path.join(programFilesX86, "Microsoft", "Edge", "Application", "msedge.exe"),
    );
  } else if (process.platform === "darwin") {
    candidates.push(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    );
  } else {
    candidates.push(
      "/usr/bin/google-chrome",
      "/usr/bin/google-chrome-stable",
      "/usr/bin/chromium",
      "/usr/bin/chromium-browser",
    );
  }
  return candidates.find((candidate) => fs.existsSync(candidate)) ?? "";
}

function copyProfileSeed(seedDir, targetDir) {
  if (!fs.existsSync(seedDir)) {
    throw new Error(`seed profile not found: ${seedDir}`);
  }

  const skipNames = new Set([
    "lockfile",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
  ]);
  const skipFragments = [
    `${path.sep}Crashpad`,
    `${path.sep}BrowserMetrics`,
    `${path.sep}ShaderCache`,
    `${path.sep}GrShaderCache`,
    `${path.sep}GraphiteDawnCache`,
    `${path.sep}DawnGraphiteCache`,
    `${path.sep}Code Cache`,
    `${path.sep}GPUCache`,
    `${path.sep}component_crx_cache`,
    `${path.sep}extensions_crx_cache`,
    `${path.sep}optimization_guide_model_store`,
    `${path.sep}Safe Browsing`,
    `${path.sep}segmentation_platform`,
  ];

  fs.rmSync(targetDir, { recursive: true, force: true });
  fs.mkdirSync(targetDir, { recursive: true });
  fs.cpSync(seedDir, targetDir, {
    recursive: true,
    force: true,
    filter: (source) => {
      if (skipNames.has(path.basename(source))) {
        return false;
      }
      return !skipFragments.some((fragment) => source.includes(fragment));
    },
  });
}

function normalizeText(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function parseBookId(publishPath = "", chapterManagePath = "") {
  const publishMatch = String(publishPath).match(/\/main\/writer\/(\d+)\/publish\//);
  if (publishMatch?.[1]) {
    return publishMatch[1];
  }
  const manageMatch = String(chapterManagePath).match(/\/chapter-manage\/(\d+)&/);
  return manageMatch?.[1] ?? "";
}

function parseChapterHeading(heading) {
  const normalized = normalizeText(heading);
  const match = normalized.match(/^第\s*(\d+)\s*章(?:\s+|[：:，,、.．。]\s*)?(.*)$/);
  if (!match) {
    return {
      chapterNumber: null,
      chapterTitle: "",
      fullHeading: normalized,
    };
  }
  return {
    chapterNumber: Number(match[1]),
    chapterTitle: normalizeText(match[2]),
    fullHeading: normalized,
  };
}

function extractChapterId(previewUrl) {
  return String(previewUrl).match(/\/preview\/\d+&(\d+)/)?.[1] ?? "";
}

function sleepSync(milliseconds) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, milliseconds);
}

function partsDirectory(outputPath) {
  return `${outputPath}.parts`;
}

function writeChapterPart(outputPath, chapter) {
  const directory = partsDirectory(outputPath);
  fs.mkdirSync(directory, { recursive: true });
  const filePath = path.join(
    directory,
    `ch_${String(chapter.chapterNumber).padStart(4, "0")}.json`,
  );
  fs.writeFileSync(filePath, `${JSON.stringify(chapter, null, 2)}\n`, "utf8");
}

function writeFinalJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const json = `${JSON.stringify(value, null, 2)}\n`;
  let lastError = null;

  for (let attempt = 1; attempt <= 10; attempt += 1) {
    try {
      fs.writeFileSync(filePath, json, "utf8");
      fs.rmSync(`${filePath}.tmp`, { force: true });
      return;
    } catch (error) {
      lastError = error;
      if (!["EPERM", "EBUSY", "UNKNOWN"].includes(error?.code)) {
        throw error;
      }
      sleepSync(attempt * 150);
    }
  }

  throw lastError;
}

function loadJsonFile(filePath) {
  if (!fs.existsSync(filePath)) {
    return null;
  }
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

function loadExistingOutput(outputPath, args) {
  const payload = {
    schemaVersion: 1,
    project: args.project,
    bookTitle: args.bookTitle,
    fetchedAt: "",
    bookId: "",
    chapterManageUrl: "",
    chapters: [],
  };
  const mergedChapters = new Map();

  for (const candidatePath of [outputPath, `${outputPath}.tmp`]) {
    const candidate = loadJsonFile(candidatePath);
    if (!candidate) {
      continue;
    }
    if (candidate.project !== args.project || candidate.bookTitle !== args.bookTitle) {
      throw new Error(`existing output belongs to another book: ${candidatePath}`);
    }
    Object.assign(payload, candidate);
    for (const chapter of candidate.chapters ?? []) {
      if (Number.isInteger(chapter.chapterNumber)) {
        mergedChapters.set(chapter.chapterNumber, chapter);
      }
    }
  }

  const directory = partsDirectory(outputPath);
  if (fs.existsSync(directory)) {
    for (const name of fs.readdirSync(directory)) {
      if (!/^ch_\d{4}\.json$/i.test(name)) {
        continue;
      }
      const chapter = loadJsonFile(path.join(directory, name));
      if (Number.isInteger(chapter?.chapterNumber)) {
        mergedChapters.set(chapter.chapterNumber, chapter);
      }
    }
  }

  payload.project = args.project;
  payload.bookTitle = args.bookTitle;
  payload.chapters = Array.from(mergedChapters.values()).sort(
    (left, right) => left.chapterNumber - right.chapterNumber,
  );
  return payload;
}

async function resolveBookRoutes(page, startUrl, bookTitle) {
  const bookManageUrl = new URL("/main/writer/book-manage", startUrl).toString();
  await page.goto(bookManageUrl, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});

  const routeInfo = await page.evaluate((targetBookTitle) => {
    const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
    const visible = (element) => {
      if (!(element instanceof Element)) {
        return false;
      }
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };

    const cardSelectors = [
      ".home-book-item",
      ".long-article-table-item",
      "[class*='home-book-item']",
      "[class*='article-table-item']",
    ].join(",");

    const matches = [];
    for (const card of document.querySelectorAll(cardSelectors)) {
      if (!(card instanceof Element) || !visible(card)) {
        continue;
      }
      const cardText = normalize(card.innerText || card.textContent || "");
      if (!cardText.includes(targetBookTitle)) {
        continue;
      }
      const publishLink = Array.from(card.querySelectorAll("a[href*='/publish/']")).find(visible);
      const chapterManageLink = Array.from(card.querySelectorAll("a[href*='/chapter-manage/']")).find(visible);
      if (!publishLink && !chapterManageLink) {
        continue;
      }
      matches.push({
        score: cardText.length,
        publishPath: publishLink?.getAttribute("href") || "",
        chapterManagePath: chapterManageLink?.getAttribute("href") || "",
      });
    }

    matches.sort((left, right) => left.score - right.score);
    return matches[0] ?? null;
  }, bookTitle);

  if (!routeInfo?.publishPath && !routeInfo?.chapterManagePath) {
    throw new Error(`book not found in manage page: ${bookTitle}`);
  }

  return {
    bookId: parseBookId(routeInfo.publishPath, routeInfo.chapterManagePath),
    chapterManageUrl: routeInfo.chapterManagePath
      ? new URL(routeInfo.chapterManagePath, startUrl).toString()
      : "",
  };
}

async function extractIndexPage(page) {
  return await page.evaluate(() => {
    const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
    const rows = [];

    for (const tableRow of document.querySelectorAll("tbody tr.arco-table-tr, tbody tr, .arco-table-tbody tr")) {
      const cells = Array.from(tableRow.querySelectorAll("td")).map(
        (cell) => normalize(cell.innerText || cell.textContent || ""),
      );
      const previewLink = tableRow.querySelector("a[href*='/preview/']");
      if (!cells.length || !previewLink || !/^第.+章/.test(cells[0] || "")) {
        continue;
      }
      rows.push({
        heading: cells[0],
        previewUrl: previewLink.href,
        chars: cells[1] || "",
        typoCount: cells[2] || "",
        status: cells[3] || "",
        publishTime: cells[4] || "",
      });
    }

    const currentPage = normalize(document.querySelector(".arco-pagination-item-active")?.textContent || "1");
    const nextItem = document.querySelector(".arco-pagination-item-next");
    return {
      rows,
      currentPage,
      nextDisabled: nextItem?.className?.includes("disabled") ?? true,
    };
  });
}

async function clickNextPage(page) {
  const before = await page.evaluate(() => ({
    currentPage: String(
      document.querySelector(".arco-pagination-item-active")?.textContent || "1",
    ).replace(/\s+/g, " ").trim(),
    firstHeading: String(
      document.querySelector("tbody tr td")?.textContent || "",
    ).replace(/\s+/g, " ").trim(),
  }));
  await page.locator(".arco-pagination-item-next").first().click({ timeout: 10000 });

  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    await page.waitForTimeout(300);
    const after = await page.evaluate(() => ({
      currentPage: String(
        document.querySelector(".arco-pagination-item-active")?.textContent || "",
      ).replace(/\s+/g, " ").trim(),
      firstHeading: String(
        document.querySelector("tbody tr td")?.textContent || "",
      ).replace(/\s+/g, " ").trim(),
    }));
    if (
      after.currentPage &&
      after.currentPage !== before.currentPage &&
      after.firstHeading &&
      after.firstHeading !== before.firstHeading
    ) {
      return;
    }
  }
  throw new Error(`pagination did not advance from page ${before.currentPage}`);
}

async function fetchChapterIndex(page, chapterManageUrl) {
  await page.goto(chapterManageUrl, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});

  const chapters = [];
  const seenPages = new Set();
  const seenPreviewUrls = new Set();

  while (true) {
    const pageData = await extractIndexPage(page);
    if (seenPages.has(pageData.currentPage)) {
      break;
    }
    seenPages.add(pageData.currentPage);

    for (const row of pageData.rows) {
      if (seenPreviewUrls.has(row.previewUrl)) {
        continue;
      }
      seenPreviewUrls.add(row.previewUrl);
      chapters.push({
        ...parseChapterHeading(row.heading),
        previewUrl: row.previewUrl,
        chapterId: extractChapterId(row.previewUrl),
        status: row.status,
        publishTime: row.publishTime,
        chars: row.chars,
        typoCount: row.typoCount,
      });
    }

    if (pageData.nextDisabled) {
      break;
    }
    await clickNextPage(page);
  }

  chapters.sort((left, right) => {
    const leftNumber = left.chapterNumber ?? Number.MAX_SAFE_INTEGER;
    const rightNumber = right.chapterNumber ?? Number.MAX_SAFE_INTEGER;
    return leftNumber - rightNumber;
  });
  return chapters;
}

async function fetchChapterBody(page, chapter) {
  await page.goto(chapter.previewUrl, { waitUntil: "domcontentloaded" });
  await page.locator(".preview-content").waitFor({ state: "visible", timeout: 20000 });

  const snapshot = await page.evaluate(() => {
    const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
    const heading = normalize(document.querySelector(".preview-title")?.textContent || "");
    const paragraphs = Array.from(document.querySelectorAll(".preview-content p"))
      .map((paragraph) => String(paragraph.innerText || paragraph.textContent || "").trim())
      .filter(Boolean);
    return {
      heading,
      paragraphs,
    };
  });

  if (!snapshot.paragraphs.length) {
    throw new Error(`empty remote chapter body: ${chapter.previewUrl}`);
  }

  const parsedHeading = parseChapterHeading(snapshot.heading || chapter.fullHeading);
  const body = snapshot.paragraphs.join("\n\n");
  return {
    ...chapter,
    ...parsedHeading,
    paragraphs: snapshot.paragraphs,
    body,
    bodyChars: Array.from(body.replace(/\s+/g, "")).length,
    fetchedAt: new Date().toISOString(),
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    printHelp();
    return;
  }
  validateArgs(args);

  const chromePath = args.chromePath || detectChromePath();
  if (!chromePath) {
    throw new Error("Chrome was not found; use --chrome-path");
  }

  copyProfileSeed(args.seedProfileDir, args.profileDir);
  const payload = loadExistingOutput(args.outputPath, args);
  const completed = new Map(
    payload.chapters.map((chapter) => [chapter.chapterNumber, chapter]),
  );
  for (const chapter of completed.values()) {
    writeChapterPart(args.outputPath, chapter);
  }

  const context = await chromium.launchPersistentContext(args.profileDir, {
    executablePath: chromePath,
    headless: true,
    viewport: { width: 1440, height: 1200 },
    locale: "zh-CN",
  });

  try {
    const indexPage = await context.newPage();
    const routes = await resolveBookRoutes(indexPage, args.startUrl, args.bookTitle);
    if (!routes.chapterManageUrl) {
      throw new Error(`chapter manage URL missing: ${args.bookTitle}`);
    }

    const index = await fetchChapterIndex(indexPage, routes.chapterManageUrl);
    await indexPage.close();

    const selected = index.filter(
      (chapter) =>
        Number.isInteger(chapter.chapterNumber) &&
        chapter.chapterNumber >= args.from &&
        chapter.chapterNumber <= args.to &&
        (
          !args.chapterNumbers.length ||
          args.chapterNumbers.includes(chapter.chapterNumber)
        ),
    );
    if (!selected.length) {
      throw new Error(`no chapters matched ${args.from}-${args.to}`);
    }

    payload.bookId = routes.bookId;
    payload.chapterManageUrl = routes.chapterManageUrl;
    payload.indexedAt = new Date().toISOString();
    payload.indexChapterCount = index.length;
    payload.requestedRange = { from: args.from, to: args.to };

    const contentPage = await context.newPage();
    for (const [position, chapter] of selected.entries()) {
      if (completed.has(chapter.chapterNumber)) {
        console.log(`[${position + 1}/${selected.length}] skip chapter ${chapter.chapterNumber}`);
        continue;
      }

      console.log(`[${position + 1}/${selected.length}] fetch chapter ${chapter.chapterNumber}`);
      const snapshot = await fetchChapterBody(contentPage, chapter);
      completed.set(snapshot.chapterNumber, snapshot);
      writeChapterPart(args.outputPath, snapshot);
    }
    await contentPage.close();

    payload.chapters = Array.from(completed.values()).sort(
      (left, right) => left.chapterNumber - right.chapterNumber,
    );
    payload.fetchedAt = new Date().toISOString();
    writeFinalJson(args.outputPath, payload);
    console.log(args.outputPath);
  } finally {
    await context.close().catch(() => {});
  }
}

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exitCode = 1;
});

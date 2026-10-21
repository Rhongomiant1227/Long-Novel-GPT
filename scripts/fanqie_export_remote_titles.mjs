import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import { chromium } from "playwright-core";

const DEFAULT_START_URL = "https://fanqienovel.com/main/writer/?enter_from=author_zone";
const DEFAULT_OUTPUT = path.resolve(".run", "fanqie_remote_titles.json");
const DEFAULT_SEED_PROFILE = path.resolve(".run", "fanqie_writer_seed_profile");
const DEFAULT_WORK_PROFILE = path.resolve(".run", "tmp_fanqie_remote_titles_profile");

function printHelp() {
  console.log(`Export remote Fanqie chapter titles.

Usage:
  node scripts/fanqie_export_remote_titles.mjs \\
    --book "lychee_prelude|她还不会爱人" \\
    --book "inscribed_immortal|删史成仙" \\
    --book "crowd_blackbox|众生黑匣"

Options:
  --book <project|title>    Repeatable. Format: project|bookTitle
  --output <path>           Output JSON path. Default: ${DEFAULT_OUTPUT}
  --seed-profile-dir <dir>  Logged-in seed profile. Default: ${DEFAULT_SEED_PROFILE}
  --profile-dir <dir>       Temporary working profile. Default: ${DEFAULT_WORK_PROFILE}
  --chrome-path <path>      Chrome/Edge/Chromium executable path
  --start-url <url>         Writer home URL
  --help                    Show help
`);
}

function parseArgs(argv) {
  const args = {
    books: [],
    outputPath: DEFAULT_OUTPUT,
    seedProfileDir: DEFAULT_SEED_PROFILE,
    profileDir: DEFAULT_WORK_PROFILE,
    chromePath: "",
    startUrl: DEFAULT_START_URL,
    help: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) {
        throw new Error(`missing value for ${arg}`);
      }
      i += 1;
      return argv[i];
    };

    switch (arg) {
      case "--book": {
        const raw = next();
        const parts = String(raw).split("|");
        if (parts.length !== 2) {
          throw new Error(`invalid --book value: ${raw}`);
        }
        args.books.push({
          project: parts[0].trim(),
          bookTitle: parts[1].trim(),
        });
        break;
      }
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
        args.chromePath = next();
        break;
      case "--start-url":
        args.startUrl = next();
        break;
      case "--help":
      case "-h":
        args.help = true;
        break;
      default:
        throw new Error(`unknown arg: ${arg}`);
    }
  }

  return args;
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
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
      path.join(programFiles, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
      path.join(programFilesX86, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
    );
  } else if (process.platform === "darwin") {
    candidates.push(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
      "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    );
  } else {
    candidates.push(
      "/usr/bin/google-chrome",
      "/usr/bin/google-chrome-stable",
      "/usr/bin/chromium",
      "/usr/bin/chromium-browser",
      "/usr/bin/microsoft-edge",
      "/usr/bin/brave-browser",
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
    filter: (src) => {
      const base = path.basename(src);
      if (skipNames.has(base)) {
        return false;
      }
      return !skipFragments.some((fragment) => src.includes(fragment));
    },
  });
}

function normalizeText(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function parseChineseChapterNumber(raw) {
  const normalized = String(raw ?? "").replace(/\s+/g, "");
  if (!normalized) {
    return Number.NaN;
  }
  if (/^\d+$/.test(normalized)) {
    return Number(normalized);
  }
  const digits = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
  };
  const units = {
    "十": 10,
    "百": 100,
    "千": 1000,
    "万": 10000,
  };

  let total = 0;
  let section = 0;
  let number = 0;
  for (const char of normalized) {
    if (Object.hasOwn(digits, char)) {
      number = digits[char];
      continue;
    }
    if (!Object.hasOwn(units, char)) {
      return Number.NaN;
    }
    const unit = units[char];
    if (unit === 10000) {
      section = (section + number) * unit;
      total += section;
      section = 0;
      number = 0;
      continue;
    }
    section += (number || 1) * unit;
    number = 0;
  }
  return total + section + number;
}

function parseBookId(publishPath = "", chapterManagePath = "") {
  const publishMatch = String(publishPath).match(/\/main\/writer\/(\d+)\/publish\//);
  if (publishMatch?.[1]) {
    return publishMatch[1];
  }
  const manageMatch = String(chapterManagePath).match(/\/chapter-manage\/(\d+)&/);
  if (manageMatch?.[1]) {
    return manageMatch[1];
  }
  return "";
}

async function resolveBookRoutes(page, startUrl, bookTitle) {
  const bookManageUrl = new URL("/main/writer/book-manage", startUrl).toString();
  await page.goto(bookManageUrl, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});

  const routeInfo = await page.evaluate((targetBookTitle) => {
    const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
    const isVisible = (el) => {
      if (!(el instanceof Element)) {
        return false;
      }
      const style = window.getComputedStyle(el);
      if (!style || style.display === "none" || style.visibility === "hidden") {
        return false;
      }
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    };
    const textOf = (el) => normalize(el.innerText || el.textContent || "");

    const cardSelectors = [
      ".home-book-item",
      ".long-article-table-item",
      "[class*='home-book-item']",
      "[class*='article-table-item']",
    ].join(",");

    const matches = [];
    for (const card of document.querySelectorAll(cardSelectors)) {
      if (!(card instanceof Element) || !isVisible(card)) {
        continue;
      }
      const cardText = textOf(card);
      if (!cardText || !cardText.includes(targetBookTitle)) {
        continue;
      }

      const exactTitle = Array.from(card.querySelectorAll("*")).some(
        (node) => node instanceof Element && isVisible(node) && textOf(node) === targetBookTitle,
      );
      const publishLink = Array.from(card.querySelectorAll("a[href*='/publish/']")).find(
        (node) => node instanceof Element && isVisible(node),
      );
      const chapterManageLink = Array.from(card.querySelectorAll("a[href*='/chapter-manage/']")).find(
        (node) => node instanceof Element && isVisible(node),
      );

      if (!publishLink && !chapterManageLink) {
        continue;
      }

      matches.push({
        score: (exactTitle ? 0 : 1000) + cardText.length,
        publishPath: publishLink?.getAttribute("href") || "",
        chapterManagePath: chapterManageLink?.getAttribute("href") || "",
      });
    }

    if (!matches.length) {
      return null;
    }
    matches.sort((a, b) => a.score - b.score);
    return matches[0];
  }, bookTitle);

  if (!routeInfo?.publishPath && !routeInfo?.chapterManagePath) {
    throw new Error(`book not found in manage page: ${bookTitle}`);
  }

  const bookId = parseBookId(routeInfo.publishPath, routeInfo.chapterManagePath);
  return {
    bookId,
    publishUrl: routeInfo.publishPath ? new URL(routeInfo.publishPath, startUrl).toString() : "",
    chapterManageUrl: routeInfo.chapterManagePath
      ? new URL(routeInfo.chapterManagePath, startUrl).toString()
      : "",
  };
}

async function extractPageData(page) {
  return await page.evaluate(() => {
    const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();

    const rows = [];
    const trList = Array.from(document.querySelectorAll("tbody tr.arco-table-tr, tbody tr, .arco-table-tbody tr"));
    for (const tr of trList) {
      const cells = Array.from(tr.querySelectorAll("td")).map((td) => normalize(td.innerText || td.textContent || ""));
      if (!cells.length) {
        continue;
      }
      const title = cells[0] || "";
      if (!/^第.+章/.test(title)) {
        continue;
      }
      rows.push({
        title,
        chars: cells[1] || "",
        typoCount: cells[2] || "",
        status: cells[3] || "",
        publishTime: cells[4] || "",
        rawCells: cells,
      });
    }

    const currentPage = normalize(document.querySelector(".arco-pagination-item-active")?.textContent || "1");
    const nextItem = document.querySelector(".arco-pagination-item-next");
    const nextDisabled = nextItem?.className?.includes("disabled") ?? true;
    return { rows, currentPage, nextDisabled };
  });
}

async function clickNextPage(page) {
  const currentPage = await page.locator(".arco-pagination-item-active").first().innerText().catch(() => "1");
  await page.locator(".arco-pagination-item-next").first().click({ timeout: 10000 });
  await page.waitForTimeout(800);
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});

  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    const newPage = await page.locator(".arco-pagination-item-active").first().innerText().catch(() => "");
    if (normalizeText(newPage) && normalizeText(newPage) !== normalizeText(currentPage)) {
      return;
    }
    await page.waitForTimeout(400);
  }
}

async function fetchBookRows(page, startUrl, book) {
  const routes = await resolveBookRoutes(page, startUrl, book.bookTitle);
  if (!routes.chapterManageUrl) {
    throw new Error(`chapter manage url missing: ${book.bookTitle}`);
  }

  await page.goto(routes.chapterManageUrl, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});

  const seenPages = new Set();
  const seenRows = new Set();
  const rows = [];

  while (true) {
    const pageData = await extractPageData(page);
    const pageKey = normalizeText(pageData.currentPage || "1");
    if (seenPages.has(pageKey)) {
      break;
    }
    seenPages.add(pageKey);

    for (const row of pageData.rows) {
      const match = row.title.match(/^第\s*([零〇一二两三四五六七八九十百千万\d]+)\s*章(?:\s+|[：:，,、.．。]\s*)?(.*)$/);
      const chapterNumber = match ? parseChineseChapterNumber(match[1]) : Number.NaN;
      const chapterTitle = normalizeText(match?.[2] || "");
      const key = `${row.title}@@${row.publishTime}@@${row.status}`;
      if (seenRows.has(key)) {
        continue;
      }
      seenRows.add(key);
      rows.push({
        chapterNumber: Number.isFinite(chapterNumber) ? chapterNumber : null,
        chapterTitle,
        fullHeading: row.title,
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

  rows.sort((a, b) => {
    const numA = a.chapterNumber ?? Number.MAX_SAFE_INTEGER;
    const numB = b.chapterNumber ?? Number.MAX_SAFE_INTEGER;
    if (numA !== numB) {
      return numA - numB;
    }
    return a.publishTime.localeCompare(b.publishTime, "zh-CN");
  });

  return {
    ...book,
    bookId: routes.bookId,
    chapterManageUrl: routes.chapterManageUrl,
    publishUrl: routes.publishUrl,
    rowCount: rows.length,
    rows,
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    printHelp();
    return;
  }
  if (!args.books.length) {
    throw new Error("at least one --book is required");
  }

  const chromePath = args.chromePath || detectChromePath();
  if (!chromePath) {
    throw new Error("chrome not found; use --chrome-path");
  }

  copyProfileSeed(args.seedProfileDir, args.profileDir);
  ensureDir(path.dirname(args.outputPath));

  const context = await chromium.launchPersistentContext(args.profileDir, {
    executablePath: chromePath,
    headless: true,
    viewport: { width: 1440, height: 1200 },
    locale: "zh-CN",
  });

  try {
    const page = await context.newPage();
    const payload = {
      fetchedAt: new Date().toISOString(),
      startUrl: args.startUrl,
      books: [],
    };

    for (const book of args.books) {
      payload.books.push(await fetchBookRows(page, args.startUrl, book));
    }

    fs.writeFileSync(args.outputPath, JSON.stringify(payload, null, 2), "utf8");
    console.log(args.outputPath);
  } finally {
    await context.close().catch(() => {});
  }
}

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exitCode = 1;
});

import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import readline from "node:readline/promises";

import { chromium } from "playwright-core";

const DEFAULT_START_URL = "https://fanqienovel.com/main/writer/?enter_from=author_zone";
const DEFAULT_OPENCLAW_CDP_URL = "http://127.0.0.1:18800";
let currentLogFile = "";

function printHelp() {
  console.log(`番茄小说章节上传脚本

用法:
  node scripts/fanqie_upload.mjs --book-title 删史成仙 --chapters-dir D:\\Long-Novel-GPT\\auto_projects\\inscribed_immortal\\manuscript\\chapters_txt --from 8

常用参数:
  --book-title <title>      作品名，必填
  --chapters-dir <dir>      章节 txt 目录，必填
  --from <n>                起始章节号
  --to <n>                  结束章节号
  --chapter <n>             只上传单章，等价于 --from n --to n
  --auto-next-count <n>     自动从远端最新已发布章节之后，连续上传 n 章
  --cdp-url <url>           连接已打开的 Chromium/CDP 浏览器
  --attach-openclaw         连接当前 openclaw 浏览器（默认 ${DEFAULT_OPENCLAW_CDP_URL}）
  --profile-dir <dir>       持久化 Chrome profile 目录
  --seed-profile-dir <dir>  从已登录 profile 复制工作副本后再启动
  --chrome-path <path>      Chrome 可执行文件路径
  --draft-only              只走到发布前，不点最终发布
  --debug                   输出精简调试快照
  --headless                无头模式启动独立浏览器
  --keep-open               结束后不关闭独立浏览器
  --start-url <url>         作家后台首页
  --artifacts-dir <dir>     失败截图和 HTML 输出目录
  --help                    显示帮助

说明:
  1. 章节文件首行必须形如: 第8章 州簿压名
  2. 默认正文会按: 章节号 -> 标题 -> 正文 的顺序填入
  3. 使用 --attach-openclaw 时，会直接复用已登录的 openclaw 浏览器会话
`);
}

function parseArgs(argv) {
  const args = {
    startUrl: DEFAULT_START_URL,
    headless: false,
    draftOnly: false,
    debug: false,
    keepOpen: false,
    artifactsDir: path.resolve(".run", "fanqie_upload_artifacts"),
    profileDir: path.resolve(".run", "fanqie_upload_profile"),
    cdpUrl: null,
    bookTitle: "",
    chaptersDir: "",
    from: 0,
    to: 0,
    chapter: 0,
    autoNextCount: 0,
    chromePath: "",
    seedProfileDir: "",
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) {
        throw new Error(`缺少参数值: ${arg}`);
      }
      i += 1;
      return argv[i];
    };

    switch (arg) {
      case "--help":
      case "-h":
        args.help = true;
        break;
      case "--book-title":
        args.bookTitle = next();
        break;
      case "--chapters-dir":
        args.chaptersDir = next();
        break;
      case "--from":
        args.from = Number(next());
        break;
      case "--to":
        args.to = Number(next());
        break;
      case "--chapter":
        args.chapter = Number(next());
        break;
      case "--auto-next-count":
        args.autoNextCount = Number(next());
        break;
      case "--cdp-url":
        args.cdpUrl = next();
        break;
      case "--attach-openclaw":
        args.cdpUrl = DEFAULT_OPENCLAW_CDP_URL;
        break;
      case "--profile-dir":
        args.profileDir = path.resolve(next());
        break;
      case "--seed-profile-dir":
        args.seedProfileDir = path.resolve(next());
        break;
      case "--chrome-path":
        args.chromePath = next();
        break;
      case "--draft-only":
        args.draftOnly = true;
        break;
      case "--debug":
        args.debug = true;
        break;
      case "--headless":
        args.headless = true;
        break;
      case "--keep-open":
        args.keepOpen = true;
        break;
      case "--start-url":
        args.startUrl = next();
        break;
      case "--artifacts-dir":
        args.artifactsDir = path.resolve(next());
        break;
      default:
        throw new Error(`未知参数: ${arg}`);
    }
  }

  if (args.chapter) {
    args.from = args.chapter;
    args.to = args.chapter;
  }

  return args;
}

function assertArgs(args) {
  if (args.help) {
    return;
  }
  if (!args.bookTitle.trim()) {
    throw new Error("缺少 --book-title");
  }
  if (!args.chaptersDir.trim()) {
    throw new Error("缺少 --chapters-dir");
  }
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function resetDir(dir) {
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
}

function sanitizeFileName(value) {
  return value.replace(/[<>:"/\\|?*\u0000-\u001F]/g, "_");
}

function normalizeWhitespace(value) {
  return value.replace(/\s+/g, " ").trim();
}

function log(message) {
  const stamp = new Date().toISOString().replace("T", " ").slice(0, 19);
  const line = `${stamp} | ${message}`;
  console.log(line);
  if (currentLogFile) {
    fs.appendFileSync(currentLogFile, `${line}\n`, "utf8");
  }
}

async function waitForEnter(prompt) {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  try {
    await rl.question(prompt);
  } finally {
    rl.close();
  }
}

function parseChapterFileContent(content, filePath) {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const titleLine = lines[0]?.trim() ?? "";
  const match = titleLine.match(/^第\s*(\d+)\s*章\s+(.+)$/);
  if (!match) {
    throw new Error(`章节标题格式无效: ${filePath}`);
  }

  let body = lines.slice(1).join("\n");
  body = body.replace(/^\s+/, "").replace(/\s+$/, "");
  if (!body) {
    throw new Error(`章节正文为空: ${filePath}`);
  }

  return {
    number: Number(match[1]),
    title: match[2].trim(),
    body,
    heading: titleLine,
  };
}

async function loadChapterByNumber(chaptersDir, chapterNumber) {
  const filePath = path.join(chaptersDir, `ch_${String(chapterNumber).padStart(4, "0")}.txt`);
  const content = await fsp.readFile(filePath, "utf8");
  return {
    ...parseChapterFileContent(content, filePath),
    filePath,
  };
}

async function loadChapterJobs(chaptersDir, from, to) {
  const names = (await fsp.readdir(chaptersDir))
    .filter((name) => /^ch_\d{4}\.txt$/i.test(name))
    .sort((a, b) => a.localeCompare(b, "en"));

  const jobs = [];
  for (const name of names) {
    const fileChapterNumber = Number(name.match(/^ch_(\d{4})\.txt$/i)?.[1] ?? 0);
    if (from && fileChapterNumber < from) {
      continue;
    }
    if (to && fileChapterNumber > to) {
      continue;
    }
    const fullPath = path.join(chaptersDir, name);
    const parsed = parseChapterFileContent(
      await fsp.readFile(fullPath, "utf8"),
      fullPath,
    );
    jobs.push({
      ...parsed,
      filePath: fullPath,
    });
  }

  if (!jobs.length) {
    throw new Error("没有匹配到要上传的章节文件");
  }

  return jobs;
}

function selectContiguousJobs(allJobs, startNumber, maxCount) {
  const pending = allJobs
    .filter((job) => job.number >= startNumber)
    .sort((a, b) => a.number - b.number);

  if (!pending.length) {
    return [];
  }
  if (pending[0].number !== startNumber) {
    throw new Error(`本地章节缺口：期望从第${startNumber}章开始，但找到的是第${pending[0].number}章`);
  }

  const selected = [];
  let expected = startNumber;
  for (const job of pending) {
    if (job.number !== expected) {
      log(`检测到本地章节断档，已在第${expected - 1}章后停止本次选章`);
      break;
    }
    selected.push(job);
    expected += 1;
    if (selected.length >= maxCount) {
      break;
    }
  }
  return selected;
}

function detectChromeExecutable() {
  const candidates = [];
  if (process.platform === "win32") {
    const programFiles = process.env.PROGRAMFILES ?? "C:\\Program Files";
    const programFilesX86 = process.env["PROGRAMFILES(X86)"] ?? "C:\\Program Files (x86)";
    const localAppData = process.env.LOCALAPPDATA ?? path.join(os.homedir(), "AppData", "Local");
    candidates.push(
      path.join(programFiles, "Google", "Chrome", "Application", "chrome.exe"),
      path.join(programFilesX86, "Google", "Chrome", "Application", "chrome.exe"),
      path.join(localAppData, "Google", "Chrome", "Application", "chrome.exe"),
      path.join(programFiles, "Microsoft", "Edge", "Application", "msedge.exe"),
      path.join(programFiles, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
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
    throw new Error(`seed profile 不存在: ${seedDir}`);
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

  resetDir(targetDir);
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

async function saveFailureArtifacts(page, artifactsDir, prefix) {
  if (!page || page.isClosed()) {
    return "";
  }
  ensureDir(artifactsDir);
  const safePrefix = sanitizeFileName(prefix);
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const base = path.join(artifactsDir, `${safePrefix}_${stamp}`);
  try {
    await page.screenshot({ path: `${base}.png`, fullPage: true });
  } catch {}
  try {
    await fsp.writeFile(`${base}.html`, await page.content(), "utf8");
  } catch {}
  return base;
}

async function getFirstVisibleLocator(factories) {
  for (const createLocator of factories) {
    const locator = createLocator().first();
    try {
      if ((await locator.count()) > 0 && (await locator.isVisible())) {
        return locator;
      }
    } catch {}
  }
  return null;
}

async function clickFirstVisible(page, factories, description) {
  const locator = await getFirstVisibleLocator(
    factories.map((factory) => () => factory(page)),
  );
  if (!locator) {
    return false;
  }
  await locator.click();
  if (description) {
    log(description);
  }
  return true;
}

async function isLoginPage(page) {
  const locator = page.getByRole("button", { name: /立即登录|登录/ }).first();
  try {
    return (await locator.count()) > 0 && (await locator.isVisible());
  } catch {
    return false;
  }
}

async function waitForDashboardReady(page, bookTitle) {
  await page.waitForLoadState("domcontentloaded");
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});

  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const ready = await getFirstVisibleLocator([
      () => page.getByRole("link", { name: "创建章节" }),
      () => page.getByRole("button", { name: "创建章节" }),
      () => page.getByRole("link", { name: "章节管理" }),
      () => page.getByRole("button", { name: "章节管理" }),
      () => page.getByText(bookTitle, { exact: false }),
    ]);
    if (ready) {
      return;
    }
    await page.waitForTimeout(1000);
  }

  throw new Error("作家后台首页加载超时，未出现作品卡片或章节入口");
}

async function selectBookOnDashboard(page, bookTitle) {
  const combo = page.getByRole("combobox").first();
  try {
    if ((await combo.count()) > 0 && (await combo.isVisible())) {
      await combo.click();
      const option = page.getByRole("option", { name: bookTitle }).first();
      if ((await option.count()) > 0) {
        await option.click();
        log(`已选择作品: ${bookTitle}`);
        await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
      }
    }
  } catch {}
}

function parseLastSubmittedChapter(text) {
  const normalized = normalizeWhitespace(text);
  const match = normalized.match(/上次提交[：:]\s*(?:第\S+卷\s*)?第\s*(\d+)\s*章\s+(.+?)(?=\s*(?:已保存|保存中|审核中|已发布|正文字数|存草稿|下一步|上次提交|作品名称|$))/);
  if (!match) {
    return null;
  }

  return {
    number: Number(match[1]),
    title: normalizeWhitespace(match[2]),
    raw: match[0],
  };
}

function parseRecentUpdateChapter(text) {
  const lines = text.split(/\r?\n/);
  for (const rawLine of lines) {
    const line = normalizeWhitespace(rawLine);
    if (!line) {
      continue;
    }
    const match = line.match(/^最近更新[：:]\s*(?:第\S+卷\s*)?第\s*(\d+)\s*章(?:\s+.+)?$/);
    if (match) {
      return {
        number: Number(match[1]),
        raw: line,
      };
    }
  }
  return null;
}

function parseChapterNumbersFromText(text) {
  const numbers = [];
  for (const rawLine of text.split(/\r?\n/)) {
    const line = normalizeWhitespace(rawLine);
    if (!line) {
      continue;
    }
    const match = line.match(/^(?:最近更新[：:]\s*)?(?:第\S+卷\s*)?第\s*(\d+)\s*章(?:\s+.+)?$/);
    if (match) {
      numbers.push(Number(match[1]));
    }
  }
  return numbers;
}

async function getLastSubmittedChapterFromEditor(page) {
  const deadline = Date.now() + 30000;
  let latestText = "";
  while (Date.now() < deadline) {
    await page.waitForLoadState("networkidle", { timeout: 3000 }).catch(() => {});
    const bodyText = await page.locator("body").innerText().catch(() => "");
    latestText = normalizeWhitespace(bodyText);
    const parsed = parseLastSubmittedChapter(latestText);
    if (parsed) {
      return parsed;
    }

    const editorReady = await page.locator("input.serial-input").first().isVisible().catch(() => false);
    if (editorReady && latestText) {
      break;
    }
    await page.waitForTimeout(1000);
  }
  return latestText ? { number: 0, title: "", raw: latestText } : null;
}

async function assertPreviousChapterMatches(page, chapter, chaptersDir) {
  if (chapter.number <= 1) {
    log("当前是第1章，跳过上一章连续性校验");
    return;
  }

  const previousLocal = await loadChapterByNumber(chaptersDir, chapter.number - 1);
  const previousRemote = await getLastSubmittedChapterFromEditor(page);
  if (!previousRemote || previousRemote.number === 0) {
    const observedText = previousRemote?.raw ? `；页面文本片段：${previousRemote.raw.slice(0, 200)}` : "";
    throw new Error(
      `未能从发布页读取“上次提交”信息，拒绝上传第${chapter.number}章；期望上一章为第${previousLocal.number}章 ${previousLocal.title}${observedText}`,
    );
  }

  if (previousRemote.number !== previousLocal.number) {
    throw new Error(
      `发布页“上次提交”为第${previousRemote.number}章 ${previousRemote.title}，但本次上传前一章应为第${previousLocal.number}章 ${previousLocal.title}，已拒绝上传`,
    );
  }

  if (normalizeWhitespace(previousRemote.title) !== normalizeWhitespace(previousLocal.title)) {
    throw new Error(
      `发布页“上次提交”标题不匹配：远端为“${previousRemote.title}”，本地上一章为“${previousLocal.title}”，已拒绝上传`,
    );
  }

  log(`上一章连续性校验通过：远端第${previousRemote.number}章 ${previousRemote.title}`);
}

async function getLatestPublishedChapter(page, startUrl, bookTitle) {
  log(`读取《${bookTitle}》远端最新已发布章节`);
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  await waitForDashboardReady(page, bookTitle);
  await selectBookOnDashboard(page, bookTitle);

  const homeText = await page.locator("body").innerText().catch(() => "");
  const recent = parseRecentUpdateChapter(homeText);
  if (recent) {
    log(`首页最近更新显示为第${recent.number}章`);
    return recent.number;
  }

  log("首页未解析到最近更新，转到章节管理读取最新章节");
  await clickFirstVisible(
    page,
    [
      (p) => p.getByRole("link", { name: "章节管理" }),
      (p) => p.getByRole("button", { name: "章节管理" }),
      (p) => p.locator("a").filter({ hasText: "章节管理" }),
      (p) => p.locator("button").filter({ hasText: "章节管理" }),
    ],
    "已进入章节管理",
  );
  await page.waitForLoadState("domcontentloaded");
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
  const manageText = await page.locator("body").innerText().catch(() => "");
  const numbers = parseChapterNumbersFromText(manageText);
  if (!numbers.length) {
    log("远端未解析到任何已发布章节，按从第1章开始处理");
    return 0;
  }
  const latest = Math.max(...numbers);
  log(`章节管理显示最新章节为第${latest}章`);
  return latest;
}

async function resolveAutoNextJobs(session, args) {
  const page = await session.context.newPage();
  try {
    await ensureLoggedIn(page, args.startUrl);
    const latestPublished = await getLatestPublishedChapter(page, args.startUrl, args.bookTitle);
    const allJobs = await loadChapterJobs(args.chaptersDir, 0, 0);
    const startNumber = latestPublished > 0 ? latestPublished + 1 : 1;
    const selected = selectContiguousJobs(allJobs, startNumber, args.autoNextCount);
    if (!selected.length) {
      log(`没有待上传的新章节；远端已到第${latestPublished}章，本地没有连续后续章节`);
      return [];
    }
    log(`自动选章完成：将从第${selected[0].number}章上传到第${selected[selected.length - 1].number}章`);
    return selected;
  } finally {
    await page.close().catch(() => {});
  }
}

async function ensureLoggedIn(page, startUrl) {
  log(`打开作家后台: ${startUrl}`);
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  if (!(await isLoginPage(page))) {
    log("检测到已登录状态");
    return;
  }

  log("检测到未登录状态，请在打开的浏览器里完成番茄作家登录。");
  await waitForEnter("登录完成后回到终端，按 Enter 继续...");
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  if (await isLoginPage(page)) {
    throw new Error("仍处于未登录状态，终止上传");
  }
}

async function selectBookAndOpenCreatePage(context, page, startUrl, bookTitle) {
  log(`进入作品首页，准备定位《${bookTitle}》`);
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  await waitForDashboardReady(page, bookTitle);
  await selectBookOnDashboard(page, bookTitle);

  const popupPromise = context.waitForEvent("page", { timeout: 5000 }).catch(() => null);
  const clicked = await clickFirstVisible(
    page,
    [
      (p) => p.getByRole("link", { name: "创建章节" }),
      (p) => p.getByRole("button", { name: "创建章节" }),
      (p) => p.locator("a").filter({ hasText: "创建章节" }),
      (p) => p.locator("button").filter({ hasText: "创建章节" }),
    ],
    "已点击创建章节",
  );
  if (!clicked) {
    throw new Error(`未找到《${bookTitle}》的创建章节入口`);
  }

  const popup = await popupPromise;
  const targetPage = popup ?? page;
  await targetPage.waitForLoadState("domcontentloaded");
  log(`已进入创建章节页: ${targetPage.url()}`);
  return targetPage;
}

async function fillEditor(page, chapter) {
  log(`开始填写第${chapter.number}章表单`);
  const numberInput = page.locator("input.serial-input").first();
  const titleInput = page.getByPlaceholder("请输入标题").first();
  const editor = page.locator(".ProseMirror").first();

  await numberInput.waitFor({ state: "visible", timeout: 30000 });
  await titleInput.waitFor({ state: "visible", timeout: 30000 });
  await editor.waitFor({ state: "visible", timeout: 30000 });

  await numberInput.click();
  await numberInput.fill(String(chapter.number));
  await titleInput.click();
  await titleInput.fill(chapter.title);

  await editor.click();
  await page.keyboard.press("Control+A");
  await page.keyboard.press("Backspace");
  const pasteModifier = process.platform === "darwin" ? "Meta" : "Control";
  const clipboardResult = await page.evaluate(async (text) => {
    try {
      await navigator.clipboard.writeText(text);
      return { ok: true };
    } catch (error) {
      return {
        ok: false,
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }, chapter.body);

  if (clipboardResult?.ok) {
    await page.keyboard.press(`${pasteModifier}+V`);
    log("正文已通过剪贴板粘贴写入");
  } else {
    await page.keyboard.insertText(chapter.body);
    log(`剪贴板写入失败，退回 insertText: ${clipboardResult?.message ?? "unknown error"}`);
  }

  await page.waitForTimeout(1000);
  log(`已填入第${chapter.number}章标题和正文`);
}

async function handleTypoAndAIDialogs(page, { draftOnly, debug }) {
  log("开始处理发布确认弹窗");
  if (debug) {
    const debugSnapshot = await page.evaluate(() => {
      const pick = (selector) =>
        Array.from(document.querySelectorAll(selector))
          .map((el) => ({
            text: (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim(),
            aria: el.getAttribute("aria-label"),
            role: el.getAttribute("role"),
          }))
          .filter((item) => item.text || item.aria);

      return {
        url: location.href,
        buttons: pick("button, [role='button']").slice(0, 20),
        labels: pick("label, [role='radio'], [role='checkbox']").slice(0, 20),
        headings: pick("h1, h2, h3, .title, .header").slice(0, 20),
        snippets: Array.from(document.querySelectorAll("body *"))
          .map((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
          .filter(Boolean)
          .filter((text) => /AI|错别字|检测|发布|提交|确认|声明|生成|原创|章节/.test(text))
          .slice(0, 20),
      };
    });
    log(`发布页调试快照: ${JSON.stringify(debugSnapshot, null, 2)}`);
  }

  const deadline = Date.now() + 90000;
  let publishClicked = false;

  while (Date.now() < deadline) {
    let progressed = false;

    const pageText = await page.locator("body").innerText().catch(() => "");
    const hasRiskDialog = /内容风险|风险检测|开启检测/.test(pageText);

    if (
      hasRiskDialog &&
      await clickFirstVisible(
        page,
        [
          (p) => p.getByRole("button", { name: /取消|暂不|关闭/ }),
          (p) => p.locator("button").filter({ hasText: /取消|暂不|关闭/ }),
          (p) => p.locator("[role='button']").filter({ hasText: /取消|暂不|关闭/ }),
        ],
        "已取消内容风险检测弹窗",
      )
    ) {
      progressed = true;
      await page.waitForTimeout(1000);
    }

    if (
      /AI|人工智能|智能生成/.test(pageText) &&
      await clickFirstVisible(
        page,
        [
          (p) => p.getByRole("radio", { name: /否|没有使用AI|未使用AI/ }),
          (p) => p.getByLabel(/否|没有使用AI|未使用AI/),
          (p) => p.locator("label").filter({ hasText: /否|没有使用AI|未使用AI/ }),
          (p) => p.locator("[role='radio']").filter({ hasText: /否|没有使用AI|未使用AI/ }),
          (p) => p.locator("span").filter({ hasText: /否|没有使用AI|未使用AI/ }),
        ],
        "已选择 AI 使用为否",
      )
    ) {
      progressed = true;
      await page.waitForTimeout(800);
    }

    if (
      /定时发布/.test(pageText) &&
      await clickFirstVisible(
        page,
        [
          (p) => p.getByRole("radio", { name: /立即发布|不定时发布|否/ }),
          (p) => p.getByLabel(/立即发布|不定时发布|否/),
          (p) => p.locator("label").filter({ hasText: /立即发布|不定时发布|否/ }),
          (p) => p.locator("[role='radio']").filter({ hasText: /立即发布|不定时发布|否/ }),
          (p) => p.locator("span").filter({ hasText: /立即发布|不定时发布|否/ }),
        ],
        "已选择非定时发布",
      )
    ) {
      progressed = true;
      await page.waitForTimeout(800);
    }

    if (
      await clickFirstVisible(
        page,
        [
          (p) => p.getByRole("button", { name: /跳过.*错别字|跳过.*检测|忽略.*错别字|继续发布|直接发布|放弃检测|提交/ }),
          (p) => p.locator("button").filter({ hasText: /跳过.*错别字|跳过.*检测|忽略.*错别字|继续发布|直接发布|放弃检测|提交/ }),
          (p) => p.locator("[role='button']").filter({ hasText: /跳过.*错别字|跳过.*检测|忽略.*错别字|继续发布|直接发布|放弃检测|提交/ }),
        ],
        "已跳过错别字/检测步骤",
      )
    ) {
      progressed = true;
      await page.waitForTimeout(1000);
    }

    if (
      await clickFirstVisible(
        page,
        [
          (p) => p.getByLabel(/没有使用AI|未使用AI/),
          (p) => p.getByRole("radio", { name: /没有使用AI|未使用AI/ }),
          (p) => p.locator("label").filter({ hasText: /没有使用AI|未使用AI/ }),
          (p) => p.locator("[role='radio']").filter({ hasText: /没有使用AI|未使用AI/ }),
          (p) => p.locator("span").filter({ hasText: /没有使用AI|未使用AI/ }),
        ],
        "已选择未使用 AI",
      )
    ) {
      progressed = true;
      await page.waitForTimeout(800);
    }

    if (
      await clickFirstVisible(
        page,
        [
          (p) => p.locator("label").filter({ hasText: /我已阅读|我确认/ }),
          (p) => p.locator("span").filter({ hasText: /我已阅读|我确认/ }),
        ],
        "已勾选发布确认项",
      )
    ) {
      progressed = true;
      await page.waitForTimeout(500);
    }

    if (!draftOnly) {
      if (
        await clickFirstVisible(
          page,
          [
            (p) => p.getByRole("button", { name: /确认发布|发布章节|立即发布|发布/ }),
            (p) => p.locator("button").filter({ hasText: /确认发布|发布章节|立即发布|发布/ }),
            (p) => p.locator("[role='button']").filter({ hasText: /确认发布|发布章节|立即发布|发布/ }),
          ],
          publishClicked ? "已继续确认发布" : "已点击发布",
        )
      ) {
        progressed = true;
        publishClicked = true;
        await page.waitForTimeout(1500);
      }
    }

    const successText = page.locator("text=/发布成功|审核中|提交成功|已发布/");
    try {
      if ((await successText.count()) > 0 && (await successText.first().isVisible())) {
        return;
      }
    } catch {}

    if (!progressed) {
      await page.waitForTimeout(1200);
    }
  }

  if (!draftOnly) {
    throw new Error("发布流程超时，未能自动完成后续弹窗");
  }
}

async function verifyPublishedOnDashboard(page, startUrl, bookTitle, chapter) {
  log(`回到首页校验第${chapter.number}章是否已显示`);
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  await waitForDashboardReady(page, bookTitle);
  await selectBookOnDashboard(page, bookTitle);
  const expected = normalizeWhitespace(`第${chapter.number}章 ${chapter.title}`);
  const expectedRecent = normalizeWhitespace(`最近更新：${expected}`);
  const bodyText = normalizeWhitespace(await page.locator("body").innerText().catch(() => ""));
  if (bodyText.includes(expectedRecent) || bodyText.includes(expected)) {
    log(`后台首页已显示目标章节: ${expected}`);
    return;
  }

  log("首页未立即显示目标章节，转到章节管理继续校验");
  await clickFirstVisible(
    page,
    [
      (p) => p.getByRole("link", { name: "章节管理" }),
      (p) => p.getByRole("button", { name: "章节管理" }),
      (p) => p.locator("a").filter({ hasText: "章节管理" }),
      (p) => p.locator("button").filter({ hasText: "章节管理" }),
    ],
    "已进入章节管理",
  );
  await page.waitForLoadState("domcontentloaded");
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const manageText = normalizeWhitespace(await page.locator("body").innerText().catch(() => ""));
    if (manageText.includes(expected)) {
      log(`章节管理已显示目标章节: ${expected}`);
      return;
    }
    await page.waitForTimeout(1000);
  }
  throw new Error(`章节管理中未找到目标章节: ${expected}`);
}

async function uploadOneChapter(session, args, chapter) {
  log(`为第${chapter.number}章打开新的工作页`);
  const dashboardPage = await session.context.newPage();
  dashboardPage.on("dialog", async (dialog) => {
    log(`检测到浏览器对话框: ${dialog.message()}`);
    await dialog.accept().catch(() => {});
  });

  let activePage = dashboardPage;
  try {
    await ensureLoggedIn(dashboardPage, args.startUrl);
    const editorPage = await selectBookAndOpenCreatePage(
      session.context,
      dashboardPage,
      args.startUrl,
      args.bookTitle,
    );
    activePage = editorPage;

    const shouldCloseEditor = editorPage !== dashboardPage;
    try {
      await assertPreviousChapterMatches(editorPage, chapter, args.chaptersDir);
      await fillEditor(editorPage, chapter);
      await clickFirstVisible(
        editorPage,
        [
          (p) => p.getByRole("button", { name: "下一步" }),
          (p) => p.locator("button").filter({ hasText: /^下一步$/ }),
        ],
        "已进入发布确认步骤",
      );
      await handleTypoAndAIDialogs(editorPage, args);
      if (!args.draftOnly) {
        await verifyPublishedOnDashboard(dashboardPage, args.startUrl, args.bookTitle, chapter);
      }
    } catch (error) {
      if (!error?.artifactsSaved) {
        const artifactPage = !editorPage.isClosed() ? editorPage : dashboardPage;
        await saveFailureArtifacts(
          artifactPage,
          args.artifactsDir,
          `chapter_${chapter.number}`,
        );
        error.artifactsSaved = true;
      }
      throw error;
    } finally {
      if (shouldCloseEditor) {
        await editorPage.close().catch(() => {});
      }
    }
  } catch (error) {
    if (!error?.artifactsSaved) {
      const artifactPage = activePage && !activePage.isClosed() ? activePage : dashboardPage;
      await saveFailureArtifacts(
        artifactPage,
        args.artifactsDir,
        `chapter_${chapter.number}`,
      );
      error.artifactsSaved = true;
    }
    throw error;
  } finally {
    await dashboardPage.close().catch(() => {});
  }
}

async function createSession(args) {
  if (args.cdpUrl) {
    log(`通过 CDP 连接浏览器: ${args.cdpUrl}`);
    const browser = await chromium.connectOverCDP(args.cdpUrl);
    const context = browser.contexts()[0];
    if (!context) {
      throw new Error(`CDP 浏览器上没有可用 context: ${args.cdpUrl}`);
    }
    await context.grantPermissions(["clipboard-read", "clipboard-write"], {
      origin: "https://fanqienovel.com",
    }).catch(() => {});
    return {
      browser,
      context,
      close: async () => {},
    };
  }

  const chromePath = args.chromePath || detectChromeExecutable();
  if (!chromePath) {
    throw new Error("未找到 Chrome/Chromium，可用 --chrome-path 显式指定");
  }

  if (args.seedProfileDir) {
    log(`从 seed profile 复制工作副本: ${args.seedProfileDir} -> ${args.profileDir}`);
    copyProfileSeed(args.seedProfileDir, args.profileDir);
  }
  ensureDir(args.profileDir);
  log(`启动独立浏览器: ${chromePath}`);
  const context = await chromium.launchPersistentContext(args.profileDir, {
    executablePath: chromePath,
    headless: args.headless,
    viewport: null,
    locale: "zh-CN",
    args: ["--start-maximized"],
  });
  await context.grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: "https://fanqienovel.com",
  }).catch(() => {});

  return {
    browser: null,
    context,
    close: async () => {
      if (!args.keepOpen) {
        await context.close();
      }
    },
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    printHelp();
    return;
  }

  assertArgs(args);
  ensureDir(args.artifactsDir);
  currentLogFile = path.join(args.artifactsDir, "run.log");
  fs.writeFileSync(currentLogFile, "", "utf8");

  const session = await createSession(args);
  try {
    const jobs = args.autoNextCount > 0
      ? await resolveAutoNextJobs(session, args)
      : await loadChapterJobs(args.chaptersDir, args.from, args.to);

    if (!jobs.length) {
      log("本次无需上传，任务结束");
      return;
    }

    log(`准备上传 ${jobs.length} 章: ${jobs.map((item) => item.number).join(", ")}`);
    for (const chapter of jobs) {
      log(`开始上传第${chapter.number}章 ${chapter.title}`);
      await uploadOneChapter(session, args, chapter);
      log(`第${chapter.number}章处理完成`);
    }
  } finally {
    await session.close();
  }
}

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exitCode = 1;
});

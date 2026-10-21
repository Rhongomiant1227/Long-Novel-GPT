import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import readline from "node:readline/promises";

import { chromium } from "playwright-core";
import {
  auditLocalProject,
  compactText as 紧凑文本,
  titleQualityIssues,
} from "./fanqie_title_integrity.mjs";

const 默认起始网址 = "https://fanqienovel.com/main/writer/?enter_from=author_zone";
const 默认_OPENCLAW_CDP_网址 = "http://127.0.0.1:18800";
const 番茄章节标题最大长度 = 10;
const 香港时区 = "Asia/Hong_Kong";
const 退出码_需要登录修复 = 20;
let 当前日志文件 = "";
const 作品路由缓存 = new Map();
const 已绑定页面诊断 = new WeakSet();
const 香港时间格式化器 = new Intl.DateTimeFormat("en-GB", {
  timeZone: 香港时区,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function 打印帮助() {
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
  --ai-usage <yes|no>       发布页选择是否使用 AI 创作，默认 no
  --cdp-url <url>           连接已打开的 Chromium/CDP 浏览器
  --attach-openclaw         连接当前 openclaw 浏览器（默认 ${默认_OPENCLAW_CDP_网址}）
  --profile-dir <dir>       持久化 Chrome profile 目录
  --seed-profile-dir <dir>  从已登录 profile 复制工作副本后再启动
  --chrome-path <path>      Chrome 可执行文件路径
  --draft-only              只走到发布前，不点最终发布
  --preflight-only          只执行本地/远端/连续性预检，不打开编辑器、不创建草稿
  --debug                   输出精简调试快照
  --headless                无头模式启动独立浏览器
  --keep-open               结束后不关闭独立浏览器
  --start-url <url>         作家后台首页
  --artifacts-dir <dir>     失败截图和 HTML 输出目录
  --allow-weak-title        仅跳过“读者向标题”质量门禁；空标题、重复、超长、乱码仍会拦截
  --help                    显示帮助

说明:
  1. 章节文件首行必须形如: 第8章 州簿压名
  2. 默认正文会按: 章节号 -> 标题 -> 正文 的顺序填入
  3. 使用 --attach-openclaw 时，会直接复用已登录的 openclaw 浏览器会话
`);
}

function 解析参数(argv) {
  const args = {
    startUrl: 默认起始网址,
    headless: false,
    draftOnly: false,
    preflightOnly: false,
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
    aiUsage: "no",
    chromePath: "",
    seedProfileDir: "",
    allowWeakTitle: false,
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
      case "--ai-usage":
        args.aiUsage = next();
        break;
      case "--cdp-url":
        args.cdpUrl = next();
        break;
      case "--attach-openclaw":
        args.cdpUrl = 默认_OPENCLAW_CDP_网址;
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
      case "--preflight-only":
        args.preflightOnly = true;
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
      case "--allow-weak-title":
        args.allowWeakTitle = true;
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

function 校验参数(args) {
  if (args.help) {
    return;
  }
  if (!args.bookTitle.trim()) {
    throw new Error("缺少 --book-title");
  }
  if (!args.chaptersDir.trim()) {
    throw new Error("缺少 --chapters-dir");
  }
  args.aiUsage = 规范AI使用值(args.aiUsage, "--ai-usage");
}

function 确保目录存在(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function 重置目录(dir) {
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
}

function 清理文件名(value) {
  return value.replace(/[<>:"/\\|?*\u0000-\u001F]/g, "_");
}

function 规范空白(value) {
  return value.replace(/\s+/g, " ").trim();
}

function 统计文本字符数(value) {
  return Array.from(String(value ?? "")).length;
}

function 转义正则文本(value) {
  return String(value ?? "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function 格式化香港时间(date = new Date()) {
  const parts = Object.create(null);
  for (const part of 香港时间格式化器.formatToParts(date)) {
    if (part.type !== "literal") {
      parts[part.type] = part.value;
    }
  }
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

function 是可重试页面跳转错误(error) {
  const message = error?.message ?? String(error);
  return /page\.goto: Timeout|Timeout \d+ms exceeded|net::ERR_CONNECTION_RESET|net::ERR_CONNECTION_CLOSED|net::ERR_CONNECTION_ABORTED|net::ERR_NETWORK_CHANGED|net::ERR_HTTP2_PROTOCOL_ERROR|net::ERR_TIMED_OUT|net::ERR_INTERNET_DISCONNECTED|net::ERR_NETWORK_IO_SUSPENDED/i.test(message);
}

async function 带重试打开页面(page, url, options = {}, {
  description = "",
  attempts = 3,
  retryWaitMs = 1500,
} = {}) {
  let lastError = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      if (description) {
        记录日志(`${description}${attempt > 1 ? `（重试 ${attempt}/${attempts}）` : ""}: ${url}`);
      }
      await page.goto(url, options);
      return;
    } catch (error) {
      lastError = error;
      if (!是可重试页面跳转错误(error) || attempt >= attempts) {
        throw error;
      }
      const reason = String(error?.message ?? error).split("\n")[0];
      记录日志(`页面跳转异常，准备重试 ${attempt}/${attempts}: ${reason} | ${url}`);
      await page.waitForTimeout(retryWaitMs).catch(() => {});
    }
  }
  throw lastError;
}

function 解析中文章节数字(rawValue) {
  const normalized = String(rawValue ?? "").replace(/\s+/g, "");
  if (!normalized) {
    return NaN;
  }

  const digits = {
    "零": 0,
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
    if (digits[char] !== undefined) {
      number = digits[char];
      continue;
    }
    if (units[char] !== undefined) {
      const unit = units[char];
      if (unit === 10000) {
        section = (section + (number || 0)) * unit;
        total += section;
        section = 0;
        number = 0;
        continue;
      }
      section += (number || 1) * unit;
      number = 0;
      continue;
    }
    return NaN;
  }

  return total + section + number;
}

function 规范AI使用值(value, sourceLabel = "aiUsage") {
  if (typeof value === "boolean") {
    return value ? "yes" : "no";
  }
  const normalized = String(value ?? "").trim().toLowerCase();
  if (!normalized) {
    return "no";
  }
  if (["yes", "true", "1", "y", "ai", "是"].includes(normalized)) {
    return "yes";
  }
  if (["no", "false", "0", "n", "human", "manual", "否"].includes(normalized)) {
    return "no";
  }
  throw new Error(`${sourceLabel} 只支持 yes/no`);
}

function 解析作品ID(publishPath = "", chapterManagePath = "") {
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

function 记录日志(message) {
  const stamp = 格式化香港时间();
  const line = `${stamp} | ${message}`;
  console.log(line);
  if (当前日志文件) {
    fs.appendFileSync(当前日志文件, `${line}\n`, "utf8");
  }
}

async function 等待回车(prompt) {
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

function 解析章节文件内容(content, filePath) {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const titleLine = lines[0]?.replace(/^[\uFEFF\u200B]+/, "").trim() ?? "";
  const match = titleLine.match(/^第\s*([零一二两三四五六七八九十百千万\d]+)\s*章(?:\s+|[：:，,、.．。]\s*)(.+)$/);
  if (!match) {
    throw new Error(`章节标题格式无效: ${filePath}；首行=${JSON.stringify(titleLine)}`);
  }
  const chapterNumber = /^\d+$/.test(match[1])
    ? Number(match[1])
    : 解析中文章节数字(match[1]);
  if (!Number.isFinite(chapterNumber) || chapterNumber <= 0) {
    throw new Error(`章节标题中的章节号无效: ${filePath}`);
  }
  const title = match[2].trim();
  const titleLength = 统计文本字符数(title);
  if (!title) {
    throw new Error(`章节标题为空: ${filePath}`);
  }
  if (titleLength > 番茄章节标题最大长度) {
    throw new Error(
      `章节标题过长: ${filePath}；标题长度=${titleLength}/${番茄章节标题最大长度}；标题=${JSON.stringify(title)}`,
    );
  }
  const hardTitleIssues = titleQualityIssues(title, { strictReaderFacing: false });
  if (hardTitleIssues.length) {
    throw new Error(
      `章节标题存在硬错误: ${filePath}；${hardTitleIssues.join("；")}；标题=${JSON.stringify(title)}`,
    );
  }

  let body = lines.slice(1).join("\n");
  body = body.replace(/^\s+/, "").replace(/\s+$/, "");
  if (!body) {
    throw new Error(`章节正文为空: ${filePath}`);
  }

  return {
    number: chapterNumber,
    title,
    body,
    bodyChars: 紧凑文本(body).length,
    heading: titleLine,
  };
}

async function 按章节号加载章节(chaptersDir, chapterNumber) {
  const filePath = path.join(chaptersDir, `ch_${String(chapterNumber).padStart(4, "0")}.txt`);
  const content = await fsp.readFile(filePath, "utf8");
  return {
    ...解析章节文件内容(content, filePath),
    filePath,
  };
}

async function 加载章节任务(chaptersDir, from, to) {
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
    const parsed = 解析章节文件内容(
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

async function 按连续章节号加载任务(chaptersDir, startNumber, maxCount) {
  const jobs = [];
  for (let current = startNumber; jobs.length < maxCount; current += 1) {
    try {
      jobs.push(await 按章节号加载章节(chaptersDir, current));
    } catch (error) {
      if (error?.code === "ENOENT") {
        break;
      }
      throw error;
    }
  }
  return jobs;
}

function 选择连续章节任务(allJobs, startNumber, maxCount) {
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
      记录日志(`检测到本地章节断档，已在第${expected - 1}章后停止本次选章`);
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

function 检测Chrome可执行文件() {
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

function 复制Profile种子(seedDir, targetDir) {
  if (!fs.existsSync(seedDir)) {
    throw new Error(`种子登录配置目录不存在: ${seedDir}`);
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

  重置目录(targetDir);
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

async function 保存失败产物(page, artifactsDir, prefix) {
  if (!page || page.isClosed()) {
    return "";
  }
  确保目录存在(artifactsDir);
  const safePrefix = 清理文件名(prefix);
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

async function 获取首个可见定位器(factories) {
  for (const createLocator of factories) {
    const locator = createLocator();
    try {
      const count = await locator.count();
      for (let index = 0; index < count; index += 1) {
        const candidate = locator.nth(index);
        try {
          if (await candidate.isVisible()) {
            return candidate;
          }
        } catch {}
      }
    } catch {}
  }
  return null;
}

async function 点击首个可见元素(page, factories, description) {
  const locator = await 获取首个可见定位器(
    factories.map((factory) => () => factory(page)),
  );
  if (!locator) {
    return false;
  }
  try {
    await locator.click();
  } catch (error) {
    const message = error?.message ?? String(error);
    if (!/intercepts pointer events|element click intercepted|another element would receive/i.test(message)) {
      throw error;
    }
    记录日志(`常规点击被页面浮层拦截，改用强制点击${description ? ` [${description}]` : ""}: ${message.split("\n")[0]}`);
    await locator.click({ force: true, timeout: 5000 });
  }
  if (description) {
    记录日志(description);
  }
  return true;
}

function 序列化正则(value) {
  return {
    source: value.source,
    flags: value.flags,
  };
}

function 是页面关闭错误(error) {
  const message = error?.message ?? String(error);
  return /Target page, context or browser has been closed|has been closed/i.test(message);
}

function 创建需要登录修复错误(message = "检测到未登录状态，且当前为 headless 模式；需要先修复种子登录态") {
  const error = new Error(message);
  error.exitCode = 退出码_需要登录修复;
  error.code = "LOGIN_REQUIRED";
  return error;
}

function 绑定页面诊断(page, label) {
  if (!page || 已绑定页面诊断.has(page)) {
    return;
  }
  已绑定页面诊断.add(page);

  page.on("dialog", async (dialog) => {
    const type = typeof dialog.type === "function" ? dialog.type() : "unknown";
    const message = typeof dialog.message === "function"
      ? 规范空白(dialog.message())
      : "";
    记录日志(`检测到浏览器对话框(${label}): type=${type}${message ? ` message=${message}` : ""}`);
    if (type === "beforeunload") {
      await dialog.dismiss().catch(() => {});
      return;
    }
    await dialog.accept().catch(() => {});
  });

  page.on("close", () => {
    记录日志(`页面已关闭(${label})`);
  });

  page.on("crash", () => {
    记录日志(`页面已崩溃(${label})`);
  });
}

async function 动作后等待(page, timeoutMs, publishClicked) {
  try {
    await page.waitForTimeout(timeoutMs);
    return true;
  } catch (error) {
    if (publishClicked && 是页面关闭错误(error)) {
      return false;
    }
    throw error;
  }
}

async function 点击作用域内可见文本(
  page,
  {
    selectors,
    targetPattern,
    scopePattern,
    description,
    skipIfSelected = false,
    rootSelectors = "",
    rootPattern = null,
  },
) {
  const result = await page.evaluate(
    ({ selectors: rawSelectors, target, scope, rawSkipIfSelected, rawRootSelectors, root }) => {
      const targetRe = new RegExp(target.source, target.flags);
      const scopeRe = scope ? new RegExp(scope.source, scope.flags) : null;
      const rootRe = root ? new RegExp(root.source, root.flags) : null;

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
      const textOf = (el) => normalize(
        el.innerText || el.textContent || el.getAttribute?.("aria-label") || "",
      );
      const parseZIndex = (value) => {
        const parsed = Number.parseInt(String(value ?? ""), 10);
        return Number.isFinite(parsed) ? parsed : 0;
      };
      const isSelected = (el) => {
        const seen = new Set();
        const queue = [
          el,
          el.closest("label, button, [role='radio'], [role='checkbox'], [role='button']"),
        ].filter(Boolean);

        while (queue.length) {
          const node = queue.shift();
          if (!node || seen.has(node)) {
            continue;
          }
          seen.add(node);

          if (node instanceof HTMLInputElement && (node.checked || node.matches(":checked"))) {
            return true;
          }
          if (node instanceof Element) {
            if (
              node.getAttribute("aria-checked") === "true" ||
              node.getAttribute("aria-selected") === "true" ||
              node.getAttribute("data-state") === "checked" ||
              node.classList.contains("checked") ||
              node.classList.contains("is-checked") ||
              node.classList.contains("active") ||
              /\barco-radio-checked\b|\barco-checkbox-checked\b|\bselected\b|\bactive\b/i.test(node.className || "")
            ) {
              return true;
            }
            const classText = normalize(node.className || "");
            const nodeText = textOf(node);
            if (
              nodeText &&
              classText &&
              /是|否|使用AI|未使用AI|不使用AI|没有使用AI|有使用AI|已使用AI/.test(nodeText) &&
              /checked|selected|active/.test(classText)
            ) {
              return true;
            }
            for (const child of node.querySelectorAll("input, [role='radio'], [role='checkbox']")) {
              queue.push(child);
            }
          }
        }
        return false;
      };

      let scopedRoot = null;
      if (rawRootSelectors && rootRe) {
        const rootCandidates = Array.from(document.querySelectorAll(rawRootSelectors));
        for (let index = 0; index < rootCandidates.length; index += 1) {
          const candidate = rootCandidates[index];
          if (!(candidate instanceof Element) || !isVisible(candidate)) {
            continue;
          }
          const candidateText = textOf(candidate);
          if (!candidateText || !rootRe.test(candidateText)) {
            continue;
          }
          const rect = candidate.getBoundingClientRect();
          const style = window.getComputedStyle(candidate);
          const scored = {
            el: candidate,
            zIndex: parseZIndex(style?.zIndex),
            area: Math.max(rect.width, 0) * Math.max(rect.height, 0),
            domIndex: index,
          };
          if (
            !scopedRoot ||
            scored.zIndex > scopedRoot.zIndex ||
            (
              scored.zIndex === scopedRoot.zIndex &&
              scored.area < scopedRoot.area
            ) ||
            (
              scored.zIndex === scopedRoot.zIndex &&
              scored.area === scopedRoot.area &&
              scored.domIndex > scopedRoot.domIndex
            )
          ) {
            scopedRoot = scored;
          }
        }
      }

      let best = null;
      if (rawRootSelectors && rootRe && !scopedRoot?.el) {
        return { status: "root_not_found" };
      }

      const container = scopedRoot?.el ?? document;
      const candidates = Array.from(container.querySelectorAll(rawSelectors));
      for (let index = 0; index < candidates.length; index += 1) {
        const el = candidates[index];
        if (!isVisible(el)) {
          continue;
        }
        const text = textOf(el);
        if (!text || !targetRe.test(text)) {
          continue;
        }

        let score = index + 1_000_000;
        let containerText = "";
        if (scopeRe) {
          let matched = false;
          let depth = 0;
          let current = el;
          while (current && depth < 10) {
            if (isVisible(current)) {
              const currentText = textOf(current);
              if (currentText && scopeRe.test(currentText)) {
                matched = true;
                score = currentText.length * 100 + depth;
                containerText = currentText;
                break;
              }
            }
            current = current.parentElement;
            depth += 1;
          }
          if (!matched) {
            continue;
          }
        }

        if (!best || score < best.score) {
          best = {
            el,
            score,
            selected: isSelected(el),
          };
        }
      }

      if (!best) {
        return { status: "not_found" };
      }
      if (rawSkipIfSelected && best.selected) {
        return { status: "already_selected" };
      }

      const clickable = best.el.closest(
        "label, button, [role='radio'], [role='checkbox'], [role='button']",
      ) || best.el;
      clickable.click();
      return { status: "clicked" };
    },
    {
      selectors,
      target: 序列化正则(targetPattern),
      scope: scopePattern ? 序列化正则(scopePattern) : null,
      rawSkipIfSelected: skipIfSelected,
      rawRootSelectors: rootSelectors,
      root: rootPattern ? 序列化正则(rootPattern) : null,
    },
  ).catch(() => ({ status: "not_found" }));

  if (result.status !== "clicked") {
    if (result.status === "already_selected") {
      if (description) {
        记录日志(`${description}（已选中）`);
      }
      return true;
    }
    return false;
  }
  if (description) {
    记录日志(description);
  }
  return true;
}

async function 检测明确发布完成(page, chapter) {
  const expected = 规范空白(`第${chapter.number}章 ${chapter.title}`);
  const lastSubmittedText = 规范空白(
    await page
      .locator(".publish-maintain-info .newcontent, .newcontent.title-label")
      .first()
      .innerText()
      .catch(() => ""),
  );
  if (lastSubmittedText.includes("上次提交") && lastSubmittedText.includes(expected)) {
    return `上次提交已更新为 ${expected}`;
  }

  const chapterManageStatus = await page
    .evaluate((target) => {
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

      for (const row of document.querySelectorAll("tr, .arco-table-tr, [role='row']")) {
        if (!(row instanceof Element) || !isVisible(row)) {
          continue;
        }
        const text = normalize(row.innerText || row.textContent || "");
        if (!text || !text.includes(target)) {
          continue;
        }
        const statusMatch = text.match(/(审核中|已发布)/);
        if (statusMatch) {
          return statusMatch[1];
        }
      }
      return "";
    }, expected)
    .catch(() => "");
  if (chapterManageStatus) {
    return `章节管理已显示 ${expected}，状态=${chapterManageStatus}`;
  }

  return (
    await page
      .evaluate(() => {
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

        const candidates = Array.from(
          document.querySelectorAll(
            [
              "[role='alert']",
              "[role='dialog']",
              ".arco-message",
              ".arco-modal",
              "[class*='message']",
              "[class*='toast']",
              "[class*='notice']",
              "[class*='modal']",
              "body *",
            ].join(","),
          ),
        );
        const seen = new Set();
        for (const el of candidates) {
          if (!(el instanceof Element) || seen.has(el)) {
            continue;
          }
          seen.add(el);
          if (!isVisible(el)) {
            continue;
          }
          if (el.closest(".warning-tip, .editor-tip, .warning-tip-content")) {
            continue;
          }

          const text = normalize(el.innerText || el.textContent || "");
          if (!text) {
            continue;
          }

          const overlayLike = el.matches(
            [
              "[role='alert']",
              "[role='dialog']",
              ".arco-message",
              ".arco-modal",
              "[class*='message']",
              "[class*='toast']",
              "[class*='notice']",
              "[class*='modal']",
            ].join(","),
          );
          if (!overlayLike && text.length > 120) {
            continue;
          }

          if (/发布成功|提交成功|发布完成/.test(text)) {
            return text;
          }
          if (/已发布/.test(text) && /审核中/.test(text)) {
            return text;
          }
          if (overlayLike && /^(?:审核中|已发布|提交成功|发布成功)$/u.test(text)) {
            return text;
          }
        }
        return "";
      })
      .catch(() => "")
  );
}

async function 是登录页(page) {
  if (/\/main\/writer\/login(?:[/?#]|$)/.test(page.url())) {
    return true;
  }

  const pageText = await page.evaluate(() => String(document.body?.innerText || ""))
    .catch(() => "");
  if (/验证码登录|扫码登录|密码登录/.test(pageText) && /登录\/注册|登录/.test(pageText)) {
    return true;
  }

  const locator = page.getByRole("button", { name: /立即登录|登录\/注册|登录/ }).first();
  try {
    return (await locator.count()) > 0 && (await locator.isVisible());
  } catch {
    return false;
  }
}

async function 自动等待登录完成(page, timeoutMs = 10 * 60 * 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!(await 是登录页(page))) {
      return true;
    }
    await page.waitForTimeout(1000);
  }
  return false;
}

async function 等待工作台就绪(page, bookTitle) {
  await page.waitForLoadState("domcontentloaded");
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});

  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const ready = await 获取首个可见定位器([
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

async function 在工作台选择作品(page, bookTitle) {
  const combo = page.getByRole("combobox").first();
  try {
    if ((await combo.count()) > 0 && (await combo.isVisible())) {
      await combo.click();
      const option = page.getByRole("option", { name: bookTitle }).first();
      if ((await option.count()) > 0) {
        await option.click();
        记录日志(`已选择作品: ${bookTitle}`);
        await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
      }
    }
  } catch {}
}

async function 从作品管理解析作品路由(page, startUrl, bookTitle) {
  const bookManageUrl = new URL("/main/writer/book-manage", startUrl).toString();
  await 带重试打开页面(page, bookManageUrl, { waitUntil: "domcontentloaded" }, {
    description: `进入作品管理，查找《${bookTitle}》`,
  });
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
  }, bookTitle).catch(() => null);

  if (!routeInfo?.publishPath && !routeInfo?.chapterManagePath) {
    throw new Error(`作品管理中未找到《${bookTitle}》的章节入口`);
  }

  const bookId = 解析作品ID(routeInfo.publishPath, routeInfo.chapterManagePath);
  if (!bookId) {
    throw new Error(`无法从作品管理页解析《${bookTitle}》的作品 ID`);
  }

  const resolved = {
    bookId,
    publishUrl: routeInfo.publishPath ? new URL(routeInfo.publishPath, startUrl).toString() : "",
    chapterManageUrl: routeInfo.chapterManagePath
      ? new URL(routeInfo.chapterManagePath, startUrl).toString()
      : "",
  };
  记录日志(`已定位作品: ${bookTitle} (bookId=${bookId})`);
  return resolved;
}

async function 获取作品路由(page, startUrl, bookTitle) {
  const cacheKey = `${startUrl}::${规范空白(bookTitle)}`;
  if (作品路由缓存.has(cacheKey)) {
    return 作品路由缓存.get(cacheKey);
  }
  const resolved = await 从作品管理解析作品路由(page, startUrl, bookTitle);
  作品路由缓存.set(cacheKey, resolved);
  return resolved;
}

async function 点击作品卡片入口(page, bookTitle, actionText, description) {
  const scopePattern = new RegExp(转义正则文本(规范空白(bookTitle)));
  if (
    await 点击作用域内可见文本(page, {
      selectors: "a, button, [role='button'], [role='link'], span",
      targetPattern: new RegExp(`^${转义正则文本(actionText)}$|${转义正则文本(actionText)}`),
      scopePattern,
      description,
    })
  ) {
    return true;
  }

  return await 点击首个可见元素(
    page,
    [
      (p) => p.getByRole("link", { name: actionText }),
      (p) => p.getByRole("button", { name: actionText }),
      (p) => p.locator("a").filter({ hasText: actionText }),
      (p) => p.locator("button").filter({ hasText: actionText }),
    ],
    description,
  );
}

async function 读取当前发布页作品名(page) {
  return 规范空白(
    await page
      .locator(".publish-header-book-name")
      .first()
      .innerText()
      .catch(async () => {
        const bodyText = await page.locator("body").innerText().catch(() => "");
        const match = bodyText.match(/作品名称[：:]\s*(.+?)(?:\r?\n|$)/);
        return match?.[1] ?? "";
      }),
  );
}

async function 校验当前发布页作品(page, bookTitle) {
  const actualTitle = await 读取当前发布页作品名(page);
  if (!actualTitle) {
    return;
  }
  if (规范空白(actualTitle) !== 规范空白(bookTitle)) {
    throw new Error(`进入了错误的作品发布页：目标为《${bookTitle}》，实际为《${actualTitle}》`);
  }
}

function 解析上次提交章节(text) {
  const normalized = 规范空白(text);
  const match = normalized.match(/上次提交[：:]\s*(?:第\S+卷\s*)?第\s*(\d+)\s*章\s+(.+?)(?=\s*(?:已保存|保存中|审核中|已发布|正文字数|存草稿|下一步|上次提交|作品名称|$))/);
  if (!match) {
    return null;
  }

  return {
    number: Number(match[1]),
    title: 规范空白(match[2]),
    raw: match[0],
  };
}

function 解析最近更新章节(text) {
  const lines = text.split(/\r?\n/);
  for (const rawLine of lines) {
    const line = 规范空白(rawLine);
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

function 从文本解析章节号列表(text) {
  const numbers = [];
  for (const rawLine of text.split(/\r?\n/)) {
    const line = 规范空白(rawLine);
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

async function 从章节管理页提取可见文本(page) {
  return await page.evaluate(() => {
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

    const selectors = [
      "tr",
      ".arco-table-tr",
      "[role='row']",
      "table",
      ".arco-table",
    ];
    const results = [];
    const seen = new Set();
    for (const selector of selectors) {
      for (const node of document.querySelectorAll(selector)) {
        if (!(node instanceof Element) || !isVisible(node)) {
          continue;
        }
        const text = normalize(node.innerText || node.textContent || "");
        if (!text || seen.has(text)) {
          continue;
        }
        seen.add(text);
        results.push(text);
      }
    }
    return results;
  }).catch(() => []);
}

async function 从章节管理页读取章节号列表(page, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const texts = await 从章节管理页提取可见文本(page);
    const numbers = [...new Set(texts.flatMap((text) => 从文本解析章节号列表(text)))];
    if (numbers.length) {
      return numbers;
    }
    await page.waitForTimeout(1000);
  }
  return [];
}

async function 从章节管理页读取标题明细(page) {
  const rows = await page.evaluate(() => {
    const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
    const result = [];
    const tableRows = Array.from(
      document.querySelectorAll("tbody tr.arco-table-tr, tbody tr, .arco-table-tbody tr"),
    );
    for (const row of tableRows) {
      const cells = Array.from(row.querySelectorAll("td")).map((cell) => normalize(cell.innerText || cell.textContent || ""));
      if (!cells.length) {
        continue;
      }
      const heading = cells[0] || "";
      if (!/^第.+章/.test(heading)) {
        continue;
      }
      const match = heading.match(/^第\s*([零〇一二两三四五六七八九十百千万\d]+)\s*章(?:\s+|[：:，,、.．。]\s*)?(.*)$/);
      const previewLink = row.querySelector("a[href*='/preview/']");
      result.push({
        heading,
        numberToken: match?.[1] ?? "",
        title: normalize(match?.[2] ?? ""),
        previewUrl: previewLink?.href || "",
        chars: cells[1] || "",
        typoCount: cells[2] || "",
        status: cells[3] || "",
        publishTime: cells[4] || "",
      });
    }
    const currentPage = normalize(document.querySelector(".arco-pagination-item-active")?.textContent || "1");
    const visiblePageNumbers = Array.from(
      document.querySelectorAll(".arco-pagination-item[aria-label^='第 ' ]"),
    )
      .map((item) => Number((item.getAttribute("aria-label") || "").match(/第\\s*(\\d+)\\s*页/)?.[1] || 0))
      .filter((number) => Number.isInteger(number) && number > 0);
    const totalPages = visiblePageNumbers.length ? Math.max(...visiblePageNumbers) : 0;
    const nextItem = document.querySelector(".arco-pagination-item-next");
    const nextDisabled = !nextItem || nextItem.classList.contains("arco-pagination-item-disabled");
    return { rows: result, currentPage, totalPages, nextDisabled };
  }).catch(() => ({ rows: [], currentPage: "1", totalPages: 0, nextDisabled: true }));

  return rows.rows.map((row) => {
    const chapterNumber = /^\d+$/.test(row.numberToken)
      ? Number(row.numberToken)
      : 解析中文章节数字(row.numberToken);
    return {
      chapterNumber: Number.isFinite(chapterNumber) && chapterNumber > 0 ? chapterNumber : 0,
      chapterTitle: 规范空白(row.title),
      fullHeading: row.heading,
      previewUrl: row.previewUrl,
      status: row.status,
      publishTime: row.publishTime,
      chars: row.chars,
      typoCount: row.typoCount,
      currentPage: rows.currentPage,
      totalPages: rows.totalPages,
      nextDisabled: rows.nextDisabled,
    };
  });
}

async function 点击章节管理下一页(page) {
  const currentPage = await page.locator(".arco-pagination-item-active").first().innerText().catch(() => "1");
  const currentFirstRow = await page.locator("tbody tr, .arco-table-tbody tr").first().innerText().catch(() => "");
  const currentPageNumber = Number(规范空白(currentPage).match(/\d+/)?.[0] || 1);
  const nextPageNumber = currentPageNumber + 1;
  const pageLocator = page
    .locator(`.arco-pagination-item[aria-label="第 ${nextPageNumber} 页"]`)
    .first();
  const nextLocator = page.locator(".arco-pagination-item-next").first();
  const pageCount = await pageLocator.count().catch(() => 0);
  const nextClass = await nextLocator.getAttribute("class").catch(() => "");
  if (!pageCount && (!nextClass || /arco-pagination-item-disabled/.test(nextClass))) {
    return false;
  }

  const chapterListResponse = page.waitForResponse(
    (response) => {
      if (!response.url().includes("/api/author/chapter/chapter_list/v1")) {
        return false;
      }
      try {
        return Number(new URL(response.url()).searchParams.get("page_index")) === nextPageNumber - 1;
      } catch {
        return false;
      }
    },
    { timeout: 20000 },
  );

  // Playwright's forced pointer click can be swallowed by Arco's pagination
  // after the table refreshes. A native DOM click reliably reaches React.
  const clickTarget = pageCount ? pageLocator : nextLocator;
  await clickTarget.evaluate((element) => element.click());
  const response = await chapterListResponse;
  if (!response.ok()) {
    throw new Error(
      `章节管理翻页接口失败：目标页=${nextPageNumber}，HTTP=${response.status()}`,
    );
  }

  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const nextPage = await page.locator(".arco-pagination-item-active").first().innerText().catch(() => "");
    const nextFirstRow = await page.locator("tbody tr, .arco-table-tbody tr").first().innerText().catch(() => "");
    const nextPageNumberObserved = Number(规范空白(nextPage).match(/\d+/)?.[0] || 0);
    if (
      (nextPageNumberObserved && nextPageNumberObserved !== currentPageNumber) ||
      (规范空白(nextFirstRow) && 规范空白(nextFirstRow) !== 规范空白(currentFirstRow))
    ) {
      return true;
    }
    await page.waitForTimeout(400);
  }

  throw new Error("章节管理翻页失败：当前页=" + (规范空白(currentPage) || "1"));
}

async function 章节管理已显示目标章节(page, expected, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const texts = await 从章节管理页提取可见文本(page);
    if (texts.some((text) => text.includes(expected))) {
      return true;
    }
    await page.waitForTimeout(1000);
  }
  return false;
}

async function 等待章节管理目标明细(page, chapter, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const rows = await 从章节管理页读取标题明细(page);
    const target = rows.find(
      (row) =>
        row.chapterNumber === chapter.number &&
        规范空白(row.chapterTitle) === 规范空白(chapter.title),
    );
    if (target) {
      return target;
    }
    await page.waitForTimeout(1000);
    await page.reload({ waitUntil: "domcontentloaded" }).catch(() => {});
    await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
  }
  return null;
}

async function 校验官网预览正文(page, chapter, previewUrl) {
  if (!previewUrl) {
    throw new Error(`第${chapter.number}章章节管理行缺少预览链接，无法完成发布后正文复核`);
  }
  await 带重试打开页面(page, previewUrl, { waitUntil: "domcontentloaded" }, {
    description: `打开第${chapter.number}章官网预览复核正文`,
  });
  await page.locator(".preview-content").waitFor({ state: "visible", timeout: 30000 });
  const preview = await page.evaluate(() => ({
    heading: String(document.querySelector(".preview-title")?.textContent ?? "")
      .replace(/\s+/g, " ")
      .trim(),
    body: Array.from(document.querySelectorAll(".preview-content p"))
      .map((paragraph) => String(paragraph.innerText || paragraph.textContent || "").trim())
      .filter(Boolean)
      .join("\n\n"),
  }));
  const expectedHeading = 规范空白(`第${chapter.number}章 ${chapter.title}`);
  if (!规范空白(preview.heading).includes(expectedHeading)) {
    throw new Error(
      `第${chapter.number}章官网预览标题不一致：期望=${JSON.stringify(expectedHeading)}，实际=${JSON.stringify(preview.heading)}`,
    );
  }
  if (紧凑文本(preview.body) !== 紧凑文本(chapter.body)) {
    throw new Error(
      `第${chapter.number}章官网预览正文不一致：期望=${紧凑文本(chapter.body).length}字，实际=${紧凑文本(preview.body).length}字`,
    );
  }
  记录日志(`第${chapter.number}章官网预览标题与正文逐字复核通过`);
}

async function 从编辑器获取上次提交章节(page) {
  const deadline = Date.now() + 30000;
  let latestText = "";
  while (Date.now() < deadline) {
    await page.waitForLoadState("networkidle", { timeout: 3000 }).catch(() => {});
    const bodyText = await page.locator("body").innerText().catch(() => "");
    latestText = 规范空白(bodyText);
    const parsed = 解析上次提交章节(latestText);
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

async function 校验上一章匹配(
  page,
  chapter,
  chaptersDir,
  verifiedRemoteTitlesByNumber = null,
) {
  if (chapter.number <= 1) {
    记录日志("当前是第1章，跳过上一章连续性校验");
    return;
  }

  const previousLocal = await 按章节号加载章节(chaptersDir, chapter.number - 1);
  const previousRemote = await 从编辑器获取上次提交章节(page);
  const verifiedRemoteTitle = verifiedRemoteTitlesByNumber?.get(previousLocal.number);
  const chapterManageConfirmedPrevious =
    verifiedRemoteTitle &&
    规范空白(verifiedRemoteTitle) === 规范空白(previousLocal.title);

  if (!previousRemote || previousRemote.number === 0) {
    if (chapterManageConfirmedPrevious) {
      记录日志(
        `发布页未提供可靠“上次提交”，但章节管理全集已确认前章为第${previousLocal.number}章 ${previousLocal.title}，按章节管理结果继续`,
      );
      return;
    }
    const observedText = previousRemote?.raw ? `；页面文本片段：${previousRemote.raw.slice(0, 200)}` : "";
    throw new Error(
      `未能从发布页读取“上次提交”信息，拒绝上传第${chapter.number}章；期望上一章为第${previousLocal.number}章 ${previousLocal.title}${observedText}`,
    );
  }

  if (previousRemote.number !== previousLocal.number) {
    if (chapterManageConfirmedPrevious) {
      记录日志(
        `发布页“上次提交”停留在第${previousRemote.number}章 ${previousRemote.title}，但章节管理全集已确认前章为第${previousLocal.number}章 ${previousLocal.title}，按章节管理结果继续`,
      );
      return;
    }
    throw new Error(
      `发布页“上次提交”为第${previousRemote.number}章 ${previousRemote.title}，但本次上传前一章应为第${previousLocal.number}章 ${previousLocal.title}，已拒绝上传`,
    );
  }

  if (规范空白(previousRemote.title) !== 规范空白(previousLocal.title)) {
    if (chapterManageConfirmedPrevious) {
      记录日志(
        `发布页“上次提交”标题为“${previousRemote.title}”，但章节管理全集已确认第${previousLocal.number}章标题为“${previousLocal.title}”，按章节管理结果继续`,
      );
      return;
    }
    throw new Error(
      `发布页“上次提交”标题不匹配：远端为“${previousRemote.title}”，本地上一章为“${previousLocal.title}”，已拒绝上传`,
    );
  }

  记录日志(`上一章连续性校验通过：远端第${previousRemote.number}章 ${previousRemote.title}`);
}

async function 获取最新已发布章节(page, startUrl, bookTitle, loginOptions = {}) {
  记录日志(`读取《${bookTitle}》远端最新已发布章节`);
  await 确保已登录(page, startUrl, loginOptions);
  const routes = await 获取作品路由(page, startUrl, bookTitle);
  if (!routes.chapterManageUrl) {
    throw new Error(`未找到《${bookTitle}》的章节管理地址`);
  }

  记录日志("转到章节管理读取最新章节（含审核中章节）");
  await 带重试打开页面(page, routes.chapterManageUrl, { waitUntil: "domcontentloaded" }, {
    description: "转到章节管理读取最新章节（含审核中章节）",
  });
  记录日志("已进入章节管理");
  await page.waitForLoadState("domcontentloaded");
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
  const numbers = await 从章节管理页读取章节号列表(page, 15000);
  if (!numbers.length) {
    记录日志("远端未解析到任何已发布章节，按从第1章开始处理");
    return 0;
  }
  const latest = Math.max(...numbers);
  记录日志(`章节管理显示最新章节为第${latest}章`);
  return latest;
}

async function 获取远端章节标题列表(page, startUrl, bookTitle, loginOptions = {}) {
  记录日志(`读取《${bookTitle}》远端章节标题全集`);
  await 确保已登录(page, startUrl, loginOptions);
  const routes = await 获取作品路由(page, startUrl, bookTitle);
  if (!routes.chapterManageUrl) {
    throw new Error(`未找到《${bookTitle}》的章节管理地址`);
  }

  await 带重试打开页面(page, routes.chapterManageUrl, { waitUntil: "domcontentloaded" }, {
    description: `进入《${bookTitle}》章节管理读取标题全集`,
  });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});

  const seenPages = new Set();
  const seenRows = new Set();
  const rows = [];

  let fallbackPageNumber = 1;
  while (true) {
    const pageRows = await 从章节管理页读取标题明细(page);
    const currentPage = pageRows[0]?.currentPage ?? await page.locator(".arco-pagination-item-active").first().innerText().catch(() => "1");
    const normalizedPage = 规范空白(currentPage) || "1";
    const currentPageNumber = Number(normalizedPage) || fallbackPageNumber;
    const totalPages = pageRows[0]?.totalPages || 0;
    if (seenPages.has(normalizedPage)) {
      throw new Error("章节管理分页重复：页码=" + normalizedPage + "，已读取" + rows.length + "章");
    }
    seenPages.add(normalizedPage);

    for (const row of pageRows) {
      const uniqueKey = `${row.fullHeading}@@${row.publishTime}@@${row.status}`;
      if (seenRows.has(uniqueKey)) {
        continue;
      }
      seenRows.add(uniqueKey);
      rows.push({
        chapterNumber: row.chapterNumber,
        chapterTitle: row.chapterTitle,
        fullHeading: row.fullHeading,
        previewUrl: row.previewUrl,
        status: row.status,
        publishTime: row.publishTime,
      });
    }

    if (!pageRows.length) {
      break;
    }
    if ((totalPages && currentPageNumber >= totalPages) || pageRows[0].nextDisabled) {
      break;
    }
    const moved = await 点击章节管理下一页(page);
    if (!moved) {
      throw new Error(
        "章节管理翻页控件不可用：当前页=" + normalizedPage + "，总页数=" + (totalPages || "未知"),
      );
    }
    fallbackPageNumber = currentPageNumber + 1;
    if (currentPageNumber === 1 || currentPageNumber % 10 === 0) {
      记录日志(
        "远端标题审计已读取第" + currentPageNumber + "页" + (totalPages ? "/" + totalPages : "") + "，累计" + rows.length + "章",
      );
    }
  }

  rows.sort((a, b) => a.chapterNumber - b.chapterNumber);
  记录日志(`远端共读取 ${rows.length} 个章节标题`);
  return rows;
}

async function 校验待上传章节标题不重复(session, args, jobs) {
  if (!jobs.length) {
    return;
  }

  const page = await session.context.newPage();
  绑定页面诊断(page, `title_audit_${args.bookTitle}`);
  try {
    const remoteRows = await 获取远端章节标题列表(page, args.startUrl, args.bookTitle, args);
    const remoteTitleMap = new Map();
    const remoteNumberSet = new Set();
    const remoteErrors = [];
    for (const row of remoteRows) {
      const normalizedTitle = 规范空白(row.chapterTitle);
      if (!Number.isInteger(row.chapterNumber) || row.chapterNumber <= 0) {
        remoteErrors.push(`远端存在无法解析章号的章节：${row.fullHeading}`);
        continue;
      }
      if (remoteNumberSet.has(row.chapterNumber)) {
        remoteErrors.push(`远端第${row.chapterNumber}章出现多条记录`);
      }
      remoteNumberSet.add(row.chapterNumber);
      const qualityIssues = titleQualityIssues(normalizedTitle, {
        strictReaderFacing: !args.allowWeakTitle,
      });
      for (const issue of qualityIssues) {
        remoteErrors.push(`远端第${row.chapterNumber}章 ${issue}：《${normalizedTitle}》`);
      }
      if (!normalizedTitle) {
        continue;
      }
      if (!remoteTitleMap.has(normalizedTitle)) {
        remoteTitleMap.set(normalizedTitle, []);
      }
      remoteTitleMap.get(normalizedTitle).push(row.chapterNumber);
    }
    for (const [title, numbers] of remoteTitleMap) {
      if (numbers.length > 1) {
        remoteErrors.push(
          `远端标题重复：《${title}》出现在 ${numbers.map((number) => `第${number}章`).join("、")}`,
        );
      }
    }
    const latestRemoteNumber = Math.max(0, ...remoteRows.map((row) => row.chapterNumber || 0));
    for (let number = 1; number <= latestRemoteNumber; number += 1) {
      if (!remoteNumberSet.has(number)) {
        remoteErrors.push(`远端章节号断档：缺少第${number}章`);
      }
    }
    const remoteReportPath = 写入远端审计报告(args, remoteRows, remoteErrors);
    if (remoteErrors.length) {
      throw new Error(
        `远端已发布章节全量审计失败，已停止发布；报告=${remoteReportPath}\n${remoteErrors.slice(0, 30).join("\n")}`,
      );
    }
    记录日志(
      `远端已发布章节全量审计通过：${remoteRows.length}章，标题质量、唯一性和章号连续性正常；报告=${remoteReportPath}`,
    );
    args.verifiedRemoteTitlesByNumber = new Map(
      remoteRows.map((row) => [row.chapterNumber, row.chapterTitle]),
    );
    args.verifiedRemoteRowsByNumber = new Map(
      remoteRows.map((row) => [row.chapterNumber, row]),
    );

    const allLocal = auditLocalProject({
      chaptersDir: args.chaptersDir,
      selectedNumbers: [],
      strictReaderFacing: !args.allowWeakTitle,
    });
    const pendingRows = allLocal.rows.filter((row) => row.fileNumber > latestRemoteNumber);
    const pendingErrors = [];
    const pendingTitleMap = new Map();
    for (const row of pendingRows) {
      for (const issue of row.issues) {
        pendingErrors.push(`第${row.fileNumber}章 ${issue}`);
      }
      const normalizedTitle = 规范空白(row.title);
      const remoteNumbers = remoteTitleMap.get(normalizedTitle) ?? [];
      if (remoteNumbers.length) {
        pendingErrors.push(
          `第${row.fileNumber}章标题《${row.title}》与远端章节 ${remoteNumbers.map((number) => `第${number}章`).join("、")} 重复`,
        );
      }
      const previousPending = pendingTitleMap.get(normalizedTitle);
      if (normalizedTitle && previousPending) {
        pendingErrors.push(
          `第${previousPending}章与第${row.fileNumber}章标题重复：《${row.title}》`,
        );
      }
      if (normalizedTitle) {
        pendingTitleMap.set(normalizedTitle, row.fileNumber);
      }
    }
    const pendingReportPath = 写入上传前审计报告(
      args,
      {
        selectedRows: pendingRows,
        missingSelected: [],
        errors: pendingErrors,
      },
      "all_pending",
    );
    if (pendingErrors.length) {
      throw new Error(
        `未上传章节全量审计失败，已停止发布；远端最新第${latestRemoteNumber}章，待上传${pendingRows.length}章，报告=${pendingReportPath}\n${pendingErrors.slice(0, 30).join("\n")}`,
      );
    }
    记录日志(
      `未上传章节全量审计通过：远端最新第${latestRemoteNumber}章，本地后续${pendingRows.length}章；报告=${pendingReportPath}`,
    );

    const seenBatchTitles = new Map();
    for (const job of jobs) {
      const normalizedTitle = 规范空白(job.title);
      if (!normalizedTitle) {
        throw new Error(`第${job.number}章标题为空，无法上传`);
      }
      const qualityIssues = titleQualityIssues(job.title, {
        strictReaderFacing: !args.allowWeakTitle,
      });
      if (qualityIssues.length) {
        throw new Error(
          `第${job.number}章标题质量校验失败：《${job.title}》；${qualityIssues.join("；")}`,
        );
      }

      const remoteNumbers = remoteTitleMap.get(normalizedTitle) ?? [];
      if (remoteNumbers.length) {
        throw new Error(
          `待上传标题与远端重复：第${job.number}章《${job.title}》撞上远端章节 ${remoteNumbers.map((num) => `第${num}章`).join("、")}`,
        );
      }

      const previousBatchNumber = seenBatchTitles.get(normalizedTitle);
      if (previousBatchNumber) {
        throw new Error(
          `本次待上传章节存在重复标题：第${previousBatchNumber}章与第${job.number}章均为《${job.title}》`,
        );
      }
      seenBatchTitles.set(normalizedTitle, job.number);
    }

    记录日志(`待上传章节标题校验通过：${jobs.length}章，未与远端或本批次重复`);
  } finally {
    await page.close().catch(() => {});
  }
}

async function 解析自动续传任务(session, args) {
  const page = await session.context.newPage();
  绑定页面诊断(page, `auto_select_${args.bookTitle}`);
  try {
    await 确保已登录(page, args.startUrl, args);
    const latestPublished = await 获取最新已发布章节(page, args.startUrl, args.bookTitle, args);
    const startNumber = latestPublished > 0 ? latestPublished + 1 : 1;
    const selected = await 按连续章节号加载任务(
      args.chaptersDir,
      startNumber,
      args.autoNextCount,
    );
    if (!selected.length) {
      记录日志(`没有待上传的新章节；远端已到第${latestPublished}章，本地没有连续后续章节`);
      return [];
    }
    记录日志(`自动选章完成：将从第${selected[0].number}章上传到第${selected[selected.length - 1].number}章`);
    return selected;
  } finally {
    await page.close().catch(() => {});
  }
}

async function 确保已登录(page, startUrl, options = {}) {
  await 带重试打开页面(page, startUrl, { waitUntil: "domcontentloaded" }, {
    description: "打开作家后台",
  });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
  if (!(await 是登录页(page))) {
    记录日志("检测到已登录状态");
    return;
  }

  if (options.headless) {
    throw 创建需要登录修复错误();
  }

  记录日志("检测到未登录状态，请直接在打开的浏览器里完成番茄作家登录，脚本会自动等待。");
  if (!(await 自动等待登录完成(page))) {
    throw new Error("等待番茄作家登录超时，终止上传");
  }
  await 带重试打开页面(page, startUrl, { waitUntil: "domcontentloaded" }, {
    description: "登录后重新打开作家后台",
  });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
  if (await 是登录页(page)) {
    throw new Error("仍处于未登录状态，终止上传");
  }
}

async function 选择作品并打开创建页(context, page, startUrl, bookTitle, loginOptions = {}) {
  void context;
  记录日志(`准备直达《${bookTitle}》创建章节页`);
  await 确保已登录(page, startUrl, loginOptions);
  const routes = await 获取作品路由(page, startUrl, bookTitle);
  if (!routes.publishUrl) {
    throw new Error(`未找到《${bookTitle}》的创建章节地址`);
  }

  await 带重试打开页面(page, routes.publishUrl, { waitUntil: "domcontentloaded" }, {
    description: `进入《${bookTitle}》创建章节页`,
  });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
  await 校验当前发布页作品(page, bookTitle);
  记录日志(`已进入创建章节页: ${page.url()}`);
  return page;
}

async function 填写编辑器(page, chapter) {
  记录日志(`开始填写第${chapter.number}章表单`);
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
    记录日志("正文已通过剪贴板粘贴写入");
  } else {
    await page.keyboard.insertText(chapter.body);
    记录日志(`剪贴板写入失败，改用逐字输入: ${clipboardResult?.message ?? "未知错误"}`);
  }

  try {
    await page.waitForTimeout(1000);
  } catch (error) {
    if (是页面关闭错误(error)) {
      throw new Error(`第${chapter.number}章正文写入后编辑页被关闭`);
    }
    throw error;
  }

  const readbackDeadline = Date.now() + 15000;
  let readback = {
    number: "",
    title: "",
    body: "",
  };
  while (Date.now() < readbackDeadline) {
    readback = {
      number: await numberInput.inputValue().catch(() => ""),
      title: await titleInput.inputValue().catch(() => ""),
      body: await editor.innerText().catch(() => ""),
    };
    if (
      String(readback.number).trim() === String(chapter.number) &&
      规范空白(readback.title) === 规范空白(chapter.title) &&
      紧凑文本(readback.body) === 紧凑文本(chapter.body)
    ) {
      break;
    }
    await page.waitForTimeout(500);
  }
  if (String(readback.number).trim() !== String(chapter.number)) {
    throw new Error(
      `第${chapter.number}章编辑器回读章号不一致：实际=${JSON.stringify(readback.number)}`,
    );
  }
  if (规范空白(readback.title) !== 规范空白(chapter.title)) {
    throw new Error(
      `第${chapter.number}章编辑器回读标题不一致：期望=${JSON.stringify(chapter.title)}，实际=${JSON.stringify(readback.title)}`,
    );
  }
  if (紧凑文本(readback.body) !== 紧凑文本(chapter.body)) {
    throw new Error(
      `第${chapter.number}章编辑器回读正文不一致：期望=${紧凑文本(chapter.body).length}字，实际=${紧凑文本(readback.body).length}字`,
    );
  }

  const editorValidationError = await 读取编辑页校验错误(page);
  if (editorValidationError) {
    throw new Error(`第${chapter.number}章编辑页校验失败: ${editorValidationError}`);
  }
  记录日志(`已填入第${chapter.number}章标题和正文`);
}

function 获取AI使用选择配置(aiUsage) {
  const normalized = 规范AI使用值(aiUsage);
  if (normalized === "yes") {
    return {
      targetPattern: /^(?:是|使用AI|有使用AI|已使用AI)$/u,
      locatorPattern: /使用AI|有使用AI|已使用AI|是/,
      description: "已选择 AI 使用为是",
    };
  }
  return {
    targetPattern: /^(?:否|没有使用AI|未使用AI|不使用AI)$/u,
    locatorPattern: /没有使用AI|未使用AI|不使用AI|否/,
    description: "已选择 AI 使用为否",
  };
}

function 写入上传前审计报告(args, report, suffix = "selected") {
  确保目录存在(args.artifactsDir);
  const outputPath = path.join(args.artifactsDir, `preflight_${suffix}.json`);
  const payload = {
    generatedAt: new Date().toISOString(),
    bookTitle: args.bookTitle,
    chaptersDir: args.chaptersDir,
    strictReaderFacing: !args.allowWeakTitle,
    selectedChapters: report.selectedRows.map((row) => ({
      number: row.fileNumber,
      title: row.title,
      heading: row.heading,
      bodyChars: row.bodyChars,
      bodySha256: row.bodySha256,
      issues: row.issues,
      filePath: row.filePath,
    })),
    missingSelected: report.missingSelected,
    errors: report.errors,
  };
  fs.writeFileSync(outputPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  return outputPath;
}

function 写入远端审计报告(args, remoteRows, errors) {
  确保目录存在(args.artifactsDir);
  const outputPath = path.join(args.artifactsDir, "preflight_remote.json");
  const payload = {
    generatedAt: new Date().toISOString(),
    bookTitle: args.bookTitle,
    strictReaderFacing: !args.allowWeakTitle,
    chapterCount: remoteRows.length,
    latestChapter: Math.max(0, ...remoteRows.map((row) => row.chapterNumber || 0)),
    chapters: remoteRows.map((row) => ({
      number: row.chapterNumber,
      title: row.chapterTitle,
      status: row.status,
      publishTime: row.publishTime,
    })),
    errors,
  };
  fs.writeFileSync(outputPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  return outputPath;
}

function 校验本地章节完整性(args, chapterNumbers, suffix = "selected") {
  const report = auditLocalProject({
    chaptersDir: args.chaptersDir,
    selectedNumbers: chapterNumbers,
    strictReaderFacing: !args.allowWeakTitle,
  });
  const reportPath = 写入上传前审计报告(args, report, suffix);
  const errors = [
    ...report.missingSelected.map((number) => `缺少第${number}章章节文件`),
    ...report.errors,
  ];
  if (errors.length) {
    throw new Error(
      `上传前本地完整性校验失败，已停止发布；报告=${reportPath}\n${errors.slice(0, 30).join("\n")}`,
    );
  }
  记录日志(
    `本地完整性校验通过：${chapterNumbers.length}章，标题、章号、正文、state 和 manifest 一致；报告=${reportPath}`,
  );
  return report;
}

const 发布流程提示正则 = /请选择内容检测方式|仅基础检测|全面检测|内容风险|风险检测|开启检测|定时发布|发布时间|确认发布|发布章节|立即发布|发布提示|是否确定提交|错别字未修改|我已阅读|我确认|跳过.*错别字|跳过.*检测|忽略.*错别字|继续发布|直接发布|放弃检测|提交|使用AI|没有使用AI|未使用AI|不使用AI|AI\s*使用|AI使用|有使用AI|已使用AI/;
const 发布流程浮层选择器 = [
  "[role='alert']",
  "[role='dialog']",
  ".arco-message",
  ".arco-modal",
  ".arco-modal-wrapper",
  "[class*='message']",
  "[class*='toast']",
  "[class*='notice']",
  "[class*='modal']",
  "[class*='dialog']",
  "[class*='popup']",
  "[class*='drawer']",
].join(",");

function 是否发布流程提示文本(pageText) {
  const text = 规范空白(pageText);
  return 发布流程提示正则.test(text);
}

async function 读取发布流程上下文(page) {
  return await page.evaluate(({ overlaySelectors, promptPattern }) => {
    const promptRe = new RegExp(promptPattern.source, promptPattern.flags);
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
    const parseZIndex = (value) => {
      const parsed = Number.parseInt(String(value ?? ""), 10);
      return Number.isFinite(parsed) ? parsed : 0;
    };

    const bodyText = normalize(document.body?.innerText || document.body?.textContent || "");
    let bestOverlay = null;
    const overlays = Array.from(document.querySelectorAll(overlaySelectors));
    for (let index = 0; index < overlays.length; index += 1) {
      const el = overlays[index];
      if (!(el instanceof Element) || !isVisible(el)) {
        continue;
      }
      if (el.closest(".warning-tip, .editor-tip, .warning-tip-content")) {
        continue;
      }

      const text = normalize(el.innerText || el.textContent || "");
      if (!text || !promptRe.test(text)) {
        continue;
      }

      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      const candidate = {
        text,
        zIndex: parseZIndex(style?.zIndex),
        area: Math.max(rect.width, 0) * Math.max(rect.height, 0),
        domIndex: index,
      };

      if (
        !bestOverlay ||
        candidate.zIndex > bestOverlay.zIndex ||
        (
          candidate.zIndex === bestOverlay.zIndex &&
          candidate.area < bestOverlay.area
        ) ||
        (
          candidate.zIndex === bestOverlay.zIndex &&
          candidate.area === bestOverlay.area &&
          candidate.domIndex > bestOverlay.domIndex
        )
      ) {
        bestOverlay = candidate;
      }
    }

    return {
      bodyText,
      promptOverlayText: bestOverlay?.text ?? "",
    };
  }, {
    overlaySelectors: 发布流程浮层选择器,
    promptPattern: 序列化正则(发布流程提示正则),
  }).catch(() => ({
    bodyText: "",
    promptOverlayText: "",
  }));
}

function 裁剪日志文本(value, maxLength = 160) {
  const text = 规范空白(String(value ?? ""));
  if (!text) {
    return "";
  }
  return 统计文本字符数(text) > maxLength
    ? `${Array.from(text).slice(0, maxLength).join("")}...`
    : text;
}

async function 读取编辑页校验错误(page) {
  return await page.evaluate((maxTitleLength) => {
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

    const messages = new Set();
    for (const el of document.querySelectorAll(
      ".serial-editor-tip, .serial-editor-tip span, .arco-form-message, .arco-form-item-message",
    )) {
      if (!(el instanceof Element) || !isVisible(el)) {
        continue;
      }
      const text = normalize(el.innerText || el.textContent || "");
      if (text) {
        messages.add(text);
      }
    }

    const titleInput = document.querySelector("input[placeholder='请输入标题']");
    if (titleInput instanceof HTMLInputElement) {
      const title = normalize(titleInput.value);
      const titleLength = Array.from(title).length;
      if (titleLength > maxTitleLength) {
        messages.add(`标题不能超过${maxTitleLength}个字（当前 ${titleLength}/${maxTitleLength}）`);
      }
    }

    return Array.from(messages).join("；");
  }, 番茄章节标题最大长度).catch(() => "");
}

async function 处理错别字和AI弹窗(page, chapter, { draftOnly, debug, aiUsage }) {
  记录日志("开始处理发布确认弹窗");
  const aiUsageOption = 获取AI使用选择配置(aiUsage);
  const 发布设置动作历史 = new Map();
  const 已执行发布设置动作 = (contextSignature, actionKey) =>
    发布设置动作历史.get(contextSignature)?.has(actionKey) ?? false;
  const 标记发布设置动作已执行 = (contextSignature, actionKey) => {
    const next = 发布设置动作历史.get(contextSignature) ?? new Set();
    next.add(actionKey);
    发布设置动作历史.set(contextSignature, next);
  };
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
    记录日志(`发布页调试快照: ${JSON.stringify(debugSnapshot, null, 2)}`);
  }

  const deadline = Date.now() + 90000;
  let publishClicked = false;
  let lastContextSignature = "";

  while (Date.now() < deadline) {
    if (page.isClosed()) {
      if (publishClicked) {
        return;
      }
      throw new Error("发布确认页在完成前被关闭");
    }

    let progressed = false;
    const editorValidationError = await 读取编辑页校验错误(page);
    if (editorValidationError) {
      throw new Error(`编辑页校验失败，未进入发布确认步骤: ${editorValidationError}`);
    }

    const publishContext = await 读取发布流程上下文(page);
    const pageText = publishContext.promptOverlayText || publishContext.bodyText;
    const hasPublishSettingsDialog = /发布设置|是否使用AI|定时发布|确认发布/.test(
      publishContext.promptOverlayText || "",
    );
    const contextSignature = `${publishClicked ? "1" : "0"}::${pageText}`;
    if (contextSignature !== lastContextSignature) {
      lastContextSignature = contextSignature;
      记录日志(
        `发布流程上下文: overlay=${publishContext.promptOverlayText ? `"${裁剪日志文本(publishContext.promptOverlayText)}"` : "<none>"} body=${publishContext.bodyText ? `"${裁剪日志文本(publishContext.bodyText)}"` : "<empty>"}`,
      );
    }
    if (/已到达当日发布字数上限，无法继续发布|已到达当日发布字数上限/.test(pageText)) {
      throw new Error("平台限制：已到达当日发布字数上限，无法继续发布");
    }
    if (/提交字数超出每日上限|每日上限/.test(pageText)) {
      const error = new Error("平台限制：提交字数超出每日上限");
      error.exitCode = 30;
      throw error;
    }
    if (/本书中存在重复标题|重复标题，请修改后再发布/.test(pageText)) {
      throw new Error("发布设置校验失败：本书中存在重复标题，请修改后再发布");
    }
    const hasPublishPrompt = 是否发布流程提示文本(pageText);
    if (!hasPublishPrompt) {
      if (!(await 动作后等待(page, 1000, publishClicked))) {
        return;
      }
      continue;
    }
    const hasDetectionModeDialog = /请选择内容检测方式|仅基础检测|全面检测/.test(pageText);
    if (
      hasDetectionModeDialog &&
      (
        await 点击首个可见元素(
          page,
          [
            (p) => p.getByRole("button", { name: /^仅基础检测$/u }),
            (p) => p.locator("button").filter({ hasText: /^仅基础检测$/u }),
            (p) => p.locator("[role='button']").filter({ hasText: /^仅基础检测$/u }),
            (p) => p.getByRole("button", { name: /仅基础检测|基础检测/ }),
            (p) => p.locator("button").filter({ hasText: /仅基础检测|基础检测/ }),
            (p) => p.locator("[role='button']").filter({ hasText: /仅基础检测|基础检测/ }),
          ],
          "已选择仅基础检测",
        ) ||
        await 点击作用域内可见文本(page, {
          selectors: "button, [role='button'], label, [role='radio'], span",
          scopePattern: /请选择内容检测方式|基础检测|全面检测/,
          targetPattern: /^仅基础检测$/u,
          description: "已选择仅基础检测",
          skipIfSelected: true,
          rootSelectors: 发布流程浮层选择器,
          rootPattern: 发布流程提示正则,
        })
      )
    ) {
      progressed = true;
      if (!(await 动作后等待(page, 1200, publishClicked))) {
        return;
      }
      continue;
    }

    if (
      /发布提示|是否确定提交|错别字未修改/.test(pageText) &&
      (
        await 点击作用域内可见文本(page, {
          selectors: "button, [role='button'], span",
          scopePattern: /发布提示|是否确定提交|错别字未修改/,
          targetPattern: /^提交$/u,
          description: "已确认错别字提示并提交",
          skipIfSelected: true,
          rootSelectors: 发布流程浮层选择器,
          rootPattern: 发布流程提示正则,
        }) ||
        await 点击首个可见元素(
          page,
          [
            (p) => p.getByRole("button", { name: /^提交$/u }),
            (p) => p.locator("button").filter({ hasText: /^提交$/u }),
            (p) => p.locator("[role='button']").filter({ hasText: /^提交$/u }),
          ],
          "已确认错别字提示并提交",
        )
      )
    ) {
      progressed = true;
      publishClicked = true;
      if (!(await 动作后等待(page, 1500, publishClicked))) {
        return;
      }
      continue;
    }

    const hasRiskDialog = !hasDetectionModeDialog && /内容风险|风险检测|开启检测/.test(pageText);

    if (
      hasRiskDialog &&
      await 点击首个可见元素(
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
      if (!(await 动作后等待(page, 1000, publishClicked))) {
        return;
      }
      continue;
    }

    const aiUsageActionKey = `ai_usage:${aiUsageOption.description}`;
    const 可尝试AI选择 = !已执行发布设置动作(contextSignature, aiUsageActionKey);
    const 已处理AI选择 =
      可尝试AI选择 &&
      hasPublishSettingsDialog &&
      /使用AI|没有使用AI|未使用AI|不使用AI|AI\s*使用|AI使用|有使用AI|已使用AI/.test(pageText) &&
      (
        await 点击作用域内可见文本(page, {
          selectors: "label, [role='radio'], button, [role='button'], span",
          scopePattern: /使用AI|没有使用AI|未使用AI|不使用AI|AI\s*使用|AI使用|有使用AI|已使用AI/,
          targetPattern: aiUsageOption.targetPattern,
          description: aiUsageOption.description,
          skipIfSelected: true,
          rootSelectors: 发布流程浮层选择器,
          rootPattern: 发布流程提示正则,
        }) ||
        await 点击首个可见元素(
          page,
          [
            (p) => p.getByRole("radio", { name: aiUsageOption.locatorPattern }),
            (p) => p.getByLabel(aiUsageOption.locatorPattern),
            (p) => p.locator("label").filter({ hasText: aiUsageOption.locatorPattern }),
            (p) => p.locator("[role='radio']").filter({ hasText: aiUsageOption.locatorPattern }),
          ],
          aiUsageOption.description,
        )
      );
    if (已处理AI选择) {
      标记发布设置动作已执行(contextSignature, aiUsageActionKey);
      progressed = true;
      if (!(await 动作后等待(page, 800, publishClicked))) {
        return;
      }
      continue;
    }

    const publishModeActionKey = "publish_mode:immediate";
    const 可尝试发布方式选择 = !已执行发布设置动作(contextSignature, publishModeActionKey);
    const 已处理发布方式 =
      可尝试发布方式选择 &&
      hasPublishSettingsDialog &&
      /定时发布/.test(pageText) &&
      (
        await 点击作用域内可见文本(page, {
          selectors: "label, [role='radio'], button, [role='button'], span",
          scopePattern: /定时发布|发布时间/,
          targetPattern: /^(?:立即发布|不定时发布|否)$/u,
          description: "已选择非定时发布",
          skipIfSelected: true,
          rootSelectors: 发布流程浮层选择器,
          rootPattern: 发布流程提示正则,
        }) ||
        await 点击首个可见元素(
          page,
          [
            (p) => p.getByRole("radio", { name: /立即发布|不定时发布/ }),
            (p) => p.getByLabel(/立即发布|不定时发布/),
            (p) => p.locator("label").filter({ hasText: /立即发布|不定时发布/ }),
            (p) => p.locator("[role='radio']").filter({ hasText: /立即发布|不定时发布/ }),
          ],
          "已选择非定时发布",
        )
      );
    if (已处理发布方式) {
      标记发布设置动作已执行(contextSignature, publishModeActionKey);
      progressed = true;
      if (!(await 动作后等待(page, 800, publishClicked))) {
        return;
      }
      continue;
    }

    if (
      await 点击首个可见元素(
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
      if (!(await 动作后等待(page, 1000, publishClicked))) {
        return;
      }
      continue;
    }

    if (
      hasPublishSettingsDialog &&
      !已执行发布设置动作(contextSignature, aiUsageActionKey) &&
      await 点击首个可见元素(
        page,
        [
          (p) => p.getByLabel(aiUsageOption.locatorPattern),
          (p) => p.getByRole("radio", { name: aiUsageOption.locatorPattern }),
          (p) => p.locator("label").filter({ hasText: aiUsageOption.locatorPattern }),
          (p) => p.locator("[role='radio']").filter({ hasText: aiUsageOption.locatorPattern }),
          (p) => p.locator("span").filter({ hasText: aiUsageOption.locatorPattern }),
        ],
        aiUsageOption.description,
      )
    ) {
      标记发布设置动作已执行(contextSignature, aiUsageActionKey);
      progressed = true;
      if (!(await 动作后等待(page, 800, publishClicked))) {
        return;
      }
      continue;
    }

    if (
      await 点击首个可见元素(
        page,
        [
          (p) => p.locator("label").filter({ hasText: /我已阅读|我确认/ }),
          (p) => p.locator("span").filter({ hasText: /我已阅读|我确认/ }),
        ],
        "已勾选发布确认项",
      )
    ) {
      progressed = true;
      if (!(await 动作后等待(page, 500, publishClicked))) {
        return;
      }
      continue;
    }

    if (!draftOnly) {
      if (
        (
          await 点击作用域内可见文本(page, {
            selectors: "button, [role='button']",
            scopePattern: /定时发布|发布时间|确认发布|发布章节|立即发布|我已阅读|我确认|继续发布|直接发布|放弃检测|跳过.*错别字|跳过.*检测|忽略.*错别字/,
            targetPattern: /^(?:确认发布|发布章节|立即发布|发布)$/u,
            description: publishClicked ? "已继续确认发布" : "已点击发布",
            rootSelectors: 发布流程浮层选择器,
            rootPattern: 发布流程提示正则,
          }) ||
          await 点击首个可见元素(
            page,
            [
              (p) => p.getByRole("button", { name: /确认发布|发布章节|立即发布|发布/ }),
              (p) => p.locator("button").filter({ hasText: /确认发布|发布章节|立即发布|发布/ }),
              (p) => p.locator("[role='button']").filter({ hasText: /确认发布|发布章节|立即发布|发布/ }),
            ],
            publishClicked ? "已继续确认发布" : "已点击发布",
          )
        )
      ) {
        progressed = true;
        publishClicked = true;
        if (!(await 动作后等待(page, 1500, publishClicked))) {
          return;
        }
        continue;
      }
    }

    const successHint = publishClicked
      ? await 检测明确发布完成(page, chapter)
      : "";
    if (successHint) {
      记录日志(`检测到发布完成提示: ${successHint}`);
      return successHint;
    }

    if (!progressed) {
      if (!(await 动作后等待(page, 1200, publishClicked))) {
        return;
      }
    }
  }

  if (!draftOnly) {
    throw new Error("发布流程超时，未能自动完成后续弹窗");
  }
}

async function 在工作台校验已发布(page, startUrl, bookTitle, chapter, loginOptions = {}) {
  记录日志(`转到章节管理校验第${chapter.number}章是否已显示`);
  await 确保已登录(page, startUrl, loginOptions);
  const routes = await 获取作品路由(page, startUrl, bookTitle);
  if (!routes.chapterManageUrl) {
    throw new Error(`未找到《${bookTitle}》的章节管理地址`);
  }
  const expected = 规范空白(`第${chapter.number}章 ${chapter.title}`);
  await 带重试打开页面(page, routes.chapterManageUrl, { waitUntil: "domcontentloaded" }, {
    description: "转到章节管理校验已发布章节",
  });
  记录日志("已进入章节管理");
  await page.waitForLoadState("domcontentloaded");
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
  const targetRow = await 等待章节管理目标明细(page, chapter, 30000);
  if (targetRow) {
    记录日志(`章节管理已显示目标章节: ${expected}，状态=${targetRow.status || "未知"}`);
    try {
      await 校验官网预览正文(page, chapter, targetRow.previewUrl);
    } catch (error) {
      if (/审核中/u.test(targetRow.status || "")) {
        记录日志(
          `第${chapter.number}章仍在审核中，官网预览暂不可完整复核；已通过提交前编辑器逐字回读、发布成功信号和章节管理标题三重校验，等待审核后由后续审计复核。原因=${String(error?.message ?? error).split("\n")[0]}`,
        );
      } else {
        throw error;
      }
    }
    return targetRow;
  }
  throw new Error(`章节管理中未找到目标章节: ${expected}`);
}

async function 上传单章(session, args, chapter) {
  记录日志(`为第${chapter.number}章打开新的工作页`);
  const dashboardPage = await session.context.newPage();
  绑定页面诊断(dashboardPage, `chapter_${chapter.number}_dashboard`);

  let activePage = dashboardPage;
  try {
    await 确保已登录(dashboardPage, args.startUrl, args);
    const editorPage = await 选择作品并打开创建页(
      session.context,
      dashboardPage,
      args.startUrl,
      args.bookTitle,
      args,
    );
    activePage = editorPage;
    绑定页面诊断(editorPage, `chapter_${chapter.number}_editor`);

    const shouldCloseEditor = editorPage !== dashboardPage;
    try {
      await 校验上一章匹配(
        editorPage,
        chapter,
        args.chaptersDir,
        args.verifiedRemoteTitlesByNumber,
      );
      await 填写编辑器(editorPage, chapter);
      if (!(await 点击首个可见元素(
        editorPage,
        [
          (p) => p.getByRole("button", { name: "下一步" }),
          (p) => p.locator("button").filter({ hasText: /^下一步$/ }),
        ],
        "已点击下一步，等待发布确认步骤",
      ))) {
        throw new Error("未找到“下一步”按钮");
      }
      const publishSuccessHint = await 处理错别字和AI弹窗(editorPage, chapter, args);
      if (!args.draftOnly) {
        try {
          await 在工作台校验已发布(dashboardPage, args.startUrl, args.bookTitle, chapter, args);
        } catch (error) {
          if (publishSuccessHint && 是可重试页面跳转错误(error)) {
            记录日志(`发布后复核遇到可重试网络错误，但已取得明确发布成功信号，按成功继续: ${String(error?.message ?? error).split("\n")[0]}`);
          } else {
            throw error;
          }
        }
        args.verifiedRemoteTitlesByNumber ??= new Map();
        args.verifiedRemoteTitlesByNumber.set(chapter.number, chapter.title);
      }
    } catch (error) {
      if (!error?.artifactsSaved) {
        const artifactPage = !editorPage.isClosed() ? editorPage : dashboardPage;
        await 保存失败产物(
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
      await 保存失败产物(
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

async function 创建会话(args) {
  if (args.cdpUrl) {
    记录日志(`通过 CDP 连接浏览器: ${args.cdpUrl}`);
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

  const chromePath = args.chromePath || 检测Chrome可执行文件();
  if (!chromePath) {
    throw new Error("未找到 Chrome/Chromium，可用 --chrome-path 显式指定");
  }

  if (args.seedProfileDir) {
    记录日志(`从种子登录配置目录复制工作副本: ${args.seedProfileDir} -> ${args.profileDir}`);
    复制Profile种子(args.seedProfileDir, args.profileDir);
  }
  确保目录存在(args.profileDir);
  记录日志(`启动独立浏览器: ${chromePath}`);
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

async function 主程序() {
  const args = 解析参数(process.argv.slice(2));
  if (args.help) {
    打印帮助();
    return;
  }

  校验参数(args);
  确保目录存在(args.artifactsDir);
  当前日志文件 = path.join(args.artifactsDir, "run.log");
  fs.writeFileSync(当前日志文件, "", "utf8");

  const session = await 创建会话(args);
  try {
    const jobs = args.autoNextCount > 0
      ? await 解析自动续传任务(session, args)
      : await 加载章节任务(args.chaptersDir, args.from, args.to);
    校验本地章节完整性(
      args,
      jobs.map((chapter) => chapter.number),
      "selected",
    );
    await 校验待上传章节标题不重复(session, args, jobs);

    if (!jobs.length) {
      记录日志("本次无需上传，任务结束");
      return;
    }

    if (args.preflightOnly) {
      记录日志(
        `只读预检通过：将上传第${jobs[0].number}章至第${jobs[jobs.length - 1].number}章；未打开编辑器，未创建草稿，未提交发布`,
      );
      return;
    }

    记录日志(`准备上传 ${jobs.length} 章: ${jobs.map((item) => item.number).join(", ")}`);
    for (const chapter of jobs) {
      记录日志(`开始上传第${chapter.number}章 ${chapter.title}`);
      await 上传单章(session, args, chapter);
      记录日志(`第${chapter.number}章处理完成`);
    }
  } finally {
    await session.close();
  }
}

主程序().catch((error) => {
  console.error(error?.stack || String(error));
  process.exitCode = Number(error?.exitCode) || 1;
});


import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import readline from "node:readline/promises";

import { chromium } from "playwright-core";

const 默认起始网址 = "https://fanqienovel.com/main/writer/?enter_from=author_zone";
const 默认_OPENCLAW_CDP_网址 = "http://127.0.0.1:18800";
let 当前日志文件 = "";

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

function 解析参数(argv) {
  const args = {
    startUrl: 默认起始网址,
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
    aiUsage: "no",
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

function 记录日志(message) {
  const stamp = new Date().toISOString().replace("T", " ").slice(0, 19);
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
  const titleLine = lines[0]?.trim() ?? "";
  const match = titleLine.match(/^第\s*(\d+)\s*章(?:\s+|[：:，,、.．。]\s*)(.+)$/);
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
    const locator = createLocator().first();
    try {
      if ((await locator.count()) > 0 && (await locator.isVisible())) {
        return locator;
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
  await locator.click();
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
  { selectors, targetPattern, scopePattern, description, skipIfSelected = false },
) {
  const result = await page.evaluate(
    ({ selectors: rawSelectors, target, scope, rawSkipIfSelected }) => {
      const targetRe = new RegExp(target.source, target.flags);
      const scopeRe = scope ? new RegExp(scope.source, scope.flags) : null;

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
              node.getAttribute("data-state") === "checked"
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

      let best = null;
      const candidates = Array.from(document.querySelectorAll(rawSelectors));
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
    },
  ).catch(() => ({ status: "not_found" }));

  if (result.status !== "clicked") {
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
  const locator = page.getByRole("button", { name: /立即登录|登录/ }).first();
  try {
    return (await locator.count()) > 0 && (await locator.isVisible());
  } catch {
    return false;
  }
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

async function 校验上一章匹配(page, chapter, chaptersDir) {
  if (chapter.number <= 1) {
    记录日志("当前是第1章，跳过上一章连续性校验");
    return;
  }

  const previousLocal = await 按章节号加载章节(chaptersDir, chapter.number - 1);
  const previousRemote = await 从编辑器获取上次提交章节(page);
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

  if (规范空白(previousRemote.title) !== 规范空白(previousLocal.title)) {
    throw new Error(
      `发布页“上次提交”标题不匹配：远端为“${previousRemote.title}”，本地上一章为“${previousLocal.title}”，已拒绝上传`,
    );
  }

  记录日志(`上一章连续性校验通过：远端第${previousRemote.number}章 ${previousRemote.title}`);
}

async function 获取最新已发布章节(page, startUrl, bookTitle) {
  记录日志(`读取《${bookTitle}》远端最新已发布章节`);
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  await 等待工作台就绪(page, bookTitle);
  await 在工作台选择作品(page, bookTitle);

  const homeText = await page.locator("body").innerText().catch(() => "");
  const recent = 解析最近更新章节(homeText);
  if (recent) {
    记录日志(`首页最近更新显示为第${recent.number}章`);
  }

  if (recent) {
    记录日志("继续转到章节管理复核最新章节（含审核中章节）");
  } else {
    记录日志("首页未解析到最近更新，转到章节管理读取最新章节");
  }
  await 点击首个可见元素(
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
  const numbers = await 从章节管理页读取章节号列表(page, 15000);
  if (!numbers.length) {
    if (recent) {
      return recent.number;
    }
    记录日志("远端未解析到任何已发布章节，按从第1章开始处理");
    return 0;
  }
  const latest = Math.max(recent?.number ?? 0, ...numbers);
  if (recent && latest > recent.number) {
    记录日志(`章节管理显示最新章节为第${latest}章（首页最近更新仍为第${recent.number}章）`);
  } else {
    记录日志(`章节管理显示最新章节为第${latest}章`);
  }
  return latest;
}

async function 解析自动续传任务(session, args) {
  const page = await session.context.newPage();
  try {
    await 确保已登录(page, args.startUrl);
    const latestPublished = await 获取最新已发布章节(page, args.startUrl, args.bookTitle);
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

async function 确保已登录(page, startUrl) {
  记录日志(`打开作家后台: ${startUrl}`);
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  if (!(await 是登录页(page))) {
    记录日志("检测到已登录状态");
    return;
  }

  记录日志("检测到未登录状态，请在打开的浏览器里完成番茄作家登录。");
  await 等待回车("登录完成后回到终端，按 Enter 继续...");
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  if (await 是登录页(page)) {
    throw new Error("仍处于未登录状态，终止上传");
  }
}

async function 选择作品并打开创建页(context, page, startUrl, bookTitle) {
  记录日志(`进入作品首页，准备定位《${bookTitle}》`);
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  await 等待工作台就绪(page, bookTitle);
  await 在工作台选择作品(page, bookTitle);

  const popupPromise = context.waitForEvent("page", { timeout: 5000 }).catch(() => null);
  const clicked = await 点击首个可见元素(
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
  记录日志(`已进入创建章节页: ${targetPage.url()}`);
  return targetPage;
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

  await page.waitForTimeout(1000);
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

async function 处理错别字和AI弹窗(page, chapter, { draftOnly, debug, aiUsage }) {
  记录日志("开始处理发布确认弹窗");
  const aiUsageOption = 获取AI使用选择配置(aiUsage);
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

  while (Date.now() < deadline) {
    if (page.isClosed()) {
      if (publishClicked) {
        return;
      }
      throw new Error("发布确认页在完成前被关闭");
    }

    let progressed = false;

    const pageText = await page.locator("body").innerText().catch(() => "");
    if (/已到达当日发布字数上限，无法继续发布|已到达当日发布字数上限/.test(pageText)) {
      throw new Error("平台限制：已到达当日发布字数上限，无法继续发布");
    }
    const hasRiskDialog = /内容风险|风险检测|开启检测/.test(pageText);

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
    }

    if (
      /AI|人工智能|智能生成/.test(pageText) &&
      (
        await 点击作用域内可见文本(page, {
          selectors: "label, [role='radio'], button, [role='button'], span",
          scopePattern: /AI|人工智能|智能生成/,
          targetPattern: aiUsageOption.targetPattern,
          description: aiUsageOption.description,
          skipIfSelected: true,
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
      )
    ) {
      progressed = true;
      if (!(await 动作后等待(page, 800, publishClicked))) {
        return;
      }
    }

    if (
      /定时发布/.test(pageText) &&
      (
        await 点击作用域内可见文本(page, {
          selectors: "label, [role='radio'], button, [role='button'], span",
          scopePattern: /定时发布|发布时间/,
          targetPattern: /^(?:立即发布|不定时发布|否)$/u,
          description: "已选择非定时发布",
          skipIfSelected: true,
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
      )
    ) {
      progressed = true;
      if (!(await 动作后等待(page, 800, publishClicked))) {
        return;
      }
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
    }

    if (
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
      progressed = true;
      if (!(await 动作后等待(page, 800, publishClicked))) {
        return;
      }
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
    }

    if (!draftOnly) {
      if (
        (
          await 点击作用域内可见文本(page, {
            selectors: "button, [role='button']",
            scopePattern: /AI|人工智能|定时发布|确认发布|声明|我已阅读|我确认|发布/,
            targetPattern: /^(?:确认发布|发布章节|立即发布|发布)$/u,
            description: publishClicked ? "已继续确认发布" : "已点击发布",
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
      }
    }

    const successHint = publishClicked
      ? await 检测明确发布完成(page, chapter)
      : "";
    if (successHint) {
      记录日志(`检测到发布完成提示: ${successHint}`);
      return;
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

async function 在工作台校验已发布(page, startUrl, bookTitle, chapter) {
  记录日志(`回到首页校验第${chapter.number}章是否已显示`);
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  await 等待工作台就绪(page, bookTitle);
  await 在工作台选择作品(page, bookTitle);
  const expected = 规范空白(`第${chapter.number}章 ${chapter.title}`);
  const expectedRecent = 规范空白(`最近更新：${expected}`);
  const bodyText = 规范空白(await page.locator("body").innerText().catch(() => ""));
  if (bodyText.includes(expectedRecent) || bodyText.includes(expected)) {
    记录日志(`后台首页已显示目标章节: ${expected}`);
    return;
  }

  记录日志("首页未立即显示目标章节，转到章节管理继续校验");
  await 点击首个可见元素(
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
  if (await 章节管理已显示目标章节(page, expected, 20000)) {
    记录日志(`章节管理已显示目标章节: ${expected}`);
    return;
  }
  throw new Error(`章节管理中未找到目标章节: ${expected}`);
}

async function 上传单章(session, args, chapter) {
  记录日志(`为第${chapter.number}章打开新的工作页`);
  const dashboardPage = await session.context.newPage();
  dashboardPage.on("dialog", async (dialog) => {
    记录日志(`检测到浏览器对话框: ${dialog.message()}`);
    await dialog.accept().catch(() => {});
  });

  let activePage = dashboardPage;
  try {
    await 确保已登录(dashboardPage, args.startUrl);
    const editorPage = await 选择作品并打开创建页(
      session.context,
      dashboardPage,
      args.startUrl,
      args.bookTitle,
    );
    activePage = editorPage;

    const shouldCloseEditor = editorPage !== dashboardPage;
    try {
      await 校验上一章匹配(editorPage, chapter, args.chaptersDir);
      await 填写编辑器(editorPage, chapter);
      await 点击首个可见元素(
        editorPage,
        [
          (p) => p.getByRole("button", { name: "下一步" }),
          (p) => p.locator("button").filter({ hasText: /^下一步$/ }),
        ],
        "已进入发布确认步骤",
      );
      await 处理错别字和AI弹窗(editorPage, chapter, args);
      if (!args.draftOnly) {
        await 在工作台校验已发布(dashboardPage, args.startUrl, args.bookTitle, chapter);
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

    if (!jobs.length) {
      记录日志("本次无需上传，任务结束");
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
  process.exitCode = 1;
});


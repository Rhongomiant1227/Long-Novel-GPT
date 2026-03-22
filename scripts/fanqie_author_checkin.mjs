import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import readline from "node:readline/promises";

import { chromium } from "playwright-core";

const DEFAULT_START_URL = "https://fanqienovel.com/main/writer/?enter_from=author_zone";
const DEFAULT_ARTIFACTS_DIR = path.resolve(".run", "fanqie_author_checkin_artifacts");
const DEFAULT_PROFILE_DIR = path.resolve(".run", "fanqie_author_checkin_profile");
const DEFAULT_SETTLEMENT_TIME = "02:40";

function printHelp() {
  console.log(`番茄作者签到修复脚本

用法:
  node scripts/fanqie_author_checkin.mjs --config fanqie_daily_jobs.json
  node scripts/fanqie_author_checkin.mjs --date 2026-03-16 --seed-profile-dir %USERPROFILE%/.openclaw/browser/openclaw/user-data

常用参数:
  --config <path>          从 fanqie_daily_jobs.json 读取 authorCheckIn/defaults 配置
  --date <YYYY-MM-DD>      指定要检查或补签的日期
  --repair-yesterday       自动处理昨天；未传 --date 时默认启用
  --ticket-id <id>         指定补签卡 id，不指定则自动选择首张可用卡
  --check-only             只检查状态，不调用补签接口
  --dry-run                打印将要执行的动作，不真正补签
  --profile-dir <dir>      持久化 Chrome profile 目录
  --seed-profile-dir <dir> 从已登录 profile 复制工作副本后再启动
  --chrome-path <path>     Chrome 可执行文件路径
  --start-url <url>        作者后台首页
  --artifacts-dir <dir>    结果日志目录
  --headless               无头模式启动独立浏览器
  --headed                 有头模式启动独立浏览器
  --keep-open              结束后不关闭独立浏览器
  --help                   显示帮助

说明:
  1. 默认检查“昨天”的作者签到修复状态
  2. 结果会同时输出作者签到月历、作品打卡月历、作品活动归属三套状态
  3. 如果平台返回“已补签”但回查状态没变化，脚本会明确报为“平台补签状态不一致”
  4. 作者签到月历通常在次日凌晨结算；当天白天看到 0 不代表当天没更字
`);
}

function parseArgs(argv) {
  const args = {
    configPath: "",
    date: "",
    repairYesterday: false,
    ticketId: "",
    checkOnly: false,
    dryRun: false,
    startUrl: "",
    profileDir: "",
    seedProfileDir: "",
    chromePath: "",
    artifactsDir: DEFAULT_ARTIFACTS_DIR,
    headless: null,
    keepOpen: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const next = () => {
      if (index + 1 >= argv.length) {
        throw new Error(`缺少参数值: ${arg}`);
      }
      index += 1;
      return argv[index];
    };

    switch (arg) {
      case "--config":
        args.configPath = path.resolve(next());
        break;
      case "--date":
        args.date = next();
        break;
      case "--repair-yesterday":
        args.repairYesterday = true;
        break;
      case "--ticket-id":
        args.ticketId = next();
        break;
      case "--check-only":
        args.checkOnly = true;
        break;
      case "--dry-run":
        args.dryRun = true;
        break;
      case "--start-url":
        args.startUrl = next();
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
      case "--artifacts-dir":
        args.artifactsDir = path.resolve(next());
        break;
      case "--headless":
        args.headless = true;
        break;
      case "--headed":
        args.headless = false;
        break;
      case "--keep-open":
        args.keepOpen = true;
        break;
      case "--help":
      case "-h":
        args.help = true;
        break;
      default:
        throw new Error(`未知参数: ${arg}`);
    }
  }

  return args;
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isRetryableFsError(error) {
  return ["EBUSY", "ENOTEMPTY", "EPERM"].includes(error?.code ?? "");
}

async function retryFsOperation(operation, { attempts = 5, delayMs = 400 } = {}) {
  let lastError = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return operation();
    } catch (error) {
      lastError = error;
      if (attempt >= attempts || !isRetryableFsError(error)) {
        throw error;
      }
      await delay(delayMs);
    }
  }
  throw lastError;
}

async function resetDir(dir) {
  await retryFsOperation(() => {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.mkdirSync(dir, { recursive: true });
  });
}

function normalizeDate(value, sourceLabel = "date") {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value ?? ""))) {
    throw new Error(`${sourceLabel} 必须是 YYYY-MM-DD`);
  }
  return String(value);
}

function expandPathVariables(value) {
  if (!value) {
    return value;
  }

  let expanded = value.replace(/%([^%]+)%/g, (_, name) => process.env[name] ?? `%${name}%`);
  if (expanded.startsWith("~")) {
    const home = process.env.USERPROFILE || process.env.HOME;
    if (home) {
      expanded = path.join(home, expanded.slice(1));
    }
  }
  return expanded;
}

function resolvePathLike(configDir, value) {
  if (!value) {
    return "";
  }

  const expanded = expandPathVariables(value);
  return path.isAbsolute(expanded) ? expanded : path.resolve(configDir, expanded);
}

function formatLocalDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function getYesterdayDate() {
  const date = new Date();
  date.setDate(date.getDate() - 1);
  return formatLocalDate(date);
}

function addDays(dateKey, days) {
  const [yearText, monthText, dayText] = normalizeDate(dateKey).split("-");
  const date = new Date(Number(yearText), Number(monthText) - 1, Number(dayText));
  date.setDate(date.getDate() + days);
  return date;
}

function getMonthQueryRange(dateKey) {
  const [yearText, monthText] = normalizeDate(dateKey).split("-");
  const year = Number(yearText);
  const month = Number(monthText);
  const start = new Date(year, month - 1, 1);
  const end = new Date(year, month, 1);
  return {
    startDate: formatLocalDate(start),
    endDate: formatLocalDate(end),
  };
}

function getMonthUnixRange(dateKey) {
  const [yearText, monthText] = normalizeDate(dateKey).split("-");
  const year = Number(yearText);
  const month = Number(monthText);
  const start = new Date(year, month - 1, 1);
  const end = new Date(year, month, 1);
  return {
    startDate: formatLocalDate(start),
    endDate: formatLocalDate(end),
    startEpoch: Math.floor(start.getTime() / 1000),
    endEpoch: Math.floor(end.getTime() / 1000),
  };
}

function resolveCheckInConfig(configDir, defaults, rawCheckIn) {
  const profileRoot = resolvePathLike(configDir, defaults.profileRoot ?? ".run/fanqie_daily_profiles");
  const artifactsRoot = resolvePathLike(configDir, defaults.artifactsRoot ?? ".run/fanqie_daily_artifacts");
  return {
    enabled: rawCheckIn?.enabled !== false,
    taskName: String(rawCheckIn?.taskName ?? "LongNovelFanqieAuthorCheckIn"),
    startTime: String(rawCheckIn?.startTime ?? "02:50"),
    days: Array.isArray(rawCheckIn?.days) && rawCheckIn.days.length
      ? rawCheckIn.days.map((day) => String(day).toUpperCase())
      : null,
    startUrl: rawCheckIn?.startUrl ?? defaults.startUrl ?? DEFAULT_START_URL,
    headless: rawCheckIn?.headless ?? defaults.headless ?? true,
    seedProfileDir: resolvePathLike(configDir, rawCheckIn?.seedProfileDir ?? defaults.seedProfileDir ?? ""),
    chromePath: resolvePathLike(configDir, rawCheckIn?.chromePath ?? defaults.chromePath ?? ""),
    profileDir: resolvePathLike(
      configDir,
      rawCheckIn?.profileDir ?? path.join(profileRoot || ".run/fanqie_daily_profiles", "author_checkin"),
    ),
    artifactsDir: resolvePathLike(
      configDir,
      rawCheckIn?.artifactsDir ?? path.join(artifactsRoot || ".run/fanqie_daily_artifacts", "author_checkin"),
    ),
  };
}

async function loadRuntimeConfig(rawArgs) {
  const args = { ...rawArgs };

  if (!rawArgs.configPath) {
    args.startUrl = args.startUrl || DEFAULT_START_URL;
    args.profileDir = args.profileDir || DEFAULT_PROFILE_DIR;
    args.artifactsDir = args.artifactsDir || DEFAULT_ARTIFACTS_DIR;
    args.headless = args.headless ?? true;
    return args;
  }

  const rawConfig = JSON.parse(await fsp.readFile(rawArgs.configPath, "utf8"));
  const configDir = path.dirname(rawArgs.configPath);
  const defaults = rawConfig.defaults ?? {};
  const checkInConfig = resolveCheckInConfig(configDir, defaults, rawConfig.authorCheckIn ?? {});

  args.authorCheckInConfig = checkInConfig;
  args.startUrl = args.startUrl || checkInConfig.startUrl || DEFAULT_START_URL;
  args.profileDir = args.profileDir || checkInConfig.profileDir || DEFAULT_PROFILE_DIR;
  args.seedProfileDir = args.seedProfileDir || checkInConfig.seedProfileDir || "";
  args.chromePath = args.chromePath || checkInConfig.chromePath || "";
  args.artifactsDir = args.artifactsDir || checkInConfig.artifactsDir || DEFAULT_ARTIFACTS_DIR;
  args.headless = args.headless ?? checkInConfig.headless ?? true;
  args.taskDays = checkInConfig.days ?? rawConfig.taskSchedule?.days ?? [];
  return args;
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

async function cloneProfileSeed(seedDir, targetDir) {
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

  await resetDir(targetDir);
  await retryFsOperation(() => {
    fs.cpSync(seedDir, targetDir, {
      recursive: true,
      force: true,
      filter: (sourcePath) => {
        const base = path.basename(sourcePath);
        if (skipNames.has(base)) {
          return false;
        }
        return !skipFragments.some((fragment) => sourcePath.includes(fragment));
      },
    });
  });
}

function createLogger(logFile) {
  return (message) => {
    const stamp = new Date().toISOString().replace("T", " ").slice(0, 19);
    const line = `${stamp} | ${message}`;
    console.log(line);
    fs.appendFileSync(logFile, `${line}\n`, "utf8");
  };
}

async function waitForEnter(message) {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  try {
    await rl.question(message);
  } finally {
    rl.close();
  }
}

async function isLoginPage(page) {
  const locator = page.getByRole("button", { name: /立即登录|登录/ }).first();
  try {
    return (await locator.count()) > 0 && (await locator.isVisible());
  } catch {
    return false;
  }
}

async function ensureLoggedIn(page, startUrl, log, { headless }) {
  log(`打开作者后台: ${startUrl}`);
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });

  if (!(await isLoginPage(page))) {
    log("检测到已登录状态");
    return;
  }

  if (headless) {
    throw new Error("检测到未登录状态，且当前为 headless 模式；请更新已登录的种子 profile 后重试");
  }

  log("检测到未登录状态，请在打开的浏览器里完成番茄作者登录。");
  await waitForEnter("登录完成后回到终端，按 Enter 继续...");
  await page.goto(startUrl, { waitUntil: "domcontentloaded" });
  if (await isLoginPage(page)) {
    throw new Error("仍处于未登录状态，终止签到修复");
  }
}

async function createSession(args, log) {
  const chromePath = args.chromePath || detectChromeExecutable();
  if (!chromePath) {
    throw new Error("未找到 Chrome/Chromium，可用 --chrome-path 显式指定");
  }

  if (args.seedProfileDir) {
    log(`从种子登录配置目录复制工作副本: ${args.seedProfileDir} -> ${args.profileDir}`);
    await cloneProfileSeed(args.seedProfileDir, args.profileDir);
  }

  ensureDir(args.profileDir);
  log(`启动独立浏览器: ${chromePath}`);
  const context = await chromium.launchPersistentContext(args.profileDir, {
    executablePath: chromePath,
    headless: Boolean(args.headless),
    viewport: { width: 1440, height: 900 },
    locale: "zh-CN",
    args: ["--start-maximized"],
  });

  return {
    context,
    close: async () => {
      if (!args.keepOpen) {
        await context.close();
      }
    },
  };
}

async function browserRequestJson(page, request) {
  const response = await page.evaluate(async (input) => {
    try {
      const result = await fetch(input.url, {
        method: input.method ?? "GET",
        credentials: "include",
        headers: input.headers ?? {},
        body: input.body ?? undefined,
        cache: input.cache ?? "default",
      });
      const text = await result.text();
      return {
        ok: true,
        status: result.status,
        url: result.url,
        text,
      };
    } catch (error) {
      return {
        ok: false,
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }, request);

  if (!response?.ok) {
    throw new Error(`浏览器请求失败: ${response?.message ?? "未知错误"}`);
  }

  let json = null;
  try {
    json = JSON.parse(response.text);
  } catch {
    json = null;
  }

  return {
    ...response,
    json,
  };
}

function assertApiSuccess(response, actionLabel) {
  if (response.status !== 200 || !response.json) {
    throw new Error(`${actionLabel}失败: status=${response.status}`);
  }
  if (response.json.code !== 0) {
    throw new Error(`${actionLabel}失败: code=${response.json.code} message=${response.json.message ?? ""}`);
  }
  return response.json.data ?? {};
}

async function queryAuthorCheckInCalendar(page, targetDate) {
  const { startDate, endDate } = getMonthQueryRange(targetDate);
  const response = await browserRequestJson(page, {
    url: `/api/author/attend/author_check_in/v0?start_date=${encodeURIComponent(startDate)}&end_date=${encodeURIComponent(endDate)}&_ts=${Date.now()}`,
    method: "GET",
    headers: {
      accept: "application/json, text/plain, */*",
      "cache-control": "no-cache",
    },
    cache: "no-store",
  });

  return {
    raw: response,
    data: assertApiSuccess(response, "查询作者签到日历"),
  };
}

async function queryCheckInTickets(page, targetDate) {
  const response = await browserRequestJson(page, {
    url: `/api/author/attend/check_in_ticket_list/v0?check_in_date=${encodeURIComponent(targetDate)}&_ts=${Date.now()}`,
    method: "GET",
    headers: {
      accept: "application/json, text/plain, */*",
      "cache-control": "no-cache",
    },
    cache: "no-store",
  });

  return {
    raw: response,
    data: assertApiSuccess(response, "查询补签卡"),
  };
}

async function queryBookList(page) {
  const response = await browserRequestJson(page, {
    url: `/api/author/homepage/book_list/v0/?page_count=50&page_index=0&image_fmt_list=396x220&_ts=${Date.now()}`,
    method: "GET",
    headers: {
      accept: "application/json, text/plain, */*",
      "cache-control": "no-cache",
    },
    cache: "no-store",
  });

  return {
    raw: response,
    data: assertApiSuccess(response, "查询作品列表"),
  };
}

async function queryBookAttendList(page, attendedActivity) {
  const response = await browserRequestJson(page, {
    url: `/api/origin/activity/novel/book_attend_list/v0/?page_count=999&page_index=0&image_fmt_list[]=172x224&attended_activity=${attendedActivity}&_ts=${Date.now()}`,
    method: "GET",
    headers: {
      accept: "application/json, text/plain, */*",
      "cache-control": "no-cache",
    },
    cache: "no-store",
  });

  return {
    raw: response,
    data: assertApiSuccess(response, "查询作品打卡活动列表"),
  };
}

async function queryBookAttendCalendar(page, bookId, targetDate) {
  const { startEpoch, endEpoch } = getMonthUnixRange(targetDate);
  const response = await browserRequestJson(page, {
    url: `/api/author/attend/book_attend/v0/?book_id=${encodeURIComponent(bookId)}&start_date=${startEpoch}&end_date=${endEpoch}&_ts=${Date.now()}`,
    method: "GET",
    headers: {
      accept: "application/json, text/plain, */*",
      "cache-control": "no-cache",
    },
    cache: "no-store",
  });

  return {
    raw: response,
    data: assertApiSuccess(response, "查询作品打卡月历"),
  };
}

function extractTargetDay(checkInInfo, targetDate) {
  const dayInfo = (checkInInfo?.check_in_info ?? []).find((item) => item.date === targetDate);
  if (!dayInfo) {
    throw new Error(`未在作者签到日历中找到目标日期: ${targetDate}`);
  }
  return dayInfo;
}

function extractBookAttendDay(attendInfo, targetDate) {
  return (attendInfo?.attend_info ?? []).find((item) => String(item.attend_date ?? "").slice(0, 10) === targetDate) ?? null;
}

function selectRepairTicket(ticketList, specifiedTicketId = "") {
  const tickets = Array.isArray(ticketList) ? ticketList : [];
  if (specifiedTicketId) {
    const matched = tickets.find((ticket) => String(ticket.ticket_id) === String(specifiedTicketId));
    if (!matched) {
      throw new Error(`未找到指定补签卡: ${specifiedTicketId}`);
    }
    return matched;
  }
  return tickets.find((ticket) => Number(ticket.status) === 0) ?? null;
}

function summarizeDayState(dayInfo) {
  return {
    date: dayInfo?.date ?? "",
    check_in_status: Number(dayInfo?.check_in_status ?? 0),
    check_in_amount: Number(dayInfo?.check_in_amount ?? 0),
    use_check_in_ticket: Boolean(dayInfo?.use_check_in_ticket),
    books: Array.isArray(dayInfo?.book_check_in_infos)
      ? dayInfo.book_check_in_infos.map((item) => ({
        book_name: item.book_name,
        check_in_amount: Number(item.check_in_amount ?? 0),
      }))
      : [],
  };
}

function summarizeTicket(ticketInfo) {
  if (!ticketInfo) {
    return null;
  }
  return {
    ticket_id: String(ticketInfo.ticket_id ?? ""),
    status: Number(ticketInfo.status ?? -1),
    type: Number(ticketInfo.type ?? 0),
    get_time: ticketInfo.get_time ?? "",
    valid_start_time: ticketInfo.valid_start_time ?? "",
    valid_end_time: ticketInfo.valid_end_time ?? "",
  };
}

function summarizeBookListItem(book) {
  return {
    book_id: String(book.book_id ?? ""),
    book_name: book.book_name ?? "",
    chapter_number: Number(book.chapter_number ?? 0),
    word_count: Number(book.word_count ?? 0),
    last_chapter_title: book.last_chapter_title ?? "",
    in_attend_activity: Number(book.in_attend_activity ?? 0),
    can_join_activity: Boolean(book.can_join_activity),
    need_extra_sign: Number(book.need_extra_sign ?? 0),
    sign_button: Number(book.sign_button ?? 0),
    sign_progress: Number(book.sign_progress ?? 0),
    book_intro_status: book.book_intro?.status ?? "",
    book_intro_tag: book.book_intro?.tag ?? "",
  };
}

function summarizeAttendListItem(item) {
  if (!item) {
    return null;
  }
  return {
    book_id: String(item.book_id ?? ""),
    book_name: item.book_name ?? "",
    in_attend_activity: Number(item.in_attend_activity ?? 0),
    days_attend_month_count: Number(item.days_attend_month_count ?? 0),
    words_attend_count_monthly: Number(item.words_attend_count_monthly ?? 0),
    words_attend_day_count: Number(item.words_attend_day_count ?? 0),
    attend_activity_list: Array.isArray(item.attend_activity_list)
      ? item.attend_activity_list.map((activity) => ({
        activity_id: Number(activity.activity_id ?? 0),
        activity_name: activity.activity_name ?? "",
        status: Number(activity.status ?? 0),
        type: Number(activity.type ?? 0),
        attend_day: Number(activity.attend_day ?? 0),
      }))
      : [],
  };
}

function summarizeBookAttendDay(dayInfo) {
  if (!dayInfo) {
    return null;
  }
  return {
    attend_date: String(dayInfo.attend_date ?? "").slice(0, 10),
    amount: Number(dayInfo.amount ?? 0),
    status: Number(dayInfo.status ?? 0),
    use_ticket_for_leave: Number(dayInfo.use_ticket_for_leave ?? 0),
  };
}

function buildTimingDiagnostics(targetDate) {
  const now = new Date();
  const today = formatLocalDate(now);
  const yesterday = getYesterdayDate();
  const target = normalizeDate(targetDate);
  const nextDay = addDays(target, 1);
  nextDay.setHours(2, 40, 0, 0);

  return {
    local_now: now.toISOString(),
    local_now_text: now.toLocaleString("zh-CN", { hour12: false }),
    today,
    yesterday,
    target_date: target,
    is_today: target === today,
    is_yesterday: target === yesterday,
    settlement_ready_at_local: nextDay.toISOString(),
    settlement_ready_text: nextDay.toLocaleString("zh-CN", { hour12: false }),
    settlement_note: target === today
      ? "目标日期是今天；作者签到月历通常在次日凌晨结算，当天白天看到 0 属正常。"
      : "作者签到月历通常在目标日期结束后的次日凌晨结算。",
  };
}

async function collectBookAttendDiagnostics(page, targetDate, authorDayInfo, log) {
  const timing = buildTimingDiagnostics(targetDate);
  const bookListResult = await queryBookList(page);
  const attendedListResult = await queryBookAttendList(page, 1);
  const unattendedListResult = await queryBookAttendList(page, 0);

  const attendedMap = new Map(
    (attendedListResult.data.book_items ?? []).map((item) => [String(item.book_id), item]),
  );
  const unattendedMap = new Map(
    (unattendedListResult.data.book_items ?? []).map((item) => [String(item.book_id), item]),
  );
  const authorBookAmounts = new Map(
    (authorDayInfo?.book_check_in_infos ?? []).map((item) => [String(item.book_name), Number(item.check_in_amount ?? 0)]),
  );

  const books = [];
  for (const book of bookListResult.data.book_list ?? []) {
    const bookSummary = summarizeBookListItem(book);
    const attendedSummary = summarizeAttendListItem(attendedMap.get(bookSummary.book_id) ?? null);
    const unattendedSummary = summarizeAttendListItem(unattendedMap.get(bookSummary.book_id) ?? null);
    const authorIncluded = authorBookAmounts.has(bookSummary.book_name);

    let bookAttendDay = null;
    let bookAttendError = "";
    try {
      const bookAttendResult = await queryBookAttendCalendar(page, bookSummary.book_id, targetDate);
      bookAttendDay = summarizeBookAttendDay(extractBookAttendDay(bookAttendResult.data, targetDate));
    } catch (error) {
      bookAttendError = error?.message || String(error);
      log(`作品打卡诊断失败: ${bookSummary.book_name} ${bookAttendError}`);
    }

    const diagnostics = [];
    if (bookAttendDay?.amount > 0 && !authorIncluded) {
      if (timing.is_today) {
        diagnostics.push("作品打卡已出字数，但作者签到月历通常要到次日凌晨才会结算");
      } else {
        diagnostics.push("作品打卡成功，但作者签到月历未收录");
      }
    }
    if (!attendedSummary) {
      diagnostics.push("未出现在 attended_activity=1 活动列表");
    }
    if (!attendedSummary && bookSummary.can_join_activity === false) {
      diagnostics.push("当前作品没有可加入的签到活动入口");
    }

    books.push({
      ...bookSummary,
      author_check_in_included: authorIncluded,
      author_check_in_amount: authorIncluded ? Number(authorBookAmounts.get(bookSummary.book_name) ?? 0) : 0,
      book_attend_day: bookAttendDay,
      attended_activity_item: attendedSummary,
      unattended_activity_item: unattendedSummary,
      book_attend_query_error: bookAttendError,
      diagnostics,
    });
  }

  const booksWithAttendAmount = books
    .filter((book) => Number(book.book_attend_day?.amount ?? 0) > 0)
    .map((book) => ({
      book_name: book.book_name,
      amount: Number(book.book_attend_day?.amount ?? 0),
      status: Number(book.book_attend_day?.status ?? 0),
    }));
  const booksMissingFromAuthorCheckIn = books
    .filter((book) => Number(book.book_attend_day?.amount ?? 0) > 0 && !book.author_check_in_included && !timing.is_today)
    .map((book) => ({
      book_name: book.book_name,
      amount: Number(book.book_attend_day?.amount ?? 0),
      diagnostics: book.diagnostics,
    }));
  const booksPendingAuthorSettlement = books
    .filter((book) => Number(book.book_attend_day?.amount ?? 0) > 0 && !book.author_check_in_included && timing.is_today)
    .map((book) => ({
      book_name: book.book_name,
      amount: Number(book.book_attend_day?.amount ?? 0),
      diagnostics: book.diagnostics,
    }));

  return {
    queried_at: new Date().toISOString(),
    target_date: targetDate,
    timing,
    author_captured_books: [...authorBookAmounts.keys()],
    attended_books: (attendedListResult.data.book_items ?? []).map((item) => summarizeAttendListItem(item)),
    unattended_books: (unattendedListResult.data.book_items ?? []).map((item) => summarizeAttendListItem(item)),
    books,
    summary: {
      books_with_attend_amount: booksWithAttendAmount,
      books_missing_from_author_check_in: booksMissingFromAuthorCheckIn,
      books_pending_author_settlement: booksPendingAuthorSettlement,
    },
  };
}

function didRepairTakeEffect(beforeDay, afterDay, beforeTicket, afterTicket) {
  if (!afterDay) {
    return false;
  }
  if (Number(afterDay.check_in_status ?? 0) !== Number(beforeDay.check_in_status ?? 0)) {
    return true;
  }
  if (Boolean(beforeDay.use_check_in_ticket) && !Boolean(afterDay.use_check_in_ticket)) {
    return true;
  }
  if (Number(afterDay.check_in_amount ?? 0) > Number(beforeDay.check_in_amount ?? 0)) {
    return true;
  }
  if (beforeTicket && !afterTicket) {
    return true;
  }
  if (beforeTicket && afterTicket && Number(afterTicket.status ?? -1) !== Number(beforeTicket.status ?? -1)) {
    return true;
  }
  return false;
}

async function pollVerifyRepair(page, targetDate, ticketId, beforeDay, beforeTicket, log) {
  const delays = [0, 1500, 4000, 8000];
  const snapshots = [];

  for (const delayMs of delays) {
    if (delayMs > 0) {
      await page.waitForTimeout(delayMs);
    }

    const checkInResult = await queryAuthorCheckInCalendar(page, targetDate);
    const ticketResult = await queryCheckInTickets(page, targetDate);
    const afterDay = extractTargetDay(checkInResult.data, targetDate);
    const afterTicket = (ticketResult.data.ticket_list ?? []).find((ticket) => String(ticket.ticket_id) === String(ticketId)) ?? null;
    const snapshot = {
      polled_at: new Date().toISOString(),
      delay_ms: delayMs,
      day: summarizeDayState(afterDay),
      ticket: summarizeTicket(afterTicket),
    };
    snapshots.push(snapshot);

    if (didRepairTakeEffect(beforeDay, afterDay, beforeTicket, afterTicket)) {
      log(`回查通过：${targetDate} 状态已变化`);
      return {
        success: true,
        afterDay,
        afterTicket,
        snapshots,
      };
    }
  }

  return {
    success: false,
    snapshots,
  };
}

async function callRepairApi(page, targetDate, ticketId) {
  const body = new URLSearchParams({
    ticket_id: String(ticketId),
    check_in_date: targetDate,
  }).toString();

  const response = await browserRequestJson(page, {
    url: "/api/author/attend/use_check_in_ticket/v0",
    method: "POST",
    headers: {
      accept: "application/json, text/plain, */*",
      "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
    },
    body,
    cache: "no-store",
  });

  if (response.status !== 200 || !response.json) {
    throw new Error(`补签接口调用失败: status=${response.status}`);
  }

  return response;
}

async function executeCheckInRepair(page, args, log) {
  const targetDate = normalizeDate(args.date || getYesterdayDate());
  const timingDiagnostics = buildTimingDiagnostics(targetDate);
  log(`目标签到日期: ${targetDate}`);
  log(`结算提示: ${timingDiagnostics.settlement_note} 建议检查时间 ${timingDiagnostics.settlement_ready_text}`);

  const beforeCheckInResult = await queryAuthorCheckInCalendar(page, targetDate);
  const beforeTicketResult = await queryCheckInTickets(page, targetDate);
  const beforeDay = extractTargetDay(beforeCheckInResult.data, targetDate);
  const selectedTicket = selectRepairTicket(beforeTicketResult.data.ticket_list, args.ticketId);
  const beforeTicket = selectedTicket
    ? (beforeTicketResult.data.ticket_list ?? []).find((ticket) => String(ticket.ticket_id) === String(selectedTicket.ticket_id)) ?? selectedTicket
    : null;
  const beforeDaySummary = summarizeDayState(beforeDay);
  const beforeBookAttendDiagnostics = await collectBookAttendDiagnostics(page, targetDate, beforeDay, log);

  log(`修复前作者签到状态: ${JSON.stringify(beforeDaySummary)}`);
  if (beforeBookAttendDiagnostics.summary.books_missing_from_author_check_in.length > 0) {
    const names = beforeBookAttendDiagnostics.summary.books_missing_from_author_check_in
      .map((item) => `${item.book_name}(${item.amount})`)
      .join("、");
    log(`发现作品级打卡与作者签到月历不一致: ${names}`);
  }
  if (beforeBookAttendDiagnostics.summary.books_pending_author_settlement.length > 0) {
    const names = beforeBookAttendDiagnostics.summary.books_pending_author_settlement
      .map((item) => `${item.book_name}(${item.amount})`)
      .join("、");
    log(`目标日期尚在作者签到结算窗口内，待次日凌晨入账: ${names}`);
  }

  if (args.checkOnly) {
    log("当前为仅检查模式，不调用补签接口");
    return {
      ok: true,
      action: "check_only",
      targetDate,
      timingDiagnostics,
      canRepair: Boolean(beforeDay.use_check_in_ticket && selectedTicket),
      selectedTicketId: selectedTicket?.ticket_id ?? "",
      beforeDay: beforeDaySummary,
      beforeTicket: summarizeTicket(beforeTicket),
      beforeBookAttendDiagnostics,
      beforeCheckInResponse: beforeCheckInResult.raw.json,
      beforeTicketResponse: beforeTicketResult.raw.json,
    };
  }

  if (timingDiagnostics.is_today && beforeDay.check_in_status === 0 && !beforeDay.use_check_in_ticket) {
    log("目标日期是今天，作者签到月历尚未进入补签阶段，按待结算处理");
    return {
      ok: true,
      action: "pending_settlement",
      targetDate,
      timingDiagnostics,
      note: "目标日期是今天；作者签到月历通常在次日凌晨结算，当天白天看到 0 属正常。",
      beforeDay: beforeDaySummary,
      beforeTicket: summarizeTicket(beforeTicket),
      beforeBookAttendDiagnostics,
      beforeCheckInResponse: beforeCheckInResult.raw.json,
      beforeTicketResponse: beforeTicketResult.raw.json,
    };
  }

  if (beforeDay.check_in_status !== 0 && !beforeDay.use_check_in_ticket) {
    log(`目标日期已是签到成功状态，无需补签: ${targetDate}`);
    return {
      ok: true,
      action: "already_signed",
      targetDate,
      timingDiagnostics,
      beforeDay: beforeDaySummary,
      beforeTicket: summarizeTicket(beforeTicket),
      beforeBookAttendDiagnostics,
      beforeCheckInResponse: beforeCheckInResult.raw.json,
      beforeTicketResponse: beforeTicketResult.raw.json,
    };
  }

  if (!beforeDay.use_check_in_ticket) {
    throw new Error(`目标日期当前不可补签: ${targetDate}`);
  }
  if (!selectedTicket) {
    throw new Error(`没有可用补签卡，无法处理 ${targetDate}`);
  }

  log(`选中的补签卡: ${selectedTicket.ticket_id}`);

  if (args.dryRun) {
    log("当前为 dry-run 模式，不真正补签");
    return {
      ok: true,
      action: "dry_run",
      targetDate,
      timingDiagnostics,
      selectedTicketId: selectedTicket.ticket_id,
      beforeDay: beforeDaySummary,
      beforeTicket: summarizeTicket(beforeTicket),
      beforeBookAttendDiagnostics,
      beforeCheckInResponse: beforeCheckInResult.raw.json,
      beforeTicketResponse: beforeTicketResult.raw.json,
    };
  }

  const postResult = await callRepairApi(page, targetDate, selectedTicket.ticket_id);
  log(`补签接口返回: code=${postResult.json.code} message=${postResult.json.message ?? ""}`);

  const verification = await pollVerifyRepair(
    page,
    targetDate,
    selectedTicket.ticket_id,
    beforeDay,
    beforeTicket,
    log,
  );

  if (postResult.json.code === 0 && verification.success) {
    const afterBookAttendDiagnostics = await collectBookAttendDiagnostics(page, targetDate, verification.afterDay, log);
    return {
      ok: true,
      action: "repaired",
      targetDate,
      timingDiagnostics,
      selectedTicketId: selectedTicket.ticket_id,
      beforeDay: beforeDaySummary,
      beforeTicket: summarizeTicket(beforeTicket),
      afterDay: summarizeDayState(verification.afterDay),
      afterTicket: summarizeTicket(verification.afterTicket),
      beforeBookAttendDiagnostics,
      afterBookAttendDiagnostics,
      beforeCheckInResponse: beforeCheckInResult.raw.json,
      beforeTicketResponse: beforeTicketResult.raw.json,
      postResponse: postResult.json,
      verificationSnapshots: verification.snapshots,
    };
  }

  if (postResult.json.code !== 0 && verification.success) {
    const afterBookAttendDiagnostics = await collectBookAttendDiagnostics(page, targetDate, verification.afterDay, log);
    log(`平台返回失败码，但回查已生效，按成功处理: code=${postResult.json.code}`);
    return {
      ok: true,
      action: "repaired_with_warning",
      targetDate,
      timingDiagnostics,
      selectedTicketId: selectedTicket.ticket_id,
      warning: `补签接口返回 code=${postResult.json.code} message=${postResult.json.message ?? ""}，但回查状态已变化`,
      beforeDay: beforeDaySummary,
      beforeTicket: summarizeTicket(beforeTicket),
      afterDay: summarizeDayState(verification.afterDay),
      afterTicket: summarizeTicket(verification.afterTicket),
      beforeBookAttendDiagnostics,
      afterBookAttendDiagnostics,
      beforeCheckInResponse: beforeCheckInResult.raw.json,
      beforeTicketResponse: beforeTicketResult.raw.json,
      postResponse: postResult.json,
      verificationSnapshots: verification.snapshots,
    };
  }

  const detail = {
    targetDate,
    timingDiagnostics,
    selectedTicketId: selectedTicket.ticket_id,
    beforeDay: beforeDaySummary,
    beforeTicket: summarizeTicket(beforeTicket),
    beforeBookAttendDiagnostics,
    postResponse: postResult.json,
    verificationSnapshots: verification.snapshots,
  };

  if (Number(postResult.json.code) === -5901) {
    throw new Error(`平台补签状态不一致: ${JSON.stringify(detail, null, 2)}`);
  }
  throw new Error(`补签未生效: ${JSON.stringify(detail, null, 2)}`);
}

async function main() {
  const rawArgs = parseArgs(process.argv.slice(2));
  if (rawArgs.help) {
    printHelp();
    return;
  }

  const args = await loadRuntimeConfig(rawArgs);
  args.date = rawArgs.date
    ? normalizeDate(rawArgs.date, "--date")
    : (rawArgs.repairYesterday || !rawArgs.date ? getYesterdayDate() : rawArgs.date);
  args.startUrl = args.startUrl || DEFAULT_START_URL;
  args.profileDir = args.profileDir || DEFAULT_PROFILE_DIR;
  args.artifactsDir = args.artifactsDir || DEFAULT_ARTIFACTS_DIR;
  args.headless = args.headless ?? true;

  ensureDir(args.artifactsDir);
  const logFile = path.join(args.artifactsDir, "run.log");
  fs.writeFileSync(logFile, "", "utf8");
  const log = createLogger(logFile);

  const resultBase = {
    startedAt: new Date().toISOString(),
    targetDate: args.date,
    mode: args.checkOnly ? "check_only" : (args.dryRun ? "dry_run" : "repair"),
    headless: Boolean(args.headless),
  };

  const session = await createSession(args, log);
  let result = resultBase;

  try {
    const page = await session.context.newPage();
    await ensureLoggedIn(page, args.startUrl, log, { headless: args.headless });
    result = {
      ...resultBase,
      ...(await executeCheckInRepair(page, args, log)),
      finishedAt: new Date().toISOString(),
    };
    const outputPath = path.join(args.artifactsDir, `result_${args.date.replaceAll("-", "")}.json`);
    await fsp.writeFile(outputPath, `${JSON.stringify(result, null, 2)}\n`, "utf8");
    log(`结果已写入: ${outputPath}`);
  } catch (error) {
    result = {
      ...resultBase,
      ok: false,
      finishedAt: new Date().toISOString(),
      error: error?.stack || String(error),
    };
    const outputPath = path.join(args.artifactsDir, `result_${args.date.replaceAll("-", "")}.json`);
    await fsp.writeFile(outputPath, `${JSON.stringify(result, null, 2)}\n`, "utf8");
    log(`失败结果已写入: ${outputPath}`);
    throw error;
  } finally {
    await session.close();
  }
}

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exitCode = 1;
});

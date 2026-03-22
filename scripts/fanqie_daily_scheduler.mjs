import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import readline from "node:readline";
import { spawn } from "node:child_process";

const DEFAULT_START_URL = "https://fanqienovel.com/main/writer/?enter_from=author_zone";
const WEEKDAY_KEYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

function printHelp() {
  console.log(`番茄小说日更调度器

用法:
  node scripts/fanqie_daily_scheduler.mjs --config fanqie_daily_jobs.json

常用参数:
  --config <path>   配置文件路径，默认 fanqie_daily_jobs.json
  --once            只检查并执行一轮后退出
  --run-now         忽略时间设置，立即执行符合筛选条件的任务
  --skip-delay      忽略随机延迟窗口，立刻执行
  --dry-run         只打印将要执行的上传命令，不真的发章
  --job <id>        只运行指定 job id
  --help            显示帮助
`);
}

function parseArgs(argv) {
  const args = {
    configPath: path.resolve(process.cwd(), "fanqie_daily_jobs.json"),
    once: false,
    runNow: false,
    retryUntilSuccess: false,
    skipDelay: false,
    dryRun: false,
    jobId: "",
    chapterCount: 0,
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
      case "--config":
        args.configPath = path.resolve(next());
        break;
      case "--once":
        args.once = true;
        break;
      case "--run-now":
        args.runNow = true;
        break;
      case "--retry-until-success":
        args.retryUntilSuccess = true;
        break;
      case "--skip-delay":
        args.skipDelay = true;
        break;
      case "--dry-run":
        args.dryRun = true;
        break;
      case "--job":
        args.jobId = next();
        break;
      case "--chapter-count":
        args.chapterCount = Number(next());
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

function logFactory(logFile) {
  return (message) => {
    const stamp = new Date().toISOString().replace("T", " ").slice(0, 19);
    const line = `${stamp} | ${message}`;
    console.log(line);
    fs.appendFileSync(logFile, `${line}\n`, "utf8");
  };
}

function isUrl(value) {
  return /^[a-z]+:\/\//i.test(value);
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
  if (isUrl(value)) {
    return value;
  }
  const expanded = expandPathVariables(value);
  return path.isAbsolute(expanded) ? expanded : path.resolve(configDir, expanded);
}

function parsePublishTime(value) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(value ?? "");
  if (!match) {
    throw new Error(`无效的 publishTime: ${value}`);
  }
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  if (hours < 0 || hours > 23 || minutes < 0 || minutes > 59) {
    throw new Error(`无效的 publishTime: ${value}`);
  }
  return { hours, minutes };
}

function computeNextOccurrence(now, publishTime) {
  const { hours, minutes } = parsePublishTime(publishTime);
  const target = new Date(now);
  target.setHours(hours, minutes, 0, 0);
  if (target <= now) {
    target.setDate(target.getDate() + 1);
  }
  return target;
}

function formatLocalDateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function computePublishAnchor(now, publishTime) {
  const { hours, minutes } = parsePublishTime(publishTime);
  const anchor = new Date(now);
  anchor.setHours(hours, minutes, 0, 0);
  return anchor;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function normalizeRetryNumber(value, fallbackValue) {
  if (value === undefined || value === null || value === "") {
    return fallbackValue;
  }
  const resolved = Number(value);
  if (!Number.isFinite(resolved) || resolved <= 0) {
    throw new Error(`invalid retry config value: ${value}`);
  }
  return resolved;
}

function computeRetryDelayMinutes(baseMinutes, multiplier, maxMinutes, attemptCount) {
  const safeAttempt = Math.max(1, Number(attemptCount) || 1);
  const scaledDelay = baseMinutes * Math.pow(multiplier, safeAttempt - 1);
  const boundedDelay = Math.min(scaledDelay, maxMinutes);
  return Math.max(1, Math.round(boundedDelay));
}

function getPendingRetryJobs(jobs, stateJobs) {
  return jobs
    .map((job) => ({ job, entry: stateJobs[job.id] ?? {} }))
    .filter(({ entry }) => Number(entry.lastExitCode ?? 0) !== 0 && entry.nextRunAt)
    .map(({ job, entry }) => ({
      job,
      entry,
      nextRetryAt: new Date(entry.nextRunAt),
      retryAttemptCount: Number(entry.retryAttemptCount ?? 0),
    }))
    .filter(({ nextRetryAt }) => Number.isFinite(nextRetryAt.getTime()));
}

function normalizeWeekdayCounts(rawMap) {
  const counts = {};
  if (!rawMap || typeof rawMap !== "object") {
    return counts;
  }
  for (const key of WEEKDAY_KEYS) {
    if (rawMap[key] === undefined || rawMap[key] === null || rawMap[key] === "") {
      continue;
    }
    const value = Number(rawMap[key]);
    if (!Number.isFinite(value) || value < 0) {
      throw new Error(`weekday 章节数无效: ${key}=${rawMap[key]}`);
    }
    counts[key] = value;
  }
  return counts;
}

function normalizeAiUsage(rawValue, sourceLabel = "aiUsage") {
  if (typeof rawValue === "boolean") {
    return rawValue ? "yes" : "no";
  }
  const value = String(rawValue ?? "").trim().toLowerCase();
  if (!value) {
    return "no";
  }
  if (["yes", "true", "1", "y", "ai", "是"].includes(value)) {
    return "yes";
  }
  if (["no", "false", "0", "n", "human", "manual", "否"].includes(value)) {
    return "no";
  }
  throw new Error(`${sourceLabel} 只支持 yes/no`);
}

function getWeekdayKey(date) {
  return WEEKDAY_KEYS[date.getDay()];
}

function resolveChapterCountForDate(job, date) {
  const weekdayKey = getWeekdayKey(date);
  if (job.chaptersPerRunByWeekday[weekdayKey] !== undefined) {
    return Number(job.chaptersPerRunByWeekday[weekdayKey]);
  }
  return Number(job.chaptersPerRun);
}

function buildUploaderArgs(repoRoot, job, chapterCount) {
  const args = [
    path.join(repoRoot, "scripts", "fanqie_upload.mjs"),
    "--book-title", job.bookTitle,
    "--chapters-dir", job.chaptersDir,
    "--auto-next-count", String(chapterCount),
    "--ai-usage", job.aiUsage,
    "--profile-dir", job.profileDir,
    "--artifacts-dir", job.artifactsDir,
    "--start-url", job.startUrl,
  ];

  if (job.seedProfileDir) {
    args.push("--seed-profile-dir", job.seedProfileDir);
  }
  if (job.chromePath) {
    args.push("--chrome-path", job.chromePath);
  }
  if (job.debug) {
    args.push("--debug");
  }
  if (job.headless !== false) {
    args.push("--headless");
  }
  return args;
}

async function loadJson(filePath, fallbackValue) {
  try {
    return JSON.parse(await fsp.readFile(filePath, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") {
      return fallbackValue;
    }
    throw error;
  }
}

async function saveJson(filePath, value) {
  ensureDir(path.dirname(filePath));
  await fsp.writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function normalizeJob(configDir, defaults, rawJob) {
  const chaptersPerRun = Number(rawJob.chaptersPerRun ?? defaults.chaptersPerRun ?? 0);
  const chaptersPerRunByWeekday = {
    ...normalizeWeekdayCounts(defaults.chaptersPerRunByWeekday),
    ...normalizeWeekdayCounts(rawJob.chaptersPerRunByWeekday),
  };
  const job = {
    id: rawJob.id,
    enabled: rawJob.enabled !== false,
    bookTitle: rawJob.bookTitle,
    chaptersDir: resolvePathLike(configDir, rawJob.chaptersDir),
    publishTime: rawJob.publishTime ?? defaults.publishTime ?? "12:00",
    chaptersPerRun,
    chaptersPerRunByWeekday,
    publishWindowMinutes: Number(rawJob.publishWindowMinutes ?? defaults.publishWindowMinutes ?? 0),
    aiUsage: normalizeAiUsage(rawJob.aiUsage ?? defaults.aiUsage ?? "no", `job ${rawJob.id} 的 aiUsage`),
    headless: rawJob.headless ?? defaults.headless ?? true,
    startUrl: rawJob.startUrl ?? defaults.startUrl ?? DEFAULT_START_URL,
    seedProfileDir: resolvePathLike(configDir, rawJob.seedProfileDir ?? defaults.seedProfileDir ?? ""),
    chromePath: resolvePathLike(configDir, rawJob.chromePath ?? defaults.chromePath ?? ""),
    profileDir: resolvePathLike(
      configDir,
      rawJob.profileDir ?? path.join(defaults.profileRoot ?? ".run/fanqie_daily_profiles", rawJob.id),
    ),
    artifactsDir: resolvePathLike(
      configDir,
      rawJob.artifactsDir ?? path.join(defaults.artifactsRoot ?? ".run/fanqie_daily_artifacts", rawJob.id),
    ),
    debug: rawJob.debug ?? defaults.debug ?? false,
  };

  if (!job.id) {
    throw new Error("job.id 不能为空");
  }
  if (!job.bookTitle) {
    throw new Error(`job ${job.id} 缺少 bookTitle`);
  }
  if (!job.chaptersDir) {
    throw new Error(`job ${job.id} 缺少 chaptersDir`);
  }
  if (!Number.isFinite(job.chaptersPerRun) || job.chaptersPerRun < 0) {
    throw new Error(`job ${job.id} 的 chaptersPerRun 不能小于 0`);
  }
  if (!Number.isFinite(job.publishWindowMinutes) || job.publishWindowMinutes < 0) {
    throw new Error(`job ${job.id} 的 publishWindowMinutes 不能小于 0`);
  }
  const availableCounts = [job.chaptersPerRun, ...Object.values(job.chaptersPerRunByWeekday)];
  if (!availableCounts.some((value) => Number(value) > 0)) {
    throw new Error(`job ${job.id} 没有任何有效的每日上传章数配置`);
  }
  parsePublishTime(job.publishTime);
  return job;
}

async function runUploaderForJob(repoRoot, log, job, dryRun, runDate, chapterCountOverride = 0) {
  const chapterCount = chapterCountOverride > 0
    ? Number(chapterCountOverride)
    : resolveChapterCountForDate(job, runDate);
  const weekdayKey = getWeekdayKey(runDate);
  if (chapterCount <= 0) {
    log(`[${job.id}] ${weekdayKey} 配置为 0 章，今日跳过`);
    return 0;
  }

  const args = buildUploaderArgs(repoRoot, job, chapterCount);
  const displayCommand = [process.execPath, ...args].join(" ");
  if (dryRun) {
    log(`[${job.id}] dry-run (${weekdayKey}=${chapterCount}章): ${displayCommand}`);
    return 0;
  }

  log(`[${job.id}] 启动上传任务 (${weekdayKey}=${chapterCount}章): ${displayCommand}`);
  return await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, {
      cwd: repoRoot,
      env: {
        ...process.env,
        PYTHONIOENCODING: "utf-8",
        PYTHONUTF8: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });

    const bindStream = (stream, label) => {
      const rl = readline.createInterface({ input: stream });
      rl.on("line", (line) => {
        log(`[${job.id}] ${label}${line}`);
      });
    };
    bindStream(child.stdout, "");
    bindStream(child.stderr, "ERR ");

    child.on("error", reject);
    child.on("close", (code) => resolve(code ?? 1));
  });
}

function getRandomizedExecutionState(entry, job, now, log) {
  const anchor = computePublishAnchor(now, job.publishTime);
  const dateKey = formatLocalDateKey(anchor);

  if (
    entry.randomizedExecution &&
    entry.randomizedExecution.dateKey === dateKey &&
    entry.randomizedExecution.publishTime === job.publishTime &&
    Number(entry.randomizedExecution.windowMinutes) === Number(job.publishWindowMinutes)
  ) {
    return entry.randomizedExecution;
  }

  const delayMinutes = Math.floor(Math.random() * (job.publishWindowMinutes + 1));
  const executeAt = new Date(anchor.getTime() + delayMinutes * 60 * 1000);
  entry.randomizedExecution = {
    dateKey,
    publishTime: job.publishTime,
    windowMinutes: job.publishWindowMinutes,
    delayMinutes,
    executeAt: executeAt.toISOString(),
  };
  log(`[${job.id}] 已生成随机延迟：${delayMinutes} 分钟，预计执行时间 ${executeAt.toLocaleString("zh-CN", { hour12: false })}`);
  return entry.randomizedExecution;
}

function clearRandomizedExecution(entry) {
  delete entry.randomizedExecution;
}

function isJobDue(stateEntry, now, runNow) {
  if (runNow) {
    return true;
  }
  if (!stateEntry.nextRunAt) {
    return false;
  }
  return new Date(stateEntry.nextRunAt) <= now;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    printHelp();
    return;
  }
  if (args.chapterCount && (!Number.isFinite(args.chapterCount) || args.chapterCount < 0)) {
    throw new Error(`invalid --chapter-count: ${args.chapterCount}`);
  }

  const configPath = args.configPath;
  const repoRoot = path.dirname(configPath);
  const configDir = path.dirname(configPath);
  const rawConfig = await loadJson(configPath, null);
  if (!rawConfig) {
    throw new Error(`配置文件不存在: ${configPath}`);
  }

  const defaults = rawConfig.defaults ?? {};
  const statePath = resolvePathLike(configDir, rawConfig.stateFile ?? ".run/fanqie_daily_scheduler_state.json");
  const logPath = resolvePathLike(configDir, rawConfig.logFile ?? ".run/fanqie_daily_scheduler.log");
  const tickSeconds = Number(rawConfig.tickSeconds ?? 30);
  const retryMinutes = normalizeRetryNumber(rawConfig.retryMinutes ?? defaults.retryMinutes ?? 30, 30);
  const retryBackoffMultiplier = normalizeRetryNumber(
    rawConfig.retryBackoffMultiplier ?? defaults.retryBackoffMultiplier ?? 2,
    2,
  );
  const retryMaxMinutes = normalizeRetryNumber(rawConfig.retryMaxMinutes ?? defaults.retryMaxMinutes ?? 360, 360);

  ensureDir(path.dirname(logPath));
  const log = logFactory(logPath);

  const jobs = (rawConfig.jobs ?? [])
    .map((job) => normalizeJob(configDir, defaults, job))
    .filter((job) => job.enabled)
    .filter((job) => !args.jobId || job.id === args.jobId);

  if (!jobs.length) {
    log("没有启用的日更任务，调度器退出");
    return;
  }

  const state = await loadJson(statePath, { jobs: {} });
  state.jobs ??= {};
  const now = new Date();
  for (const job of jobs) {
    const entry = state.jobs[job.id] ?? {};
    if (!args.runNow) {
      entry.nextRunAt = computeNextOccurrence(now, job.publishTime).toISOString();
    }
    entry.lastKnownPublishTime = job.publishTime;
    state.jobs[job.id] = entry;
  }
  await saveJson(statePath, state);
  log(`调度器启动，任务数=${jobs.length}，config=${configPath}`);
  let forceRunNow = args.runNow;

  while (true) {
    let hasFailure = false;
    const failedJobs = [];
    const loopNow = new Date();
    const latestState = await loadJson(statePath, { jobs: {} });
    latestState.jobs ??= {};

    for (const job of jobs) {
      const entry = latestState.jobs[job.id] ?? {};
      if (!args.runNow && !entry.nextRunAt) {
        entry.nextRunAt = computeNextOccurrence(loopNow, job.publishTime).toISOString();
        latestState.jobs[job.id] = entry;
      }
      if (!isJobDue(entry, loopNow, forceRunNow)) {
        continue;
      }

      entry.lastAttemptAt = new Date().toISOString();
      latestState.jobs[job.id] = entry;
      await saveJson(statePath, latestState);

      if (!args.skipDelay && !args.dryRun && job.publishWindowMinutes > 0) {
        const delayNow = new Date();
        const randomized = getRandomizedExecutionState(entry, job, delayNow, log);
        latestState.jobs[job.id] = entry;
        await saveJson(statePath, latestState);

        const executeAt = new Date(randomized.executeAt);
        if (executeAt > delayNow) {
          if (args.once) {
            const waitMs = executeAt.getTime() - delayNow.getTime();
            log(`[${job.id}] 等待随机窗口，到 ${executeAt.toLocaleString("zh-CN", { hour12: false })} 后执行`);
            await sleep(waitMs);
          } else {
            entry.nextRunAt = randomized.executeAt;
            latestState.jobs[job.id] = entry;
            await saveJson(statePath, latestState);
            continue;
          }
        }
      }

      const exitCode = await runUploaderForJob(
        repoRoot,
        log,
        job,
        args.dryRun,
        new Date(),
        args.chapterCount,
      );
      if (exitCode === 0) {
        entry.lastSuccessAt = new Date().toISOString();
        entry.lastExitCode = 0;
        entry.retryAttemptCount = 0;
        entry.nextRunAt = computeNextOccurrence(new Date(), job.publishTime).toISOString();
        clearRandomizedExecution(entry);
        log(`[${job.id}] 本轮任务成功，下一次执行时间 ${entry.nextRunAt}`);
      } else {
        hasFailure = true;
        failedJobs.push(`${job.id}:${exitCode}`);
        entry.lastFailureAt = new Date().toISOString();
        entry.lastExitCode = exitCode;
        entry.retryAttemptCount = Number(entry.retryAttemptCount ?? 0) + 1;
        const retryDelayMinutes = computeRetryDelayMinutes(
          retryMinutes,
          retryBackoffMultiplier,
          retryMaxMinutes,
          entry.retryAttemptCount,
        );
        entry.nextRunAt = new Date(Date.now() + retryDelayMinutes * 60 * 1000).toISOString();
        log(`[${job.id}] retry scheduled: attempt=${entry.retryAttemptCount}, delayMinutes=${retryDelayMinutes}`);
        log(`[${job.id}] 本轮任务失败，exit=${exitCode}，将在 ${entry.nextRunAt} 重试`);
      }
      latestState.jobs[job.id] = entry;
      await saveJson(statePath, latestState);
    }

    forceRunNow = false;

    if (args.once) {
      if (hasFailure && args.retryUntilSuccess) {
        const pendingRetryJobs = getPendingRetryJobs(jobs, latestState.jobs);
        const nextRetry = pendingRetryJobs
          .slice()
          .sort((left, right) => left.nextRetryAt.getTime() - right.nextRetryAt.getTime())[0];

        if (nextRetry) {
          const waitMs = Math.max(0, nextRetry.nextRetryAt.getTime() - Date.now());
          const retrySummary = pendingRetryJobs
            .map(({ job, retryAttemptCount, nextRetryAt }) => {
              const attemptLabel = retryAttemptCount > 0 ? retryAttemptCount : 1;
              return `${job.id}(retry=${attemptLabel}, at=${nextRetryAt.toISOString()})`;
            })
            .join(", ");
          log(`retry-until-success active: ${retrySummary}`);
          if (waitMs > 0) {
            log(`waiting ${Math.ceil(waitMs / 1000)} seconds before next retry loop`);
            await sleep(waitMs);
          }
          continue;
        }
      }
      if (failedJobs.length > 0) {
        log(`单轮执行存在失败任务：${failedJobs.join(", ")}`);
      }
      log("完成单轮检查，调度器退出");
      if (hasFailure) {
        process.exitCode = 1;
      }
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, tickSeconds * 1000));
  }
}

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exitCode = 1;
});

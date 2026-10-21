import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import { chromium } from "playwright-core";

const DEFAULT_SEED_PROFILE = path.resolve(".run", "fanqie_writer_seed_profile");
const DEFAULT_PROFILE = path.resolve(".run", "tmp_fanqie_title_apply_profile");
const DEFAULT_RESULT = path.resolve(".run", "fanqie_title_apply_result.json");

function printHelp() {
  console.log(`Safely apply body-linked chapter titles to published Fanqie chapters.

Usage:
  node scripts/fanqie_apply_integrated_title_plan.mjs \
    --plan external/fanqie_snapshots/book_human_retitle_plan.json \
    --from 1 \
    --to 20 \
    --dry-run

Options:
  --plan <path>             Integrated plan containing chapter bodies and audit.finalTitle
  --from <n>                First chapter, default 1
  --to <n>                  Last chapter, default latest
  --seed-profile-dir <dir>  Logged-in seed profile
  --profile-dir <dir>       Temporary working profile
  --chrome-path <path>      Chrome or Edge executable
  --result <path>           JSON execution report
  --verify-seconds <n>      Preview verification timeout, default 180
  --visible                 Use a visible browser
  --dry-run                 Verify title and full body without submitting
  --continue-on-locked      Record Fanqie code -3029 and continue
  --help                    Show help
`);
}

function parseArgs(argv) {
  const args = {
    planPath: "",
    from: 1,
    to: Number.MAX_SAFE_INTEGER,
    seedProfileDir: DEFAULT_SEED_PROFILE,
    profileDir: DEFAULT_PROFILE,
    chromePath: "",
    resultPath: DEFAULT_RESULT,
    verifySeconds: 180,
    visible: false,
    dryRun: false,
    continueOnLocked: false,
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
      case "--plan":
        args.planPath = path.resolve(next());
        break;
      case "--from":
        args.from = Number(next());
        break;
      case "--to":
        args.to = Number(next());
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
      case "--result":
        args.resultPath = path.resolve(next());
        break;
      case "--verify-seconds":
        args.verifySeconds = Number(next());
        break;
      case "--visible":
        args.visible = true;
        break;
      case "--dry-run":
        args.dryRun = true;
        break;
      case "--continue-on-locked":
        args.continueOnLocked = true;
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
  if (!args.planPath) {
    throw new Error("--plan is required");
  }
  if (!fs.existsSync(args.planPath)) {
    throw new Error(`plan not found: ${args.planPath}`);
  }
  if (!Number.isInteger(args.from) || args.from <= 0) {
    throw new Error(`invalid --from: ${args.from}`);
  }
  if (!Number.isInteger(args.to) || args.to < args.from) {
    throw new Error(`invalid --to: ${args.to}`);
  }
  if (!Number.isInteger(args.verifySeconds) || args.verifySeconds < 0) {
    throw new Error(`invalid --verify-seconds: ${args.verifySeconds}`);
  }
}

function detectChromePath() {
  const candidates = [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    path.join(
      process.env.LOCALAPPDATA ?? "",
      "Google",
      "Chrome",
      "Application",
      "chrome.exe",
    ),
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ];
  return candidates.find((candidate) => candidate && fs.existsSync(candidate)) ?? "";
}

function copyProfileSeed(seedDir, targetDir) {
  if (!fs.existsSync(seedDir)) {
    throw new Error(`seed profile not found: ${seedDir}`);
  }
  const skippedNames = new Set([
    "lockfile",
    "SingletonCookie",
    "SingletonLock",
    "SingletonSocket",
  ]);
  const skippedFragments = [
    `${path.sep}Crashpad`,
    `${path.sep}BrowserMetrics`,
    `${path.sep}Code Cache`,
    `${path.sep}GPUCache`,
    `${path.sep}ShaderCache`,
    `${path.sep}GrShaderCache`,
    `${path.sep}DawnGraphiteCache`,
    `${path.sep}GraphiteDawnCache`,
  ];

  fs.rmSync(targetDir, { recursive: true, force: true });
  fs.mkdirSync(targetDir, { recursive: true });
  fs.cpSync(seedDir, targetDir, {
    recursive: true,
    force: true,
    filter: (source) => {
      if (skippedNames.has(path.basename(source))) {
        return false;
      }
      return !skippedFragments.some((fragment) => source.includes(fragment));
    },
  });
}

function compactText(value) {
  return String(value ?? "").replace(/\s+/g, "");
}

function normalizeTitle(value) {
  return String(value ?? "").replace(/\s+/g, " ").trim();
}

function validatePlanTitles(chapters) {
  const seen = new Map();
  const errors = [];
  for (const chapter of chapters) {
    const title = normalizeTitle(chapter.audit?.finalTitle);
    const length = Array.from(title).length;
    if (!title) {
      errors.push(`chapter ${chapter.chapterNumber}: empty title`);
      continue;
    }
    if (length > 10) {
      errors.push(`chapter ${chapter.chapterNumber}: title too long ${length}/10`);
    }
    const previous = seen.get(title);
    if (previous) {
      errors.push(
        `chapters ${previous} and ${chapter.chapterNumber}: duplicate title ${title}`,
      );
    } else {
      seen.set(title, chapter.chapterNumber);
    }
  }
  if (errors.length) {
    throw new Error(`invalid title plan:\n${errors.join("\n")}`);
  }
}

async function waitForEditor(page) {
  const serialInputs = page.locator("input.serial-input:visible");
  const title = page.locator("input.serial-editor-input-hint-area:visible").first();
  const editor = page.locator(".ProseMirror:visible").first();
  await serialInputs.first().waitFor({ state: "visible", timeout: 30000 });
  await title.waitFor({ state: "visible", timeout: 30000 });
  await editor.waitFor({ state: "visible", timeout: 30000 });
  return { serialInputs, title, editor };
}

async function readPopulatedEditor(controls, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let latest = {
    serialValues: [],
    title: "",
    body: "",
  };

  while (Date.now() < deadline) {
    const serialValues = [];
    for (let index = 0; index < (await controls.serialInputs.count()); index += 1) {
      serialValues.push(
        String(await controls.serialInputs.nth(index).inputValue()).trim(),
      );
    }
    const title = normalizeTitle(await controls.title.inputValue());
    const body = await controls.editor.innerText();
    latest = { serialValues, title, body };
    if (
      serialValues.some((value) => /^\d+$/.test(value)) &&
      title &&
      compactText(body)
    ) {
      return latest;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }

  throw new Error(
    `editor data did not populate: inputs=${JSON.stringify(latest.serialValues)}, ` +
      `title=${JSON.stringify(latest.title)}, bodyChars=${compactText(latest.body).length}`,
  );
}

async function reachPublishSettings(page) {
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    await page.waitForTimeout(700);
    const modals = page.locator(".arco-modal:visible");
    if (!(await modals.count())) {
      continue;
    }

    const modal = modals.last();
    const text = (await modal.innerText()).replace(/\s+/g, " ").trim();
    if (/发布设置/u.test(text)) {
      return modal;
    }
    if (/发布提示|错别字未修改|是否确定提交/u.test(text)) {
      await modal.locator("button.arco-btn-primary:visible").last().click();
      continue;
    }
    if (/请选择内容检测方式/u.test(text)) {
      const basic = modal
        .locator("button:visible")
        .filter({ hasText: /仅基础检测/u })
        .first();
      if (!(await basic.count())) {
        throw new Error(`basic detection option missing: ${text}`);
      }
      await basic.click();
      continue;
    }
    throw new Error(`unknown Fanqie modal: ${text.slice(0, 500)}`);
  }
  throw new Error("publish settings were not reached");
}

async function selectNoAi(settings) {
  const labels = settings.locator("label.arco-radio:visible");
  if ((await labels.count()) < 2) {
    return;
  }
  await labels.nth(1).click();
  const inputs = settings.locator("input[type=radio]");
  if ((await inputs.count()) >= 2 && !(await inputs.nth(1).isChecked())) {
    throw new Error("AI usage 'no' did not become selected");
  }
}

async function submitTitle(page) {
  const settings = await reachPublishSettings(page);
  await selectNoAi(settings);
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes("/api/author/publish_article/v0/"),
    { timeout: 60000 },
  );
  await settings.locator("button.arco-btn-primary:visible").last().click();
  const response = await responsePromise;
  const raw = await response.text();
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new Error(`invalid publish response: ${raw.slice(0, 1000)}`);
  }
  return {
    httpStatus: response.status(),
    payload,
  };
}

async function readPreview(page, previewUrl) {
  await page.goto(previewUrl, {
    waitUntil: "domcontentloaded",
    timeout: 90000,
  });
  await page.locator(".preview-content").waitFor({
    state: "visible",
    timeout: 30000,
  });
  return await page.evaluate(() => ({
    heading: String(
      document.querySelector(".preview-title")?.textContent ?? "",
    ).replace(/\s+/g, " ").trim(),
    body: Array.from(document.querySelectorAll(".preview-content p"))
      .map((paragraph) =>
        String(paragraph.innerText || paragraph.textContent || "").trim(),
      )
      .filter(Boolean)
      .join("\n\n"),
  }));
}

async function verifyPreview(context, chapter, targetTitle, verifySeconds) {
  const deadline = Date.now() + verifySeconds * 1000;
  let latest = null;
  while (Date.now() <= deadline) {
    const page = await context.newPage();
    try {
      latest = await readPreview(page, chapter.previewUrl);
    } finally {
      await page.close().catch(() => {});
    }
    if (
      latest.heading.includes(targetTitle) &&
      compactText(latest.body) === compactText(chapter.body)
    ) {
      return latest;
    }
    if (!verifySeconds) {
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 5000));
  }
  throw new Error(
    `preview verification failed for chapter ${chapter.chapterNumber}: ` +
      `${latest?.heading ?? "<missing>"}; ` +
      `body=${compactText(latest?.body).length}/${compactText(chapter.body).length}`,
  );
}

function writeResult(filePath, result) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(result, null, 2)}\n`, "utf8");
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    printHelp();
    return;
  }
  validateArgs(args);

  const plan = JSON.parse(fs.readFileSync(args.planPath, "utf8"));
  validatePlanTitles(plan.chapters ?? []);
  const selected = (plan.chapters ?? []).filter(
    (chapter) =>
      chapter.chapterNumber >= args.from &&
      chapter.chapterNumber <= args.to &&
      chapter.audit?.finalTitle &&
      chapter.chapterId &&
      chapter.previewUrl,
  );
  if (!selected.length) {
    throw new Error(`no chapters selected for ${args.from}-${args.to}`);
  }

  const chromePath = args.chromePath || detectChromePath();
  if (!chromePath) {
    throw new Error("Chrome was not found; use --chrome-path");
  }
  copyProfileSeed(args.seedProfileDir, args.profileDir);

  const context = await chromium.launchPersistentContext(args.profileDir, {
    executablePath: chromePath,
    headless: !args.visible,
    viewport: args.visible ? null : { width: 1440, height: 1200 },
    locale: "zh-CN",
    args: args.visible ? ["--start-maximized"] : [],
  });
  const result = {
    schemaVersion: 1,
    plan: args.planPath,
    startedAt: new Date().toISOString(),
    dryRun: args.dryRun,
    chapters: [],
  };

  try {
    for (const chapter of selected) {
      const targetTitle = normalizeTitle(chapter.audit.finalTitle);
      const editUrl =
        `https://fanqienovel.com/main/writer/${plan.bookId}` +
        `/publish/${chapter.chapterId}`;
      const page = await context.newPage();
      const record = {
        chapterNumber: chapter.chapterNumber,
        sourceTitle: chapter.chapterTitle,
        targetTitle,
        status: "started",
      };
      result.chapters.push(record);
      writeResult(args.resultPath, result);

      try {
        await page.goto(editUrl, {
          waitUntil: "domcontentloaded",
          timeout: 90000,
        });
        const controls = await waitForEditor(page);
        const populated = await readPopulatedEditor(controls);
        const serialValues = populated.serialValues;
        const pageNumber = Number(
          serialValues.find((value) => /^\d+$/.test(value)) ?? 0,
        );
        const currentTitle = populated.title;
        const currentBody = populated.body;

        if (pageNumber !== chapter.chapterNumber) {
          throw new Error(
            `chapter number mismatch: page=${pageNumber}, plan=${chapter.chapterNumber}, ` +
              `inputs=${JSON.stringify(serialValues)}`,
          );
        }
        if (
          currentTitle !== normalizeTitle(chapter.chapterTitle) &&
          currentTitle !== targetTitle
        ) {
          throw new Error(`unexpected current title: ${currentTitle}`);
        }
        if (compactText(currentBody) !== compactText(chapter.body)) {
          throw new Error(
            `body drift: ${compactText(currentBody).length}/` +
              `${compactText(chapter.body).length}`,
          );
        }

        record.currentTitle = currentTitle;
        record.bodyChars = compactText(currentBody).length;
        if (currentTitle === targetTitle) {
          record.status = "already_applied";
          continue;
        }
        if (args.dryRun) {
          record.status = "ready";
          continue;
        }

        await controls.title.fill(targetTitle);
        await page.waitForTimeout(1000);
        await page.locator("button.auto-editor-next").click();
        const submission = await submitTitle(page);
        record.httpStatus = submission.httpStatus;
        record.response = submission.payload;

        if (submission.payload?.code === -3029 && args.continueOnLocked) {
          record.status = "locked";
          continue;
        }
        if (submission.payload?.code !== 0) {
          throw new Error(
            `Fanqie rejected chapter ${chapter.chapterNumber}: ` +
              JSON.stringify(submission.payload),
          );
        }

        const verified = await verifyPreview(
          context,
          chapter,
          targetTitle,
          args.verifySeconds,
        );
        record.status = "applied";
        record.verifiedHeading = verified.heading;
      } catch (error) {
        record.status = "failed";
        record.error = String(error?.stack || error);
        writeResult(args.resultPath, result);
        throw error;
      } finally {
        await page.close().catch(() => {});
        writeResult(args.resultPath, result);
      }
    }
  } finally {
    result.finishedAt = new Date().toISOString();
    writeResult(args.resultPath, result);
    await context.close().catch(() => {});
  }

  const summary = result.chapters.reduce((counts, chapter) => {
    counts[chapter.status] = (counts[chapter.status] ?? 0) + 1;
    return counts;
  }, {});
  console.log(JSON.stringify(summary, null, 2));
}

main().catch((error) => {
  console.error(error?.stack || error);
  process.exitCode = 1;
});

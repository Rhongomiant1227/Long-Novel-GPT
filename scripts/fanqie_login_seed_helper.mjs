import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";
import process from "node:process";

import { chromium } from "playwright-core";

const DEFAULT_LOGIN_URL = "https://fanqienovel.com/main/writer/login";
const DEFAULT_CHROME_PATH = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

function parseArgs(argv) {
  const args = {
    loginUrl: DEFAULT_LOGIN_URL,
    chromePath: DEFAULT_CHROME_PATH,
    profileDir: path.resolve(".run", "fanqie_writer_seed_profile"),
    screenshotPath: path.resolve(".run", "fanqie_login_qr.png"),
    statusPath: path.resolve(".run", "fanqie_login_status.json"),
    timeoutSeconds: 900,
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
      case "--login-url":
        args.loginUrl = next();
        break;
      case "--chrome-path":
        args.chromePath = next();
        break;
      case "--profile-dir":
        args.profileDir = path.resolve(next());
        break;
      case "--screenshot-path":
        args.screenshotPath = path.resolve(next());
        break;
      case "--status-path":
        args.statusPath = path.resolve(next());
        break;
      case "--timeout-seconds":
        args.timeoutSeconds = Number(next());
        break;
      default:
        throw new Error(`unknown arg: ${arg}`);
    }
  }

  return args;
}

function now() {
  return new Date().toISOString();
}

async function ensureDirFor(filePath) {
  await fsp.mkdir(path.dirname(filePath), { recursive: true });
}

async function writeStatus(statusPath, payload) {
  await ensureDirFor(statusPath);
  await fsp.writeFile(
    statusPath,
    JSON.stringify(
      {
        updatedAt: now(),
        ...payload,
      },
      null,
      2,
    ),
    "utf8",
  );
}

async function isLoginPage(page) {
  const currentUrl = page.url();
  if (/\/main\/writer\/login(?:[/?#]|$)/.test(currentUrl)) {
    return true;
  }

  const bodyText = await page.evaluate(() => String(document.body?.innerText || ""))
    .catch(() => "");
  return /验证码登录|扫码登录|密码登录/.test(bodyText) && /登录\/注册|登录/.test(bodyText);
}

async function switchToQrLogin(page) {
  const candidates = [
    page.locator("text=/扫码登录/").first(),
    page.locator("div,span,button").filter({ hasText: /扫码登录/ }).first(),
  ];
  for (const locator of candidates) {
    try {
      if ((await locator.count()) > 0) {
        await locator.click({ timeout: 3000 });
        return;
      }
    } catch {}
  }
}

async function saveQrScreenshot(page, screenshotPath) {
  await ensureDirFor(screenshotPath);
  const qr = page.locator("img.slogin-qrcode-scan-page__content__code__img").first();
  if ((await qr.count()) > 0) {
    await qr.screenshot({ path: screenshotPath });
    return true;
  }
  await page.screenshot({ path: screenshotPath, fullPage: true });
  return false;
}

function log(message) {
  console.log(`${now()} | ${message}`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!fs.existsSync(args.chromePath)) {
    throw new Error(`chrome not found: ${args.chromePath}`);
  }

  await ensureDirFor(args.screenshotPath);
  await ensureDirFor(args.statusPath);
  await writeStatus(args.statusPath, {
    phase: "starting",
    loggedIn: false,
    profileDir: args.profileDir,
    screenshotPath: args.screenshotPath,
  });
  log(`starting visible login helper with profile=${args.profileDir}`);

  const context = await chromium.launchPersistentContext(args.profileDir, {
    executablePath: args.chromePath,
    headless: false,
    viewport: null,
    locale: "zh-CN",
    args: ["--start-maximized"],
  });

  let page = context.pages()[0] || await context.newPage();
  const deadline = Date.now() + args.timeoutSeconds * 1000;

  const openLoginPage = async (reason = "") => {
    if (page.isClosed()) {
      page = context.pages()[0] || await context.newPage();
    }
    if (reason) {
      log(reason);
    }
    await page.goto(args.loginUrl, { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
    await switchToQrLogin(page);
    await page.waitForTimeout(1500);
    const hasQr = await saveQrScreenshot(page, args.screenshotPath);
    await writeStatus(args.statusPath, {
      phase: "waiting_for_scan",
      loggedIn: false,
      profileDir: args.profileDir,
      screenshotPath: args.screenshotPath,
      qrVisible: hasQr,
      currentUrl: page.url(),
    });
  };

  try {
    log(`opening login page: ${args.loginUrl}`);
    await openLoginPage();
    log(`waiting for manual login, qrVisible=${fs.existsSync(args.screenshotPath) ? "yes" : "no"}`);

    while (Date.now() < deadline) {
      if (page.isClosed()) {
        await openLoginPage("visible login page was closed; reopening it");
        continue;
      }
      if (!(await isLoginPage(page))) {
        await writeStatus(args.statusPath, {
          phase: "logged_in",
          loggedIn: true,
          profileDir: args.profileDir,
          screenshotPath: args.screenshotPath,
          currentUrl: page.url(),
        });
        log("login detected, helper will exit");
        return;
      }

      await switchToQrLogin(page);
      await saveQrScreenshot(page, args.screenshotPath).catch(() => {});
      await page.waitForTimeout(3000).catch(async () => {
        if (!page.isClosed()) {
          throw new Error("login page became unavailable");
        }
      });
    }

    await writeStatus(args.statusPath, {
      phase: "timeout",
      loggedIn: false,
      profileDir: args.profileDir,
      screenshotPath: args.screenshotPath,
      currentUrl: page.url(),
    });
    log("login helper timed out");
    process.exitCode = 1;
  } finally {
    await context.close().catch(() => {});
  }
}

main().catch(async (error) => {
  const statusPath = path.resolve(".run", "fanqie_login_status.json");
  await writeStatus(statusPath, {
    phase: "error",
    loggedIn: false,
    error: String(error?.stack || error),
  }).catch(() => {});
  console.error(error);
  process.exit(1);
});

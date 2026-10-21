import fs from "node:fs/promises";
import path from "node:path";

import { chromium } from "playwright-core";

const 默认作者后台 = "https://fanqienovel.com/main/writer/?enter_from=author_zone";
const 默认Chrome路径 = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

function 解析参数(argv) {
  const args = {
    startUrl: 默认作者后台,
    chromePath: 默认Chrome路径,
    seedProfileDir: path.resolve(".run", "fanqie_writer_seed_profile"),
    profileDir: path.resolve(".run", "fanqie_create_book_inspect_profile"),
    outputDir: path.resolve(".run", "fanqie_create_book_inspect"),
    headless: true,
    keepOpen: false,
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
      case "--start-url":
        args.startUrl = next();
        break;
      case "--chrome-path":
        args.chromePath = next();
        break;
      case "--seed-profile-dir":
        args.seedProfileDir = path.resolve(next());
        break;
      case "--profile-dir":
        args.profileDir = path.resolve(next());
        break;
      case "--output-dir":
        args.outputDir = path.resolve(next());
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
      default:
        throw new Error(`未知参数: ${arg}`);
    }
  }

  return args;
}

async function 重置目录(dir) {
  await fs.rm(dir, { recursive: true, force: true });
  await fs.mkdir(dir, { recursive: true });
}

async function 复制目录(src, dest) {
  await fs.cp(src, dest, { recursive: true, force: true });
}

async function 写文本(filePath, content) {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, content, "utf8");
}

async function 抓页面(page, outputDir, tag) {
  const body = await page.locator("body").innerText().catch(() => "");
  const html = await page.content().catch(() => "");
  const meta = {
    tag,
    url: page.url(),
    title: await page.title().catch(() => ""),
    bodyPreview: body.slice(0, 2000),
  };
  await 写文本(path.join(outputDir, `${tag}.txt`), body);
  await 写文本(path.join(outputDir, `${tag}.html`), html);
  await 写文本(path.join(outputDir, `${tag}.json`), JSON.stringify(meta, null, 2));
  await page.screenshot({
    path: path.join(outputDir, `${tag}.png`),
    fullPage: true,
  }).catch(() => {});
  return { body, meta };
}

async function 抓标签弹层(page, outputDir, tag) {
  await page.waitForTimeout(1000);
  const popup = await page.evaluate(() => {
    const isVisible = (el) => {
      if (!(el instanceof HTMLElement)) {
        return false;
      }
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.visibility !== "hidden"
        && style.display !== "none"
        && rect.width > 0
        && rect.height > 0;
    };

    const nodes = Array.from(document.querySelectorAll("body *"))
      .filter((node) => isVisible(node))
      .map((node) => ({
        tag: node.tagName,
        className: node.className || "",
        text: String(node.textContent || "").replace(/\s+/g, " ").trim(),
      }))
      .filter((item) => item.text && item.text.length <= 50);

    const options = nodes
      .filter((item) => /option|select|tag|label|item|popup|trigger/i.test(item.className))
      .map((item) => item.text);

    return {
      nodes,
      options,
    };
  }).catch(() => ({ nodes: [], options: [] }));

  await 写文本(path.join(outputDir, `${tag}.json`), JSON.stringify(popup, null, 2));
  await page.screenshot({
    path: path.join(outputDir, `${tag}.png`),
    fullPage: true,
  }).catch(() => {});

  console.log(JSON.stringify({
    phase: tag,
    options: popup.options.slice(0, 200),
  }, null, 2));
}

async function 是否登录页(page) {
  const url = page.url();
  if (/\/main\/writer\/login(?:[/?#]|$)/.test(url)) {
    return true;
  }
  const text = await page.locator("body").innerText().catch(() => "");
  return /验证码登录|扫码登录|密码登录/.test(text) && /登录\/注册|登录/.test(text);
}

async function 列出可见按钮文本(page) {
  return page.evaluate(() => {
    const isVisible = (el) => {
      if (!(el instanceof HTMLElement)) {
        return false;
      }
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.visibility !== "hidden"
        && style.display !== "none"
        && rect.width > 0
        && rect.height > 0;
    };
    return Array.from(document.querySelectorAll("button, a, span, div"))
      .filter((el) => isVisible(el))
      .map((el) => (el.textContent || "").replace(/\s+/g, " ").trim())
      .filter((text) => text && text.length <= 40)
      .slice(0, 300);
  }).catch(() => []);
}

async function main() {
  const args = 解析参数(process.argv.slice(2));
  await 重置目录(args.outputDir);
  await 重置目录(args.profileDir);
  await 复制目录(args.seedProfileDir, args.profileDir);

  const context = await chromium.launchPersistentContext(args.profileDir, {
    executablePath: args.chromePath,
    headless: args.headless,
    viewport: { width: 1440, height: 1400 },
    locale: "zh-CN",
    args: ["--window-size=1440,1400"],
  });

  const page = context.pages()[0] || await context.newPage();

  try {
    await page.goto(args.startUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {});
    const home = await 抓页面(page, args.outputDir, "home");
    const visibleTexts = await 列出可见按钮文本(page);
    await 写文本(
      path.join(args.outputDir, "home_visible_texts.json"),
      JSON.stringify(visibleTexts, null, 2),
    );

    const loggedIn = !(await 是否登录页(page));
    console.log(JSON.stringify({
      phase: "home",
      url: home.meta.url,
      title: home.meta.title,
      loggedIn,
      visibleTexts: visibleTexts.slice(0, 60),
    }, null, 2));

    if (!loggedIn) {
      console.log("NOT_LOGGED_IN");
      return;
    }

    const createTrigger = page.getByText("创建新书", { exact: true }).first();
    await createTrigger.click({ timeout: 20000 });
    await page.waitForTimeout(1500);
    const menu = await 抓页面(page, args.outputDir, "after_create_click");
    const menuVisibleTexts = await 列出可见按钮文本(page);
    await 写文本(
      path.join(args.outputDir, "after_create_click_visible_texts.json"),
      JSON.stringify(menuVisibleTexts, null, 2),
    );
    console.log(JSON.stringify({
      phase: "after_create_click",
      url: menu.meta.url,
      title: menu.meta.title,
      visibleTexts: menuVisibleTexts.slice(0, 120),
    }, null, 2));

    const createBookEntry = page.getByText("创建书本", { exact: true }).first();
    await createBookEntry.click({ timeout: 20000 });
    await page.waitForLoadState("domcontentloaded", { timeout: 30000 }).catch(() => {});
    await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(2000);

    const createBook = await 抓页面(page, args.outputDir, "create_book_page");
    const createBookVisibleTexts = await 列出可见按钮文本(page);
    await 写文本(
      path.join(args.outputDir, "create_book_page_visible_texts.json"),
      JSON.stringify(createBookVisibleTexts, null, 2),
    );
    console.log(JSON.stringify({
      phase: "create_book_page",
      url: createBook.meta.url,
      title: createBook.meta.title,
      visibleTexts: createBookVisibleTexts.slice(0, 150),
    }, null, 2));

    const formFields = await page.evaluate(() => {
      const isVisible = (el) => {
        if (!(el instanceof HTMLElement)) {
          return false;
        }
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.visibility !== "hidden"
          && style.display !== "none"
          && rect.width > 0
          && rect.height > 0;
      };

      const normalize = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
      const fieldNodes = Array.from(document.querySelectorAll("input, textarea, [role='combobox'], .arco-select-view, .byte-select-view"));
      return fieldNodes
        .filter((node) => isVisible(node))
        .map((node) => {
          const container = node.closest("label, .arco-form-item, .form-item, .base-info-form-item, .create-book-form-item") || node.parentElement;
          return {
            tag: node.tagName,
            type: node.getAttribute("type") || "",
            name: node.getAttribute("name") || "",
            placeholder: node.getAttribute("placeholder") || "",
            text: normalize(node.textContent || ""),
            label: normalize(container?.textContent || "").slice(0, 200),
            className: node.className || "",
          };
        });
    }).catch(() => []);
    await 写文本(
      path.join(args.outputDir, "long_book_form_fields.json"),
      JSON.stringify(formFields, null, 2),
    );

    const femaleRadio = page.locator("label.arco-radio", { hasText: "女频" }).first();
    await femaleRadio.click({ timeout: 20000 });
    await page.waitForTimeout(800);
    await page.locator(".select-view").first().click({ timeout: 20000 });
    await 抓标签弹层(page, args.outputDir, "female_tag_popup");
    await page.keyboard.press("Escape").catch(() => {});

    const maleRadio = page.locator("label.arco-radio", { hasText: "男频" }).first();
    await maleRadio.click({ timeout: 20000 });
    await page.waitForTimeout(800);
    await page.locator(".select-view").first().click({ timeout: 20000 });
    await 抓标签弹层(page, args.outputDir, "male_tag_popup");
    await page.keyboard.press("Escape").catch(() => {});

    await page.getByText("选择封面", { exact: true }).first().click({ timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(1000);
    await 抓页面(page, args.outputDir, "cover_picker");

    console.log(JSON.stringify({
      phase: "long_book_form",
      url: createBook.meta.url,
      title: createBook.meta.title,
      fields: formFields,
      visibleTexts: createBookVisibleTexts.slice(0, 150),
    }, null, 2));
  } finally {
    if (!args.keepOpen) {
      await context.close().catch(() => {});
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

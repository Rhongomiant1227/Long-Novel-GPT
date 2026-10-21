import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import {
  normalizeTitle,
  parseChapterFile,
  titleQualityIssues,
} from "./fanqie_title_integrity.mjs";

function parseArgs(argv) {
  const args = {
    planPath: "",
    projectDir: "",
    after: 0,
    dryRun: false,
    resultPath: path.resolve(".run", "fanqie_apply_local_title_plan.json"),
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
      case "--project-dir":
        args.projectDir = path.resolve(next());
        break;
      case "--after":
        args.after = Number(next());
        break;
      case "--dry-run":
        args.dryRun = true;
        break;
      case "--result":
        args.resultPath = path.resolve(next());
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  return args;
}

function rawBodyHash(body) {
  return crypto.createHash("sha256").update(String(body ?? ""), "utf8").digest("hex");
}

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function firstNonEmptyLineIndex(lines) {
  return lines.findIndex((line) => normalizeTitle(line));
}

function replaceHeading(text, chapterNumber, currentTitles, targetTitle, filePath) {
  const lines = String(text ?? "").replace(/\r\n?/gu, "\n").split("\n");
  const index = firstNonEmptyLineIndex(lines);
  if (index < 0) {
    throw new Error(`empty chapter source: ${filePath}`);
  }
  const parsed = parseChapterFile(lines.slice(index).join("\n"), filePath);
  if (parsed.number !== chapterNumber) {
    throw new Error(`chapter number mismatch in ${filePath}: ${parsed.number}/${chapterNumber}`);
  }
  const allowedTitles = new Set(
    [...currentTitles, targetTitle].map(normalizeTitle).filter(Boolean),
  );
  if (!allowedTitles.has(parsed.title)) {
    throw new Error(`unexpected current title in ${filePath}: ${parsed.title}`);
  }
  lines[index] = `第${chapterNumber}章 ${targetTitle}`;
  return `${lines.join("\n").trimEnd()}\n`;
}

function backupFile(filePath, backupRoot, projectDir) {
  if (!fs.existsSync(filePath)) {
    return;
  }
  const rawRelative = path.relative(projectDir, filePath);
  const relative =
    rawRelative.startsWith(`..${path.sep}`) || path.isAbsolute(rawRelative)
      ? path.join("_external", path.basename(filePath))
      : rawRelative;
  const target = path.join(backupRoot, relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.copyFileSync(filePath, target);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.planPath || !args.projectDir) {
    throw new Error("--plan and --project-dir are required");
  }
  if (!Number.isInteger(args.after) || args.after < 0) {
    throw new Error(`invalid --after: ${args.after}`);
  }

  const plan = JSON.parse(fs.readFileSync(args.planPath, "utf8"));
  const statePath = path.join(args.projectDir, "state.json");
  const chaptersDir = path.join(args.projectDir, "manuscript", "chapters_txt");
  const manifestPath = path.join(chaptersDir, "chapters_manifest.json");
  const fullNovelPath = path.join(args.projectDir, "manuscript", "full_novel.txt");
  const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const records = [...(state.completed_chapters ?? []), ...(state.pending_chapters ?? [])];
  const stateByNumber = new Map(records.map((item) => [Number(item.chapter_number), item]));
  const manifestByNumber = new Map(manifest.map((item) => [Number(item.output_number), item]));
  const planByNumber = new Map((plan.chapters ?? []).map((chapter) => [chapter.chapterNumber, chapter]));
  const allFiles = fs.readdirSync(chaptersDir)
    .filter((name) => /^ch_\d{4}\.txt$/iu.test(name))
    .sort((left, right) => left.localeCompare(right, "en"));
  const current = new Map();
  for (const name of allFiles) {
    const number = Number(name.match(/\d+/u)?.[0] ?? 0);
    const filePath = path.join(chaptersDir, name);
    current.set(number, {
      filePath,
      ...parseChapterFile(fs.readFileSync(filePath, "utf8"), filePath),
    });
  }

  const targets = [];
  for (const [number, chapter] of planByNumber) {
    if (number <= args.after || !chapter.audit?.finalTitle) {
      continue;
    }
    const local = current.get(number);
    if (!local) {
      throw new Error(`missing local chapter ${number}`);
    }
    const targetTitle = normalizeTitle(chapter.audit.finalTitle);
    const issues = titleQualityIssues(targetTitle, { strictReaderFacing: true });
    if (issues.length) {
      throw new Error(`invalid target title for chapter ${number}: ${issues.join("; ")}`);
    }
    if (local.title !== normalizeTitle(chapter.chapterTitle) && local.title !== targetTitle) {
      throw new Error(`source title drift at chapter ${number}: ${local.title}`);
    }
    if (rawBodyHash(local.body) !== chapter.bodySha256) {
      throw new Error(`body drift at chapter ${number}`);
    }
    targets.push({
      number,
      planSourceTitle: normalizeTitle(chapter.chapterTitle),
      sourceTitle: local.title,
      targetTitle,
      local,
      stateRecord: stateByNumber.get(number),
      manifestRecord: manifestByNumber.get(number),
    });
  }

  const finalTitles = new Map();
  const seen = new Map();
  for (const [number, local] of current) {
    const target = targets.find((item) => item.number === number);
    const title = target?.targetTitle ?? local.title;
    finalTitles.set(number, title);
    const previous = seen.get(title);
    if (previous) {
      throw new Error(`duplicate final title: chapters ${previous} and ${number}: ${title}`);
    }
    seen.set(title, number);
  }

  const result = {
    schemaVersion: 1,
    plan: args.planPath,
    projectDir: args.projectDir,
    after: args.after,
    dryRun: args.dryRun,
    startedAt: new Date().toISOString(),
    chapters: targets.map((item) => ({
      chapterNumber: item.number,
      sourceTitle: item.sourceTitle,
      targetTitle: item.targetTitle,
      bodySha256: item.local.bodySha256,
      status: item.sourceTitle === item.targetTitle ? "unchanged" : "ready",
    })),
  };
  writeJson(args.resultPath, result);
  if (args.dryRun) {
    result.finishedAt = new Date().toISOString();
    writeJson(args.resultPath, result);
    console.log(JSON.stringify({ ready: result.chapters.filter((item) => item.status === "ready").length }, null, 2));
    return;
  }

  const stamp = new Date().toISOString().replace(/[:.]/gu, "-");
  const backupRoot = path.resolve(".run", "title_plan_backups", `${path.basename(args.projectDir)}_${stamp}`);
  for (const filePath of [statePath, manifestPath, fullNovelPath]) {
    backupFile(filePath, backupRoot, args.projectDir);
  }

  for (const target of targets) {
    if (target.sourceTitle !== target.targetTitle) {
      backupFile(target.local.filePath, backupRoot, args.projectDir);
      const chapterText = fs.readFileSync(target.local.filePath, "utf8");
      fs.writeFileSync(
        target.local.filePath,
        replaceHeading(
          chapterText,
          target.number,
          [target.sourceTitle, target.planSourceTitle],
          target.targetTitle,
          target.local.filePath,
        ),
        "utf8",
      );
    }
    if (target.stateRecord) {
      target.stateRecord.title = target.targetTitle;
      const shouldSyncSources =
        target.sourceTitle !== target.targetTitle ||
        target.planSourceTitle !== target.targetTitle;
      if (shouldSyncSources) {
        const draftPath = String(target.stateRecord.draft_file ?? "").trim();
        const sourcePaths = new Set(
          ["draft_file", "outline_file"]
            .map((key) => String(target.stateRecord[key] ?? "").trim())
            .filter(Boolean),
        );
        if (draftPath) {
          sourcePaths.add(path.join(path.dirname(draftPath), "outline.md"));
        }
        for (const sourcePath of sourcePaths) {
          if (!sourcePath || !fs.existsSync(sourcePath)) {
            continue;
          }
          const sourceText = fs.readFileSync(sourcePath, "utf8");
          const firstLine = sourceText.replace(/\r\n?/gu, "\n").split("\n").find((line) => normalizeTitle(line));
          if (!firstLine || !/^第\s*\d+\s*章/u.test(normalizeTitle(firstLine))) {
            continue;
          }
          backupFile(sourcePath, backupRoot, args.projectDir);
          fs.writeFileSync(
            sourcePath,
            replaceHeading(
              sourceText,
              target.number,
              [target.sourceTitle, target.planSourceTitle],
              target.targetTitle,
              sourcePath,
            ),
            "utf8",
          );
        }
      }
    }
    if (target.manifestRecord) {
      target.manifestRecord.heading_line = `第${target.number}章 ${target.targetTitle}`;
    }
    const record = result.chapters.find((item) => item.chapterNumber === target.number);
    record.status = target.sourceTitle === target.targetTitle ? "synced" : "applied";
  }

  state.updated_at = new Date().toISOString().replace("T", " ").slice(0, 19);
  writeJson(statePath, state);
  writeJson(manifestPath, manifest);
  const fullNovel = allFiles.map((name) =>
    fs.readFileSync(path.join(chaptersDir, name), "utf8").trim(),
  ).join("\n\n");
  fs.writeFileSync(fullNovelPath, `${fullNovel}\n`, "utf8");
  result.backupRoot = backupRoot;
  result.finishedAt = new Date().toISOString();
  writeJson(args.resultPath, result);
  console.log(JSON.stringify({
    applied: result.chapters.filter((item) => item.status === "applied").length,
    unchanged: result.chapters.filter((item) => item.status === "unchanged").length,
    synced: result.chapters.filter((item) => item.status === "synced").length,
    backupRoot,
  }, null, 2));
}

try {
  main();
} catch (error) {
  console.error(error?.stack || String(error));
  process.exitCode = 1;
}

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

import { parseChapterFile } from "./fanqie_title_integrity.mjs";

function parseArgs(argv) {
  const args = {
    project: "",
    bookTitle: "",
    from: 1,
    to: Number.MAX_SAFE_INTEGER,
    outputPath: "",
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
      case "--project":
        args.project = next().trim();
        break;
      case "--book-title":
        args.bookTitle = next().trim();
        break;
      case "--from":
        args.from = Number(next());
        break;
      case "--to":
        args.to = Number(next());
        break;
      case "--output":
        args.outputPath = path.resolve(next());
        break;
      default:
        throw new Error(`unknown argument: ${arg}`);
    }
  }
  return args;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.project || !args.bookTitle || !args.outputPath) {
    throw new Error("--project, --book-title and --output are required");
  }
  if (!Number.isInteger(args.from) || args.from <= 0) {
    throw new Error(`invalid --from: ${args.from}`);
  }
  if (!Number.isInteger(args.to) || args.to < args.from) {
    throw new Error(`invalid --to: ${args.to}`);
  }

  const projectDir = path.resolve("auto_projects", args.project);
  const chaptersDir = path.join(projectDir, "manuscript", "chapters_txt");
  const names = fs.readdirSync(chaptersDir)
    .filter((name) => /^ch_\d{4}\.txt$/iu.test(name))
    .sort((left, right) => left.localeCompare(right, "en"));
  const chapters = [];
  for (const name of names) {
    const fileNumber = Number(name.match(/\d+/u)?.[0] ?? 0);
    if (fileNumber < args.from || fileNumber > args.to) {
      continue;
    }
    const filePath = path.join(chaptersDir, name);
    const parsed = parseChapterFile(fs.readFileSync(filePath, "utf8"), filePath);
    if (parsed.number !== fileNumber) {
      throw new Error(`chapter number mismatch: file=${fileNumber}, heading=${parsed.number}`);
    }
    chapters.push({
      chapterNumber: parsed.number,
      chapterTitle: parsed.title,
      fullHeading: parsed.heading,
      body: parsed.body,
      bodyChars: parsed.bodyChars,
      sourceFile: filePath,
    });
  }
  if (!chapters.length) {
    throw new Error(`no chapters matched ${args.from}-${args.to}`);
  }
  const payload = {
    schemaVersion: 1,
    source: "local_pending",
    project: args.project,
    bookTitle: args.bookTitle,
    projectDir,
    chaptersDir,
    requestedRange: { from: args.from, to: args.to },
    generatedAt: new Date().toISOString(),
    chapters,
  };
  fs.mkdirSync(path.dirname(args.outputPath), { recursive: true });
  fs.writeFileSync(args.outputPath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  console.log(args.outputPath);
}

try {
  main();
} catch (error) {
  console.error(error?.stack || String(error));
  process.exitCode = 1;
}

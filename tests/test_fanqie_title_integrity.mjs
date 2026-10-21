import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditLocalProject,
  bodySha256,
  parseChapterFile,
  titleQualityIssues,
} from "../scripts/fanqie_title_integrity.mjs";

test("parseChapterFile keeps title and body bound", () => {
  const parsed = parseChapterFile(
    "\uFEFF第12章 她终于开口\n\n第一段。\n\n第二段。\n",
    "ch_0012.txt",
  );
  assert.equal(parsed.number, 12);
  assert.equal(parsed.title, "她终于开口");
  assert.equal(parsed.body, "第一段。\n\n第二段。");
  assert.equal(parsed.bodySha256, bodySha256(parsed.body));
});

test("titleQualityIssues rejects upload-risk titles", () => {
  assert.deepEqual(titleQualityIssues("她终于开口"), []);
  assert.match(titleQualityIssues("尾声").join(";"), /少于4字|写作提纲词/);
  assert.match(titleQualityIssues("样本归档").join(";"), /后台术语过重/);
  assert.match(titleQualityIssues("ä¸å¾—ä»£ç").join(";"), /乱码/);
  assert.match(titleQualityIssues("这是一句超过十个字的完整标题").join(";"), /超过10字/);
});

test("auditLocalProject catches duplicate titles and mapping drift", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "fanqie-title-audit-"));
  const projectDir = path.join(root, "book");
  const chaptersDir = path.join(projectDir, "manuscript", "chapters_txt");
  fs.mkdirSync(chaptersDir, { recursive: true });
  fs.writeFileSync(
    path.join(projectDir, "state.json"),
    JSON.stringify({
      completed_chapters: [
        { chapter_number: 1, title: "她终于开口" },
        { chapter_number: 2, title: "不是同一个标题" },
      ],
    }),
    "utf8",
  );
  fs.writeFileSync(
    path.join(chaptersDir, "chapters_manifest.json"),
    JSON.stringify([
      { output_number: 1, heading_line: "第1章 她终于开口" },
      { output_number: 2, heading_line: "第2章 她终于开口" },
    ]),
    "utf8",
  );
  fs.writeFileSync(
    path.join(chaptersDir, "ch_0001.txt"),
    `第1章 她终于开口\n\n${"正文".repeat(600)}`,
    "utf8",
  );
  fs.writeFileSync(
    path.join(chaptersDir, "ch_0002.txt"),
    `第2章 她终于开口\n\n${"正文".repeat(600)}`,
    "utf8",
  );

  try {
    const report = auditLocalProject({
      chaptersDir,
      selectedNumbers: [1, 2],
      strictReaderFacing: true,
    });
    assert.ok(report.errors.some((error) => error.includes("标题重复")));
    assert.ok(report.errors.some((error) => error.includes("state.json 标题不一致")));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

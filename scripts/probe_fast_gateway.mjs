import fs from "node:fs";
import path from "node:path";

const baseUrl = (process.env.FAST_BASE_URL || "https://fast.sbbbbbbbbb.xyz/v1").replace(/\/$/, "");
const authPath = process.env.FAST_AUTH_FILE
  || path.join(process.env.USERPROFILE || "", ".codex", "fast_sbbbb_auth.json");
const timeoutMs = Number(process.env.FAST_PROBE_TIMEOUT_MS || 90_000);

const auth = JSON.parse(fs.readFileSync(authPath, "utf8"));
const apiKey = String(auth.OPENAI_API_KEY || auth.api_key || auth.key || "").trim();
if (!apiKey) {
  throw new Error(`No API key in ${authPath}`);
}

const prompt = [
  "写一段约一百五十字的中文都市悬疑正文。",
  "凌晨两点十七分，封闭三年的旧地铁站只有一盏信号灯亮起。",
  "主角许沉是来拆除设备的通信工程师；他没有门禁卡，只带拆线钳和没电的旧对讲机。",
  "让他通过现场物件得到一个可验证的新线索，结尾必须是完整动作或完整对白。",
  "只输出正文，不要标题、解释、总结或英文。",
].join("");

async function probe(model) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const startedAt = Date.now();
  try {
    const response = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      signal: controller.signal,
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json; charset=utf-8",
        Accept: "application/json",
        "Accept-Encoding": "identity",
        Connection: "close",
        "User-Agent": "Long-Novel-GPT/fast-gateway-probe",
      },
      body: JSON.stringify({
        model,
        messages: [
          {
            role: "system",
            content: "你是中文商业小说作者。严格遵守用户事实，只输出可发布正文。",
          },
          { role: "user", content: prompt },
        ],
        max_tokens: 360,
        temperature: 0.55,
        stream: false,
      }),
    });
    const raw = await response.text();
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = { raw };
    }
    const text = String(payload?.choices?.[0]?.message?.content || "");
    return {
      model,
      status: response.status,
      elapsedMs: Date.now() - startedAt,
      chars: text.length,
      finishReason: payload?.choices?.[0]?.finish_reason || "",
      text,
      error: payload?.error || null,
    };
  } catch (error) {
    return {
      model,
      status: 0,
      elapsedMs: Date.now() - startedAt,
      chars: 0,
      finishReason: "",
      text: "",
      error: String(error),
    };
  } finally {
    clearTimeout(timeout);
  }
}

const results = [];
for (const model of ["grok-4.5", "grok-4.6"]) {
  results.push(await probe(model));
}
process.stdout.write(`${JSON.stringify({ baseUrl, results }, null, 2)}\n`);

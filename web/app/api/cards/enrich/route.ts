import { env } from "cloudflare:workers";
import { AuthError, requireUser } from "@/lib/auth";
import { cleanArray } from "@/lib/card-data";
import { jsonError } from "@/lib/database";

const SYSTEM = `You create one English-to-Russian vocabulary note from a word in a full sentence.
Return JSON only. translationsRu: 2-5 short Russian translations matching this exact context.
replacementRu: one Russian replacement in the right grammatical form, without punctuation.
alternativesEn: 2-6 simpler English near-synonyms, never antonyms and never the target.`;

export async function POST(request: Request) {
  try {
    await requireUser(request);
    const body = (await request.json()) as { target?: string; sentence?: string };
    const target = (body.target ?? "").trim();
    const sentence = (body.sentence ?? "").trim();
    if (!target || !sentence) return jsonError("Не хватает слова или предложения.");
    const runtime = env as unknown as { OPENROUTER_API_KEY?: string; OPENROUTER_MODEL?: string };
    if (!runtime.OPENROUTER_API_KEY) {
      return jsonError("Автоперевод пока не настроен. Введите перевод вручную.", 503);
    }
    const response = await fetch("https://openrouter.ai/api/v1/chat/completions", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${runtime.OPENROUTER_API_KEY}`,
        "Content-Type": "application/json",
        "HTTP-Referer": new URL(request.url).origin,
        "X-Title": "Paperdeck",
      },
      body: JSON.stringify({
        model: runtime.OPENROUTER_MODEL ?? "google/gemma-4-26b-a4b-it",
        messages: [
          { role: "system", content: SYSTEM },
          { role: "user", content: JSON.stringify({ target, sentence }) },
        ],
        temperature: 0.2,
        response_format: {
          type: "json_schema",
          json_schema: {
            name: "vocabulary_note",
            strict: true,
            schema: {
              type: "object",
              additionalProperties: false,
              required: ["translationsRu", "replacementRu", "alternativesEn"],
              properties: {
                translationsRu: { type: "array", minItems: 2, maxItems: 5, items: { type: "string" } },
                replacementRu: { type: "string" },
                alternativesEn: { type: "array", minItems: 2, maxItems: 6, items: { type: "string" } },
              },
            },
          },
        },
      }),
    });
    if (!response.ok) return jsonError("Автоперевод временно недоступен.", 502);
    const payload = await response.json() as { choices?: Array<{ message?: { content?: string } }> };
    const raw = payload.choices?.[0]?.message?.content;
    if (!raw) return jsonError("Автоперевод вернул пустой ответ.", 502);
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const translationsRu = cleanArray(parsed.translationsRu, 5);
    const alternativesEn = cleanArray(parsed.alternativesEn, 6);
    const replacementRu = String(parsed.replacementRu ?? "").trim();
    if (!translationsRu.length || !replacementRu) return jsonError("Автоперевод вернул неполный ответ.", 502);
    return Response.json({ translationsRu, alternativesEn, replacementRu });
  } catch (error) {
    if (error instanceof AuthError) return jsonError("Нужен вход.", 401);
    return jsonError("Автоперевод временно недоступен.", 500);
  }
}

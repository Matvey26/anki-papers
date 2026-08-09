import { AuthError, requireUser } from "@/lib/auth";
import { cleanArray, mapCard, normalizeTarget } from "@/lib/card-data";
import { database, isoNow, jsonError } from "@/lib/database";

const CARD_SELECT = `SELECT selections.id, selections.document_id,
  documents.name AS document_name, selections.target, selections.sentence,
  selections.page, selections.translations_ru, selections.replacement_ru,
  selections.alternatives_en, selections.csv_exported_at,
  selections.apkg_exported_at, selections.created_at
  FROM selections JOIN documents ON documents.id = selections.document_id`;

export async function GET(request: Request) {
  try {
    const user = await requireUser(request);
    const channel = new URL(request.url).searchParams.get("channel");
    const pending = channel === "csv"
      ? "AND selections.csv_exported_at IS NULL"
      : channel === "apkg"
        ? "AND selections.apkg_exported_at IS NULL"
        : "";
    const result = await database()
      .prepare(`${CARD_SELECT} WHERE selections.user_id = ? ${pending}
        ORDER BY selections.created_at ASC`)
      .bind(user.id)
      .all<Record<string, unknown>>();
    const rows = result.results as Array<Record<string, unknown>>;
    return Response.json({ cards: rows.map((row) => mapCard(row as never)) });
  } catch (error) {
    if (error instanceof AuthError) return jsonError("Нужен вход.", 401);
    return jsonError("Не удалось получить карточки.", 500);
  }
}

export async function POST(request: Request) {
  try {
    const user = await requireUser(request);
    const body = (await request.json()) as {
      documentId?: string;
      target?: string;
      sentence?: string;
      page?: number;
      translationsRu?: unknown;
      replacementRu?: string;
      alternativesEn?: unknown;
    };
    const target = (body.target ?? "").trim();
    const normalized = normalizeTarget(target);
    const sentence = (body.sentence ?? "").trim();
    const page = Math.max(1, Math.floor(Number(body.page) || 1));
    const translations = cleanArray(body.translationsRu, 5);
    const alternatives = cleanArray(body.alternativesEn, 6);
    const replacement = (body.replacementRu ?? "").trim();
    if (!normalized || !sentence || !body.documentId) return jsonError("Не хватает слова или контекста.");
    if (!translations.length) return jsonError("Добавьте хотя бы один перевод.");
    const document = await database()
      .prepare("SELECT id FROM documents WHERE id = ? AND user_id = ?")
      .bind(body.documentId, user.id)
      .first();
    if (!document) return jsonError("Статья не найдена.", 404);
    const id = crypto.randomUUID();
    await database()
      .prepare(`INSERT INTO selections
        (id, user_id, document_id, target, target_normalized, sentence, page,
          translations_ru, replacement_ru, alternatives_en, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
      .bind(
        id, user.id, body.documentId, target, normalized, sentence, page,
        JSON.stringify(translations), replacement || translations[0],
        JSON.stringify(alternatives), isoNow(),
      )
      .run();
    const row = await database()
      .prepare(`${CARD_SELECT} WHERE selections.id = ? AND selections.user_id = ?`)
      .bind(id, user.id)
      .first<Record<string, unknown>>();
    return Response.json({ card: mapCard(row as never) }, { status: 201 });
  } catch (error) {
    if (error instanceof AuthError) return jsonError("Нужен вход.", 401);
    const message = error instanceof Error ? error.message : "Не удалось сохранить карточку.";
    if (/UNIQUE|constraint/i.test(message)) return jsonError("Это слово уже есть в вашей коллекции.", 409);
    return jsonError(message, 500);
  }
}

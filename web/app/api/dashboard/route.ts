import { AuthError, requireUser } from "@/lib/auth";
import { mapCard } from "@/lib/card-data";
import { database, jsonError } from "@/lib/database";
import type { DashboardData, DeckRecord, DocumentRecord } from "@/lib/types";

export async function GET(request: Request) {
  try {
    const user = await requireUser(request);
    const db = database();
    const [documentResult, deckResult, cardResult] = await Promise.all([
      db.prepare(`SELECT id, name, size, created_at AS createdAt FROM documents
        WHERE user_id = ? ORDER BY created_at DESC`).bind(user.id).all<DocumentRecord>(),
      db.prepare(`SELECT id, name, size, created_at AS createdAt FROM decks
        WHERE user_id = ? ORDER BY created_at DESC`).bind(user.id).all<DeckRecord>(),
      db.prepare(`SELECT selections.id, selections.document_id, documents.name AS document_name,
          selections.target, selections.sentence, selections.page, selections.translations_ru,
          selections.replacement_ru, selections.alternatives_en, selections.csv_exported_at,
          selections.apkg_exported_at, selections.created_at
        FROM selections JOIN documents ON documents.id = selections.document_id
        WHERE selections.user_id = ? ORDER BY selections.created_at DESC`)
        .bind(user.id).all<Record<string, unknown>>(),
    ]);
    const cards = (cardResult.results as unknown[]).map((row) => mapCard(row as never));
    const data: DashboardData = {
      user,
      documents: documentResult.results,
      decks: deckResult.results,
      cards,
      newCsvCount: cards.filter((card) => !card.csvExportedAt).length * 2,
      newApkgCount: cards.filter((card) => !card.apkgExportedAt).length * 2,
    };
    return Response.json(data);
  } catch (error) {
    if (error instanceof AuthError) return jsonError("Нужен вход.", 401);
    return jsonError("Не удалось загрузить библиотеку.", 500);
  }
}

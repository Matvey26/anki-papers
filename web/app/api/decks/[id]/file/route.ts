import { AuthError, requireUser } from "@/lib/auth";
import { database, files, jsonError } from "@/lib/database";

export async function GET(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const user = await requireUser(request);
    const { id } = await context.params;
    const row = await database()
      .prepare("SELECT name, r2_key FROM decks WHERE id = ? AND user_id = ?")
      .bind(id, user.id)
      .first<{ name: string; r2_key: string }>();
    if (!row) return jsonError("Колода не найдена.", 404);
    const object = await files().get(row.r2_key);
    if (!object) return jsonError("APKG не найден в хранилище.", 404);
    return new Response(object.body, {
      headers: {
        "Content-Type": "application/zip",
        "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(row.name)}`,
        "Cache-Control": "private, no-store",
      },
    });
  } catch (error) {
    if (error instanceof AuthError) return jsonError("Нужен вход.", 401);
    return jsonError("Не удалось скачать APKG.", 500);
  }
}

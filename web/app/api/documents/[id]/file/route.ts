import { AuthError, requireUser } from "@/lib/auth";
import { database, files, jsonError } from "@/lib/database";

export async function GET(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const user = await requireUser(request);
    const { id } = await context.params;
    const row = await database()
      .prepare("SELECT name, r2_key FROM documents WHERE id = ? AND user_id = ?")
      .bind(id, user.id)
      .first<{ name: string; r2_key: string }>();
    if (!row) return jsonError("Статья не найдена.", 404);
    const object = await files().get(row.r2_key);
    if (!object) return jsonError("PDF не найден в хранилище.", 404);
    return new Response(object.body, {
      headers: {
        "Content-Type": object.httpMetadata?.contentType ?? "application/pdf",
        "Content-Disposition": `inline; filename*=UTF-8''${encodeURIComponent(row.name)}`,
        "Cache-Control": "private, max-age=300",
      },
    });
  } catch (error) {
    if (error instanceof AuthError) return jsonError("Нужен вход.", 401);
    return jsonError("Не удалось открыть PDF.", 500);
  }
}

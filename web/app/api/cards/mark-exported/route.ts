import { AuthError, requireUser } from "@/lib/auth";
import { database, isoNow, jsonError } from "@/lib/database";

export async function POST(request: Request) {
  try {
    const user = await requireUser(request);
    const body = (await request.json()) as { channel?: string; ids?: unknown };
    const ids = Array.isArray(body.ids) ? [...new Set(body.ids.map(String))].slice(0, 500) : [];
    const column = body.channel === "csv"
      ? "csv_exported_at"
      : body.channel === "apkg"
        ? "apkg_exported_at"
        : null;
    if (!column || !ids.length) return jsonError("Нет карточек для отметки.");
    const statements = ids.map((id) => database()
      .prepare(`UPDATE selections SET ${column} = ? WHERE id = ? AND user_id = ? AND ${column} IS NULL`)
      .bind(isoNow(), id, user.id));
    await database().batch(statements);
    return Response.json({ ok: true });
  } catch (error) {
    if (error instanceof AuthError) return jsonError("Нужен вход.", 401);
    return jsonError("Не удалось обновить историю выгрузок.", 500);
  }
}

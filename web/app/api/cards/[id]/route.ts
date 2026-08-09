import { AuthError, requireUser } from "@/lib/auth";
import { database, jsonError } from "@/lib/database";

export async function DELETE(request: Request, context: { params: Promise<{ id: string }> }) {
  try {
    const user = await requireUser(request);
    const { id } = await context.params;
    await database().prepare("DELETE FROM selections WHERE id = ? AND user_id = ?").bind(id, user.id).run();
    return Response.json({ ok: true });
  } catch (error) {
    if (error instanceof AuthError) return jsonError("Нужен вход.", 401);
    return jsonError("Не удалось удалить карточку.", 500);
  }
}

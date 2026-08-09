import { createSession, verifyPassword } from "@/lib/auth";
import { database, ensureSchema, jsonError } from "@/lib/database";

type UserRow = {
  id: string;
  username: string;
  password_hash: string;
  password_salt: string;
  password_iterations: number;
};

export async function POST(request: Request) {
  try {
    await ensureSchema();
    const body = (await request.json()) as { username?: string; password?: string };
    const normalized = (body.username ?? "").trim().normalize("NFC").toLocaleLowerCase("ru-RU");
    const row = await database()
      .prepare(`SELECT id, username, password_hash, password_salt, password_iterations
        FROM users WHERE username_normalized = ?`)
      .bind(normalized)
      .first<UserRow>();
    if (!row || !(await verifyPassword(
      body.password ?? "",
      row.password_hash,
      row.password_salt,
      row.password_iterations,
    ))) {
      return jsonError("Неверный логин или пароль.", 401);
    }
    return Response.json(
      { user: { id: row.id, username: row.username } },
      { headers: { "Set-Cookie": await createSession(row.id, request) } },
    );
  } catch {
    return jsonError("Не удалось войти.", 400);
  }
}

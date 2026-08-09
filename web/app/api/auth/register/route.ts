import { createSession, hashPassword, validateCredentials } from "@/lib/auth";
import { database, ensureSchema, isoNow, jsonError } from "@/lib/database";

export async function POST(request: Request) {
  try {
    await ensureSchema();
    const body = (await request.json()) as { username?: string; password?: string };
    const credentials = validateCredentials(body.username ?? "", body.password ?? "");
    const password = await hashPassword(body.password ?? "");
    const user = { id: crypto.randomUUID(), username: credentials.username };
    await database()
      .prepare(`INSERT INTO users
        (id, username, username_normalized, password_hash, password_salt, password_iterations, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)`)
      .bind(
        user.id,
        user.username,
        credentials.normalized,
        password.hash,
        password.salt,
        password.iterations,
        isoNow(),
      )
      .run();
    return Response.json(
      { user },
      { status: 201, headers: { "Set-Cookie": await createSession(user.id, request) } },
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : "Не удалось зарегистрироваться.";
    if (/UNIQUE|constraint/i.test(message)) return jsonError("Этот логин уже занят.", 409);
    return jsonError(message, 400);
  }
}

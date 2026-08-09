import { AuthError, requireUser } from "@/lib/auth";
import { database, files, isoNow, jsonError } from "@/lib/database";

const MAX_PDF_BYTES = 50 * 1024 * 1024;
const MAX_APKG_BYTES = 80 * 1024 * 1024;

export async function POST(request: Request) {
  try {
    const user = await requireUser(request);
    const data = await request.formData();
    const file = data.get("file");
    const kind = data.get("kind");
    if (!(file instanceof File)) return jsonError("Выберите файл.");
    if (kind !== "pdf" && kind !== "apkg") return jsonError("Неизвестный тип файла.");

    const extension = file.name.toLocaleLowerCase("en-US");
    const validName = kind === "pdf" ? extension.endsWith(".pdf") : extension.endsWith(".apkg");
    if (!validName) return jsonError(kind === "pdf" ? "Нужен PDF-файл." : "Нужен APKG-файл.");
    const limit = kind === "pdf" ? MAX_PDF_BYTES : MAX_APKG_BYTES;
    if (file.size === 0 || file.size > limit) {
      return jsonError(`Размер файла: от 1 байта до ${Math.round(limit / 1024 / 1024)} МБ.`);
    }
    const head = new Uint8Array(await file.slice(0, 5).arrayBuffer());
    const validSignature = kind === "pdf"
      ? String.fromCharCode(...head) === "%PDF-"
      : head[0] === 0x50 && head[1] === 0x4b;
    if (!validSignature) return jsonError("Содержимое файла не соответствует расширению.");

    const id = crypto.randomUUID();
    const r2Key = `${user.id}/${kind}/${id}`;
    await files().put(r2Key, file.stream(), {
      httpMetadata: { contentType: kind === "pdf" ? "application/pdf" : "application/zip" },
      customMetadata: { originalName: file.name },
    });
    const table = kind === "pdf" ? "documents" : "decks";
    await database()
      .prepare(`INSERT INTO ${table} (id, user_id, name, size, r2_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?)`)
      .bind(id, user.id, file.name, file.size, r2Key, isoNow())
      .run();
    return Response.json(
      { file: { id, name: file.name, size: file.size, createdAt: isoNow() }, kind },
      { status: 201 },
    );
  } catch (error) {
    if (error instanceof AuthError) return jsonError("Нужен вход.", 401);
    return jsonError(error instanceof Error ? error.message : "Загрузка не удалась.", 500);
  }
}

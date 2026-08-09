import { database, ensureSchema, isoNow } from "./database";

const COOKIE = "paperdeck_session";
const SESSION_DAYS = 30;
const PASSWORD_ITERATIONS = 120_000;

export type SessionUser = { id: string; username: string };

export function validateCredentials(usernameRaw: string, password: string) {
  const username = usernameRaw.trim().normalize("NFC");
  if (username.length < 3 || username.length > 32) {
    throw new Error("Логин: от 3 до 32 символов.");
  }
  if (!/^[\p{L}\p{N}._-]+$/u.test(username)) {
    throw new Error("В логине допустимы буквы, цифры, точка, дефис и подчёркивание.");
  }
  if (password.length < 6 || password.length > 128) {
    throw new Error("Пароль: от 6 до 128 символов.");
  }
  return { username, normalized: username.toLocaleLowerCase("ru-RU") };
}

export async function hashPassword(password: string, saltBytes?: Uint8Array) {
  const salt = new Uint8Array(saltBytes ?? crypto.getRandomValues(new Uint8Array(16)));
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations: PASSWORD_ITERATIONS },
    key,
    256,
  );
  return {
    hash: bytesToBase64(new Uint8Array(bits)),
    salt: bytesToBase64(salt),
    iterations: PASSWORD_ITERATIONS,
  };
}

export async function verifyPassword(
  password: string,
  expectedHash: string,
  salt: string,
  iterations: number,
): Promise<boolean> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: base64ToBytes(salt), iterations },
    key,
    256,
  );
  return timingSafeEqual(new Uint8Array(bits), base64ToBytes(expectedHash));
}

export async function createSession(userId: string, request: Request): Promise<string> {
  const tokenBytes = crypto.getRandomValues(new Uint8Array(32));
  const token = bytesToBase64Url(tokenBytes);
  const id = await sha256(token);
  const now = isoNow();
  const expires = new Date(Date.now() + SESSION_DAYS * 86_400_000);
  await database()
    .prepare("INSERT INTO sessions (id, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)")
    .bind(id, userId, expires.toISOString(), now)
    .run();
  return serializeCookie(token, expires, request);
}

export async function getSessionUser(request: Request): Promise<SessionUser | null> {
  await ensureSchema();
  const token = readCookie(request.headers.get("cookie"), COOKIE);
  if (!token) return null;
  const id = await sha256(token);
  const row = await database()
    .prepare(`SELECT users.id, users.username
      FROM sessions JOIN users ON users.id = sessions.user_id
      WHERE sessions.id = ? AND sessions.expires_at > ?`)
    .bind(id, isoNow())
    .first<SessionUser>();
  return row ?? null;
}

export async function requireUser(request: Request): Promise<SessionUser> {
  const user = await getSessionUser(request);
  if (!user) throw new AuthError();
  return user;
}

export async function destroySession(request: Request): Promise<string> {
  const token = readCookie(request.headers.get("cookie"), COOKIE);
  if (token) {
    await ensureSchema();
    await database().prepare("DELETE FROM sessions WHERE id = ?").bind(await sha256(token)).run();
  }
  const secure = new URL(request.url).protocol === "https:" ? "; Secure" : "";
  return `${COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0${secure}`;
}

export class AuthError extends Error {}

function serializeCookie(token: string, expires: Date, request: Request): string {
  const secure = new URL(request.url).protocol === "https:" ? "; Secure" : "";
  return `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Lax; Expires=${expires.toUTCString()}${secure}`;
}

function readCookie(header: string | null, name: string): string | null {
  if (!header) return null;
  for (const part of header.split(";")) {
    const [key, ...value] = part.trim().split("=");
    if (key === name) return value.join("=");
  }
  return null;
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function timingSafeEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let mismatch = 0;
  for (let index = 0; index < a.length; index += 1) mismatch |= a[index] ^ b[index];
  return mismatch === 0;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  bytes.forEach((byte) => (binary += String.fromCharCode(byte)));
  return btoa(binary);
}

function bytesToBase64Url(bytes: Uint8Array): string {
  return bytesToBase64(bytes).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function base64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const binary = atob(value);
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

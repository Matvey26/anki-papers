import { env } from "cloudflare:workers";

let schemaReady: Promise<void> | null = null;

export function database(): D1Database {
  const binding = (env as unknown as { DB?: D1Database }).DB;
  if (!binding) throw new Error("Хранилище данных пока недоступно.");
  return binding;
}

export function files(): R2Bucket {
  const binding = (env as unknown as { FILES?: R2Bucket }).FILES;
  if (!binding) throw new Error("Хранилище файлов пока недоступно.");
  return binding;
}

export async function ensureSchema(): Promise<void> {
  if (schemaReady) return schemaReady;
  schemaReady = createSchema().catch((error) => {
    schemaReady = null;
    throw error;
  });
  return schemaReady;
}

async function createSchema(): Promise<void> {
  const db = database();
  await db.batch([
    db.prepare(`CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY,
      username TEXT NOT NULL,
      username_normalized TEXT NOT NULL,
      password_hash TEXT NOT NULL,
      password_salt TEXT NOT NULL,
      password_iterations INTEGER NOT NULL,
      created_at TEXT NOT NULL
    )`),
    db.prepare(`CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_normalized
      ON users(username_normalized)`),
    db.prepare(`CREATE TABLE IF NOT EXISTS sessions (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      created_at TEXT NOT NULL
    )`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_sessions_user_id
      ON sessions(user_id)`),
    db.prepare(`CREATE TABLE IF NOT EXISTS documents (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      name TEXT NOT NULL,
      size INTEGER NOT NULL,
      r2_key TEXT NOT NULL,
      created_at TEXT NOT NULL
    )`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_documents_user_created
      ON documents(user_id, created_at)`),
    db.prepare(`CREATE TABLE IF NOT EXISTS decks (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      name TEXT NOT NULL,
      size INTEGER NOT NULL,
      r2_key TEXT NOT NULL,
      created_at TEXT NOT NULL
    )`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_decks_user_created
      ON decks(user_id, created_at)`),
    db.prepare(`CREATE TABLE IF NOT EXISTS selections (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      document_id TEXT NOT NULL,
      target TEXT NOT NULL,
      target_normalized TEXT NOT NULL,
      sentence TEXT NOT NULL,
      page INTEGER NOT NULL,
      translations_ru TEXT NOT NULL,
      replacement_ru TEXT NOT NULL,
      alternatives_en TEXT NOT NULL,
      csv_exported_at TEXT,
      apkg_exported_at TEXT,
      created_at TEXT NOT NULL
    )`),
    db.prepare(`CREATE UNIQUE INDEX IF NOT EXISTS idx_selections_user_target
      ON selections(user_id, target_normalized)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_selections_user_created
      ON selections(user_id, created_at)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_selections_document
      ON selections(document_id)`),
  ]);
  await db.prepare("PRAGMA optimize").run();
}

export function isoNow(): string {
  return new Date().toISOString();
}

export function jsonError(message: string, status = 400): Response {
  return Response.json({ error: message }, { status });
}

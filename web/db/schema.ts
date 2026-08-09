import { index, integer, sqliteTable, text, uniqueIndex } from "drizzle-orm/sqlite-core";

export const users = sqliteTable(
  "users",
  {
    id: text("id").primaryKey(),
    username: text("username").notNull(),
    usernameNormalized: text("username_normalized").notNull(),
    passwordHash: text("password_hash").notNull(),
    passwordSalt: text("password_salt").notNull(),
    passwordIterations: integer("password_iterations").notNull(),
    createdAt: text("created_at").notNull(),
  },
  (table) => [
    uniqueIndex("idx_users_username_normalized").on(table.usernameNormalized),
  ],
);

export const sessions = sqliteTable(
  "sessions",
  {
    id: text("id").primaryKey(),
    userId: text("user_id").notNull(),
    expiresAt: text("expires_at").notNull(),
    createdAt: text("created_at").notNull(),
  },
  (table) => [index("idx_sessions_user_id").on(table.userId)],
);

export const documents = sqliteTable(
  "documents",
  {
    id: text("id").primaryKey(),
    userId: text("user_id").notNull(),
    name: text("name").notNull(),
    size: integer("size").notNull(),
    r2Key: text("r2_key").notNull(),
    createdAt: text("created_at").notNull(),
  },
  (table) => [index("idx_documents_user_created").on(table.userId, table.createdAt)],
);

export const decks = sqliteTable(
  "decks",
  {
    id: text("id").primaryKey(),
    userId: text("user_id").notNull(),
    name: text("name").notNull(),
    size: integer("size").notNull(),
    r2Key: text("r2_key").notNull(),
    createdAt: text("created_at").notNull(),
  },
  (table) => [index("idx_decks_user_created").on(table.userId, table.createdAt)],
);

export const selections = sqliteTable(
  "selections",
  {
    id: text("id").primaryKey(),
    userId: text("user_id").notNull(),
    documentId: text("document_id").notNull(),
    target: text("target").notNull(),
    targetNormalized: text("target_normalized").notNull(),
    sentence: text("sentence").notNull(),
    page: integer("page").notNull(),
    translationsRu: text("translations_ru").notNull(),
    replacementRu: text("replacement_ru").notNull(),
    alternativesEn: text("alternatives_en").notNull(),
    csvExportedAt: text("csv_exported_at"),
    apkgExportedAt: text("apkg_exported_at"),
    createdAt: text("created_at").notNull(),
  },
  (table) => [
    uniqueIndex("idx_selections_user_target").on(table.userId, table.targetNormalized),
    index("idx_selections_user_created").on(table.userId, table.createdAt),
    index("idx_selections_document").on(table.documentId),
  ],
);

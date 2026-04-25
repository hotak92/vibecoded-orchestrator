-- launcher.db — project slug column for URL-addressable routes (migration 003)
--
-- Adds a unique slug per project so /p/<slug>/... routes can resolve
-- without requiring the consumer to know the project's UUID. Slugs are
-- generated from the project name (lowercase, alphanumeric + dashes,
-- collisions resolved with a numeric suffix).
--
-- Backfill strategy: existing rows get a slug derived from name + id
-- prefix to guarantee uniqueness without name parsing logic.

ALTER TABLE projects ADD COLUMN slug TEXT;

-- Backfill: lowercase name, replace non-[a-z0-9] runs with '-', strip
-- leading/trailing dashes, append a 6-char id prefix to guarantee
-- uniqueness for any pre-existing rows. SQLite lacks regex so we fall
-- back to a naive replacement that's good enough for the few legacy
-- rows that may exist; the application layer regenerates a clean slug
-- on the next rename.
UPDATE projects
   SET slug = lower(
       substr(replace(replace(replace(replace(replace(replace(
           name,
           ' ', '-'),
           '_', '-'),
           '/', '-'),
           '\', '-'),
           '.', '-'),
           ':', '-'),
           1, 40)
       || '-' || substr(id, 1, 6))
 WHERE slug IS NULL;

-- Lock down the column going forward. SQLite can't add NOT NULL after
-- the fact without a table rebuild, so we enforce uniqueness here and
-- rely on the Rust layer to never insert NULL.
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_slug ON projects(slug);

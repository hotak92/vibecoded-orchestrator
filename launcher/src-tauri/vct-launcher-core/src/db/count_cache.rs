//! Cached Weaviate class counts (v0.2.105 "lazy-load counts").
//!
//! Weaviate (1.28 and earlier) loads a collection into RAM the first time
//! anything queries it and never unloads it. The launcher's KG and
//! /codegraph pages used to run one GraphQL `Aggregate { <Class> { meta
//! { count } } }` per class on the machine at every render — which loaded
//! EVERY project's KG and code graph into Weaviate's memory, although a
//! project only ever reads its own collections plus the shared one.
//!
//! Counts for out-of-scope collections are served from the
//! `collection_count_cache` table (migration 050) instead. The live/counted
//! decision itself lives in ONE home, `crate::weaviate_counts`, so the
//! launcher commands and the hub twins cannot drift.
//!
//! Row lifecycle:
//!  * written whenever a live count runs (own/shared/granted stay fresh,
//!    and the explicit "Refresh counts" action refills the whole cache);
//!  * DELETED on invalidation — a KG sync completing, a code-graph build
//!    finalizing, the orphan-cleanup wizard deleting a class — so a number
//!    the launcher knows is stale reads as "needs recount" (row absent →
//!    the UI shows "—"), never as fresh;
//!  * "row absent" otherwise means simply "never counted".

use rusqlite::{params, OptionalExtension};

use super::Db;

/// One cached measurement: the object count and the epoch-ms moment it
/// was taken. Returned by [`Db::collection_count_get`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CachedCount {
    pub count: u64,
    pub counted_at: i64,
}

impl Db {
    /// The last measured object count for `collection`, with the epoch-ms
    /// moment it was measured. `None` = never counted (or invalidated).
    pub fn collection_count_get(&self, collection: &str) -> Result<Option<CachedCount>, String> {
        let guard = self.lock();
        guard
            .query_row(
                "SELECT count, counted_at FROM collection_count_cache
                 WHERE collection = ?1",
                params![collection],
                |row| {
                    Ok(CachedCount {
                        count: row.get::<_, i64>(0)? as u64,
                        counted_at: row.get::<_, i64>(1)?,
                    })
                },
            )
            .optional()
            .map_err(|e| format!("collection_count_get {}: {}", collection, e))
    }

    /// Record a freshly measured count for `collection` (now = measured-at).
    /// Soft-fail callers log; a cache write must never fail a live count.
    pub fn collection_count_set(&self, collection: &str, count: u64) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "INSERT INTO collection_count_cache (collection, count, counted_at)
                 VALUES (?1, ?2, ?3)
                 ON CONFLICT(collection) DO UPDATE SET
                     count = excluded.count,
                     counted_at = excluded.counted_at",
                params![collection, count as i64, chrono::Utc::now().timestamp_millis()],
            )
            .map_err(|e| format!("collection_count_set {}: {}", collection, e))?;
        Ok(())
    }

    /// Drop `collection`'s cached count. Call this when the launcher KNOWS
    /// the collection changed (written or dropped): the next view shows
    /// "—" (needs recount) instead of a number that is known stale.
    pub fn collection_count_invalidate(&self, collection: &str) -> Result<(), String> {
        let guard = self.lock();
        guard
            .execute(
                "DELETE FROM collection_count_cache WHERE collection = ?1",
                params![collection],
            )
            .map_err(|e| format!("collection_count_invalidate {}: {}", collection, e))?;
        Ok(())
    }

    /// Drop the cached counts for every class in `classes`. The batch form
    /// of [`Db::collection_count_invalidate`] — one lock acquisition for a
    /// whole code-graph prefix (five classes) or a project's KG bindings.
    pub fn collection_count_invalidate_many(
        &self,
        classes: &[String],
    ) -> Result<(), String> {
        if classes.is_empty() {
            return Ok(());
        }
        let guard = self.lock();
        let mut stmt = guard
            .prepare("DELETE FROM collection_count_cache WHERE collection = ?1")
            .map_err(|e| format!("collection_count_invalidate_many: prepare: {}", e))?;
        for class in classes {
            stmt.execute(params![class])
                .map_err(|e| format!("collection_count_invalidate_many {}: {}", class, e))?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absent_row_reads_as_never_counted() {
        let db = Db::open_in_memory().expect("in-memory db");
        assert_eq!(
            db.collection_count_get("NeverCounted_KnowledgeGraph")
                .expect("get must not fail on an absent row"),
            None,
            "no row = never counted, NOT an error"
        );
    }

    #[test]
    fn set_then_get_round_trips_count_and_timestamp() {
        let db = Db::open_in_memory().expect("in-memory db");
        let before = chrono::Utc::now().timestamp_millis();
        db.collection_count_set("My_KnowledgeGraph", 4242)
            .expect("set");
        let cached = db
            .collection_count_get("My_KnowledgeGraph")
            .expect("get")
            .expect("row must exist after set");
        assert_eq!(cached.count, 4242);
        assert!(
            cached.counted_at >= before,
            "counted_at ({}) must not predate the set call ({})",
            cached.counted_at,
            before
        );
    }

    #[test]
    fn set_upserts_and_invalidate_removes() {
        let db = Db::open_in_memory().expect("in-memory db");
        db.collection_count_set("C", 1).expect("first set");
        db.collection_count_set("C", 2).expect("second set (upsert)");
        assert_eq!(
            db.collection_count_get("C").expect("get").unwrap().count,
            2,
            "the second measurement must overwrite the first"
        );

        // ACT: invalidate removes the row.
        db.collection_count_invalidate("C").expect("invalidate");
        assert_eq!(
            db.collection_count_get("C").expect("get after invalidate"),
            None,
            "an invalidated entry reads as never counted"
        );

        // LEAVE-ALONE: invalidating a class with no row is a no-op, and
        // the many-form never touches sibling rows.
        db.collection_count_set("Sibling", 7).expect("seed sibling");
        db.collection_count_invalidate_many(&[
            "C".to_string(), // already gone
            "Missing".to_string(),
        ])
        .expect("invalidate many with absent entries");
        assert_eq!(
            db.collection_count_get("Sibling").expect("get sibling").unwrap().count,
            7,
            "invalidate_many must not touch classes outside its list"
        );
        // Empty list is a no-op (no lock churn).
        db.collection_count_invalidate_many(&[]).expect("empty list");
    }
}

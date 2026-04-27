-- ---------------------------------------------------------------------------
-- 003_influencer_trending_stats.sql — materialized view backing /influencers/trending
--
-- WHY THIS MIGRATION EXISTS:
--   The previous implementation of /api/v1/influencers/trending used a query
--   with two correlated subqueries:
--
--     SELECT i.*,
--       (SELECT COUNT(c.id) FROM conversations c WHERE c.influencer_id = i.id),
--       (SELECT COUNT(m.id) FROM conversations c JOIN messages m ON c.id = m.conversation_id
--        WHERE c.influencer_id = i.id AND m.role = 'user')
--     FROM ai_influencers i
--     WHERE i.is_active = 'active'
--     ORDER BY message_count DESC
--     LIMIT 50;
--
--   Postgres can't push the LIMIT down because the sort key is computed —
--   it has to evaluate the subqueries for EVERY active influencer first,
--   THEN sort, THEN take 50. With ~3M+ messages this came out to a
--   sustained P95 of 6.7 seconds (Sentry Insights, 14-day window, 2026-04-27).
--
-- THE FIX:
--   Pre-compute the per-influencer counts into a materialized view. The
--   trending route then becomes a fast indexed lookup. The view is
--   refreshed by a background task in app/main.py every 15 min using
--   REFRESH MATERIALIZED VIEW CONCURRENTLY (which keeps reads working
--   during the refresh).
--
--   Trade-off: trending data is stale by up to 15 min. For a "trending
--   influencers" UX that's well within tolerance — users don't perceive
--   the difference between "trending right now" and "trending 14 min ago."
--
-- WITH NO DATA:
--   Materialized views support `WITH NO DATA` which CREATES the view
--   structure WITHOUT populating it. We use that here so the migration
--   itself is fast (seconds, not minutes). The first REFRESH happens
--   asynchronously from app/main.py at startup. During that brief
--   window (~30s-2min depending on data volume), the trending list
--   returns influencers in the LEFT JOIN's natural order with all
--   COALESCE'd counts at zero. Acceptable cold-start cost.
--
-- WHY CONCURRENT REFRESH NEEDS A UNIQUE INDEX:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY uses the unique index to
--   compute a diff and apply only the changed rows. Without one,
--   Postgres rejects the CONCURRENTLY flag.
-- ---------------------------------------------------------------------------

-- The view itself. JOINs left-outer so influencers with zero conversations
-- still appear (their counts will be NULL, which list_trending COALESCEs to 0).
-- Using FILTER (WHERE m.role = 'user') instead of WHERE in the JOIN so that
-- a conversation with zero messages still contributes to conversation_count.
CREATE MATERIALIZED VIEW IF NOT EXISTS influencer_trending_stats AS
SELECT
    i.id                                              AS influencer_id,
    COUNT(DISTINCT c.id)                              AS conversation_count,
    COUNT(m.id) FILTER (WHERE m.role = 'user')        AS message_count
FROM ai_influencers i
LEFT JOIN conversations c ON c.influencer_id = i.id
LEFT JOIN messages m      ON m.conversation_id = c.id
GROUP BY i.id
WITH NO DATA;

-- Unique index — required for REFRESH CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS idx_influencer_trending_stats_id
    ON influencer_trending_stats(influencer_id);

-- Sort-supporting index. Trending query orders by message_count DESC, so
-- this index lets Postgres scan the top-N without sorting the full view.
CREATE INDEX IF NOT EXISTS idx_influencer_trending_stats_msg_count
    ON influencer_trending_stats(message_count DESC, influencer_id);

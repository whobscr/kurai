# Kurai — review findings

Scope: `todo.md` items 1–2 (recommendation system review, mood-aware design) and the
plugin-system/version-compatibility item. Read against `github.com/whobscr/kurai@main`
(c67a2cf, 1.11.0) plus the uploaded modules. Nov 2026.

Legend: **[bug]** confirmed defect · **[risk]** plausible failure, mechanism identified ·
**[perf]** cost · **[design]** judgement call · **[retracted]** earlier advice of mine that was wrong.

---

## 0. Verdict

- The recommendation engine is a strong heuristic radio pipeline. Most of the Qwen review's
  "missing" items are already implemented; its ML suggestions are not trainable at 1–4 users.
- The mood document is a good textbook chapter written for a system with an online learned
  rating. Kurai has no such rating and rebuilds taste from source nightly, so the document's
  most elaborate part (§8, θ/φ logits) is the part needed least.
- **The mood feature layer already exists** (`tags.MOOD_HEADS`, `danceability`, `voice`, `bpm`,
  `energy()`), and **the event log already exists** (`WaveEvent`, all apps). Mood needs no new
  table and no re-indexing.
- Two confirmed bugs corrupt the feedback any mood layer would sit on. Fix those first.
- The plugin system is a clean hook registry with **no version-compatibility mechanism that
  executes**, and three failure modes that turn a mismatch into a full outage with no in-app
  recovery.

---

## 1. Confirmed bugs — fix these first

### 1.1 [bug] `main_artist` misses parenthesised guest credits
`artists.py`: `_GUESTS = r"\s+(?:feat\.?|ft\.|featuring)\s.*$"` requires whitespace
*immediately* before the marker, so an opening bracket defeats it. Run verbatim:

```
'Artist feat. Guest'        -> key 'artist'
'Artist (feat. Guest)'      -> key 'artist (feat. guest)'
'Artist [feat. Guest]'      -> key 'artist [feat. guest]'
'Artist (ft. Guest)'        -> key 'artist (ft. guest)'
'Artist feat.Guest'         -> key 'artist feat.guest'      # no space after marker
```

Consequence: `Artist` and `Artist (feat. Guest)` are two artists to the artist cooldown,
`artist_max`, `artist_gap`, `resting_artists`, `top_artists`, «Любимое» and the player's own
spacing. **Plausible root cause of the listed bug "same artist a few tracks away despite a
1-hour cooldown"** — same class of bug 1.8.2 fixed, just the bracketed spelling.

The rule is implemented **three times**, all must change together:
- `src/kurai/artists.py` (`_GUESTS`)
- `src/kurai/web/static/js/player.js` (`artistKey`)
- `src/kurai/migrations/versions/0010_artist_key.py` (SQL backfill)

They also already disagree on case: Python `.casefold()`, SQL `lower()`, JS `.toLowerCase()`.
These differ for ß (casefold → `ss`, others → `ß`), so a German-named artist is one artist on
the server and two in the player. Consider collapsing to one definition — the queue payload
already carries `artist_gap`; it could carry the key per track.

Fix: allow an optional opening bracket, e.g. `\s*[\(\[]?\s*(?:feat\.?|ft\.?|featuring|with|vs\.?)\s.*$`,
plus a new backfill migration. Check 0010's escaping directly (the extractor stripped backslashes).

### 1.2 [bug] An explicit 👎 is outvoted by play history
`vectors.listen_weight` = `log1p(play_count) + 2·starred + (rating − 3)`:

```
plays=   5 starred=False -> -0.208   negative
plays=   7 starred=False -> +0.079   POSITIVE
plays=  50 starred=False -> +1.932   POSITIVE
plays=  50 starred=True  -> +3.932   POSITIVE
```

A non-starred 👎 stays positive from **7 plays** onward; a starred one stays positive at *any*
play count (`+2` star exactly cancels `−2` for rating 1). The track is excluded from candidates
(`retrieval` filters `rating in (1,2)`), but it still pulls the taste vector toward itself and
still enters `top_artists` (`if w > 0`) → still seeds «слушают вместе». Disliking a
formerly-loved track does not demote its artist.

This is the most direct answer to "fix the track/artist rating" in todo item 2, and it has
nothing to do with mood. Fix: an explicit negative should override the implicit positive, not
be added to it.

### 1.3 [bug] Kurai's optimistic annotation writes are reverted/deleted by sync
`player.rate` commits locally via `note_listen`, *then* calls Navidrome; on `NavidromeError`
it raises 502 but the local row stays. `library.sync_listens` then does
`delete(Listen).where(track_id.not_in(active))`, where `active` only holds tracks Navidrome
reports with a play/star/rating. So:
- a 👎 on a never-played track whose Navidrome write failed is **deleted** at the next sync;
- same for a play whose scrobble failed;
- narrow race even on success: a sync between the two writes sees nothing and clears the row.

**Concrete mechanism for the listed bug "disliked tracks in the playlist".**
Fix: either write-through (fail the request, don't keep the local change) or keep a Kurai-side
overlay that sync may not clobber. Any mood/feedback layer built on `Listen` inherits this.

### 1.4 [bug] The onboarding prior is re-drawn at random every night
`library.build_taste` → `start.first_taste` → `.order_by(func.random()).limit(STYLE_TRACKS)`,
plus a fresh LLM embedding call, every night, forever. With `PRIOR = 10` a new user's taste is
mostly prior, so their wave swings nightly for no reason. Worse: `top_artists` from that random
sample becomes `Taste.top_artists` when nothing is learned → seeds `related.seeds_of` → a
newcomer's entire ListenBrainz neighbourhood is rebuilt from a different random sample nightly.
Fix: compute the first taste once at `start_asked_at` and store it.

### 1.5 [bug] `taste_of` doesn't model-guard the sound vector
`playlists.taste_of` checks `same_model` for text only; `"sound": taste.audio_vector` is used
unconditionally. `Taste` records `embedding_model` but no effnet model. After an EffNet change
the stored sound taste is compared against new-model track vectors until the next
`build_taste` — and that rebuild joins on `effnet_model == EFFNET_MODEL`, so during re-analysis
it computes the sound taste from whichever arbitrary subset was analysed first.
Fix: add `Taste.audio_model`, mirror the text guard.

### 1.6 [bug] `sync_library` can delete most of the library and its audio analysis
Only guard is `count == 0`. `navidrome.songs()` pages until a short page, sorted
`_sort=title&_order=ASC` — ties are unordered, so paging can skip rows, and a Navidrome rescan
can legitimately return a partial library. Anything not seen gets
`delete(Track).where(seen_at < started)`; `TrackAudio`, `Listen`, `WaveEvent` all cascade.
On this box (4–5 s/track analysis) that is days of CPU lost to a transient partial response.
Fix: page by `id` (unique, stable) instead of `title`; refuse the delete when the drop exceeds
a fraction of the table, or mark unseen and purge after N consecutive syncs.

### 1.7 [bug] `discography(limit=100)` counts raw release groups, not albums
`public_data.discography`: loop condition is `len(groups) < limit` on the *unfiltered* list;
the studio-album filter runs after. An artist whose first 100 groups are mostly compilations
and live releases silently returns far fewer than 100 albums.

### 1.8 [bug] `_unheard_wave` treats a failed job as "nothing new found"
`player._unheard_wave`: `rebuilt_in_vain` accepts `status in ("done", "failed")`, so a DB error
makes the player replay already-heard tracks. Should be `done` with a `skipped` result only.

---

## 2. Retractions — do not act on my earlier advice here

- **[retracted] "No play history outside the web player."** Wrong. `wave.Watcher` polls
  Navidrome «now playing» every 15 s for *every* app and writes
  `WaveEvent(user_id, track_id, skipped, seconds, at)`, kept 30 days. Do **not** build a
  `PlayEvent` table. Mood at event time is reconstructable from the log; a stored snapshot is
  a convenience, not a blocker.
- **[retracted] "The mood doc's z-score is in a different unit than your corrections."**
  `vectors.spread()` is `max(0.01, np.std(...))` — already std. The doc's z-score drops
  straight in.
- **[retracted] "Negative weights may collapse the taste vector; redesign."**
  `taste_vector` already separates positive/negative means and caps the subtraction at
  `push=0.3`. Residual risk is narrow (see 4.1), worth a unit test, not a redesign.
- **[retracted] "`related.py` keys artists differently from `Track.artist_key`."**
  `song_fields` sets `artist_key(song["artist"])`, and `main_artist` is idempotent, so
  `artist_key(main_artist(x)) == artist_key(x)`. The chain is consistent end to end.
- **[retracted] "The regex dots aren't escaped."** They are, in the real repo. Paste artifact.
- **[retracted] "Two different skip definitions."** `player.event` and `Watcher.update` use the
  same formula and the same `mix.skip_share`. Only the inputs differ (see 4.9).
- **[retracted] "`player.event` double-counts."** `report()` zeroes `played` and returns early
  when `played < 1`, so the client guards against firing twice. No server-side dedupe exists,
  so a retried POST would still double-count — latent, not observed.
- **[retracted] "`artist_gap` isn't kept across wave versions."** `player.js spaced()` runs in
  both `start()` and `refresh()`, against the last `gap` tracks played including the current
  one. And `arrange`'s crowded-artist lookahead was added in 1.8.2 precisely to stop tail
  adjacency.

Already implemented, despite the Qwen review listing them as missing: separate vector spaces;
separate «сейчас» directions instead of query mixing; corrections in spread units; artist pause
with shape that yields on scarcity; MMR + per-artist caps; part quotas with overflow; session
decay; explicit/implicit weighting in `listen_weight`. Candidate source labels partly exist
(`Candidate.kind` = new/known/related/popular); what's missing is *which space* it came from.

---

## 3. Performance — two easy wins that fund everything else

### 3.1 [perf] `retrieval.joint()` recomputes library means on every call
`func.avg(func.l2_normalize(col))` over every valid row, per space. Called by `wave.plan` on
every rebuild (every 4 min per listening user), plus every `plan_mixes`, `ask` and admin
preview. At ~30k tracks × (1024 text + 1280 effnet + 512 clap) that is a few hundred MB read
and ~10⁸ normalize ops per call, on the Postgres capped at ~1 CPU / 1 GB (decision G) and
shared with RealVelach. The means change only when the library or a model changes.
**Cache them in a table; refresh after sync/analysis and on model change.**

### 3.2 [perf] `mmr_select` recomputes norms in the innermost loop
`normalize(c.vector)` and `normalize(s.vector)` are called for every (candidate, chosen) pair
on every iteration: ≈ `count × pool × avg_chosen` ≈ 40 × 900 × 20 ≈ 700k iterations per part,
each two `norm` calls plus a dot on a ~2800-dim vector, times up to 5 parts per wave.
Precompute unit vectors once per pool and keep chosen as a matrix so redundancy is one
`chosen_matrix @ unit` BLAS call. Note `j.item` already returns a unit vector when all spaces
are present, so most of those norms are no-ops being paid for. Measure, but expect this and
3.1 to dominate build time.

### 3.3 [perf] Smaller
- `popularity._put_tracks`: one `UPDATE` per track (an artist with 500 library tracks = 500
  round-trips, ×150 artists/run). Batch it.
- `public_data._get` sleeps **inside** the `async with`, holding session + response open for
  up to 60 s (Retry-After cap) across four retries. Move the sleep out.
- `retrieval.candidates` runs 2–3 ANN queries per part with `not_in` subquery filters; with
  `track_rest_days=7` and a heavy listener the filter can be selective enough that HNSW's
  default `ef_search` returns very few rows. Consider raising `hnsw.ef_search` or pgvector 0.8
  iterative scans.

---

## 4. Correctness and quality findings

### 4.1 [risk] Taste vector can shrink or invert
Both means in `taste_vector` are of unit vectors. If the positive mean is short (diverse taste,
vectors cancel) and the negative mean is long and roughly parallel (concentrated dislikes), the
result can approach zero or flip sign. `Joint.center` then yields `normalize(≈ −mean)` — a
definite "anti-average music" direction, not noise. Bounded by `push=0.3`; add a unit test.

### 4.2 [design] Negatives never decay, positives do
`listen_weight`: `if weight <= 0: return weight` skips the recency factor entirely. Intentional
("a 👎 always counts"), but with `floor=0.3` a three-year-old 👎 has full weight while a
three-year-old love has 30%. The negative term's relative influence grows monotonically over
years — the opposite of an evolving taste.

### 4.3 [bug] `_anchors` groups against a stale representative, and force-absorbs outliers
`wave._anchors`: `groups[best][0]` stays the *freshest* track's vector while the query is the
mean of all members, so identity and direction diverge. Once `len(groups) >= ANCHORS`, a fifth
unrelated direction is force-joined to its nearest group even at likeness 0.05 — silently
mixing directions, the exact failure the separate-quotas design exists to prevent.
Fix: use the running mean as representative; drop or replace the weakest group.

### 4.4 [design] Repeat listening doesn't strengthen a direction
`fresh[e.track_id]` is overwritten, so a track played five times counts once. Repeats are one
of the strongest "into this right now" signals available. Sum the freshness instead.

### 4.5 [design] `away` has no decay, and only the nearest skip counts
Every skip in the 3-hour session pushes at full strength — a skip from 2.9 h ago repels as hard
as one from 10 s ago, while positives decay with `session_half_life`. And `adjust` uses
`max(0, max(dot))`, so repeated similar skips don't compound. **Both block mood doc §19**
("several skips share a mood") until `away` carries weights.

### 4.6 [bug] Skipped tracks eat pool slots
`candidates` filters `Listen.last_played >= since` in SQL, but `wave.recently_heard` also
unions `WaveEvent` — and a skip Navidrome never counted as a play has no `Listen` row. Those
are fetched into the 300 then dropped in Python. After a skip-heavy session a meaningful
fraction of every pool is wasted. Push the `WaveEvent` exclusion into SQL.

### 4.7 [bug] `retrieval.seeds` takes `.limit(200)` with no `order_by`
For several named artists with large discographies an arbitrary 200 rows may all come from one
of them, so «Спросить» silently gets one artist's sound. Sample per artist.

### 4.8 [risk] «Слушают вместе» can starve at depth 1
`related.refresh` keeps `KEEP=60` sorted by score across all depths; depth-2 entries accumulate
score from many seeds and can crowd out depth-1 ones. If playlists use depth 2 and the wave
depth 1, `artists_for(taste, 1)` returns little. Keep per-depth quotas.

### 4.9 [design] Skip-label quality — the real mood prerequisite
The log exists; the labels are biased in specific directions:
- `Watcher.seconds` is **wall-clock between polls**, not playback. Pauses inflate it →
  paused-then-skipped tracks are labelled completed. Seeking breaks it too.
  `player.js` is correct by contrast: `count()` adds `t - lastTime` only on forward movement
  and every seek goes through `seek()`. **So the bias is by client, not random.**
- The last track of every session is never logged (`set(playing) - set(current)` just deletes);
  the first track after each Kurai restart isn't either (`since=None`). One-track sessions
  produce nothing.
- Anything played and skipped inside one 15 s window is invisible — the very-early-skip signal
  the Qwen review wanted is exactly the one you can't see.
- Keying is `(username, str(playerId))`; verify Navidrome's `playerId` is stable and present.
- `player.event` sets `skipped = disliked or seconds < …`, so a 👎 after a full play is logged
  as a skip. For mood attribution those are opposite cases. Add a flag (deriving it later from
  `Listen.rating` doesn't work — ratings change).
- No source column, so wave-caused plays are indistinguishable from deliberate ones.
  **`chosen` is already a parameter to `player.event` and is thrown away** — persisting it is
  one column and unlocks both the exposure-bias discount and a much stronger intent signal
  (a track sought out and finished ≫ a passive completion).
- 30 days is short for daypart priors, and `_save` only prunes when a wave is saved. Keep raw
  events 30 days and add a tiny nightly rollup (user × daypart × mood bucket × outcome).
- Add absolute-time thresholds: `< 10 s` → ignore as accidental; `> 180 s and > 25%` → not a
  skip. `DEFAULT_DURATION = 240` is used when the client reports no duration.

### 4.10 [risk] Landmine for the mood work: popular candidates have 1-dim vectors
`wave._popular` builds `Candidate(i, "", np.zeros(1), 0.0, "popular")`. Safe today **only**
because `_between` runs after `arrange`. The moment you add mood continuity to ordering (mood
doc §5.3) or score popular candidates at all, `np.dot` against a 2800-dim vector raises.
Give them real vectors first.

### 4.11 [risk] `new_listener` is tied to the embedded-track count
`tracks_used = len(text_vectors)`. Change the embedding model and everyone becomes a newcomer
until re-embedding finishes — popular tracks every 3rd, `now_weight` forced to 1.0. Use a listen
count, not a successfully-embedded count.

### 4.12 [bug] MusicBrainz's 1 req/s rule is not enforced across processes
`public_data._musicbrainz` throttles on `self._last_mb`, but `client(db)` builds a fresh
`PublicData` per call. Worse: `/admin/check/musicbrainz` runs in the **web process** while
`artist_ids.look_up` runs in the worker. A module-level timestamp (like `navidrome._tokens`)
is therefore **insufficient** — needs a DB lease or advisory lock. MusicBrainz bans for this.

### 4.13 [design] Contamination path: mood → top_artists → related → 15 % of the wave
`build_taste` appends to `artists` only when `w > 0` → `top_artists` → `Taste.top_artists` →
`related.artists_for` → the «слушают вместе» share. One bad evening can reshuffle top artists,
which reshuffles the ListenBrainz neighbourhood, which reshapes 15 % of the wave for days.
Bigger and more persistent than the taste-vector damage; neither review mentions it.
Fix: compute `top_artists` from explicit feedback and completions only, or smooth it over a
longer window than the vector.

### 4.14 [design] Discount plays the system caused
`WaveEvent` identifies wave plays; `Listen` comes from everywhere. Down-weight wave-originated
plays in `build_taste` (×0.5–0.7, explicit feedback untouched) to correct exposure bias. Needs
4.9's source column. Concrete fix for the Qwen review's feedback-loop concern.

### 4.15 Minor
- `mixing.REST_LAST = 10` is applied as `rest_penalty * scale * 2` → actually **20 spreads**;
  undocumented factor 2.
- `candidates` retrieves with `q + mean`, but `q` is the *normalized* deviation, so the
  deviation is over-scaled — the pool query is more extreme than the scoring query, worst for
  mainstream taste near the library mean.
- A track missing a space gets zeros there, shrinking its relevance toward 0: it can never be
  a strong match. During indexing, unanalysed new music can't reach the top of the wave.
- `popularity.track_score`: `artist.c.listens / 10` is Postgres **integer division** → ties at 0
  for artists under 10 listens.
- `search._matches` escapes `%` and `_` but not `\`.
- `public_data.similar_artists` hits `labs.api.listenbrainz.org`, which fails
  `url.startswith(LISTENBRAINZ)`, so a 401 there gives a bare "HTTP 401" instead of
  `TOKEN_NEEDED`.
- `popularity._put_tracks` resets `listens` to 0 for tracks not in the current top-recordings
  answer — lossy if the API truncates.
- `start.popular` orders by household plays then `track_score()` then random, never
  `Track.listens` — which `search._popular_first` puts first. A filler track on a popular album
  ranks as popular. Align the two.
- `library.recent_listens` is one row per track, so «История» can't show repeats even though
  `WaveEvent` has them.

### 4.16 [risk] `audio/models.py` operational notes
- `_clap_audio` decodes the **whole file at 48 kHz**: a 20-minute track is ~230 MB of samples
  before intermediates. If the audio container is capped near 1 GB that's a real spike;
  extracting just the three windows with ffmpeg avoids it.
- `torch.set_num_threads(self.threads)` only reaches torch. Essentia's TensorFlow graphs have
  their own thread config — verify they're capped on the shared 6-core box.
- `SEGMENT_SECONDS = 180` means effnet sees only the **middle three minutes**. For a 12-minute
  post-rock/ambient piece that builds, the mood read can be plainly wrong. CLAP already samples
  three windows; effnet doesn't. Accept it and treat long tracks' mood as lower-confidence
  rather than paying 3× CPU.

---

## 5. Mood-aware design — review of `mood-aware_recommendation_mechanism.md`

### 5.1 The feature layer is already built (Phase 1 is done)
`tags.MOOD_HEADS` gives happy/sad/relaxed/aggressive/party/acoustic/electronic as per-track
probabilities, plus `danceability`, `voice`, `bpm`, `key`, and `energy()` already combining
them. `audio/models.Analyzer` runs all of it on every effnet pass and stores it. **Phase 2
needs no re-analysis and no schema change.**

Recommended vector:
- **Drop `electronic`, `acoustic`, `voice`** — timbre/genre, already covered by the text and
  sound spaces. Including them makes the mood bonus collinear with taste, i.e. you'd just be
  re-weighting taste. Keep them for `describe()`.
- **Two affective axes** (a circumplex): valence ≈ `happy − sad` (aggressive pushing negative);
  arousal ≈ `mean(party, aggressive, danceability) − relaxed`. Reuse `energy()` for the arousal
  side rather than defining a second one (DRY; it's already what `fits()` and «Спросить» speak in).
- **Center each axis across the library (z-score, not min-max).** These are probabilities in
  [0,1]; raw cosine sits in ~0.7–0.99, so the doc's `coherence` is pinned near 1 (confidence
  becomes just an event count, and the "don't overfit to 2 tracks" guard is decorative) and its
  change detector `cosine < 0.2` **never fires**. Confirmed, not hypothetical.
- **Don't put raw `bpm` in the vector.** `PercivalBpmEstimator` halves/doubles freely — `fits()`
  already works around it with the energy-agreement trick. A tempo feature wrong by 2× is worse
  than none; arousal already carries it. Octave-fold first if you want it later.
- **`themes` is a trap.** The Jamendo mood/theme head is a 57-tag mood taxonomy, but `top()`
  keeps only the top 5 above 0.1 and *which* 5 differs per track. No consistent vector without
  storing all 57 and re-analysing. Start with the binary heads (complete scalars).
- **Missing mood must be `None`, not zero.** `Result.moods` is `{}` when effnet didn't run, so
  mood coverage == effnet coverage. Mirror `energy()`'s `None` convention: skip unknown tracks
  when inferring session mood, give them bonus 0 (neutral), never a negative z-score. The doc
  has no notion of track-side uncertainty.
- Don't derive mood as a projection of `effnet` — that space carries genre/timbre already.

### 5.2 [design] The attribution formula cannot see a bad day
§8.2 sets `mood_explained = conf·(1 − match)` for skips. But mood is inferred from *accepted*
tracks. On a day when the user skips everything, few tracks are accepted, so the mood estimate
stays stale and confident, so every skip looks "on-mood" → `global_weight ≈ 1` → full permanent
penalty. **That is exactly the scenario the document exists to prevent.** The formula only
protects off-mood tracks, which the pipeline mostly doesn't play by construction.

What's actually wanted for "spikes" is a **session-level regime detector**, not per-event mood
attribution: acceptance rate collapsing + no coherent accepted direction = "nothing fits right
now". Response: damp *all* implicit negatives for that session and switch to comfort mode
(raise familiar share, cut new/related, cap `skip_push`). Note `skip_push` on a bad day pushes
the wave away from everything the user likes, since everything played is near their taste —
that guardrail is worth having regardless of mood.

### 5.3 [design] θ/φ online ratings fight the architecture
`build_taste` recomputes from scratch nightly from `Listen` + decay. There is no accumulating
permanent score to poison — damage already decays with `taste.half_life_days`. Mutable online
logits as a source of truth break idempotency, model-change rebuilds, un-disliking and history
deletion, and create a state-repair problem that doesn't currently exist.

**All of Phase 5 collapses to: make `listen_weight` context-aware at the nightly rebuild** —
down-weight a negative *implicit* event whose context says it was off-mood; leave explicit 👎 at
full weight. One function plus the event log. §11 is already satisfied: `Listen.rating`/`starred`
are Navidrome's own annotations, synced both ways.

### 5.4 Scope and knobs
- **Mood is wave-only.** Nightly mixes have no session; `ask` already *is* an explicit mood
  request (CLAP text + bpm/energy/vocals). Add params with `defaults={"wave": ...}` only — and
  note `Mix.__getattr__` falls back to the first profile's default, so guard against playlists
  code reading them accidentally.
- Ship **two** knobs (influence; reuse `session_half_life` for decay). Keep the rest as module
  constants. There are already 22 params; AGENTS.md says YAGNI.
- **4–6 dayparts with shrinkage** to the user's mean, not 24 hourly buckets that will never fill.
- Apply the bonus in `spread()` units (already std). **Don't apply it inside the «слушают
  вместе» part** — that share exists to be off-taste, and a genre-correlated mood bonus will
  quietly conventionalise it.
- Reuse the existing "now" directions for change detection rather than building a second
  detector: several comparable fresh directions = ambiguous mood = low confidence; one dominant
  fresh direction = high confidence.
- **Do the wave→pulse rename before adding `mix.wave.mood_*` keys**, or write two settings
  migrations.

### 5.5 What's genuinely worth taking from the doc
Phase 0 (logging — but see 4.9: fix labels, don't build a log), shadow mode first, and
**«Не сейчас» / "Not now"** as a `WaveEvent` kind that never touches Navidrome. Highest value
per line of code in either document, and it gives *labelled* mood-mismatch events instead of
inferred ones.

---

## 6. Qwen review triage

Wrong for this box:
- **"Retrieve 500–2000 per space."** Shared Ryzen 2600, Postgres ~1 CPU / 1 GB, rebuild every
  3 tracks / 4 min. Make the pool **adaptive** — grow only when the filtered pool is thin
  (`plan_mixes` already has the signal: `len(pool) < mix.size // 2`).
- **"Adaptive per-user parameters."** There is a settings tab for that. Silently moving a slider
  the user set is hostile and undebuggable. Better and Kurai-shaped: show evidence on «Задачи»
  ("new tracks skipped 62 % vs 31 % familiar — lower «Доля нового»?") and let them decide.
- **"Learned reranker / bandits."** Not trainable at 1–4 users. The blocker is data and
  evaluation, not CPU. Defer.
- **"A/B test."** Meaningless at household scale. What is available today: `choose`/`plan_mixes`
  take `rng`, and `mixing.standard(profile)` exists → deterministic seeded replay of "same
  session, mood on/off". Use that plus shadow logging. `ask` already returns
  `plan`/`fitting`/`short` for «Задачи» — same observability pattern for mood.

Worth doing, cheap: absolute-time skip thresholds (4.9); `artist_skip_ban` as "2 skips **and no
completed track** by that artist this session"; the negative-weight unit test (4.1); the
exposure discount (4.14); per-space candidate source labels (extend `Candidate.kind`).

---

## 7. Plugin system — will it survive a plugin/core version difference?

**No, not as built.** The hook design is clean; what's missing is detection and containment.

### 7.1 [bug] The version contract exists only on paper
- `Plugin` has **no** `requires` / `api_version` field; `installed()` is a bare
  `tuple(ep.load() for ep in sorted(entry_points(group=GROUP)))`. No check in `plugins.py` or
  `cli.migrate`.
- The documented plugin Dockerfile installs with **`uv pip install --no-deps`**, so
  `kurai>=1.0.0,<2` is never evaluated in the image. Decorative.
- The plugin image is `FROM ghcr.io/whobscr/kurai:${KURAI_VERSION}` with the tag from `.env`.
  Bumping `KURAI_VERSION` — the documented update procedure — **silently rebuilds every plugin
  against an untested core**, or pulls a plugin tag that doesn't exist.

Fix: `requires: str = ">=1,<2"` on `Plugin`, checked against `kurai.__version__` at load; on
mismatch skip, log, show red on «Плагины».

### 7.2 [bug] One bad plugin bricks the install, and the off-switch can't rescue it
`cli.migrate()` builds its list from `plugins.installed()`, and `migrate()` runs before
`uvicorn.run`, before `worker.run`, before the audio service. An `ImportError` in one plugin
means **no container starts**. And the documented remedy is unreachable: `installed()` loads
*every* entry point, `disabled()` only filters afterwards, and the «Плагины» page calls
`installed()` in both `_tab_data` and `save_plugins`. `@lru_cache` doesn't cache exceptions, so
it re-raises on every call. Recovery = hand-editing the settings table or rebuilding the image.

Fix: per-`ep.load()` try/except, collect failures, surface them on «Плагины»; read `disabled()`
**before** loading so a disabled entry point is never imported.

### 7.3 [bug] `disabled()` fails open, and caches the failure
`except Exception: log.warning(...); return frozenset()` — a transient DB hiccup at start turns
every plugin on, `@lru_cache`d for the process lifetime. For the example plugin, which moves
albums into the library, that's file operations the admin forbade. Distinguish "no settings
table yet" (first run → all on) from "couldn't read" (retry or fail closed).

### 7.4 [bug] Namespaces are convention only — a plugin can replace core behaviour
Every merge is last-wins with no collision check:
- `handlers() = {**HANDLERS, **plugins.jobs()}` → a plugin defining `jobs={"wave": …}`
  **replaces the wave builder**.
- `{**EVERY, **plugins.every()}` in `schedule()` → change a core job's interval.
- `{**CORE_MANUAL_JOBS, **plugins.merged("manual_jobs")}` → relabel core buttons.
- `merged()` is `out.update(getattr(p, attr))` → between plugins the alphabetically-last
  entry-point name wins, silently, for jobs, tabs, checks, apps.
- `admin.tabs()` sandwiches plugin tabs between two slices of `CORE_TABS` at
  `index("gateway")+1`, so a plugin tab named `wave`/`playlists`/`models`/`audio`/`gateway`/
  `general` **overwrites the core tab**, while `users`/`apps`/`plugins`/`jobs` happen to be
  protected. An accident of dict ordering, not a policy. `fields(tab)` and `tab_docs(tab)`
  check plugin tabs first.

The form-save whitelist itself is **correctly implemented** — `save()` iterates
`tab_fields.items()`, not the posted form. The gap is that nothing validates the *declaration*:
`AdminTab.fields = {"llm.api_key": str}` or `{"navidrome.admin_password": str}` would be written
by core without complaint.

Fix: one registration point rejecting collisions with core names and other plugins, and
enforcing the `<plugin>.` prefix the docs already ask for.

### 7.5 [design] Hook signatures are positional, so any evolution is a major break
`after_save(db, saved, before)`, `Step(db)`, `JobHandler(db, job)`, `Check(db)`. Adding an
argument breaks every plugin. `App(label, url, keep_prefix, headers, frame=True, about="")` is
documented positionally, so reordering fields breaks callers too. Make the public dataclasses
`kw_only=True` and pass callables a single context object — then fields can be added in minor
versions, which is what the semver promise claims.

### 7.6 [design] The real public API is wider than the documented one
The "What a plugin may use" table omits things the same document *instructs* plugins to use:
`base.html` and its blocks, `admin_macros.html` (macro names and the `form_start(tab)`
signature), `partials/music_tabs.html`, the `music_tab` variable. Also `related.py`'s docstring
says "`neighbours` is the walk itself (the plugins use it too)" — but
`kurai.recommend.related` isn't in the table. A minor release can break every plugin without
touching one listed import. Either publish and freeze these, or move the coupling behind
functions. CI already runs the example plugin's tests; extend it to render every hook (tab,
page, music tab, docs page, check).

### 7.7 [bug] Core's `env.py` lacks the filter the plugin's has
Plugin models go on core's `Base`, and core `migrations/env.py` is just
`target_metadata=Base.metadata` with **no `include_object`**. The docs tell plugin authors to
limit *their* autogenerate; there's no reciprocal guard.
- Autogenerate with the plugin installed but its tables not yet in the dev DB → a **core**
  migration gains `create_table` for plugin tables → both histories try to create them.
- Autogenerate without the plugin installed against a DB that has its tables → a core migration
  gains `drop_table` for them.

Fix: mirror the plugin's filter in core — exclude tables owned by any installed plugin.

### 7.8 [risk] Migration and runtime isolation
- `cli.migrate`'s loop has no per-plugin try/except: one failing plugin migration aborts every
  later one (entry-point-name order) and restart-loops the container.
- `pg_advisory_lock` has no timeout — a stuck migration wedges web, worker and audio together.
- Plugin nightly steps receive **the same `db` session** as core steps, and `_nightly` does
  `await db.rollback()` on failure then continues. A plugin can discard core's pending work or
  leave the session unusable for `old_playlists_removed` and the rest. Give each plugin step
  its own session.
- The general lane is single-slot (`limit = … if interactive else 1`) with no per-job timeout.
  One hanging plugin job stops library syncs, popularity, artist ids and the whole night.
- `claim(…, handlers().keys())` parks a disabled plugin's jobs (neat), but an **uninstalled**
  plugin's queued jobs are never claimable and never pruned (`prune` only removes done/failed).

### 7.9 What's genuinely good
Separate Alembic history and version table per plugin, under one advisory lock, after core's —
the right design and rare in plugin systems. The form-save whitelist. Parking a disabled
plugin's jobs by filtering `claim` on handler keys. Step-level failure isolation in `_nightly`
with `PartialFailure` keeping what succeeded. A working example plugin with tests in CI.
Unusually complete docs.

---

## 8. Suggested order of work

**Bugs that corrupt everything downstream (small):**
1. `main_artist` regex — three places + backfill migration (1.1).
2. Annotation write-through vs. overlay decision (1.3).
3. 👎 weight: explicit negative overrides implicit positive (1.2).
4. Persist the first taste (1.4); add `Taste.audio_model` guard (1.5).
5. `sync_library`: page by `id`, refuse large drops (1.6).

**Cheap performance, buys the headroom mood needs:**
6. Cache `joint()` means (3.1); precompute unit vectors in `mmr_select` (3.2).

**Plugin containment (answers the todo question, a few dozen lines):**
7. Per-entry-point try/except in `installed()`; read `disabled()` first; show failures on
   «Плагины» (7.2).
8. `Plugin.requires` + version check at load (7.1).
9. Collision detection + `<plugin>.` prefix enforcement in one registration point (7.4).
10. `include_object` in core `env.py`; per-plugin try/except in `cli.migrate` (7.7, 7.8).
11. `kw_only=True` on public dataclasses; context objects for callables (7.5).
12. Own session per plugin nightly step; per-job timeout (7.8).
13. Publish the template/macro contract; render every hook in CI (7.6).

**Mood, in order — no new table, no re-indexing, no θ/φ:**
14. Label quality: absolute-time thresholds, dislike flag, persist `chosen`, idempotency,
    nightly daypart rollup (4.9).
15. `mood_of(a) -> ndarray | None` in `tags.py` next to `energy()`: two centered axes from the
    existing heads. Pure function, trivially testable (5.1).
16. **Shadow mode**: compute session mood in `wave.plan`, put it in `Plan.result` beside
    `played`/`skips` so «Задачи» shows it. Verify skipped tracks score lower than completed
    ones. **If they don't, stop here.**
17. «Не сейчас» in the web player as a `WaveEvent` kind that never touches Navidrome (5.5).
18. Session regime detector + comfort mode (5.2).
19. Soft bonus in `adjust`, in spread units, wave-only, not inside the related part (5.4).
    Give popular candidates real vectors first (4.10).
20. Context-weighted `listen_weight` at the nightly rebuild + `top_artists` from explicit
    feedback and completions only (5.3, 4.13).

**Also worth doing:** `away` decay and weights (4.5); `_anchors` representative + outlier
handling (4.3); repeat freshness (4.4); WaveEvent exclusion in SQL (4.6); per-artist seed
sampling (4.7); per-depth related quotas (4.8); MusicBrainz cross-process lease (4.12);
`new_listener` signal (4.11); `rebuild_minutes`/rename sequencing (5.4).

---

## 9. Not verified

Never read: `audio/service.py`, `audio/analyze.py`, `settings.py`, `outbound.py`, `web/app.py`,
`jobs/worker.run_one` (whether a per-job timeout exists), `jobs/window.py`, the example plugin's
`migrations/env.py` (404 on the guessed path — its filter is inferred from the docs), and
`llm/`. Open questions those would settle:
- whether `run_bot()` blocks when the bot is disabled (if it returns immediately,
  `asyncio.wait(FIRST_COMPLETED)` in `worker.main` would exit the worker at once);
- whether `Navidrome`'s `playerName` for the web player is exactly `"kurai-web"` (if Navidrome
  decorates it, `Watcher`'s filter fails and web-player events are double-counted);
- whether `Track.artist_key` is populated on every insert path (the column default and
  `song_fields` both do it; a third path would drift).

The pasted copies in the knowledge base have artefacts (spaces inside identifiers and strings,
`name` for `__name__`, `due_artists` for `_due_artists`, unescaped regex dots). Findings above
were checked against the clean repo source where it mattered; 1.1 and 1.2 were verified by
running the actual code.
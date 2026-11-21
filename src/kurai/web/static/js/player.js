// The web player: plays «Пульс» or a playlist from the server (/player/…) in the bar at the bottom, which stays
// while the pages change (kurai.js swaps only the page). Each track that gives way to the next is reported, played on or
// skipped; the pulse is rebuilt from that on the server, and the player takes the new version's tracks as the next ones
// (asking after each report and every half a minute), without a gap. Buttons with data-play="pulse" or a playlist's id
// start it (data-start: from that track of it, as in «История»), or pause and resume what they started; data-play
// "artist:…", "album:…" or "track:…" is a set chosen in the library search: played to its end, then on into the pulse,
// and a skip of what was chosen isn't told as one (a 👎 is). data-add (the same values) puts a set into the player's
// list instead: the end of a chosen set, or right after the current track of the pulse or a playlist (it plays next,
// then they go on). The list («Очередь», from the bar or Q) shows what plays and what comes; a track plays on a tap,
// ✕ takes it out, «Сохранить» keeps it as the user's own playlist. The list is kept on this device (a reload finds it
// paused where it was). ♥ and 👎 (data-rate, data-track) rate a track. Repeat (off, the playlist, the track; the pulse has no end, so no playlist
// repeat there), the previous track, the volume and the keys (the «Клавиши» dialog lists them) are the player's too.
// The phone's lock screen and headphones control it as well (Media Session).
// Two audio elements take turns: while one plays, the other loads the next track (PRELOAD seconds before the end), and
// the next track starts on it at once. A phone with its screen off lets a page go on only while it sounds: a gap to
// load the next file was enough for Chrome to freeze the player between tracks.
(() => {
  const $ = (id) => document.getElementById(id);
  const bar = $("player"), first = $("player-audio");
  if (!bar || !first) return;
  const strings = first.dataset;  // the texts the page gives the player
  const elements = [first, new Audio()];
  elements[1].preload = "auto";
  let audio = first;  // the one playing (or ready to); the other is the spare
  const spare = () => elements.find((el) => el !== audio);
  let preloaded = null;  // the track id the spare holds
  const PRELOAD = 30;
  const els = {cover: $("player-cover"), title: $("player-title"), artist: $("player-artist"), source: $("player-source"),
    toggle: $("player-toggle"), next: $("player-next"), prev: $("player-prev"), seek: $("player-seek"),
    time: $("player-time"), duration: $("player-duration"), repeat: $("player-repeat"), keys: $("player-keys"),
    queue: $("player-queue"), queueList: $("player-queue-list"), notNow: $("player-not-now")};
  const store = {  // per-device remembering; private windows may refuse it
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* not remembered */ } },
  };
  const say = (key, values = {}) => Object.entries(values).reduce((s, [k, v]) => s.replace(`{${k}}`, v), strings[key]);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const post = (url, data, keepalive = false) => fetch(url, {method: "POST", keepalive, credentials: "same-origin",
    headers: {"X-CSRF-Token": document.body.dataset.csrf, "Content-Type": "application/json"},
    body: JSON.stringify(data)});
  const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  let source = null;  // {which: "pulse", a playlist's id, a chosen set or "queue", name, version, chosen}
  let tracks = [], index = 0;  // a track the user put in the list has `added`
  // seconds the current track really played: every step forward of its time, but not a seek (every seek goes
  // through seek(), which counts up to where it was first). Counted from the time itself, not from how often
  // «timeupdate» comes: a phone with its screen off sends it seldom, and a gap of seconds was taken for a seek, so
  // those plays went untold
  let played = 0, lastTime = 0;
  const count = () => {
    const t = audio.currentTime;
    if (t > lastTime) played += t - lastTime;
    lastTime = t;
  };
  const seek = (to) => {
    count();
    audio.currentTime = to;
    lastTime = to;
  };
  let failures = 0;
  const heard = new Set();  // this session's tracks: a new version of the pulse doesn't bring them back
  const statuses = {};  // what the player says about each source (shown wherever the page has its place)
  let refreshing = null;
  const current = () => tracks[index];
  const isPulse = () => source && source.which === "pulse";
  const REPEATS = ["off", "all", "one"];
  let repeat = REPEATS.includes(store.get("kurai.repeat")) ? store.get("kurai.repeat") : "off";
  const volume = Number(store.get("kurai.volume"));
  if (volume >= 0 && volume <= 1 && store.get("kurai.volume") !== null) elements.forEach((el) => { el.volume = volume; });

  // what a key or a button did, for a moment at the bottom
  const note = (text) => {
    const el = $("player-note");
    if (!el || !text) return;
    el.textContent = text;
    if (typeof ui === "function") ui("#player-note", 1500);
  };

  const setStatus = (which, text) => {
    statuses[which] = text;
    document.querySelectorAll(`[data-player-status="${which}"]`).forEach((el) => { el.textContent = text; });
  };

  // the page's play buttons show what plays (the page may have been swapped since); one with data-start is a track
  // of its source («История»)
  const playing = (b) => !audio.paused && source && b.dataset.play === String(source.which)
    && (!b.dataset.start || (current() && current().id === b.dataset.start));
  const syncButtons = () => {
    document.querySelectorAll("[data-play]").forEach((b) => b.classList.toggle("playing", playing(b)));
    showRating();
    els.toggle.querySelector("i").textContent = audio.paused ? "play_arrow" : "pause";
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = audio.paused ? "paused" : "playing";
    for (const [which, text] of Object.entries(statuses)) setStatus(which, text);
  };

  async function fetchQueue(which) {
    const response = await fetch(`/player/queue/${encodeURIComponent(which)}`, {credentials: "same-origin"});
    if (response.redirected) { location.assign(response.url); return null; }  // logged out
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || response.status);
    return data;
  }

  // a source's tracks; the pulse may first have to be made (the server queues it): asked again until it's there
  async function load(which) {
    for (let i = 0; i < 40; i++) {
      const data = await fetchQueue(which);
      if (!data) return null;
      if (data.message) { setStatus(which, data.message); return null; }
      if (!data.pending) return data;
      setStatus(which, say("pending"));
      await sleep(3000);
    }
    return null;
  }

  // a source from its start, or from one of its tracks (`startId`); the one already playing pauses or resumes
  async function start(which, startId) {
    if (source && String(source.which) === which && (!startId || (current() && current().id === startId))) {
      if (audio.paused) audio.play(); else audio.pause();
      return;
    }
    report();
    try {
      const data = await load(which);
      if (!data || !data.tracks.length) return;
      source = {which, name: data.kind === "pulse" ? say("pulse") : data.name, version: data.version,
        chosen: data.kind === "set"};
      // the pulse after something else: spaced from what just played (still `tracks` here)
      let fresh = which === "pulse" ? data.tracks.filter((t) => !heard.has(t.id)) : data.tracks;
      if (!fresh.length) fresh = data.tracks;
      tracks = which === "pulse" && current() ? spaced(fresh, data.artist_gap || 0) : fresh;
      index = Math.max(0, startId ? tracks.findIndex((t) => t.id === startId) : 0);
      if (which === "pulse") setStatus("pulse", data.description || "");
      playCurrent();
    } catch (e) {
      setStatus(which, `${say("error")} ${e.message || e}`);
    }
  }

  // `autoplay` false: the track only made ready (a list restored, or filled while nothing played)
  function playCurrent(autoplay = true) {
    const t = current();
    if (!t) return;
    heard.add(t.id);
    played = 0; lastTime = 0;
    if (preloaded === t.id) {  // the spare has it loaded: it takes over at once
      const old = audio;
      audio = spare();
      old.pause();
      old.removeAttribute("src");
      old.load();  // lets the finished file go
    } else {
      audio.src = stream(t.id);
    }
    preloaded = null;
    if (autoplay) audio.play().catch(() => {});  // a refused autoplay leaves the bar paused, ready for a tap
    bar.hidden = false;
    els.cover.src = `/player/cover/${encodeURIComponent(t.id)}`;
    els.title.textContent = t.title;
    els.artist.textContent = t.artist;
    els.source.querySelector("span").textContent = source.name;
    els.source.querySelector("i").textContent = isPulse() ? "pulses" : "queue_music";
    els.duration.textContent = clock(t.duration || 0);
    showRepeat();
    bar.querySelectorAll("[data-rate]").forEach((b) => { b.dataset.track = t.id; });
    showRating();
    if (isPulse()) setStatus("pulse", say("now", {track: `${t.artist} — ${t.title}`}));
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({title: t.title, artist: t.artist, album: t.album,
        artwork: [{src: els.cover.src, sizes: "600x600"}]});
    }
    keep();
    showQueue();
  }

  // who a track is by when keeping artists apart: the first credited one (as kurai/artists.py on the server; Navidrome
  // joins a track's artists with « • », and «feat.»/«(ft. …)» guests don't make another artist)
  const artistKey = (name) => (name || "").split(" • ")[0]
      .replace(/(?:\s+|\s*[\(\[]\s*)(?:feat\.|ft\.|(?:feat|ft|featuring)(?=\s|$))\s*.*$/i, "").trim()
      .toLowerCase();

  const stream = (id) => `/player/stream/${encodeURIComponent(id)}`;

  // the track that plays after this one if nothing changes (the pulse's next version is asked earlier: next())
  const following = () => (repeat === "one" ? null
    : tracks[index + 1] || (repeat === "all" && !isPulse() && tracks.length > 1 ? tracks[0] : null));

  // near the end: the spare loads what follows (once; a list changed since only means it isn't used)
  function preload() {
    const t = following();
    if (!t || preloaded === t.id) return;
    preloaded = t.id;
    const el = spare();
    el.src = stream(t.id);
    el.load();
  }

  // the current track gave way (next, the end, another source, the page closed): played on or skipped, told to the
  // server, which counts the play and rebuilds the pulse when it's due
  // a 👎 is a skip however long the track played
  function report(disliked = false) {
    const t = current();
    if (t) count();  // up to now, whenever the last «timeupdate» came
    if (!t || (played < 1 && !disliked)) return;
    const seconds = Math.round(played);
    played = 0;
    post("/player/event", {track_id: t.id, seconds, disliked, chosen: !!(t.added || source.chosen)}, true)
      .then((r) => (r.ok ? r.json() : {}))
      .then((r) => { if (r.rebuilding && isPulse()) followRebuild(); })
      .catch(() => {});
  }

  function next(disliked = false) {
    report(disliked);
    index += 1;
    if (isPulse() && index >= tracks.length - 3) refresh();  // the pulse never ends: its next version is asked early
    if (index < tracks.length) playCurrent();
    else if (isPulse()) refresh().then(() => { if (index < tracks.length) playCurrent(); });
    else if (repeat === "all" && tracks.length) { index = 0; playCurrent(); }
    else if (source.chosen) { index = tracks.length - 1; start("pulse"); }  // a chosen set over: on into the pulse
    else { audio.pause(); index = tracks.length - 1; }
  }

  // «Не сейчас»: this track doesn't fit the moment — skip without prejudice (no 👎, no scrobble, no taste signal)
  function notNow() {
    const t = current();
    if (!t) return;
    count();
    const seconds = Math.round(played);
    played = 0;
    post("/player/event", {track_id: t.id, seconds, not_now: true, chosen: !!(t.added || source.chosen)}, true)
        .catch(() => {});
    next();  // report() inside next() is a no-op now (played reset)
  }

  // back: to this track's start when it has played a few seconds (as players do), else the one before
  function previous() {
    if (!current()) return;
    if (audio.currentTime > 3 || index === 0) { seek(0); return; }
    report();
    index -= 1;
    playCurrent();
  }

  // the track over: again (repeat one: it counts as a play), or the next
  function ended() {
    if (repeat === "one" && current()) {
      report();
      played = 0; lastTime = 0;
      audio.currentTime = 0;
      audio.play().catch(() => {});
      return;
    }
    next();
  }

  // off → the playlist → the track; the pulse can't be repeated as a whole (it never ends): off → the track
  const showRepeat = () => {
    const shown = isPulse() && repeat === "all" ? "off" : repeat;
    els.repeat.classList.toggle("on", shown !== "off");
    els.repeat.setAttribute("aria-pressed", shown !== "off" ? "true" : "false");
    els.repeat.querySelector("i").textContent = shown === "one" ? "repeat_one" : "repeat";
    return shown;
  };
  function cycleRepeat() {
    const shown = showRepeat();
    repeat = shown === "off" ? (isPulse() ? "one" : "all") : shown === "all" ? "one" : "off";
    store.set("kurai.repeat", repeat);
    note(strings[{off: "repeatOff", all: "repeatAll", one: "repeatOne"}[showRepeat()]]);
  }
  showRepeat();

  // the new version's tracks in its order, but none by an artist of the last `gap` tracks played (the current one
  // too: the server doesn't know it yet) while another is left — as the server spaces a version within itself
  function spaced(upcoming, gap) {
    if (gap <= 0) return upcoming;
    const artist = (t) => t.artist_key || artistKey(t.artist);
    const recent = tracks.slice(Math.max(0, index + 1 - gap), index + 1).map(artist);
    const rest = upcoming.slice(), out = [];
    while (rest.length) {
      const last = recent.concat(out.map(artist)).slice(-gap);
      const i = Math.max(0, rest.findIndex((t) => !last.includes(artist(t))));
      out.push(rest.splice(i, 1)[0]);
    }
    return out;
  }

  // the pulse's newest version: its tracks not yet heard become the ones after the current track
  async function refresh() {
    if (!isPulse()) return false;
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        const data = await fetchQueue("pulse");
        if (!data || !data.tracks || data.version === source.version) return false;
        const now = current();
        const pending = tracks.slice(index + 1).filter((t) => t.added);  // what the user put in plays first
        const upcoming = data.tracks.filter((t) => !heard.has(t.id) && (!now || t.id !== now.id)
          && !pending.some((p) => p.id === t.id));
        tracks = tracks.slice(0, index + 1).concat(pending, spaced(upcoming, data.artist_gap || 0));
        keep();
        showQueue();
        source.version = data.version;
        setStatus("pulse", say("updated", {t: new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}));
        return true;
      } catch {
        return false;
      } finally {
        refreshing = null;
      }
    })();
    return refreshing;
  }

  // after a report that queued a rebuild: the new version usually comes within seconds
  async function followRebuild() {
    for (const wait of [3000, 4000, 6000, 10000, 15000]) {
      await sleep(wait);
      if (await refresh()) return;
    }
  }
  setInterval(() => { if (isPulse() && !audio.paused) refresh(); }, 30000);

  // ---------- the list: kept on this device, filled from the search, shown in «Очередь», saved as a playlist ----------
  const KEEP = "kurai.player";
  let keptAt = 0;
  function keep() {
    if (!source || !tracks.length) { store.set(KEEP, ""); return; }
    store.set(KEEP, JSON.stringify({source, tracks, index, position: Math.floor(audio.currentTime || 0)}));
  }
  function restore() {
    let saved = null;
    try { saved = JSON.parse(store.get(KEEP) || "null"); } catch { /* a broken one is forgotten */ }
    if (!saved || !saved.source || !Array.isArray(saved.tracks) || !saved.tracks.length) return;
    ({source, tracks} = saved);
    index = Math.min(Math.max(0, saved.index | 0), tracks.length - 1);
    playCurrent(false);
    if (saved.position > 0) {
      audio.addEventListener("loadedmetadata", () => seek(saved.position),
        {once: true});
    }
  }

  // a chosen set (or the list itself) gets them at its end; the pulse or a playlist right after the current track and
  // what was added before, then goes on; nothing in the player: the list is made of them, ready, not playing
  function add(items) {
    const fresh = items.map((t) => ({...t, added: true}));
    if (!fresh.length) return;
    if (!source || !current()) {
      source = {which: "queue", name: say("queue"), version: "", chosen: true};
      tracks = fresh; index = 0;
      playCurrent(false);
    } else if (source.chosen) {
      tracks = tracks.concat(fresh);
      source.which = "queue"; source.name = say("queue");
      els.source.querySelector("span").textContent = source.name;
    } else {
      let at = index + 1;
      while (at < tracks.length && tracks[at].added) at += 1;
      tracks.splice(at, 0, ...fresh);
    }
    note(say("added", {n: fresh.length}));
    keep();
    showQueue();
    syncButtons();
  }

  // what the list shows: a chosen list whole (the heard ones dimmed), else from the current track: the added ones,
  // then the pulse's or the playlist's next
  function showQueue() {
    if (!els.queue) return;
    const list = els.queueList;
    list.replaceChildren();
    $("player-queue-empty").hidden = !!tracks.length;
    $("player-queue-source").textContent = source ? source.name : "";
    $("player-queue-what").textContent = !source ? "" : source.chosen ? say("after")
      : say("then", {name: source.name});
    const from = source && source.chosen ? 0 : index;
    let heading = "";
    tracks.forEach((t, i) => {
      if (i < from) return;
      const part = i <= index || source.chosen ? "" : t.added ? say("next") : say("then", {name: source.name});
      if (part && part !== heading) {
        const h = document.createElement("li");
        h.className = "player-queue-part small-text";
        h.textContent = heading = part;
        list.append(h);
      }
      const li = document.createElement("li");
      li.className = `player-queue-item${i === index ? " current" : i < index ? " heard" : ""}`;
      const play = document.createElement("button");
      play.type = "button"; play.className = "player-queue-play"; play.dataset.jump = String(i);
      const title = document.createElement("b"), who = document.createElement("span");
      title.textContent = t.title;
      who.className = "small-text"; who.textContent = t.artist + (t.album ? ` · ${t.album}` : "");
      play.append(title, who);
      const remove = document.createElement("button");
      remove.type = "button"; remove.className = "circle transparent small"; remove.dataset.remove = String(i);
      remove.title = say("remove");
      remove.setAttribute("aria-label", say("remove"));
      remove.innerHTML = "<i>close</i>";
      li.append(play, remove);
      list.append(li);
    });
    const now = list.querySelector(".current");
    if (now && els.queue.open) now.scrollIntoView({block: "nearest"});
  }

  function jump(i) {
    if (i === index || !tracks[i]) return;
    report();
    index = i;
    playCurrent();
  }

  // out of the list; the one playing gives way to the next (the list ending: the player stops)
  function remove(i) {
    if (!tracks[i]) return;
    if (i !== index) {
      tracks.splice(i, 1);
      if (i < index) index -= 1;
      keep(); showQueue();
      return;
    }
    const wasPlaying = !audio.paused;
    report();
    tracks.splice(i, 1);
    if (!tracks.length) { clear(); return; }
    index = Math.min(index, tracks.length - 1);
    playCurrent(wasPlaying);
  }

  // «Очистить»: a chosen list is emptied and the player stops; the pulse or a playlist only loses what was added
  function clear() {
    if (source && !source.chosen) {
      tracks = tracks.filter((t, i) => i <= index || !t.added);
      keep(); showQueue();
      return;
    }
    report();
    elements.forEach((el) => { el.pause(); el.removeAttribute("src"); });
    preloaded = null;
    source = null; tracks = []; index = 0;
    bar.hidden = true;
    keep(); showQueue(); syncButtons();
  }

  // «Сохранить»: a chosen list whole; of the pulse or a playlist, the current track and what was added
  async function saveQueue(form) {
    const name = form.elements.name.value.trim();
    const picked = !source ? [] : source.chosen ? tracks
      : tracks.filter((t, i) => i === index || (i > index && t.added));
    if (!name || !picked.length) return;
    try {
      const response = await post("/player/save", {name, track_ids: picked.map((t) => t.id)});
      const answer = await response.json();
      if (!response.ok) throw new Error(answer.detail || response.status);
      form.reset();
      note(say("saved", {name: answer.name}));
    } catch (e) {
      const toast = $("player-error");
      toast.textContent = String(e.message || e);
      if (typeof ui === "function") ui("#player-error");
    }
  }

  const openQueue = () => { if (els.queue && !els.queue.open) { showQueue(); els.queue.showModal(); } };
  if (els.queue) {
    $("player-queue-open").addEventListener("click", openQueue);
    $("player-queue-close").addEventListener("click", () => els.queue.close());
    $("player-queue-clear").addEventListener("click", clear);
    els.queue.addEventListener("click", (e) => {
      if (e.target === els.queue) { els.queue.close(); return; }  // the backdrop
      const jumpTo = e.target.closest("[data-jump]"), out = e.target.closest("[data-remove]");
      if (jumpTo) jump(Number(jumpTo.dataset.jump));
      if (out) remove(Number(out.dataset.remove));
    });
    // its own submit, before kurai.js would take it as a page's form
    $("player-queue-save").addEventListener("submit", (e) => { e.preventDefault(); saveQueue(e.target); });
  }
  document.addEventListener("click", async (e) => {
    const button = e.target.closest("[data-add]");
    if (!button || button.disabled) return;
    try {
      const data = await fetchQueue(button.dataset.add);
      if (data && data.tracks) add(data.tracks);
    } catch (err) {
      const toast = $("player-error");
      toast.textContent = String(err.message || err);
      if (typeof ui === "function") ui("#player-error");
    }
  });

  // ---------- the audio elements: only the playing one's events count (the spare's error only drops what it loaded) ----------
  const on = (type, handler) => elements.forEach((el) => el.addEventListener(type, (e) => {
    if (e.target === audio) handler(e);
  }));
  on("timeupdate", () => {
    count();
    const t = audio.currentTime;
    const d = audio.duration || (current() && current().duration) || 0;
    if (Math.abs(t - keptAt) >= 10) {  // now and then: where it is (a reload resumes there; the lock screen shows it)
      keptAt = t;
      keep();
      if ("mediaSession" in navigator && Number.isFinite(audio.duration) && audio.duration > 0) {
        try {
          navigator.mediaSession.setPositionState({duration: audio.duration, position: Math.min(t, audio.duration),
            playbackRate: audio.playbackRate});
        } catch { /* a browser without positions */ }
      }
    }
    if (d && d - t < PRELOAD) preload();
    els.time.textContent = clock(t);
    if (d && document.activeElement !== els.seek) els.seek.value = String(Math.round((t / d) * 100));
  });
  on("seeked", () => { lastTime = audio.currentTime; });
  on("playing", () => { failures = 0; });
  on("ended", ended);
  on("play", syncButtons);
  on("pause", syncButtons);
  elements.forEach((el) => el.addEventListener("error", () => { if (el !== audio) preloaded = null; }));
  on("error", () => {
    // a file that won't play (a format the browser lacks, a missing file): the next one, but not round and round
    if (!source || !current()) return;
    failures += 1;
    setStatus(String(source.which), `${say("error")} ${current().artist} — ${current().title}`);
    if (failures < 3) setTimeout(() => next(), 1500);
  });

  // ---------- ♥ and 👎: told to the server (Navidrome too), every pair of the same track kept in step; a 👎 on what
  // plays skips it ----------
  const ratings = {};  // track id -> {liked, disliked}, as last known (the queue's, then the answers')
  function showRating() {
    document.querySelectorAll("[data-rate][data-track]").forEach((b) => {
      const known = ratings[b.dataset.track] || (current() && current().id === b.dataset.track ? current() : null);
      if (!known) return;
      const on = b.dataset.rate === "like" ? known.liked : known.disliked;
      b.classList.toggle("on", !!on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  async function rate(button) {
    const id = button.dataset.track;
    const value = button.classList.contains("on") ? "none" : button.dataset.rate;
    try {
      const response = await post("/player/rate", {track_id: id, value});
      const answer = await response.json();
      if (!response.ok) throw new Error(answer.detail || response.status);
      ratings[id] = answer;
      tracks.forEach((t) => { if (t.id === id) Object.assign(t, answer); });
      showRating();
      if (value === "dislike" && current() && current().id === id) next(true);
    } catch (e) {
      const toast = $("player-error");
      toast.textContent = String(e.message || e);
      if (typeof ui === "function") ui("#player-error");
    }
  }
  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-rate]");
    if (button && button.dataset.track) rate(button);
  });

  // ---------- the bar's buttons, the page's buttons, the lock screen ----------
  els.toggle.addEventListener("click", () => { if (audio.paused) audio.play(); else audio.pause(); });
  els.next.addEventListener("click", () => next());
  if (els.notNow) els.notNow.addEventListener("click", notNow);
  els.prev.addEventListener("click", previous);
  els.repeat.addEventListener("click", cycleRepeat);
  els.seek.addEventListener("change", () => {
    const d = audio.duration || 0;
    if (d) seek((Number(els.seek.value) / 100) * d);
  });
  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-play]");
    if (button && !button.disabled) start(button.dataset.play, button.dataset.start);
  });
  if ("mediaSession" in navigator) {
    navigator.mediaSession.setActionHandler("play", () => audio.play());
    navigator.mediaSession.setActionHandler("pause", () => audio.pause());
    navigator.mediaSession.setActionHandler("seekto", (d) => { if (audio.duration) seek(d.seekTime); });
    navigator.mediaSession.setActionHandler("nexttrack", () => next());
    navigator.mediaSession.setActionHandler("previoustrack", previous);
  }

  // ---------- the keys, as in other web players; by the key's place (e.code), so the layout doesn't matter ----------
  const typing = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  const toggle = () => {
    if (current()) { if (audio.paused) audio.play(); else audio.pause(); return; }
    const first = document.querySelector("#page [data-play]");  // nothing yet: the page's first play button
    if (first) first.click();
  };
  const seekBy = (s) => {
    if (current() && audio.duration) seek(Math.max(0, Math.min(audio.duration - 1, audio.currentTime + s)));
  };
  const setVolume = (v) => {
    const value = Math.round(Math.max(0, Math.min(1, v)) * 100) / 100;
    elements.forEach((el) => { el.muted = false; el.volume = value; });
    store.set("kurai.volume", String(value));
    note(say("volume", {n: Math.round(audio.volume * 100)}));
  };
  const rateCurrent = (kind) => {
    const button = bar.querySelector(`[data-rate="${kind}"]`);
    if (current() && button && button.dataset.track) rate(button);
  };
  const showKeys = () => { if (els.keys && !els.keys.open) els.keys.showModal(); };
  // a click outside the window closes it too: by where it landed (the dimmed page behind a modal window gets no
  // clicks of its own, and Beer CSS hides the backdrop's)
  const outside = (dialog, e) => {
    const r = dialog.getBoundingClientRect();
    return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
  };
  document.addEventListener("click", (e) => {
    if (e.target.closest(".player-keys-open")) { showKeys(); return; }
    if (els.keys && els.keys.open && (e.target.closest("[data-close-keys]") || outside(els.keys, e))) els.keys.close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.isComposing || typing(e.target)) return;
    if (e.key === "?" || (e.code === "Slash" && e.shiftKey)) { e.preventDefault(); showKeys(); return; }
    if (document.querySelector("dialog[open]")) return;  // a dialog's own keys (Esc, Tab) come first
    const playing = !!current();
    const keys = {
      Space: toggle, KeyK: toggle,
      ArrowRight: () => (e.shiftKey ? next() : seekBy(10)), ArrowLeft: () => (e.shiftKey ? previous() : seekBy(-10)),
      KeyN: () => next(), KeyP: previous,
      ArrowUp: () => setVolume(audio.volume + 0.1), ArrowDown: () => setVolume(audio.volume - 0.1),
      KeyM: () => {
        const muted = !audio.muted;
        elements.forEach((el) => { el.muted = muted; });
        note(muted ? strings.muted : say("volume", {n: Math.round(audio.volume * 100)}));
      },
      KeyL: () => rateCurrent("like"), KeyD: () => rateCurrent("dislike"), KeyR: cycleRepeat,
      KeyQ: openQueue,
    };
    const action = keys[e.code];
    // with nothing playing, only «play» means anything: the arrows still scroll the page
    if (!action || (!playing && !["Space", "KeyK", "KeyQ"].includes(e.code))) return;
    if (e.code === "Space" && e.target.closest && e.target.closest("button, a, summary")) e.target.blur();
    e.preventDefault();
    action();
  });
  document.addEventListener("kurai:page", syncButtons);
  window.addEventListener("pagehide", () => { report(); keep(); });
  on("pause", keep);
  restore();
})();

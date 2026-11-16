// The web player: plays «Моя волна» or a playlist from the server (/player/…) in the bar at the bottom, which stays
// while the pages change (kurai.js swaps only the page). Each track that gives way to the next is reported, played on or
// skipped; the wave is rebuilt from that on the server, and the player takes the new version's tracks as the next ones
// (asking after each report and every half a minute), without a gap. Buttons with data-play="wave" or a playlist's id
// start it (data-start: from that track of it, as in «История»), or pause and resume what they started; data-play
// "artist:…", "album:…" or "track:…" is a set chosen in the library search: played to its end, then on into the wave,
// and a skip of what was chosen isn't told as one (a 👎 is). data-add (the same values) puts a set into the player's
// list instead: the end of a chosen set, or right after the current track of the wave or a playlist (it plays next,
// then they go on). The list («Очередь», from the bar or Q) shows what plays and what comes; a track plays on a tap,
// ✕ takes it out, «Сохранить» keeps it as the user's own playlist. The list is kept on this device (a reload finds it
// paused where it was). ♥ and 👎 (data-rate, data-track) rate a track. Repeat (off, the playlist, the track; the wave has no end, so no playlist
// repeat there), the previous track, the volume and the keys (the «Клавиши» dialog lists them) are the player's too.
// The phone's lock screen and headphones control it as well (Media Session).
(() => {
  const $ = (id) => document.getElementById(id);
  const bar = $("player"), audio = $("player-audio");
  if (!bar || !audio) return;
  const els = {cover: $("player-cover"), title: $("player-title"), artist: $("player-artist"), source: $("player-source"),
    toggle: $("player-toggle"), next: $("player-next"), prev: $("player-prev"), seek: $("player-seek"),
    time: $("player-time"), duration: $("player-duration"), repeat: $("player-repeat"), keys: $("player-keys"),
    queue: $("player-queue"), queueList: $("player-queue-list")};
  const store = {  // per-device remembering; private windows may refuse it
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* not remembered */ } },
  };
  const say = (key, values = {}) => Object.entries(values).reduce((s, [k, v]) => s.replace(`{${k}}`, v), audio.dataset[key]);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const post = (url, data, keepalive = false) => fetch(url, {method: "POST", keepalive, credentials: "same-origin",
    headers: {"X-CSRF-Token": document.body.dataset.csrf, "Content-Type": "application/json"},
    body: JSON.stringify(data)});
  const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  let source = null;  // {which: "wave", a playlist's id, a chosen set or "queue", name, version, chosen}
  let tracks = [], index = 0;  // a track the user put in the list has `added`
  let played = 0, lastTime = 0;  // seconds the current track really played (seeking doesn't count)
  let failures = 0;
  const heard = new Set();  // this session's tracks: a new version of the wave doesn't bring them back
  const statuses = {};  // what the player says about each source (shown wherever the page has its place)
  let refreshing = null;
  const current = () => tracks[index];
  const isWave = () => source && source.which === "wave";
  const REPEATS = ["off", "all", "one"];
  let repeat = REPEATS.includes(store.get("kurai.repeat")) ? store.get("kurai.repeat") : "off";
  const volume = Number(store.get("kurai.volume"));
  if (volume >= 0 && volume <= 1 && store.get("kurai.volume") !== null) audio.volume = volume;

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
    for (const [which, text] of Object.entries(statuses)) setStatus(which, text);
  };

  async function fetchQueue(which) {
    const response = await fetch(`/player/queue/${encodeURIComponent(which)}`, {credentials: "same-origin"});
    if (response.redirected) { location.assign(response.url); return null; }  // logged out
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || response.status);
    return data;
  }

  // a source's tracks; the wave may first have to be made (the server queues it): asked again until it's there
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
      source = {which, name: data.kind === "wave" ? say("wave") : data.name, version: data.version,
        chosen: data.kind === "set"};
      tracks = which === "wave" ? data.tracks.filter((t) => !heard.has(t.id)) : data.tracks;
      if (!tracks.length) tracks = data.tracks;
      index = Math.max(0, startId ? tracks.findIndex((t) => t.id === startId) : 0);
      if (which === "wave") setStatus("wave", data.description || "");
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
    audio.src = `/player/stream/${encodeURIComponent(t.id)}`;
    if (autoplay) audio.play().catch(() => {});  // a refused autoplay leaves the bar paused, ready for a tap
    bar.hidden = false;
    els.cover.src = `/player/cover/${encodeURIComponent(t.id)}`;
    els.title.textContent = t.title;
    els.artist.textContent = t.artist;
    els.source.querySelector("span").textContent = source.name;
    els.source.querySelector("i").textContent = isWave() ? "waves" : "queue_music";
    els.duration.textContent = clock(t.duration || 0);
    showRepeat();
    bar.querySelectorAll("[data-rate]").forEach((b) => { b.dataset.track = t.id; });
    showRating();
    if (isWave()) setStatus("wave", say("now", {track: `${t.artist} — ${t.title}`}));
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({title: t.title, artist: t.artist, album: t.album,
        artwork: [{src: els.cover.src, sizes: "600x600"}]});
    }
    keep();
    showQueue();
  }

  // the current track gave way (next, the end, another source, the page closed): played on or skipped, told to the
  // server, which counts the play and rebuilds the wave when it's due
  // a 👎 is a skip however long the track played
  function report(disliked = false) {
    const t = current();
    if (!t || (played < 1 && !disliked)) return;
    const seconds = Math.round(played);
    played = 0;
    post("/player/event", {track_id: t.id, seconds, disliked, chosen: !!(t.added || source.chosen)}, true)
      .then((r) => (r.ok ? r.json() : {}))
      .then((r) => { if (r.rebuilding && isWave()) followRebuild(); })
      .catch(() => {});
  }

  function next(disliked = false) {
    report(disliked);
    index += 1;
    if (isWave() && index >= tracks.length - 3) refresh();  // the wave never ends: its next version is asked early
    if (index < tracks.length) playCurrent();
    else if (isWave()) refresh().then(() => { if (index < tracks.length) playCurrent(); });
    else if (repeat === "all" && tracks.length) { index = 0; playCurrent(); }
    else if (source.chosen) { index = tracks.length - 1; start("wave"); }  // a chosen set over: on into the wave
    else { audio.pause(); index = tracks.length - 1; }
  }

  // back: to this track's start when it has played a few seconds (as players do), else the one before
  function previous() {
    if (!current()) return;
    if (audio.currentTime > 3 || index === 0) { audio.currentTime = 0; lastTime = 0; return; }
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

  // off → the playlist → the track; the wave can't be repeated as a whole (it never ends): off → the track
  const showRepeat = () => {
    const shown = isWave() && repeat === "all" ? "off" : repeat;
    els.repeat.classList.toggle("on", shown !== "off");
    els.repeat.setAttribute("aria-pressed", shown !== "off" ? "true" : "false");
    els.repeat.querySelector("i").textContent = shown === "one" ? "repeat_one" : "repeat";
    return shown;
  };
  function cycleRepeat() {
    const shown = showRepeat();
    repeat = shown === "off" ? (isWave() ? "one" : "all") : shown === "all" ? "one" : "off";
    store.set("kurai.repeat", repeat);
    note(audio.dataset[{off: "repeatOff", all: "repeatAll", one: "repeatOne"}[showRepeat()]]);
  }
  showRepeat();

  // the wave's newest version: its tracks not yet heard become the ones after the current track
  async function refresh() {
    if (!isWave()) return false;
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        const data = await fetchQueue("wave");
        if (!data || !data.tracks || data.version === source.version) return false;
        const now = current();
        const pending = tracks.slice(index + 1).filter((t) => t.added);  // what the user put in plays first
        const upcoming = data.tracks.filter((t) => !heard.has(t.id) && (!now || t.id !== now.id)
          && !pending.some((p) => p.id === t.id));
        tracks = tracks.slice(0, index + 1).concat(pending, upcoming);
        keep();
        showQueue();
        source.version = data.version;
        setStatus("wave", say("updated", {t: new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}));
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
  setInterval(() => { if (isWave() && !audio.paused) refresh(); }, 30000);

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
      audio.addEventListener("loadedmetadata", () => { audio.currentTime = saved.position; lastTime = saved.position; },
        {once: true});
    }
  }

  // a chosen set (or the list itself) gets them at its end; the wave or a playlist right after the current track and
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
  // then the wave's or the playlist's next
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

  // «Очистить»: a chosen list is emptied and the player stops; the wave or a playlist only loses what was added
  function clear() {
    if (source && !source.chosen) {
      tracks = tracks.filter((t, i) => i <= index || !t.added);
      keep(); showQueue();
      return;
    }
    report();
    audio.pause();
    audio.removeAttribute("src");
    source = null; tracks = []; index = 0;
    bar.hidden = true;
    keep(); showQueue(); syncButtons();
  }

  // «Сохранить»: a chosen list whole; of the wave or a playlist, the current track and what was added
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

  // ---------- the audio element ----------
  audio.addEventListener("timeupdate", () => {
    const t = audio.currentTime;
    if (!audio.paused && t > lastTime && t - lastTime < 2) played += t - lastTime;
    lastTime = t;
    if (Math.abs(t - keptAt) >= 10) { keptAt = t; keep(); }  // where it is, now and then (a reload resumes there)
    const d = audio.duration || (current() && current().duration) || 0;
    els.time.textContent = clock(t);
    if (d && document.activeElement !== els.seek) els.seek.value = String(Math.round((t / d) * 100));
  });
  audio.addEventListener("seeked", () => { lastTime = audio.currentTime; });
  audio.addEventListener("playing", () => { failures = 0; });
  audio.addEventListener("ended", ended);
  audio.addEventListener("play", syncButtons);
  audio.addEventListener("pause", syncButtons);
  audio.addEventListener("error", () => {
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
  els.prev.addEventListener("click", previous);
  els.repeat.addEventListener("click", cycleRepeat);
  els.seek.addEventListener("change", () => {
    const d = audio.duration || 0;
    if (d) audio.currentTime = (Number(els.seek.value) / 100) * d;
  });
  document.addEventListener("click", (e) => {
    const button = e.target.closest("[data-play]");
    if (button && !button.disabled) start(button.dataset.play, button.dataset.start);
  });
  if ("mediaSession" in navigator) {
    navigator.mediaSession.setActionHandler("play", () => audio.play());
    navigator.mediaSession.setActionHandler("pause", () => audio.pause());
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
    if (current() && audio.duration) audio.currentTime = Math.max(0, Math.min(audio.duration - 1, audio.currentTime + s));
  };
  const setVolume = (v) => {
    audio.muted = false;
    audio.volume = Math.round(Math.max(0, Math.min(1, v)) * 100) / 100;
    store.set("kurai.volume", String(audio.volume));
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
      KeyM: () => { audio.muted = !audio.muted; note(audio.muted ? audio.dataset.muted : say("volume", {n: Math.round(audio.volume * 100)})); },
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
  audio.addEventListener("pause", keep);
  restore();
})();

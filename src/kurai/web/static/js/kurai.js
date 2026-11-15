// The theme (accent colour, light / dark), the CSRF header for HTMX, the «Проверить» buttons, the apps' frames, the
// colour presets, and web push.
(() => {
  const body = document.body;

  // ---------- theme: Beer CSS builds the whole Material palette (with readable on-colours) from one colour ----------
  const applyTheme = async () => {
    // both libraries are modules: they may run after this script
    if (typeof ui !== "function" || typeof materialDynamicColors !== "function") return setTimeout(applyTheme, 50);
    const mode = body.dataset.mode;
    const dark = mode === "dark" || (mode === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
    ui("mode", dark ? "dark" : "light");
    if (body.dataset.accent) await ui("theme", body.dataset.accent);
  };
  applyTheme();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);

  // ---------- HTMX requests carry the session's form token ----------
  document.addEventListener("htmx:configRequest", (e) => { e.detail.headers["X-CSRF-Token"] = body.dataset.csrf; });

  const post = (url, data) => fetch(url, {
    method: "POST", headers: {"X-CSRF-Token": body.dataset.csrf, "Content-Type": "application/json"},
    body: data ? JSON.stringify(data) : null,
  });

  // ---------- «Проверить»: one real request, its time or its error ----------
  document.querySelectorAll("button.check").forEach((btn) => btn.addEventListener("click", async () => {
    const out = document.querySelector(`.check-result[data-for="${btn.dataset.check}"]`);
    out.className = "check-result small-text"; out.textContent = "…";
    try {
      const r = await (await post(`/admin/check/${btn.dataset.check}`)).json();
      out.classList.add(r.ok ? "ok" : "bad");
      out.textContent = `${r.ok ? "✓" : "✗"} ${r.detail} (${r.seconds} с)`;
    } catch (e) { out.classList.add("bad"); out.textContent = `✗ ${e}`; }
  }));

  // ---------- colour fields: the swatch shows the choice; presets fill the field ----------
  const showColour = (input) => { input.parentElement.style.background = input.value; };
  document.querySelectorAll(".swatch input[type=color]").forEach((i) => i.addEventListener("input", () => showColour(i)));
  document.querySelectorAll(".preset").forEach((b) => b.addEventListener("click", () => {
    const input = document.querySelector('input[type=color][name="ui.default_accent"]');
    if (input) { input.value = b.dataset.color; showColour(input); }
  }));

  // ---------- the apps' admin pages in one frame ----------
  document.querySelectorAll(".apps-tabs a").forEach((a) => a.addEventListener("click", () => {
    document.querySelectorAll(".apps-tabs a").forEach((x) => x.classList.toggle("active", x === a));
    document.getElementById("app-frame").src = `/admin/apps/${a.dataset.app}/`;
  }));

  // ---------- the service worker (installable app, push, «Поделиться») and push subscription ----------
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", {scope: "/"});
  const pushButton = document.getElementById("push-enable");
  const b64 = (s) => Uint8Array.from(atob((s + "=".repeat((4 - s.length % 4) % 4)).replace(/-/g, "+").replace(/_/g, "/")),
    (c) => c.charCodeAt(0));
  if (pushButton) pushButton.addEventListener("click", async () => {
    if (!("PushManager" in window)) { pushButton.textContent = "Этот браузер не поддерживает push"; return; }
    if (await Notification.requestPermission() !== "granted") { pushButton.textContent = "Разрешение не дано"; return; }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64(pushButton.dataset.key)});
    await post("/me/push", sub.toJSON());
    pushButton.textContent = "Push включён на этом устройстве";
  });
})();

// BallonsTranslator panel UI. The work happens in core.js.
"use strict";

const { entrypoints, host } = require("uxp");
const { app } = require("photoshop");
const bt = require("./core.js");

const STRINGS = {
  en: {
    offline: "BallonsTranslator is not running, or the panel connection is off. Turn it on in BallonsTranslator: Tools > Photoshop Bridge.",
    unpaired: "BallonsTranslator is running. Connect to it, then confirm in BallonsTranslator.",
    waitingConfirm: "Confirm the connection in BallonsTranslator...",
    noProject: "Connected. Open a project in BallonsTranslator.",
    connected: "Connected: {project}",
    unsaved: "Connected: {project} (unsaved changes in BallonsTranslator)",
    connect: "Connect",
    importHeading: "Import pages",
    current: "Current",
    all: "All",
    none: "None",
    optInpainted: "Clean plate (inpainted)",
    optMask: "Text mask (hidden)",
    optSource: "Source text layers (hidden)",
    untranslated: "{count} block(s) have no translation yet, so they got no text layer. Translate them in BallonsTranslator, then import again.",
    optStroke: "Text effects (stroke, shadow, glow)",
    optCenter: "Center text vertically",
    optSmart: "Widen narrow vertical boxes",
    import: "Import",
    sendHeading: "Send changes",
    sendNote: "Sends the active document's text, character styles (font, size, color, underline), alignment, opacity, rotation and box position to BallonsTranslator. Tracking, leading and layer effects stay as they are in BallonsTranslator.",
    send: "Send Changes",
    importing: "Importing {page}...",
    imported: "Imported {count} page(s).",
    missingFonts: "Fonts not found in Photoshop (default used): {fonts}",
    noPages: "Select pages to import.",
    noDocument: "Open a page imported from BallonsTranslator.",
    unchanged: "No changes to send.",
    applied: "Sent: {message}",
    blocked: "Not applied yet: {message}",
    reload: "Reload Panel"
  },
  ru: {
    offline: "BallonsTranslator не запущен или подключение панели выключено. Включите его в BallonsTranslator: Инструменты > Photoshop Bridge.",
    unpaired: "BallonsTranslator запущен. Подключитесь и подтвердите в BallonsTranslator.",
    waitingConfirm: "Подтвердите подключение в BallonsTranslator...",
    noProject: "Подключено. Откройте проект в BallonsTranslator.",
    connected: "Подключено: {project}",
    unsaved: "Подключено: {project} (в BallonsTranslator есть несохранённые изменения)",
    connect: "Подключить",
    importHeading: "Импорт страниц",
    current: "Текущая",
    all: "Все",
    none: "Снять",
    optInpainted: "Чистый слой (inpainted)",
    optMask: "Маска текста (скрыта)",
    optSource: "Слои с исходным текстом (скрыты)",
    untranslated: "У блоков без перевода ({count}) нет текстового слоя. Переведите их в BallonsTranslator и импортируйте снова.",
    optStroke: "Эффекты текста (обводка, тень, свечение)",
    optCenter: "Центрировать текст по вертикали",
    optSmart: "Расширять узкие вертикальные рамки",
    import: "Импортировать",
    sendHeading: "Отправка изменений",
    sendNote: "Отправляет в BallonsTranslator текст активного документа, стили символов (шрифт, размер, цвет, подчёркивание), выравнивание, непрозрачность, поворот и положение рамок. Трекинг, интерлиньяж и эффекты слоя остаются как в BallonsTranslator.",
    send: "Отправить изменения",
    importing: "Импорт {page}...",
    imported: "Импортировано страниц: {count}.",
    missingFonts: "Шрифты не найдены в Photoshop (взят стандартный): {fonts}",
    noPages: "Выберите страницы для импорта.",
    noDocument: "Откройте страницу, импортированную из BallonsTranslator.",
    unchanged: "Изменений нет.",
    applied: "Отправлено: {message}",
    blocked: "Пока не применено: {message}",
    reload: "Перезагрузить панель"
  }
};
const LANG = String(host.uiLocale || "en").toLowerCase().indexOf("ru") === 0 ? "ru" : "en";

function t(key, params) {
  let text = STRINGS[LANG][key] || STRINGS.en[key] || key;
  for (const name in params || {}) text = text.split("{" + name + "}").join(params[name]);
  return text;
}

const $ = (id) => document.getElementById(id);
const client = new bt.BridgeClient();
let pollTimer = null;
let busy = false;
let currentPages = "";
let currentPage = "";

function showMessage(text, kind) {
  const message = $("message");
  message.textContent = text || "";
  message.className = kind || "";
}

function setStatus(kind, text) {
  $("status-dot").className = kind;
  $("status-text").textContent = text;
}

function renderPages(project) {
  const key = project.pages.join("\n");
  currentPage = project.current_page || "";
  if (key === currentPages) return;
  const selected = new Set(selectedPages());
  const list = $("pages");
  list.innerHTML = "";
  for (const page of project.pages) {
    const box = document.createElement("sp-checkbox");
    box.textContent = page;
    box.dataset.page = page;
    if (selected.has(page) || (!currentPages && page === currentPage)) box.checked = true;
    list.appendChild(box);
  }
  currentPages = key;
}

function selectedPages() {
  return Array.from(document.querySelectorAll("#pages sp-checkbox"))
    .filter((box) => box.checked)
    .map((box) => box.dataset.page);
}

function selectPages(predicate) {
  for (const box of document.querySelectorAll("#pages sp-checkbox")) {
    box.checked = predicate(box.dataset.page);
  }
}

async function refresh() {
  if (busy) return;
  try {
    const status = await client.status();
    $("connect").classList.add("hidden");
    if (!status.project) {
      setStatus("warn", t("noProject"));
      $("project-section").classList.add("hidden");
      return;
    }
    const name = status.project.name;
    setStatus(status.project.unsaved ? "warn" : "ok", t(status.project.unsaved ? "unsaved" : "connected", { project: name }));
    renderPages(status.project);
    $("project-section").classList.remove("hidden");
  } catch (error) {
    $("project-section").classList.add("hidden");
    if (error.kind === "unpaired") {
      setStatus("warn", t("unpaired"));
      $("connect").classList.remove("hidden");
    } else {
      setStatus("error", t("offline"));
      $("connect").classList.add("hidden");
    }
  }
}

async function run(task) {
  if (busy) return;
  busy = true;
  try {
    await task();
  } catch (error) {
    showMessage(error.message || String(error), "error");
  } finally {
    busy = false;
    refresh();
  }
}

function onConnect() {
  run(async () => {
    setStatus("warn", t("waitingConfirm"));
    await client.pair();
    showMessage("");
  });
}

function onImport() {
  run(async () => {
    const pages = selectedPages();
    if (!pages.length) {
      showMessage(t("noPages"), "error");
      return;
    }
    const options = {
      inpainted: $("opt-inpainted").checked,
      mask: $("opt-mask").checked,
      originalText: $("opt-source").checked,
      effects: $("opt-stroke").checked,
      verticalCenter: $("opt-center").checked,
      smartBounds: $("opt-smart").checked
    };
    const missing = new Set();
    let untranslated = 0;
    for (const page of pages) {
      showMessage(t("importing", { page: page }));
      const result = await bt.importPage(client, page, options);
      result.missingFonts.forEach((font) => missing.add(font));
      untranslated += result.untranslated;
    }
    let text = t("imported", { count: pages.length });
    if (untranslated) text += "\n" + t("untranslated", { count: untranslated });
    if (missing.size) text += "\n" + t("missingFonts", { fonts: Array.from(missing).join(", ") });
    showMessage(text, untranslated ? "error" : "ok");
  });
}

function onSend() {
  run(async () => {
    const doc = app.activeDocument;
    if (!doc) {
      showMessage(t("noDocument"), "error");
      return;
    }
    const result = await bt.sendChanges(client, doc);
    if (result.state === "unchanged") showMessage(t("unchanged"));
    else if (result.state === "applied") showMessage(t("applied", { message: result.message }), "ok");
    else if (result.state === "blocked") showMessage(t("blocked", { message: result.message }), "error");
    else showMessage(result.message, "error");
  });
}

function setupTexts() {
  $("connect").textContent = t("connect");
  $("import-heading").textContent = t("importHeading");
  $("select-current").textContent = t("current");
  $("select-all").textContent = t("all");
  $("select-none").textContent = t("none");
  $("opt-inpainted").textContent = t("optInpainted");
  $("opt-mask").textContent = t("optMask");
  $("opt-source").textContent = t("optSource");
  $("opt-stroke").textContent = t("optStroke");
  $("opt-center").textContent = t("optCenter");
  $("opt-smart").textContent = t("optSmart");
  $("import").textContent = t("import");
  $("send-heading").textContent = t("sendHeading");
  $("send-note").textContent = t("sendNote");
  $("send").textContent = t("send");
}

setupTexts();
$("connect").addEventListener("click", onConnect);
$("import").addEventListener("click", onImport);
$("send").addEventListener("click", onSend);
$("select-current").addEventListener("click", () => selectPages((page) => page === currentPage));
$("select-all").addEventListener("click", () => selectPages(() => true));
$("select-none").addEventListener("click", () => selectPages(() => false));

$("version").textContent = "BallonsTranslator panel v" + (require("uxp").versions.plugin || "?");

entrypoints.setup({
  panels: {
    btPanel: {
      show() {
        refresh();
        if (!pollTimer) pollTimer = setInterval(refresh, 3000);
      },
      hide() {
        clearInterval(pollTimer);
        pollTimer = null;
      },
      menuItems: [{ id: "reload", label: t("reload") }],
      invokeMenu(id) {
        // Picks up panel files changed on disk without restarting Photoshop.
        if (id === "reload") location.reload();
      }
    }
  }
});

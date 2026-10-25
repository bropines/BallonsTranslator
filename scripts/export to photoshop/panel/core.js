// BallonsTranslator panel core: talks to the app and edits Photoshop documents.
// No UI here; main.js drives it. The page state stored in the PSD (XMP) and the
// update payload are shared with BallonTranslator_PS_Bridge.jsx, see
// scripts/export to photoshop/readme.md.
"use strict";

const { app, core, action, constants } = require("photoshop");
const { storage, xmp } = require("uxp");

// Must match PS_BRIDGE_PORTS in ballontranslator/utils/config.py and the
// network domains in manifest.json.
const PORTS = [19871, 19872, 19873, 19874, 19875];
const SERVER_NAME = "BallonsTranslatorBridge";
const TOKEN_KEY = "ballonstranslator.token";
const XMP_NS = "http://github.com/dmMaze/BallonsTranslator/ns/psbridge/1.0/";
const TRANSLATIONS_GROUP = "[BT] Translations";
const SOURCE_GROUP = "[BT] Source Text";

class BridgeError extends Error {
  // kind: "offline" | "unpaired" | "error"
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

// ---------------------------------------------------------------------------
// Connection to the app

// A plain file name for the temporary copy: no folders, no reserved characters.
function safeFileName(name) {
  const base = String(name).split(/[\\/]/).pop().replace(/[<>:"|?*\u0000-\u001f]/g, "_");
  return base.replace(/^\.+/, "") || "page.png";
}

class BridgeClient {
  constructor() {
    this.port = null;
    this.token = localStorage.getItem(TOKEN_KEY) || "";
  }

  url(path) {
    return "http://localhost:" + this.port + "/api/v1/" + path;
  }

  // Find the port the app listens on. Other programs may use these ports, so
  // only a response from the bridge server counts.
  async find() {
    for (const port of PORTS) {
      try {
        // No token here: it goes only to a server known to be the app.
        const response = await fetch("http://localhost:" + port + "/api/v1/status");
        if ((response.headers.get("server") || "").indexOf(SERVER_NAME) === 0) {
          this.port = port;
          return;
        }
      } catch (error) {
        // Nothing listens on this port.
      }
    }
    this.port = null;
    throw new BridgeError("offline", "BallonsTranslator is not reachable.");
  }

  async request(path, options) {
    if (this.port === null) await this.find();
    const headers = Object.assign({ "X-BT-Token": this.token }, (options && options.headers) || {});
    let response;
    try {
      response = await fetch(this.url(path), Object.assign({}, options, { headers: headers }));
    } catch (error) {
      this.port = null;
      throw new BridgeError("offline", "BallonsTranslator is not reachable.");
    }
    if (response.status === 401) {
      throw new BridgeError("unpaired", "Connect the panel to BallonsTranslator.");
    }
    if (!response.ok) {
      let message = "";
      try {
        message = (await response.json()).error;
      } catch (error) {
        // Not a JSON error body.
      }
      throw new BridgeError("error", message || "BallonsTranslator answered " + response.status + ".");
    }
    return response;
  }

  async json(path, options) {
    return (await this.request(path, options)).json();
  }

  // Ask the user in the app to allow this panel; waits for the answer.
  async pair() {
    if (this.port === null) await this.find();
    let response;
    try {
      response = await fetch(this.url("pair"), {
        method: "POST",
        // A custom header makes browsers preflight the request, which the
        // app never allows, so web pages cannot trigger pairing.
        headers: { "Content-Type": "application/json", "X-BT-Client": "photoshop-panel" },
        body: "{}"
      });
    } catch (error) {
      this.port = null;
      throw new BridgeError("offline", "BallonsTranslator is not reachable.");
    }
    const data = await response.json();
    if (!response.ok) throw new BridgeError("error", data.error || "The connection failed.");
    this.token = data.token;
    localStorage.setItem(TOKEN_KEY, this.token);
  }

  status() {
    return this.json("status");
  }

  page(name) {
    return this.json("page?name=" + encodeURIComponent(name));
  }

  // Returns {bytes, filename}, or null when the app has no such image yet.
  async image(page, kind) {
    try {
      const response = await this.request(
        "image?page=" + encodeURIComponent(page) + "&kind=" + kind
      );
      return {
        bytes: await response.arrayBuffer(),
        filename: safeFileName(response.headers.get("x-bt-filename") || page)
      };
    } catch (error) {
      if (error.kind === "error") return null;
      throw error;
    }
  }

  sendUpdates(payload) {
    return this.json("updates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
  }
}

// ---------------------------------------------------------------------------
// Page state stored in the document

async function readXmp(doc) {
  const result = await action.batchPlay([{
    _obj: "get",
    _target: [{ _property: "XMPMetadataAsUTF8" }, { _ref: "document", _id: doc.id }]
  }], {});
  return (result[0] && result[0].XMPMetadataAsUTF8) || "";
}

// Returns {version, project_path, page, blocks} or null for other documents.
async function readState(doc) {
  try {
    const meta = new xmp.XMPMeta(await readXmp(doc));
    const prop = meta.getProperty(XMP_NS, "state");
    if (!prop) return null;
    const state = JSON.parse(String(prop.value));
    if (state && state.version === 1 && state.project_path && state.page &&
        Array.isArray(state.blocks)) {
      return state;
    }
  } catch (error) {
    // No or unreadable XMP.
  }
  return null;
}

// Must run inside executeAsModal.
async function writeState(doc, state) {
  let meta;
  try {
    meta = new xmp.XMPMeta(await readXmp(doc));
  } catch (error) {
    meta = new xmp.XMPMeta();
  }
  xmp.XMPMeta.registerNamespace(XMP_NS, "btbridge");
  meta.setProperty(XMP_NS, "state", JSON.stringify(state));
  await action.batchPlay([{
    _obj: "set",
    _target: [{ _ref: "property", _property: "XMPMetadataAsUTF8" }, { _ref: "document", _id: doc.id }],
    to: { _obj: "document", XMPMetadataAsUTF8: meta.serialize() }
  }], {});
}

// ---------------------------------------------------------------------------
// Fonts

let fontIndex = null;

function cleanFontName(name) {
  return String(name || "").toLowerCase()
    .replace(/^[@vV][_\s-]/, "")
    .replace(/[\s\-_]+/g, "");
}

function fonts() {
  if (fontIndex) return fontIndex;
  fontIndex = { byPostScript: {}, byName: {}, byFamily: {}, all: [] };
  for (const font of app.fonts) {
    const entry = {
      postScriptName: font.postScriptName,
      name: font.name,
      family: font.family,
      style: font.style || ""
    };
    fontIndex.all.push(entry);
    fontIndex.byPostScript[cleanFontName(entry.postScriptName)] =
      fontIndex.byPostScript[cleanFontName(entry.postScriptName)] || entry;
    fontIndex.byName[cleanFontName(entry.name)] =
      fontIndex.byName[cleanFontName(entry.name)] || entry;
    const family = cleanFontName(entry.family);
    (fontIndex.byFamily[family] = fontIndex.byFamily[family] || []).push(entry);
  }
  return fontIndex;
}

function styleMatches(entry, words) {
  const text = (entry.style + " " + entry.postScriptName).toLowerCase();
  return words.some((word) => text.indexOf(word) !== -1);
}

const isBoldFace = (entry) => styleMatches(entry, ["bold", "black", "heavy"]);
const isItalicFace = (entry) => styleMatches(entry, ["italic", "oblique"]);
const PLAIN_STYLE = /^(regular|roman|normal|book|bold|italic|oblique|bold italic|bold oblique)$/i;

// The face with this boldness and slant, preferring the plain one (Regular
// over Light, Bold over Black); null when the family has none.
function familyFace(candidates, bold, italic) {
  const matching = candidates.filter((entry) => isBoldFace(entry) === bold && isItalicFace(entry) === italic);
  return matching.find((entry) => PLAIN_STYLE.test(entry.style.trim())) || matching[0] || null;
}

// Find the Photoshop font for an app font family; null when none fits.
function resolveFont(family, bold, italic) {
  if (!family) return null;
  const index = fonts();
  const wanted = cleanFontName(family);
  // A face name ("Arial Black") names its style. A family name can also be
  // its regular face's name ("Arial"): pick the face by bold and italic.
  const exact = index.byPostScript[wanted] || index.byName[wanted];
  if (exact && !index.byFamily[wanted]) return exact.postScriptName;

  let candidates = index.byFamily[wanted];
  if (!candidates) {
    const key = Object.keys(index.byFamily).find(
      (name) => name.indexOf(wanted) !== -1 || wanted.indexOf(name) !== -1
    );
    candidates = key ? index.byFamily[key] : null;
  }
  if (!candidates) return null;
  const pick =
    familyFace(candidates, bold, italic) ||
    candidates.find((entry) => styleMatches(entry, ["regular", "roman", "medium", "normal"])) ||
    candidates[0];
  return pick.postScriptName;
}

// The app's view of a Photoshop font: {family, bold, italic}, or null.
function fontInfo(postScriptName) {
  const entry = fonts().byPostScript[cleanFontName(postScriptName)];
  if (!entry) return null;
  return { family: entry.family, bold: isBoldFace(entry), italic: isItalicFace(entry) };
}

// ---------------------------------------------------------------------------
// Text layers

// Photoshop ends paragraphs with \r and stores Shift+Enter breaks as \u0003.
// Photoshop text has no \r\n, so its offsets stay the same.
function normalizeText(text) {
  return String(text).replace(/\r\n/g, "\n").replace(/[\r\u0003]/g, "\n");
}

// The app's text with Photoshop paragraph breaks, and a function that maps an
// offset in the app's text to the same place in the Photoshop text.
function photoshopText(text) {
  const source = String(text);
  const offsets = [];
  let result = "";
  for (let index = 0; index < source.length; index++) {
    offsets.push(result.length);
    const char = source[index];
    // "\r\n" is one break: its \n writes it.
    if (char === "\r" && source[index + 1] === "\n") continue;
    result += char === "\n" ? "\r" : char;
  }
  offsets.push(result.length);
  return {
    text: result,
    offset: (index) => offsets[Math.max(0, Math.min(index, source.length))]
  };
}

async function textKey(layer) {
  const result = await action.batchPlay([{
    _obj: "get",
    _target: [{ _property: "textKey" }, { _ref: "layer", _id: layer.id }]
  }], {});
  return result[0].textKey;
}

const IDENTITY = { xx: 1, xy: 0, yx: 0, yy: 1, tx: 0, ty: 0 };

// textKey lengths follow Preferences > Units > Type. Bare numbers (the box
// bounds) carry no unit; they use the unit of the layer's other lengths.
function typeUnit(key) {
  const sample = key.bounds && key.bounds.left;
  return sample && sample._unit ? sample._unit : "pointsUnit";
}

function lengthPx(value, resolution, bareUnit) {
  if (value === null || value === undefined) return 0;
  const unit = typeof value === "object" ? value._unit : bareUnit;
  const number = Number(typeof value === "object" ? value._value : value) || 0;
  if (unit === "pixelsUnit") return number;
  if (unit === "millimetersUnit") return number * resolution / 25.4;
  return number * resolution / 72;
}

function round2(value) {
  return Math.round(value * 100) / 100;
}

// Degrees in (-180, 180].
function normalizeAngle(angle) {
  const turned = ((angle % 360) + 360) % 360;
  return turned > 180 ? turned - 360 : turned;
}

// The paragraph box in document pixels: its unrotated frame [left, top,
// right, bottom] around the box center (like the app's text box), and the
// clockwise rotation. Moving the layer changes the click point; scaling and
// rotating change the transform.
function textGeometry(doc, key) {
  const unit = typeUnit(key);
  const px = (value) => lengthPx(value, doc.resolution, unit);
  const transform = key.transform || IDENTITY;
  const shape = key.textShape && key.textShape[0];
  const bounds = shape && shape.bounds ? shape.bounds : key.bounds;
  const left = px(bounds.left);
  const top = px(bounds.top);
  const right = px(bounds.right);
  const bottom = px(bounds.bottom);
  const localX = (left + right) / 2;
  const localY = (top + bottom) / 2;
  // Photoshop reads the click point in percent of the document size.
  const center = [
    key.textClickPoint.horizontal._value / 100 * doc.width +
      transform.xx * localX + transform.yx * localY + px(transform.tx || 0),
    key.textClickPoint.vertical._value / 100 * doc.height +
      transform.xy * localX + transform.yy * localY + px(transform.ty || 0)
  ];
  const width = (right - left) * Math.hypot(transform.xx, transform.xy);
  const height = (bottom - top) * Math.hypot(transform.yx, transform.yy);
  return {
    frame: [center[0] - width / 2, center[1] - height / 2, center[0] + width / 2, center[1] + height / 2],
    center: center,
    // Photoshop rotates clockwise on screen, like the app.
    angle: normalizeAngle(Math.atan2(transform.xy, transform.xx) * 180 / Math.PI)
  };
}

async function textFrame(doc, layer) {
  return textGeometry(doc, await textKey(layer)).frame;
}

// Later ranges list only the keys that differ from baseParentStyle.
function flattenStyle(style, depth) {
  if (!style || typeof style !== "object" || (depth || 0) > 16) return {};
  const own = Object.assign({}, style);
  const parent = own.baseParentStyle;
  delete own.baseParentStyle;
  return Object.assign(flattenStyle(parent, (depth || 0) + 1), own);
}

function colorRgb(color) {
  if (!color) return [0, 0, 0];
  if (color._obj === "RGBColor") return [color.red, color.grain, color.blue].map(Math.round);
  if (color._obj === "grayscale") return [0, 0, 0].map(() => Math.round(255 * (1 - color.gray / 100)));
  if (color._obj === "CMYKColor") {
    const white = 1 - color.black / 100;
    return [color.cyan, color.magenta, color.yellowColor].map((ink) => Math.round(255 * (1 - ink / 100) * white));
  }
  return [0, 0, 0];
}

const ALIGNMENTS = { left: 0, justifyLeft: 0, justifyAll: 0, center: 1, justifyCenter: 1, right: 2, justifyRight: 2 };

// The app's view of a range's font. `families` maps the Photoshop fonts used
// at import back to the app's family names, which may differ.
function runFont(style, families) {
  const postScriptName = style.fontPostScriptName || "";
  const info = fontInfo(postScriptName) ||
    { family: style.fontName || postScriptName, bold: false, italic: false };
  return {
    family: (families && families[postScriptName]) || info.family,
    bold: info.bold || Boolean(style.syntheticBold),
    italic: info.italic || Boolean(style.syntheticItalic)
  };
}

function sameRun(a, b) {
  return a.font.family === b.font.family && a.font.bold === b.font.bold &&
    a.font.italic === b.font.italic && Math.abs(a.font_size - b.font_size) < 0.05 &&
    a.underline === b.underline && String(a.color) === String(b.color);
}

function sameRuns(a, b) {
  return a.length === b.length &&
    a.every((run, index) => run.start === b[index].start && run.end === b[index].end && sameRun(run, b[index]));
}

// What the app can carry of a text layer, read from one textKey: the text,
// character runs in the update format (offsets into the \n text, sizes in
// image pixels), alignment and geometry.
function readText(doc, key, families) {
  const text = normalizeText(key.textKey || "");
  const unit = typeUnit(key);
  const transform = key.transform || IDENTITY;
  const ranges = (key.textStyleRange || []).slice().sort((a, b) => a.from - b.from);
  const shared = ranges.length ? flattenStyle(ranges[0].textStyle && ranges[0].textStyle.baseParentStyle) : {};
  const runs = [];
  let font = "";
  for (const range of ranges) {
    const start = runs.length ? runs[runs.length - 1].end : 0;
    // The last range usually ends one past the text (a hidden paragraph end).
    const end = Math.min(range.to, text.length);
    if (end <= start) continue;
    const style = Object.assign({}, shared, flattenStyle(range.textStyle));
    // impliedFontSize is the displayed size: it already includes the transform.
    const size = style.impliedFontSize
      ? lengthPx(style.impliedFontSize, doc.resolution, unit)
      : lengthPx(style.size, doc.resolution, unit) * Math.hypot(transform.yx, transform.yy);
    const run = {
      start: start,
      end: end,
      font: runFont(style, families),
      font_size: round2(size),
      color: colorRgb(style.color),
      underline: Boolean(style.underline && style.underline._value !== "underlineOff")
    };
    if (!runs.length) font = style.fontPostScriptName || "";
    const previous = runs[runs.length - 1];
    if (previous && sameRun(previous, run)) previous.end = end;
    else runs.push(run);
  }
  if (runs.length) runs[runs.length - 1].end = text.length;
  const paragraph = key.paragraphStyleRange && key.paragraphStyleRange[0];
  const align = paragraph ? flattenStyle(paragraph.paragraphStyle).align : null;
  const geometry = textGeometry(doc, key);
  return {
    text: text,
    font: font,
    runs: runs,
    alignment: align && align._value in ALIGNMENTS ? ALIGNMENTS[align._value] : null,
    frame: geometry.frame,
    angle: round2(geometry.angle)
  };
}

// The block's character runs as offsets into the Photoshop text, contiguous
// and covering it. Apps without per-run styles send none: one run from the
// block style.
function blockRuns(block, text) {
  const style = block.style;
  const sent = Array.isArray(block.runs) ? block.runs : [];
  const runs = sent
    .map((run) => Object.assign({}, run, { start: text.offset(run.start), end: text.offset(run.end) }))
    .filter((run) => run.end > run.start)
    .sort((a, b) => a.start - b.start);
  if (!runs.length) {
    runs.push({
      font_family: style.font_family, weight: style.weight, bold: style.bold, italic: style.italic,
      underline: style.underline, font_size: style.font_size, color: style.color, tracking: style.tracking
    });
  }
  runs.forEach((run, index) => {
    run.start = index ? runs[index - 1].end : 0;
  });
  runs[runs.length - 1].end = text.text.length;
  return runs.length > 1 ? runs.filter((run) => run.end > run.start) : runs;
}

function rgbColor(color) {
  const rgb = color || [0, 0, 0];
  return { _obj: "RGBColor", red: rgb[0], grain: rgb[1], blue: rgb[2] };
}

// A complete textStyle: a range without some key gets Photoshop's default,
// not the previous range's value.
function runTextStyle(doc, block, run) {
  const style = block.style;
  const toPt = 72 / doc.resolution;
  const bold = Boolean(run.bold) || Number(run.weight) >= 600;
  const italic = Boolean(run.italic);
  const family = run.font_family || style.font_family;
  const fontName = resolveFont(family, bold, italic);
  const face = fontName ? fontInfo(fontName) : null;
  const tracking = Number(run.tracking !== undefined ? run.tracking : style.tracking) || 0;
  const underline = run.underline !== undefined ? run.underline : style.underline;
  const textStyle = {
    _obj: "textStyle",
    size: { _unit: "pointsUnit", _value: (run.font_size || style.font_size) * toPt },
    color: rgbColor(run.color || style.color),
    tracking: Math.max(-1000, Math.min(10000, Math.round(tracking))),
    horizontalScale: 100,
    verticalScale: 100,
    baselineShift: { _unit: "pointsUnit", _value: 0 },
    // The app draws a missing bold or italic face synthetically; so does Photoshop.
    syntheticBold: bold && !(face && face.bold),
    syntheticItalic: italic && !(face && face.italic),
    underline: {
      _enum: "underline",
      _value: !underline ? "underlineOff"
        : style.vertical ? "underlineOnRightInVertical" : "underlineOnLeftInVertical"
    },
    autoLeading: true
  };
  if (style.leading > 0) {
    textStyle.autoLeading = false;
    textStyle.leading = { _unit: "pointsUnit", _value: style.leading * toPt };
  }
  if (fontName) textStyle.fontPostScriptName = fontName;
  return { textStyle: textStyle, family: family, missing: fontName ? null : family };
}

const STROKE_POSITIONS = { outside: "outsetFrame", inside: "insetFrame", center: "centeredFrame" };

function percent(value) {
  return { _unit: "percentUnit", _value: (value === undefined || value === null ? 1 : value) * 100 };
}

// Layer effects for the block's stroke, drop shadow and outer glow, sizes
// already in Photoshop pixels; null when it has none.
function blockEffects(style) {
  const effects = {};
  const stroke = style.stroke;
  if (stroke && stroke.width > 0) {
    effects.frameFX = {
      _obj: "frameFX", enabled: true, present: true, showInDialog: true,
      style: { _enum: "frameStyle", _value: STROKE_POSITIONS[stroke.position] || "outsetFrame" },
      paintType: { _enum: "frameFill", _value: "solidColor" },
      mode: { _enum: "blendMode", _value: "normal" },
      opacity: percent(stroke.opacity),
      size: { _unit: "pixelsUnit", _value: stroke.width },
      color: rgbColor(stroke.color)
    };
  }
  // Photoshop keeps Spread (percent) in chokeMatte with a pixel unit.
  const shadow = style.shadow;
  if (shadow) {
    effects.dropShadow = {
      _obj: "dropShadow", enabled: true, present: true, showInDialog: true,
      mode: { _enum: "blendMode", _value: "normal" },
      color: rgbColor(shadow.color),
      opacity: percent(shadow.opacity),
      useGlobalAngle: false,
      localLightingAngle: { _unit: "angleUnit", _value: shadow.angle || 0 },
      distance: { _unit: "pixelsUnit", _value: shadow.distance || 0 },
      chokeMatte: { _unit: "pixelsUnit", _value: shadow.spread || 0 },
      blur: { _unit: "pixelsUnit", _value: shadow.size || 0 }
    };
  }
  const glow = style.glow;
  if (glow) {
    effects.outerGlow = {
      _obj: "outerGlow", enabled: true, present: true, showInDialog: true,
      mode: { _enum: "blendMode", _value: "normal" },
      color: rgbColor(glow.color),
      opacity: percent(glow.opacity),
      chokeMatte: { _unit: "pixelsUnit", _value: glow.spread || 0 },
      blur: { _unit: "pixelsUnit", _value: glow.size || 0 }
    };
  }
  return Object.keys(effects).length ? effects : null;
}

// Setting layerEffects replaces the whole list, so merge into the effects
// the layer already has.
async function addLayerEffects(layer, effects) {
  const result = await action.batchPlay([{
    _obj: "get",
    _target: [{ _property: "layerEffects" }, { _ref: "layer", _id: layer.id }]
  }], {});
  const merged = Object.assign({}, (result[0] && result[0].layerEffects) || {}, { _obj: "layerEffects" });
  for (const name in effects) {
    delete merged[name + "Multi"];
    merged[name] = effects[name];
  }
  await action.batchPlay([{
    _obj: "set",
    _target: [{ _ref: "property", _property: "layerEffects" }, { _ref: "layer", _id: layer.id }],
    to: merged
  }], {});
}

// Rotate clockwise around a document point. Photoshop turns text around its
// own reference point, so then move the box center to where the pivot puts it.
async function rotateAround(doc, layer, angle, pivot) {
  const before = textGeometry(doc, await textKey(layer)).center;
  await layer.rotate(angle, constants.AnchorPosition.MIDDLECENTER);
  const after = textGeometry(doc, await textKey(layer)).center;
  const radians = angle * Math.PI / 180;
  const dx = before[0] - pivot[0];
  const dy = before[1] - pivot[1];
  const shift = [
    pivot[0] + dx * Math.cos(radians) - dy * Math.sin(radians) - after[0],
    pivot[1] + dx * Math.sin(radians) + dy * Math.cos(radians) - after[1]
  ];
  if (Math.abs(shift[0]) >= 0.01 || Math.abs(shift[1]) >= 0.01) {
    // translate() moves every selected layer; the new layer is the only one.
    await layer.translate(shift[0], shift[1]);
  }
}

// options: {effects, verticalCenter, smartBounds}. Returns {layer,
// missingFonts, families}; families maps the layer's Photoshop fonts to the
// app's family names.
async function createTextLayer(doc, block, options) {
  const style = block.style;
  const vertical = Boolean(style.vertical);
  const toPt = 72 / doc.resolution;
  let [left, top, width, height] = block.rect;
  // The app rotates the block around the center of its box.
  const pivot = [left + width / 2, top + height / 2];
  width = Math.max(width, 20);
  height = Math.max(height, 20);
  // Skinny boxes left by vertical source text are too narrow for horizontal
  // translations.
  if (!vertical && options.smartBounds && width < 70 && height > width * 1.5) {
    const wider = Math.max(Math.round(width * 1.8), 120);
    left = Math.max(0, left - (wider - width) / 2);
    width = wider;
    height = Math.max(Math.round(height * 1.05), 40);
  }
  const text = photoshopText(block.translation);
  const runs = blockRuns(block, text);
  const missingFonts = new Set();
  const ranges = runs.map((run) => {
    const made = runTextStyle(doc, block, run);
    if (made.missing) missingFonts.add(made.missing);
    run.family = made.family;
    return { _obj: "textStyleRange", from: run.start, to: run.end, textStyle: made.textStyle };
  });
  const fontSize = Math.max.apply(null, runs.map((run) => run.font_size || style.font_size));
  const align = ["left", "center", "right"][style.alignment] || "center";
  const orientation = { _enum: "orientation", _value: vertical ? "vertical" : "horizontal" };
  const points = (px) => ({ _unit: "pointsUnit", _value: px * toPt });
  const make = async (box) => {
    await action.batchPlay([{
      _obj: "make",
      _target: [{ _ref: "textLayer" }],
      using: {
        _obj: "textLayer",
        textKey: text.text,
        orientation: orientation,
        // Photoshop reads the click point in percent of the document size.
        textClickPoint: {
          _obj: "paint",
          horizontal: { _unit: "percentUnit", _value: box.left / doc.width * 100 },
          vertical: { _unit: "percentUnit", _value: box.top / doc.height * 100 }
        },
        textShape: [{
          _obj: "textShape",
          char: { _enum: "char", _value: "box" },
          orientation: orientation,
          bounds: { _obj: "rectangle", left: points(0), top: points(0), right: points(box.width), bottom: points(box.height) }
        }],
        textStyleRange: ranges,
        paragraphStyleRange: [{
          _obj: "paragraphStyleRange", from: 0, to: text.text.length,
          paragraphStyle: { _obj: "paragraphStyle", align: { _enum: "alignmentType", _value: align } }
        }]
      }
    }], {});
    return doc.activeLayers[0];
  };
  const box = { left: left, top: top, width: width, height: height };
  let layer = await make(box);
  if (options.verticalCenter) {
    // Paragraph boxes start lines at the top (columns at the right for
    // vertical text). Measure the text, then remake the box so the text sits
    // in the middle of the block. The spare room after the text keeps
    // Photoshop from hiding the last line when the leading is larger than
    // the ink.
    const ink = layer.boundsNoEffects;
    const spare = fontSize * 0.8;
    if (vertical) {
      const inset = left + width - ink.right;
      const inkWidth = ink.right - ink.left;
      const boxWidth = inset + inkWidth + spare;
      if (inkWidth > 0 && boxWidth < width) {
        await layer.delete();
        const right = left + width / 2 + inkWidth / 2 + inset;
        layer = await make({ left: right - boxWidth, top: top, width: boxWidth, height: height });
      }
    } else {
      const inset = ink.top - top;
      const inkHeight = ink.bottom - ink.top;
      const boxHeight = inset + inkHeight + spare;
      if (inkHeight > 0 && boxHeight < height) {
        await layer.delete();
        layer = await make({ left: left, top: top + (height - inkHeight) / 2 - inset, width: width, height: boxHeight });
      }
    }
  }
  if (typeof style.opacity === "number" && style.opacity < 1) {
    layer.opacity = Math.max(0, style.opacity) * 100;
  }
  const effects = options.effects ? blockEffects(style) : null;
  if (effects) await addLayerEffects(layer, effects);
  const angle = normalizeAngle(Number(block.angle) || 0);
  if (Math.abs(angle) >= 0.01) await rotateAround(doc, layer, angle, pivot);

  // Photoshop may merge equal ranges and uses its default font for missing
  // ones: map the fonts it used back by position.
  const families = {};
  for (const range of (await textKey(layer)).textStyleRange || []) {
    const run = runs.find((item) => item.start <= range.from && range.from < item.end);
    const name = range.textStyle && range.textStyle.fontPostScriptName;
    if (run && name && !families[name]) families[name] = run.family;
  }
  return { layer: layer, missingFonts: Array.from(missingFonts), families: families };
}

// What Send Changes compares a layer against; see collectChanges.
async function layerRecord(doc, layer, families) {
  const read = readText(doc, await textKey(layer), families);
  return {
    font: read.font,
    frame: read.frame,
    runs: read.runs,
    alignment: read.alignment,
    opacity: round2(layer.opacity / 100),
    angle: read.angle,
    families: families
  };
}

function layerPreview(text) {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > 22 ? flat.substring(0, 22) + "..." : flat;
}

// ---------------------------------------------------------------------------
// Import

// Each image gets its own folder so the document keeps the page's file name.
async function writeTempImage(image) {
  const temp = await storage.localFileSystem.getTemporaryFolder();
  const folder = await temp.createFolder("bt_" + Date.now() + "_" + Math.floor(Math.random() * 1e6));
  const file = await folder.createFile(image.filename, { overwrite: true });
  await file.write(image.bytes, { format: storage.formats.binary });
  return file;
}

async function addImageLayer(doc, image, name, visible) {
  const file = await writeTempImage(image);
  const source = await app.open(file);
  const copy = await source.layers[0].duplicate(doc);
  await source.closeWithoutSaving();
  app.activeDocument = doc;
  copy.name = name;
  copy.visible = visible;
  return copy;
}

// A hidden reference group with the recognized source text. Send Changes
// ignores it.
async function addSourceTextLayers(doc, pageBlocks) {
  const plain = { effects: false, verticalCenter: false, smartBounds: false };
  const layers = [];
  for (let index = pageBlocks.length - 1; index >= 0; index--) {
    const source = pageBlocks[index].text.join("\n").trim();
    if (!source) continue;
    // The runs belong to the translation.
    const block = Object.assign({}, pageBlocks[index], { translation: source, runs: null });
    const made = await createTextLayer(doc, block, plain);
    made.layer.name = "Source #" + (index + 1) + ": " + layerPreview(source);
    layers.push(made.layer);
  }
  if (layers.length) {
    const group = await doc.createLayerGroup({ name: SOURCE_GROUP, fromLayers: layers });
    group.visible = false;
  }
}

// options: {inpainted, mask, originalText, effects, verticalCenter, smartBounds}
// Returns {document, missingFonts, untranslated}; untranslated counts blocks
// that have source text but no translation yet, which get no text layer.
async function importPage(client, pageName, options) {
  const data = await client.page(pageName);
  const original = await client.image(pageName, "original");
  if (!original) throw new BridgeError("error", "The page image is missing.");
  const inpainted = options.inpainted ? await client.image(pageName, "inpainted") : null;
  const mask = options.mask ? await client.image(pageName, "mask") : null;

  return core.executeAsModal(async () => {
    const doc = await app.open(await writeTempImage(original));
    // The Background layer cannot be renamed; turn it into a normal layer.
    await action.batchPlay([{
      _obj: "set",
      _target: [{ _ref: "layer", _property: "background" }],
      to: { _obj: "layer", name: "[BT] Original Scan" }
    }], {});
    if (inpainted) await addImageLayer(doc, inpainted, "[BT] Clean Inpaint", true);
    if (mask) await addImageLayer(doc, mask, "[BT] Text Mask", false);

    if (options.originalText) await addSourceTextLayers(doc, data.blocks);

    const blocks = data.bases.map((base) => Object.assign({}, base));
    const layers = [];
    const missingFonts = new Set();
    // Create the last block first so block #1 ends up on top.
    for (let index = data.blocks.length - 1; index >= 0; index--) {
      const block = data.blocks[index];
      if (!block.translation) continue;
      const made = await createTextLayer(doc, block, options);
      made.layer.name = "#" + (index + 1) + ": " + layerPreview(block.translation);
      made.missingFonts.forEach((font) => missingFonts.add(font));
      blocks[index].layer = await layerRecord(doc, made.layer, made.families);
      layers.push(made.layer);
    }
    if (layers.length) {
      await doc.createLayerGroup({ name: TRANSLATIONS_GROUP, fromLayers: layers });
    }
    await writeState(doc, {
      version: 1,
      project_path: data.project_path,
      page: pageName,
      blocks: blocks
    });
    const untranslated = data.blocks.filter(
      (block) => !block.translation && block.text.join("").trim()
    ).length;
    return { document: doc, missingFonts: Array.from(missingFonts), untranslated: untranslated };
  }, { commandName: "Import from BallonsTranslator" });
}

// ---------------------------------------------------------------------------
// Send changes

function translationLayers(doc) {
  const group = doc.layers.find((layer) => layer.kind === "group" && layer.name === TRANSLATIONS_GROUP);
  return group ? group.layers.filter((layer) => layer.kind === "text") : [];
}

function blockIndex(layerName) {
  const match = /^#(\d+)\s*:/.exec(layerName || "");
  return match ? parseInt(match[1], 10) - 1 : -1;
}

function smallestSize(runs) {
  return runs.length ? Math.min.apply(null, runs.map((run) => run.font_size)) : null;
}

// One block's update, or null when the layer still matches what was imported
// (base.layer, see layerRecord). Documents imported by older panels have no
// runs, alignment, opacity or angle there, so those are not sent for them.
function blockUpdate(index, base, read) {
  const imported = base.layer || null;
  const update = { block_index: index, translation: read.text, font_size: null, base: base };
  const textChanged = read.text !== normalizeText(base.translation || "");

  // Apps before protocol 2 read only font_size and font.
  if (imported && imported.runs) {
    const size = smallestSize(read.runs);
    const before = smallestSize(imported.runs);
    if (size !== null && before !== null && Math.abs(size - before) >= 0.1) update.font_size = size;
  } else if (read.runs.length && typeof base.font_size === "number" &&
             Math.abs(read.runs[0].font_size - base.font_size) >= 0.1) {
    update.font_size = read.runs[0].font_size;
  }
  if (imported && read.runs.length && read.font !== imported.font) update.font = read.runs[0].font;
  if (imported && imported.frame) {
    const delta = read.frame.map((edge, i) => round2(edge - imported.frame[i]));
    if (delta.some((edge) => Math.abs(edge) >= 0.5)) update.box_delta = delta;
  }

  if (imported && imported.runs && (textChanged || !sameRuns(read.runs, imported.runs))) {
    update.runs = read.runs;
  }
  if (imported && typeof imported.alignment === "number" && read.alignment !== null &&
      read.alignment !== imported.alignment) {
    update.alignment = read.alignment;
  }
  if (imported && typeof imported.opacity === "number" && Math.abs(read.opacity - imported.opacity) >= 0.005) {
    update.opacity = read.opacity;
  }
  if (imported && typeof imported.angle === "number" &&
      Math.abs(normalizeAngle(read.angle - imported.angle)) >= 0.05) {
    update.angle = read.angle;
  }
  const changed = textChanged || update.font_size !== null || update.font || update.box_delta ||
    ["runs", "alignment", "opacity", "angle"].some((name) => name in update);
  return changed ? update : null;
}

// Returns {state, payload, layerStates} for the active document; payload.updates
// is empty when nothing changed. Throws for documents not imported by the bridge.
async function collectChanges(doc) {
  const state = await readState(doc);
  if (!state) {
    throw new BridgeError("error", "This document was not imported from BallonsTranslator.");
  }
  const updates = [];
  const skipped = [];
  const layerStates = {};
  const seen = {};
  for (const layer of translationLayers(doc)) {
    const index = blockIndex(layer.name);
    if (index < 0 || index >= state.blocks.length || seen[index]) {
      skipped.push(layer.name);
      continue;
    }
    seen[index] = true;
    const base = state.blocks[index];
    const families = base.layer && base.layer.families;
    const read = readText(doc, await textKey(layer), families);
    read.opacity = round2(layer.opacity / 100);
    layerStates[index] = {
      font: read.font,
      frame: read.frame,
      runs: read.runs,
      alignment: read.alignment,
      opacity: read.opacity,
      angle: read.angle,
      families: families || {}
    };
    const update = blockUpdate(index, base, read);
    if (update) updates.push(update);
  }
  return {
    state: state,
    layerStates: layerStates,
    payload: {
      version: 1,
      project_path: state.project_path,
      page: state.page,
      block_count: state.blocks.length,
      updates: updates,
      skipped_layers: skipped,
      timestamp: Date.now() / 1000
    }
  };
}

// After the app applied the changes, later sends compare against them. The
// app's own snapshot of the changed blocks becomes their base; without it
// (the app is unreachable), the sent values are the best guess.
async function markApplied(client, doc, changes) {
  const state = changes.state;
  let bases = null;
  try {
    const data = await client.page(state.page);
    if (data.project_path === state.project_path && data.bases.length === state.blocks.length) {
      bases = data.bases;
    }
  } catch (error) {
    // Fall back to the sent values.
  }
  for (const update of changes.payload.updates) {
    const index = update.block_index;
    let base = state.blocks[index];
    if (bases) {
      base = Object.assign({}, bases[index]);
    } else {
      base.translation = update.translation;
      if (update.font_size !== null) base.font_size = update.font_size;
      if (update.font) base.font = update.font;
      for (const name of ["alignment", "opacity", "angle"]) {
        if (name in update) base[name] = update[name];
      }
    }
    base.layer = changes.layerStates[index];
    state.blocks[index] = base;
  }
  await core.executeAsModal(() => writeState(doc, state), { commandName: "Update BallonsTranslator state" });
}

// Send the active document's changes. Returns the app's answer, or
// {state: "unchanged"} when there is nothing to send.
async function sendChanges(client, doc) {
  const changes = await collectChanges(doc);
  if (!changes.payload.updates.length) return { state: "unchanged", message: "" };
  const result = await client.sendUpdates(changes.payload);
  if (result.state === "applied") await markApplied(client, doc, changes);
  return result;
}

module.exports = {
  BridgeClient: BridgeClient,
  BridgeError: BridgeError,
  importPage: importPage,
  collectChanges: collectChanges,
  sendChanges: sendChanges,
  readState: readState,
  textFrame: textFrame,
  resolveFont: resolveFont,
  normalizeText: normalizeText,
  PORTS: PORTS
};

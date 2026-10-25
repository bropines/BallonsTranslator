// BallonsTranslator panel core: talks to the app and edits Photoshop documents.
// No UI here; main.js drives it. The page state stored in the PSD (XMP) and the
// update payload are shared with BallonTranslator_PS_Bridge.jsx, see
// scripts/export to photoshop/readme.md.
"use strict";

const { app, core, action } = require("photoshop");
const { storage, xmp } = require("uxp");

// Must match PS_BRIDGE_PORTS in ballontranslator/utils/config.py and the
// network domains in manifest.json.
const PORTS = [19871, 19872, 19873, 19874, 19875];
const SERVER_NAME = "BallonsTranslatorBridge";
const TOKEN_KEY = "ballonstranslator.token";
const XMP_NS = "http://github.com/dmMaze/BallonsTranslator/ns/psbridge/1.0/";
const TRANSLATIONS_GROUP = "[BT] Translations";

class BridgeError extends Error {
  // kind: "offline" | "unpaired" | "error"
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

// ---------------------------------------------------------------------------
// Connection to the app

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
        const response = await fetch("http://localhost:" + port + "/api/v1/status", {
          headers: { "X-BT-Token": this.token }
        });
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
        headers: { "Content-Type": "application/json" },
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
        filename: response.headers.get("x-bt-filename") || page
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

// Find the Photoshop font for an app font family; null when none fits.
function resolveFont(family, bold, italic) {
  if (!family) return null;
  const index = fonts();
  const wanted = cleanFontName(family);
  const exact = index.byPostScript[wanted] || index.byName[wanted];
  if (exact) return exact.postScriptName;

  let candidates = index.byFamily[wanted];
  if (!candidates) {
    const key = Object.keys(index.byFamily).find(
      (name) => name.indexOf(wanted) !== -1 || wanted.indexOf(name) !== -1
    );
    candidates = key ? index.byFamily[key] : null;
  }
  if (!candidates) return null;
  const isBold = (entry) => styleMatches(entry, ["bold", "black", "heavy"]);
  const isItalic = (entry) => styleMatches(entry, ["italic", "oblique"]);
  const pick =
    candidates.find((entry) => isBold(entry) === bold && isItalic(entry) === italic) ||
    candidates.find((entry) => styleMatches(entry, ["regular", "roman", "medium", "normal"])) ||
    candidates[0];
  return pick.postScriptName;
}

// The app's view of a Photoshop font: {family, bold, italic}, or null.
function fontInfo(postScriptName) {
  const entry = fonts().byPostScript[cleanFontName(postScriptName)];
  if (!entry) return null;
  return {
    family: entry.family,
    bold: styleMatches(entry, ["bold", "black", "heavy"]),
    italic: styleMatches(entry, ["italic", "oblique"])
  };
}

// ---------------------------------------------------------------------------
// Text layers

// Photoshop ends paragraphs with \r and stores Shift+Enter breaks as \u0003.
function normalizeText(text) {
  return String(text).replace(/\r\n/g, "\n").replace(/[\r\u0003]/g, "\n");
}

function unitValue(value) {
  return value && typeof value === "object" ? value._value : value;
}

async function textKey(layer) {
  const result = await action.batchPlay([{
    _obj: "get",
    _target: [{ _property: "textKey" }, { _ref: "layer", _id: layer.id }]
  }], {});
  return result[0].textKey;
}

// The paragraph box in document pixels as [left, top, right, bottom].
// Moving and transforming the layer change the click point and transform.
async function textFrame(doc, layer) {
  const key = await textKey(layer);
  const toPx = doc.resolution / 72;
  const origin = [
    unitValue(key.textClickPoint.horizontal) / 100 * doc.width,
    unitValue(key.textClickPoint.vertical) / 100 * doc.height
  ];
  const shape = key.textShape && key.textShape[0];
  const bounds = shape && shape.bounds ? shape.bounds : key.bounds;
  const transform = key.transform || { xx: 1, xy: 0, yx: 0, yy: 1, tx: 0, ty: 0 };
  const corners = [
    [unitValue(bounds.left), unitValue(bounds.top)],
    [unitValue(bounds.right), unitValue(bounds.bottom)]
  ].map(([x, y]) => [
    origin[0] + (transform.xx * x + transform.yx * y + (transform.tx || 0)) * toPx,
    origin[1] + (transform.xy * x + transform.yy * y + (transform.ty || 0)) * toPx
  ]);
  return [
    Math.min(corners[0][0], corners[1][0]),
    Math.min(corners[0][1], corners[1][1]),
    Math.max(corners[0][0], corners[1][0]),
    Math.max(corners[0][1], corners[1][1])
  ];
}

// The rendered font size in pixels, including a scaling transform.
async function textSizePx(doc, layer) {
  const key = await textKey(layer);
  const style = key.textStyleRange[0].textStyle;
  const transform = key.transform || { yy: 1, xy: 0 };
  const scale = Math.sqrt(transform.yy * transform.yy + transform.xy * transform.xy);
  return unitValue(style.impliedFontSize || style.size) * scale * doc.resolution / 72;
}

async function createTextLayer(doc, block, options) {
  const style = block.style;
  const toPt = 72 / doc.resolution;
  let [left, top, width, height] = block.rect;
  width = Math.max(width, 40);
  height = Math.max(height, 20);
  // Skinny vertical-text boxes are too narrow for horizontal translations.
  if (options.smartBounds && width < 70 && height > width * 1.5) {
    const wider = Math.max(Math.round(width * 1.8), 120);
    left = Math.max(0, left - (wider - width) / 2);
    width = wider;
    height = Math.max(Math.round(height * 1.05), 40);
  }
  const text = block.translation.replace(/\r\n|\n/g, "\r");
  const textStyle = {
    _obj: "textStyle",
    size: { _unit: "pointsUnit", _value: style.font_size * toPt },
    color: { _obj: "RGBColor", red: style.color[0], grain: style.color[1], blue: style.color[2] },
    autoLeading: true
  };
  const fontName = resolveFont(style.font_family, style.bold, style.italic);
  if (fontName) textStyle.fontPostScriptName = fontName;
  const align = ["left", "center", "right"][style.alignment] || "center";
  const make = async (boxTop, boxHeight) => {
    await action.batchPlay([{
      _obj: "make",
      _target: [{ _ref: "textLayer" }],
      using: {
        _obj: "textLayer",
        textKey: text,
        // Photoshop reads the click point in percent of the document size.
        textClickPoint: {
          _obj: "paint",
          horizontal: { _unit: "percentUnit", _value: left / doc.width * 100 },
          vertical: { _unit: "percentUnit", _value: boxTop / doc.height * 100 }
        },
        textShape: [{
          _obj: "textShape",
          char: { _enum: "char", _value: "box" },
          orientation: { _enum: "orientation", _value: "horizontal" },
          bounds: { _obj: "rectangle", left: 0, top: 0, right: width * toPt, bottom: boxHeight * toPt }
        }],
        textStyleRange: [{ _obj: "textStyleRange", from: 0, to: text.length, textStyle: textStyle }],
        paragraphStyleRange: [{
          _obj: "paragraphStyleRange", from: 0, to: text.length,
          paragraphStyle: { _obj: "paragraphStyle", align: { _enum: "alignmentType", _value: align } }
        }]
      }
    }], {});
    return doc.activeLayers[0];
  };
  let layer = await make(top, height);
  if (options.verticalCenter) {
    // Paragraph boxes keep text at the top. Measure the text, then remake the
    // box so the text sits in the middle of the block. The spare room below
    // keeps Photoshop from hiding the last line when the leading is taller
    // than the ink.
    const bounds = layer.boundsNoEffects;
    const inset = bounds.top - top;
    const textHeight = bounds.bottom - bounds.top;
    const boxHeight = inset + textHeight + style.font_size * 0.8;
    if (textHeight > 0 && boxHeight < height) {
      await layer.delete();
      layer = await make(top + (height - textHeight) / 2 - inset, boxHeight);
    }
  }
  if (options.stroke && style.stroke && style.stroke.width > 0) {
    await setStroke(layer, style.stroke.width, style.stroke.color);
  }
  return { layer: layer, font: fontName, missingFont: fontName ? null : style.font_family };
}

async function setStroke(layer, widthPx, color) {
  await action.batchPlay([{
    _obj: "set",
    _target: [{ _ref: "property", _property: "layerEffects" }, { _ref: "layer", _id: layer.id }],
    to: {
      _obj: "layerEffects",
      frameFX: {
        _obj: "frameFX",
        enabled: true,
        present: true,
        showInDialog: true,
        style: { _enum: "frameStyle", _value: "outsetFrame" },
        paintType: { _enum: "frameFill", _value: "solidColor" },
        mode: { _enum: "blendMode", _value: "normal" },
        opacity: { _unit: "percentUnit", _value: 100 },
        size: { _unit: "pixelsUnit", _value: widthPx },
        color: { _obj: "RGBColor", red: color[0], grain: color[1], blue: color[2] }
      }
    }
  }], {});
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

// options: {inpainted, mask, stroke, verticalCenter, smartBounds}
// Returns {document, missingFonts}.
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

    const blocks = data.bases.map((base) => Object.assign({}, base));
    const layers = [];
    const missingFonts = new Set();
    // Create the last block first so block #1 ends up on top.
    for (let index = data.blocks.length - 1; index >= 0; index--) {
      const block = data.blocks[index];
      if (!block.translation) continue;
      const made = await createTextLayer(doc, block, options);
      made.layer.name = "#" + (index + 1) + ": " + layerPreview(block.translation);
      if (made.missingFont) missingFonts.add(made.missingFont);
      blocks[index].layer = {
        font: made.layer.textItem.characterStyle.font,
        frame: await textFrame(doc, made.layer)
      };
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
    return { document: doc, missingFonts: Array.from(missingFonts) };
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
    const imported = base.layer || null;
    const translation = normalizeText(layer.textItem.contents);
    const update = { block_index: index, translation: translation, font_size: null, base: base };

    const sizePx = await textSizePx(doc, layer);
    if (typeof base.font_size === "number" && Math.abs(sizePx - base.font_size) >= 0.1) {
      update.font_size = Math.round(sizePx * 100) / 100;
    }
    const font = layer.textItem.characterStyle.font;
    if (imported && font !== imported.font) {
      const info = fontInfo(font);
      if (info) update.font = info;
    }
    const frame = await textFrame(doc, layer);
    if (imported && imported.frame) {
      const delta = frame.map((edge, i) => Math.round((edge - imported.frame[i]) * 100) / 100);
      if (delta.some((edge) => Math.abs(edge) >= 0.5)) update.box_delta = delta;
    }
    layerStates[index] = { font: font, frame: frame };

    const changed = translation !== normalizeText(base.translation || "") ||
      update.font_size !== null || update.font || update.box_delta;
    if (changed) updates.push(update);
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

// After the app applied the changes, later sends compare against them.
async function markApplied(doc, changes) {
  const state = changes.state;
  for (const update of changes.payload.updates) {
    const base = state.blocks[update.block_index];
    base.translation = update.translation;
    if (update.font_size !== null) base.font_size = update.font_size;
    if (update.font) base.font = update.font;
    base.layer = changes.layerStates[update.block_index];
  }
  await core.executeAsModal(() => writeState(doc, state), { commandName: "Update BallonsTranslator state" });
}

// Send the active document's changes. Returns the app's answer, or
// {state: "unchanged"} when there is nothing to send.
async function sendChanges(client, doc) {
  const changes = await collectChanges(doc);
  if (!changes.payload.updates.length) return { state: "unchanged", message: "" };
  const result = await client.sendUpdates(changes.payload);
  if (result.state === "applied") await markApplied(doc, changes);
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
  textSizePx: textSizePx,
  resolveFont: resolveFont,
  normalizeText: normalizeText,
  PORTS: PORTS
};

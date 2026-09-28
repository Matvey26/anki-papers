import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../src/articles_to_anki/static");

async function readerHarness() {
  const elements = new Map();
  function element() {
    return {
      dataset: {workerUrl: "worker", highlightsUrl: "/highlights", initialPage: "1"},
      style: {setProperty() {}},
      classList: {add() {}, remove() {}},
      addEventListener() {},
      append() {},
      querySelector() { return null; },
      setAttribute() {},
      clientWidth: 800,
      hidden: true,
      getBoundingClientRect() { return {left: 0, top: 0, width: 500, height: 700}; },
    };
  }
  const errors = [];
  const sandbox = {
    console: {error(error) { errors.push(error); }},
    document: {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, element());
        return elements.get(selector);
      },
      addEventListener() {},
      createElement: element,
    },
    window: {
      innerWidth: 800,
      innerHeight: 800,
      devicePixelRatio: 1,
      addEventListener() {},
      setTimeout() { return 1; },
      clearTimeout() {},
    },
    IntersectionObserver: class { observe() {} },
    fetch: async () => ({ok: true, json: async () => ({highlights: []})}),
  };
  const context = vm.createContext(sandbox);
  const pdf = new vm.SyntheticModule(["GlobalWorkerOptions", "getDocument", "TextLayer"], function () {
    this.setExport("GlobalWorkerOptions", {});
    this.setExport("getDocument", () => ({promise: Promise.resolve({numPages: 0})}));
    this.setExport("TextLayer", class {});
  }, {context});
  const modules = new Map();
  function load(filename) {
    if (filename.includes("/vendor/")) return pdf;
    if (!modules.has(filename)) {
      modules.set(filename, new vm.SourceTextModule(readFileSync(filename, "utf8"), {context, identifier: filename}));
    }
    return modules.get(filename);
  }
  const main = load(path.join(root, "reader.js"));
  await main.link((specifier, referencing) => load(path.resolve(path.dirname(referencing.identifier), specifier)));
  await main.evaluate();
  await new Promise(resolve => setImmediate(resolve));
  return {
    errors, elements, sandbox,
    module: name => modules.get(path.join(root, name)).namespace,
  };
}

test("reader modules link and bootstrap without runtime errors", async () => {
  const harness = await readerHarness();
  assert.deepEqual(harness.errors, []);
  assert.equal(harness.elements.get("#pdf-status").hidden, true);
  const selection = harness.module("reader-selection.js");
  assert.equal(selection.normalizeSelectedText("inter-\nnational"), "international");
  assert.equal(selection.isSelectableTarget("one two three four five"), false);
  assert.equal(selection.isSelectableTarget("rule out"), true);
});

test("discarding the open highlight closes its popover and clears state", async () => {
  const harness = await readerHarness();
  const state = harness.module("reader-state.js");
  const highlight = {id: "test", page: 1, target: "robust", sentence: "A robust result.", rects: []};
  state.highlights.set(highlight.id, highlight);
  state.state.openHighlightId = highlight.id;
  state.popover.hidden = false;
  harness.sandbox.fetch = async () => ({ok: true, json: async () => ({discarded_highlight_id: highlight.id})});
  await harness.module("reader-highlights.js").saveHighlight(highlight);
  assert.equal(state.state.openHighlightId, null);
  assert.equal(state.popover.hidden, true);
  assert.equal(state.highlights.has(highlight.id), false);
  assert.equal(state.savingHighlights.size, 0);
});

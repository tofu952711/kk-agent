import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import { migrateFullVideoWidgets, resizeFullVideoInputs } from "../web/lib/full_video_widgets.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const old = ["local-placeholder", "https://old.invalid", "old-model", "图1人物、图2商品", 900, 32768, "/old/model", "deepseek-messages",
  "A规则\n逐字保留", false, "分段规则", "反推规则", "low", true, "B审核规则", "自定义",
  "review-placeholder", "https://review.invalid", "review-model", "openai-completions", "A审核规则"];
const expected = [old[0], old[3], old[8], old[9], old[10], old[11], old[13], old[14], old[16], old[17], old[20]];
assert.deepEqual(migrateFullVideoWidgets(old), expected);
assert.equal(migrateFullVideoWidgets(expected), expected, "compact workflow must not migrate twice");
assert.equal(old.length, 21, "migration must not mutate the original widget array");
const visibleCredential = { widgets: [{ name: "api_key", value: "" }, { name: "review_api_key", value: "" }] };
resizeFullVideoInputs(visibleCredential);
assert.equal(visibleCredential.widgets[0].label, "数值");
assert.equal(visibleCredential.widgets[0].name, "api_key");
assert.equal(visibleCredential.widgets[1].label, "数值");
const compactPrevious = [old[0], old[3], old[8], old[9], old[10], old[11], old[13], ...old.slice(14)];
assert.deepEqual(migrateFullVideoWidgets(compactPrevious), expected);
const inherited = [...old.slice(0, 15), "沿用当前AI", "A审核规则"];
assert.deepEqual(migrateFullVideoWidgets(inherited), [old[0], old[3], old[8], old[9], old[10], old[11], old[13], old[14], "", "https://api.zdjapi.com", "A审核规则"]);

let script = await fs.readFile(path.join(root, "web/kkgo_video_inputs.js"), "utf8");
script = script.replace('import { app } from "../../scripts/app.js";', 'const app = { registerExtension(extension) { globalThis.fixedReverseExtension = extension; } };');
for (const name of ["full_video_widgets", "video_stage_graph"]) {
  script = script.replace(`"./lib/${name}.js"`, JSON.stringify(pathToFileURL(path.join(root, `web/lib/${name}.js`)).href));
}
await import("data:text/javascript;base64," + Buffer.from(script).toString("base64"));
const graph = {
  nodes: [
    { id: 1, type: "Text", outputs: [{ links: [10, 11, 12, 13] }] },
    { id: 2, type: "KKGO_VideoSegments", widgets_values: [...old], inputs: [
      { name: "video", link: null }, { name: "base_url", link: 10 }, { name: "rules_text", link: 11 },
      { name: "review_ai.api_key", link: 12, widget: { name: "review_ai.api_key" } }, { name: "review_ai.model", link: 13 }
    ], outputs: [] },
    { id: 3, type: "Unrelated", widgets_values: [...old] }
  ],
  links: [[10, 1, 0, 2, 1, "STRING"], [11, 1, 0, 2, 2, "STRING"], [12, 1, 0, 2, 3, "STRING"], [13, 1, 0, 2, 4, "STRING"]]
};
globalThis.fixedReverseExtension.beforeConfigureGraph(graph);
assert.deepEqual(graph.nodes[1].widgets_values, expected);
assert.equal(graph.nodes[1].inputs.some(input => input.name === "base_url"), false);
assert.equal(graph.links.length, 2);
assert.equal(graph.links[0][0], 11);
assert.equal(graph.nodes[1].inputs[graph.links[0][4]].name, "rules_text");
assert.deepEqual(graph.nodes[0].outputs[0].links, [11, 12]);
assert.equal(graph.nodes[1].inputs[graph.links[1][4]].name, "review_api_key");
assert.deepEqual(graph.nodes[2].widgets_values, old);
globalThis.fixedReverseExtension.beforeConfigureGraph(graph);
assert.deepEqual(graph.nodes[1].widgets_values, expected);
console.log("Passed legacy/custom reviewer migration, compact reload and connected-rule preservation.");

const previousFixed = [...expected.slice(0, 9), expected[10]];
assert.deepEqual(migrateFullVideoWidgets(previousFixed), [...expected.slice(0, 9), "https://api.zdjapi.com", expected[10]]);
const urlWidget = { widgets: [{ name: "review_base_url" }] };
resizeFullVideoInputs(urlWidget);
assert.equal(urlWidget.widgets[0].label, "数值2");

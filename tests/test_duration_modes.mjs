import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
let script = await fs.readFile(path.join(root, "web/kkgo_video_inputs.js"), "utf8");
script = script.replace('import { app } from "../../scripts/app.js";',
  'const app = { registerExtension(extension) { globalThis.durationModeExtension = extension; } };');
for (const name of ["full_video_widgets", "video_stage_graph"]) {
  script = script.replace(`"./lib/${name}.js"`, JSON.stringify(pathToFileURL(path.join(root, `web/lib/${name}.js`)).href));
}
await import("data:text/javascript;base64," + Buffer.from(script).toString("base64"));
const extension = globalThis.durationModeExtension;
const OLD = "32秒模式（原模式）", NEW = "17秒模式";
const names = ["api_key", "base_url", "model", "requirement", "rules_text", "keep_audio", "segment_rules_text",
  "chain_b_reverse_rules_text", "stream_usage", "chain_b_review_rules_text", "review_api_key",
  "review_base_url", "review_model", "chain_a_review_rules_text", "duration_mode"];
const oldValues = ["offline", "user", "A rules", true, "B split", "B reverse", true,
  "B review", "offline-review", "https://review.invalid", "A review"];
for (const type of ["KKGO_FullVideoAgent", "KKGO_VideoSegments"]) {
  function Node() {
    this.widgets = names.map(name => ({name, value: name === "duration_mode" ? OLD : undefined}));
    this.inputs = [];
  }
  Node.prototype.onConfigure = function(info) {
    this.widgets.forEach((widget, i) => { widget.value = info.widgets_values[i]; });
  };
  extension.beforeRegisterNodeDef(Node, {name:type, input:{optional:{
    base_url:["STRING",{default:"https://api.deepseek.com"}],
    duration_mode:["COMBO",{default:OLD,options:[OLD,NEW]}],
    chain_a_review_rules_text:["STRING",{default:"A review"}],
    chain_b_review_rules_text:["STRING",{default:"B review"}],
  }}});
  for (const mode of [undefined, OLD, NEW]) {
    for (const url of [undefined, "https://reverse.invalid/v1"]) {
      const saved = [...oldValues];
      if (mode !== undefined || url !== undefined) saved.push(mode ?? OLD);
      if (url !== undefined) saved.push(url);
      const graph = {nodes:[{id:1,type,widgets_values:saved,inputs:[],outputs:[]}],links:[]};
      extension.beforeConfigureGraph(graph);
      const node = new Node();
      node.onConfigure(graph.nodes[0]);
      const expected = [oldValues[0], url ?? "https://api.deepseek.com", "deepseek-flash", ...oldValues.slice(1,10), "gpt-6.1-sol", oldValues[10], mode ?? OLD];
      assert.deepEqual(node.widgets.map(w=>w.value), expected, "move URL without shifting rules, keys or mode");
      const reload = new Node();
      reload.onConfigure({widgets_values:node.widgets.map(w=>w.value)});
      assert.deepEqual(reload.widgets.map(w=>w.value), expected, "all values survive a second reload");
      assert.equal(node.widgets[1].label, "反推 AI Base URL");
      assert.equal(node.widgets[11].label, "审核 AI Base URL");
    }
  }
  const older = [...oldValues.slice(0,9), oldValues[10]];
  const migrated = new Node();
  migrated.onConfigure({widgets_values:older});
  assert.equal(migrated.widgets[14].value, OLD);
  assert.equal(migrated.widgets[13].value, "A review");
}
console.log("Passed key/URL/requirement order, historical layouts, custom addresses, rules, credentials, modes and save/reload.");

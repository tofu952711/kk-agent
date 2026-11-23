import { app } from "../../scripts/app.js";

// Results remain available through the node outputs. Do not add a large,
// persistent preview/status widget underneath the three prompt editors.
app.registerExtension({
  rh: { type: "nodes", nodes: "all" }, name: "KKGO.FullVideo.EarlyPreview" });

"""Check the ComfyUI repository contract without importing nodes or downloading assets."""
import ast
import json
from pathlib import Path
import re
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
NODE_IDS = ("KKGO_FullVideoAgent", "KKGO_VideoSegments", "KKGO_VideoReverse", "KKGO_VideoFrames")


def main():
    errors = []
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project, comfy = metadata["project"], metadata["tool"]["comfy"]
    name = project.get("name", "")
    if len(name) >= 100 or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*", name):
        errors.append("Invalid immutable project identifier")
    if not re.fullmatch(r"\d+\.\d+\.\d+", project.get("version", "")):
        errors.append("Version must use X.Y.Z")
    for field in ("description", "readme", "requires-python", "classifiers"):
        if not project.get(field):
            errors.append("Missing project field: " + field)
    for field in ("Repository", "Documentation", "Bug Tracker"):
        if not project.get("urls", {}).get(field, "").startswith("https://"):
            errors.append("Missing project URL: " + field)
    if not (ROOT / project["readme"]).is_file() or not (ROOT / project["license"]["file"]).is_file():
        errors.append("README or license reference is missing")
    for field in ("PublisherId", "DisplayName", "requires-comfyui"):
        if not comfy.get(field):
            errors.append("Missing tool.comfy field: " + field)
    requirements = [line.split("#", 1)[0].strip() for line in
                    (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    if sorted(filter(None, requirements)) != sorted(project.get("dependencies", [])):
        errors.append("pyproject dependencies differ from requirements.txt")
    entry = ast.parse((ROOT / "__init__.py").read_text(encoding="utf-8"))
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == "comfy_entrypoint" for node in entry.body):
        errors.append("Missing V3 comfy_entrypoint")
    for node_id in NODE_IDS:
        if not (ROOT / "web" / "docs" / (node_id + ".md")).is_file():
            errors.append("Missing WEB_DIRECTORY/docs help: " + node_id)
    ignore = (ROOT / ".comfyignore").read_text(encoding="utf-8").splitlines()
    if "models/**/*.onnx" not in ignore or "vendor/" not in ignore:
        errors.append("Registry archive must exclude model weights and bundled dependencies")
    template = json.loads((ROOT / "example_workflows" / "KKagent_basic.json").read_text(encoding="utf-8"))
    if any(node.get("widgets_values", [""])[0] for node in template["nodes"]
           if node["type"] == "KKGO_VideoSegments"):
        errors.append("Example reverse API key must be empty")
    for directory in ("nodes", "kkagent_bridge", "routes"):
        for path in (ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
                    errors.append(f"Prohibited {node.func.id} call: {path.relative_to(ROOT)}:{node.lineno}")
    print(json.dumps({"name": name, "display_name": comfy["DisplayName"], "version": project["version"],
                      "checks_passed": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())

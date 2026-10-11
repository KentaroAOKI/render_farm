"""Executed by mayapy for inspection, or imported inside a dedicated Maya GUI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import traceback

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from farm.common import atomic_json, read_json, safe_name, sha256, digest


def setup_arnold():
    import maya.cmds as cmds
    cmds.loadPlugin("mtoa", quiet=True)
    import mtoa.core
    mtoa.core.createOptions()


def layers():
    import maya.cmds as cmds
    import maya.app.renderSetup.model.renderSetup as rs
    setup = rs.instance()
    found = setup.getRenderLayers()
    if found:
        return [{"name": layer.name(), "enabled": bool(layer.isRenderable()), "kind": "setup"} for layer in found]
    return [{"name": node, "enabled": bool(cmds.getAttr(node + ".renderable")), "kind": "legacy"}
            for node in cmds.ls(type="renderLayer") or [] if ":" not in node]


def activate_layer(name):
    import maya.cmds as cmds
    import maya.app.renderSetup.model.renderSetup as rs
    for layer in rs.instance().getRenderLayers():
        if layer.name() == name:
            rs.instance().switchToLayer(layer)
            return
    cmds.editRenderLayerGlobals(currentRenderLayer=name)


def frame_rate():
    import maya.cmds as cmds
    unit = cmds.currentUnit(query=True, time=True)
    rates = {"game": "15", "film": "24", "pal": "25", "ntsc": "30", "show": "48", "palf": "50", "ntscf": "60"}
    if unit in rates:
        return rates[unit]
    from maya.api import OpenMaya as om
    return format(om.MTime(1, om.MTime.kSeconds).asUnits(om.MTime.uiUnit()), ".12g")


def file_attributes(patterns=True):
    import maya.cmds as cmds
    found = {}
    cmds.filePathEditor(refresh=True)
    for directory in cmds.filePathEditor(query=True, listDirectories="") or []:
        values = cmds.filePathEditor(query=True, listFiles=directory, withAttribute=True) or []
        for index in range(0, len(values) - 1, 2):
            attr = values[index + 1]
            try:
                value = cmds.getAttr(attr)
                if isinstance(value, str) and value:
                    found[attr] = value
            except Exception:
                pass
    # Explicit coverage for common nodes not registered in File Path Editor.
    known = {"file": ["fileTextureName"], "aiImage": ["filename"], "aiStandIn": ["dso"],
             "AlembicNode": ["abc_File"], "gpuCache": ["cacheFileName"], "aiVolume": ["filename"],
             "audio": ["filename"], "cacheFile": ["cachePath"], "imagePlane": ["imageName"]}
    for typ, attrs in known.items():
        if typ not in (cmds.allNodeTypes() or []):
            continue
        for node in cmds.ls(type=typ) or []:
            for attr in attrs:
                plug = node + "." + attr
                if cmds.objExists(plug):
                    value = cmds.getAttr(plug)
                    if value:
                        found[plug] = value
            if typ == "file" and patterns and (cmds.getAttr(node + ".useFrameExtension") or cmds.getAttr(node + ".uvTilingMode")):
                # Resolve Maya's evaluated frame-extension token explicitly.
                import re
                plug = node + ".fileTextureName"
                computed = cmds.getAttr(node + ".computedFileTextureNamePattern")
                found[plug] = computed or re.sub(r"\d+(?=\.[^.]+$)", "<f>", found.get(plug, ""))
    return found


def scene_ranges(strict=True):
    """Read saved playback options without executing any scene script."""
    import re
    import maya.cmds as cmds
    values = {key: cmds.playbackOptions(query=True, **{flag: True}) for key, flag in
              [("start", "minTime"), ("end", "maxTime"), ("animation_start", "animationStartTime"),
               ("animation_end", "animationEndTime"), ("step", "by")]}
    node = "sceneConfigurationScriptNode"
    if cmds.objExists(node) and not cmds.referenceQuery(node, isNodeReferenced=True):
        script = cmds.getAttr(node + ".before") or ""
        aliases = {"min": "start", "minTime": "start", "max": "end", "maxTime": "end",
                   "ast": "animation_start", "animationStartTime": "animation_start",
                   "aet": "animation_end", "animationEndTime": "animation_end", "by": "step"}
        command = re.fullmatch(r"\s*playbackOptions\s+((?:-[A-Za-z]+\s+[-+]?\d+(?:\.\d+)?\s*)+);?\s*", script)
        if not command:
            if strict:
                raise RuntimeError("保存された再生範囲を安全に解釈できません: sceneConfigurationScriptNode")
            command_text = ""
        else:
            command_text = command.group(1)
        for flag, number in re.findall(r"-([A-Za-z]+)\s+([-+]?\d+(?:\.\d+)?)", command_text):
            if flag not in aliases:
                if strict:
                    raise RuntimeError("未対応の再生範囲フラグ: " + flag)
                continue
            values[aliases[flag]] = float(number)
    return {"playback": {"start": values["start"], "end": values["end"], "step": values["step"]},
            "animation": {"start": values["animation_start"], "end": values["animation_end"], "step": values["step"]},
            "render": {"start": cmds.getAttr("defaultRenderGlobals.startFrame"),
                       "end": cmds.getAttr("defaultRenderGlobals.endFrame"),
                       "step": cmds.getAttr("defaultRenderGlobals.byFrameStep")}}


def camera_info():
    import maya.cmds as cmds
    return [{"name": node, "shape": node,
             "transform": (cmds.listRelatives(node, parent=True, fullPath=True) or [node])[0],
             "enabled": bool(cmds.getAttr(node + ".renderable")),
             "renderable": bool(cmds.getAttr(node + ".renderable")),
             "default": bool(cmds.camera(node, query=True, startupCamera=True))}
            for node in cmds.ls(type="camera", long=True) or []]


def inspect_catalog(request):
    """Basic scene queries, matching audit_all_latest_ranges.py; no renderer validation."""
    import maya.cmds as cmds
    cmds.workspace(request["project"], openWorkspace=True)
    configure_mappings(request)
    cmds.file(request["scene"], open=True, force=True, prompt=False,
              loadReferenceDepth="none", executeScriptNodes=False)
    ranges = scene_ranges(strict=False)
    cameras = camera_info()
    resolution = {"width": cmds.getAttr("defaultResolution.width"), "height": cmds.getAttr("defaultResolution.height"),
                  "pixel_aspect": cmds.getAttr("defaultResolution.pixelAspect")}
    unknown_nodes = cmds.ls(type="unknown") or []
    unknown_plugins = cmds.unknownPlugin(query=True, list=True) or []
    warnings = []
    if unknown_nodes:
        warnings.append("未知のノード（情報取得は継続）: " + ", ".join(unknown_nodes))
    if unknown_plugins:
        warnings.append("未知のプラグイン（情報取得は継続）: " + ", ".join(unknown_plugins))
    result = {"inspection": "catalog", "ranges": ranges, "time_unit": cmds.currentUnit(query=True, time=True),
            "resolution": resolution, "cameras": cameras,
            "non_default_cameras": [c for c in cameras if not c["default"]],
            "settings": {**ranges["playback"], "step": ranges["render"]["step"], "range_source": "playback",
                         "width": resolution["width"], "height": resolution["height"], "fps": frame_rate(),
                         "cameras": [c["name"] for c in cameras if not c["default"] and c["renderable"]]},
            "warnings": warnings, "unknown_nodes": unknown_nodes, "unknown_plugins": unknown_plugins,
            "maya_version": str(cmds.about(version=True))[:4], "references_loaded": False}
    # Save all available job settings here; enqueue never opens the source again.
    def attr(name, default):
        return cmds.getAttr(name) if cmds.objExists(name) else default
    result["settings"].update(format=attr("defaultArnoldDriver.aiTranslator", "png"),
        aa=attr("defaultArnoldRenderOptions.AASamples", 3),
        adaptive=bool(attr("defaultArnoldRenderOptions.enableAdaptiveSampling", False)),
        device="GPU" if attr("defaultArnoldRenderOptions.renderDevice", 0) == 1 else "CPU",
        oidn="scene", post={"video": False, "fastdvdnet": False})
    result["layers"] = layers()
    result["settings"]["layers"] = [l["name"] for l in result["layers"] if l["enabled"]]
    aovs = {}
    original_layer = cmds.editRenderLayerGlobals(query=True, currentRenderLayer=True)
    try:
        for layer in result["layers"]:
            activate_layer(layer["name"])
            for node in cmds.ls(type="aiAOV") or []:
                if cmds.objExists(node + ".enabled") and cmds.getAttr(node + ".enabled"):
                    name = cmds.getAttr(node + ".name")
                    aovs.setdefault(name, {"name": name, "layers": [], "type": cmds.getAttr(node + ".type")})["layers"].append(layer["name"])
    finally:
        activate_layer(original_layer)
    result["aovs"] = list(aovs.values())
    result["settings"]["aovs"] = [a["name"] for a in result["aovs"]]
    result["plugins"] = {}
    for plugin in cmds.pluginInfo(query=True, listPlugins=True) or []:
        types = cmds.pluginInfo(plugin, query=True, dependNode=True) or []
        if plugin == "mtoa" or any(cmds.ls(type=typ) for typ in types if typ in cmds.allNodeTypes()):
            result["plugins"][plugin] = cmds.pluginInfo(plugin, query=True, version=True)
    result["attributes"] = file_attributes()
    result["dependencies"] = sorted(set(result["attributes"].values()) | set(cmds.file(query=True, reference=True) or []))
    result["references"] = saved_reference_settings(request["scene"])
    result["unknown_node_details"] = unknown_scene_details()
    result["enqueue_ready"] = True
    return result


def saved_reference_settings(scene):
    """Read persisted reference load states without loading reference nodes."""
    import maya.cmds as cmds
    cmds.file(scene, open=True, buildLoadSettings=True, executeScriptNodes=False)
    def scalar(value):
        return value[0] if isinstance(value, (tuple, list)) else value
    found = {}
    for index in range(1, cmds.selLoadSettings(query=True, numSettings=True)):
        node = scalar(cmds.selLoadSettings(str(index), query=True, referenceNode=True))
        found[node] = {"loaded": not bool(scalar(cmds.selLoadSettings(str(index), query=True, deferReference=True))),
                       "path": scalar(cmds.selLoadSettings(str(index), query=True, fileName=True))}
    return found


def unknown_scene_details():
    import maya.cmds as cmds
    editor_classes = {"nodeGraphEditorInfo", "nodeGraphEditorBookmarks", "nodeGraphEditorBookmarkInfo"}
    editor_tags = {0x4e474549, 0x4e474542, 0x4e454249}
    nodes = []
    for node in sorted(set((cmds.ls(type="unknown") or []) + (cmds.ls(type="unknownDag") or []) +
                           (cmds.ls(type="unknownTransform") or []))):
        values = {}
        for flag in ("realClassName", "realClassTag", "plugin"):
            try:
                values[flag] = cmds.unknownNode(node, query=True, **{flag: True})
            except RuntimeError:
                values[flag] = None
        nodes.append({"node": node, **values, "editor_only": values["realClassName"] in editor_classes
                      or values["realClassTag"] in editor_tags})
    plugins = []
    for plugin in cmds.unknownPlugin(query=True, list=True) or []:
        try:
            types = cmds.unknownPlugin(plugin, query=True, nodeTypes=True) or []
            data = cmds.unknownPlugin(plugin, query=True, dataTypes=True) or []
            editor_only = not data and set(types) <= editor_classes
        except RuntimeError:
            types, data, editor_only = [], [], False
        plugins.append({"plugin": plugin, "node_types": types, "data_types": data, "editor_only": editor_only})
    return {"nodes": nodes, "plugins": plugins}


def unknown_scene_warnings(details):
    warnings, errors = [], []
    for node in details["nodes"]:
        description = node["node"] + " / 型=" + str(node.get("realClassName") or node.get("realClassTag")) + " / プラグイン=" + str(node.get("plugin") or "不明")
        if node["editor_only"]:
            warnings.append("エディター保存情報の未知ノードを保持して続行: " + description)
        else:
            errors.append(description)
    for plugin in details["plugins"]:
        description = plugin["plugin"] + " / ノード型=" + ", ".join(plugin["node_types"]) + " / データ型=" + ", ".join(plugin["data_types"])
        if plugin["editor_only"]:
            warnings.append("未使用またはエディター情報のみのプラグイン記録: " + description)
        else:
            errors.append(description)
    if errors:
        raise RuntimeError("必要なノード・プラグインを読み込めません。対象を確認してください:\n" + "\n".join(errors))
    return warnings


def inspect_scene(request):
    if request.get("catalog_only"):
        return inspect_catalog(request)
    import maya.cmds as cmds
    from farm.assets import expand_asset
    lightweight = bool(request.get("lightweight")) and not request.get("sources")
    setup_arnold()
    cmds.workspace(request["project"], openWorkspace=True)
    configure_mappings(request)
    open_scene(request)
    unknown_details = unknown_scene_details()
    if request.get("result"):
        atomic_json(Path(request["result"]).with_name("unknown_nodes.json"), unknown_details)
    unknown_warnings = unknown_scene_warnings(unknown_details)
    attrs = file_attributes()
    dependencies = set(attrs.values())
    dependencies.update(cmds.file(query=True, reference=True) or [])
    dependencies = sorted({value.rsplit("{", 1)[0] if value.endswith("}") else value for value in dependencies})
    missing = sorted(set(request.get("_missing_assets", [])) | {dep for dep in dependencies if not expand_asset(dep, request["project"])})
    cameras = camera_info()
    layer_info = layers()
    aovs = {}
    original_layer = cmds.editRenderLayerGlobals(query=True, currentRenderLayer=True)
    for layer in ([{"name": original_layer}] if lightweight else layer_info):
        if not lightweight:
            activate_layer(layer["name"])
        for node in cmds.ls(type="aiAOV") or []:
            name = cmds.getAttr(node + ".name")
            if name and cmds.getAttr(node + ".enabled"):
                aovs.setdefault(name, {"name": name, "layers": [], "type": cmds.getAttr(node + ".type")})["layers"].append(layer["name"])
    try:
        if not lightweight:
            activate_layer(original_layer)
    except Exception:
        pass
    plugins = {"mtoa": cmds.pluginInfo("mtoa", query=True, version=True)}
    for name in ([] if lightweight else cmds.pluginInfo(query=True, listPlugins=True) or []):
        try:
            if cmds.pluginInfo(name, query=True, dependNode=True):
                types = cmds.pluginInfo(name, query=True, dependNode=True) or []
                if any(cmds.ls(type=typ) for typ in types if typ in cmds.allNodeTypes()):
                    plugins[name] = cmds.pluginInfo(name, query=True, version=True)
        except RuntimeError:
            pass
    ranges = scene_ranges()
    settings = {**ranges["playback"], "range_source": "playback",
        "width": cmds.getAttr("defaultResolution.width"), "height": cmds.getAttr("defaultResolution.height"),
        "fps": frame_rate(), "format": cmds.getAttr("defaultArnoldDriver.aiTranslator"),
        "aa": cmds.getAttr("defaultArnoldRenderOptions.AASamples"),
        "adaptive": bool(cmds.getAttr("defaultArnoldRenderOptions.enableAdaptiveSampling")),
        "device": "GPU" if cmds.getAttr("defaultArnoldRenderOptions.renderDevice") == 1 else "CPU",
        "cameras": [c["name"] for c in cameras if c["enabled"] and not c["default"]],
        "layers": [l["name"] for l in layer_info if l["enabled"]], "aovs": sorted(aovs),
        "oidn": "scene", "post": {"video": False, "fastdvdnet": False}}
    reference_states = {}
    for node in cmds.ls(type="reference") or []:
        if node == "sharedReferenceNode":
            continue
        try:
            reference_states[node] = {"loaded": cmds.referenceQuery(node, isLoaded=True),
                "path": cmds.referenceQuery(node, filename=True, withoutCopyNumber=True)}
        except RuntimeError:
            pass
    return {"settings": settings, "ranges": ranges, "time_unit": cmds.currentUnit(query=True, time=True),
            "resolution": {"width": settings["width"], "height": settings["height"],
                           "pixel_aspect": cmds.getAttr("defaultResolution.pixelAspect")},
            "non_default_cameras": [c for c in cameras if not c["default"]],
            "cameras": cameras, "layers": layer_info, "aovs": list(aovs.values()),
            "plugins": plugins, "dependencies": dependencies, "attributes": attrs, "missing": missing,
            "references": reference_states, "maya_version": str(cmds.about(version=True))[:4],
            "inspection": "lightweight" if lightweight else "full",
            "path_mappings": request.get("mappings", []),
            "unknown_node_details": unknown_details,
            "warnings": unknown_warnings + ["旧パスを読み替え: " + source + " -> " + target
                                            for source, target in request.get("mappings", [])]}


def configure_mappings(request):
    import maya.cmds as cmds
    cmds.dirmap(enable=True)
    for source, target in request.get("mappings", []):
        cmds.dirmap(mapDirectory=(source.replace("\\", "/"), target.replace("\\", "/")))


def mapped_path(value, request):
    old = value.replace("\\", "/")
    for source, target in sorted(request.get("mappings", []), key=lambda x: len(x[0]), reverse=True):
        source = source.replace("\\", "/").rstrip("/")
        if old.casefold() == source.casefold() or old.casefold().startswith(source.casefold() + "/"):
            return target.replace("\\", "/").rstrip("/") + old[len(source):]
    if not request.get("sources"):
        import re
        project = Path(request["project"]).resolve()
        match = re.search(r"(?:^|/)" + re.escape(project.name) + r"/+(.*)$", old, re.IGNORECASE)
        if match:
            target = (project / match.group(1)).resolve()
            if target.is_relative_to(project) and target.exists():
                return str(target)
    return str(Path(request["project"]) / old) if not Path(old).is_absolute() else old


def open_scene(request):
    import maya.cmds as cmds
    if request.get("job") or request.get("mappings"):
        # Match the existing MUR renderer's stable DG mode when evaluating complex rigs.
        cmds.evaluationManager(mode="off")
    if not request.get("sources") and not request.get("portable"):
        options = {"loadReferenceDepth": "none"} if request.get("lightweight") else {}
        cmds.file(request["scene"], open=True, force=True, prompt=False, executeScriptNodes=False, **options)
        if request.get("mappings") and not request.get("lightweight"):
            # Native full loading evaluates the complete rig graph, matching the MUR renderer.
            # Replace only references that still resolved to an old existing project copy.
            for node in cmds.ls(type="reference") or []:
                if node == "sharedReferenceNode" or node.rsplit(":", 1)[-1].startswith("_UNKNOWN_REF_NODE_"):
                    continue
                if not cmds.referenceQuery(node, isLoaded=True):
                    continue
                old = cmds.referenceQuery(node, filename=True, withoutCopyNumber=True)
                target = mapped_path(old, request)
                if Path(old).resolve() != Path(target).resolve():
                    print("参照パス読み替え: " + old + " -> " + target, flush=True)
                    cmds.file(target, loadReference=node, executeScriptNodes=False)
        if not request.get("lightweight"):
            remap_attributes(request)
        return
    # Directory mapping alone does not replace an original reference that still exists.
    # Open without references, then load only snapshot references, preserving saved load states.
    cmds.file(request["scene"], open=True, force=True, prompt=False, executeScriptNodes=False, loadReferenceDepth="none")
    saved = request.get("metadata", {}).get("references", {})
    if request.get("metadata", {}).get("inspection") == "catalog" and not saved:
        saved = saved_reference_settings(request["scene"])
    processed = set()
    while True:
        nodes = [n for n in cmds.ls(type="reference") or [] if n != "sharedReferenceNode" and n not in processed]
        if not nodes:
            break
        for node in nodes:
            processed.add(node)
            old = cmds.referenceQuery(node, filename=True, withoutCopyNumber=True)
            target = mapped_path(old, request)
            if (request.get("sources") and not Path(target).resolve().is_relative_to(Path(request["sources"]).resolve())) or not Path(target).is_file():
                raise RuntimeError("参照をスナップショット内に解決できません: " + old)
            # Old snapshots without reference metadata must be inspected again, not guessed.
            if node not in saved:
                raise RuntimeError("参照のロード状態が不明です。シーンを再調査してください: " + node)
            print("参照読み込み: " + node + " -> " + target, flush=True)
            cmds.file(target, loadReference=node, loadReferenceDepth="topOnly" if saved[node]["loaded"] else "none",
                      executeScriptNodes=False)
    remap_attributes(request)


def remap_attributes(request):
    import maya.cmds as cmds
    from farm.assets import expand_asset
    from farm.asset_paths import TextureResolver
    resolver = TextureResolver(request["project"])
    relinked = []
    mappings = sorted(request.get("mappings", []), key=lambda x: len(x[0]), reverse=True)
    missing = []
    pattern_attrs = file_attributes(patterns=True)
    for attr, value in file_attributes(patterns=False).items():
        old = value.replace("\\", "/")
        new = mapped_path(value, request) if not request.get("sources") else old
        for source, target in mappings:
            source = source.replace("\\", "/").rstrip("/")
            if old.casefold() == source.casefold() or old.casefold().startswith(source.casefold() + "/"):
                new = target.replace("\\", "/").rstrip("/") + old[len(source):]
                break
        if not Path(new).is_absolute():
            new = str(Path(request["project"]) / new)
        if not request.get("sources") and not expand_asset(new, request["project"]) and attr.rsplit(".", 1)[-1] in {"fileTextureName", "filename", "imageName"}:
            alternative = resolver.resolve(value)
            if alternative:
                new = alternative
                request.setdefault("mappings", []).append([value, new])
                relinked.append({"attribute": attr, "source": value, "target": new})
                print("素材パス読み替え: " + value + " -> " + new, flush=True)
        if request.get("job") and request.get("sources") and not new.isascii():
            # Keep Arnold's narrow Windows file API away from non-ASCII asset names.
            # Copies remain under this job's sources; Maya reference files are untouched.
            import re
            import shutil
            def ascii_leaf(name):
                return re.sub(r"[^\x00-\x7f]+", lambda m: "u" + digest(m.group())[:12], name)
            pattern = mapped_path(pattern_attrs.get(attr, value), request)
            matches = expand_asset(pattern, request["project"])
            if any(not p.is_relative_to(Path(request["sources"]).resolve()) for p in matches):
                raise RuntimeError("スナップショット外の素材はコピーできません: " + value)
            alias_dir = Path(request["sources"]) / "_render_assets" / digest(pattern)[:20]
            alias_dir.mkdir(parents=True, exist_ok=True)
            directory_input = Path(new).is_dir()
            for source in matches:
                relative = source.relative_to(Path(new).resolve()) if directory_input else Path(source.name)
                target = alias_dir.joinpath(*(ascii_leaf(p) for p in relative.parts))
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() or sha256(target) != sha256(source):
                    shutil.copyfile(source, target)
            new = str(alias_dir if directory_input else alias_dir / ascii_leaf(Path(new).name))
        if new != value:
            try:
                cmds.setAttr(attr, new, type="string")
            except RuntimeError as exc:
                raise RuntimeError("素材パスを再設定できません: " + attr) from exc
        matched = expand_asset(new, request["project"])
        if not matched and attr in pattern_attrs:
            matched = expand_asset(mapped_path(pattern_attrs[attr], request), request["project"])
        if not matched:
            missing.append(new)
        for path in matched:
            if request.get("sources") and not path.is_relative_to(Path(request["sources"]).resolve()):
                missing.append("スナップショット外の素材: " + str(path))
    for ref in cmds.file(query=True, reference=True) or []:
        clean = ref.rsplit("{", 1)[0] if ref.endswith("}") else ref
        if request.get("sources") and not Path(clean).resolve().is_relative_to(Path(request["sources"]).resolve()):
            missing.append("スナップショット外の参照: " + clean)
    if request.get("work") or request.get("result"):
        folder = Path(request["work"]) if request.get("work") else Path(request["result"]).parent
        atomic_json(folder / "asset_paths_report.json", {"relinked": relinked, "missing": sorted(set(missing)),
                    "missing_policy": "warn" if request.get("job") or request.get("mode") == "render" else "inspect",
                    "mappings": request.get("mappings", [])})
    if missing:
        missing = sorted(set(missing))
        if request.get("mode") == "inspect":
            request["_missing_assets"] = missing
        elif request.get("job") or request.get("mode") == "render":
            outside = [p for p in missing if p.startswith(("スナップショット外の素材:", "スナップショット外の参照:"))]
            if outside:
                raise RuntimeError("素材の再配置に失敗しました:\n" + "\n".join(outside))
            request["_missing_assets"] = missing
            request["_asset_warnings"] = ["不足素材を無視して続行: " + path for path in missing]
            for warning in request["_asset_warnings"]:
                print("警告: " + warning, flush=True)
            # Missing textures otherwise abort Arnold even after our preflight continues.
            if cmds.objExists("defaultArnoldRenderOptions.abortOnError"):
                cmds.setAttr("defaultArnoldRenderOptions.abortOnError", False)
        else:
            raise RuntimeError("素材の再配置に失敗しました:\n" + "\n".join(missing))


def capabilities(request):
    import maya.cmds as cmds
    setup_arnold()
    plugins = {}
    for name in request.get("plugins", ["mtoa"]):
        try:
            cmds.loadPlugin(name, quiet=True)
            plugins[name] = cmds.pluginInfo(name, query=True, version=True)
        except Exception:
            pass
    import subprocess
    from farm.common import command_flags
    try:
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, timeout=15, **command_flags()).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        gpu = False
    mtoa = Path(cmds.pluginInfo("mtoa", query=True, path=True)).parent.parent
    candidates = [mtoa / "bin/oiiotool.exe", mtoa.parent / "bin/oiiotool.exe"]
    oiio = next((str(p) for p in candidates if p.exists()), "")
    return {"plugins": plugins, "gpu": gpu, "oiiotool": oiio,
            "maya_version": str(cmds.about(version=True))[:4]}


class RenderSession:
    def __init__(self, request):
        self.request = request
        self.work = Path(request["work"])
        self.settings = request["job"]["settings"]
        self.checkpoint_path = self.work / "frames.json"
        self.frames = read_json(self.checkpoint_path, {})
        self.units = [(layer, camera, frame) for layer in self.settings["layers"]
                      for camera in self.settings["cameras"]
                      for frame in range(self.settings["start"], self.settings["end"] + 1, self.settings["step"])]
        self.index = 0
        self.loading_started = None
        self.loading_seconds = None

    def status(self, state, **values):
        atomic_json(self.work / "maya_status.json", {"state": state, "attempt": self.request["attempt"],
                    "updated": time.time(), "completed": len(self.frames), "total": len(self.units),
                    "loading_started": self.loading_started, "loading_seconds": self.loading_seconds,
                    "max_frame_seconds": max((r.get("seconds", 0) for r in self.frames.values()), default=None),
                    "asset_warnings": self.request.get("_asset_warnings", []),
                    **values})

    def guard(self):
        from farm.store import Store
        job = Store(self.request["root"]).owned(self.request["job"]["id"], self.request["job"]["token"])
        command = read_json(self.work / "control.json", {})
        return not (job.get("control") or command.get("stop"))

    def begin(self):
        from PySide6.QtCore import QTimer
        import maya.cmds as cmds
        try:
            self.status("opening")
            if not self.guard():
                return self.quit("paused")
            setup_arnold()
            if str(cmds.about(version=True))[:4] != self.request["job"]["maya_version"]:
                raise RuntimeError("Mayaバージョンが一致しません")
            required_plugins = self.request["job"].get(
                "plugin_versions", self.request["job"].get("plugins", {}))
            for plugin in {"mtoa", *required_plugins}:
                try:
                    cmds.loadPlugin(plugin, quiet=True)
                except Exception as exc:
                    raise RuntimeError("必要なプラグインを読み込めません: " + plugin) from exc
            configure_mappings(self.request)
            self.status("opening", phase="プロジェクトを設定中")
            cmds.workspace(self.request["project"], openWorkspace=True)
            self.loading_started = time.time()
            loading_start = time.monotonic()
            self.status("opening", phase="シーン・参照を読み込み中", scene=self.request["scene"])
            open_scene(self.request)
            self.loading_seconds = time.monotonic() - loading_start
            cmds.evaluationManager(mode="off")
            self.status("opening", phase="再開記録を確認中")
            # Validate checkpoints by content, not merely filename or frame count.
            from farm.images import verify_record
            expected_units = {digest(unit)[:24]: unit for unit in self.units}
            for key, record in list(self.frames.items()):
                if record.get("signature") != self.request["job"]["signature"]:
                    raise RuntimeError("既存フレームのジョブ設定が一致しません")
                if key not in expected_units or (record.get("layer"), record.get("camera"), record.get("frame")) != expected_units[key]:
                    raise RuntimeError("既存フレームのカメラ・レイヤー・フレームが一致しません")
                try:
                    verify_record(self.work, record, self.request["oiiotool"])
                except (ValueError, FileNotFoundError):
                    # Keep evidence of damaged output and regenerate only this unit.
                    from farm.common import inside
                    target = self.work / "quarantine" / (key + "_invalid_" + str(time.time_ns()))
                    for file in record["files"]:
                        source = inside(self.work, file["path"])
                        if source.exists():
                            quarantine = target / file["path"]
                            quarantine.parent.mkdir(parents=True, exist_ok=True)
                            source.rename(quarantine)
                    del self.frames[key]
            atomic_json(self.checkpoint_path, self.frames)
            if any(Path(file["path"]).parent != Path("raw") for record in self.frames.values() for file in record["files"]):
                from farm.images import migrate_raw_checkpoint
                report = migrate_raw_checkpoint(self.work)
                self.frames = read_json(self.checkpoint_path, {})
                print("旧raw画像を移行: " + str(report["moved"]) + "件", flush=True)
            QTimer.singleShot(0, self.next_frame)
        except Exception:
            self.fail()

    def next_frame(self):
        from PySide6.QtCore import QTimer
        import maya.cmds as cmds
        try:
            try:
                proceed = self.guard()
            except (OSError, TimeoutError):
                try:
                    self.status("waiting_io")
                except OSError:
                    pass
                QTimer.singleShot(5000, self.next_frame)
                return
            if not proceed:
                return self.quit("paused")
            # A previous successful render may have lost its checkpoint write during an SMB outage.
            atomic_json(self.checkpoint_path, self.frames)
            while self.index < len(self.units) and digest(self.units[self.index])[:24] in self.frames:
                self.index += 1
            if self.index == len(self.units):
                return self.quit("render_complete")
            layer, camera, frame = self.units[self.index]
            key = digest(self.units[self.index])[:24]
            unitdir = self.work / "rendering" / key
            if unitdir.exists():
                quarantine = self.work / "quarantine" / (key + "_" + str(time.time_ns()))
                quarantine.parent.mkdir(parents=True, exist_ok=True)
                unitdir.rename(quarantine)
            unitdir.mkdir(parents=True)
            self.status("rendering", unit=key, frame=frame, layer=layer, camera=camera, frame_started=time.time())
            start = time.monotonic()
            activate_layer(layer)
            cmds.setAttr("defaultRenderGlobals.currentRenderer", "arnold", type="string")
            for attr, value in {"defaultRenderGlobals.animation": False,
                "defaultRenderGlobals.useRenderRegion": False,
                "defaultResolution.width": self.settings["width"], "defaultResolution.height": self.settings["height"],
                "defaultResolution.deviceAspectRatio": self.settings["width"] / self.settings["height"],
                "defaultArnoldRenderOptions.renderDevice": int(self.settings["device"] == "GPU"),
                "defaultArnoldRenderOptions.AASamples": self.settings["aa"],
                "defaultArnoldRenderOptions.enableAdaptiveSampling": self.settings["adaptive"],
                "defaultArnoldRenderOptions.log_to_file": True,
                "defaultArnoldRenderOptions.log_verbosity": 2}.items():
                cmds.setAttr(attr, value)
            aovs = []
            if cmds.objExists("defaultArnoldDisplayDriver.aiTranslator"):
                cmds.setAttr("defaultArnoldDisplayDriver.aiTranslator", "maya", type="string")
            for node in cmds.ls(type="aiAOV") or []:
                name = cmds.getAttr(node + ".name")
                layer_aovs = {a["name"] for a in self.request["metadata"]["aovs"] if layer in a["layers"]}
                enabled = name in layer_aovs and name in self.settings.get("aovs", [])
                cmds.setAttr(node + ".enabled", enabled)
                if enabled:
                    aovs.append(name)
                    driver_name = "PortableFarmAOV_" + digest(name)[:12]
                    driver = driver_name if cmds.objExists(driver_name) else cmds.createNode("aiAOVDriver", name=driver_name)
                    cmds.setAttr(driver + ".aiTranslator", "exr", type="string")
                    cmds.setAttr(driver + ".prefix", str(unitdir / "<RenderPass>"), type="string")
                    if cmds.objExists(driver + ".mergeAOVs"):
                        cmds.setAttr(driver + ".mergeAOVs", False)
                    for index in cmds.getAttr(node + ".outputs", multiIndices=True) or [0]:
                        plug = node + ".outputs[%d].driver" % index
                        for source in cmds.listConnections(plug, source=True, destination=False, plugs=True) or []:
                            cmds.disconnectAttr(source, plug)
                    cmds.connectAttr(driver + ".message", node + ".outputs[0].driver", force=True)
            # Separate AOV files retain data type/channels while giving a deterministic manifest.
            for driver in cmds.ls(type="aiAOVDriver") or []:
                if driver == "defaultArnoldDisplayDriver" or driver.startswith("PortableFarmAOV_"):
                    continue
                cmds.setAttr(driver + ".aiTranslator", self.settings["format"], type="string")
                if cmds.objExists(driver + ".mergeAOVs"):
                    cmds.setAttr(driver + ".mergeAOVs", False)
                if cmds.objExists(driver + ".prefix"):
                    cmds.setAttr(driver + ".prefix", str(unitdir / "<RenderPass>"), type="string")
            cmds.setAttr("defaultRenderGlobals.imageFilePrefix", str(unitdir / "<RenderPass>"), type="string")
            log = self.work / "logs" / (key + ".log")
            log.parent.mkdir(exist_ok=True)
            cmds.setAttr("defaultArnoldRenderOptions.log_filename", str(log), type="string")
            if self.settings.get("oidn") != "scene":
                imagers = cmds.listConnections("defaultArnoldRenderOptions.imagers", source=True, destination=False) or []
                oidns = [n for n in imagers if cmds.nodeType(n) == "aiImagerDenoiserOidn"]
                if self.settings["oidn"] == "on" and not oidns:
                    node = cmds.createNode("aiImagerDenoiserOidn", name="PortableFarmOIDN")
                    indices = cmds.getAttr("defaultArnoldRenderOptions.imagers", multiIndices=True) or []
                    cmds.connectAttr(node + ".message", "defaultArnoldRenderOptions.imagers[%d]" % (max(indices, default=-1) + 1))
                    oidns = [node]
                for n in imagers + oidns:
                    if "Denoiser" in cmds.nodeType(n):
                        cmds.setAttr(n + ".enable", bool(self.settings["oidn"] == "on" and n == oidns[0]))
            cmds.currentTime(frame, edit=True)
            cmds.dgdirty(allPlugs=True)
            cmds.refresh(force=True)
            cmds.arnoldRender(seq="", cam=camera, w=self.settings["width"], h=self.settings["height"])
            from farm.images import collect_outputs, flatten_outputs
            files = collect_outputs(unitdir, self.settings, aovs, self.request["oiiotool"], frame=frame)
            files = flatten_outputs(self.work, unitdir, files, layer, camera, key)
            record = {"unit": key, "layer": layer, "camera": camera, "frame": frame,
                      "seconds": time.monotonic() - start, "signature": self.request["job"]["signature"],
                      "files": files}
            self.frames[key] = record
            atomic_json(self.checkpoint_path, self.frames)
            self.status("between_frames", unit=key)
            self.index += 1
            QTimer.singleShot(0, self.next_frame)
        except OSError:
            try:
                self.status("waiting_io")
            except OSError:
                pass
            QTimer.singleShot(5000, self.next_frame)
        except Exception:
            self.fail()

    def quit(self, state):
        import maya.cmds as cmds
        self.status(state)
        cmds.quit(force=True, exitCode=0)

    def fail(self):
        import maya.cmds as cmds
        try:
            self.status("error", error=traceback.format_exc())
        finally:
            cmds.quit(force=True, exitCode=1)


def gui_start():
    from PySide6.QtCore import QTimer
    global _session
    _session = RenderSession(read_json(os.environ["PORTABLE_FARM_REQUEST"]))
    QTimer.singleShot(1000, _session.begin)


def main():
    request = read_json(sys.argv[1])
    try:
        import maya.standalone
        maya.standalone.initialize(name="python")
        if request["mode"] == "capabilities":
            result = capabilities(request)
        elif request["mode"] == "inspect":
            result = inspect_scene(request)
        elif request["mode"] == "verify_package":
            setup_arnold()
            import maya.cmds as cmds
            configure_mappings(request)
            cmds.workspace(request["project"], openWorkspace=True)
            open_scene(request)
            result = {"verified": True}
        elif request["mode"] == "make_fixture":
            from farm.test_scene import make_fixture
            result = make_fixture(request)
        else:
            raise ValueError(request["mode"])
        atomic_json(request["result"], result)
        maya.standalone.uninitialize()
    except Exception:
        atomic_json(request["result"], {"error": traceback.format_exc()})
        # Maya shutdown callbacks are sometimes unreliable after a failed open.
        sys.exit(1)


if __name__ == "__main__":
    main()

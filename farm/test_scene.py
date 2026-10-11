"""Creates a tiny, original project used for isolated integration checks."""
from pathlib import Path


def make_fixture(request):
    import maya.cmds as cmds
    from farm.maya_entry import setup_arnold
    project = Path(request["project"])
    (project / "scenes").mkdir(parents=True, exist_ok=True)
    cmds.file(new=True, force=True)
    cmds.currentUnit(time=request.get("time_unit", "film"))
    cmds.workspace(str(project), newWorkspace=True)
    cmds.workspace(fileRule=["scene", "scenes"])
    cmds.workspace(saveWorkspace=True)
    setup_arnold()
    sphere = cmds.polySphere(name="TestSphere")[0]
    cmds.setKeyframe(sphere, attribute="rotateY", time=1, value=0)
    cmds.setKeyframe(sphere, attribute="rotateY", time=2, value=15)
    shader = cmds.shadingNode("aiStandardSurface", asShader=True, name="TestMaterial")
    cmds.setAttr(shader + ".baseColor", .2, .5, .8, type="double3")
    shading = cmds.sets(renderable=True, noSurfaceShader=True, empty=True)
    cmds.connectAttr(shader + ".outColor", shading + ".surfaceShader")
    cmds.sets(sphere, edit=True, forceElement=shading)
    if request.get("external"):
        from PySide6.QtGui import QImage, QColor
        external = Path(request["external"])
        external.mkdir(parents=True, exist_ok=True)
        image = QImage(8, 8, QImage.Format_RGB32)
        image.fill(QColor(80, 120, 180))
        texture = external / "texture.png"
        image.save(str(texture))
        node = cmds.shadingNode("file", asTexture=True)
        cmds.setAttr(node + ".fileTextureName", str(texture), type="string")
        cmds.connectAttr(node + ".outColor", shader + ".baseColor", force=True)
        if request.get("reference_camera"):
            cmds.camera(name="ReferenceCamera")
        ref = external / "component.ma"
        cmds.file(rename=str(ref))
        cmds.file(save=True, type="mayaAscii")
        cmds.file(new=True, force=True)
        cmds.file(str(ref), reference=True, namespace="asset", executeScriptNodes=False)
    light = cmds.shadingNode("aiSkyDomeLight", asLight=True, name="TestSky")
    cmds.setAttr(light + ".intensity", .8)
    camera, shape = cmds.camera(name="TestCamera")
    cmds.setAttr(camera + ".translateZ", 5)
    for node in cmds.ls(type="camera"):
        cmds.setAttr(node + ".renderable", node == shape)
    if request.get("second_camera"):
        other, other_shape = cmds.camera(name="SecondCamera")
        cmds.setAttr(other + ".translateZ", 6)
        cmds.setAttr(other_shape + ".renderable", True)
    cmds.setAttr("defaultRenderGlobals.currentRenderer", "arnold", type="string")
    for attr, value in {"defaultRenderGlobals.startFrame": 1, "defaultRenderGlobals.endFrame": 2,
                        "defaultRenderGlobals.byFrameStep": request.get("render_step", 1),
                        "defaultResolution.width": 64, "defaultResolution.height": 64,
                        "defaultArnoldRenderOptions.AASamples": 1,
                        "defaultArnoldRenderOptions.renderDevice": 0}.items():
        cmds.setAttr(attr, value)
    cmds.setAttr("defaultArnoldDriver.aiTranslator", "png", type="string")
    playback = request.get("playback_range", [1, 2, 1])
    animation = request.get("animation_range", [1, 2])
    cmds.playbackOptions(minTime=playback[0], maxTime=playback[1], by=playback[2],
                         animationStartTime=animation[0], animationEndTime=animation[1])
    if request.get("aov"):
        import mtoa.aovs
        mtoa.aovs.AOVInterface().addAOV("N")
    if request.get("layers"):
        import maya.app.renderSetup.model.renderSetup as rs
        for name in ("LayerA", "LayerB"):
            layer = rs.instance().createRenderLayer(name)
            collection = layer.createCollection("AllObjects")
            collection.getSelector().setPattern("*")
    scene = project / "scenes" / "test.ma"
    if request.get("script_marker"):
        cmds.scriptNode(name="InspectionScriptMustNotRun", scriptType=1, sourceType="python",
                        beforeScript="from pathlib import Path; Path(" + repr(request["script_marker"]) + ").write_text('executed')")
    cmds.file(rename=str(scene))
    cmds.file(save=True, type="mayaAscii")
    return {"scene": str(scene)}

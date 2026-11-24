#!/usr/bin/env python3
"""Generate a deterministic Xcode project: one iOS app target plus optional
unit-test and UI-test targets.

Usage: python3 make_xcodeproj.py project.json

Writes <name>.xcodeproj/project.pbxproj and a shared scheme under the current
directory. Standard library only: no network, no Xcode GUI, no third-party tool.
Object identifiers derive from names, so regenerating does not churn the file.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SOURCE_TYPES = {
    ".swift": "sourcecode.swift", ".m": "sourcecode.c.objc", ".mm": "sourcecode.cpp.objcpp",
    ".c": "sourcecode.c.c", ".cpp": "sourcecode.cpp.cpp",
}
RESOURCE_TYPES = {
    ".xcassets": "folder.assetcatalog", ".storyboard": "file.storyboard", ".xib": "file.xib",
    ".strings": "text.plist.strings", ".json": "text.json", ".png": "image.png",
    ".jpg": "image.jpeg", ".plist": "text.plist.xml",
}
SKIP_DIRS = {".build", ".git", ".proto-build", "DerivedData", "Pods", "build", "node_modules"}
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
BUNDLE = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}")
INFO_KEY = re.compile(r"[A-Za-z0-9_]+")
PLAIN = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")
CONFIGS = ("Debug", "Release")
COMMON = {"ALWAYS_SEARCH_USER_PATHS": "NO", "CLANG_ENABLE_MODULES": "YES", "CLANG_ENABLE_OBJC_ARC": "YES",
          "SDKROOT": "iphoneos"}
MODES = {"Debug": {"DEBUG_INFORMATION_FORMAT": "dwarf", "ENABLE_TESTABILITY": "YES",
                   "GCC_PREPROCESSOR_DEFINITIONS": "DEBUG=1 $(inherited)", "ONLY_ACTIVE_ARCH": "YES",
                   "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "DEBUG $(inherited)",
                   "SWIFT_OPTIMIZATION_LEVEL": "-Onone"},
         "Release": {"DEBUG_INFORMATION_FORMAT": "dwarf-with-dsym", "ENABLE_NS_ASSERTIONS": "NO",
                     "SWIFT_OPTIMIZATION_LEVEL": "-O", "VALIDATE_PRODUCT": "YES"}}


class SpecError(ValueError):
    pass


class _Raw(str):
    """A string that still needs pbxproj quoting."""


class _Quoted(_Raw):
    """始终带引号的字符串（清单里用户给的值不猜类型）。"""


class _Ref:
    __slots__ = ("ident",)

    def __init__(self, ident):
        self.ident = ident


def _ident(kind, key):
    return hashlib.sha1((kind + "\x00" + key).encode("utf-8")).hexdigest()[:24].upper()


def _text(value):
    if value and PLAIN.fullmatch(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _Doc:
    def __init__(self):
        self.objects = {}
        self.comments = {}

    def add(self, ident, comment, fields):
        self.objects[ident] = fields
        self.comments[ident] = comment
        return ident

    def _value(self, value):
        if isinstance(value, _Ref):
            comment = self.comments[value.ident]
            return "%s /* %s */" % (value.ident, comment) if comment else value.ident
        if isinstance(value, _Quoted):
            return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int):
            return str(value)
        return _text(str(value))

    def _fields(self, fields, indent):
        out = []
        for key, value in fields.items():
            if isinstance(value, dict):
                out.append("%s%s = {\n" % (indent, key))
                out.append(self._fields(value, indent + "\t"))
                out.append("%s};\n" % indent)
            elif isinstance(value, (list, tuple)):
                if not value:
                    out.append("%s%s = ();\n" % (indent, key))
                    continue
                out.append("%s%s = (\n" % (indent, key))
                for item in value:
                    out.append("%s\t%s,\n" % (indent, self._value(item)))
                out.append("%s);\n" % indent)
            else:
                out.append("%s%s = %s;\n" % (indent, key, self._value(value)))
        return "".join(out)

    def render(self, root):
        out = ["// !$*UTF8*$!\n{\n\tarchiveVersion = 1;\n\tclasses = {\n\t};\n\tobjectVersion = 56;\n\tobjects = {\n"]
        for ident in sorted(self.objects):
            comment = " /* %s */" % self.comments[ident] if self.comments[ident] else ""
            out.append("\t\t%s%s = {\n" % (ident, comment))
            out.append(self._fields(self.objects[ident], "\t\t\t"))
            out.append("\t\t};\n")
        out.append("\t};\n\trootObject = %s /* Project object */;\n}\n" % root)
        return "".join(out)


def _setting(value):
    if isinstance(value, bool):
        return _Raw("YES" if value else "NO")
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return _Quoted(" ".join(str(item) for item in value))
    if isinstance(value, str):
        return _Raw(value) if value in ("YES", "NO") else _Quoted(value)
    raise SpecError("unsupported setting value: %r" % (value,))


def _per_mode(label, values):
    """Settings keyed Debug/Release apply to that mode; any other key applies to both."""
    values = values or {}
    if not isinstance(values, dict):
        raise SpecError("%s must be an object" % label)
    both = {key: _setting(value) for key, value in values.items() if key not in CONFIGS}
    modes = {}
    for mode in CONFIGS:
        extra = values.get(mode) or {}
        if not isinstance(extra, dict):
            raise SpecError("%s.%s must be an object" % (label, mode))
        modes[mode] = dict(both, **{key: _setting(value) for key, value in extra.items()})
    return modes


def _collect(root, entries, extensions, label):
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise SpecError("%s must be an array of paths" % label)
    found = []
    for entry in entries:
        if not isinstance(entry, str) or not entry:
            raise SpecError("%s must contain non-empty path strings" % label)
        relative = Path(entry)
        path = root / relative
        try:
            inside = (not relative.is_absolute() and ".." not in relative.parts
                      and path.exists() and path.resolve().is_relative_to(root.resolve()))
        except OSError:
            inside = False
        if not inside:
            raise SpecError("%s path must exist inside the project: %s" % (label, entry))
        if path.is_dir() and path.suffix not in extensions:
            for current, dirs, files in os.walk(path):
                dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS and not name.startswith("."))
                for name in sorted(files):
                    if Path(name).suffix in extensions and not name.startswith("."):
                        found.append(Path(current) / name)
        elif path.suffix in extensions:      # .xcassets 这类目录本身就是一个资源单元
            found.append(path)
        else:
            raise SpecError("%s does not take %s files: %s" % (label, path.suffix or "plain", entry))
    return sorted(set(found), key=lambda item: item.relative_to(root).as_posix())


def _load_test_part(root, spec, key, name, bundle):
    """tests 与 ui_tests 共用一份校验：命名/标识与 App 不同、来源只收 Swift。"""
    part = spec.get(key)
    if part is None:
        return None
    if not isinstance(part, dict):
        raise SpecError("%s must be an object with name, bundle_id and sources" % key)
    tname, tbundle = part.get("name", ""), part.get("bundle_id", "")
    if not isinstance(tname, str) or not NAME.fullmatch(tname) or tname == name:
        raise SpecError("%s.name must differ from name and use only letters, digits or underscores" % key)
    if not isinstance(tbundle, str) or not BUNDLE.fullmatch(tbundle) or tbundle == bundle:
        raise SpecError("%s.bundle_id must look like com.example.app.tests and differ from bundle_id" % key)
    sources = _collect(root, part.get("sources"), {".swift"}, key + ".sources")
    if not sources:
        raise SpecError("%s.sources listed no Swift files" % key)
    return {"name": tname, "bundle_id": tbundle, "sources": sources,
            "settings": _per_mode(key + ".settings", part.get("settings"))}


def load_spec(path, root):
    try:
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpecError("cannot read manifest %s: %s" % (path, exc))
    if not isinstance(spec, dict):
        raise SpecError("manifest must be a JSON object")
    name, bundle = spec.get("name", ""), spec.get("bundle_id", "")
    target, swift = spec.get("deployment_target", "17.0"), spec.get("swift_version", "5.0")
    family = spec.get("device_family", "1,2")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise SpecError("name must start with a letter and use only letters, digits or underscores")
    if not isinstance(bundle, str) or not BUNDLE.fullmatch(bundle):
        raise SpecError("bundle_id must look like com.example.app")
    if not VERSION.fullmatch(str(target)) or not VERSION.fullmatch(str(swift)):
        raise SpecError("deployment_target and swift_version must look like 17.0 and 5.0")
    if not re.fullmatch(r"[12](,[12])?", str(family)):
        raise SpecError("device_family must be 1 (iPhone), 2 (iPad) or 1,2")
    sources = _collect(root, spec.get("sources"), set(SOURCE_TYPES), "sources")
    if not sources:
        raise SpecError("sources listed no compilable files")
    resources = _collect(root, spec.get("resources"), set(RESOURCE_TYPES), "resources")
    info = spec.get("info") or {}
    if not isinstance(info, dict):
        raise SpecError("info must be an object")
    for key in info:
        if not isinstance(key, str) or not INFO_KEY.fullmatch(key):
            raise SpecError("info keys may use only letters, digits or underscores: %r" % (key,))
        _setting(info[key])
    tests = _load_test_part(root, spec, "tests", name, bundle)
    ui_tests = _load_test_part(root, spec, "ui_tests", name, bundle)
    if tests and ui_tests:
        for field in ("name", "bundle_id"):
            if tests[field] == ui_tests[field]:
                raise SpecError("ui_tests.%s must differ from tests.%s" % (field, field))
    return {"name": name, "bundle_id": bundle, "target": str(target), "swift": str(swift),
            "family": str(family), "sources": sources, "resources": resources, "info": info,
            "settings": _per_mode("settings", spec.get("settings")),
            "tests": tests, "ui_tests": ui_tests}


# 单元测试注入 App 进程（TEST_HOST/BUNDLE_LOADER）；UI 测试独立跑、由 TEST_TARGET_NAME
# 指回被测 App——XCUITest 的点按走查只认后一种 target。
TEST_TARGET_KINDS = (
    ("tests", "com.apple.product-type.bundle.unit-test"),
    ("ui_tests", "com.apple.product-type.bundle.ui-testing"),
)


def _test_extra_settings(key, app_name):
    host = "$(BUILT_PRODUCTS_DIR)/%s.app/$(BUNDLE_EXECUTABLE_FOLDER_PATH)/%s" % (app_name, app_name)
    if key == "tests":
        return {"BUNDLE_LOADER": _Raw("$(TEST_HOST)"), "TEST_HOST": _Raw(host)}
    return {"TEST_TARGET_NAME": _Raw(app_name)}


def build(spec, root):
    name = spec["name"]
    doc = _Doc()
    project = _ident("PBXProject", name)
    main, products = _ident("PBXGroup", "main"), _ident("PBXGroup", "products")
    app = _ident("PBXNativeTarget", name)
    app_product = _ident("PBXFileReference", "product:" + name)
    order = {products: "~"}      # group children sort by path; Products stays last

    doc.add(main, "", {"isa": "PBXGroup", "children": [_Ref(products)], "sourceTree": _Raw("<group>")})
    doc.add(products, "Products", {"isa": "PBXGroup", "children": [], "name": _Raw("Products"),
                                   "sourceTree": _Raw("<group>")})
    doc.add(app_product, name + ".app", {
        "isa": "PBXFileReference", "explicitFileType": _Raw("wrapper.cfbundle"), "includeInIndex": 0,
        "path": _Raw(name + ".app"), "sourceTree": _Raw("BUILT_PRODUCTS_DIR")})
    doc.objects[products]["children"].append(_Ref(app_product))

    test_targets = []      # (key, part, product_type) 按清单顺序：单测在前、UI 测试在后
    for key, product_type in TEST_TARGET_KINDS:
        part = spec.get(key)
        if not part:
            continue
        product = _ident("PBXFileReference", "product:" + part["name"])
        doc.add(product, part["name"] + ".xctest", {
            "isa": "PBXFileReference", "explicitFileType": _Raw("wrapper.cfbundle"), "includeInIndex": 0,
            "path": _Raw(part["name"] + ".xctest"), "sourceTree": _Raw("BUILT_PRODUCTS_DIR")})
        doc.objects[products]["children"].append(_Ref(product))
        test_targets.append((key, part, product_type, _ident("PBXNativeTarget", part["name"]), product))

    groups = {}

    def place(path, ref):
        """Mirror the directory chain between the project root and the file as nested groups."""
        chain, folder = [], path.parent
        while folder != root:
            chain.append(folder)
            folder = folder.parent
        parent = main
        for current in reversed(chain):
            key = current.relative_to(root).as_posix()
            if key not in groups:
                ident = _ident("PBXGroup", "group:" + key)
                doc.add(ident, current.name, {"isa": "PBXGroup", "children": [], "path": _Raw(current.name),
                                              "sourceTree": _Raw("<group>")})
                groups[key] = ident
                order[ident] = key
                doc.objects[parent]["children"].append(_Ref(ident))
            parent = groups[key]
        order[ref] = path.relative_to(root).as_posix()
        doc.objects[parent]["children"].append(_Ref(ref))

    def build_file(path, ref):
        ident = _ident("PBXBuildFile", "build:" + path.relative_to(root).as_posix())
        doc.add(ident, path.name + " in Sources", {"isa": "PBXBuildFile", "fileRef": _Ref(ref)})
        return _Ref(ident)

    def resource_file(path, ref):
        ident = _ident("PBXBuildFile", "resource:" + path.relative_to(root).as_posix())
        doc.add(ident, path.name + " in Resources", {"isa": "PBXBuildFile", "fileRef": _Ref(ref)})
        return _Ref(ident)

    def add_source(path):
        rel = path.relative_to(root).as_posix()
        ref = _ident("PBXFileReference", "file:" + rel)
        doc.add(ref, path.name, {"isa": "PBXFileReference", "lastKnownFileType": SOURCE_TYPES[path.suffix],
                                 "path": _Raw(path.name), "sourceTree": _Raw("<group>")})
        place(path, ref)
        return ref

    def add_resource(path):
        rel = path.relative_to(root).as_posix()
        ref = _ident("PBXFileReference", "file:" + rel)
        doc.add(ref, path.name, {"isa": "PBXFileReference", "lastKnownFileType": RESOURCE_TYPES[path.suffix],
                                 "path": _Raw(path.name), "sourceTree": _Raw("<group>")})
        place(path, ref)
        return ref

    app_sources = [build_file(path, add_source(path)) for path in spec["sources"]]
    app_resources = [resource_file(path, add_resource(path)) for path in spec["resources"]]

    def phase(kind, target, tag, files):
        ident = _ident(kind, "%s:%s" % (target, tag))
        doc.add(ident, "%s for %s" % (tag, target), {
            "isa": kind, "buildActionMask": 2147483647, "files": files, "runOnlyForDeploymentPostprocessing": 0})
        return _Ref(ident)

    def configurations(scope, base, extra):
        refs = []
        for mode in CONFIGS:
            ident = _ident("XCBuildConfiguration", "%s:%s" % (scope, mode))
            doc.add(ident, mode, {"isa": "XCBuildConfiguration", "buildSettings": dict(base, **extra[mode]),
                                  "name": _Raw(mode)})
            refs.append(_Ref(ident))
        return refs

    def configuration_list(scope, kind, display, refs):
        ident = _ident("XCConfigurationList", scope)
        doc.add(ident, 'Build configuration list for %s "%s"' % (kind, display), {
            "isa": "XCConfigurationList", "buildConfigurations": refs, "defaultConfigurationIsVisible": 0,
            "defaultConfigurationName": _Raw("Release")})
        return _Ref(ident)

    device = {"CODE_SIGN_STYLE": _Raw("Automatic"), "CURRENT_PROJECT_VERSION": 1,
              "GENERATE_INFOPLIST_FILE": _Raw("YES"), "IPHONEOS_DEPLOYMENT_TARGET": _Raw(spec["target"]),
              "MARKETING_VERSION": _Raw("1.0"), "PRODUCT_NAME": _Raw("$(TARGET_NAME)"),
              "SWIFT_EMIT_LOC_STRINGS": _Raw("YES"), "SWIFT_VERSION": _Raw(spec["swift"]),
              "TARGETED_DEVICE_FAMILY": _Quoted(spec["family"])}
    info = {"UILaunchScreen_Generation": _Raw("YES")}
    info.update({key: _setting(value) for key, value in spec["info"].items()})
    info = {"INFOPLIST_KEY_" + key: value for key, value in info.items()}
    app_settings = dict(device, PRODUCT_BUNDLE_IDENTIFIER=_Raw(spec["bundle_id"]), **info)

    app_list = configuration_list(name, "PBXNativeTarget", name, configurations(
        name, app_settings, spec["settings"]))
    app_phases = [phase("PBXSourcesBuildPhase", name, "Sources", app_sources),
                  phase("PBXFrameworksBuildPhase", name, "Frameworks", []),
                  phase("PBXResourcesBuildPhase", name, "Resources", app_resources)]
    doc.add(app, name, {
        "isa": "PBXNativeTarget", "buildConfigurationList": app_list, "buildPhases": app_phases,
        "buildRules": [], "dependencies": [], "name": _Raw(name), "productName": _Raw(name),
        "productReference": _Ref(app_product), "productType": _Raw("com.apple.product-type.application")})

    targets, attributes = [_Ref(app)], {app: {"CreatedOnToolsVersion": _Raw("15.0")}}
    for key, part, product_type, tident, product in test_targets:
        tname = part["name"]
        settings = dict(device, PRODUCT_BUNDLE_IDENTIFIER=_Raw(part["bundle_id"]),
                        **_test_extra_settings(key, name))
        proxy = _ident("PBXContainerItemProxy", "%s->%s" % (tname, name))
        doc.add(proxy, "PBXContainerItemProxy", {
            "isa": "PBXContainerItemProxy", "containerPortal": _Ref(project), "proxyType": 1,
            "remoteGlobalIDString": app, "remoteInfo": _Raw(name)})
        dependency = _ident("PBXTargetDependency", "%s->%s" % (tname, name))
        doc.add(dependency, "PBXTargetDependency", {
            "isa": "PBXTargetDependency", "target": _Ref(app), "targetProxy": _Ref(proxy)})
        sources = [build_file(path, add_source(path)) for path in part["sources"]]
        test_list = configuration_list(tname, "PBXNativeTarget", tname, configurations(
            tname, settings, part["settings"]))
        test_phases = [phase("PBXSourcesBuildPhase", tname, "Sources", sources),
                       phase("PBXFrameworksBuildPhase", tname, "Frameworks", []),
                       phase("PBXResourcesBuildPhase", tname, "Resources", [])]
        doc.add(tident, tname, {
            "isa": "PBXNativeTarget", "buildConfigurationList": test_list, "buildPhases": test_phases,
            "buildRules": [], "dependencies": [_Ref(dependency)], "name": _Raw(tname),
            "productName": _Raw(tname), "productReference": _Ref(product),
            "productType": _Raw(product_type)})
        targets.append(_Ref(tident))
        attributes[tident] = {"CreatedOnToolsVersion": _Raw("15.0"), "TestTargetID": _Ref(app)}

    for fields in doc.objects.values():
        if fields["isa"] == "PBXGroup":
            fields["children"].sort(key=lambda ref: order.get(ref.ident, ""))
    common = {key: _Raw(value) for key, value in COMMON.items()}
    common["IPHONEOS_DEPLOYMENT_TARGET"] = _Raw(spec["target"])
    common["SWIFT_VERSION"] = _Raw(spec["swift"])
    modes = {mode: {key: _Raw(value) for key, value in values.items()} for mode, values in MODES.items()}
    doc.add(project, "Project object", {
        "isa": "PBXProject",
        "attributes": {"BuildIndependentTargetsInParallel": 1, "LastUpgradeCheck": 1500,
                       "TargetAttributes": attributes},
        "buildConfigurationList": configuration_list(
            "project:" + name, "PBXProject", name, configurations("project", common, modes)),
        "compatibilityVersion": _Raw("Xcode 14.0"), "developmentRegion": _Raw("en"),
        "hasScannedForEncodings": 0, "knownRegions": [_Raw("en"), _Raw("Base")], "mainGroup": _Ref(main),
        "productRefGroup": _Ref(products), "projectDirPath": _Raw(""), "projectRoot": _Raw(""),
        "targets": targets})
    return doc, project, app, [entry[3] for entry in test_targets]


def scheme(spec, doc, app, test_targets):
    def reference(target, product):
        return ('            <BuildableReference\n               BuildableIdentifier = "primary"\n'
                '               BlueprintIdentifier = "%s"\n               BuildableName = "%s"\n'
                '               BlueprintName = "%s"\n'
                '               ReferencedContainer = "container:%s.xcodeproj">\n'
                '            </BuildableReference>\n'
                % (target, product, doc.comments[target], spec["name"]))

    def entry(running, body):
        return ('         <BuildActionEntry\n            buildForTesting = "YES"\n'
                '            buildForRunning = "%s"\n            buildForProfiling = "YES"\n'
                '            buildForArchiving = "YES"\n            buildForAnalyzing = "YES">\n'
                '%s         </BuildActionEntry>\n' % (running, body))

    app_ref = reference(app, spec["name"] + ".app")
    entries = entry("YES", app_ref)
    test_refs = [reference(target, doc.comments[target] + ".xctest") for target in test_targets]
    entries += "".join(entry("NO", test_ref) for test_ref in test_refs)
    test_action = ""
    if test_refs:
        testables = "".join('         <TestableReference skipped = "NO">\n%s         </TestableReference>\n'
                            % test_ref for test_ref in test_refs)
        test_action = ('   <TestAction\n      buildConfiguration = "Debug"\n'
                       '      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"\n'
                       '      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"\n'
                       '      shouldUseLaunchSchemeArgsEnv = "YES">\n      <Testables>\n%s'
                       '      </Testables>\n   </TestAction>\n' % testables)
    debugger = ('      selectedDebuggerIdentifier = "Xcode.DebuggerFoundation.Debugger.LLDB"\n'
                '      selectedLauncherIdentifier = "Xcode.DebuggerFoundation.Launcher.LLDB"\n')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<Scheme\n   LastUpgradeVersion = "1500"\n   version = "1.7">\n'
        '   <BuildAction\n      parallelizeBuildables = "YES"\n      buildImplicitDependencies = "YES">\n'
        '      <BuildActionEntries>\n%s      </BuildActionEntries>\n   </BuildAction>\n%s'
        '   <LaunchAction\n      buildConfiguration = "Debug"\n%s      launchStyle = "0"\n'
        '      useCustomWorkingDirectory = "NO"\n      ignoresPersistentStateOnLaunch = "NO"\n'
        '      debugDocumentVersioning = "YES"\n      debugServiceExtension = "internal"\n'
        '      allowLocationSimulation = "YES">\n      <BuildableProductRunnable runnableDebuggingMode = "0">\n'
        '%s      </BuildableProductRunnable>\n   </LaunchAction>\n'
        '   <ProfileAction\n      buildConfiguration = "Release"\n      shouldUseLaunchSchemeArgsEnv = "YES"\n'
        '      savedToolIdentifier = ""\n      useCustomWorkingDirectory = "NO"\n'
        '      debugDocumentVersioning = "YES">\n      <BuildableProductRunnable runnableDebuggingMode = "0">\n'
        '%s      </BuildableProductRunnable>\n   </ProfileAction>\n'
        '   <AnalyzeAction\n      buildConfiguration = "Debug">\n   </AnalyzeAction>\n'
        '   <ArchiveAction\n      buildConfiguration = "Release"\n      revealArchiveInOrganizer = "YES">\n'
        '   </ArchiveAction>\n</Scheme>\n' % (entries, test_action, debugger, app_ref, app_ref))


def generate(manifest, root):
    root = Path(root)
    manifest = Path(manifest)
    if not manifest.is_absolute():
        manifest = root / manifest          # 清单路径相对工位，不依赖进程的当前目录
    spec = load_spec(manifest, root)
    doc, project, app, test_targets = build(spec, root)
    folder = root / (spec["name"] + ".xcodeproj")
    schemes = folder / "xcshareddata" / "xcschemes"
    schemes.mkdir(parents=True, exist_ok=True)
    (folder / "project.pbxproj").write_text(doc.render(project), encoding="utf-8")
    (schemes / (spec["name"] + ".xcscheme")).write_text(
        scheme(spec, doc, app, test_targets), encoding="utf-8")
    return {"project": folder.name, "scheme": spec["name"], "app_sources": len(spec["sources"]),
            "test_sources": len(spec["tests"]["sources"]) if spec["tests"] else 0,
            "ui_test_sources": len(spec["ui_tests"]["sources"]) if spec["ui_tests"] else 0,
            "resources": len(spec["resources"]),
            "next": "xcodebuild -project %s -scheme %s -destination \"generic/platform=iOS Simulator\" "
                    "-derivedDataPath .proto-build-01 CODE_SIGNING_ALLOWED=NO build"
                    % (folder.name, spec["name"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate a minimal iOS Xcode project from a JSON manifest.")
    parser.add_argument("manifest", help="project manifest (JSON) describing targets, sources and tests")
    args = parser.parse_args(argv)
    try:
        summary = generate(args.manifest, Path.cwd())
    except SpecError as exc:
        sys.stderr.write("make_xcodeproj: %s\n" % exc)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

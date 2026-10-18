#!/usr/bin/env python3
"""Idempotently inject a UI-testing-bundle target into an xcodegen-generated
.xcodeproj, plus the scheme's Testables. xcodegen 2.46 cannot emit ui-testing
targets, so this runs after `xcodegen generate`.

Usage:
    python3 patch_uitests.py <AppProjectName> <HostTargetName> <TestTargetName> \
        <e2e_relpath1> [e2e_relpathN]
    e.g.
    python3 patch_uitests.py NervePhone NervePhone NervePhoneUITests \
        ../../e2e/XCUITestSupport ../../e2e/NervePhoneUITests

The e2e_relpath* args are project-relative source dirs (project dir is
apple/Distribution/Generated/), one per source folder to compile into the test
target. Each is added as a PBXGroup mirroring its basename, and every .swift
file inside is compiled. The committed project already carries the patch;
re-running is a no-op.
"""
import hashlib
import re
import sys


def hid(*salt):
    """Deterministic 24-hex object id from a stable salt string."""
    h = hashlib.sha1(("|".join(salt)).encode()).hexdigest()[:24].upper()
    return h


def main():
    project_name = sys.argv[1]       # e.g. NervePhone  (also the .xcodeproj name)
    host_target = sys.argv[2]        # e.g. NervePhone (the app under test)
    test_target = sys.argv[3]        # e.g. NervePhoneUITests
    src_dirs = sys.argv[4:]          # project-relative source dirs
    proj = f"{project_name}.xcodeproj/project.pbxproj"

    with open(proj) as f:
        s = f.read()

    # Idempotency: bail if the test target is already wired.
    if f"{test_target} /* {test_target} */" in s and "UI testing" in s:
        print(f"patch_uitests: {test_target} already present in {proj}; no-op.")
        return

    # ---- Discover existing object ids ----
    m = re.search(r"\t\t([0-9A-F]{24}) /\* Project object \*/ = \{", s)
    project_obj = m.group(1)
    # Host app target id: the PBXNativeTarget named host_target
    m = re.search(rf"\t\t([0-9A-F]{{24}}) /\* {host_target} \*/ = \{{\n\t\t\tisa = PBXNativeTarget;.*?productReference = ([0-9A-F]{{24}}) /\* ([^*]+)\*/;", s, re.S)
    if not m:
        raise SystemExit(f"patch_uitests: could not locate host target {host_target}")
    host_tid, host_prod_ref, host_prod_name = m.group(1), m.group(2), m.group(3).strip()
    # Products group id
    m = re.search(r"(\t\t[0-9A-F]{24}) /\* Products \*/ = \{", s)
    products_grp = m.group(1).strip().split()[0]
    if not products_grp:
        # fall back: the group whose children include host_prod_ref
        m = re.search(r"\t\t([0-9A-F]{24}) /\* [^*]* \*/ = \{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = \((?:[^)]*" + re.escape(host_prod_ref) + r"[^)]*)\);\n\t\t\tname = Products;", s, re.S)
        products_grp = m.group(1)
    # Host app bundle id for the test bundle id
    m = re.search(rf"PRODUCT_BUNDLE_IDENTIFIER = ([^;]+);", s)
    host_bundle = m.group(1).strip() if m else host_target

    # ---- Stable ids for the new objects ----
    P = test_target
    prod_ref = hid(P, "prodref")          # NervePhoneUITests.xctest
    tgt_id = hid(P, "target")
    cfg_list = hid(P, "cfglist")
    cfg_dbg = hid(P, "cfgdebug")
    cfg_rel = hid(P, "cfgrelease")
    src_phase = hid(P, "srcphase")
    dep_id = hid(P, "dep")
    proxy_id = hid(P, "proxy")
    grp_e2e = hid(P, "grp_e2e")
    test_bundle_id = f"{host_bundle}.UITests"

    # ---- Build file + fileRef per source dir ----
    build_file_lines = []      # entries in Sources phase
    file_ref_lines = []        # PBXFileReference lines
    group_child_lines = []     # children of the e2e group
    for rel in src_dirs:
        dname = rel.rstrip("/").split("/")[-1]
        grp_sub = hid(P, "grp", dname)
        group_child_lines.append(f"\t\t\t\t{grp_sub} /* {dname} */;")
        # file refs: we do not parse the FS here; the xcodegen app already
        # enumerates its sources. For the test target we reference the known
        # harness files by name (stable, small set).
    # Enumerate actual .swift files in each dir (relative to the project dir).
    import os
    proj_dir = os.path.dirname(proj)
    for rel in src_dirs:
        full = os.path.normpath(os.path.join(proj_dir, rel))
        if not os.path.isdir(full):
            print(f"patch_uitests: WARN source dir not found: {full}")
            continue
        dname = rel.rstrip("/").split("/")[-1]
        grp_sub = hid(P, "grp", dname)
        child_fr = []
        for root, _, files in os.walk(full):
            for fn in sorted(files):
                if not fn.endswith(".swift"):
                    continue
                fr = hid(P, "fr", os.path.relpath(os.path.join(root, fn), proj_dir))
                bf = hid(P, "bf", os.path.relpath(os.path.join(root, fn), proj_dir))
                child_fr.append(f"\t\t\t\t{fr} /* {fn} */;")
                file_ref_lines.append(f"\t\t{fr} /* {fn} */ = {{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = {fn}; sourceTree = \"<group>\"; }};")
                build_file_lines.append(f"\t\t{bf} /* {fn} in Sources */ = {{isa = PBXBuildFile; fileRef = {fr} /* {fn} */; }};")
        # group children per subdir file refs
        if child_fr:
            file_ref_lines.append(f"\t\t{grp_sub} /* {dname} */ = {{isa = PBXGroup; children = (")
            file_ref_lines.append("\n".join(child_fr))
            file_ref_lines.append(f"\t\t\t); name = {dname}; sourceTree = \"<group>\"; }};")
        else:
            file_ref_lines.append(f"\t\t{grp_sub} /* {dname} */ = {{isa = PBXGroup; children = (); name = {dname}; sourceTree = \"<group>\"; }};")

    # ---- Assemble insertion blocks ----
    # 1) PBXBuildFile (one line) — insert before End PBXBuildFile section
    bf_block = "\n".join(build_file_lines)

    # 2) PBXFileReference (product + source files)
    fr_product = f"\t\t{prod_ref} /* {test_target}.xctest */ = {{isa = PBXFileReference; explicitFileType = wrapper.cfbundle; includeInIndex = 0; path = {test_target}.xctest; sourceTree = BUILT_PRODUCTS_DIR; }};"
    fr_block = fr_product + "\n" + "\n".join(file_ref_lines)

    # e2e umbrella group
    e2e_grp_block = (
        f"\t\t{grp_e2e} /* e2e */ = {{\n"
        f"\t\t\tisa = PBXGroup;\n"
        f"\t\t\tchildren = (\n"
        + "\n".join(group_child_lines) + "\n"
        f"\t\t\t);\n"
        f"\t\t\tname = e2e;\n"
        f"\t\t\tpath = ../../e2e;\n"
        f"\t\t\tsourceTree = \"<group>\";\n"
        f"\t\t}};"
    )

    # 3) PBXSourcesBuildPhase
    src_phase_block = (
        f"\t\t{src_phase} /* Sources */ = {{\n"
        f"\t\t\tisa = PBXSourcesBuildPhase;\n"
        f"\t\t\tbuildActionMask = 2147483647;\n"
        f"\t\t\tfiles = (\n"
        + "\n".join(f"\t\t\t\t{b.split(' /* ')[0]} /* {b.split(' /* ')[1]} */;" for b in build_file_lines) + "\n"
        f"\t\t\t);\n"
        f"\t\t\trunOnlyForDeploymentPostprocessing = 0;\n"
        f"\t\t}};"
    )

    # 4) PBXContainerItemProxy
    proxy_block = (
        f"\t\t{proxy_id} /* PBXContainerItemProxy */ = {{\n"
        f"\t\t\tisa = PBXContainerItemProxy;\n"
        f"\t\t\tcontainerPortal = {project_obj} /* Project object */;\n"
        f"\t\t\tproxyType = 1;\n"
        f"\t\t\tremoteGlobalIDString = {host_tid};\n"
        f"\t\t\tremoteInfo = {host_target};\n"
        f"\t\t}};"
    )

    # 5) PBXTargetDependency
    dep_block = (
        f"\t\t{dep_id} /* PBXTargetDependency */ = {{\n"
        f"\t\t\tisa = PBXTargetDependency;\n"
        f"\t\t\ttarget = {host_tid} /* {host_target} */;\n"
        f"\t\t\ttargetProxy = {proxy_id} /* PBXContainerItemProxy */;\n"
        f"\t\t}};"
    )

    # 6) PBXNativeTarget (the ui-testing-bundle)
    native_block = (
        f"\t\t{tgt_id} /* {test_target} */ = {{\n"
        f"\t\t\tisa = PBXNativeTarget;\n"
        f"\t\t\tbuildConfigurationList = {cfg_list} /* Build configuration list for PBXNativeTarget \"{test_target}\" */;\n"
        f"\t\t\tbuildPhases = (\n"
        f"\t\t\t\t{src_phase} /* Sources */,\n"
        f"\t\t\t);\n"
        f"\t\t\tbuildRules = (\n"
        f"\t\t\t);\n"
        f"\t\t\tdependencies = (\n"
        f"\t\t\t\t{dep_id} /* PBXTargetDependency */,\n"
        f"\t\t\t);\n"
        f"\t\t\tname = {test_target};\n"
        f"\t\t\tproductName = {test_target};\n"
        f"\t\t\tproductReference = {prod_ref} /* {test_target}.xctest */;\n"
        f"\t\t\tproductType = \"com.apple.product-type.bundle.ui-testing\";\n"
        f"\t\t}};"
    )

    # 7) XCBuildConfiguration Debug + Release
    def cfg_block(cid, name):
        return (
            f"\t\t{cid} /* {name} */ = {{\n"
            f"\t\t\tisa = XCBuildConfiguration;\n"
            f"\t\t\tbuildSettings = {{\n"
            f"\t\t\t\tCODE_SIGN_IDENTITY = \"\";\n"
            f"\t\t\t\tCODE_SIGN_STYLE = Automatic;\n"
            f"\t\t\t\tCURRENT_PROJECT_VERSION = 1;\n"
            f"\t\t\t\tGENERATE_INFOPLIST_FILE = YES;\n"
            f"\t\t\t\tIPHONEOS_DEPLOYMENT_TARGET = 17.0;\n"
            f"\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = {test_bundle_id};\n"
            f"\t\t\t\tPRODUCT_NAME = \"$(TARGET_NAME)\";\n"
            f"\t\t\t\tSDKROOT = iphoneos;\n"
            f"\t\t\t\tTARGETED_DEVICE_FAMILY = \"1,2\";\n"
            f"\t\t\t\tTESTHOST = \"$(BUILT_PRODUCTS_DIR)/{host_prod_name}/{host_target}\";\n"
            f"\t\t\t}};\n"
            f"\t\t\tname = {name};\n"
            f"\t\t}};"
        )

    cfg_block_dbg = cfg_block(cfg_dbg, "Debug")
    cfg_block_rel = cfg_block(cfg_rel, "Release")

    # 8) XCConfigurationList
    cfglist_block = (
        f"\t\t{cfg_list} /* Build configuration list for PBXNativeTarget \"{test_target}\" */ = {{\n"
        f"\t\t\tisa = XCConfigurationList;\n"
        f"\t\t\tbuildConfigurations = (\n"
        f"\t\t\t\t{cfg_dbg} /* Debug */,\n"
        f"\t\t\t\t{cfg_rel} /* Release */,\n"
        f"\t\t\t);\n"
        f"\t\t\tdefaultConfigurationIsVisible = 0;\n"
        f"\t\t\tdefaultConfigurationName = Debug;\n"
        f"\t\t}};"
    )

    # ---- Inject into sections (idempotent by anchor) ----
    def inject_before_end(section_name, block, marker_comment=None):
        nonlocal s
        end_marker = f"/* End {section_name} section */"
        idx = s.find(end_marker)
        if idx < 0:
            raise SystemExit(f"patch_uitests: no {section_name} section")
        s = s[:idx] + block + "\n" + s[idx:]

    # PBXBuildFile
    if bf_block:
        inject_before_end("PBXBuildFile", bf_block)
    # PBXContainerItemProxy
    inject_before_end("PBXContainerItemProxy", proxy_block)
    # PBXFileReference
    inject_before_end("PBXFileReference", fr_block)
    # PBXGroup: e2e group
    inject_before_end("PBXGroup", e2e_grp_block)
    # PBXNativeTarget
    inject_before_end("PBXNativeTarget", native_block)
    # PBXSourcesBuildPhase
    inject_before_end("PBXSourcesBuildPhase", src_phase_block)
    # PBXTargetDependency
    inject_before_end("PBXTargetDependency", dep_block)
    # XCBuildConfiguration (Debug + Release)
    inject_before_end("XCBuildConfiguration", cfg_block_dbg + "\n" + cfg_block_rel)
    # XCConfigurationList
    inject_before_end("XCConfigurationList", cfglist_block)

    # ---- Products group: add product ref to children ----
    m = re.search(r"(\t\t[0-9A-F]{24} /\* [^*]* \*/ = \{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = \()((?:\t\t\t\t[0-9A-F]{24} /\* [^*]*\*/,\n?)*)(\)\;\n\t\t\tname = Products;)", s, re.S)
    if m:
        s = s[: m.start()] + m.group(1) + "\n".join(m.group(2).splitlines()) + f"\n\t\t\t\t{prod_ref} /* {test_target}.xctest */," + m.group(3) + s[m.end():]
    else:
        raise SystemExit("patch_uitests: Products group children not found")

    # ---- Project targets list: add test target ----
    m = re.search(r"(\t\t\ttargets = \()((?:\t\t\t\t[0-9A-F]{24} /\* [^*]*\*/,\n?)*)(\)\;)", s, re.S)
    if m:
        s = s[: m.start()] + m.group(1) + "\n".join(m.group(2).splitlines()) + f"\n\t\t\t\t{tgt_id} /* {test_target} */," + m.group(3) + s[m.end():]
    else:
        raise SystemExit("patch_uitests: project targets list not found")

    with open(proj, "w") as f:
        f.write(s)
    print(f"patch_uitests: injected {test_target} into {proj}")
    print(f"  host={host_target} ({host_tid})  test_bundle_id={test_bundle_id}")
    print(f"  src_dirs={src_dirs}  build_files={len(build_file_lines)}")

    # ---- Scheme Testables ----
    scheme = f"{project_name}.xcodeproj/xcshareddata/xcschemes/{project_name}.xcscheme"
    if os.path.exists(scheme):
        with open(scheme) as f:
            sc = f.read()
        # host app buildable ref block from scheme
        hm = re.search(r"(\t\t\t<BuildableReference\s+BuildableIdentifier = \"primary\"\s+BlueprintIdentifier = \"[^\"]*\"\s+BuildableName = \"[^\"]*\"\s+BlueprintName = \"[^\"]*\"\s+ReferencedContainer = \"[^\"]*\">\s*</BuildableReference>)", sc)
        if "<Testables>" not in sc:
            sc = sc.replace("<Testables>\n", "<Testables>\n")
        entry = (
            "         <TestableReference\n"
            "            skipped = \"NO\">\n"
            f"            <BuildableReference\n"
            f"               BuildableIdentifier = \"primary\"\n"
            f"               BlueprintIdentifier = \"{tgt_id}\"\n"
            f"               BuildableName = \"{test_target}.xctest\"\n"
            f"               BlueprintName = \"{test_target}\"\n"
            f"               ReferencedContainer = \"container:{project_name}.xcodeproj\">\n"
            f"            </BuildableReference>\n"
            "         </TestableReference>\n"
        )
        if f'BlueprintIdentifier = "{tgt_id}"' not in sc:
            sc = sc.replace("<Testables>\n", "<Testables>\n" + entry, 1)
        with open(scheme, "w") as f:
            f.write(sc)
        print(f"  scheme test target added: {scheme}")
    else:
        print(f"patch_uitests: WARN scheme not found: {scheme}")


if __name__ == "__main__":
    main()

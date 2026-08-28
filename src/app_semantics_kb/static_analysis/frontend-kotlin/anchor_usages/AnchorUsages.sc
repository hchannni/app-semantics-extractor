import io.shiftleft.codepropertygraph.Cpg

/**
 * Entry point script.
 *
 * NOTE: This script is intentionally slim and depends on modules imported via `joern --import`.
 * Run example:
 *   joern \
 *     --import /path/to/joern/view_anchors/ViewAnchorContract.sc \
 *     --import /path/to/joern/anchor_usages/io/AnchorUsagesModel.sc \
 *     --import /path/to/joern/anchor_usages/io/AnchorUsagesInput.sc \
 *     --import /path/to/joern/anchor_usages/io/AnchorUsagesJson.sc \
 *     --import /path/to/joern/anchor_usages/analysis/AnchorUsagesUiSignals.sc \
 *     --import /path/to/joern/anchor_usages/analysis/AnchorUsagesKotlinScope.sc \
 *     --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesCaseSupport.sc \
 *     --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesClosureCaptureCase.sc \
 *     --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesChainingCase.sc \
 *     --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesPassThroughCase.sc \
 *     --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesFieldInstanceCase.sc \
 *     --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesReceiverCase.sc \
 *     --import /path/to/joern/anchor_usages/analysis/AnchorUsagesSemanticUsageDetector.sc \
 *     --import /path/to/joern/anchor_usages/analysis/AnchorUsagesTargetResolver.sc \
 *     --import /path/to/joern/anchor_usages/analysis/AnchorUsagesPostProcessor.sc \
 *     --import /path/to/joern/anchor_usages/analysis/AnchorUsagesAnalysis.sc \
 *     --script /path/to/joern/anchor_usages/AnchorUsages.sc \
 *     --param cpgPath=... --param viewAnchorsPath=... --param assignmentDeclRefsPath=... --param outputPath=...
 */

def loadCpg(path: String): Cpg =
  io.joern.joerncli.console.Joern
    .importCpg(path)
    .getOrElse(throw new RuntimeException(s"Failed to load CPG: $path"))

@main def anchorUsages(
  cpgPath: String,
  viewAnchorsPath: String,
  assignmentDeclRefsPath: String,
  outputPath: String = "anchor-usages.json"
):Unit = {
  import AnchorUsagesModel.*
  implicit val cpg: Cpg = loadCpg(cpgPath)

  val anchors = AnchorUsagesInput.parseAnchors(viewAnchorsPath)
  val declarations = AnchorUsagesInput.parseDeclarations(assignmentDeclRefsPath)

  println(s"[*] Loaded ${anchors.size} anchors, ${declarations.size} assignment entries")

  val reports: List[UsageReport] = anchors.map { anchor =>
    val decl = declarations.get(anchor.cpgNodeId)
    AnchorUsagesAnalysis.usagesForAnchor(anchor, decl)
  }
  val dedupedReports = AnchorUsagesPostProcessor.dedupeAcrossAnchors(reports)

  val json = ujson.Arr(dedupedReports.map(AnchorUsagesJson.toJson): _*)
  AnchorUsagesJson.writeJson(outputPath, ujson.write(json, indent = 2))
  println(s"[+] Saved anchor usages to $outputPath")
}

// 실행 예시 (분리된 모듈을 반드시 --import로 로드해야 함):
// /path/to/joern/joern-cli/joern \
//   --import /path/to/joern/view_anchors/ViewAnchorContract.sc \
//   --import /path/to/joern/anchor_usages/io/AnchorUsagesModel.sc \
//   --import /path/to/joern/anchor_usages/io/AnchorUsagesInput.sc \
//   --import /path/to/joern/anchor_usages/io/AnchorUsagesJson.sc \
//   --import /path/to/joern/anchor_usages/analysis/AnchorUsagesUiSignals.sc \
//   --import /path/to/joern/anchor_usages/analysis/AnchorUsagesKotlinScope.sc \
//   --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesCaseSupport.sc \
//   --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesClosureCaptureCase.sc \
//   --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesChainingCase.sc \
//   --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesPassThroughCase.sc \
//   --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesFieldInstanceCase.sc \
//   --import /path/to/joern/anchor_usages/analysis/cases/AnchorUsagesReceiverCase.sc \
//   --import /path/to/joern/anchor_usages/analysis/AnchorUsagesSemanticUsageDetector.sc \
//   --import /path/to/joern/anchor_usages/analysis/AnchorUsagesTargetResolver.sc \
//   --import /path/to/joern/anchor_usages/analysis/AnchorUsagesPostProcessor.sc \
//   --import /path/to/joern/anchor_usages/analysis/AnchorUsagesAnalysis.sc \
//   --script /path/to/joern/anchor_usages/AnchorUsages.sc \
//   --param cpgPath=/path/to/joern/joern-cli/alarmclock.cpg \
//   --param viewAnchorsPath=/path/to/joern/view-anchors.json \
//   --param assignmentDeclRefsPath=/path/to/joern/assignment-declarations.json \
//   --param outputPath=/path/to/joern/anchor-usages.json

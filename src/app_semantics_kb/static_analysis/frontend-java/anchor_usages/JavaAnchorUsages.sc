import io.shiftleft.codepropertygraph.Cpg

def loadJavaAnchorUsagesCpg(path: String): Cpg =
  io.joern.joerncli.console.Joern
    .importCpg(path)
    .getOrElse(throw new RuntimeException(s"Failed to load CPG: $path"))

@main def javaAnchorUsages(
  cpgPath: String,
  viewAnchorsPath: String,
  assignmentDeclRefsPath: String,
  outputPath: String = "anchor-usages.json",
  runsDir: String = ""
): Unit = {
  import JavaAnchorUsagesModel.*
  implicit val cpg: Cpg = loadJavaAnchorUsagesCpg(cpgPath)

  val anchors = JavaAnchorUsagesInput.parseAnchors(viewAnchorsPath)
  val declarations = JavaAnchorUsagesInput.parseDeclarations(assignmentDeclRefsPath)
  println(s"[*] Loaded ${anchors.size} Java anchors, ${declarations.size} Java assignment entries")

  val reports = anchors.map { anchor =>
    val decl = declarations.get(anchor.cpgNodeId)
    JavaAnchorUsagesAnalysis.usagesForAnchor(anchor, decl)
  }
  val dedupedReports = JavaAnchorUsagesPostProcessor.dedupeAcrossAnchors(reports)
  val json = ujson.Arr(dedupedReports.map(JavaAnchorUsagesJson.toJson): _*)
  JavaAnchorUsagesJson.writeJson(outputPath, runsDir, ujson.write(json, indent = 2))
  println(s"[+] Saved Java anchor usages to $outputPath")
}

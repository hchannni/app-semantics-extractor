import io.shiftleft.codepropertygraph.Cpg

def loadCpg(path: String): Cpg =
  io.joern.joerncli.console.Joern
    .importCpg(path)
    .getOrElse(throw new RuntimeException(s"Failed to load CPG: $path"))

@main def contextSlicer(
  cpgPath: String,
  anchorUsagesPath: String,
  outputDir: String = "context-slicer-output",
  sourcePath: String = ""
): Unit = {
  implicit val cpg: Cpg = loadCpg(cpgPath)

  val entries = ContextSlicerJson.parseAnchorUsages(anchorUsagesPath)
  println(s"[*] Loaded ${entries.size} anchor usage entries")

  val slices = entries
    .filter(_.enclosingMethodFullName.nonEmpty)
    .groupBy(e => TypeResolution.outerNonLambdaMethod(e.enclosingMethodFullName))
    .map { case (fn, group) => ContextMethodCollector.collect(fn, group, sourcePath) }
    .toList
    .sortBy(_.primaryMethod)

  // 전체 슬라이스에서 methodBodies / typeDefinitions 집계 (dedupe)
  val globalMethodBodies = slices.flatMap(_.methodBodies.toSeq).toMap
  val globalTypeIndex    = slices.flatMap(_.typeDefinitions.toSeq).toMap

  ContextSlicerJson.writeOutput(outputDir, slices, globalMethodBodies, globalTypeIndex)
  println(s"[+] Saved ${slices.size} method slices to $outputDir/")
  println(s"    slices.json:        ${slices.size} slices")
  println(s"    method-bodies.json: ${globalMethodBodies.size} methods")
  println(s"    type-index.json:    ${globalTypeIndex.size} types")
}

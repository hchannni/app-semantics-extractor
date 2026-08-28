import io.shiftleft.codepropertygraph.Cpg

object JavaViewAnchorsRunner {
  def loadCpg(path: String): Cpg =
    io.joern.joerncli.console.Joern
      .importCpg(path)
      .getOrElse(throw new RuntimeException(s"Failed to load CPG: $path"))

  def exec(
    inputPath: String,
    outputPath: String,
    runsDir: String = "",
    v2OutputPath: String = "",
    viewInstancesOutputPath: String = "",
    canonicalViewInstancesOutputPath: String = "",
    resourceInventoryPath: String = "",
    viewBindingFieldTypesPath: String = ""
  ): Unit = {
    implicit val cpg: Cpg = loadCpg(inputPath)
    println("[*] Collecting Java view anchors...")
    val resourceDecls = JavaResourceInventoryLoader.load(resourceInventoryPath)
    val viewBindingFieldTypes = JavaViewBindingFieldTypeLoader.load(viewBindingFieldTypesPath)
    println(s"[*] Loaded ${resourceDecls.size} Java resource declarations")
    println(s"[*] Loaded ${viewBindingFieldTypes.size} generated Java ViewBinding field types")

    val instances = JavaViewAnchorCollector.collect(resourceDecls, viewBindingFieldTypes)
    val canonicalInstances = JavaViewInstanceCanonicalizer.canonicalize(instances)
    val canonicalHandleInstances = JavaViewInstanceCanonicalizer.canonicalizeHandles(instances)

    if (viewInstancesOutputPath.trim.nonEmpty) {
      JavaOutputPathGuard.writeJson(
        viewInstancesOutputPath,
        runsDir,
        JavaViewAnchorJson.toV2JsonString(instances)
      )
      println(s"[+] Saved ${instances.size} Java view instances to $viewInstancesOutputPath")
    }

    if (canonicalViewInstancesOutputPath.trim.nonEmpty) {
      JavaOutputPathGuard.writeJson(
        canonicalViewInstancesOutputPath,
        runsDir,
        JavaViewAnchorJson.toV2JsonString(canonicalInstances)
      )
      println(s"[+] Saved ${canonicalInstances.size} Java canonical view instances to $canonicalViewInstancesOutputPath")
    }

    JavaOutputPathGuard.writeJson(outputPath, runsDir, JavaViewAnchorJson.toJsonString(canonicalHandleInstances))
    println(s"[+] Saved ${canonicalHandleInstances.size} Java compatibility view anchors to $outputPath")

    if (v2OutputPath.trim.nonEmpty) {
      JavaOutputPathGuard.writeJson(v2OutputPath, runsDir, JavaViewAnchorJson.toV2JsonString(canonicalInstances))
      println(s"[+] Saved ${canonicalInstances.size} Java V2 canonical view occurrences to $v2OutputPath")
    }
  }
}

@main def runJavaViewAnchors(
  inputPath: String,
  outputPath: String,
  runsDir: String = "",
  v2OutputPath: String = "",
  viewInstancesOutputPath: String = "",
  canonicalViewInstancesOutputPath: String = "",
  resourceInventoryPath: String = "",
  viewBindingFieldTypesPath: String = ""
): Unit = {
  JavaViewAnchorsRunner.exec(
    inputPath,
    outputPath,
    runsDir,
    v2OutputPath,
    viewInstancesOutputPath,
    canonicalViewInstancesOutputPath,
    resourceInventoryPath,
    viewBindingFieldTypesPath
  )
}

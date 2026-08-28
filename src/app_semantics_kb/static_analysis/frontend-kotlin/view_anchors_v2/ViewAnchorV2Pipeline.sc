import io.shiftleft.codepropertygraph.Cpg

object ViewAnchorV2Pipeline {
  private def loadCpg(path: String): Cpg =
    io.joern.joerncli.console.Joern
      .importCpg(path)
      .getOrElse(throw new RuntimeException(s"Failed to load CPG: $path"))

  def exec(
    inputPath: String,
    resourceInventoryPath: String,
    viewInstancesOutputPath: String,
    canonicalViewInstancesOutputPath: String,
    anchorsOutputPath: String,
    legacyOutputPath: String,
    viewBindingFieldTypesPath: String = ""
  ): Unit = {
    implicit val cpg: Cpg = loadCpg(inputPath)
    val resourceDecls = ResourceInventoryLoader.load(resourceInventoryPath)
    val viewBindingFieldTypes = ViewBindingFieldTypeLoader.load(viewBindingFieldTypesPath)

    println(s"[*] Loaded ${resourceDecls.size} resource declarations")
    println(s"[*] Loaded ${viewBindingFieldTypes.size} generated ViewBinding field types")
    val instances = ViewInstanceCollector.collect(resourceDecls, viewBindingFieldTypes)
    val canonicalInstances = ViewInstanceCanonicalizer.canonicalize(instances)
    val canonicalHandleInstances = ViewInstanceCanonicalizer.canonicalizeHandles(instances)

    ViewAnchorV2Json.writeJson(
      viewInstancesOutputPath,
      ujson.Arr(instances.map(ViewAnchorV2Json.viewInstanceToJson): _*)
    )
    ViewAnchorV2Json.writeJson(
      canonicalViewInstancesOutputPath,
      ujson.Arr(canonicalInstances.map(ViewAnchorV2Json.viewInstanceToJson): _*)
    )
    ViewAnchorV2Json.writeJson(
      anchorsOutputPath,
      ujson.Arr(canonicalInstances.map(ViewAnchorV2Json.viewInstanceToJson): _*)
    )
    ViewAnchorV2Json.writeJson(
      legacyOutputPath,
      ujson.Arr(canonicalHandleInstances.map(ViewAnchorV2Json.legacyAnchorToJson): _*)
    )

    println(s"[+] Saved ${instances.size} view instances to $viewInstancesOutputPath")
    println(s"[+] Saved ${canonicalInstances.size} canonical view instances to $canonicalViewInstancesOutputPath")
    println(s"[+] Saved ${canonicalInstances.size} v2 canonical view occurrences to $anchorsOutputPath")
    println(s"[+] Saved ${canonicalHandleInstances.size} compatibility handle anchors to $legacyOutputPath")
  }
}

import io.shiftleft.codepropertygraph.Cpg

import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Paths}

import ViewAnchorContract.ViewAnchor
import ViewAnchorContract.JsonId.writeId

object ViewAnchorsPipeline {
  private def dedupeAnchors(anchors: List[ViewAnchor]): List[ViewAnchor] =
    anchors
      .groupBy(anchor => (anchor.location, Option(anchor.code).getOrElse(""), anchor.resourceId))
      .values
      .flatMap { group =>
        group.sortBy(_.cpgNodeId).headOption
      }
      .toList
      .sortBy(anchor => (anchor.location, anchor.resourceId, anchor.cpgNodeId))

  def collectViewAnchors()(implicit cpg: Cpg): List[ViewAnchor] = {
    val resourceArgumentAnchors = ResourceLookupCollector.collect()
    val bindingFieldAnchors = BindingFieldCollector.collect()
    dedupeAnchors(resourceArgumentAnchors ++ bindingFieldAnchors)
  }

  def toJson(anchors: List[ViewAnchor]): String = {
    val arr = ujson.Arr(anchors.map { anchor =>
      ujson.Obj(
        "view_type" -> anchor.viewType,
        "resource_id" -> anchor.resourceId,
        "usage_type" -> anchor.usageType,
        "cpg_node_id" -> writeId(anchor.cpgNodeId),
        "cpg_node_type" -> anchor.cpgNodeType,
        "anchor_name" -> anchor.anchorName.map(ujson.Str).getOrElse(ujson.Null),
        "location" -> anchor.location,
        "code" -> anchor.code,
        "declaration_scope" -> anchor.declarationScope.map(ujson.Str).getOrElse(ujson.Null)
      )
    }: _*)
    ujson.write(arr, indent = 2)
  }

  def loadCpg(path: String): Cpg =
    io.joern.joerncli.console.Joern
      .importCpg(path)
      .getOrElse(throw new RuntimeException(s"Failed to load CPG: $path"))

  def exec(inputPath: String, outputPath: String = "view-anchors.json"): Unit = {
    implicit val cpg: Cpg = loadCpg(inputPath)

    println("[*] Collecting view anchors...")
    val anchors = collectViewAnchors()
    val json = toJson(anchors)

    val path = Paths.get(outputPath)
    Files.createDirectories(path.getParent match {
      case null => Paths.get(".")
      case parent => parent
    })
    Files.write(path, json.getBytes(StandardCharsets.UTF_8))

    println(s"[+] Saved ${anchors.size} view anchors to $outputPath")
  }
}

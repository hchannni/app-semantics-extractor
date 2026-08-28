import java.nio.file.{Files, Paths}
import java.nio.charset.StandardCharsets

// Handles output serialization only: UsageReport → JSON, file I/O.
// Input parsing (ViewAnchor, AnchorDeclaration) lives in AnchorUsagesInput.
object AnchorUsagesJson {
  import AnchorUsagesModel.*
  import ViewAnchorContract.JsonId.writeId
  import scala.collection.mutable

  def readJson(path: String): ujson.Value = {
    val source = scala.io.Source.fromFile(path)
    try ujson.read(source.mkString)
    finally source.close()
  }

  def writeJson(outputPath: String, data: String): Unit = {
    val path = Paths.get(outputPath)
    Option(path.getParent).foreach(parent => Files.createDirectories(parent))
    Files.write(path, data.getBytes(StandardCharsets.UTF_8))
  }

  // 동일 nodeId에서 가장 긴 code snippet을 선택하고, 패키지 경로 노이즈를 제거
  private def finalizeSlicedUsages(usages: List[AnchorUsage]): List[AnchorUsage] = {
    val (withNodeId, noNodeId) = usages.partition(_.usagePoint.nodeId > 0)
    val nodeDeduped = withNodeId
      .groupBy(_.usagePoint.nodeId)
      .values
      .flatMap { group =>
        group.sortBy(u => -Option(u.usagePoint.code).map(_.trim.length).getOrElse(0)).headOption
      }
      .toList
    (nodeDeduped ++ noNodeId).filterNot(u => isPackagePathOnly(u.usagePoint.code))
  }

  private def toAnchorUsage(anchor: ViewAnchor, usage: SemanticUsage): AnchorUsage = {
    val usageMethodName =
      Option(usage.usageMethodFullName).filter(_.nonEmpty).getOrElse(usage.methodFullName)
    AnchorUsage(
      anchorId = anchor.resourceId,
      usagePoint = UsagePoint(
        nodeId = usage.nodeId,
        nodeLabel = usage.nodeLabel,
        file = usage.sourceLocation.file,
        startLine = usage.sourceLocation.line,
        endLine = usage.sourceLocation.line,
        code = usage.code,
        usageKind = usage.usageKind.outputLabel
      ),
      enclosingMethodFullName = usageMethodName
    )
  }

  def toJson(report: UsageReport): ujson.Value = {
    val rawUsages = report.usages
      .map(usage => toAnchorUsage(report.anchor, usage))

    val anchorUsages = finalizeSlicedUsages(rawUsages)
      .sortBy(u => (u.usagePoint.file, u.usagePoint.startLine, u.usagePoint.nodeId))

    ujson.Obj(
      "anchor" -> ujson.Obj(
        "resource_id" -> report.anchor.resourceId,
        "anchor_name" -> report.anchor.anchorName.map(ujson.Str(_)).getOrElse(ujson.Null),
        "view_type" -> report.anchor.viewType,
        "usage_type" -> report.anchor.usageType,
        "cpg_node_id" -> writeId(report.anchor.cpgNodeId),
        "cpg_node_type" -> report.anchor.cpgNodeType,
        "declaration_scope" -> report.anchor.declarationScope.map(ujson.Str(_)).getOrElse(ujson.Null),
        "location" -> report.anchor.location,
        "code" -> report.anchor.code
      ),
      "usages" -> ujson.Arr(anchorUsages.map { au =>
        ujson.Obj(
          "anchor_id" -> au.anchorId,
          "usage_point" -> ujson.Obj(
            "node_id" -> writeId(au.usagePoint.nodeId),
            "node_label" -> au.usagePoint.nodeLabel,
            "file" -> au.usagePoint.file,
            "start_line" -> au.usagePoint.startLine,
            "end_line" -> au.usagePoint.endLine,
            "code" -> au.usagePoint.code,
            "usage_kind" -> au.usagePoint.usageKind
          ),
          "enclosing_method_full_name" -> au.enclosingMethodFullName
        )
      }: _*)
    )
  }
}

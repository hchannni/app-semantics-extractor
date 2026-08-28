object JavaAnchorUsagesJson {
  import JavaAnchorUsagesModel.*
  import JavaViewAnchorModel.JsonId.writeId

  def writeJson(outputPath: String, runsDir: String, data: String): Unit =
    JavaOutputPathGuard.writeJson(outputPath, runsDir, data)

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
    val anchorUsages = report.usages
      .map(usage => toAnchorUsage(report.anchor, usage))
      .filterNot(usage => isPackagePathOnly(usage.usagePoint.code))
      .sortBy(usage =>
        (usage.usagePoint.file, usage.usagePoint.startLine, usage.usagePoint.usageKind, usage.usagePoint.code)
      )

    ujson.Obj(
      "anchor" -> JavaViewAnchorJson.toJson(report.anchor),
      "usages" -> ujson.Arr(anchorUsages.map { usage =>
        ujson.Obj(
          "anchor_id" -> usage.anchorId,
          "usage_point" -> ujson.Obj(
            "node_id" -> writeId(usage.usagePoint.nodeId),
            "node_label" -> usage.usagePoint.nodeLabel,
            "file" -> usage.usagePoint.file,
            "start_line" -> usage.usagePoint.startLine,
            "end_line" -> usage.usagePoint.endLine,
            "code" -> usage.usagePoint.code,
            "usage_kind" -> usage.usagePoint.usageKind
          ),
          "enclosing_method_full_name" -> usage.enclosingMethodFullName
        )
      }: _*)
    )
  }
}

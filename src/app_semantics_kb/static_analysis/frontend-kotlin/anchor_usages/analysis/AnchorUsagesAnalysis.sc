import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*

object AnchorUsagesAnalysis {
  import AnchorUsagesModel.*

  def targetNodesFor(anchor: ViewAnchor, declarations: Option[AnchorDeclaration])(implicit cpg: Cpg): List[AstNode] =
    AnchorUsagesTargetResolver.targetNodesFor(
      anchor = anchor,
      declarations = declarations,
      loadNode = AnchorUsagesCaseSupport.loadNode,
      isScopeFunction = AnchorUsagesCaseSupport.isScopeFunction,
      callerCallSitesOf = AnchorUsagesCaseSupport.callerCallSitesOf,
      lazyPropertyFallbackOf = AnchorUsagesCaseSupport.lazyPropertyFallbackTargets
    )

  def analyzeTargets(anchor: ViewAnchor, targets: List[AstNode])(implicit cpg: Cpg): List[SemanticUsage] =
    targets.flatMap(AnchorUsagesSemanticUsageDetector.detectSemanticUsage(anchor, _))

  def usagesForAnchor(anchor: ViewAnchor, declarations: Option[AnchorDeclaration])(implicit cpg: Cpg): UsageReport = {
    val targets = targetNodesFor(anchor, declarations)
    val usages = analyzeTargets(anchor, targets)

    val dedupedUsages = AnchorUsagesPostProcessor.dedupeUsages(usages)
    val filteredUsages = AnchorUsagesPostProcessor.filterMeaningful(
      dedupedUsages,
      AnchorUsagesCaseSupport.isFalsePositive
    )

    val normalizedUsages = filteredUsages.map { usage =>
      val usageMethodFullName =
        Option(usage.usageMethodFullName).filter(_.nonEmpty).getOrElse(usage.methodFullName)
      usage.copy(usageMethodFullName = usageMethodFullName)
    }

    val outputUsages = AnchorUsagesPostProcessor.dedupeForOutput(anchor, normalizedUsages)

    UsageReport(anchor, outputUsages)
  }
}

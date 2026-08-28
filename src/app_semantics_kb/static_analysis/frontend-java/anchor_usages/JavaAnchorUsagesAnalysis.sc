import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*

object JavaAnchorUsagesAnalysis {
  import JavaAnchorUsagesModel.*

  def usagesForAnchor(anchor: ViewAnchor, declarations: Option[AnchorDeclaration])(implicit cpg: Cpg): UsageReport = {
    val targets = JavaAnchorUsagesTargetResolver.targetNodesFor(anchor, declarations)
    val usages = targets.flatMap(node => JavaAnchorUsagesSemanticUsageDetector.detectSemanticUsage(anchor, node))
    UsageReport(anchor, JavaAnchorUsagesPostProcessor.dedupe(usages))
  }
}

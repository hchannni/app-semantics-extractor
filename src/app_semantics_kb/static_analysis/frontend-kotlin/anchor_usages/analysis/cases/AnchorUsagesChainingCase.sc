import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*

object AnchorUsagesChainingCase {
  import AnchorUsagesModel.*

  def detect(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    if (anchor.usageType != "CHAINING") return Nil

    node match {
      case call: Call =>
        AnchorUsagesClosureCaptureCase.detectOnCall(anchor, node, call)
          .map(List(_))
          .getOrElse(AnchorUsagesCaseSupport.classifyCallAsUsages(anchor, call))
      case _ => Nil
    }
  }
}

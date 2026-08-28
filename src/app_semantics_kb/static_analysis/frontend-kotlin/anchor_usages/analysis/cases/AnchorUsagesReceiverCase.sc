import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

// Detects usages where the anchor node appears in the receiver position of a call site.
// Three strategies in priority order:
//   1. Kotlin scope function (run/apply/with): scan the receiver-lambda body for UI signals
//   2. Direct call where the anchor is the receiver: classify or detect closure
//   3. Full method fallback: scan all calls in the enclosing method for receiver-position usages
object AnchorUsagesReceiverCase {
  import AnchorUsagesModel.*

  private def scopeUsages(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    val usages = scala.collection.mutable.ListBuffer[SemanticUsage]()

    AnchorUsagesCaseSupport.ancestorsOfType[Call](node)
      .filter(AnchorUsagesCaseSupport.isScopeFunction)
      .toList
      .headOption
      .foreach { scopeCall =>
        val scoped = AnchorUsagesKotlinScope.scopeReceiverSignalUsages(
          anchor = anchor,
          anchorRef = node,
          scopeCall = scopeCall,
          buildUsage = (kind, call) => AnchorUsagesCaseSupport.buildUsage(anchor, kind, call)
        )
        usages ++= scoped
      }

    usages.toList
  }

  private def receiverCallUsages(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    val usages = scala.collection.mutable.ListBuffer[SemanticUsage]()

    AnchorUsagesCaseSupport.statementCallOf(node)
      .orElse(AnchorUsagesCaseSupport.contextCallOf(node))
      .foreach { call =>
        if (AnchorUsagesCaseSupport.isWithinReceiver(call, node)) {
          val lambdaUsage = AnchorUsagesClosureCaptureCase.detectOnCall(anchor, node, call)
          lambdaUsage match {
            case Some(usage) => usages += usage
            case None => usages ++= AnchorUsagesCaseSupport.classifyCallAsUsages(anchor, call)
          }
        }
      }

    usages.toList
  }

  private def receiverFallbackUsages(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    val usages = scala.collection.mutable.ListBuffer[SemanticUsage]()

    AnchorUsagesCaseSupport.firstAncestorOfType[Method](node).foreach { method =>
      Option(method.block).foreach { block =>
        block.ast.collectAll[Call].l
          .filterNot(AnchorUsagesCaseSupport.isOperatorCall)
          .filter(call => AnchorUsagesCaseSupport.isWithinReceiver(call, node))
          .foreach { call =>
            val lambdaUsage = AnchorUsagesClosureCaptureCase.detectOnCall(anchor, node, call)
            lambdaUsage match {
              case Some(usage) => usages += usage
              case None => usages ++= AnchorUsagesCaseSupport.classifyCallAsUsages(anchor, call)
            }
          }
      }
    }

    usages.toList
  }

  def detect(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    val scoped = scopeUsages(anchor, node)
    if (scoped.nonEmpty) return scoped

    val direct = receiverCallUsages(anchor, node)
    if (direct.nonEmpty) return direct

    receiverFallbackUsages(anchor, node)
  }
}

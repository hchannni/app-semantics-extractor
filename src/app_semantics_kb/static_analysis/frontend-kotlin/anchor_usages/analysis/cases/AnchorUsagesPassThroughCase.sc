import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*

object AnchorUsagesPassThroughCase {
  import AnchorUsagesModel.*

  private def returnUsages(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    if (anchor.usageType != "RETURN") return Nil

    AnchorUsagesCaseSupport.preferredCarrierCall(node)
      .map(carrier => AnchorUsagesCaseSupport.buildUsage(anchor, UsageKind.Delegate, carrier))
      .toList
  }

  private def argumentPassThroughUsages(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    val eligibleUsageType = anchor.usageType == "ASSIGNMENT" || anchor.usageType == "CHAINING"
    if (!eligibleUsageType) return Nil

    val usages = scala.collection.mutable.ListBuffer[SemanticUsage]()

    AnchorUsagesCaseSupport.preferredCarrierCall(node).foreach { call =>
      if (!AnchorUsagesCaseSupport.isOperatorCall(call) && AnchorUsagesCaseSupport.isWithinArgument(call, node)) {
        usages += AnchorUsagesCaseSupport.buildUsage(anchor, UsageKind.Delegate, call)

        val calleeTargets = AnchorUsagesCaseSupport.interProceduralArgumentTargets(node, call)
        calleeTargets.foreach { calleeNode =>
          AnchorUsagesCaseSupport.preferredCarrierCall(calleeNode).foreach { calleeCarrier =>
            usages += AnchorUsagesCaseSupport.buildUsage(anchor, UsageKind.Delegate, calleeCarrier)
          }
        }
      }
    }

    usages.toList
  }

  def detect(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] =
    (returnUsages(anchor, node) ++ argumentPassThroughUsages(anchor, node)).distinctBy { usage =>
      val methodName = Option(usage.usageMethodFullName).filter(_.nonEmpty).getOrElse(usage.methodFullName)
      (usage.nodeId, usage.usageKind.toString, methodName, Option(usage.code).getOrElse(""))
    }
}

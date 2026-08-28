import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

object AnchorUsagesFieldInstanceCase {
  import AnchorUsagesModel.*
  import AnchorUsagesUiSignals.*

  private def directFieldAccessUsages(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    val usages = scala.collection.mutable.ListBuffer[SemanticUsage]()

    AnchorUsagesCaseSupport.propertyAccessOf(node).foreach { case (fieldAccess, propNameOpt) =>
      if (AnchorUsagesCaseSupport.isViewFieldAccess(fieldAccess, anchor)) {
        val carrier = AnchorUsagesCaseSupport.statementCallOf(node)
        val parent = AnchorUsagesCaseSupport.effectiveParent(fieldAccess).collect { case c: Call => c }
        val isSetterAssignment =
          parent.exists(p => p.name == "<operator>.assignment" && p.argumentOption(1).exists(lhs => lhs.id == fieldAccess.id))

        val sig = classifyPropertyAccess(propNameOpt, isWrite = isSetterAssignment)
        val kind = usageKindOf(sig)
        val effectiveCarrier = if (isSetterAssignment && parent.isDefined) parent else carrier

        effectiveCarrier.foreach { call =>
          usages += AnchorUsagesCaseSupport.buildUsage(anchor, kind, call)
        }
      }
    }

    usages.toList
  }

  private def nestedFieldAccessUsages(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    val usages = scala.collection.mutable.ListBuffer[SemanticUsage]()

    AnchorUsagesCaseSupport.propertyAccessOf(node).foreach { case (innerFieldAccess, innerPropNameOpt) =>
      AnchorUsagesCaseSupport.outerFieldAccessOf(innerFieldAccess).foreach { outerFa =>
        if (AnchorUsagesCaseSupport.isViewFieldAccess(outerFa, anchor)) {
          val outerPropNameOpt = outerFa.argument(2).collect { case fi: FieldIdentifier => fi.canonicalName }.headOption
          AnchorUsagesCaseSupport.assignmentForLhs(outerFa).foreach { assign =>
            if (AnchorUsagesCaseSupport.sameEnclosingMethod(assign, node) && innerPropNameOpt.exists(_.nonEmpty)) {
              val sig = classifyPropertyAccess(outerPropNameOpt, isWrite = true)
              val kind = usageKindOf(sig)
              usages += AnchorUsagesCaseSupport.buildUsage(anchor, kind, assign)
            }
          }
        }
      }
    }

    usages.toList
  }

  def detect(anchor: ViewAnchor, node: AstNode)(implicit cpg: Cpg): List[SemanticUsage] = {
    val direct = directFieldAccessUsages(anchor, node)
    if (direct.nonEmpty) direct
    else nestedFieldAccessUsages(anchor, node)
  }
}

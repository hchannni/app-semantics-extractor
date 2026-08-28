import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

import JavaViewAnchorModel.JavaViewAnchor
import JavaViewAnchorModel.JsonId.writeId

object JavaAssignmentDeclRefReport {
  import JavaAssignmentDeclRefCore.*
  import JavaAssignmentDeclRefExpansion.referencesForDeclaration

  private val indexedAnchorPattern = """^([A-Za-z_$][\w$]*)\[(.+)\]$""".r

  private def regexQuote(value: String): String =
    java.util.regex.Pattern.quote(value)

  private def contextualCode(node: AstNode): String = {
    val parent = astParentOf(node)
    val grandparent = parent.flatMap(astParentOf)
    val candidate = (parent, grandparent) match {
      case (Some(parentCall: Call), Some(grandCall: Call))
          if Option(grandCall.name).contains("<operator>.indexAccess") =>
        Some(grandCall)
      case (Some(parentCall: Call), _) if Option(parentCall.name).contains("<operator>.indexAccess") =>
        Some(parentCall)
      case (Some(parentCall: Call), Some(grandCall: Call))
          if Option(parentCall.name).contains("<operator>.fieldAccess") && Option(grandCall.code).exists(_.contains(parentCall.code)) =>
        Some(grandCall)
      case (Some(parentNode), _) =>
        Some(parentNode)
      case _ =>
        Some(node)
    }
    candidate.map(nodeCode).getOrElse(nodeCode(node))
  }

  private def indexedAnchor(anchor: JavaViewAnchor): Option[(String, String)] =
    anchor.anchorName.flatMap {
      case indexedAnchorPattern(base, index) => Some(base -> index)
      case _ => None
    }

  private def matchesIndexedAnchor(node: AstNode, base: String, index: String): Boolean = {
    val code = contextualCode(node).replace("this.", "")
    val exactIndex = s"""\\b${regexQuote(base)}\\s*\\[\\s*${regexQuote(index)}\\s*\\]""".r
    val dynamicLoopIndex = s"""\\b${regexQuote(base)}\\s*\\[\\s*i\\s*\\]""".r
    exactIndex.findFirstIn(code).nonEmpty || dynamicLoopIndex.findFirstIn(code).nonEmpty
  }

  private def referencesForAnchor(anchor: JavaViewAnchor, declaration: Declaration)(implicit cpg: Cpg): List[AstNode] = {
    val refs = referencesForDeclaration(declaration)
    indexedAnchor(anchor) match {
      case Some((base, index)) => refs.filter(ref => matchesIndexedAnchor(ref, base, index))
      case None => refs
    }
  }

  def referenceToJson(node: AstNode)(implicit cpg: Cpg): ujson.Obj =
    ujson.Obj(
      "nodeId" -> writeId(node.id),
      "nodeLabel" -> node.label,
      "code" -> contextualCode(node),
      "location" -> nodeLocation(node),
      "methodFullName" -> enclosingMethodFullName(node)
    )

  def declarationToJson(anchor: JavaViewAnchor, decl: Declaration)(implicit cpg: Cpg): ujson.Obj = {
    val declNode = decl.asInstanceOf[AstNode]
    val references = referencesForAnchor(anchor, decl).map(referenceToJson)
    ujson.Obj(
      "nodeId" -> writeId(decl.id),
      "nodeLabel" -> decl.label,
      "code" -> nodeCode(declNode),
      "location" -> nodeLocation(declNode),
      "methodFullName" -> enclosingMethodFullName(declNode),
      "references" -> ujson.Arr(references: _*)
    )
  }

  private def declarationScopeOf(declaration: Declaration): Option[String] =
    declaration match {
      case _: Member => Some("MEMBER")
      case _: Local => Some("LOCAL")
      case _: MethodParameterIn => Some("PARAMETER")
      case _ => Some("UNKNOWN")
    }

  def anchorSummary(
    anchor: JavaViewAnchor,
    target: Option[AstNode],
    declaration: Option[Declaration] = None
  ): ujson.Obj =
    ujson.Obj(
      "cpg_node_id" -> writeId(anchor.cpgNodeId),
      "cpg_node_type" -> anchor.cpgNodeType,
      "usage_type" -> anchor.usageType,
      "resource_id" -> anchor.resourceId,
      "view_type" -> anchor.viewType,
      "location" -> anchor.location,
      "code" -> anchor.code,
      "anchor_name" -> anchor.anchorName
        .orElse(target.flatMap(node => Option(node.code)))
        .map(ujson.Str(_))
        .getOrElse(ujson.Null),
      "declaration_scope" -> anchor.declarationScope
        .orElse(declaration.flatMap(declarationScopeOf))
        .map(ujson.Str(_))
        .getOrElse(ujson.Null)
    )

  def assignmentPayload(anchor: JavaViewAnchor)(implicit cpg: Cpg): Option[ujson.Obj] =
    resolveDeclarationForAnchor(anchor).map { case (target, declaration) =>
      ujson.Obj(
        "anchor" -> anchorSummary(anchor, Some(target), Some(declaration)),
        "declarations" -> ujson.Arr(declarationToJson(anchor, declaration))
      )
    }

  def writeJson(outputPath: String, runsDir: String, data: String): Unit =
    JavaOutputPathGuard.writeJson(outputPath, runsDir, data)
}

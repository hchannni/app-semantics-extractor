import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*

import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Paths}

import ViewAnchorContract.ViewAnchor
import ViewAnchorContract.JsonId.writeId

object AssignmentDeclRefReport {
  import AssignmentDeclRefCore.*
  import AssignmentDeclRefExpansion.referencesForDeclaration

  def referenceToJson(node: AstNode)(implicit cpg: Cpg): ujson.Obj =
    ujson.Obj(
      "nodeId" -> node.id,
      "nodeLabel" -> node.label,
      "code" -> nodeCode(node.astParent),
      "location" -> nodeLocation(node),
      "methodFullName" -> enclosingMethodFullName(node)
    )

  def declarationToJson(decl: Declaration)(implicit cpg: Cpg): ujson.Obj = {
    val references = referencesForDeclaration(decl)
    val refJson = references.map(referenceToJson)
    ujson.Obj(
      "nodeId" -> decl.id,
      "nodeLabel" -> decl.label,
      "code" -> nodeCode(decl.asInstanceOf[AstNode]),
      "location" -> nodeLocation(decl.asInstanceOf[AstNode]),
      "methodFullName" -> enclosingMethodFullName(decl.asInstanceOf[AstNode]),
      "references" -> ujson.Arr(refJson: _*)
    )
  }

  def anchorSummary(anchor: ViewAnchor, identifierOpt: Option[Identifier]): ujson.Obj =
    ujson.Obj(
      "cpg_node_id" -> writeId(anchor.cpgNodeId),
      "cpg_node_type" -> anchor.cpgNodeType,
      "usage_type" -> anchor.usageType,
      "resource_id" -> anchor.resourceId,
      "view_type" -> anchor.viewType,
      "location" -> anchor.location,
      "code" -> anchor.code,
      "anchor_name" -> anchor.anchorName
        .orElse(identifierOpt.map(_.name))
        .map(ujson.Str)
        .getOrElse(ujson.Null),
      "declaration_scope" -> anchor.declarationScope.map(ujson.Str).getOrElse(ujson.Null)
    )

  def assignmentPayload(anchor: ViewAnchor)(implicit cpg: Cpg): Option[ujson.Obj] =
    resolveDeclarationForAnchor(anchor).map { case (identifier, declaration) =>
      ujson.Obj(
        "anchor" -> anchorSummary(anchor, Some(identifier)),
        "declarations" -> ujson.Arr(declarationToJson(declaration))
      )
    }

  def writeJson(outputPath: String, data: String): Unit = {
    val path = Paths.get(outputPath)
    Option(path.getParent).foreach(parent => Files.createDirectories(parent))
    Files.write(path, data.getBytes(StandardCharsets.UTF_8))
  }
}
